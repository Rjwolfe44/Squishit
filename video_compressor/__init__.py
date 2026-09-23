"""SquishIt application package."""

__version__ = "2.0.1"
__author__ = "Vlad"
__app_name__ = "SquishIt"

__all__ = [
    "VideoCompressor",
    "CompressionProfile",
    "ProfileManager",
    "HardwareDetector",
]


def __getattr__(name):
    if name == "VideoCompressor":
        from .core.compressor import VideoCompressor
        return VideoCompressor
    if name in {"CompressionProfile", "ProfileManager"}:
        from .core.profiles import CompressionProfile, ProfileManager
        return {"CompressionProfile": CompressionProfile, "ProfileManager": ProfileManager}[name]
    if name == "HardwareDetector":
        from .core.hardware import HardwareDetector
        return HardwareDetector
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")