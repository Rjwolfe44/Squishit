"""
Core module for video compression functionality.
"""

from .compressor import VideoCompressor
from .profiles import CompressionProfile, ProfileManager
from .hardware import HardwareDetector
from .codecs import CodecManager, VideoCodec, AudioCodec, VideoContainer, ImageFormat
from .eta import ETACalculator
from .utils import format_size, format_time, parse_size

__all__ = [
    "VideoCompressor",
    "CompressionProfile",
    "ProfileManager",
    "HardwareDetector",
    "CodecManager",
    "VideoCodec",
    "AudioCodec",
    "VideoContainer",
    "ImageFormat",
    "ETACalculator",
    "format_size",
    "format_time",
    "parse_size",
]