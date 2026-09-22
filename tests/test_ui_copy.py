"""UI wording for profiles, quick compress, and progress cards.

These checks stay off the encode path: names, CRF, presets, and codecs for the
built-in profiles stay on the values the ladder tests already lock.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from video_compressor.core.codecs import CodecManager, VideoCodec
from video_compressor.core.descriptions import (
    STREAMING_UPLOAD_DESCRIPTION,
    YOUTUBE_UPLOAD_DESCRIPTION,
)
from video_compressor.core.profiles import ProfileManager
from video_compressor.core.utils import format_time
from video_compressor.gui.copy import (
    DROP_ZONE_NEXT,
    DROP_ZONE_TITLE,
    EMPTY_QUEUE,
    MORE_PROFILES_TOOLTIP,
    PROFILE_HELPER,
    QUICK_COMPRESS_SUBTITLE,
    QUICK_COMPRESS_TITLE,
    WAITING_FOR_SOFTWARE_FALLBACK,
    human_progress_status,
    profile_ui_text,
    progress_detail,
    progress_phase,
    secondary_menu_entries,
)

_FORBIDDEN = re.compile(r"\b(live|rtmp|broadcast)\b", re.IGNORECASE)


def _assert_file_upload_copy(*texts: str) -> None:
    for text in texts:
        assert text
        assert _FORBIDDEN.search(text) is None, text


def test_profile_and_quick_copy_describes_file_upload(tmp_path):
    manager = ProfileManager(config_dir=tmp_path)
    youtube = manager.get_profile("YouTube Upload")
    streaming = manager.get_profile("Streaming")

    assert youtube.name == "YouTube Upload"
    assert youtube.description == YOUTUBE_UPLOAD_DESCRIPTION
    assert youtube.video_codec is VideoCodec.H264
    assert youtube.crf == 23
    assert youtube.preset == "fast"
    assert youtube.video_container == "mp4"

    assert streaming.name == "Streaming"
    assert streaming.description == STREAMING_UPLOAD_DESCRIPTION
    assert streaming.video_codec is VideoCodec.HEVC
    assert streaming.crf == 24
    assert streaming.preset == "veryfast"
    assert streaming.video_container == "mp4"

    youtube_label, youtube_tip = profile_ui_text(
        youtube.name, youtube.description
    )
    streaming_label, streaming_tip = profile_ui_text(
        streaming.name, streaming.description
    )
    assert youtube_label == "YouTube / social upload"
    assert streaming_label == "Streaming upload"
    assert youtube_tip == youtube.description
    assert streaming_tip == streaming.description

    entries = secondary_menu_entries(
        manager.get_all_profiles(),
        ["Fast", "Balanced", "Max / Archival"],
    )
    labels = {label: name for label, name in entries}
    assert labels["YouTube / social upload"] == "YouTube Upload"
    assert labels["Streaming upload"] == "Streaming"
    assert labels["Mobile"] == "Mobile"

    _assert_file_upload_copy(
        PROFILE_HELPER,
        QUICK_COMPRESS_TITLE,
        QUICK_COMPRESS_SUBTITLE,
        DROP_ZONE_TITLE,
        DROP_ZONE_NEXT,
        EMPTY_QUEUE,
        MORE_PROFILES_TOOLTIP,
        WAITING_FOR_SOFTWARE_FALLBACK,
        youtube_label,
        youtube_tip,
        streaming_label,
        streaming_tip,
    )
    assert PROFILE_HELPER == "Full app profiles (incl. Archival Max)."
    assert QUICK_COMPRESS_SUBTITLE == (
        "Explorer one-click · Lite / Balanced / HEVC Max — not the full profile list."
    )
    assert DROP_ZONE_TITLE == "Drop videos or images"
    assert EMPTY_QUEUE == "Add files to start"


def test_codec_recommendation_blurbs_keep_the_same_settings():
    manager = CodecManager()
    upload = manager.get_codec_recommendations("upload")
    streaming = manager.get_codec_recommendations("streaming")

    assert upload["description"] == YOUTUBE_UPLOAD_DESCRIPTION
    assert upload["video_codec"] is VideoCodec.H264
    assert upload["crf"] == 23
    assert upload["preset"] == "fast"

    assert streaming["description"] == STREAMING_UPLOAD_DESCRIPTION
    assert streaming["video_codec"] is VideoCodec.HEVC
    assert streaming["crf"] == 24
    assert streaming["preset"] == "veryfast"
    _assert_file_upload_copy(upload["description"], streaming["description"])


def test_progress_phases_and_human_status(tmp_path):
    output = tmp_path / "out.mp4"
    output.write_bytes(b"1234")
    job = SimpleNamespace(
        status=SimpleNamespace(value="compressing"),
        progress=40,
        encoder_name="h264_nvenc",
        attempt_count=1,
        eta=75,
        speed=24.0,
        profile=SimpleNamespace(
            name="Streaming",
            description=STREAMING_UPLOAD_DESCRIPTION,
        ),
        video_info=SimpleNamespace(size=2_000_000),
        output_file=output,
    )

    assert progress_phase("queued", 0) == "Queued"
    assert progress_phase("analyzing", 0) == "Queued"
    assert progress_phase("compressing", 40) == "Encoding"
    assert progress_phase("compressing", 95) == "Finishing"
    assert progress_phase("paused", 10) == "Encoding"
    assert progress_phase("completed", 100) == "Finishing"

    assert human_progress_status(job) == "Encoding with NVENC…"
    assert (
        human_progress_status(job, awaiting_software_fallback=True)
        == "Waiting for software fallback answer…"
    )
    job.encoder_name = "hevc_qsv"
    assert human_progress_status(job) == "Encoding with QSV…"
    job.encoder_name = "h264_amf"
    assert human_progress_status(job) == "Encoding with AMF…"
    job.encoder_name = "h264_nvenc"
    job.attempt_count = 2
    assert "retry 2" in human_progress_status(job)
    job.progress = 96
    job.attempt_count = 1
    assert human_progress_status(job) == "Finishing…"

    detail = progress_detail(job)
    assert "Streaming upload" in detail
    assert f"ETA {format_time(75)}" in detail
    assert "24.0 fps" in detail
    assert "→" in detail
    _assert_file_upload_copy(detail, human_progress_status(job))
