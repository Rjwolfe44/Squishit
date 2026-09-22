import pytest

from video_compressor.core.codecs import AudioCodec, VideoCodec
from video_compressor.core.profiles import CompressionProfile, ProfileManager, ProfileType


@pytest.fixture
def manager(tmp_path):
    return ProfileManager(config_dir=tmp_path)


def test_builtin_profiles_cover_cli_names(manager):
    expected = {
        "Fast",
        "Balanced",
        "Max / Archival",
        "YouTube Upload",
        "Mobile",
        "Streaming",
    }
    assert expected <= {profile.name for profile in manager.get_all_profiles()}


def test_builtin_profile_defaults(manager):
    fast = manager.get_profile("Fast")
    balanced = manager.get_profile("Balanced")
    archival = manager.get_profile("Max / Archival")
    youtube = manager.get_profile("YouTube Upload")
    mobile = manager.get_profile("Mobile")
    streaming = manager.get_profile("Streaming")

    assert fast.video_codec is VideoCodec.H264
    assert fast.crf == 26
    assert fast.preset == "fast"
    assert fast.audio_codec is AudioCodec.OPUS
    assert fast.use_hw_accel is True

    assert balanced.video_codec is VideoCodec.HEVC
    assert balanced.crf == 28
    assert balanced.preset == "medium"
    assert balanced.video_container == "mp4"
    assert balanced.audio_bitrate == 128_000

    assert archival.video_codec is VideoCodec.SVT_AV1
    assert archival.crf == 35
    assert archival.preset == "6"
    assert archival.video_container == "mkv"
    assert archival.audio_codec is AudioCodec.OPUS
    assert archival.audio_bitrate == 96_000
    assert archival.use_hw_accel is False

    assert youtube.video_codec is VideoCodec.H264
    assert youtube.audio_codec is AudioCodec.AAC
    assert youtube.crf == 23
    assert youtube.preset == "fast"
    assert youtube.video_container == "mp4"

    assert mobile.video_codec is VideoCodec.HEVC
    assert mobile.audio_codec is AudioCodec.AAC
    assert mobile.crf == 26
    assert mobile.max_resolution is None

    assert streaming.video_codec is VideoCodec.HEVC
    assert streaming.preset == "veryfast"
    assert streaming.crf == 24
    assert streaming.audio_codec is AudioCodec.AAC


def test_profile_round_trip_preserves_defaults(manager):
    original = manager.get_profile("Balanced")
    restored = CompressionProfile.from_dict(original.to_dict())

    assert restored.name == original.name
    assert restored.profile_type is original.profile_type
    assert restored.video_codec is original.video_codec
    assert restored.audio_codec is original.audio_codec
    assert restored.audio_bitrate == original.audio_bitrate
    assert restored.crf == original.crf
    assert restored.preset == original.preset
    assert restored.video_container == original.video_container
    assert restored.use_hw_accel is original.use_hw_accel


def test_from_dict_uses_profile_field_defaults_when_keys_are_absent():
    restored = CompressionProfile.from_dict({"name": "Partial"})

    assert restored.profile_type is ProfileType.CUSTOM
    assert restored.video_codec is VideoCodec.HEVC
    assert restored.audio_codec is AudioCodec.OPUS
    assert restored.audio_bitrate == 128_000
    assert restored.crf == 23
    assert restored.video_container == "mp4"


def test_system_profiles_cannot_be_deleted(manager):
    assert manager.delete_profile("YouTube Upload") is False
    assert manager.delete_profile("Fast") is False
    assert manager.get_profile("YouTube Upload") is not None

    custom = manager.create_custom_profile("Nightly", base_profile="Fast")
    assert manager.save_profile(custom) is True
    assert manager.delete_profile("Nightly") is True
    assert manager.get_profile("Nightly") is None


def test_legacy_container_normalization_matches_codec():
    hevc_webm = CompressionProfile.from_dict({
        "name": "Legacy HEVC",
        "video_codec": "hevc",
        "audio_codec": "opus",
        "video_container": "webm",
    })
    vp9_mp4 = CompressionProfile.from_dict({
        "name": "Legacy VP9",
        "video_codec": "vp9",
        "audio_codec": "opus",
        "video_container": "mp4",
    })
    h264_webm = CompressionProfile.from_dict({
        "name": "Legacy H264",
        "video_codec": "h264",
        "audio_codec": "aac",
        "video_container": "webm",
    })

    assert hevc_webm.video_container == "mkv"
    assert vp9_mp4.video_container == "webm"
    assert h264_webm.video_container == "mp4"
