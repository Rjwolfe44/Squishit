"""
Codec definitions and management for video compression.
Supports the app's public video codec set plus image and audio encoders.

OBS-Studio-parity codec engine â€” exposes every useful rate-control mode,
per-encoder presets / profiles / tunes, and a rich container-compat matrix.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List, Dict, Any, Set, Tuple
import subprocess
import platform
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RateControl(Enum):
    """Rate-control modes understood by the codec engine."""
    CRF = "crf"
    CBR = "cbr"
    ABR = "abr"
    VBR = "vbr"
    CQP = "cqp"
    ICQ = "icq"
    QVBR = "qvbr"
    LOSSLESS = "lossless"


class AudioMode(Enum):
    """How to handle the audio track during compression."""
    AUTO = "auto"
    REENCODE = "reencode"
    COPY = "copy"
    STRIP = "strip"


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
            VideoCodec.AV1: "AV1 (AOMedia â€” reference, slow)",
            VideoCodec.SVT_AV1: "SVT-AV1 (Fast AV1 â€” recommended)",
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
                "intel": "vp9_qsv",
            },
        }
        return hw.get(self, {})

    @property
    def quality_order(self) -> int:
        order = {
            VideoCodec.AV1: 4,
            VideoCodec.SVT_AV1: 4,
            VideoCodec.HEVC: 3,
            VideoCodec.VP9: 2,
            VideoCodec.H264: 1,
        }
        return order.get(self, 1)

    @property
    def speed_order(self) -> int:
        order = {
            VideoCodec.H264: 4,
            VideoCodec.HEVC: 3,
            VideoCodec.SVT_AV1: 2,
            VideoCodec.VP9: 2,
            VideoCodec.AV1: 1,
        }
        return order.get(self, 1)


# Full set of user-visible codecs including AV1 (restored in v2.0)
USER_VISIBLE_VIDEO_CODECS = (
    VideoCodec.H264,
    VideoCodec.HEVC,
    VideoCodec.VP9,
    VideoCodec.SVT_AV1,
    VideoCodec.AV1,
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
    VORBIS = "vorbis"
    FLAC = "flac"
    ALAC = "alac"
    AC3 = "ac3"
    PCM_S16LE = "pcm_s16le"
    PCM_S24LE = "pcm_s24le"
    PCM_F32LE = "pcm_f32le"

    @property
    def display_name(self) -> str:
        names = {
            AudioCodec.AAC: "AAC (Advanced Audio Coding)",
            AudioCodec.OPUS: "Opus (Modern, High Quality)",
            AudioCodec.MP3: "MP3 (MPEG Audio Layer III)",
            AudioCodec.VORBIS: "Vorbis (Open Source)",
            AudioCodec.FLAC: "FLAC (Lossless)",
            AudioCodec.ALAC: "ALAC (Apple Lossless)",
            AudioCodec.AC3: "AC-3 (Dolby Digital)",
            AudioCodec.PCM_S16LE: "PCM 16-bit (Uncompressed)",
            AudioCodec.PCM_S24LE: "PCM 24-bit (Uncompressed)",
            AudioCodec.PCM_F32LE: "PCM 32-float (Uncompressed)",
        }
        return names.get(self, self.value)

    @property
    def ffmpeg_encoder(self) -> str:
        encoders = {
            AudioCodec.AAC: "aac",
            AudioCodec.OPUS: "libopus",
            AudioCodec.MP3: "libmp3lame",
            AudioCodec.VORBIS: "libvorbis",
            AudioCodec.FLAC: "flac",
            AudioCodec.ALAC: "alac",
            AudioCodec.AC3: "ac3",
            AudioCodec.PCM_S16LE: "pcm_s16le",
            AudioCodec.PCM_S24LE: "pcm_s24le",
            AudioCodec.PCM_F32LE: "pcm_f32le",
        }
        return encoders.get(self, "aac")

    @property
    def default_bitrate(self) -> int:
        """Default bitrate in bits per second (0 for lossless)."""
        bitrates = {
            AudioCodec.AAC: 192_000,
            AudioCodec.OPUS: 128_000,
            AudioCodec.MP3: 192_000,
            AudioCodec.VORBIS: 160_000,
            AudioCodec.FLAC: 0,
            AudioCodec.ALAC: 0,
            AudioCodec.AC3: 384_000,
            AudioCodec.PCM_S16LE: 0,
            AudioCodec.PCM_S24LE: 0,
            AudioCodec.PCM_F32LE: 0,
        }
        return bitrates.get(self, 192_000)

    @property
    def is_lossless(self) -> bool:
        return self in {
            AudioCodec.FLAC,
            AudioCodec.ALAC,
            AudioCodec.PCM_S16LE,
            AudioCodec.PCM_S24LE,
            AudioCodec.PCM_F32LE,
        }


# ---------------------------------------------------------------------------
# Per-encoder registry
# ---------------------------------------------------------------------------

ENCODER_REGISTRY: Dict[str, Dict[str, Any]] = {
    "libx264": {
        "codec": VideoCodec.H264, "type": "software",
        "rate_controls": [RateControl.CRF, RateControl.CBR, RateControl.ABR, RateControl.VBR, RateControl.LOSSLESS],
        "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
        "profiles": ["baseline", "main", "high"],
        "tunes": ["film", "animation", "grain", "stillimage", "psnr", "ssim", "fastdecode", "zerolatency"],
        "crf_range": (0, 51), "crf_default": 23, "default_preset": "medium", "default_profile": "high",
    },
    "libx265": {
        "codec": VideoCodec.HEVC, "type": "software",
        "rate_controls": [RateControl.CRF, RateControl.CBR, RateControl.VBR, RateControl.CQP, RateControl.LOSSLESS],
        "presets": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"],
        "profiles": ["main", "main10"],
        "tunes": ["grain", "animation", "psnr", "ssim", "fastdecode", "zerolatency"],
        "crf_range": (0, 51), "crf_default": 28, "default_preset": "medium", "default_profile": "main",
    },
    "libvpx-vp9": {
        "codec": VideoCodec.VP9, "type": "software",
        "rate_controls": [RateControl.CRF, RateControl.CBR, RateControl.VBR],
        "presets": ["0", "1", "2", "3", "4", "5", "6", "7", "8"],
        "profiles": [], "tunes": [],
        "crf_range": (0, 63), "crf_default": 31, "default_preset": "4",
    },
    "libsvtav1": {
        "codec": VideoCodec.SVT_AV1, "type": "software",
        "rate_controls": [RateControl.CRF, RateControl.CBR, RateControl.VBR],
        "presets": [str(i) for i in range(14)],
        "profiles": [], "tunes": [],
        "crf_range": (0, 63), "crf_default": 35, "default_preset": "8",
        "extra_params": {"film_grain": (0, 50)},
    },
    "libaom-av1": {
        "codec": VideoCodec.AV1, "type": "software",
        "rate_controls": [RateControl.CRF, RateControl.CBR, RateControl.VBR],
        "presets": [str(i) for i in range(11)],
        "profiles": [], "tunes": [],
        "crf_range": (0, 63), "crf_default": 30, "default_preset": "6",
    },
    "h264_nvenc": {
        "codec": VideoCodec.H264, "type": "hardware", "vendor": "nvidia",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.LOSSLESS],
        "presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        "tunes": ["hq", "ll", "ull"], "profiles": ["baseline", "main", "high"],
        "cqp_range": (1, 51), "default_preset": "p5",
        "supports_multipass": True, "supports_bframes": (0, 4), "supports_psycho_aq": True,
    },
    "hevc_nvenc": {
        "codec": VideoCodec.HEVC, "type": "hardware", "vendor": "nvidia",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.LOSSLESS],
        "presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        "tunes": ["hq", "ll", "ull"], "profiles": ["main", "main10"],
        "cqp_range": (1, 51), "default_preset": "p5",
        "supports_multipass": True, "supports_bframes": (0, 4), "supports_psycho_aq": True,
    },
    "av1_nvenc": {
        "codec": VideoCodec.AV1, "type": "hardware", "vendor": "nvidia",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR],
        "presets": ["p1", "p2", "p3", "p4", "p5", "p6", "p7"],
        "tunes": ["hq", "ll", "ull"], "profiles": [],
        "cqp_range": (1, 63), "default_preset": "p5",
        "supports_multipass": True, "supports_bframes": (0, 4), "supports_psycho_aq": True,
    },
    "h264_qsv": {
        "codec": VideoCodec.H264, "type": "hardware", "vendor": "intel",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.ICQ],
        "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        "tunes": [], "profiles": ["baseline", "main", "high"],
        "cqp_range": (1, 51), "default_preset": "medium", "supports_bframes": (0, 3),
    },
    "hevc_qsv": {
        "codec": VideoCodec.HEVC, "type": "hardware", "vendor": "intel",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.ICQ],
        "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        "tunes": [], "profiles": ["main", "main10"],
        "cqp_range": (1, 51), "default_preset": "medium", "supports_bframes": (0, 3),
    },
    "av1_qsv": {
        "codec": VideoCodec.AV1, "type": "hardware", "vendor": "intel",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.ICQ],
        "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        "tunes": [], "profiles": [],
        "cqp_range": (1, 63), "default_preset": "medium",
    },
    "vp9_qsv": {
        "codec": VideoCodec.VP9, "type": "hardware", "vendor": "intel",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR],
        "presets": ["veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
        "tunes": [], "profiles": [],
        "cqp_range": (1, 63), "default_preset": "medium",
    },
    "h264_amf": {
        "codec": VideoCodec.H264, "type": "hardware", "vendor": "amd",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.QVBR],
        "presets": ["speed", "balanced", "quality"], "tunes": [],
        "profiles": ["baseline", "main", "high"],
        "cqp_range": (1, 51), "default_preset": "balanced", "supports_bframes": (0, 3),
    },
    "hevc_amf": {
        "codec": VideoCodec.HEVC, "type": "hardware", "vendor": "amd",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.QVBR],
        "presets": ["speed", "balanced", "quality"], "tunes": [],
        "profiles": ["main"],
        "cqp_range": (1, 51), "default_preset": "balanced", "supports_bframes": (0, 3),
    },
    "av1_amf": {
        "codec": VideoCodec.AV1, "type": "hardware", "vendor": "amd",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR, RateControl.QVBR],
        "presets": ["speed", "balanced", "quality"], "tunes": [],
        "profiles": [],
        "cqp_range": (1, 63), "default_preset": "balanced",
    },
    "h264_videotoolbox": {
        "codec": VideoCodec.H264, "type": "hardware", "vendor": "apple",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR],
        "presets": [], "tunes": [], "profiles": ["baseline", "main", "high"],
        "cqp_range": (1, 51),
    },
    "hevc_videotoolbox": {
        "codec": VideoCodec.HEVC, "type": "hardware", "vendor": "apple",
        "rate_controls": [RateControl.CQP, RateControl.CBR, RateControl.VBR],
        "presets": [], "tunes": [], "profiles": ["main", "main10"],
        "cqp_range": (1, 51),
    },
}


# ---------------------------------------------------------------------------
# Container compatibility matrices
# ---------------------------------------------------------------------------

CONTAINER_VIDEO_MATRIX: Dict[VideoContainer, Set[VideoCodec]] = {
    VideoContainer.MP4: {VideoCodec.H264, VideoCodec.HEVC, VideoCodec.AV1, VideoCodec.SVT_AV1},
    VideoContainer.MKV: {VideoCodec.H264, VideoCodec.HEVC, VideoCodec.AV1, VideoCodec.SVT_AV1, VideoCodec.VP9},
    VideoContainer.WEBM: {VideoCodec.VP9, VideoCodec.AV1, VideoCodec.SVT_AV1},
    VideoContainer.MOV: {VideoCodec.H264, VideoCodec.HEVC},
    VideoContainer.AVI: {VideoCodec.H264},
}

CONTAINER_AUDIO_MATRIX: Dict[VideoContainer, Set[AudioCodec]] = {
    VideoContainer.MP4: {AudioCodec.AAC, AudioCodec.AC3, AudioCodec.ALAC, AudioCodec.OPUS, AudioCodec.MP3, AudioCodec.FLAC},
    VideoContainer.MKV: set(AudioCodec),
    VideoContainer.WEBM: {AudioCodec.OPUS, AudioCodec.VORBIS},
    VideoContainer.MOV: {AudioCodec.AAC, AudioCodec.ALAC, AudioCodec.AC3, AudioCodec.PCM_S16LE, AudioCodec.PCM_S24LE, AudioCodec.PCM_F32LE, AudioCodec.FLAC},
    VideoContainer.AVI: {AudioCodec.MP3, AudioCodec.PCM_S16LE, AudioCodec.PCM_S24LE, AudioCodec.PCM_F32LE, AudioCodec.AC3},
}


# ---------------------------------------------------------------------------
# CodecSettings
# ---------------------------------------------------------------------------

@dataclass
class CodecSettings:
    """Settings for a specific codec configuration."""

    video_codec: VideoCodec = VideoCodec.HEVC
    audio_codec: AudioCodec = AudioCodec.AAC
    video_bitrate: Optional[int] = None
    audio_bitrate: Optional[int] = None
    crf: Optional[int] = None
    preset: str = "medium"
    pixel_format: str = "yuv420p"
    profile: Optional[str] = None
    level: Optional[str] = None
    threads: Optional[int] = None
    disable_audio: bool = False

    # Advanced OBS-parity fields
    rate_control: RateControl = RateControl.CRF
    max_bitrate: Optional[int] = None
    buffer_size: Optional[int] = None
    keyframe_interval_sec: int = 0
    b_frames: Optional[int] = None
    tune: Optional[str] = None
    multipass: Optional[str] = None
    psycho_aq: bool = False
    gpu_index: int = 0
    film_grain: int = 0
    audio_mode: AudioMode = AudioMode.AUTO
    custom_encoder_opts: str = ""

    def to_ffmpeg_args(self, hw_encoder: Optional[str] = None) -> List[str]:
        """Convert settings to FFmpeg command-line arguments."""
        args: List[str] = []
        encoder = hw_encoder or self.video_codec.ffmpeg_encoder
        args.extend(["-c:v", encoder])

        reg = ENCODER_REGISTRY.get(encoder, {})

        if hw_encoder:
            if "nvenc" in hw_encoder:
                args.extend(self._build_nvenc_args(hw_encoder, reg))
            elif "qsv" in hw_encoder:
                args.extend(self._build_qsv_args(hw_encoder, reg))
            elif "amf" in hw_encoder:
                args.extend(self._build_amf_args(hw_encoder, reg))
            elif "videotoolbox" in hw_encoder:
                args.extend(self._build_vtb_args(hw_encoder, reg))
        else:
            if encoder == "libx264":
                args.extend(self._build_x264_args(reg))
            elif encoder == "libx265":
                args.extend(self._build_x265_args(reg))
            elif encoder == "libvpx-vp9":
                args.extend(self._build_vp9_args(reg))
            elif encoder == "libsvtav1":
                args.extend(self._build_svtav1_args(reg))
            elif encoder == "libaom-av1":
                args.extend(self._build_aom_args(reg))
            else:
                args.extend(self._build_generic_sw_args())

        args.extend(["-pix_fmt", self.pixel_format])

        if self.profile:
            args.extend(["-profile:v", self.profile])
        if self.level:
            args.extend(["-level", self.level])

        if self.keyframe_interval_sec > 0:
            args.extend(["-g", str(self.keyframe_interval_sec * 30)])

        if self.custom_encoder_opts:
            for tok in self.custom_encoder_opts.split():
                args.append(tok)

        args.extend(self._build_audio_args())
        return args

    # â”€â”€ x264 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_x264_args(self, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        if rc == RateControl.LOSSLESS:
            args.extend(["-qp", "0"])
        elif rc == RateControl.CRF:
            args.extend(["-crf", str(self.crf if self.crf is not None else 23)])
        elif rc == RateControl.CBR:
            br = str(self.video_bitrate or 5_000_000)
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-b:v", br, "-maxrate", br, "-minrate", br, "-bufsize", buf])
        elif rc == RateControl.ABR:
            args.extend(["-b:v", str(self.video_bitrate or 5_000_000)])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            args.extend(["-b:v", br, "-maxrate", mx])
        else:
            args.extend(["-crf", str(self.crf if self.crf is not None else 23)])

        args.extend(["-preset", self.preset if self.preset in (reg.get("presets") or []) else "medium"])
        if self.tune and self.tune in (reg.get("tunes") or []):
            args.extend(["-tune", self.tune])
        if self.threads:
            args.extend(["-threads", str(self.threads)])
        return args

    # â”€â”€ x265 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_x265_args(self, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        if rc == RateControl.LOSSLESS:
            args.extend(["-x265-params", "lossless=1"])
        elif rc == RateControl.CRF:
            args.extend(["-crf", str(self.crf if self.crf is not None else 28)])
        elif rc == RateControl.CBR:
            br_k = (self.video_bitrate or 5_000_000) // 1000
            buf_k = (self.buffer_size or (self.video_bitrate or 5_000_000) * 2) // 1000
            args.extend(["-b:v", f"{br_k}k", "-maxrate", f"{br_k}k", "-bufsize", f"{buf_k}k"])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            args.extend(["-b:v", br, "-maxrate", mx])
        elif rc == RateControl.CQP:
            args.extend(["-qp", str(self.crf if self.crf is not None else 28)])
        else:
            args.extend(["-crf", str(self.crf if self.crf is not None else 28)])

        args.extend(["-preset", self.preset if self.preset in (reg.get("presets") or []) else "medium"])
        if self.tune and self.tune in (reg.get("tunes") or []):
            args.extend(["-tune", self.tune])
        if self.threads:
            args.extend(["-threads", str(self.threads)])
        return args

    # â”€â”€ VP9 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_vp9_args(self, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        if rc == RateControl.CRF:
            args.extend(["-crf", str(self.crf if self.crf is not None else 31), "-b:v", "0"])
        elif rc == RateControl.CBR:
            br = str(self.video_bitrate or 5_000_000)
            args.extend(["-b:v", br, "-minrate", br, "-maxrate", br])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            args.extend(["-b:v", br, "-maxrate", mx])
        else:
            args.extend(["-crf", str(self.crf if self.crf is not None else 31), "-b:v", "0"])

        speed = self._map_vp9_preset(self.preset)
        args.extend(["-cpu-used", str(speed)])
        args.extend(["-row-mt", "1", "-deadline", "good"])
        if self.threads and self.threads >= 8:
            args.extend(["-tile-columns", "2"])
        if self.threads and self.threads >= 16:
            args.extend(["-tile-rows", "1"])
        if self.threads:
            args.extend(["-threads", str(self.threads)])
        return args

    # â”€â”€ SVT-AV1 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_svtav1_args(self, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        if rc == RateControl.CRF:
            args.extend(["-crf", str(self.crf if self.crf is not None else 35)])
        elif rc == RateControl.CBR:
            br_k = (self.video_bitrate or 5_000_000) // 1000
            args.extend(["-b:v", f"{br_k}k", "-rc", "1"])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            args.extend(["-b:v", br])
        else:
            args.extend(["-crf", str(self.crf if self.crf is not None else 35)])

        svt_preset = self._map_svtav1_preset(self.preset)
        args.extend(["-preset", str(svt_preset)])

        svtav1_params: List[str] = []
        if self.film_grain > 0:
            svtav1_params.append(f"film-grain={self.film_grain}")
        if self.threads:
            svtav1_params.append(f"lp={self.threads}")
        if svtav1_params:
            args.extend(["-svtav1-params", ":".join(svtav1_params)])

        return args

    # â”€â”€ AOM-AV1 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_aom_args(self, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        if rc == RateControl.CRF:
            args.extend(["-crf", str(self.crf if self.crf is not None else 30), "-b:v", "0"])
        elif rc == RateControl.CBR:
            br = str(self.video_bitrate or 5_000_000)
            args.extend(["-b:v", br, "-minrate", br, "-maxrate", br])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            args.extend(["-b:v", br, "-maxrate", mx])
        else:
            args.extend(["-crf", str(self.crf if self.crf is not None else 30), "-b:v", "0"])

        speed = self._map_av1_preset(self.preset)
        args.extend(["-usage", "good", "-cpu-used", str(speed)])
        args.extend(["-aq-mode", "1", "-lag-in-frames", "25", "-row-mt", "1"])
        if self.threads and self.threads >= 8:
            args.extend(["-tile-columns", "2"])
        if self.threads and self.threads >= 16:
            args.extend(["-tile-rows", "1"])
        if self.threads:
            args.extend(["-threads", str(self.threads)])
        return args

    # â”€â”€ Generic SW fallback â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_generic_sw_args(self) -> List[str]:
        args: List[str] = []
        if self.crf is not None and self.video_bitrate is None:
            args.extend(["-crf", str(self.crf)])
        elif self.video_bitrate:
            args.extend(["-b:v", str(self.video_bitrate)])
        if self.threads:
            args.extend(["-threads", str(self.threads)])
        return args

    # â”€â”€ NVENC â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_nvenc_args(self, encoder: str, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        preset = self.preset if self.preset in (reg.get("presets") or []) else (reg.get("default_preset") or "p5")
        args.extend(["-preset", preset])

        if self.tune and self.tune in (reg.get("tunes") or []):
            args.extend(["-tune", self.tune])

        if rc == RateControl.LOSSLESS:
            args.extend(["-tune", "lossless", "-multipass", "disabled"])
        elif rc == RateControl.CQP:
            qp = self.crf if self.crf is not None else 28
            args.extend(["-rc", "constqp", "-qp", str(qp)])
        elif rc == RateControl.CBR:
            br = str(self.video_bitrate or 5_000_000)
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-rc", "cbr", "-b:v", br, "-maxrate", br, "-bufsize", buf])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-rc", "vbr", "-b:v", br, "-maxrate", mx, "-bufsize", buf])
        else:
            cq = self.crf if self.crf is not None else 28
            args.extend(["-cq", str(cq), "-b:v", "0"])

        if self.multipass and self.multipass != "disabled" and reg.get("supports_multipass"):
            args.extend(["-multipass", self.multipass])
        if self.psycho_aq and reg.get("supports_psycho_aq"):
            args.extend(["-spatial-aq", "1", "-temporal-aq", "1"])
        if self.b_frames is not None and reg.get("supports_bframes"):
            bmin, bmax = reg["supports_bframes"]
            args.extend(["-bf", str(max(bmin, min(self.b_frames, bmax)))])
        if self.gpu_index > 0:
            args.extend(["-gpu", str(self.gpu_index)])

        return args

    # â”€â”€ QSV â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_qsv_args(self, encoder: str, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        preset = self.preset if self.preset in (reg.get("presets") or []) else (reg.get("default_preset") or "medium")
        args.extend(["-preset", preset])

        if rc == RateControl.CQP:
            qp = self.crf if self.crf is not None else 28
            args.extend(["-q", str(qp)])
        elif rc == RateControl.ICQ:
            quality = self.crf if self.crf is not None else 25
            args.extend(["-global_quality", str(quality), "-look_ahead", "1"])
        elif rc == RateControl.CBR:
            br = str(self.video_bitrate or 5_000_000)
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-b:v", br, "-maxrate", br, "-bufsize", buf])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-b:v", br, "-maxrate", mx, "-bufsize", buf])
        else:
            quality = self.crf if self.crf is not None else 28
            args.extend(["-global_quality", str(quality)])

        if self.b_frames is not None and reg.get("supports_bframes"):
            bmin, bmax = reg["supports_bframes"]
            args.extend(["-bf", str(max(bmin, min(self.b_frames, bmax)))])

        return args

    # â”€â”€ AMF â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_amf_args(self, encoder: str, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        args.extend(["-usage", "transcoding"])
        preset = self.preset if self.preset in (reg.get("presets") or []) else (reg.get("default_preset") or "balanced")
        args.extend(["-quality", preset])

        if rc == RateControl.CQP:
            qp = self.crf if self.crf is not None else 28
            args.extend(["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(min(51, qp + 2))])
        elif rc == RateControl.QVBR:
            quality = self.crf if self.crf is not None else 28
            args.extend(["-rc", "qvbr", "-qvbr_quality_level", str(quality)])
        elif rc == RateControl.CBR:
            br = str(self.video_bitrate or 5_000_000)
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-rc", "cbr", "-b:v", br, "-maxrate", br, "-bufsize", buf])
        elif rc == RateControl.VBR:
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            args.extend(["-rc", "vbr_peak", "-b:v", br, "-maxrate", mx])
        else:
            qp = self.crf if self.crf is not None else 28
            args.extend(["-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(min(51, qp + 2))])

        if self.b_frames is not None and reg.get("supports_bframes"):
            bmin, bmax = reg["supports_bframes"]
            args.extend(["-bf", str(max(bmin, min(self.b_frames, bmax)))])

        return args

    # â”€â”€ VideoToolbox â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_vtb_args(self, encoder: str, reg: Dict) -> List[str]:
        args: List[str] = []
        rc = self.rate_control

        if rc in (RateControl.CBR, RateControl.VBR, RateControl.ABR):
            br = str(self.video_bitrate or 5_000_000)
            mx = str(self.max_bitrate or int((self.video_bitrate or 5_000_000) * 1.5))
            buf = str(self.buffer_size or int((self.video_bitrate or 5_000_000) * 2))
            args.extend(["-b:v", br, "-maxrate", mx, "-bufsize", buf])
        else:
            q = max(1, 65 - (self.crf or 28))
            args.extend(["-q:v", str(q)])

        return args

    # â”€â”€ Audio â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    def _build_audio_args(self) -> List[str]:
        args: List[str] = []

        if self.disable_audio or self.audio_mode == AudioMode.STRIP:
            args.append("-an")
            return args

        if self.audio_mode == AudioMode.COPY:
            args.extend(["-c:a", "copy"])
            return args

        args.extend(["-c:a", self.audio_codec.ffmpeg_encoder])
        audio_br = self.audio_bitrate or self.audio_codec.default_bitrate
        if audio_br > 0 and not self.audio_codec.is_lossless:
            args.extend(["-b:a", str(audio_br)])

        return args

    # â”€â”€ Preset mapping helpers â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    @staticmethod
    def _map_nvenc_preset(preset: str) -> str:
        mapping = {
            "ultrafast": "p1", "superfast": "p2", "veryfast": "p3",
            "faster": "p4", "fast": "p5", "medium": "p5",
            "slow": "p6", "slower": "p7", "veryslow": "p7",
        }
        return mapping.get(preset, preset if preset.startswith("p") else "p4")

    @staticmethod
    def _map_qsv_preset(preset: str) -> str:
        mapping = {
            "ultrafast": "veryfast", "superfast": "faster", "veryfast": "fast",
            "faster": "fast", "fast": "medium", "medium": "medium",
            "slow": "slow", "slower": "slower", "veryslow": "veryslow",
        }
        return mapping.get(preset, "medium")

    @staticmethod
    def _map_amf_preset(preset: str) -> str:
        mapping = {
            "ultrafast": "speed", "superfast": "speed", "veryfast": "balanced",
            "faster": "balanced", "fast": "balanced", "medium": "balanced",
            "slow": "quality", "slower": "quality", "veryslow": "quality",
        }
        return mapping.get(preset, preset if preset in {"speed", "balanced", "quality"} else "balanced")

    @staticmethod
    def _map_av1_preset(preset: str) -> int:
        mapping = {
            "ultrafast": 8, "superfast": 8, "veryfast": 8,
            "faster": 7, "fast": 6, "medium": 5,
            "slow": 3, "slower": 2, "veryslow": 0,
        }
        try:
            return max(0, min(10, int(preset)))
        except ValueError:
            return max(0, min(8, mapping.get(preset, 5)))

    @staticmethod
    def _map_vp9_preset(preset: str) -> int:
        mapping = {
            "ultrafast": 5, "superfast": 5, "veryfast": 4,
            "faster": 4, "fast": 3, "medium": 2,
            "slow": 1, "slower": 0, "veryslow": 0,
        }
        try:
            return max(0, min(8, int(preset)))
        except ValueError:
            return mapping.get(preset, 2)

    @staticmethod
    def _map_svtav1_preset(preset: str) -> int:
        mapping = {
            "ultrafast": 12, "superfast": 11, "veryfast": 10,
            "faster": 9, "fast": 8, "medium": 6,
            "slow": 4, "slower": 3, "veryslow": 2,
        }
        try:
            return max(0, min(13, int(preset)))
        except ValueError:
            return mapping.get(preset, 6)


# ---------------------------------------------------------------------------
# CodecManager
# ---------------------------------------------------------------------------

class CodecManager:
    """Manages codec availability and selection."""

    CONTAINER_CODEC_MATRIX = CONTAINER_VIDEO_MATRIX

    def __init__(self, ffmpeg_path: str = "ffmpeg"):
        self.ffmpeg_path = ffmpeg_path
        self._available_encoders: Optional[set] = None
        self._hw_accel_available: Dict[str, bool] = {}

    @property
    def available_encoders(self) -> set:
        if self._available_encoders is None:
            self._available_encoders = self._detect_encoders()
        return self._available_encoders

    def _detect_encoders(self) -> set:
        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-encoders", "-hide_banner"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
            encoders = set()
            for line in result.stdout.split("\n"):
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].startswith(("V", "A")):
                    encoders.add(parts[1])
            return encoders
        except Exception as e:
            logger.error(f"Failed to detect encoders: {e}")
            return {"libx264", "aac"}

    def is_encoder_available(self, encoder: str) -> bool:
        return encoder in self.available_encoders

    def is_codec_available(self, codec: VideoCodec) -> bool:
        return self.is_encoder_available(codec.ffmpeg_encoder)

    def is_codec_usable(self, codec: VideoCodec, hw_vendor: Optional[str] = None) -> bool:
        if self.is_codec_available(codec):
            return True
        if hw_vendor and self.get_hw_encoder(codec, hw_vendor):
            return True
        return False

    def get_hw_encoder(self, codec: VideoCodec, vendor: str) -> Optional[str]:
        hw_encoders = codec.hw_encoders
        encoder = hw_encoders.get(vendor)
        if encoder and self.is_encoder_available(encoder):
            return encoder
        return None

    def get_best_codec(self, prefer_efficiency: bool = True, hw_vendor: Optional[str] = None) -> VideoCodec:
        if prefer_efficiency:
            preference_order = [VideoCodec.SVT_AV1, VideoCodec.HEVC, VideoCodec.VP9, VideoCodec.H264]
        else:
            preference_order = [VideoCodec.H264, VideoCodec.HEVC, VideoCodec.VP9, VideoCodec.SVT_AV1]
        for codec in preference_order:
            if self.is_codec_usable(codec, hw_vendor=hw_vendor):
                return codec
        return VideoCodec.H264

    def get_supported_codecs(self, hw_vendor: Optional[str] = None) -> List[VideoCodec]:
        return [codec for codec in USER_VISIBLE_VIDEO_CODECS if self.is_codec_usable(codec, hw_vendor=hw_vendor)]

    def get_supported_containers(self, codec: Optional[VideoCodec] = None) -> List[VideoContainer]:
        containers = list(VideoContainer)
        if codec is None:
            return containers
        return [c for c in containers if self.is_video_container_supported(codec, c)]

    def is_video_container_supported(self, codec: VideoCodec, container) -> bool:
        container = self.normalize_container(container)
        return codec in CONTAINER_VIDEO_MATRIX.get(container, set())

    def is_audio_container_supported(self, audio_codec: AudioCodec, container) -> bool:
        container = self.normalize_container(container)
        return audio_codec in CONTAINER_AUDIO_MATRIX.get(container, set())

    def normalize_container(self, container) -> VideoContainer:
        if isinstance(container, VideoContainer):
            return container
        return VideoContainer(str(container).lower().lstrip("."))

    def get_default_container(self, codec: VideoCodec) -> VideoContainer:
        defaults = {
            VideoCodec.H264: VideoContainer.MP4,
            VideoCodec.HEVC: VideoContainer.MP4,
            VideoCodec.AV1: VideoContainer.MKV,
            VideoCodec.SVT_AV1: VideoContainer.MKV,
            VideoCodec.VP9: VideoContainer.WEBM,
        }
        return defaults.get(codec, VideoContainer.MP4)

    def get_compatible_container(self, codec: VideoCodec, preferred: Optional[str] = None) -> VideoContainer:
        if preferred:
            try:
                preferred_container = self.normalize_container(preferred)
                if self.is_video_container_supported(codec, preferred_container):
                    return preferred_container
            except ValueError:
                pass
        return self.get_default_container(codec)

    def get_compatible_audio_codecs(self, container) -> List[AudioCodec]:
        """Return audio codecs supported by a given container."""
        container = self.normalize_container(container)
        supported = CONTAINER_AUDIO_MATRIX.get(container, set())
        return [ac for ac in AudioCodec if ac in supported and self.is_encoder_available(ac.ffmpeg_encoder)]

    def get_supported_audio_codecs(self) -> List[AudioCodec]:
        supported = []
        for codec in AudioCodec:
            if self.is_encoder_available(codec.ffmpeg_encoder):
                supported.append(codec)
        return supported if supported else [AudioCodec.AAC]

    def get_encoder_info(self, encoder_name: str) -> Dict[str, Any]:
        return ENCODER_REGISTRY.get(encoder_name, {})

    def get_available_hw_encoders(self, codec: VideoCodec) -> List[str]:
        result = []
        for vendor, enc_name in codec.hw_encoders.items():
            if self.is_encoder_available(enc_name):
                result.append(enc_name)
        return result

    def get_codec_recommendations(self, use_case: str) -> Dict[str, Any]:
        recommendations = {
            "upload": {
                "video_codec": VideoCodec.H264, "audio_codec": AudioCodec.AAC,
                "preset": "fast", "crf": 23,
                "description": "Optimized for YouTube/Social Media â€” maximum compatibility",
            },
            "archive": {
                "video_codec": VideoCodec.SVT_AV1, "audio_codec": AudioCodec.OPUS,
                "preset": "6", "crf": 28,
                "description": "Best compression for long-term storage (SVT-AV1 + Opus)",
            },
            "mobile": {
                "video_codec": VideoCodec.HEVC, "audio_codec": AudioCodec.AAC,
                "preset": "fast", "crf": 26,
                "description": "Smaller files for mobile viewing",
            },
            "streaming": {
                "video_codec": VideoCodec.HEVC, "audio_codec": AudioCodec.AAC,
                "preset": "veryfast", "crf": 24,
                "description": "Fast encoding for live streaming",
            },
            "editing": {
                "video_codec": VideoCodec.H264, "audio_codec": AudioCodec.AAC,
                "preset": "veryfast", "crf": 18,
                "description": "High quality for video editing proxies",
            },
        }
        rec = recommendations.get(use_case, recommendations["upload"])
        if not self.is_codec_available(rec["video_codec"]):
            rec["video_codec"] = self.get_best_codec()
        return rec
