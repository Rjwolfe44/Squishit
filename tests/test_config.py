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


def test_trim_editor_sound_settings_default_on_and_round_trip():
    fresh = AppConfig()
    assert (fresh.editor_sound_on, fresh.editor_volume, fresh.editor_sound_wave) == (True, 80, True)
    loaded = AppConfig.from_dict({})
    assert (loaded.editor_sound_on, loaded.editor_volume, loaded.editor_sound_wave) == (True, 80, True)
    fresh.editor_sound_on, fresh.editor_volume, fresh.editor_sound_wave = False, 35, False
    again = AppConfig.from_dict(fresh.to_dict())
    assert (again.editor_sound_on, again.editor_volume, again.editor_sound_wave) == (False, 35, False)


def test_a_hand_edited_volume_is_kept_in_range():
    assert AppConfig.from_dict({"editor_volume": 250}).editor_volume == 100
    assert AppConfig.from_dict({"editor_volume": -3}).editor_volume == 0
    assert AppConfig.from_dict({"editor_volume": "loud"}).editor_volume == 80
    assert AppConfig.from_dict({"editor_volume": 42.6}).editor_volume == 43
