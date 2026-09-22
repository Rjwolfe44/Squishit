"""
Utility functions for media compression.
"""

import re
from pathlib import Path
from typing import Iterable, Union, Optional


SUPPORTED_VIDEO_EXTENSIONS = {
    '.mp4', '.mkv', '.mov', '.avi', '.webm', '.wmv', '.flv',
    '.m4v', '.mpeg', '.mpg', '.mts', '.m2ts', '.vob', '.ogv',
    '.ts', '.m2v', '.m4p', '.3gp', '.3g2', '.f4v'
}

SUPPORTED_IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.webp', '.bmp', '.tif', '.tiff', '.gif', '.avif'
}

SUPPORTED_VIDEO_CONTAINERS = {"mp4", "mkv", "webm", "mov", "avi"}
SUPPORTED_IMAGE_OUTPUT_FORMATS = {"webp", "avif", "jpg", "jpeg", "png", "jxl"}

SUPPORTED_MEDIA_EXTENSIONS = SUPPORTED_VIDEO_EXTENSIONS | SUPPORTED_IMAGE_EXTENSIONS


def collect_media_files(paths: Iterable[Union[str, Path]], recursive: bool = True) -> list[Path]:
    """Expand dropped files/folders into a deduplicated list of supported media files."""
    collected: list[Path] = []
    seen: set[Path] = set()

    for raw_path in paths:
        path = Path(raw_path).expanduser()
        if not path.exists():
            continue

        candidates = [path]
        if path.is_dir():
            walker = path.rglob("*") if recursive else path.glob("*")
            candidates = [child for child in walker if child.is_file()]

        for candidate in candidates:
            normalized = candidate.resolve()
            if normalized in seen:
                continue
            if detect_media_type(candidate):
                seen.add(normalized)
                collected.append(candidate)

    return collected


def format_size(size_bytes: Union[int, float]) -> str:
    """
    Format byte size to human-readable string.
    
    Args:
        size_bytes: Size in bytes
        
    Returns:
        Formatted string (e.g., "1.5 GB", "256 MB")
    """
    if size_bytes is None or size_bytes < 0:
        return "Unknown"
    
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    size = float(size_bytes)
    
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    
    return f"{size:.2f} PB"


def parse_size(size_str: str) -> Optional[int]:
    """
    Parse human-readable size string to bytes.
    
    Args:
        size_str: Size string (e.g., "100MB", "1.5GB", "500 KB")
        
    Returns:
        Size in bytes, or None if parsing fails
    """
    if not size_str:
        return None
    
    size_str = size_str.strip().upper().replace(" ", "")
    
    # Match number and unit
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMGTP]?B?)?$', size_str)
    if not match:
        return None
    
    value = float(match.group(1))
    unit = match.group(2) or "B"
    
    multipliers = {
        "B": 1,
        "KB": 1024,
        "K": 1024,
        "MB": 1024 ** 2,
        "M": 1024 ** 2,
        "GB": 1024 ** 3,
        "G": 1024 ** 3,
        "TB": 1024 ** 4,
        "T": 1024 ** 4,
        "PB": 1024 ** 5,
        "P": 1024 ** 5,
    }
    
    multiplier = multipliers.get(unit, 1)
    return int(value * multiplier)


def format_time(seconds: Union[int, float]) -> str:
    """
    Format seconds to human-readable time string.
    
    Args:
        seconds: Time in seconds
        
    Returns:
        Formatted string (e.g., "2h 30m", "45s")
    """
    if seconds is None or seconds < 0:
        return "Unknown"
    
    seconds = int(seconds)
    
    if seconds < 60:
        return f"{seconds}s"
    elif seconds < 3600:
        minutes = seconds // 60
        secs = seconds % 60
        if secs == 0:
            return f"{minutes}m"
        return f"{minutes}m {secs}s"
    else:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes == 0:
            return f"{hours}h"
        return f"{hours}h {minutes}m"


