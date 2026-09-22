"""
Application configuration and settings management.
"""

import os
import logging
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
import yaml

# Application metadata
APP_NAME = "SquishIt"
APP_VERSION = "2.0.0"
APP_AUTHOR = "Vlad"


def _get_config_dir() -> Path:
    """Return the per-user configuration directory for the current platform."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / APP_NAME
    return Path.home() / ".squishit"


# Default paths
CONFIG_DIR = _get_config_dir()
LOGS_DIR = CONFIG_DIR / "logs"
PROFILES_DIR = CONFIG_DIR / "profiles"

# Ensure directories exist
CONFIG_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
PROFILES_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class AppConfig:
    """Application configuration settings."""
    
    # Output settings
    default_output_dir: Path = field(default_factory=lambda: Path.home() / "Videos" / "SquishIt")
    default_profile: str = "Balanced"
    output_suffix: str = "_compressed"
    default_video_container: str = "mp4"
    default_image_format: str = "webp"
    quick_compress_profile: str = "Balanced"
    start_in_tray: bool = False
    launch_tray_on_startup: bool = False
    
    # GUI settings
    theme: str = "dark"
    window_width: int = 2240
    window_height: int = 1480
    remember_window_size: bool = True
    ui_scale_mode: str = "100%"
    
    # Compression settings
    use_hardware_accel: bool = True
    default_codec: str = "hevc"
    default_preset: str = "medium"
    default_crf: int = 22
    default_audio_bitrate: int = 192000
    default_target_size_mode: str = "auto"
    resource_governor: str = "auto"
    max_resolution: Optional[int] = None
    max_frame_rate: Optional[int] = None
    
    # Advanced settings
    max_threads: Optional[int] = None
    auto_analyze: bool = True
    show_advanced_options: bool = False
    
    # Update settings
    check_updates_on_startup: bool = True
    last_update_check: str = ""  # ISO-8601 timestamp of the most recent update check

    # Parallel / naming / history
    max_parallel_jobs: int = 1
    output_name_template: str = "{name}_compressed"
    notify_on_completion: bool = True
    history_max_entries: int = 100
    batch_queue_order: str = "manual"

    # Logging
    log_level: str = "INFO"
    log_to_file: bool = True
    
    def __post_init__(self):
        if isinstance(self.default_output_dir, str):
            self.default_output_dir = Path(self.default_output_dir)
    
    def to_dict(self) -> dict:
        """Convert to dictionary."""
        return {
            "default_output_dir": str(self.default_output_dir),
            "default_profile": self.default_profile,
            "output_suffix": self.output_suffix,
            "default_video_container": self.default_video_container,
            "default_image_format": self.default_image_format,
            "quick_compress_profile": self.quick_compress_profile,
            "start_in_tray": self.start_in_tray,
            "launch_tray_on_startup": self.launch_tray_on_startup,
            "theme": self.theme,
            "window_width": self.window_width,
            "window_height": self.window_height,
            "remember_window_size": self.remember_window_size,
            "ui_scale_mode": self.ui_scale_mode,
            "use_hardware_accel": self.use_hardware_accel,
            "default_codec": self.default_codec,
            "default_preset": self.default_preset,
            "default_crf": self.default_crf,
            "default_audio_bitrate": self.default_audio_bitrate,
            "default_target_size_mode": self.default_target_size_mode,
            "resource_governor": self.resource_governor,
            "max_resolution": self.max_resolution,
            "max_frame_rate": self.max_frame_rate,
            "max_threads": self.max_threads,
            "auto_analyze": self.auto_analyze,
            "show_advanced_options": self.show_advanced_options,
            "log_level": self.log_level,
            "log_to_file": self.log_to_file,
            "check_updates_on_startup": self.check_updates_on_startup,
            "last_update_check": self.last_update_check,
            "max_parallel_jobs": self.max_parallel_jobs,
            "output_name_template": self.output_name_template,
            "notify_on_completion": self.notify_on_completion,
            "history_max_entries": self.history_max_entries,
            "batch_queue_order": self.batch_queue_order,
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> 'AppConfig':
        """Create from dictionary."""
        return cls(
            default_output_dir=Path(data.get("default_output_dir", Path.home() / "Videos" / "Compressed")),
            default_profile=data.get("default_profile", "Balanced"),
            output_suffix=data.get("output_suffix", "_compressed"),
            default_video_container=data.get("default_video_container", "mp4"),
            default_image_format=data.get("default_image_format", "webp"),
            quick_compress_profile=data.get("quick_compress_profile", "Balanced"),
            start_in_tray=data.get("start_in_tray", False),
            launch_tray_on_startup=data.get("launch_tray_on_startup", False),
            theme=data.get("theme", "dark"),
            window_width=data.get("window_width", 2240),
            window_height=data.get("window_height", 1480),
            remember_window_size=data.get("remember_window_size", True),
            ui_scale_mode=data.get("ui_scale_mode", "auto"),
            use_hardware_accel=data.get("use_hardware_accel", True),
            default_codec=data.get("default_codec", "hevc"),
            default_preset=data.get("default_preset", "medium"),
            default_crf=data.get("default_crf", 22),
            default_audio_bitrate=data.get("default_audio_bitrate", 192000),
            default_target_size_mode=data.get("default_target_size_mode", "auto"),
            resource_governor=data.get("resource_governor", "auto"),
            max_resolution=data.get("max_resolution"),
            max_frame_rate=data.get("max_frame_rate"),
            max_threads=data.get("max_threads"),
            auto_analyze=data.get("auto_analyze", True),
            show_advanced_options=data.get("show_advanced_options", False),
            log_level=data.get("log_level", "INFO"),
            log_to_file=data.get("log_to_file", True),
            check_updates_on_startup=data.get("check_updates_on_startup", True),
            last_update_check=data.get("last_update_check", ""),
            max_parallel_jobs=data.get("max_parallel_jobs", 1),
            output_name_template=data.get("output_name_template", "{name}_compressed"),
            notify_on_completion=data.get("notify_on_completion", True),
            history_max_entries=data.get("history_max_entries", 100),
            batch_queue_order=data.get("batch_queue_order", "manual"),
        )


class ConfigManager:
    """Manages application configuration."""
    
    CONFIG_FILE = CONFIG_DIR / "config.yaml"
    
    def __init__(self):
        self.config = self._load_config()
    
    def _load_config(self) -> AppConfig:
        """Load configuration from file."""
        if self.CONFIG_FILE.exists():
            try:
                with open(self.CONFIG_FILE, 'r') as f:
                    data = yaml.safe_load(f) or {}
                return AppConfig.from_dict(data)
            except Exception as e:
                print(f"Warning: Failed to load config: {e}")
        return AppConfig()
    
    def save_config(self):
        """Save configuration to file."""
        try:
            with open(self.CONFIG_FILE, 'w') as f:
                yaml.dump(self.config.to_dict(), f, default_flow_style=False)
        except Exception as e:
            print(f"Warning: Failed to save config: {e}")
    
    def update_config(self, **kwargs):
        """Update configuration values."""
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                setattr(self.config, key, value)
        self.save_config()
    
    def reset_to_defaults(self):
        """Reset configuration to defaults."""
        self.config = AppConfig()
        self.save_config()


def setup_logging(log_level: str = "INFO", log_to_file: bool = True) -> logging.Logger:
    """
    Setup application logging.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_to_file: Whether to log to file
        
    Returns:
        Root logger instance
    """
    # Create root logger
    logger = logging.getLogger("squishit")
    logger.setLevel(getattr(logging, log_level.upper(), logging.INFO))
    
    # Clear existing handlers
    logger.handlers.clear()
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_format = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%H:%M:%S'
    )
    console_handler.setFormatter(console_format)
    logger.addHandler(console_handler)
    
    # File handler
    if log_to_file:
        log_file = LOGS_DIR / "squishit.log"
        file_handler = logging.FileHandler(log_file, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_format)
        logger.addHandler(file_handler)
    
    return logger


# Global config instance
_config_manager: Optional[ConfigManager] = None


def get_config() -> AppConfig:
    """Get the global application configuration."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager.config


def get_config_manager() -> ConfigManager:
    """Get the global configuration manager."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager()
    return _config_manager