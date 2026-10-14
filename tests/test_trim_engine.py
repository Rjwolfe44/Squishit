"""Trim-aware engine: clip argv, joins, effective-duration math, never-overwrite.

No binaries are needed: ffmpeg/ffprobe lookups are stubbed and encodes are
faked, like ``test_target_size.py``. ``test_trim_smoke.py`` runs the real thing.
"""

import json
import platform
import threading
from pathlib import Path

import pytest

from video_compressor.core import compressor as compressor_module
from video_compressor.core.clips import MAX_JOIN_CLIPS, Clip, ClipMode, ClipPlan
from video_compressor.core.codecs import AudioCodec, VideoCodec, VideoContainer
from video_compressor.core.compressor import (
    CompressionJob,
    CompressionResult,
    VideoCompressor,
    VideoInfo,
)
from video_compressor.core.profiles import CompressionProfile, ProfileType, build_target_size_plan

NULL_DEVICE = "NUL" if platform.system() == "Windows" else "/dev/null"


def _compressor(monkeypatch):
    monkeypatch.setattr(VideoCompressor, "_find_ffmpeg", lambda self: "ffmpeg")
    monkeypatch.setattr(VideoCompressor, "_find_ffprobe", lambda self: "ffprobe")
    monkeypatch.setattr(VideoCompressor, "_find_cjxl", lambda self: None)
    monkeypatch.setattr(VideoCompressor, "_get_thread_count", lambda *args, **kwargs: 4)
    compressor = VideoCompressor()
    compressor.codec_manager._available_encoders = {"libx264", "libx265", "aac", "libopus"}
    return compressor


def _info(path, **changes):
    values = dict(
        filepath=path,
        duration=60.0,
        size=60_000_000,
        width=1920,
        height=1080,
        fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        video_bitrate=7_800_000,
        audio_bitrate=128_000,
        total_bitrate=8_000_000,
        frame_count=1800,
    )
    values.update(changes)
    return VideoInfo(**values)


def _profile(**changes):
    values = dict(
        name="Trim Test",
        profile_type=ProfileType.CUSTOM,
        video_codec=VideoCodec.H264,
        audio_codec=AudioCodec.AAC,
        audio_bitrate=128_000,
        use_hw_accel=False,
    )
    values.update(changes)
    return CompressionProfile(**values)


def _job(tmp_path, profile, *, plan=None, info=None, output="out.mp4"):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"0")
    job = CompressionJob(
        id="trim-job",
        input_file=source,
        output_file=tmp_path / output,
        profile=profile,
    )
    job.video_info = info or _info(source)
    job.clip_plan = plan
    return job


def _join(*ranges):
    return ClipPlan(tuple(Clip(start, end) for start, end in ranges), ClipMode.JOIN)