def format_bitrate(bitrate: Union[int, float, None]) -> str:
    """
    Format bitrate to human-readable string.
    
    Args:
        bitrate: Bitrate in bits per second
        
    Returns:
        Formatted string (e.g., "5.2 Mbps", "128 Kbps")
    """
    if bitrate is None or bitrate < 0:
        return "Unknown"
    
    if bitrate >= 1_000_000:
        return f"{bitrate / 1_000_000:.2f} Mbps"
    elif bitrate >= 1_000:
        return f"{bitrate / 1_000:.0f} Kbps"
    else:
        return f"{bitrate:.0f} bps"


def parse_bitrate(bitrate_str: str) -> Optional[int]:
    """
    Parse bitrate string to bits per second.
    
    Args:
        bitrate_str: Bitrate string (e.g., "5M", "1500k", "2.5M")
        
    Returns:
        Bitrate in bits per second, or None if parsing fails
    """
    if not bitrate_str:
        return None
    
    bitrate_str = bitrate_str.strip().upper().replace(" ", "")
    
    match = re.match(r'^(\d+(?:\.\d+)?)\s*([KMG]?)$', bitrate_str)
    if not match:
        return None
    
    value = float(match.group(1))
    unit = match.group(2)
    
    multipliers = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "G": 1_000_000_000,
    }
    
    return int(value * multipliers.get(unit, 1))


def sanitize_filename(filename: str) -> str:
    """
    Sanitize filename by removing/replacing invalid characters.
    
    Args:
        filename: Original filename
        
    Returns:
        Sanitized filename safe for all platforms
    """
    # Characters not allowed in filenames on Windows
    invalid_chars = r'[<>:"/\\|?*\x00-\x1f]'
    filename = re.sub(invalid_chars, '_', filename)
    
    # Remove leading/trailing spaces and dots
    filename = filename.strip('. ')
    
    # Ensure not empty
    if not filename:
        filename = "output"
    
    return filename


def detect_media_type(filepath: Union[str, Path]) -> Optional[str]:
    """Detect whether a file is a supported video or image."""
    suffix = Path(filepath).suffix.lower()
    if suffix in SUPPORTED_VIDEO_EXTENSIONS:
        return "video"
    if suffix in SUPPORTED_IMAGE_EXTENSIONS:
        return "image"
    return None


def get_audio_extension(codec: str) -> str:
    """Map an audio codec to a sensible output extension."""
    normalized = codec.lower().strip()
    mapping = {
        "aac": "m4a",
        "opus": "opus",
        "mp3": "mp3",
        "flac": "flac",
        "ac3": "ac3",
    }
    return mapping.get(normalized, "m4a")


