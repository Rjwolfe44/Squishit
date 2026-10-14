"""Real FFmpeg clip encodes through ``VideoCompressor``.

``test_trim_engine.py`` pins the argv. These runs prove FFmpeg accepts it and
the files come out the right length: separate clips, a join (with and without
audio), an audio-only join, progress, target size and never-overwrite. They
use the same ffmpeg/ffprobe the app finds and skip when it has none (CI
requires one). Software H.264 only, so no GPU is needed.
"""

import json
import os
import subprocess

import pytest

from video_compressor.core.clips import Clip, ClipMode, ClipPlan, expand_clip_jobs
from video_compressor.core.compressor import JobStatus, VideoCompressor
from video_compressor.core.profiles import CompressionProfile, build_quick_compress_profiles

_TIMEOUT_SECONDS = 60
_TOLERANCE = 0.15
_CLIPS = ((1.0, 3.0), (5.0, 6.5))


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _run(cmd):
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_SECONDS,
        check=False,
        creationflags=_creationflags(),
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


@pytest.fixture(scope="module")
def compressor():
    engine = VideoCompressor()
    for tool in (engine.ffmpeg_path, engine.ffprobe_path):
        try:
            works = subprocess.run(
                [tool, "-version"],
                capture_output=True,
                timeout=_TIMEOUT_SECONDS,
                creationflags=_creationflags(),
            ).returncode == 0
        except (OSError, subprocess.SubprocessError):
            works = False
        if not works:
            if os.environ.get("CI"):
                pytest.fail(f"{tool} is required in CI but does not run")
            pytest.skip(f"{tool} is not available; install FFmpeg to run the clip smoke tests")
    return engine


def _make_source(compressor, path, *, audio=True, size="160x120", noise=False):
    video = f"testsrc2=size={size}:rate=15:duration=10"
    if noise:
        video += ",noise=alls=60:allf=t+u"
    cmd = [
        compressor.ffmpeg_path, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", video,
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=10"]
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "12" if noise else "23", "-pix_fmt", "yuv420p"]
    cmd += ["-c:a", "aac", "-b:a", "96k", "-shortest"] if audio else ["-an"]
    cmd.append(str(path))
    _run(cmd)
    return path


def _probe(compressor, path):
    data = json.loads(_run([
        compressor.ffprobe_path, "-v", "error",
        "-show_entries", "stream=codec_type,duration:format=duration",
        "-of", "json", str(path),
    ]))
    streams = {}
    for stream in data.get("streams", []):
        duration = stream.get("duration")
        streams.setdefault(stream["codec_type"], float(duration) if duration not in (None, "N/A") else None)
    return float(data["format"]["duration"]), streams


def _lite() -> CompressionProfile:
    profile = CompressionProfile.from_dict(build_quick_compress_profiles()["Quick Lite"].to_dict())
    profile.use_hw_accel = False
    return profile


def _compress(compressor, source, output, profile, **kwargs):
    result = compressor.compress(source, output, profile, **kwargs)
    assert result.success, result.error_message
    assert result.output_file is not None and result.output_file.is_file()
    return result


@pytest.mark.smoke
def test_separate_clips_become_files_of_the_clip_length(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "source.mp4")
    plan = ClipPlan(tuple(Clip(start, end) for start, end in _CLIPS), ClipMode.SEPARATE)

    durations = []
    for job in expand_clip_jobs(plan):
        result = _compress(
            compressor,
            source,
            tmp_path / f"source_{job.token}.mp4",
            _lite(),
            job_id=f"sep-{job.token}",
            clips=job.plan,
        )
        total, streams = _probe(compressor, result.output_file)
        assert set(streams) == {"video", "audio"}
        durations.append(total)
        assert result.duration_seconds == pytest.approx(job.plan.total_duration)

    assert durations[0] == pytest.approx(2.0, abs=_TOLERANCE)
    assert durations[1] == pytest.approx(1.5, abs=_TOLERANCE)


