"""
Codec definitions and management for video compression.
Supports the app's public video codec set plus image and audio encoders.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any
import subprocess
import platform
import logging

logger = logging.getLogger(__name__)


class VideoCodec(Enum):
    """Supported video codecs with their properties."""
    
    AV1 = "av1"
    SVT_AV1 = "svt-av1"
    HEVC = "hevc"
    H264 = "h264"
    VP9 = "vp9"
    
    @property
    def display_name(self) -> str:
        names = {
            VideoCodec.AV1: "AV1 (AOMedia Video 1)",
            VideoCodec.SVT_AV1: "SVT-AV1 (Fast AV1)",
            VideoCodec.HEVC: "HEVC (H.265)",
            VideoCodec.H264: "H.264 (AVC)",
            VideoCodec.VP9: "VP9 (Google)",
        }
        return names.get(self, self.value)
    
    @property
    def ffmpeg_encoder(self) -> str:
        encoders = {
            VideoCodec.AV1: "libaom-av1",
            VideoCodec.SVT_AV1: "libsvtav1",
            VideoCodec.HEVC: "libx265",
            VideoCodec.H264: "libx264",
            VideoCodec.VP9: "libvpx-vp9",
        }
        return encoders.get(self, "libx264")
    
    @property
    def hw_encoders(self) -> Dict[str, str]:
        """Hardware-accelerated encoder variants by vendor."""
        hw = {
            VideoCodec.AV1: {
                "nvidia": "av1_nvenc",
                "intel": "av1_qsv",
                "amd": "av1_amf",
            },
            VideoCodec.SVT_AV1: {},
            VideoCodec.HEVC: {
                "nvidia": "hevc_nvenc",
                "intel": "hevc_qsv",
                "amd": "hevc_amf",
                "apple": "hevc_videotoolbox",
            },
            VideoCodec.H264: {
                "nvidia": "h264_nvenc",
                "intel": "h264_qsv",
                "amd": "h264_amf",
                "apple": "h264_videotoolbox",
            },
            VideoCodec.VP9: {
                # VP9 has limited hardware encoding support
            },
        }
        return hw.get(self, {})
    
    @property
    def quality_order(self) -> int:
        """Quality order (higher is better efficiency)."""
        order = {
            VideoCodec.AV1: 4,      # Best efficiency
            VideoCodec.SVT_AV1: 4,
            VideoCodec.HEVC: 3,
            VideoCodec.VP9: 2,
            VideoCodec.H264: 1,     # Most compatible
        }
        return order.get(self, 1)

    @property
    def speed_order(self) -> int:
        """Speed order (higher is faster encoding)."""
        order = {
            VideoCodec.H264: 4,     # Fastest
            VideoCodec.HEVC: 3,
            VideoCodec.SVT_AV1: 2,
            VideoCodec.VP9: 2,
            VideoCodec.AV1: 1,      # Slowest (but most efficient)
        }
        return order.get(self, 1)


USER_VISIBLE_VIDEO_CODECS = (
    VideoCodec.H264,
    VideoCodec.HEVC,
    VideoCodec.VP9,
)


class VideoContainer(Enum):
    """Supported video output containers."""

    MP4 = "mp4"
    MKV = "mkv"
    WEBM = "webm"
    MOV = "mov"
    AVI = "avi"


class ImageFormat(Enum):
    """Supported image output formats."""

    WEBP = "webp"
    AVIF = "avif"
    JPEG = "jpg"
    PNG = "png"
    JXL = "jxl"


class AudioCodec(Enum):
    """Supported audio codecs with their properties."""
    
    AAC = "aac"
    OPUS = "opus"
    MP3 = "mp3"
    FLAC = "flac"
    AC3 = "ac3"
    
    @property
    def display_name(self) -> str:
        names = {
            AudioCodec.AAC: "AAC (Advanced Audio Coding)",
            AudioCodec.OPUS: "Opus (Modern, High Quality)",
            AudioCodec.MP3: "MP3 (MPEG Audio Layer III)",
            AudioCodec.FLAC: "FLAC (Lossless)",
            AudioCodec.AC3: "AC-3 (Dolby Digital)",
        }
        return names.get(self, self.value)
    
    @property
    def ffmpeg_encoder(self) -> str:
        encoders = {
            AudioCodec.AAC: "aac",
            AudioCodec.OPUS: "libopus",
            AudioCodec.MP3: "libmp3lame",
            AudioCodec.FLAC: "flac",
            AudioCodec.AC3: "ac3",
        }
        return encoders.get(self, "aac")
    
    @property
    def default_bitrate(self) -> int:
        """Default bitrate in bits per second."""
        bitrates = {
            AudioCodec.AAC: 192_000,
            AudioCodec.OPUS: 128_000,
            AudioCodec.MP3: 192_000,
            AudioCodec.FLAC: 0,  # Lossless
            AudioCodec.AC3: 384_000,
        }
        return bitrates.get(self, 192_000)
    
    @property
    def is_lossless(self) -> bool:
        return self == AudioCodec.FLAC


@dataclass
class CodecSettings:
    """Settings for a specific codec configuration."""
    
    video_codec: VideoCodec = VideoCodec.HEVC
    audio_codec: AudioCodec = AudioCodec.AAC
    video_bitrate: Optional[int] = None  # bits per second
    audio_bitrate: Optional[int] = None  # bits per second
    crf: Optional[int] = None  # Constant Rate Factor (0-51 for x264/x265)
    preset: str = "medium"  # Encoder preset
    pixel_format: str = "yuv420p"
    profile: Optional[str] = None  # Codec profile
    level: Optional[str] = None  # Codec level
    threads: Optional[int] = None
    disable_audio: bool = False
    
    def to_ffmpeg_args(self, hw_encoder: Optional[str] = None) -> List[str]:
        """
        Convert settings to FFmpeg command-line arguments.
        
        Args:
            hw_encoder: Hardware encoder to use (if available)
            
        Returns:
            List of FFmpeg arguments
        """
        args = []
        
        # Video codec selection
        encoder = hw_encoder if hw_encoder else self.video_codec.ffmpeg_encoder
        args.extend(["-c:v", encoder])
        
        # Hardware encoder specific settings
        if hw_encoder:
            is_av1_hw = hw_encoder.startswith("av1_")
            bitrate_maxrate = str(int((self.video_bitrate or 0) * 1.08)) if self.video_bitrate else "0"
            bitrate_bufsize = str(int((self.video_bitrate or 0) * 2)) if self.video_bitrate else "0"

            # NVIDIA NVENC settings
            if "nvenc" in hw_encoder:
                args.extend(["-preset", self._map_nvenc_preset(self.preset)])
                if self.video_bitrate:
                    args.extend(["-rc", "vbr"])
                    args.extend(["-b:v", str(self.video_bitrate)])
                    args.extend(["-maxrate", bitrate_maxrate, "-bufsize", bitrate_bufsize])
                else:
                    # Use CQ mode for quality-based encoding
                    cq_value = self.crf if self.crf else 28
                    if is_av1_hw:
                        cq_value = min(51, cq_value + 6)
                    args.extend(["-cq", str(cq_value)])
                    args.extend(["-b:v", "0"])  # Required for CQ mode
            
            # Intel QSV settings
            elif "qsv" in hw_encoder:
                args.extend(["-preset", self._map_qsv_preset(self.preset)])
                if self.video_bitrate:
                    args.extend(["-b:v", str(self.video_bitrate)])
                    args.extend(["-maxrate", bitrate_maxrate, "-bufsize", bitrate_bufsize])
                else:
                    quality_value = self.crf or 28
                    if is_av1_hw:
                        quality_value = min(51, quality_value + 4)
                    args.extend(["-global_quality", str(quality_value)])
            
            # AMD AMF settings
            elif "amf" in hw_encoder:
                args.extend(["-usage", "transcoding"])
                args.extend(["-quality", self._map_amf_preset(self.preset)])
                if self.video_bitrate:
                    args.extend(["-rc", "cbr"])
                    args.extend(["-b:v", str(self.video_bitrate)])
                    args.extend(["-maxrate", bitrate_maxrate, "-bufsize", bitrate_bufsize])
                else:
                    qp_value = self.crf or 28
                    if is_av1_hw:
                        qvbr_quality = min(51, max(20, qp_value + 10))
                        args.extend(["-rc", "qvbr"])
                        args.extend(["-qvbr_quality_level", str(qvbr_quality)])
                        args.extend(["-aq_mode", "caq"])
                        args.extend(["-preanalysis", "1"])
                    else:
                        args.extend(["-qp_i", str(qp_value)])
                        args.extend(["-qp_p", str(min(50, qp_value + 2))])
            
            # Apple VideoToolbox settings
            elif "videotoolbox" in hw_encoder:
                if self.video_bitrate:
                    args.extend(["-b:v", str(self.video_bitrate)])
                    args.extend(["-maxrate", bitrate_maxrate, "-bufsize", bitrate_bufsize])
                else:
                    args.extend(["-q:v", str(65 - (self.crf or 28))])  # Inverted scale
        
        # Software encoder settings
        else:
            # CRF mode (quality-based)
            if self.crf is not None and self.video_bitrate is None:
                if self.video_codec in [VideoCodec.HEVC, VideoCodec.H264]:
                    args.extend(["-crf", str(self.crf)])
                elif self.video_codec == VideoCodec.AV1:
                    args.extend(["-crf", str(self.crf)])
                    args.extend(["-b:v", "0"])
                elif self.video_codec == VideoCodec.SVT_AV1:
                    args.extend(["-crf", str(self.crf)])
                elif self.video_codec == VideoCodec.VP9:
                    args.extend(["-crf", str(self.crf)])
                    args.extend(["-b:v", "0"])
            
            # Bitrate mode
            elif self.video_bitrate:
                args.extend(["-b:v", str(self.video_bitrate)])
                args.extend(["-maxrate", str(int(self.video_bitrate * 1.08))])
                args.extend(["-bufsize", str(self.video_bitrate * 2)])
            
            # Preset
            if self.video_codec == VideoCodec.AV1:
                args.extend(["-usage", "good"])
                args.extend(["-cpu-used", str(self._map_av1_preset(self.preset))])
                args.extend(["-aq-mode", "1"])
                args.extend(["-lag-in-frames", "25"])
                args.extend(["-row-mt", "1"])
                if self.threads and self.threads >= 8:
                    args.extend(["-tile-columns", "2"])
                if self.threads and self.threads >= 16:
                    args.extend(["-tile-rows", "1"])
            elif self.video_codec == VideoCodec.SVT_AV1:
                args.extend(["-preset", str(self._map_svtav1_preset(self.preset))])
                if self.threads:
                    args.extend(["-svtav1-params", f"lp={self.threads}"])
            elif self.video_codec == VideoCodec.VP9:
                args.extend(["-cpu-used", str(self._map_vp9_preset(self.preset))])
                args.extend(["-row-mt", "1"])
                args.extend(["-deadline", "good"])
                if self.threads and self.threads >= 8:
                    args.extend(["-tile-columns", "2"])
                if self.threads and self.threads >= 16:
                    args.extend(["-tile-rows", "1"])
            else:
                args.extend(["-preset", self.preset])

            if self.threads:
                args.extend(["-threads", str(self.threads)])
        
        # Pixel format
        args.extend(["-pix_fmt", self.pixel_format])
        
        # Profile and level (for H.264/HEVC)
        if self.profile:
            args.extend(["-profile:v", self.profile])
        if self.level:
            args.extend(["-level", self.level])
        
        if self.disable_audio:
            args.append("-an")
            return args

        # Audio codec
        args.extend(["-c:a", self.audio_codec.ffmpeg_encoder])

        # Audio bitrate
        audio_br = self.audio_bitrate or self.audio_codec.default_bitrate
        if audio_br > 0 and not self.audio_codec.is_lossless:
            args.extend(["-b:a", str(audio_br)])
        
        return args
    
    def _map_nvenc_preset(self, preset: str) -> str:
        """Map standard preset to NVENC preset."""
        mapping = {
            "ultrafast": "p1",
            "superfast": "p2",
            "veryfast": "p3",
            "faster": "p4",
            "fast": "p5",
            "medium": "p5",
            "slow": "p6",
            "slower": "p7",
            "veryslow": "p7",
        }
        return mapping.get(preset, "p4")
    
    def _map_qsv_preset(self, preset: str) -> str:
        """Map standard preset to QSV preset."""
        mapping = {
            "ultrafast": "veryfast",
            "superfast": "faster",
            "veryfast": "fast",
            "faster": "fast",
            "fast": "medium",
            "medium": "medium",
            "slow": "slow",
            "slower": "slower",
            "veryslow": "veryslow",
        }
        return mapping.get(preset, "medium")
    
    def _map_amf_preset(self, preset: str) -> str:
        """Map standard preset to AMF quality."""
        mapping = {
            "ultrafast": "speed",
            "superfast": "speed",
            "veryfast": "balanced",
            "faster": "balanced",
            "fast": "balanced",
            "medium": "balanced",
            "slow": "quality",
            "slower": "quality",
            "veryslow": "quality",
        }
        return mapping.get(preset, "balanced")
    
    def _map_av1_preset(self, preset: str) -> int:
        """Map standard preset to libaom-av1 cpu-used (0-8, lower is slower)."""
        mapping = {
            "ultrafast": 8,
            "superfast": 8,
            "veryfast": 8,
            "faster": 7,
            "fast": 6,
            "medium": 5,
            "slow": 3,
            "slower": 2,
            "veryslow": 0,
        }
        return max(0, min(8, mapping.get(preset, 5)))
    
    def _map_vp9_preset(self, preset: str) -> int:
        """Map standard preset to VP9 CPU usage (0-5, lower is slower)."""
        mapping = {
            "ultrafast": 5,
            "superfast": 5,
            "veryfast": 4,
            "faster": 4,
            "fast": 3,
            "medium": 2,
            "slow": 1,
            "slower": 0,
            "veryslow": 0,
        }
        return mapping.get(preset, 2)

    def _map_svtav1_preset(self, preset: str) -> int:
        """Map standard preset names to SVT-AV1 preset values."""
        mapping = {
            "ultrafast": 12,
            "superfast": 11,
            "veryfast": 10,
            "faster": 9,
            "fast": 8,
            "medium": 6,
            "slow": 4,
            "slower": 3,
            "veryslow": 2,
        }
        return mapping.get(preset, 6)


class CodecManager:
    """Manages codec availability and selection."""

    CONTAINER_CODEC_MATRIX = {
        VideoContainer.MP4: {VideoCodec.H264, VideoCodec.HEVC, VideoCodec.AV1, VideoCodec.SVT_AV1},
        VideoContainer.MKV: {VideoCodec.H264, VideoCodec.HEVC, VideoCodec.AV1, VideoCodec.SVT_AV1, VideoCodec.VP9},
        VideoContainer.WEBM: {VideoCodec.VP9, VideoCodec.AV1, VideoCodec.SVT_AV1},
        VideoContainer.MOV: {VideoCodec.H264, VideoCodec.HEVC},
        VideoContainer.AVI: {VideoCodec.H264},
    }
    
    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = ffmpeg_path
        self._available_encoders: Optional[set] = None
        self._hw_accel_available: Dict[str, bool] = {}
    
    @property
    def available_encoders(self) -> set:
        """Get set of available FFmpeg encoders."""
        if self._available_encoders is None:
            self._available_encoders = self._detect_encoders()
        return self._available_encoders
    
    def _detect_encoders(self) -> set:
        """Detect available FFmpeg encoders."""
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-encoders", "-hide_banner"],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            
            encoders = set()
            for line in result.stdout.split('\n'):
                # Parse encoder lines like: " V..... libx264           libx264 H.264"
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].startswith('V'):
                    encoders.add(parts[1])
                elif len(parts) >= 2 and parts[0].startswith('A'):
                    encoders.add(parts[1])
            
            return encoders
        except Exception as e:
            logger.error(f"Failed to detect encoders: {e}")
            return {"libx264", "aac"}  # Fallback defaults
    
    def is_encoder_available(self, encoder: str) -> bool:
        """Check if a specific encoder is available."""
        return encoder in self.available_encoders
    
    def is_codec_available(self, codec: VideoCodec) -> bool:
        """Check if a video codec is available."""
        return self.is_encoder_available(codec.ffmpeg_encoder)

    def is_codec_usable(self, codec: VideoCodec, hw_vendor: Optional[str] = None) -> bool:
        """Return whether a codec can be used in software or through the active GPU vendor."""
        if self.is_codec_available(codec):
            return True
        if hw_vendor and self.get_hw_encoder(codec, hw_vendor):
            return True
        return False
    
    def get_hw_encoder(self, codec: VideoCodec, vendor: str) -> Optional[str]:
        """
        Get hardware encoder for codec and vendor if available.
        
        Args:
            codec: Video codec
            vendor: GPU vendor (nvidia, intel, amd, apple)
            
        Returns:
            Hardware encoder name or None
        """
        hw_encoders = codec.hw_encoders
        encoder = hw_encoders.get(vendor)
        
        if encoder and self.is_encoder_available(encoder):
            return encoder
        return None
    
    def get_best_codec(self, prefer_efficiency: bool = True, hw_vendor: Optional[str] = None) -> VideoCodec:
        """
        Get the best available codec.
        
        Args:
            prefer_efficiency: If True, prefer compression efficiency over speed
            
        Returns:
            Best available VideoCodec
        """
        preference_order = [VideoCodec.HEVC, VideoCodec.VP9, VideoCodec.H264]
        if not prefer_efficiency:
            preference_order = [VideoCodec.H264, VideoCodec.HEVC, VideoCodec.VP9]

        for codec in preference_order:
            if self.is_codec_usable(codec, hw_vendor=hw_vendor):
                return codec

        return VideoCodec.H264
    
    def get_supported_codecs(self, hw_vendor: Optional[str] = None) -> List[VideoCodec]:
        """Get list of all video codecs usable on this machine."""
        return [codec for codec in USER_VISIBLE_VIDEO_CODECS if self.is_codec_usable(codec, hw_vendor=hw_vendor)]

    def get_supported_containers(self, codec: Optional[VideoCodec] = None) -> List[VideoContainer]:
        """Return supported containers, optionally filtered by codec compatibility."""
        containers = list(VideoContainer)
        if codec is None:
            return containers
        return [container for container in containers if self.is_video_container_supported(codec, container)]

    def is_video_container_supported(self, codec: VideoCodec, container: VideoContainer | str) -> bool:
        """Return whether a codec is valid for a given output container."""
        container = self.normalize_container(container)
        return codec in self.CONTAINER_CODEC_MATRIX.get(container, set())

    def normalize_container(self, container: VideoContainer | str) -> VideoContainer:
        """Normalize a container enum or string to VideoContainer."""
        if isinstance(container, VideoContainer):
            return container
        return VideoContainer(str(container).lower().lstrip("."))

    def get_default_container(self, codec: VideoCodec) -> VideoContainer:
        """Pick the most broadly useful container for a codec."""
        defaults = {
            VideoCodec.H264: VideoContainer.MP4,
            VideoCodec.HEVC: VideoContainer.MP4,
            VideoCodec.AV1: VideoContainer.MKV,
            VideoCodec.SVT_AV1: VideoContainer.MKV,
            VideoCodec.VP9: VideoContainer.WEBM,
        }
        return defaults.get(codec, VideoContainer.MP4)

    def get_compatible_container(self, codec: VideoCodec, preferred: Optional[str] = None) -> VideoContainer:
        """Return a compatible container, honoring preference when possible."""
        if preferred:
            try:
                preferred_container = self.normalize_container(preferred)
                if self.is_video_container_supported(codec, preferred_container):
                    return preferred_container
            except ValueError:
                pass
        return self.get_default_container(codec)
    
    def get_supported_audio_codecs(self) -> List[AudioCodec]:
        """Get list of all supported audio codecs."""
        supported = []
        for codec in AudioCodec:
            if self.is_encoder_available(codec.ffmpeg_encoder):
                supported.append(codec)
        return supported if supported else [AudioCodec.AAC]
    
    def get_codec_recommendations(self, use_case: str) -> Dict[str, Any]:
        """
        Get codec recommendations based on use case.
        
        Args:
            use_case: Use case (upload, archive, mobile, streaming, editing)
            
        Returns:
            Dictionary with recommended settings
        """
        recommendations = {
            "upload": {
                "video_codec": VideoCodec.H264,  # Most compatible
                "audio_codec": AudioCodec.AAC,
                "preset": "fast",
                "crf": 23,
                "description": "Optimized for YouTube/Social Media - maximum compatibility"
            },
            "archive": {
                "video_codec": VideoCodec.HEVC,
                "audio_codec": AudioCodec.OPUS,
                "preset": "slow",
                "crf": 24,
                "description": "Strong HEVC compression for long-term storage"
            },
            "mobile": {
                "video_codec": VideoCodec.HEVC,
                "audio_codec": AudioCodec.AAC,
                "preset": "fast",
                "crf": 26,
                "description": "Smaller files for mobile viewing"
            },
            "streaming": {
                "video_codec": VideoCodec.HEVC,
                "audio_codec": AudioCodec.AAC,
                "preset": "veryfast",
                "crf": 24,
                "description": "Video file for Twitch, YouTube, or OBS upload."
            },
            "editing": {
                "video_codec": VideoCodec.H264,
                "audio_codec": AudioCodec.AAC,
                "preset": "veryfast",
                "crf": 18,
                "description": "High quality for video editing proxies"
            },
        }
        
        # Adjust based on availability
        rec = recommendations.get(use_case, recommendations["upload"])
        
        if not self.is_codec_available(rec["video_codec"]):
            rec["video_codec"] = self.get_best_codec()
        
        return rec