def parse_timecode(value: Union[str, int, float, None]) -> Optional[float]:
    """Parse a seconds or hh:mm:ss style timecode into seconds."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if seconds >= 0 else None

    text = str(value).strip()
    if not text:
        return None

    try:
        seconds = float(text)
        return seconds if seconds >= 0 else None
    except ValueError:
        pass

    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        return None

    try:
        values = [float(part) for part in parts]
    except ValueError:
        return None

    seconds = 0.0
    for part in values:
        seconds = seconds * 60 + part
    return seconds if seconds >= 0 else None


def get_video_extension(codec: str, container: Optional[str] = None) -> str:
    """
    Get appropriate file extension for video codec.
    
    Args:
        codec: Video codec name (e.g., 'av1', 'hevc', 'h264')
        
    Returns:
        File extension without dot (e.g., 'mp4', 'mkv')
    """
    if container:
        normalized = container.lower().lstrip(".")
        if normalized in SUPPORTED_VIDEO_CONTAINERS:
            return normalized

    codec = codec.lower()
    
    # AV1 and HEVC work best in MKV or MP4
    extension_map = {
        "av1": "mkv",
        "hevc": "mp4",
        "h265": "mp4",
        "h264": "mp4",
        "avc": "mp4",
        "vp9": "webm",
        "vp8": "webm",
    }
    
    return extension_map.get(codec, "mp4")


def get_image_extension(
    preferred_format: Optional[str] = None,
    prefer_lossless: bool = False,
    has_alpha: bool = False,
) -> str:
    """Get an appropriate output extension for image compression."""
    if preferred_format:
        normalized = preferred_format.lower().lstrip(".")
        if normalized in SUPPORTED_IMAGE_OUTPUT_FORMATS:
            return "jpg" if normalized == "jpeg" else normalized
    if prefer_lossless and has_alpha:
        return "png"
    return "webp"


def calculate_reduction(original_size: int, compressed_size: int) -> str:
    """
    Calculate and format compression reduction percentage.
    
    Args:
        original_size: Original file size in bytes
        compressed_size: Compressed file size in bytes
        
    Returns:
        Formatted reduction string (e.g., "45% smaller")
    """
    if original_size <= 0:
        return "N/A"
    
    reduction = (1 - compressed_size / original_size) * 100
    
    if reduction > 0:
        return f"{reduction:.1f}% smaller"
    elif reduction < 0:
        return f"{abs(reduction):.1f}% larger"
    else:
        return "No change"


def estimate_compression_time(
    duration_seconds: float,
    resolution_height: int,
    codec: str,
    preset: str,
    hw_accel: bool = False
) -> float:
    """
    Estimate compression time based on video properties and settings.
    
    Args:
        duration_seconds: Video duration in seconds
        resolution_height: Video resolution height
        codec: Target codec
        preset: Encoding preset
        hw_accel: Whether hardware acceleration is available
        
    Returns:
        Estimated time in seconds
    """
    # Base time multipliers (relative to real-time)
    codec_multipliers = {
        "av1": 10.0,
        "hevc": 4.0,
        "h265": 4.0,
        "h264": 1.5,
        "avc": 1.5,
        "vp9": 6.0,
    }
    
    preset_multipliers = {
        "ultrafast": 0.5,
        "superfast": 0.7,
        "veryfast": 1.0,
        "faster": 1.3,
        "fast": 1.5,
        "medium": 2.0,
        "slow": 3.0,
        "slower": 4.5,
        "veryslow": 6.0,
    }
    
    # Resolution multiplier (relative to 1080p)
    res_multiplier = resolution_height / 1080
    
    # Get multipliers
    codec_mult = codec_multipliers.get(codec.lower(), 3.0)
    preset_mult = preset_multipliers.get(preset.lower(), 2.0)
    
    # Hardware acceleration reduces time significantly
    hw_mult = 0.2 if hw_accel else 1.0
    
    # Calculate estimate
    estimated_time = duration_seconds * codec_mult * preset_mult * res_multiplier * hw_mult
    
    return estimated_time


def validate_video_file(filepath: Union[str, Path]) -> tuple[bool, str]:
    """
    Validate that a file is a valid video file.
    
    Args:
        filepath: Path to video file
        
    Returns:
        Tuple of (is_valid, error_message)
    """
    path = Path(filepath)
    
    # Check file exists
    if not path.exists():
        return False, f"File does not exist: {filepath}"
    
    # Check is a file
    if not path.is_file():
        return False, f"Not a file: {filepath}"
    
    # Check file size
    if path.stat().st_size == 0:
        return False, f"File is empty: {filepath}"
    
    if path.suffix.lower() not in SUPPORTED_VIDEO_EXTENSIONS:
        return False, f"Unsupported file format: {path.suffix}"
    
    return True, ""


def validate_image_file(filepath: Union[str, Path]) -> tuple[bool, str]:
    """Validate that a file is a valid image file."""
    path = Path(filepath)

    if not path.exists():
        return False, f"File does not exist: {filepath}"

    if not path.is_file():
        return False, f"Not a file: {filepath}"

    if path.stat().st_size == 0:
        return False, f"File is empty: {filepath}"

    if path.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
        return False, f"Unsupported file format: {path.suffix}"

    return True, ""


def validate_media_file(filepath: Union[str, Path]) -> tuple[bool, str]:
    """Validate that a file is a supported media file."""
    media_type = detect_media_type(filepath)
    if media_type == "video":
        return validate_video_file(filepath)
    if media_type == "image":
        return validate_image_file(filepath)
    return False, f"Unsupported file format: {Path(filepath).suffix}"


class Singleton(type):
    """Singleton metaclass for ensuring single instance."""
    
    _instances = {}
    
    def __call__(cls, *args, **kwargs):
        if cls not in cls._instances:
            cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]