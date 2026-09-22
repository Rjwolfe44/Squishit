from pathlib import Path

from video_compressor.config import AppConfig


def test_config_from_dict_uses_the_same_output_dir_as_a_fresh_config():
    fresh = AppConfig()
    loaded = AppConfig.from_dict({})

    assert loaded.default_output_dir == fresh.default_output_dir
    assert loaded.default_output_dir == Path.home() / "Videos" / "SquishIt"
    assert loaded.default_profile == "Balanced"
    assert loaded.default_codec == "hevc"
    assert loaded.default_target_size_mode == "auto"