def _after(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def _graph(cmd):
    return _after(cmd, "-filter_complex")


def _maps(cmd):
    return [cmd[i + 1] for i, value in enumerate(cmd) if value == "-map"]


# --- argv ----------------------------------------------------------------------


def test_no_plan_keeps_the_argv_byte_for_byte(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(max_resolution=720, frame_rate=24)
    job = _job(tmp_path, profile)
    # compress() always records the source; no clips means no plan.
    job.source_info = job.video_info

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    assert cmd == [
        "ffmpeg", "-y", "-hide_banner",
        "-i", str(job.input_file),
        "-c:v", "libx264", "-crf", "23", "-preset", "medium", "-threads", "4",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128000",
        "-vf", "scale=-2:720",
        "-r", "24",
        "-movflags", "+faststart",
        str(job.output_file),
    ]


def test_legacy_profile_trim_seeks_on_the_input_side(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(trim_enabled=True, trim_start="0:05", trim_end=12.5)
    job = _job(tmp_path, profile)

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    assert cmd[:9] == [
        "ffmpeg", "-y", "-hide_banner",
        "-ss", "5.000", "-t", "7.500",
        "-i", str(job.input_file),
    ]
    assert cmd.count("-ss") == 1
    assert "-filter_complex" not in cmd


@pytest.mark.parametrize(
    ("clip", "expected"),
    [
        (Clip(1.5, 4.0), ["-ss", "1.500", "-t", "2.500"]),
        (Clip(0.0, 4.0), ["-t", "4.000"]),
        (Clip(30.0, float("inf")), ["-ss", "30.000"]),
    ],
)
def test_one_clip_seeks_before_the_input(monkeypatch, tmp_path, clip, expected):
    compressor = _compressor(monkeypatch)
    profile = _profile()
    job = _job(tmp_path, profile, plan=ClipPlan((clip,)))

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    assert cmd[3:3 + len(expected)] == expected
    assert cmd[3 + len(expected):5 + len(expected)] == ["-i", str(job.input_file)]
    assert cmd.count("-i") == 1
    assert "-filter_complex" not in cmd


def test_join_builds_one_concat_graph_with_audio(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(max_resolution=720, frame_rate=24)
    job = _job(tmp_path, profile, plan=_join((1, 3), (5, 6.5), (10, 12)))

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    source = str(job.input_file)
    assert cmd[3:25] == [
        "-ss", "1.000", "-t", "2.000", "-i", source,
        "-ss", "5.000", "-t", "1.500", "-i", source,
        "-ss", "10.000", "-t", "2.000", "-i", source,
        "-filter_complex",
        "[0:v:0][0:a:0][1:v:0][1:a:0][2:v:0][2:a:0]concat=n=3:v=1:a=1[vcat][acat];"
        "[vcat]scale=-2:720[vout]",
        "-map", "[vout]",
    ]
    assert _maps(cmd) == ["[vout]", "[acat]"]
    assert cmd.index("-filter_complex") < cmd.index("-c:v")
    assert "-vf" not in cmd
    assert _after(cmd, "-r") == "24"
    assert _after(cmd, "-movflags") == "+faststart"
    assert _after(cmd, "-c:a") == "aac"
    assert cmd[-1] == str(job.output_file)


def test_join_places_decode_hwaccel_on_every_input(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    monkeypatch.setattr(compressor_module.platform, "system", lambda: "Windows")
    profile = _profile(video_codec=VideoCodec.SVT_AV1, video_container="mkv")
    job = _job(tmp_path, profile, plan=_join((1, 3), (5, 6.5)), output="out.mkv")

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    source = str(job.input_file)
    assert cmd[3:17] == [
        "-hwaccel", "d3d11va", "-ss", "1.000", "-t", "2.000", "-i", source,
        "-hwaccel", "d3d11va", "-ss", "5.000", "-t", "1.500",
    ]
    assert cmd[17:19] == ["-i", source]


@pytest.mark.parametrize(
    "changes",
    [
        {"info": {"audio_codec": ""}},  # the source has no audio
        {"profile": {"disable_audio": True}},
        {"profile": {"audio_mode": "strip"}},
        {"profile": {"extra_ffmpeg_args": "-an"}},
    ],
)
def test_join_without_audio_maps_video_only(monkeypatch, tmp_path, changes):
    compressor = _compressor(monkeypatch)
    profile = _profile(**changes.get("profile", {}))
    source = tmp_path / "source.mp4"
    job = _job(
        tmp_path,
        profile,
        plan=_join((0, 2), (4, 6)),
        info=_info(source, **changes.get("info", {})),
    )

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    assert _graph(cmd) == "[0:v:0][1:v:0]concat=n=2:v=1:a=0[vcat]"
    assert _maps(cmd) == ["[vcat]"]


def test_join_reencodes_audio_the_profile_would_copy(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(audio_mode="copy")

    joined = compressor._build_video_ffmpeg_command(
        _job(tmp_path, profile, plan=_join((0, 2), (4, 6))),
        profile,
    )
    single = compressor._build_video_ffmpeg_command(
        _job(tmp_path, profile, plan=ClipPlan((Clip(0, 2),))),
        profile,
    )

    assert _after(joined, "-c:a") == "aac"
    assert "copy" not in joined
    # One clip has no filter, so stream copy still works there.
    assert _after(single, "-c:a") == "copy"


def test_join_folds_user_filters_into_the_graph(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(
        max_resolution=720,
        extra_ffmpeg_args='-vf "hflip" -af volume=2 -tune film',
    )
    job = _job(tmp_path, profile, plan=_join((0, 2), (4, 6)))

    cmd = compressor._build_video_ffmpeg_command(job, profile)

    assert _graph(cmd) == (
        "[0:v:0][0:a:0][1:v:0][1:a:0]concat=n=2:v=1:a=1[vcat][acat];"
        "[vcat]hflip,scale=-2:720[vout];[acat]volume=2[aout]"
    )
    assert _maps(cmd) == ["[vout]", "[aout]"]
    assert "-vf" not in cmd and "-af" not in cmd
    assert cmd[-3:] == ["-tune", "film", str(job.output_file)]


def test_join_two_pass_first_pass_carries_no_audio(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(exact_two_pass=True, target_size_mb=5, target_size_mode="exact")
    job = _job(tmp_path, profile, plan=_join((0, 2), (4, 6)))
    plan = build_target_size_plan(
        target_size_mb=5,
        duration_seconds=4.0,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.H264,
        mode="exact",
    )
    # Hold a slot the way compress() does; each build re-reserves it.
    compressor._reserve_encode_slot(job, profile, codec="h264", hw_encoder=None)

    first, second = compressor._build_two_pass_commands(
        job,
        profile,
        target_plan=plan,
        target_video_bitrate=900_000,
        passlogfile=tmp_path / "pass",
    )

    assert _graph(first) == "[0:v:0][1:v:0]concat=n=2:v=1:a=0[vcat]"
    assert _maps(first) == ["[vcat]"]
    assert first[-8:] == [
        "-an", "-pass", "1", "-passlogfile", str(tmp_path / "pass"), "-f", "null", NULL_DEVICE,
    ]
    assert "-movflags" not in first
    assert _graph(second).endswith("concat=n=2:v=1:a=1[vcat][acat]")
    assert _maps(second) == ["[vcat]", "[acat]"]
    assert second[-5:-1] == ["-pass", "2", "-passlogfile", str(tmp_path / "pass")]
    assert second[-1] == str(job.output_file)
    assert job.video_only_pass is False
    # Building twice re-reserved the same slot instead of taking a second one.
    assert compressor.governor.holds(job.id)
    assert compressor.governor.active_count() == 1
    compressor.governor.release(job.id)


def test_two_pass_without_a_join_is_unchanged(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(exact_two_pass=True)
    job = _job(tmp_path, profile)
    job.source_info = job.video_info
    plan = build_target_size_plan(
        target_size_mb=5,
        duration_seconds=60.0,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.H264,
        mode="exact",
    )

    first, second = compressor._build_two_pass_commands(
        job,
        profile,
        target_plan=plan,
        target_video_bitrate=900_000,
        passlogfile=tmp_path / "pass",
    )
    common = second[:-5]
    movflags = common.index("-movflags")

    assert "-filter_complex" not in second
    assert first == [
        *common[:movflags],
        *common[movflags + 2:],
        "-an", "-pass", "1", "-passlogfile", str(tmp_path / "pass"), "-f", "null", NULL_DEVICE,
    ]


def test_audio_only_join_concatenates_audio(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(output_mode="audio", extra_ffmpeg_args="-vf hflip -af volume=2")
    job = _job(tmp_path, profile, plan=_join((1, 3), (5, 6.5)), output="out.m4a")

    cmd = compressor._build_audio_ffmpeg_command(job, profile)

    source = str(job.input_file)
    assert cmd[3:15] == [
        "-ss", "1.000", "-t", "2.000", "-i", source,
        "-ss", "5.000", "-t", "1.500", "-i", source,
    ]
    assert _graph(cmd) == "[0:a:0][1:a:0]concat=n=2:v=0:a=1[acat];[acat]volume=2[aout]"
    assert _maps(cmd) == ["[aout]"]
    assert "-vn" in cmd and "-vf" not in cmd and "-af" not in cmd


def test_audio_only_single_clip_seeks_without_a_graph(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(output_mode="audio")
    job = _job(tmp_path, profile, plan=ClipPlan((Clip(2, 5),)), output="out.m4a")

    cmd = compressor._build_audio_ffmpeg_command(job, profile)

    assert cmd[3:9] == ["-ss", "2.000", "-t", "3.000", "-i", str(job.input_file)]
    assert "-filter_complex" not in cmd


def test_audio_only_clip_of_a_silent_video_is_a_clear_error(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(output_mode="audio")
    source = tmp_path / "source.mp4"
    job = _job(
        tmp_path,
        profile,
        plan=ClipPlan((Clip(2, 5),)),
        info=_info(source, audio_codec=""),
        output="out.m4a",
    )

    with pytest.raises(ValueError, match="no audio track"):
        compressor._build_audio_ffmpeg_command(job, profile)


def test_join_over_the_cap_and_multi_clip_separate_plans_are_rejected(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile()
    too_many = _join(*[(i * 2, i * 2 + 1) for i in range(MAX_JOIN_CLIPS + 1)])
    separate = ClipPlan((Clip(0, 1), Clip(2, 3)), ClipMode.SEPARATE)

    with pytest.raises(ValueError, match=f"up to {MAX_JOIN_CLIPS} clips"):
        compressor._build_video_ffmpeg_command(_job(tmp_path, profile, plan=too_many), profile)
    with pytest.raises(ValueError, match="one job per clip"):
        compressor._build_video_ffmpeg_command(_job(tmp_path, profile, plan=separate), profile)


# --- compress(): effective numbers, guards ----------------------------------------


def _run_compress(
    compressor,
    tmp_path,
    *,
    profile,
    info,
    output_size,
    clips=None,
    overwrite=True,
    output_name="out.mp4",
):
    """compress() with a stubbed probe and a fake encode that writes ``output_size`` bytes."""

    source = info.filepath
    if not source.exists():
        source.write_bytes(b"0" * 1000)
    seen = {"commands": [], "durations": [], "total_frames": []}

    def fake_run(cmd, *, job, job_id, media_info, start_time, **kwargs):
        seen["commands"].append(list(cmd))
        seen["durations"].append(media_info.duration)
        seen["total_frames"].append(job.total_frames)
        job.output_file.write_bytes(b"0" * output_size)
        return 0, []

    compressor._run_ffmpeg_process = fake_run
    compressor.analyze_media = lambda path: info
    result = compressor.compress(
        source,
        tmp_path / output_name,
        profile,
        job_id="trim-run",
        clips=clips,
        overwrite=overwrite,
    )
    return result, seen


def test_clip_job_plans_progress_and_target_size_on_the_clip(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    monkeypatch.setattr(
        compressor.codec_manager,
        "get_compatible_container",
        lambda codec, container: VideoContainer.MP4,
    )
    source = tmp_path / "long.mp4"
    info = _info(source, duration=100.0, size=100_000_000, frame_count=3000)
    profile = _profile(target_size_mb=5, target_size_mode="strict")
    seen_plans = []
    original_resolve = compressor._resolve_target_size_plan

    def spy_resolve(profile_arg, media_info):
        plan = original_resolve(profile_arg, media_info)
        seen_plans.append((media_info.duration, plan))
        return plan

    compressor._resolve_target_size_plan = spy_resolve

    result, seen = _run_compress(
        compressor,
        tmp_path,
        profile=profile,
        info=info,
        output_size=5_000_000,
        clips=ClipPlan((Clip(10, 40),)),
    )

    assert result.success, result.error_message
    assert seen["durations"] == [30.0]
    assert seen["total_frames"] == [900]
    assert {duration for duration, _ in seen_plans} == {30.0}
    expected = build_target_size_plan(
        target_size_mb=5,
        duration_seconds=30.0,
        audio_bitrate=128_000,
        container="mp4",
        codec=VideoCodec.H264,
        mode="strict",
    )
    cmd = seen["commands"][0]
    assert _after(cmd, "-b:v") == str(expected.video_bitrate)
    assert cmd[3:9] == ["-ss", "10.000", "-t", "30.000", "-i", str(source)]
    assert result.duration_seconds == 30.0
    assert result.original_size == 30_000_000
    assert result.average_bitrate == int(5_000_000 * 8 / 30.0)
    job = compressor.get_job("trim-run")
    assert job.clip_plan == ClipPlan((Clip(10, 40),))
    assert job.source_info.duration == 100.0
    assert job.video_info.duration == 30.0


class _FakeProcess:
    """Popen stand-in that prints FFmpeg-style progress and writes the output."""

    def __init__(self, cmd, lines, output_size):
        Path(cmd[-1]).write_bytes(b"0" * output_size)
        self.stderr = iter(lines)
        self.stdout = iter(())
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass


def test_progress_parsing_uses_the_clip_duration(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "long.mp4"
    source.write_bytes(b"0" * 1000)
    compressor.analyze_media = lambda path: _info(source, duration=100.0, size=100_000_000, frame_count=3000)
    lines = [
        "frame=  450 fps= 90 q=28.0 size=     256KiB time=00:00:15.00 bitrate= 140.0kbits/s speed=3.0x\n",
    ]
    monkeypatch.setattr(
        compressor_module.subprocess,
        "Popen",
        lambda cmd, **kwargs: _FakeProcess(cmd, lines, 1_000_000),
    )
    progress = []
    compressor.set_progress_callback(
        lambda job: progress.append((job.status.value, round(job.progress, 2), round(job.eta, 2)))
    )

    result = compressor.compress(
        source,
        tmp_path / "out.mp4",
        _profile(),
        job_id="progress-run",
        clips=ClipPlan((Clip(10, 40),)),
    )

    assert result.success, result.error_message
    compressing = [entry for entry in progress if entry[0] == "compressing" and entry[1] > 0]
    # 15 s of a 30 s clip is half way (99 % span), not 15 % of the 100 s source.
    assert compressing[0][1] == pytest.approx(49.5)
    # 15 s left at 3x speed.
    assert compressing[0][2] == pytest.approx(5.0)


def test_target_not_smaller_than_source_still_encodes_a_clip(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    monkeypatch.setattr(compressor, "_optimize_video_profile", lambda profile, media_info=None: profile)
    source = tmp_path / "small.mp4"
    info = _info(source, duration=120.0, size=2_000_000, frame_count=3600)
    profile = _profile(target_size_mb=3, target_size_mode="strict")

    whole, _ = _run_compress(compressor, tmp_path, profile=profile, info=info, output_size=500_000)
    clipped, seen = _run_compress(
        compressor,
        tmp_path,
        profile=profile,
        info=info,
        output_size=500_000,
        clips=ClipPlan((Clip(10, 70),)),
        output_name="clip.mp4",
    )

    assert whole.skipped is True
    assert clipped.success, clipped.error_message
    assert clipped.skipped is False
    assert clipped.output_file == tmp_path / "clip.mp4"
    assert clipped.output_file.stat().st_size == 500_000
    assert seen["commands"][0][3:7] == ["-ss", "10.000", "-t", "60.000"]


def test_clip_larger_than_its_share_of_the_source_is_kept(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "tiny.mp4"
    info = _info(source, duration=10.0, size=1_000_000, frame_count=300)

    whole, _ = _run_compress(
        compressor, tmp_path, profile=_profile(), info=info, output_size=1_200_000,
        output_name="whole.mp4",
    )
    clipped, _ = _run_compress(
        compressor,
        tmp_path,
        profile=_profile(),
        info=info,
        output_size=800_000,  # more than the 500 KB half of the source
        clips=ClipPlan((Clip(0, 5),)),
        output_name="clip.mp4",
    )

    assert whole.kept_original is True
    assert clipped.success, clipped.error_message
    assert clipped.kept_original is False
    assert clipped.output_file == tmp_path / "clip.mp4"
    assert clipped.compressed_size == 800_000
    assert clipped.original_size == 500_000
    assert "larger than the same part of the original" in clipped.note


def test_join_cap_fails_the_job_with_a_message(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "long.mp4"
    info = _info(source, duration=600.0, size=600_000_000, frame_count=18000)
    too_many = _join(*[(i * 10, i * 10 + 5) for i in range(MAX_JOIN_CLIPS + 1)])

    result, seen = _run_compress(
        compressor, tmp_path, profile=_profile(), info=info, output_size=1, clips=too_many,
    )

    assert result.success is False
    assert f"up to {MAX_JOIN_CLIPS} clips" in result.error_message
    assert seen["commands"] == []


def test_images_ignore_clips(monkeypatch, tmp_path):
    from PIL import Image

    compressor = _compressor(monkeypatch)
    source = tmp_path / "photo.png"
    Image.new("RGB", (64, 48), (200, 30, 30)).save(source)

    result = compressor.compress(
        source,
        tmp_path / "photo-out.webp",
        _profile(image_format="webp"),
        job_id="image-clips",
        clips=_join((0, 1), (2, 3)),
    )

    assert result.success, result.error_message
    assert result.media_type == "image"


def test_compare_sample_trim_seeks_on_the_input_and_reports_the_sample(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "long.mp4"
    info = _info(source, duration=100.0, size=100_000_000, frame_count=3000)
    sample = compressor.build_sample_profile(_profile(), info, sample_seconds=20.0, label="Lite")

    result, seen = _run_compress(compressor, tmp_path, profile=sample, info=info, output_size=1_000_000)

    assert result.success, result.error_message
    assert seen["commands"][0][3:9] == ["-ss", "40.000", "-t", "20.000", "-i", str(source)]
    assert seen["durations"] == [20.0]
    assert seen["total_frames"] == [600]
    assert result.duration_seconds == 20.0


# --- never overwrite --------------------------------------------------------------


def test_claim_output_numbers_and_releases_names(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    existing = tmp_path / "video.mp4"
    existing.write_bytes(b"old")

    assert compressor._claim_output(existing, True, "a") == existing
    assert compressor._reserved_outputs == set()

    first = compressor._claim_output(existing, False, "a")
    second = compressor._claim_output(existing, False, "b")
    assert first == tmp_path / "video (2).mp4"
    assert second == tmp_path / "video (3).mp4"

    # Reservations are case-insensitive, like Windows file names.
    fresh = compressor._claim_output(tmp_path / "clip.mp4", False, "c")
    shouted = compressor._claim_output(tmp_path / "CLIP.mp4", False, "d")
    assert fresh == tmp_path / "clip.mp4"
    assert shouted == tmp_path / "CLIP (2).mp4"

    compressor._release_outputs("a")
    assert compressor._claim_output(existing, False, "e") == tmp_path / "video (2).mp4"
    for job_id in ("b", "c", "d", "e"):
        compressor._release_outputs(job_id)
    assert compressor._reserved_outputs == set()
    assert existing.read_bytes() == b"old"


def test_compress_without_overwrite_writes_a_new_name(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "talk.mp4"
    info = _info(source, duration=10.0, size=10_000_000, frame_count=300)
    (tmp_path / "talk-small.mp4").write_bytes(b"keep me")

    result, seen = _run_compress(
        compressor,
        tmp_path,
        profile=_profile(),
        info=info,
        output_size=1_000_000,
        overwrite=False,
        output_name="talk-small.mp4",
    )

    assert result.success, result.error_message
    assert result.output_file == tmp_path / "talk-small (2).mp4"
    assert seen["commands"][0][-1] == str(tmp_path / "talk-small (2).mp4")
    assert (tmp_path / "talk-small.mp4").read_bytes() == b"keep me"
    assert compressor._reserved_outputs == set()
    assert compressor._job_outputs == {}


def test_output_named_like_the_source_never_replaces_it(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "same.mp4"
    source.write_bytes(b"source bytes")
    info = _info(source, duration=10.0, size=10_000_000, frame_count=300)

    result, _ = _run_compress(
        compressor,
        tmp_path,
        profile=_profile(),
        info=info,
        output_size=1_000_000,
        overwrite=False,
        output_name="same.mp4",
    )

    assert result.output_file == tmp_path / "same (2).mp4"
    assert source.read_bytes() == b"source bytes"


def test_keep_original_without_overwrite_does_not_replace_a_file(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "phone.mov"
    source.write_bytes(b"0" * 1000)
    unrelated = tmp_path / "phone-small.mov"
    unrelated.write_bytes(b"unrelated")
    info = _info(source, duration=10.0, size=1000, frame_count=300)

    result, _ = _run_compress(
        compressor,
        tmp_path,
        profile=_profile(),
        info=info,
        output_size=5000,  # larger than the source, so the source is kept
        overwrite=False,
        output_name="phone-small.mp4",
    )

    assert result.kept_original is True
    assert result.output_file == tmp_path / "phone-small (2).mov"
    assert unrelated.read_bytes() == b"unrelated"
    assert result.output_file.read_bytes() == source.read_bytes()


def test_cancel_before_claim_leaves_an_existing_file_alone(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    existing = tmp_path / "out.mp4"
    existing.write_bytes(b"precious")
    job = CompressionJob(
        id="cancel",
        input_file=tmp_path / "in.mp4",
        output_file=existing,
        profile=_profile(),
        overwrite=False,
    )

    compressor._cancel_result(job)
    assert existing.exists()

    job.overwrite = True
    compressor._cancel_result(job)
    assert not existing.exists()


# --- compress_async ---------------------------------------------------------------


def test_compress_async_forwards_clips_and_overwrite_only_when_set(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    calls = []
    done = threading.Event()

    def legacy_compress(input_file, output_file, profile, job_id=None, parallel_jobs=1):
        calls.append({})
        return CompressionResult(success=True, input_file=Path(input_file))

    compressor.compress = legacy_compress
    compressor.compress_async(tmp_path / "a.mp4", tmp_path / "b.mp4", _profile(), callback=lambda r: done.set())
    assert done.wait(timeout=5)
    assert calls == [{}]

    done.clear()
    plan = ClipPlan((Clip(1, 2),))

    def new_compress(input_file, output_file, profile, job_id=None, parallel_jobs=1, **kwargs):
        calls.append(kwargs)
        return CompressionResult(success=True, input_file=Path(input_file))

    compressor.compress = new_compress
    compressor.compress_async(
        tmp_path / "a.mp4",
        tmp_path / "b.mp4",
        _profile(),
        callback=lambda r: done.set(),
        clips=plan,
        overwrite=False,
    )
    assert done.wait(timeout=5)
    assert calls[-1] == {"clips": plan, "overwrite": False}


# --- probe fields -----------------------------------------------------------------


def _probe_payload(video_extra, with_audio=True):
    streams = [
        {
            "codec_type": "video",
            "codec_name": "h264",
            "width": 1920,
            "height": 1080,
            "r_frame_rate": "30/1",
            "avg_frame_rate": "30000/1001",
            "nb_frames": "300",
            **video_extra,
        }
    ]
    if with_audio:
        streams.append({"codec_type": "audio", "codec_name": "aac", "bit_rate": "128000"})
    return json.dumps({
        "format": {"duration": "10.0", "size": "5000000", "bit_rate": "4000000"},
        "streams": streams,
    })


class _Completed:
    def __init__(self, stdout):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


@pytest.mark.parametrize(
    ("video_extra", "with_audio", "expected"),
    [
        (
            {
                "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}],
                "sample_aspect_ratio": "0:1",
                "pix_fmt": "yuv420p10le",
                "color_transfer": "arib-std-b67",
            },
            True,
            {"rotation": 90, "sar": "1:1", "pix_fmt": "yuv420p10le", "color_transfer": "arib-std-b67", "has_audio": True},
        ),
        (
            {"tags": {"rotate": "270"}, "sample_aspect_ratio": "4:3"},
            False,
            {"rotation": 270, "sar": "4:3", "pix_fmt": "", "color_transfer": "", "has_audio": False},
        ),
        (
            {"side_data_list": [{"rotation": 180}], "sample_aspect_ratio": "N/A"},
            True,
            {"rotation": 180, "sar": "1:1", "has_audio": True},
        ),
    ],
)
def test_analyze_video_reads_the_extra_fields_from_one_probe(
    monkeypatch, tmp_path, video_extra, with_audio, expected
):
    compressor = _compressor(monkeypatch)
    source = tmp_path / "phone.mp4"
    source.write_bytes(b"0" * 100)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _Completed(_probe_payload(video_extra, with_audio))

    monkeypatch.setattr(compressor_module.subprocess, "run", fake_run)

    info = compressor.analyze_video(source)

    assert len(calls) == 1
    assert info.avg_fps == pytest.approx(29.97, abs=0.01)
    assert info.fps == 30.0
    for name, value in expected.items():
        assert getattr(info, name) == value, name


def test_video_info_defaults_and_public_tool_paths(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    info = VideoInfo(filepath=tmp_path / "x.mp4")

    assert (info.rotation, info.sar, info.avg_fps, info.pix_fmt, info.color_transfer) == (0, "1:1", 0.0, "", "")
    assert info.has_audio is False
    assert compressor.ffmpeg_path == "ffmpeg"
    assert compressor.ffprobe_path == "ffprobe"


# --- fixes found in review ------------------------------------------------------


def test_a_quoted_user_filter_keeps_its_quotes_inside_a_join(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(extra_ffmpeg_args="-vf format=pix_fmts='yuv420p'")
    job = _job(tmp_path, profile, plan=_join((0, 5), (10, 15)))

    graph = _graph(compressor._build_video_ffmpeg_command(job, profile))

    assert "format=pix_fmts='yuv420p'" in graph  # not "...'yuv420p" with the end quote eaten


def test_a_user_filter_complex_is_refused_for_a_join_with_a_clear_message(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(extra_ffmpeg_args="-filter_complex [0:v]null[v]")
    job = _job(tmp_path, profile, plan=_join((0, 5), (10, 15)))

    with pytest.raises(ValueError, match="joined clips"):
        compressor._build_video_ffmpeg_command(job, profile)


def test_two_vf_options_behave_as_ffmpeg_does_in_a_join(monkeypatch, tmp_path):
    compressor = _compressor(monkeypatch)
    profile = _profile(extra_ffmpeg_args="-vf hflip -vf vflip")
    job = _job(tmp_path, profile, plan=_join((0, 5), (10, 15)))

    graph = _graph(compressor._build_video_ffmpeg_command(job, profile))

    assert "vflip" in graph and "hflip" not in graph  # the last one wins, like a single clip


def test_a_join_of_repeated_parts_is_measured_at_its_real_length(monkeypatch, tmp_path):
    from video_compressor.core.clips import effective_media_info

    source = tmp_path / "source.mp4"
    info = _info(source, duration=60.0, size=60_000_000)
    plan = _join((0, 50), (0, 50))  # the same 50 seconds twice

    eff = effective_media_info(info, plan)

    assert eff.duration == pytest.approx(100.0)  # not capped at the 60 s source
    assert eff.frame_count == 3000
