"""
Compression profiles and presets for different use cases.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any
from enum import Enum
import yaml
from pathlib import Path

from .codecs import VideoCodec, AudioCodec, CodecSettings, RateControl, AudioMode
from .quality_ladder import (
    ARCHIVAL_PROFILE_NAME,
    PROFILE_FLAG_RUNGS,
    QUICK_COMPRESS_LITE_LABEL,
    QUICK_COMPRESS_MAX_LABEL,
    QualityRung,
    get_step,
)
from ..config import PROFILES_DIR


class ProfileType(Enum):
    """Types of compression profiles."""
    FAST = "fast"
    BALANCED = "balanced"
    MAX = "max"
    CUSTOM = "custom"


class TargetSizeMode(Enum):
    """How aggressively SquishIt should chase a requested target size."""

    AUTO = "auto"
    FAST = "fast"
    BALANCED = "balanced"
    STRICT = "strict"
    EXACT = "exact"


class ResourceGovernor(Enum):
    """Global resource usage preference for encode threads."""

    AUTO = "auto"
    LOW = "low"
    BALANCED = "balanced"
    MAX = "max"


@dataclass(frozen=True)
class TargetSizePlan:
    """Resolved target-size plan for a specific media file and profile."""

    mode: TargetSizeMode
    target_size_mb: int
    target_size_bytes: int
    minimum_size_bytes: int
    video_bitrate: int
    audio_bitrate: int
    min_video_bitrate: int
    tolerance_ratio: float
    max_attempts: int
    overhead_ratio: float
    warning: str = ""


@dataclass
class CompressionProfile:
    """
    Complete compression profile with all settings.
    
    A profile encapsulates all compression settings including codec choices,
    quality settings, and advanced options.
    """
    
    name: str
    profile_type: ProfileType
    description: str = ""
    
    # Video settings
    video_codec: VideoCodec = VideoCodec.HEVC
    crf: int = 23
    preset: str = "medium"
    video_bitrate: Optional[int] = None  # Target bitrate in bits/sec
    
    # Audio settings
    audio_codec: AudioCodec = AudioCodec.OPUS
    audio_bitrate: int = 128_000
    output_mode: str = "video"
    
    # Resolution settings
    max_resolution: Optional[int] = None  # Max height (e.g., 1080, 720)
    resolution_scale: float = 1.0
    frame_rate: Optional[int] = None  # Target frame rate
    video_container: str = "mp4"
    image_format: str = "webp"
    
    # Advanced settings
    pixel_format: str = "yuv420p"
    use_hw_accel: bool = True
    threads: Optional[int] = None
    image_quality: int = 82

    # OBS-parity advanced fields
    rate_control: str = RateControl.CRF.value
    max_bitrate: Optional[int] = None
    buffer_size: Optional[int] = None
    keyframe_interval_sec: int = 0
    b_frames: Optional[int] = None
    tune: Optional[str] = None
    multipass: Optional[str] = None
    psycho_aq: bool = False
    gpu_index: int = 0
    film_grain: int = 0
    audio_mode: str = AudioMode.AUTO.value
    custom_encoder_opts: str = ""
    profile_level: Optional[str] = None
    codec_profile: Optional[str] = None
    
    # Target size mode
    target_size_mb: Optional[int] = None
    target_reduction_percent: Optional[int] = None
    target_size_mode: str = TargetSizeMode.AUTO.value
    resource_governor: str = ResourceGovernor.AUTO.value
    exact_audio_policy: str = "reduce"
    exact_two_pass: bool = False
    disable_audio: bool = False

    # Optional edit/expert controls
    trim_enabled: bool = False
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    extra_ffmpeg_args: str = ""
    
    # Metadata
    created_by: str = "system"
    
    def to_codec_settings(self) -> CodecSettings:
        """Convert profile to CodecSettings object."""
        try:
            rc = RateControl(self.rate_control)
        except ValueError:
            rc = RateControl.CRF
        try:
            am = AudioMode(self.audio_mode)
        except ValueError:
            am = AudioMode.AUTO
        return CodecSettings(
            video_codec=self.video_codec,
            audio_codec=self.audio_codec,
            video_bitrate=self.video_bitrate,
            audio_bitrate=self.audio_bitrate,
            crf=self.crf,
            preset=self.preset,
            pixel_format=self.pixel_format,
            profile=self.codec_profile,
            level=self.profile_level,
            threads=self.threads,
            disable_audio=self.disable_audio,
            rate_control=rc,
            max_bitrate=self.max_bitrate,
            buffer_size=self.buffer_size,
            keyframe_interval_sec=self.keyframe_interval_sec,
            b_frames=self.b_frames,
            tune=self.tune,
            multipass=self.multipass,
            psycho_aq=self.psycho_aq,
            gpu_index=self.gpu_index,
            film_grain=self.film_grain,
            audio_mode=am,
            custom_encoder_opts=self.custom_encoder_opts,
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert profile to dictionary for serialization."""
        return {
            "name": self.name,
            "profile_type": self.profile_type.value,
            "description": self.description,
            "video_codec": self.video_codec.value,
            "crf": self.crf,
            "preset": self.preset,
            "video_bitrate": self.video_bitrate,
            "audio_codec": self.audio_codec.value,
            "audio_bitrate": self.audio_bitrate,
            "output_mode": self.output_mode,
            "max_resolution": self.max_resolution,
            "resolution_scale": self.resolution_scale,
            "frame_rate": self.frame_rate,
            "video_container": self.video_container,
            "image_format": self.image_format,
            "pixel_format": self.pixel_format,
            "use_hw_accel": self.use_hw_accel,
            "threads": self.threads,
            "image_quality": self.image_quality,
            "target_size_mb": self.target_size_mb,
            "target_reduction_percent": self.target_reduction_percent,
            "target_size_mode": self.target_size_mode,
            "resource_governor": self.resource_governor,
            "exact_audio_policy": self.exact_audio_policy,
            "exact_two_pass": self.exact_two_pass,
            "disable_audio": self.disable_audio,
            "trim_enabled": self.trim_enabled,
            "trim_start": self.trim_start,
            "trim_end": self.trim_end,
            "extra_ffmpeg_args": self.extra_ffmpeg_args,
            "created_by": self.created_by,
            "rate_control": self.rate_control,
            "max_bitrate": self.max_bitrate,
            "buffer_size": self.buffer_size,
            "keyframe_interval_sec": self.keyframe_interval_sec,
            "b_frames": self.b_frames,
            "tune": self.tune,
            "multipass": self.multipass,
            "psycho_aq": self.psycho_aq,
            "gpu_index": self.gpu_index,
            "film_grain": self.film_grain,
            "audio_mode": self.audio_mode,
            "custom_encoder_opts": self.custom_encoder_opts,
            "profile_level": self.profile_level,
            "codec_profile": self.codec_profile,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'CompressionProfile':
        """Create profile from dictionary."""
        video_codec = normalize_profile_video_codec(data.get("video_codec", "hevc"))
        audio_codec = AudioCodec(data.get("audio_codec", AudioCodec.OPUS.value))
        return cls(
            name=data["name"],
            profile_type=ProfileType(data.get("profile_type", "custom")),
            description=data.get("description", ""),
            video_codec=video_codec,
            crf=data.get("crf", 23),
            preset=normalize_profile_preset(data.get("preset", "medium")),
            video_bitrate=data.get("video_bitrate"),
            audio_codec=audio_codec,
            audio_bitrate=data.get("audio_bitrate", 128_000),
            output_mode=data.get("output_mode", "video"),
            max_resolution=data.get("max_resolution"),
            resolution_scale=data.get("resolution_scale", 1.0),
            frame_rate=data.get("frame_rate"),
            video_container=normalize_profile_container(
                video_codec,
                data.get("video_container", "mp4"),
                audio_codec,
            ),
            image_format=data.get("image_format", "webp"),
            pixel_format=data.get("pixel_format", "yuv420p"),
            use_hw_accel=data.get("use_hw_accel", True),
            threads=data.get("threads"),
            image_quality=data.get("image_quality", 82),
            target_size_mb=data.get("target_size_mb"),
            target_reduction_percent=data.get("target_reduction_percent"),
            target_size_mode=data.get("target_size_mode", TargetSizeMode.AUTO.value),
            resource_governor=data.get("resource_governor", ResourceGovernor.AUTO.value),
            exact_audio_policy=data.get("exact_audio_policy", "reduce"),
            exact_two_pass=data.get("exact_two_pass", False),
            disable_audio=data.get("disable_audio", False),
            trim_enabled=data.get("trim_enabled", False),
            trim_start=data.get("trim_start"),
            trim_end=data.get("trim_end"),
            extra_ffmpeg_args=data.get("extra_ffmpeg_args", ""),
            created_by=data.get("created_by", "user"),
            rate_control=data.get("rate_control", RateControl.CRF.value),
            max_bitrate=data.get("max_bitrate"),
            buffer_size=data.get("buffer_size"),
            keyframe_interval_sec=data.get("keyframe_interval_sec", 0),
            b_frames=data.get("b_frames"),
            tune=data.get("tune"),
            multipass=data.get("multipass"),
            psycho_aq=data.get("psycho_aq", False),
            gpu_index=data.get("gpu_index", 0),
            film_grain=data.get("film_grain", 0),
            audio_mode=data.get("audio_mode", AudioMode.AUTO.value),
            custom_encoder_opts=data.get("custom_encoder_opts", ""),
            profile_level=data.get("profile_level"),
            codec_profile=data.get("codec_profile"),
        )


_VALID_ENCODER_PRESETS = {
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
}


def normalize_profile_video_codec(value: Any) -> VideoCodec:
    """Normalise a persisted video codec value into a valid VideoCodec."""

    if isinstance(value, VideoCodec):
        return value

    normalized = str(value or VideoCodec.HEVC.value).lower()
    try:
        return VideoCodec(normalized)
    except ValueError:
        return VideoCodec.HEVC


def normalize_profile_preset(value: Any) -> str:
    """Normalize persisted presets so legacy numeric AV1 values map to a valid name."""

    normalized = str(value or "medium").lower()
    if normalized in _VALID_ENCODER_PRESETS:
        return normalized
    # Accept numeric presets used by SVT-AV1 / AOM / VP9
    try:
        int(normalized)
        return normalized
    except ValueError:
        return "slow"


def normalize_profile_container(codec: VideoCodec, value: Any, audio_codec: AudioCodec) -> str:
    """Keep migrated legacy profiles on containers that still fit the chosen codec."""

    normalized = str(value or "mp4").lower().lstrip(".")
    if codec == VideoCodec.HEVC and normalized == "webm":
        return "mkv" if audio_codec == AudioCodec.OPUS else "mp4"
    if codec == VideoCodec.H264 and normalized == "webm":
        return "mp4"
    if codec == VideoCodec.VP9 and normalized in {"mp4", "mov", "avi"}:
        return "webm"
    return normalized


class ProfileManager:
    """Manages compression profiles."""
    
    # Default profiles. Fast, Balanced, and Max / Archival take CRF, preset,
    # and codec from the shared quality ladder so the CLI matches the GUI.
    DEFAULT_PROFILES = [
        CompressionProfile(
            name="Fast",
            profile_type=ProfileType.FAST,
            description=get_step(VideoCodec.H264, QualityRung.QUICK).hint,
            video_codec=VideoCodec.H264,
            crf=get_step(VideoCodec.H264, QualityRung.QUICK).crf,
            preset=get_step(VideoCodec.H264, QualityRung.QUICK).preset,
            audio_codec=AudioCodec.OPUS,
            audio_bitrate=128_000,
            use_hw_accel=True,
            image_quality=76,
        ),
        CompressionProfile(
            name="Balanced",
            profile_type=ProfileType.BALANCED,
            description=get_step(VideoCodec.HEVC, QualityRung.BALANCED).hint,
            video_codec=VideoCodec.HEVC,
            crf=get_step(VideoCodec.HEVC, QualityRung.BALANCED).crf,
            preset=get_step(VideoCodec.HEVC, QualityRung.BALANCED).preset,
            audio_codec=AudioCodec.OPUS,
            audio_bitrate=128_000,
            use_hw_accel=True,
            image_quality=82,
        ),
        CompressionProfile(
            name=ARCHIVAL_PROFILE_NAME,
            profile_type=ProfileType.MAX,
            description=get_step(VideoCodec.SVT_AV1, QualityRung.MAX).hint,
            video_codec=VideoCodec.SVT_AV1,
            crf=get_step(VideoCodec.SVT_AV1, QualityRung.MAX).crf,
            preset=get_step(VideoCodec.SVT_AV1, QualityRung.MAX).preset,
            audio_codec=AudioCodec.OPUS,
            audio_bitrate=96_000,
            video_container="mkv",
            use_hw_accel=False,
            image_quality=90,
        ),
        CompressionProfile(
            name="YouTube Upload",
            profile_type=ProfileType.CUSTOM,
            description="Optimized for YouTube/Social Media - maximum compatibility",
            video_codec=VideoCodec.H264,
            crf=23,
            preset="fast",
            audio_codec=AudioCodec.AAC,
            audio_bitrate=128_000,
            video_container="mp4",
            use_hw_accel=True,
        ),
        CompressionProfile(
            name="Mobile",
            profile_type=ProfileType.CUSTOM,
            description="Smaller files for mobile viewing",
            video_codec=VideoCodec.HEVC,
            crf=26,
            preset="fast",
            audio_codec=AudioCodec.AAC,
            audio_bitrate=128_000,
            video_container="mp4",
            use_hw_accel=True,
        ),
        CompressionProfile(
            name="Streaming",
            profile_type=ProfileType.CUSTOM,
            description="Fast encoding for live streaming",
            video_codec=VideoCodec.HEVC,
            crf=24,
            preset="veryfast",
            audio_codec=AudioCodec.AAC,
            audio_bitrate=128_000,
            video_container="mp4",
            use_hw_accel=True,
        ),
    ]
    
    def __init__(self, config_dir: Optional[Path] = None):
        """
        Initialize profile manager.
        
        Args:
            config_dir: Directory to store custom profiles
        """
        self.config_dir = config_dir or PROFILES_DIR
        self._profiles: Dict[str, CompressionProfile] = {}
        self._load_default_profiles()
        self._load_custom_profiles()
    
    def _load_default_profiles(self):
        """Load default profiles."""
        for profile in self.DEFAULT_PROFILES:
            self._profiles[profile.name] = profile
    
    def _load_custom_profiles(self):
        """Load custom profiles from config directory."""
        if not self.config_dir.exists():
            return
        
        for profile_file in self.config_dir.glob("*.yaml"):
            try:
                with open(profile_file, 'r') as f:
                    data = yaml.safe_load(f)
                    if data and "name" in data:
                        profile = CompressionProfile.from_dict(data)
                        self._profiles[profile.name] = profile
            except Exception as e:
                print(f"Warning: Failed to load profile {profile_file}: {e}")
    
    def get_profile(self, name: str) -> Optional[CompressionProfile]:
        """Get a profile by name."""
        return self._profiles.get(name)
    
    def get_all_profiles(self) -> List[CompressionProfile]:
        """Get all available profiles."""
        return list(self._profiles.values())
    
    def get_profiles_by_type(self, profile_type: ProfileType) -> List[CompressionProfile]:
        """Get profiles filtered by type."""
        return [p for p in self._profiles.values() if p.profile_type == profile_type]
    
    def save_profile(self, profile: CompressionProfile) -> bool:
        """
        Save a custom profile.
        
        Args:
            profile: Profile to save
            
        Returns:
            True if saved successfully
        """
        try:
            self.config_dir.mkdir(parents=True, exist_ok=True)
            
            profile_file = self.config_dir / f"{profile.name.lower().replace(' ', '_')}.yaml"
            
            with open(profile_file, 'w') as f:
                yaml.dump(profile.to_dict(), f, default_flow_style=False)
            
            self._profiles[profile.name] = profile
            return True
        except Exception as e:
            print(f"Failed to save profile: {e}")
            return False
    
    def delete_profile(self, name: str) -> bool:
        """
        Delete a custom profile.
        
        Args:
            name: Profile name to delete
            
        Returns:
            True if deleted successfully
        """
        profile = self._profiles.get(name)
        if not profile:
            return False
        
        # Built-ins stay available even when their type is custom (YouTube, Mobile, Streaming).
        if profile.created_by == "system":
            return False
        if profile.profile_type in [ProfileType.FAST, ProfileType.BALANCED, ProfileType.MAX]:
            return False
        
        try:
            profile_file = self.config_dir / f"{name.lower().replace(' ', '_')}.yaml"
            if profile_file.exists():
                profile_file.unlink()
            
            del self._profiles[name]
            return True
        except Exception as e:
            print(f"Failed to delete profile: {e}")
            return False
    
    def create_custom_profile(
        self,
        name: str,
        base_profile: Optional[str] = None,
        **kwargs
    ) -> CompressionProfile:
        """
        Create a custom profile based on an existing one.
        
        Args:
            name: New profile name
            base_profile: Name of profile to base on
            **kwargs: Override settings
            
        Returns:
            New CompressionProfile
        """
        if base_profile and base_profile in self._profiles:
            base = self._profiles[base_profile]
            data = base.to_dict()
        else:
            data = CompressionProfile(
                name=name,
                profile_type=ProfileType.CUSTOM,
            ).to_dict()
        
        data.update(kwargs)
        data["name"] = name
        data["profile_type"] = ProfileType.CUSTOM.value
        data["created_by"] = "user"
        
        return CompressionProfile.from_dict(data)


def calculate_bitrate_for_target_size(
    target_size_mb: int,
    duration_seconds: float,
    audio_bitrate: int = 192_000,
    container_overhead_ratio: float = 0.02,
    min_video_bitrate: int = 120_000,
) -> int:
    """
    Calculate video bitrate needed to achieve target file size.
    
    Args:
        target_size_mb: Target file size in megabytes
        duration_seconds: Video duration in seconds
        audio_bitrate: Audio bitrate in bits per second
        
    Returns:
        Video bitrate in bits per second
    """
    # Convert target size to bits and reserve a practical container overhead margin.
    target_bits = target_size_mb * 8 * 1_000_000
    target_bits = int(target_bits * max(0.5, 1.0 - container_overhead_ratio))
    
    # Subtract audio size
    audio_bits = audio_bitrate * duration_seconds
    video_bits = target_bits - audio_bits
    
    # Calculate video bitrate
    if duration_seconds > 0:
        video_bitrate = int(video_bits / duration_seconds)
        return max(video_bitrate, min_video_bitrate)
    
    return 1_000_000  # Default 1 Mbps


def normalize_target_size_mode(value: Optional[str]) -> TargetSizeMode:
    """Return a valid target-size mode, defaulting to auto."""

    try:
        return TargetSizeMode((value or TargetSizeMode.AUTO.value).lower())
    except ValueError:
        return TargetSizeMode.AUTO


def resolve_target_size_mode(
    mode: Optional[str],
    *,
    target_size_mb: Optional[int],
    source_size_bytes: int,
    duration_seconds: float,
    codec: VideoCodec,
    use_hw_accel: bool = True,
    profile_type: Optional[ProfileType] = None,
) -> TargetSizeMode:
    """Resolve an explicit or automatic target-size mode for a specific source."""

    requested_mode = normalize_target_size_mode(mode)
    if requested_mode != TargetSizeMode.AUTO:
        return requested_mode

    if not target_size_mb:
        return TargetSizeMode.BALANCED

    if source_size_bytes <= 0:
        if profile_type == ProfileType.FAST:
            return TargetSizeMode.FAST
        if profile_type == ProfileType.MAX:
            return TargetSizeMode.STRICT
        return TargetSizeMode.BALANCED

    source_size_mb = max(1.0, source_size_bytes / 1_000_000)
    target_ratio = max(0.01, target_size_mb / source_size_mb)
    short_clip = 0 < duration_seconds <= 240
    long_form = duration_seconds >= 1_200
    aggressive_target = target_ratio <= 0.55
    mild_target = target_ratio >= 0.80
    heavy_software_codec = codec == VideoCodec.VP9 and not use_hw_accel

    if aggressive_target:
        return TargetSizeMode.STRICT

    if target_ratio <= 0.65 and (long_form or source_size_mb >= 700):
        return TargetSizeMode.STRICT

    if heavy_software_codec and long_form:
        return TargetSizeMode.BALANCED

    if profile_type == ProfileType.FAST and target_ratio >= 0.65:
        return TargetSizeMode.FAST

    if mild_target and (short_clip or use_hw_accel or source_size_mb <= 300):
        return TargetSizeMode.FAST

    if profile_type == ProfileType.MAX and target_ratio <= 0.75:
        return TargetSizeMode.STRICT

    return TargetSizeMode.BALANCED


def normalize_resource_governor(value: Optional[str]) -> ResourceGovernor:
    """Return a valid resource-governor value, defaulting to auto."""

    try:
        return ResourceGovernor((value or ResourceGovernor.AUTO.value).lower())
    except ValueError:
        return ResourceGovernor.AUTO


def resolve_requested_target_size_mb(
    explicit_target_mb: Optional[int],
    reduction_percent: Optional[int],
    original_size_bytes: int,
) -> Optional[int]:
    """Resolve target-size input from an explicit MB target or reduction percentage."""

    if explicit_target_mb and explicit_target_mb > 0:
        return int(explicit_target_mb)

    if reduction_percent and reduction_percent > 0 and original_size_bytes > 0:
        original_size_mb = original_size_bytes / 1_000_000
        target_size_mb = original_size_mb * (1 - (reduction_percent / 100))
        return max(1, int(round(target_size_mb)))

    return None


def estimate_container_overhead_ratio(
    container: str,
    codec: VideoCodec,
    duration_seconds: float,
) -> float:
    """Estimate muxing overhead for a container/codec combination."""

    base = {
        "mp4": 0.018,
        "mkv": 0.012,
        "webm": 0.014,
        "mov": 0.022,
        "avi": 0.03,
    }.get((container or "mp4").lower(), 0.018)

    if codec in {VideoCodec.AV1, VideoCodec.SVT_AV1, VideoCodec.VP9}:
        base += 0.002
    if duration_seconds >= 3_600:
        base += 0.002
    if duration_seconds <= 60:
        base += 0.003

    return max(0.01, min(base, 0.05))


def build_target_size_plan(
    target_size_mb: int,
    duration_seconds: float,
    audio_bitrate: int,
    container: str,
    codec: VideoCodec,
    mode: Optional[str] = None,
) -> TargetSizePlan:
    """Build a policy-aware target-size plan for a job."""

    resolved_mode = normalize_target_size_mode(mode)
    if resolved_mode == TargetSizeMode.AUTO:
        resolved_mode = TargetSizeMode.BALANCED
    tolerance_ratio = {
        TargetSizeMode.FAST: 0.10,
        TargetSizeMode.BALANCED: 0.04,
        TargetSizeMode.STRICT: 0.02,
        TargetSizeMode.EXACT: 0.005,
    }[resolved_mode]
    max_attempts = {
        TargetSizeMode.FAST: 1,
        TargetSizeMode.BALANCED: 3,
        TargetSizeMode.STRICT: 5,
        TargetSizeMode.EXACT: 8,
    }[resolved_mode]
    min_video_bitrate = {
        TargetSizeMode.FAST: 180_000,
        TargetSizeMode.BALANCED: 140_000,
        TargetSizeMode.STRICT: 120_000,
        TargetSizeMode.EXACT: 8_000,
    }[resolved_mode]
    overhead_ratio = estimate_container_overhead_ratio(container, codec, duration_seconds)
    resolved_audio_bitrate = max(32_000, int(audio_bitrate or 0))
    video_bitrate = calculate_bitrate_for_target_size(
        target_size_mb=target_size_mb,
        duration_seconds=duration_seconds,
        audio_bitrate=resolved_audio_bitrate,
        container_overhead_ratio=overhead_ratio,
        min_video_bitrate=min_video_bitrate,
    )
    media_floor_bits = (resolved_audio_bitrate + min_video_bitrate) * max(duration_seconds, 0.0)
    minimum_size_bytes = max(
        1,
        int((media_floor_bits / 8) / max(0.5, 1.0 - overhead_ratio)),
    )

    warning = ""
    reserved_audio_mb = (resolved_audio_bitrate * max(duration_seconds, 0.0)) / 8 / 1_000_000
    minimum_size_mb = minimum_size_bytes / 1_000_000
    if target_size_mb * 1_000_000 < minimum_size_bytes:
        warning = (
            f"Target is below the estimated floor of {minimum_size_mb:.1f} MB "
            "for this duration and current audio settings."
        )
    elif resolved_mode == TargetSizeMode.EXACT:
        warning = (
            "Exact mode tries hardware first and asks before a software retry. "
            "It may lower audio bitrate, frame rate, and resolution, "
            "and pad the final file to hit the requested size."
        )
    elif target_size_mb <= max(8, int(round(reserved_audio_mb * 1.2))):
        warning = "Target is close to the audio budget alone; quality may drop sharply."

    return TargetSizePlan(
        mode=resolved_mode,
        target_size_mb=max(1, int(target_size_mb)),
        target_size_bytes=max(1, int(target_size_mb * 1_000_000)),
        minimum_size_bytes=minimum_size_bytes,
        video_bitrate=video_bitrate,
        audio_bitrate=resolved_audio_bitrate,
        min_video_bitrate=min_video_bitrate,
        tolerance_ratio=tolerance_ratio,
        max_attempts=max_attempts,
        overhead_ratio=overhead_ratio,
        warning=warning,
    )


def is_within_target_size_tolerance(actual_size_bytes: int, plan: TargetSizePlan) -> bool:
    """Return True when the encoded file is acceptably close to the target."""

    if actual_size_bytes <= 0:
        return False

    lower_bound = int(plan.target_size_bytes * (1 - plan.tolerance_ratio))
    upper_bound = int(plan.target_size_bytes * (1 + plan.tolerance_ratio))
    return lower_bound <= actual_size_bytes <= upper_bound


def refine_target_size_bitrate(
    current_video_bitrate: int,
    actual_size_bytes: int,
    plan: TargetSizePlan,
) -> int:
    """Adjust the video bitrate for the next attempt based on the previous output."""

    if actual_size_bytes <= 0:
        return current_video_bitrate

    target_ratio = plan.target_size_bytes / actual_size_bytes
    projected_bitrate = int(current_video_bitrate * target_ratio)

    damping = {
        TargetSizeMode.FAST: 1.0,
        TargetSizeMode.BALANCED: 0.9,
        TargetSizeMode.STRICT: 0.97,
        TargetSizeMode.EXACT: 1.0,
    }[plan.mode]
    adjusted = int(current_video_bitrate + ((projected_bitrate - current_video_bitrate) * damping))

    max_upscale = int(current_video_bitrate * 1.8)
    max_downscale = int(current_video_bitrate * (0.25 if plan.mode == TargetSizeMode.EXACT else 0.45))
    adjusted = max(max_downscale, min(adjusted, max_upscale))
    return max(plan.min_video_bitrate, adjusted)


def describe_target_size_plan(plan: TargetSizePlan, source_size_bytes: int) -> str:
    """Create a concise preview string for the UI and CLI."""

    source_size_mb = source_size_bytes / 1_000_000 if source_size_bytes > 0 else 0
    tolerance_pct = int(round(plan.tolerance_ratio * 100))
    parts = [
        f"{plan.mode.value} mode",
        f"Source {source_size_mb:.1f} MB",
        f"target {plan.target_size_mb} MB",
        f"{plan.video_bitrate // 1000} kbps video",
        f"±{tolerance_pct}%",
    ]
    if plan.mode == TargetSizeMode.EXACT:
        parts.append("pads exact bytes when under")
    if plan.minimum_size_bytes > plan.target_size_bytes:
        parts.append(f"floor ~{plan.minimum_size_bytes / 1_000_000:.1f} MB")
    if plan.warning:
        parts.append(plan.warning)
    return "  ·  ".join(parts)


def calculate_target_size_bitrate(
    original_size_mb: int,
    reduction_percent: int,
    duration_seconds: float,
    audio_bitrate: int = 192_000,
) -> int:
    """
    Calculate bitrate for a target reduction percentage.
    
    Args:
        original_size_mb: Original file size in megabytes
        reduction_percent: Target reduction percentage (e.g., 50 for 50% smaller)
        duration_seconds: Video duration in seconds
        audio_bitrate: Audio bitrate in bits per second
        
    Returns:
        Video bitrate in bits per second
    """
    target_size_mb = max(1, int(round(original_size_mb * (1 - reduction_percent / 100))))
    return calculate_bitrate_for_target_size(
        target_size_mb,
        duration_seconds,
        audio_bitrate,
    )


QUICK_COMPRESS_ORDER = [
    QUICK_COMPRESS_LITE_LABEL,
    "Balanced",
    QUICK_COMPRESS_MAX_LABEL,
]

_QUICK_COMPRESS_ALIASES = {
    "lite": QUICK_COMPRESS_LITE_LABEL,
    "quick": QUICK_COMPRESS_LITE_LABEL,
    "quick lite": QUICK_COMPRESS_LITE_LABEL,
    "balanced": "Balanced",
    "max": QUICK_COMPRESS_MAX_LABEL,
    "hevc max": QUICK_COMPRESS_MAX_LABEL,
}


def normalize_quick_compress_name(name: str) -> str:
    """Map saved Quick Compress labels, including the old Lite/Quick/Max names."""

    return _QUICK_COMPRESS_ALIASES.get(str(name or "").strip().lower(), "Balanced")


def quick_compress_rung(name: str) -> QualityRung:
    """Rung selected by a Quick Compress button, after alias normalization."""

    normalized = normalize_quick_compress_name(name)
    if normalized == QUICK_COMPRESS_LITE_LABEL:
        return QualityRung.QUICK
    if normalized == QUICK_COMPRESS_MAX_LABEL:
        return QualityRung.MAX
    return QualityRung.BALANCED


def build_quick_compress_profiles() -> Dict[str, CompressionProfile]:
    """Quick Lite (H.264) / Balanced (HEVC) / HEVC Max for the context menu.

    Video CRF and preset come from that codec's ladder rung. Audio and
    container stay with the quick-compress choices. HEVC Max is not
    Max / Archival, and Quick Lite is not AV1.
    """

    specs = (
        (
            QUICK_COMPRESS_LITE_LABEL,
            VideoCodec.H264,
            QualityRung.QUICK,
            ProfileType.FAST,
            AudioCodec.AAC,
            128_000,
            "mp4",
            76,
        ),
        (
            "Balanced",
            VideoCodec.HEVC,
            QualityRung.BALANCED,
            ProfileType.BALANCED,
            AudioCodec.AAC,
            128_000,
            "mp4",
            82,
        ),
        (
            QUICK_COMPRESS_MAX_LABEL,
            VideoCodec.HEVC,
            QualityRung.MAX,
            ProfileType.MAX,
            AudioCodec.OPUS,
            96_000,
            "mkv",
            90,
        ),
    )
    profiles: Dict[str, CompressionProfile] = {}
    for name, codec, rung, profile_type, audio, bitrate, container, image_quality in specs:
        step = get_step(codec, rung)
        profiles[name] = CompressionProfile(
            name=name,
            profile_type=profile_type,
            description=step.hint,
            video_codec=codec,
            crf=step.crf,
            preset=step.preset,
            audio_codec=audio,
            audio_bitrate=bitrate,
            video_container=container,
            use_hw_accel=step.allow_hw_accel,
            image_quality=image_quality,
        )
    return profiles


def retarget_quick_compress_fallback(
    profile: CompressionProfile,
    fallback_codec: VideoCodec,
    selected_name: str,
) -> CompressionProfile:
    """When HEVC is missing, apply the same rung on the fallback codec.

    Leaving the HEVC CRF and preset in place would encode H.264 (or SVT-AV1)
    with HEVC rung numbers. The ladder step for the fallback codec replaces
    both, and SVT-AV1 turns hardware off.
    """

    step = get_step(fallback_codec, quick_compress_rung(selected_name))
    profile.video_codec = fallback_codec
    profile.crf = step.crf
    profile.preset = step.preset
    profile.use_hw_accel = step.allow_hw_accel and not step.force_software
    profile.description = step.hint
    return profile


def apply_cli_quality_ladder(
    profile: CompressionProfile,
    profile_flag: str,
    *,
    codec_overridden: bool,
    crf_overridden: bool,
    preset_overridden: bool,
    hw_overridden: bool,
) -> None:
    """Retarget CRF/preset when --profile is a rung and --codec changes lane.

    Explicit --crf / --preset still win. SVT-AV1 always forces software.
    Switching onto HEVC Max (``--profile max --codec hevc``) restores that
    lane's hardware default unless the user passed --no-hw-accel.
    """

    rung = PROFILE_FLAG_RUNGS.get(profile_flag)
    if rung is None:
        return
    try:
        step = get_step(profile.video_codec, rung)
    except KeyError:
        return
    if not crf_overridden:
        profile.crf = step.crf
    if not preset_overridden:
        profile.preset = step.preset
    if step.force_software:
        profile.use_hw_accel = False
    elif codec_overridden and not hw_overridden:
        profile.use_hw_accel = step.allow_hw_accel


def profile_video_ffmpeg_args(profile: CompressionProfile) -> List[str]:
    """Software video args for a profile, with audio stripped off."""

    settings = profile.to_codec_settings()
    settings.disable_audio = True
    args = settings.to_ffmpeg_args(hw_encoder=None)
    if args[-1:] == ["-an"]:
        args = args[:-1]
    return args