@pytest.mark.smoke
def test_join_is_one_file_with_audio_in_sync_and_full_progress(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "source.mp4")
    plan = ClipPlan(tuple(Clip(start, end) for start, end in _CLIPS), ClipMode.JOIN)
    seen = []
    compressor.set_progress_callback(lambda job: seen.append((job.id, job.status, job.progress)))
    try:
        result = _compress(compressor, source, tmp_path / "joined.mp4", _lite(), job_id="join", clips=plan)
    finally:
        compressor.set_progress_callback(None)

    total, streams = _probe(compressor, result.output_file)
    assert total == pytest.approx(3.5, abs=_TOLERANCE)
    assert set(streams) == {"video", "audio"}
    assert streams["video"] == pytest.approx(3.5, abs=_TOLERANCE)
    assert abs(streams["video"] - streams["audio"]) <= _TOLERANCE
    # Progress is measured against the 3.5 s join, not the 10 s source.
    encoding = [progress for job_id, status, progress in seen if job_id == "join" and status is JobStatus.COMPRESSING]
    assert encoding and max(encoding) >= 95


@pytest.mark.smoke
def test_join_of_a_video_without_audio_has_no_audio_stream(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "silent.mp4", audio=False)
    plan = ClipPlan(tuple(Clip(start, end) for start, end in _CLIPS), ClipMode.JOIN)

    result = _compress(compressor, source, tmp_path / "silent-joined.mp4", _lite(), job_id="silent", clips=plan)

    total, streams = _probe(compressor, result.output_file)
    assert set(streams) == {"video"}
    assert total == pytest.approx(3.5, abs=_TOLERANCE)


@pytest.mark.smoke
def test_audio_only_join_saves_the_joined_soundtrack(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "source.mp4")
    plan = ClipPlan(tuple(Clip(start, end) for start, end in _CLIPS), ClipMode.JOIN)
    profile = _lite()
    profile.output_mode = "audio"

    result = _compress(compressor, source, tmp_path / "soundtrack.mp4", profile, job_id="audio", clips=plan)

    assert result.output_file.suffix == ".m4a"
    total, streams = _probe(compressor, result.output_file)
    assert set(streams) == {"audio"}
    assert total == pytest.approx(3.5, abs=_TOLERANCE)


@pytest.mark.smoke
def test_target_size_on_a_clip_lands_near_the_target(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "noisy.mp4", size="320x240", noise=True)
    assert source.stat().st_size > 2_000_000, "the noisy source should be well above the target"
    profile = _lite()
    profile.target_size_mb = 1
    profile.target_size_mode = "strict"

    result = _compress(
        compressor,
        source,
        tmp_path / "noisy-clip.mp4",
        profile,
        job_id="target",
        clips=ClipPlan((Clip(1.0, 7.0),)),
    )

    total, _ = _probe(compressor, result.output_file)
    assert total == pytest.approx(6.0, abs=_TOLERANCE)
    assert result.skipped is False
    assert result.compressed_size == pytest.approx(1_000_000, rel=0.2), result.note


@pytest.mark.smoke
def test_exact_two_pass_join_hits_the_target(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "noisy.mp4", size="320x240", noise=True)
    profile = _lite()
    profile.target_size_mb = 1
    profile.target_size_mode = "exact"
    profile.exact_two_pass = True
    plan = ClipPlan((Clip(0.5, 3.5), Clip(5.0, 8.0)), ClipMode.JOIN)

    result = _compress(compressor, source, tmp_path / "exact-join.mp4", profile, job_id="exact", clips=plan)

    total, streams = _probe(compressor, result.output_file)
    assert "video" in streams and "audio" in streams
    assert total == pytest.approx(6.0, abs=_TOLERANCE)
    assert result.compressed_size <= 1_000_000, result.note


@pytest.mark.smoke
def test_rerun_without_overwrite_adds_a_number(compressor, tmp_path):
    source = _make_source(compressor, tmp_path / "talk.mp4")
    plan = ClipPlan((Clip(1.0, 2.0),))

    first = _compress(compressor, source, tmp_path / "talk_clip01.mp4", _lite(), clips=plan, overwrite=False)
    first_bytes = first.output_file.read_bytes()
    second = _compress(compressor, source, tmp_path / "talk_clip01.mp4", _lite(), clips=plan, overwrite=False)

    assert first.output_file == tmp_path / "talk_clip01.mp4"
    assert second.output_file == tmp_path / "talk_clip01 (2).mp4"
    assert first.output_file.read_bytes() == first_bytes
