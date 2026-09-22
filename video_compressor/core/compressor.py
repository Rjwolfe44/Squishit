"""
Main media compression engine.
Handles video and image compression, progress tracking, and job execution.
"""

import subprocess
import logging
import re
import os
import platform
import time
import json
import shutil
import tempfile
import sys
import shlex
import uuid
from dataclasses import dataclass
from typing import Optional, List, Dict, Any, Callable, Tuple, Union
from pathlib import Path
from enum import Enum
import threading
import queue

from PIL import Image, ImageOps, ImageSequence, features

from .codecs import VideoCodec, AudioCodec, CodecSettings, CodecManager, AudioMode, RateControl, ENCODER_REGISTRY
from .eta import ETACalculator
from .profiles import (
    CompressionProfile,
    TargetSizeMode,
    TargetSizePlan,
    build_target_size_plan,
    describe_target_size_plan,
    is_within_target_size_tolerance,
    resolve_target_size_mode,
    refine_target_size_bitrate,
    resolve_requested_target_size_mb,
)
from .hardware import HardwareDetector, get_hardware_detector
from .utils import (
    format_size, format_time, format_bitrate,
    validate_video_file, validate_image_file, detect_media_type,
    calculate_reduction, get_audio_extension, get_image_extension, get_video_extension,
    parse_timecode, sanitize_filename,
)

logger = logging.getLogger(__name__)


class JobStatus(Enum):
    """Status of a compression job."""
    QUEUED = "queued"
    ANALYZING = "analyzing"
    COMPRESSING = "compressing"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class VideoInfo:
    """Information about a video file."""
    filepath: Path
    duration: float = 0.0
    size: int = 0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    video_bitrate: int = 0
    audio_bitrate: int = 0
    total_bitrate: int = 0
    frame_count: int = 0
    
    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "Unknown"
    
    @property
    def resolution_label(self) -> str:
        """Get resolution label (e.g., '1080p', '4K')."""
        if self.height >= 2160:
            return "4K"
        elif self.height >= 1440:
            return "1440p"
        elif self.height >= 1080:
            return "1080p"
        elif self.height >= 720:
            return "720p"
        elif self.height >= 480:
            return "480p"
        elif self.height > 0:
            return f"{self.height}p"
        return "Unknown"


@dataclass
class ImageInfo:
    """Information about an image file."""
    filepath: Path
    size: int = 0
    width: int = 0
    height: int = 0
    image_format: str = ""
    mode: str = ""
    has_alpha: bool = False
    frame_count: int = 1
    duration: float = 0.0

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "Unknown"

    @property
    def resolution_label(self) -> str:
        if self.height >= 4320:
            return "8K"
        if self.height >= 2160:
            return "4K"
        if self.height >= 1440:
            return "1440p"
        if self.height >= 1080:
            return "1080p"
        if self.height >= 720:
            return "720p"
        if self.height >= 480:
            return "480p"
        if self.height > 0:
            return f"{self.height}p"
        return "Unknown"


MediaInfo = Union[VideoInfo, ImageInfo]


@dataclass
class CompressionResult:
    """Result of a compression job."""
    success: bool
    input_file: Path
    output_file: Optional[Path] = None
    original_size: int = 0
    compressed_size: int = 0
    duration_seconds: float = 0.0
    encoding_time: float = 0.0
    video_codec: str = ""
    audio_codec: str = ""
    average_bitrate: int = 0
    error_message: str = ""
    media_type: str = "video"
    output_format: str = ""
    kept_original: bool = False
    note: str = ""
    cancelled: bool = False
    skipped: bool = False
    encoder_name: str = ""
    threads_used: int = 0
    attempt_count: int = 0
    
    @property
    def reduction(self) -> str:
        return calculate_reduction(self.original_size, self.compressed_size)
    
    @property
    def reduction_percent(self) -> float:
        if self.original_size <= 0:
            return 0.0
        return (1 - self.compressed_size / self.original_size) * 100


@dataclass
class CompressionJob:
    """A single compression job."""
    id: str
    input_file: Path
    output_file: Path
    profile: CompressionProfile
    status: JobStatus = JobStatus.QUEUED
    progress: float = 0.0
    current_frame: int = 0
    total_frames: int = 0
    speed: float = 0.0  # frames per second
    throughput_mb_s: float = 0.0
    eta: float = 0.0  # seconds
    media_type: str = "video"
    video_info: Optional[MediaInfo] = None
    result: Optional[CompressionResult] = None
    error: str = ""
    encoder_name: str = ""
    threads_used: int = 0
    attempt_count: int = 0
    parallel_jobs: int = 1
    
    def __post_init__(self):
        if isinstance(self.input_file, str):
            self.input_file = Path(self.input_file)
        if isinstance(self.output_file, str):
            self.output_file = Path(self.output_file)


@dataclass
class TargetSizeSearchState:
    """Track bitrate bounds discovered while chasing a target size."""

    # Highest bitrate known to land at or under the target.
    lower_bitrate: Optional[int] = None
    lower_size_bytes: Optional[int] = None
    # Lowest bitrate known to still overshoot the target.
    upper_bitrate: Optional[int] = None
    upper_size_bytes: Optional[int] = None


class VideoCompressor:
    """
    Main video compression engine.
    
    Handles video analysis, compression, and progress tracking.
    """
    
    def __init__(self, hw_detector: Optional[HardwareDetector] = None):
        """
        Initialize the compressor.
        
        Args:
            hw_detector: Hardware detector instance (optional)
        """
        self.hw_detector = hw_detector or get_hardware_detector()
        self._ffmpeg_path = self._find_ffmpeg()
        self._ffprobe_path = self._find_ffprobe()
        self._cjxl_path = self._find_cjxl()
        self.codec_manager = CodecManager(self._ffmpeg_path)
        
        # Active jobs
        self._jobs: Dict[str, CompressionJob] = {}
        self._processes: Dict[str, subprocess.Popen] = {}
        self._paused: Dict[str, bool] = {}
        self._cancelled: Dict[str, bool] = {}
        
        # Progress callback
        self._progress_callback: Optional[Callable] = None
    
    def _find_ffmpeg(self) -> str:
        """Find FFmpeg executable."""
        # Try common locations
        candidates = ["ffmpeg", "ffmpeg.exe"]
        
        # Check if bundled with app
        bundle_candidates = [
            Path(__file__).parent.parent / "bin" / "ffmpeg",
            Path(__file__).parent.parent.parent / "vendor" / "ffmpeg" / "ffmpeg.exe",
            Path(getattr(sys, "_MEIPASS", Path.cwd())) / "vendor" / "ffmpeg" / "ffmpeg.exe",
        ]
        for bundle_path in bundle_candidates:
            candidate_path = bundle_path
            if platform.system() == "Windows" and bundle_path.suffix.lower() != ".exe":
                candidate_path = bundle_path.with_suffix(".exe")
            if candidate_path.exists():
                return str(candidate_path)
        
        # Try system PATH
        for candidate in candidates:
            try:
                result = subprocess.run(
                    [candidate, "-version"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                )
                if result.returncode == 0:
                    return candidate
            except FileNotFoundError:
                continue
        
        return "ffmpeg"  # Default, will error later if not found
    
    def _find_ffprobe(self) -> str:
        """Find FFprobe executable."""
        candidates = ["ffprobe", "ffprobe.exe"]
        
        bundle_candidates = [
            Path(__file__).parent.parent / "bin" / "ffprobe",
            Path(__file__).parent.parent.parent / "vendor" / "ffmpeg" / "ffprobe.exe",
            Path(getattr(sys, "_MEIPASS", Path.cwd())) / "vendor" / "ffmpeg" / "ffprobe.exe",
        ]
        for bundle_path in bundle_candidates:
            candidate_path = bundle_path
            if platform.system() == "Windows" and bundle_path.suffix.lower() != ".exe":
                candidate_path = bundle_path.with_suffix(".exe")
            if candidate_path.exists():
                return str(candidate_path)
        
        for candidate in candidates:
            try:
                result = subprocess.run(
                    [candidate, "-version"],
                    capture_output=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                )
                if result.returncode == 0:
                    return candidate
            except FileNotFoundError:
                continue
        
        return "ffprobe"

    def _find_cjxl(self) -> Optional[str]:
        """Find the JPEG XL encoder if it is available."""
        candidates = ["cjxl", "cjxl.exe"]
        bundle_dir = Path(__file__).parent.parent / "bin"
        extra_bundle_dirs = [
            Path(__file__).parent.parent.parent / "vendor" / "ffmpeg",
            Path(getattr(sys, "_MEIPASS", Path.cwd())) / "vendor" / "ffmpeg",
        ]
        for candidate in candidates:
            bundled = bundle_dir / candidate
            if bundled.exists():
                return str(bundled)
            for extra_dir in extra_bundle_dirs:
                bundled = extra_dir / candidate
                if bundled.exists():
                    return str(bundled)
            discovered = shutil.which(candidate)
            if discovered:
                return discovered
        return None
    
    def set_progress_callback(self, callback: Callable):
        """Set callback for progress updates."""
        self._progress_callback = callback
    
    def _report_progress(self, job: CompressionJob):
        """Report progress to callback."""
        if self._progress_callback:
            self._progress_callback(job)

    def _cancel_result(self, job: CompressionJob) -> CompressionResult:
        """Finalize a job as cancelled and clean up any partial output."""
        try:
            if job.output_file.exists():
                job.output_file.unlink()
        except Exception:
            pass

        job.status = JobStatus.CANCELLED
        job.error = "Cancelled by user"
        self._report_progress(job)
        return CompressionResult(
            success=False,
            input_file=job.input_file,
            error_message="Cancelled by user",
            cancelled=True,
        )
    
    def analyze_video(self, filepath: Path) -> Optional[VideoInfo]:
        """
        Analyze a video file to get its properties.
        
        Args:
            filepath: Path to video file
            
        Returns:
            VideoInfo object or None if analysis fails
        """
        filepath = Path(filepath)
        
        # Validate file
        is_valid, error = validate_video_file(filepath)
        if not is_valid:
            logger.error(f"Invalid video file: {error}")
            return None
        
        try:
            # Use FFprobe to get video info
            cmd = [
                self._ffprobe_path,
                "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                str(filepath)
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
            )
            
            if result.returncode != 0:
                logger.error(f"FFprobe failed: {result.stderr}")
                return None
            
            data = json.loads(result.stdout)
            
            # Parse format info
            format_info = data.get("format", {})
            duration = float(format_info.get("duration", 0))
            size = int(format_info.get("size", 0))
            total_bitrate = int(format_info.get("bit_rate", 0))
            
            # Find video and audio streams
            video_stream = None
            audio_stream = None
            
            for stream in data.get("streams", []):
                codec_type = stream.get("codec_type", "")
                if codec_type == "video" and video_stream is None:
                    video_stream = stream
                elif codec_type == "audio" and audio_stream is None:
                    audio_stream = stream
            
            if not video_stream:
                logger.error("No video stream found")
                return None
            
            # Parse video stream
            width = int(video_stream.get("width", 0))
            height = int(video_stream.get("height", 0))
            video_codec = video_stream.get("codec_name", "")
            video_bitrate = int(video_stream.get("bit_rate", 0) or total_bitrate)
            
            # Parse frame rate
            fps_str = video_stream.get("r_frame_rate", "0/1")
            if "/" in fps_str:
                num, den = fps_str.split("/")
                fps = float(num) / float(den) if float(den) > 0 else 0
            else:
                fps = float(fps_str)
            
            # Calculate frame count
            nb_frames = video_stream.get("nb_frames")
            try:
                frame_count = int(nb_frames) if nb_frames not in (None, "N/A") else 0
            except (TypeError, ValueError):
                frame_count = 0
            if frame_count <= 0:
                frame_count = int(duration * fps) if duration > 0 and fps > 0 else 0
            
            # Parse audio stream
            audio_codec = ""
            audio_bitrate = 0
            if audio_stream:
                audio_codec = audio_stream.get("codec_name", "")
                audio_bitrate = int(audio_stream.get("bit_rate", 0))
            
            return VideoInfo(
                filepath=filepath,
                duration=duration,
                size=size,
                width=width,
                height=height,
                fps=fps,
                video_codec=video_codec,
                audio_codec=audio_codec,
                video_bitrate=video_bitrate,
                audio_bitrate=audio_bitrate,
                total_bitrate=total_bitrate,
                frame_count=frame_count
            )
            
        except Exception as e:
            logger.error(f"Failed to analyze video: {e}")
            return None

    def analyze_image(self, filepath: Path) -> Optional[ImageInfo]:
        """Analyze an image file to get its properties."""
        filepath = Path(filepath)

        is_valid, error = validate_image_file(filepath)
        if not is_valid:
            logger.error(f"Invalid image file: {error}")
            return None

        try:
            with Image.open(filepath) as image:
                width, height = image.size
                frame_count = getattr(image, "n_frames", 1)
                has_alpha = image.mode in {"RGBA", "LA"} or "transparency" in image.info

                return ImageInfo(
                    filepath=filepath,
                    size=filepath.stat().st_size,
                    width=width,
                    height=height,
                    image_format=(image.format or filepath.suffix.lstrip(".")).lower(),
                    mode=image.mode,
                    has_alpha=has_alpha,
                    frame_count=frame_count,
                )
        except Exception as e:
            logger.error(f"Failed to analyze image: {e}")
            return None

    def analyze_media(self, filepath: Path) -> Optional[MediaInfo]:
        """Analyze a supported video or image file."""
        media_type = detect_media_type(filepath)
        if media_type == "image":
            return self.analyze_image(filepath)
        return self.analyze_video(filepath)

    def _clone_profile(self, profile: CompressionProfile) -> CompressionProfile:
        """Return a detached copy of a profile so caller defaults are not mutated."""
        return CompressionProfile.from_dict(profile.to_dict())

    def _normalize_requested_video_codec(self, profile: CompressionProfile) -> None:
        """No-op: AV1/SVT-AV1 are now fully supported output codecs."""
        pass

    def _effective_audio_codec_name(self, profile: CompressionProfile) -> str:
        """Return the audio codec that will actually be present in the output."""

        return "none" if getattr(profile, "disable_audio", False) else profile.audio_codec.value

    def _estimate_target_total_bitrate(self, target_size_mb: Optional[int], duration_seconds: float) -> int:
        """Estimate total bitrate implied by a requested target size."""

        if not target_size_mb or duration_seconds <= 0:
            return 0
        return max(1, int((target_size_mb * 8 * 1_000_000) / duration_seconds))

    def _pick_exact_target_codec(self, media_info: VideoInfo, prefer_two_pass: bool = False) -> VideoCodec:
        """Pick a software codec for exact mode, preferring efficiency over speed."""

        preferred_codecs = [VideoCodec.HEVC, VideoCodec.VP9, VideoCodec.H264]

        for codec in preferred_codecs:
            if self.codec_manager.is_codec_available(codec):
                return codec
        return self.codec_manager.get_best_codec(prefer_efficiency=True, hw_vendor=None)

    def _recommend_exact_audio_bitrate(
        self,
        target_size_mb: Optional[int],
        duration_seconds: float,
        current_audio_bitrate: int,
        audio_policy: str = "reduce",
    ) -> int:
        """Choose a harsh but usable audio bitrate for exact mode."""

        if (audio_policy or "reduce").lower() == "keep":
            return max(8_000, current_audio_bitrate or 96_000)

        total_bitrate = self._estimate_target_total_bitrate(target_size_mb, duration_seconds)
        if total_bitrate <= 0:
            return max(8_000, min(current_audio_bitrate or 96_000, 96_000))

        share = 0.14 if (audio_policy or "reduce").lower() == "drop" else 0.16
        if total_bitrate <= 500_000:
            share = 0.12
        if total_bitrate <= 180_000:
            share = 0.08

        recommended = int(total_bitrate * share)
        ceiling = min(current_audio_bitrate or 96_000, 96_000)
        return max(8_000, min(ceiling, recommended))

    def _recommend_exact_resolution_limit(
        self,
        media_info: VideoInfo,
        target_size_mb: Optional[int],
        current_limit: Optional[int],
    ) -> Optional[int]:
        """Suggest a harsh resolution cap for exact mode at very low target bitrates."""

        total_bitrate = self._estimate_target_total_bitrate(target_size_mb, media_info.duration)
        if total_bitrate <= 0:
            return current_limit

        source_limit = current_limit or media_info.height or 0
        if source_limit <= 0:
            return current_limit

        suggested = source_limit
        if total_bitrate <= 80_000:
            suggested = 240
        elif total_bitrate <= 140_000:
            suggested = 360
        elif total_bitrate <= 240_000:
            suggested = 480
        elif total_bitrate <= 450_000:
            suggested = 720
        elif total_bitrate <= 900_000:
            suggested = 1080

        return min(source_limit, suggested)

    def _recommend_exact_frame_rate(
        self,
        media_info: VideoInfo,
        target_size_mb: Optional[int],
        current_frame_rate: Optional[int],
    ) -> Optional[int]:
        """Suggest a frame-rate cap for exact mode at very low target bitrates."""

        total_bitrate = self._estimate_target_total_bitrate(target_size_mb, media_info.duration)
        if total_bitrate <= 0:
            return current_frame_rate

        source_fps = current_frame_rate or int(round(media_info.fps or 0)) or None
        if source_fps is None:
            return current_frame_rate

        suggested = source_fps
        if total_bitrate <= 80_000:
            suggested = 10
        elif total_bitrate <= 140_000:
            suggested = 12
        elif total_bitrate <= 240_000:
            suggested = 15
        elif total_bitrate <= 450_000:
            suggested = 24
        elif total_bitrate <= 900_000:
            suggested = 30

        return min(source_fps, suggested)

    def _prepare_target_size_profile(
        self,
        profile: CompressionProfile,
        media_info: Optional[VideoInfo] = None,
    ) -> CompressionProfile:
        """Prepare an encode profile for advanced target-size modes."""

        prepared = self._clone_profile(profile)
        self._normalize_requested_video_codec(prepared)
        if media_info is None:
            return prepared

        effective_mode = self._resolve_effective_target_mode(prepared, media_info)
        if effective_mode != TargetSizeMode.EXACT:
            return prepared

        prepared.target_size_mode = TargetSizeMode.EXACT.value

        target_size_mb = resolve_requested_target_size_mb(
            prepared.target_size_mb,
            prepared.target_reduction_percent,
            media_info.size,
        )
        exact_audio_policy = (prepared.exact_audio_policy or "reduce").lower()
        prepared.disable_audio = False
        prepared.use_hw_accel = False
        prepared.video_codec = self._pick_exact_target_codec(
            media_info,
            prefer_two_pass=bool(prepared.exact_two_pass),
        )

        if exact_audio_policy != "keep" and self.codec_manager.is_encoder_available(AudioCodec.OPUS.ffmpeg_encoder):
            prepared.audio_codec = AudioCodec.OPUS
            prepared.video_container = self.codec_manager.get_compatible_container(prepared.video_codec, "mkv").value
        else:
            prepared.video_container = self.codec_manager.get_compatible_container(
                prepared.video_codec,
                prepared.video_container,
            ).value

        prepared.audio_bitrate = self._recommend_exact_audio_bitrate(
            target_size_mb,
            media_info.duration,
            prepared.audio_bitrate or media_info.audio_bitrate or AudioCodec.AAC.default_bitrate,
            audio_policy=exact_audio_policy,
        )
        prepared.max_resolution = self._recommend_exact_resolution_limit(
            media_info,
            target_size_mb,
            prepared.max_resolution,
        )
        prepared.frame_rate = self._recommend_exact_frame_rate(
            media_info,
            target_size_mb,
            prepared.frame_rate,
        )

        if prepared.video_codec in {VideoCodec.HEVC, VideoCodec.VP9}:
            prepared.preset = "slow"
        else:
            prepared.preset = "medium"

        return prepared

    def _apply_exact_target_fallback(self, profile: CompressionProfile, media_info: VideoInfo) -> Optional[str]:
        """Apply the next exact-target fallback step and return a short description."""

        exact_audio_policy = (profile.exact_audio_policy or "reduce").lower()
        if exact_audio_policy != "keep" and not profile.disable_audio:
            for bitrate in [96_000, 64_000, 48_000, 32_000, 24_000, 16_000, 12_000, 8_000]:
                if profile.audio_bitrate > bitrate:
                    profile.audio_bitrate = bitrate
                    return f"audio {bitrate // 1000} kbps"
            if exact_audio_policy == "drop":
                profile.disable_audio = True
                return "audio removed"

        current_fps = profile.frame_rate or int(round(media_info.fps or 0)) or None
        if current_fps:
            for fps in [30, 24, 20, 15, 12, 10, 8, 6]:
                if current_fps > fps and (profile.frame_rate is None or profile.frame_rate > fps):
                    profile.frame_rate = fps
                    return f"{fps} fps"

        current_limit = profile.max_resolution or media_info.height or 0
        for limit in [1080, 900, 720, 540, 480, 360, 240, 144]:
            if current_limit > limit and (profile.max_resolution is None or profile.max_resolution > limit):
                profile.max_resolution = limit
                return f"{limit}p"

        return None

    def _pad_output_to_target_size(self, output_file: Path, target_size_bytes: int) -> int:
        """Pad a completed output file to the exact requested size."""

        if target_size_bytes <= 0 or not output_file.exists():
            return 0

        current_size = output_file.stat().st_size
        if current_size >= target_size_bytes:
            return 0

        padding_bytes = target_size_bytes - current_size
        zero_chunk = b"\0" * min(1_048_576, padding_bytes)
        with output_file.open("ab") as handle:
            remaining = padding_bytes
            while remaining > 0:
                chunk_size = min(len(zero_chunk), remaining)
                handle.write(zero_chunk[:chunk_size])
                remaining -= chunk_size
        return padding_bytes

    def _describe_exact_target_changes(
        self,
        requested_profile: CompressionProfile,
        final_profile: CompressionProfile,
        media_info: VideoInfo,
        padding_bytes: int,
    ) -> str:
        """Summarize the destructive adjustments exact mode applied."""

        parts: List[str] = []
        if requested_profile.use_hw_accel and not final_profile.use_hw_accel:
            parts.append("software encode")
        if final_profile.video_codec != requested_profile.video_codec:
            parts.append(final_profile.video_codec.value)
        if final_profile.disable_audio and not requested_profile.disable_audio:
            parts.append("audio removed")
        elif final_profile.audio_codec != requested_profile.audio_codec:
            parts.append(final_profile.audio_codec.value)
        if not final_profile.disable_audio and final_profile.audio_bitrate != requested_profile.audio_bitrate:
            parts.append(f"audio {final_profile.audio_bitrate // 1000} kbps")

        source_limit = requested_profile.max_resolution or media_info.height or 0
        if final_profile.max_resolution and source_limit and final_profile.max_resolution < source_limit:
            parts.append(f"{final_profile.max_resolution}p")

        source_fps = requested_profile.frame_rate or int(round(media_info.fps or 0)) or 0
        if final_profile.frame_rate and source_fps and int(final_profile.frame_rate) < int(source_fps):
            parts.append(f"{int(final_profile.frame_rate)} fps")

        if padding_bytes > 0:
            if padding_bytes >= 1_000_000:
                parts.append(f"padded {padding_bytes / 1_000_000:.1f} MB")
            else:
                parts.append(f"padded {padding_bytes / 1024:.0f} KB")

        return ", ".join(parts)

    def _record_target_size_attempt(
        self,
        search_state: TargetSizeSearchState,
        *,
        attempted_bitrate: int,
        actual_size_bytes: int,
        target_plan: TargetSizePlan,
    ) -> None:
        """Update the search bounds using the latest encoded output."""

        if attempted_bitrate <= 0 or actual_size_bytes <= 0:
            return

        if actual_size_bytes <= target_plan.target_size_bytes:
            if search_state.lower_bitrate is None or attempted_bitrate > search_state.lower_bitrate:
                search_state.lower_bitrate = attempted_bitrate
                search_state.lower_size_bytes = actual_size_bytes
            return

        if search_state.upper_bitrate is None or attempted_bitrate < search_state.upper_bitrate:
            search_state.upper_bitrate = attempted_bitrate
            search_state.upper_size_bytes = actual_size_bytes

    def _choose_next_target_video_bitrate(
        self,
        *,
        attempted_bitrate: int,
        actual_size_bytes: int,
        target_plan: TargetSizePlan,
        search_state: TargetSizeSearchState,
    ) -> int:
        """Choose the next retry bitrate using proportional and bracketed search."""

        self._record_target_size_attempt(
            search_state,
            attempted_bitrate=attempted_bitrate,
            actual_size_bytes=actual_size_bytes,
            target_plan=target_plan,
        )

        lower_bitrate = search_state.lower_bitrate
        upper_bitrate = search_state.upper_bitrate
        lower_size = search_state.lower_size_bytes
        upper_size = search_state.upper_size_bytes

        if (
            lower_bitrate is not None
            and upper_bitrate is not None
            and lower_bitrate < upper_bitrate
        ):
            next_bitrate: Optional[int] = None

            if (
                lower_size is not None
                and upper_size is not None
                and lower_size < upper_size
            ):
                size_span = upper_size - lower_size
                bitrate_span = upper_bitrate - lower_bitrate
                projected = lower_bitrate + int(round(
                    (target_plan.target_size_bytes - lower_size) * bitrate_span / size_span
                ))
                next_bitrate = projected

            if (
                next_bitrate is None
                or next_bitrate <= lower_bitrate
                or next_bitrate >= upper_bitrate
            ):
                next_bitrate = lower_bitrate + ((upper_bitrate - lower_bitrate) // 2)

            if next_bitrate == attempted_bitrate:
                if actual_size_bytes > target_plan.target_size_bytes:
                    step = max(1, (attempted_bitrate - lower_bitrate) // 2)
                    next_bitrate = max(lower_bitrate + 1, attempted_bitrate - step)
                else:
                    step = max(1, (upper_bitrate - attempted_bitrate) // 2)
                    next_bitrate = min(upper_bitrate - 1, attempted_bitrate + step)

            return max(target_plan.min_video_bitrate, next_bitrate)

        return refine_target_size_bitrate(
            attempted_bitrate,
            actual_size_bytes,
            target_plan,
        )

    def _resolve_target_size_plan(
        self,
        profile: CompressionProfile,
        media_info: VideoInfo,
    ) -> Optional[TargetSizePlan]:
        """Resolve target-size settings into a reusable bitrate plan."""

        target_size_mb = resolve_requested_target_size_mb(
            profile.target_size_mb,
            profile.target_reduction_percent,
            media_info.size,
        )
        if not target_size_mb:
            return None

        plan_profile = self._prepare_target_size_profile(profile, media_info)

        audio_bitrate = 0 if plan_profile.disable_audio else (plan_profile.audio_bitrate or media_info.audio_bitrate or AudioCodec.AAC.default_bitrate)
        if not plan_profile.disable_audio and plan_profile.audio_codec == AudioCodec.FLAC and media_info.audio_bitrate > 0:
            audio_bitrate = media_info.audio_bitrate

        resolved_mode = self._resolve_effective_target_mode(plan_profile, media_info)

        return build_target_size_plan(
            target_size_mb=target_size_mb,
            duration_seconds=media_info.duration,
            audio_bitrate=audio_bitrate,
            container=plan_profile.video_container,
            codec=plan_profile.video_codec,
            mode=resolved_mode.value,
        )

    def _resolve_effective_target_mode(
        self,
        profile: CompressionProfile,
        media_info: Optional[VideoInfo] = None,
    ) -> TargetSizeMode:
        """Resolve auto target mode into the specific mode used for this encode."""

        target_size_mb = resolve_requested_target_size_mb(
            profile.target_size_mb,
            profile.target_reduction_percent,
            media_info.size if media_info else 0,
        )
        resolved_mode = resolve_target_size_mode(
            profile.target_size_mode,
            target_size_mb=target_size_mb,
            source_size_bytes=media_info.size if media_info else 0,
            duration_seconds=media_info.duration if media_info else 0.0,
            codec=profile.video_codec,
            use_hw_accel=profile.use_hw_accel,
            profile_type=profile.profile_type,
        )

        if (
            media_info is None
            or not target_size_mb
            or (profile.target_size_mode or TargetSizeMode.AUTO.value).lower() != TargetSizeMode.AUTO.value
            or resolved_mode == TargetSizeMode.EXACT
        ):
            return resolved_mode

        preview_audio_bitrate = 0 if profile.disable_audio else (
            profile.audio_bitrate or media_info.audio_bitrate or AudioCodec.AAC.default_bitrate
        )
        if not profile.disable_audio and profile.audio_codec == AudioCodec.FLAC and media_info.audio_bitrate > 0:
            preview_audio_bitrate = media_info.audio_bitrate

        try:
            preview_container = self.codec_manager.get_compatible_container(
                profile.video_codec,
                profile.video_container,
            ).value
        except Exception:
            preview_container = profile.video_container

        preview_plan = build_target_size_plan(
            target_size_mb=target_size_mb,
            duration_seconds=media_info.duration,
            audio_bitrate=preview_audio_bitrate,
            container=preview_container,
            codec=profile.video_codec,
            mode=resolved_mode.value,
        )
        if preview_plan.minimum_size_bytes > preview_plan.target_size_bytes:
            return TargetSizeMode.EXACT

        return resolved_mode

    def recommend_profile_adjustments(
        self,
        profile: CompressionProfile,
        media_info: Optional[VideoInfo],
    ) -> str:
        """Return a short, file-specific recommendation for the current selection."""

        if media_info is None:
            return ""

        if profile.output_mode == "audio":
            return "Recommendation: audio-only mode is best when you only need the soundtrack and want the smallest export."

        target_plan = self._resolve_target_size_plan(profile, media_info)
        if target_plan:
            if target_plan.target_size_bytes >= media_info.size:
                return "Recommendation: turn target size off for this file because the requested size is already larger than the source."

            if target_plan.minimum_size_bytes > target_plan.target_size_bytes:
                floor_mb = target_plan.minimum_size_bytes / 1_000_000
                if (profile.exact_audio_policy or "reduce").lower() != "drop" and media_info.audio_bitrate > 0:
                    return (
                        f"Recommendation: this clip likely bottoms out around {floor_mb:.1f} MB. "
                        "Switch Exact audio to drop or raise the target to avoid extreme quality loss."
                    )
                return (
                    f"Recommendation: the estimated floor is about {floor_mb:.1f} MB. "
                    "Raise the target if you want cleaner results."
                )

            if target_plan.mode == TargetSizeMode.FAST and media_info.duration >= 900:
                return "Recommendation: balanced or strict mode will land closer for long clips than fast mode."

            if target_plan.mode == TargetSizeMode.EXACT and not profile.exact_two_pass and media_info.duration >= 300:
                return "Recommendation: enable two-pass exact sizing for tighter exact-mode landings on longer clips."

            return f"Recommendation: {target_plan.mode.value.title()} target mode looks appropriate for this source."

        if media_info.height >= 2160 or media_info.duration >= 1_200:
            return "Recommendation: Balanced HEVC is the best starting point for long or high-resolution clips."

        if media_info.duration <= 180 and media_info.height <= 1080:
            return "Recommendation: Fast H.264 is enough if you care more about speed than the absolute smallest file."

        if media_info.video_codec in {"hevc", "av1", "vp9"} and media_info.size <= 200_000_000:
            return "Recommendation: keep Balanced unless you need a hard size target; this source is already fairly efficient."

        return "Recommendation: Balanced remains the safest default for this clip."

    def _skip_target_size_result(
        self,
        job: CompressionJob,
        media_info: VideoInfo,
        target_plan: TargetSizePlan,
        video_codec: str,
        audio_codec: str,
    ) -> CompressionResult:
        """Skip encoding when the requested target cannot make the file smaller."""

        source_size_mb = media_info.size / 1_000_000 if media_info.size > 0 else 0.0
        note = (
            f"Skipped because the requested target of {target_plan.target_size_mb} MB is not smaller "
            f"than the source ({source_size_mb:.1f} MB)."
        )

        return CompressionResult(
            success=True,
            input_file=job.input_file,
            output_file=None,
            original_size=media_info.size,
            compressed_size=media_info.size,
            duration_seconds=media_info.duration,
            encoding_time=0.0,
            video_codec=video_codec,
            audio_codec=audio_codec,
            average_bitrate=media_info.total_bitrate or media_info.video_bitrate,
            media_type="video",
            skipped=True,
            note=note,
            encoder_name=job.encoder_name,
            threads_used=job.threads_used,
            attempt_count=0,
        )

    def _select_hw_encoder(self, codec: VideoCodec) -> Optional[str]:
        """Pick the best available hardware encoder for a codec.

        A later hardware pass should walk vendors with
        quality_ladder.ordered_hw_vendors (NVENC, then QSV, then AMF)
        before software. This method still honors the detector's preferred
        vendor first.
        """
        info = self.hw_detector.info
        if not info.gpus:
            return None

        preferred_vendor = info.preferred_hw_encoder
        vendors: List[str] = []
        if preferred_vendor:
            vendors.append(preferred_vendor)

        for gpu in info.gpus:
            vendor_name = gpu.vendor.value
            if vendor_name not in vendors:
                vendors.append(vendor_name)

        for vendor in vendors:
            encoder = self.codec_manager.get_hw_encoder(codec, vendor)
            if not encoder:
                continue
            for gpu in info.gpus:
                if gpu.vendor.value == vendor and gpu.encoder_support.get(codec.value, False):
                    return encoder

        return None

    def _build_scale_filter(self, width: int, height: int, max_resolution: Optional[int], scale: float) -> Optional[str]:
        """Build a scale filter for video or image resizing."""
        if max_resolution and height > max_resolution:
            return f"scale=-2:{max_resolution}"

        if scale != 1.0:
            new_width = int(width * scale)
            new_height = int(height * scale)
            new_width = max(2, new_width - (new_width % 2))
            new_height = max(2, new_height - (new_height % 2))
            return f"scale={new_width}:{new_height}"

        return None

    def _optimize_video_profile(
        self,
        profile: CompressionProfile,
        media_info: Optional[VideoInfo] = None,
    ) -> CompressionProfile:
        """Tune the selected profile for available encoders and current hardware.

        Respects the user's explicit codec choice.  Only falls back to another
        codec when the chosen one is genuinely unavailable.
        """
        optimized = self._prepare_target_size_profile(profile, media_info)

        target_mode = self._resolve_effective_target_mode(optimized, media_info)

        if target_mode == TargetSizeMode.EXACT:
            optimized.use_hw_accel = False

        if optimized.video_codec == VideoCodec.SVT_AV1:
            optimized.use_hw_accel = False

        if optimized.video_codec == VideoCodec.AV1:
            hw_av1 = self._select_hw_encoder(VideoCodec.AV1) if optimized.use_hw_accel else None

            if target_mode not in {TargetSizeMode.STRICT, TargetSizeMode.EXACT}:
                if hw_av1:
                    optimized.use_hw_accel = True
                elif self.codec_manager.is_codec_available(VideoCodec.SVT_AV1):
                    optimized.video_codec = VideoCodec.SVT_AV1
                    optimized.use_hw_accel = False
                    if optimized.preset in {"slow", "slower", "veryslow"}:
                        optimized.preset = "medium"
            elif optimized.use_hw_accel and optimized.crf >= 32 and self.codec_manager.is_codec_available(VideoCodec.AV1):
                # Strict target mode can trade speed for better size efficiency when needed.
                optimized.use_hw_accel = False

        hw_vendor = self.hw_detector.info.preferred_hw_encoder if optimized.use_hw_accel else None
        fallback_hw_vendor = None if target_mode == TargetSizeMode.EXACT else hw_vendor

        if not self.codec_manager.is_codec_usable(optimized.video_codec, hw_vendor=fallback_hw_vendor):
            optimized.video_codec = self.codec_manager.get_best_codec(
                prefer_efficiency=target_mode == TargetSizeMode.EXACT or optimized.profile_type.value != "fast",
                hw_vendor=fallback_hw_vendor,
            )

        return optimized

    def _get_thread_count(
        self,
        profile: CompressionProfile,
        codec: VideoCodec,
        hw_encoder: Optional[str],
        parallel_jobs: int = 1,
        media_info: Optional[VideoInfo] = None,
    ) -> int:
        """Pick a practical thread count for the selected codec and hardware path."""
        max_threads: Optional[int] = None
        if not hw_encoder and codec == VideoCodec.HEVC:
            # libx265 rejects very high frame-thread counts on some systems.
            max_threads = 16

        if profile.threads:
            requested = max(1, profile.threads)
            return min(requested, max_threads) if max_threads is not None else requested

        try:
            cpu_threads = max(
                1,
                self.hw_detector.recommend_threads_for_job(
                    n_parallel=max(1, parallel_jobs),
                    governor=profile.resource_governor,
                    codec=codec.value,
                    hw_encoder=hw_encoder,
                    frame_height=getattr(media_info, "height", profile.max_resolution or 0) or 0,
                ),
            )
        except TypeError:
            cpu_threads = max(1, self.hw_detector.info.recommended_threads)
        physical_cores = max(1, min(self.hw_detector.info.cpu_cores, cpu_threads))

        if hw_encoder:
            return max(2 if cpu_threads >= 2 else 1, min(cpu_threads, max(2, physical_cores)))
        if codec == VideoCodec.SVT_AV1:
            requested = max(2, cpu_threads)
            return min(requested, max_threads) if max_threads is not None else requested
        if codec == VideoCodec.AV1:
            requested = max(2, min(cpu_threads, max(physical_cores, int(cpu_threads * 0.85))))
            return min(requested, max_threads) if max_threads is not None else requested
        if codec == VideoCodec.HEVC:
            requested = max(physical_cores, int(cpu_threads * 0.75))
            return min(requested, max_threads) if max_threads is not None else requested
        if codec == VideoCodec.H264:
            requested = max(1, int(cpu_threads * 0.9))
            return min(requested, max_threads) if max_threads is not None else requested
        if codec == VideoCodec.VP9:
            requested = max(1, min(cpu_threads, 12))
            return min(requested, max_threads) if max_threads is not None else requested
        return min(cpu_threads, max_threads) if max_threads is not None else cpu_threads

    def _select_target_rate_control(
        self,
        encoder_name: str,
        target_mode: Optional[TargetSizeMode] = None,
    ) -> Optional[RateControl]:
        """Pick a bitrate-driven rate-control mode for target-size encodes."""

        supported = ENCODER_REGISTRY.get(encoder_name, {}).get("rate_controls") or []
        if not supported:
            return None

        if target_mode in {TargetSizeMode.STRICT, TargetSizeMode.EXACT}:
            preferred = [RateControl.CBR, RateControl.ABR, RateControl.VBR]
        else:
            preferred = [RateControl.VBR, RateControl.ABR, RateControl.CBR]

        for rate_control in preferred:
            if rate_control in supported:
                return rate_control
        return None

    def _build_video_ffmpeg_command(
        self,
        job: CompressionJob,
        profile: CompressionProfile,
        target_plan: Optional[TargetSizePlan] = None,
        target_video_bitrate: Optional[int] = None,
    ) -> List[str]:
        """Build an FFmpeg command for video compression."""
        video_info = job.video_info

        cmd = self._build_input_command(job, profile)

        hw_encoder = None
        if profile.use_hw_accel:
            hw_encoder = self._select_hw_encoder(profile.video_codec)

        codec_settings = profile.to_codec_settings()
        codec_settings.threads = self._get_thread_count(
            profile,
            profile.video_codec,
            hw_encoder,
            parallel_jobs=job.parallel_jobs,
            media_info=video_info if isinstance(video_info, VideoInfo) else None,
        )
        job.encoder_name = hw_encoder or profile.video_codec.ffmpeg_encoder
        job.threads_used = codec_settings.threads or 0

        if not codec_settings.disable_audio:
            codec_settings.audio_codec = self.codec_manager.coerce_audio_codec(
                codec_settings.audio_codec,
                profile.video_container,
            )

        effective_target_plan = target_plan
        if effective_target_plan is None and isinstance(video_info, VideoInfo):
            effective_target_plan = self._resolve_target_size_plan(profile, video_info)

        if target_video_bitrate is not None:
            codec_settings.video_bitrate = target_video_bitrate
            codec_settings.crf = None
        elif effective_target_plan is not None:
            codec_settings.video_bitrate = effective_target_plan.video_bitrate
            codec_settings.crf = None

        if codec_settings.video_bitrate:
            target_rate_control = self._select_target_rate_control(
                hw_encoder or profile.video_codec.ffmpeg_encoder,
                effective_target_plan.mode if effective_target_plan is not None else None,
            )
            if target_rate_control is not None:
                codec_settings.rate_control = target_rate_control

        codec_args = codec_settings.to_ffmpeg_args(hw_encoder)
        cmd.extend(codec_args)

        filters: List[str] = []
        if isinstance(video_info, VideoInfo):
            scale_filter = self._build_scale_filter(
                video_info.width,
                video_info.height,
                profile.max_resolution,
                profile.resolution_scale,
            )
            if scale_filter:
                filters.append(scale_filter)

        if filters:
            cmd.extend(["-vf", ",".join(filters)])

        if profile.frame_rate:
            cmd.extend(["-r", str(profile.frame_rate)])

        threads = codec_settings.threads or self.hw_detector.info.recommended_threads
        if "-threads" not in codec_args:
            cmd.extend(["-threads", str(threads)])

        if job.output_file.suffix.lower() == ".mp4":
            cmd.extend(["-movflags", "+faststart"])

        extra_args = self._parse_extra_ffmpeg_args(profile.extra_ffmpeg_args)
        if extra_args:
            cmd.extend(extra_args)

        cmd.append(str(job.output_file))
        return cmd

    def _build_input_command(self, job: CompressionJob, profile: CompressionProfile) -> List[str]:
        """Build the shared FFmpeg input and optional trim arguments."""
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-hide_banner",
            "-i",
            str(job.input_file),
        ]

        if profile.trim_enabled:
            start = parse_timecode(profile.trim_start)
            end = parse_timecode(profile.trim_end)
            if start is not None and start > 0:
                cmd.extend(["-ss", f"{start:.3f}"])
            if end is not None:
                if start is not None and end > start:
                    cmd.extend(["-t", f"{(end - start):.3f}"])
                elif start is None and end > 0:
                    cmd.extend(["-t", f"{end:.3f}"])

        return cmd

    def _parse_extra_ffmpeg_args(self, raw_args: str) -> List[str]:
        """Parse optional expert FFmpeg flags from the UI."""
        text = (raw_args or "").strip()
        if not text:
            return []
        return shlex.split(text, posix=False)

    def _creationflags(self) -> int:
        return subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0

    def _supports_two_pass_exact(
        self,
        profile: CompressionProfile,
        target_plan: Optional[TargetSizePlan],
    ) -> bool:
        """Whether the current exact encode can use FFmpeg two-pass bitrate control."""

        if not target_plan or target_plan.mode != TargetSizeMode.EXACT:
            return False
        if not profile.exact_two_pass or profile.use_hw_accel or profile.output_mode != "video":
            return False
        return profile.video_codec in {VideoCodec.AV1, VideoCodec.HEVC, VideoCodec.H264, VideoCodec.VP9}

    def _build_two_pass_commands(
        self,
        job: CompressionJob,
        profile: CompressionProfile,
        *,
        target_plan: TargetSizePlan,
        target_video_bitrate: int,
        passlogfile: Path,
    ) -> Tuple[List[str], List[str]]:
        """Build pass-1 and pass-2 commands for an exact two-pass encode."""

        second_pass = self._build_video_ffmpeg_command(
            job,
            profile,
            target_plan=target_plan,
            target_video_bitrate=target_video_bitrate,
        )
        output_path = str(job.output_file)
        common_args = second_pass[:-1]
        first_pass_common: List[str] = []
        skip_next = False
        for index, value in enumerate(common_args):
            if skip_next:
                skip_next = False
                continue
            if value == "-movflags" and index + 1 < len(common_args):
                skip_next = True
                continue
            first_pass_common.append(value)
        passlog = str(passlogfile)
        null_device = "NUL" if platform.system() == "Windows" else "/dev/null"
        first_pass = [
            *first_pass_common,
            "-an",
            "-pass",
            "1",
            "-passlogfile",
            passlog,
            "-f",
            "null",
            null_device,
        ]
        second_pass = [
            *common_args,
            "-pass",
            "2",
            "-passlogfile",
            passlog,
            output_path,
        ]
        return first_pass, second_pass

    def _run_ffmpeg_process(
        self,
        cmd: List[str],
        *,
        job: CompressionJob,
        job_id: str,
        media_info: VideoInfo,
        start_time: float,
        progress_start: float = 0.0,
        progress_span: float = 99.0,
        track_output: bool = True,
    ) -> Tuple[int, List[str]]:
        """Run FFmpeg while updating job progress from its stderr stream."""

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=self._creationflags(),
        )
        self._processes[job_id] = process

        frame_pattern = re.compile(r"frame=\s*(\d+)")
        fps_pattern = re.compile(r"fps=\s*([\d.]+)")
        speed_pattern = re.compile(r"speed=\s*([\d.]+)x")
        time_pattern = re.compile(r"time=\s*([0-9:.]+)")
        stderr_lines: List[str] = []
        eta_calculator = ETACalculator(window_seconds=5.0, smoothing=0.25)
        encoded_seconds = 0.0
        progress_limit = progress_start + progress_span

        try:
            for line in process.stderr:
                stderr_lines.append(line.rstrip())
                if self._cancelled.get(job_id, False):
                    process.terminate()
                    break

                while self._paused.get(job_id, False):
                    time.sleep(0.1)

                frame_match = frame_pattern.search(line)
                if frame_match:
                    job.current_frame = int(frame_match.group(1))
                    if job.total_frames > 0:
                        raw_progress = (job.current_frame / job.total_frames) * progress_span
                        job.progress = min(progress_limit, progress_start + raw_progress)
                        snapshot = eta_calculator.update(job.current_frame, job.total_frames)
                        job.speed = snapshot.units_per_second
                        job.eta = snapshot.eta_seconds

                time_match = time_pattern.search(line)
                if time_match and media_info.duration > 0:
                    parsed_time = parse_timecode(time_match.group(1))
                    if parsed_time is not None:
                        encoded_seconds = min(media_info.duration, parsed_time)
                        raw_progress = (encoded_seconds / media_info.duration) * progress_span
                        job.progress = min(progress_limit, progress_start + raw_progress)

                fps_match = fps_pattern.search(line)
                if fps_match:
                    ffmpeg_speed = float(fps_match.group(1))
                    if ffmpeg_speed > job.speed:
                        job.speed = ffmpeg_speed

                speed_match = speed_pattern.search(line)
                if speed_match:
                    speed_mult = float(speed_match.group(1))
                    if speed_mult > 0 and media_info.duration > 0 and encoded_seconds > 0:
                        job.eta = max(0.0, (media_info.duration - encoded_seconds) / speed_mult)
                    elif speed_mult > 0 and job.progress > 0:
                        remaining_progress = 100 - job.progress
                        remaining_time = (remaining_progress / job.progress) * (time.time() - start_time)
                        if job.eta <= 0:
                            job.eta = remaining_time / speed_mult

                if track_output and job.output_file.exists():
                    elapsed = max(0.001, time.time() - start_time)
                    job.throughput_mb_s = job.output_file.stat().st_size / (1024 * 1024) / elapsed

                self._report_progress(job)

            process.wait()
            return process.returncode, stderr_lines
        finally:
            self._processes.pop(job_id, None)

    def _build_audio_ffmpeg_command(
        self,
        job: CompressionJob,
        profile: CompressionProfile,
    ) -> List[str]:
        """Build an FFmpeg command for audio extraction."""
        cmd = self._build_input_command(job, profile)
        cmd.extend(["-vn", "-sn", "-dn", "-c:a", profile.audio_codec.ffmpeg_encoder])

        if not profile.audio_codec.is_lossless and profile.audio_bitrate > 0:
            cmd.extend(["-b:a", str(profile.audio_bitrate)])

        extra_args = self._parse_extra_ffmpeg_args(profile.extra_ffmpeg_args)
        if extra_args:
            cmd.extend(extra_args)

        cmd.append(str(job.output_file))
        return cmd
    
    def _build_ffmpeg_command(
        self,
        job: CompressionJob
    ) -> List[str]:
        """
        Build FFmpeg command for a compression job.
        
        Args:
            job: Compression job
            
        Returns:
            List of command arguments
        """
        media_info = job.video_info if isinstance(job.video_info, VideoInfo) else None
        return self._build_video_ffmpeg_command(job, self._optimize_video_profile(job.profile, media_info))

    def _should_retry_with_safe_av1_settings(self, profile: CompressionProfile, error_text: str) -> bool:
        """Detect libaom-av1 option failures that can be retried with safer settings."""
        if profile.video_codec != VideoCodec.AV1 or profile.use_hw_accel:
            return False

        normalized = error_text.lower()
        retry_markers = [
            "libaom-av1",
            "cpu-used",
            "error applying encoder options",
            "error setting option",
            "result too large",
        ]
        return any(marker in normalized for marker in retry_markers)

    def _get_image_output_details(self, image_info: ImageInfo, profile: CompressionProfile) -> Tuple[str, Dict[str, Any]]:
        """Choose image output format and save options."""
        method_map = {
            "fast": 3,
            "balanced": 5,
            "max": 6,
            "custom": 5,
        }
        method = method_map.get(profile.profile_type.value, 5)
        quality = max(45, min(profile.image_quality, 100))
        preferred = (profile.image_format or "webp").lower()

        if preferred == "jxl":
            return "jxl", {"format": "JXL", "quality": quality}

        if preferred == "avif":
            return "avif", {"format": "AVIF", "quality": quality}

        if preferred in {"jpg", "jpeg"}:
            return "jpg", {
                "format": "JPEG",
                "quality": quality,
                "optimize": True,
                "progressive": True,
            }

        if preferred == "png":
            return "png", {
                "format": "PNG",
                "optimize": True,
                "compress_level": 9,
            }

        if image_info.has_alpha and profile.profile_type.value == "max":
            return "png", {
                "format": "PNG",
                "optimize": True,
                "compress_level": 9,
            }

        if features.check("webp"):
            save_kwargs: Dict[str, Any] = {
                "format": "WEBP",
                "method": method,
            }
            if image_info.has_alpha and profile.profile_type.value == "max":
                save_kwargs["lossless"] = True
            else:
                save_kwargs["quality"] = quality
            return get_image_extension(
                prefer_lossless=bool(save_kwargs.get("lossless")),
                has_alpha=image_info.has_alpha,
            ), save_kwargs

        if image_info.has_alpha:
            return "png", {
                "format": "PNG",
                "optimize": True,
                "compress_level": 9,
            }

        return "jpg", {
            "format": "JPEG",
            "quality": quality,
            "optimize": True,
            "progressive": True,
        }

    def _compress_avif_image(self, prepared_input: Path, output_file: Path, quality: int) -> None:
        """Encode a still image to AVIF using FFmpeg."""
        crf = max(18, min(45, 50 - round((quality / 100) * 28)))
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-hide_banner",
            "-i",
            str(prepared_input),
            "-frames:v",
            "1",
            "-c:v",
            "libaom-av1",
            "-still-picture",
            "1",
            "-crf",
            str(crf),
            "-b:v",
            "0",
            str(output_file),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "AVIF encoding failed")

    def _compress_jxl_image(self, prepared_input: Path, output_file: Path, quality: int) -> None:
        """Encode a still image to JPEG XL using cjxl when available."""
        if not self._cjxl_path:
            raise ValueError("JPEG XL output requires cjxl.exe to be installed or bundled")
        distance = max(0.0, min(15.0, (100 - quality) / 8))
        cmd = [self._cjxl_path, str(prepared_input), str(output_file), "-d", f"{distance:.2f}"]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
        )
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "JPEG XL encoding failed")

    def _compress_image(self, job: CompressionJob, profile: CompressionProfile, image_info: ImageInfo) -> CompressionResult:
        """Compress an image using Pillow with format-aware defaults."""
        if image_info.frame_count > 1:
            return self._compress_animated_image(job, profile, image_info)

        start_time = time.time()
        output_ext, save_kwargs = self._get_image_output_details(image_info, profile)
        job.output_file = job.output_file.with_suffix(f".{output_ext}")
        job.output_file.parent.mkdir(parents=True, exist_ok=True)

        with Image.open(job.input_file) as source:
            image = ImageOps.exif_transpose(source)

            scale_filter = self._build_scale_filter(
                image_info.width,
                image_info.height,
                profile.max_resolution,
                profile.resolution_scale,
            )
            if scale_filter:
                if profile.max_resolution and image_info.height > profile.max_resolution:
                    target_height = profile.max_resolution
                    target_width = max(1, round(image_info.width * (target_height / image_info.height)))
                else:
                    target_width = max(1, round(image_info.width * profile.resolution_scale))
                    target_height = max(1, round(image_info.height * profile.resolution_scale))
                image = image.resize((target_width, target_height), Image.Resampling.LANCZOS)

            if save_kwargs["format"] == "JPEG":
                image = image.convert("RGB")
            elif image.mode == "P" and image_info.has_alpha:
                image = image.convert("RGBA")

            if save_kwargs["format"] in {"AVIF", "JXL"}:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as temp_file:
                    temp_path = Path(temp_file.name)
                try:
                    prepared = image.convert("RGBA") if image_info.has_alpha else image.convert("RGB")
                    prepared.save(temp_path, format="PNG", optimize=True)
                    if save_kwargs["format"] == "AVIF":
                        self._compress_avif_image(temp_path, job.output_file, save_kwargs["quality"])
                    else:
                        self._compress_jxl_image(temp_path, job.output_file, save_kwargs["quality"])
                finally:
                    temp_path.unlink(missing_ok=True)
            else:
                image.save(job.output_file, **save_kwargs)

        compressed_size = job.output_file.stat().st_size
        encoding_time = time.time() - start_time

        if compressed_size >= image_info.size:
            return self._keep_original_result(
                job=job,
                media_info=image_info,
                encoding_time=encoding_time,
                video_codec="image",
                audio_codec="none",
                media_type="image",
            )

        job.progress = 100
        job.status = JobStatus.COMPLETED
        self._report_progress(job)

        return CompressionResult(
            success=True,
            input_file=job.input_file,
            output_file=job.output_file,
            original_size=image_info.size,
            compressed_size=compressed_size,
            duration_seconds=0.0,
            encoding_time=encoding_time,
            video_codec="image",
            audio_codec="none",
            average_bitrate=0,
            media_type="image",
            output_format=save_kwargs["format"].lower(),
        )

    def _compress_animated_image(self, job: CompressionJob, profile: CompressionProfile, image_info: ImageInfo) -> CompressionResult:
        """Compress animated GIF/WebP inputs while keeping animation frames."""
        start_time = time.time()
        output_ext = "webp"
        job.output_file = job.output_file.with_suffix(f".{output_ext}")
        job.output_file.parent.mkdir(parents=True, exist_ok=True)

        quality = max(45, min(profile.image_quality, 100))
        loop = 0
        durations: list[int] = []
        frames: list[Image.Image] = []

        with Image.open(job.input_file) as image:
            for frame in ImageSequence.Iterator(image):
                current = ImageOps.exif_transpose(frame.copy())
                if image_info.has_alpha:
                    current = current.convert("RGBA")
                else:
                    current = current.convert("RGB")

                scale_filter = self._build_scale_filter(
                    image_info.width,
                    image_info.height,
                    profile.max_resolution,
                    profile.resolution_scale,
                )
                if scale_filter:
                    if profile.max_resolution and image_info.height > profile.max_resolution:
                        target_height = profile.max_resolution
                        target_width = max(1, round(image_info.width * (target_height / image_info.height)))
                    else:
                        target_width = max(1, round(image_info.width * profile.resolution_scale))
                        target_height = max(1, round(image_info.height * profile.resolution_scale))
                    current = current.resize((target_width, target_height), Image.Resampling.LANCZOS)

                frames.append(current)
                durations.append(int(frame.info.get("duration", image.info.get("duration", 80)) or 80))
                loop = int(image.info.get("loop", 0))

        if not frames:
            raise ValueError("Animated image did not contain any frames")

        save_kwargs: Dict[str, Any] = {
            "format": "WEBP",
            "save_all": True,
            "append_images": frames[1:],
            "duration": durations,
            "loop": loop,
            "method": 6 if profile.profile_type.value == "max" else 4,
        }
        if image_info.has_alpha and profile.profile_type.value == "max":
            save_kwargs["lossless"] = True
        else:
            save_kwargs["quality"] = quality

        frames[0].save(job.output_file, **save_kwargs)

        compressed_size = job.output_file.stat().st_size
        encoding_time = time.time() - start_time

        if compressed_size >= image_info.size:
            return self._keep_original_result(
                job=job,
                media_info=image_info,
                encoding_time=encoding_time,
                video_codec="image",
                audio_codec="none",
                media_type="image",
            )

        job.progress = 100
        job.status = JobStatus.COMPLETED
        self._report_progress(job)

        return CompressionResult(
            success=True,
            input_file=job.input_file,
            output_file=job.output_file,
            original_size=image_info.size,
            compressed_size=compressed_size,
            duration_seconds=0.0,
            encoding_time=encoding_time,
            video_codec="image",
            audio_codec="none",
            average_bitrate=0,
            media_type="image",
            output_format="webp",
        )

    def _keep_original_result(
        self,
        job: CompressionJob,
        media_info: MediaInfo,
        encoding_time: float,
        video_codec: str,
        audio_codec: str,
        media_type: str,
    ) -> CompressionResult:
        """Preserve the source file when recompression would make it larger."""
        fallback_output = job.output_file.with_suffix(job.input_file.suffix)

        if job.output_file != fallback_output and job.output_file.exists():
            job.output_file.unlink(missing_ok=True)

        if fallback_output.resolve() != job.input_file.resolve():
            fallback_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(job.input_file, fallback_output)

        return CompressionResult(
            success=True,
            input_file=job.input_file,
            output_file=fallback_output,
            original_size=media_info.size,
            compressed_size=media_info.size,
            duration_seconds=getattr(media_info, "duration", 0.0),
            encoding_time=encoding_time,
            video_codec=video_codec,
            audio_codec=audio_codec,
            average_bitrate=int((media_info.size * 8) / media_info.duration) if getattr(media_info, "duration", 0) > 0 else 0,
            media_type=media_type,
            output_format=fallback_output.suffix.lstrip(".").lower(),
            kept_original=True,
            note="Original file was already smaller, so SquishIt kept a copy of it instead of enlarging it.",
            encoder_name=job.encoder_name,
            threads_used=job.threads_used,
            attempt_count=max(1, job.attempt_count),
        )

    def build_sample_profile(
        self,
        profile: CompressionProfile,
        media_info: VideoInfo,
        sample_seconds: float = 20.0,
        label: Optional[str] = None,
    ) -> CompressionProfile:
        """Create a trimmed sample profile for compare-mode runs."""

        sample = self._clone_profile(profile)
        sample.name = label or sample.name
        sample.target_size_mb = None
        sample.target_reduction_percent = None
        sample.target_size_mode = TargetSizeMode.FAST.value

        sample_duration = float(sample_seconds or 20.0)
        if media_info.duration > 0:
            sample_duration = max(1.0, min(sample_duration, media_info.duration))
        else:
            sample_duration = max(1.0, sample_duration)

        if media_info.duration > sample_duration:
            start = max(0.0, (media_info.duration - sample_duration) / 2)
        else:
            start = 0.0

        sample.trim_enabled = True
        sample.trim_start = round(start, 3)
        sample.trim_end = round(start + sample_duration, 3)
        return sample

    def compare_sample(
        self,
        input_file: Path,
        output_dir: Path,
        profiles: List[CompressionProfile],
        sample_seconds: float = 20.0,
    ) -> List[CompressionResult]:
        """Run a short sample encode for several profiles and return their results."""

        media_info = self.analyze_media(input_file)
        if not isinstance(media_info, VideoInfo):
            raise ValueError("Compare mode currently supports videos only")

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        results: List[CompressionResult] = []

        for index, profile in enumerate(profiles, start=1):
            sample_profile = self.build_sample_profile(
                profile=profile,
                media_info=media_info,
                sample_seconds=sample_seconds,
                label=profile.name,
            )
            ext = get_video_extension(sample_profile.video_codec.value, sample_profile.video_container)
            stem = sanitize_filename(f"{Path(input_file).stem}_compare_{index}_{sample_profile.name.lower()}")
            output_file = output_dir / f"{stem}.{ext}"
            job_id = f"cmp-{index}-{uuid.uuid4().hex[:6]}"
            result = self.compress(
                input_file=input_file,
                output_file=output_file,
                profile=sample_profile,
                job_id=job_id,
            )
            prefix = f"Sample compare: {sample_profile.name}."
            if result.note:
                result.note = f"{prefix} {result.note}"
            else:
                result.note = prefix
            results.append(result)
            if result.cancelled:
                break

        return results
    
    def compress(
        self,
        input_file: Path,
        output_file: Path,
        profile: CompressionProfile,
        job_id: Optional[str] = None,
        parallel_jobs: int = 1,
    ) -> CompressionResult:
        """
        Compress a video file.
        
        Args:
            input_file: Input video file path
            output_file: Output video file path
            profile: Compression profile to use
            job_id: Optional job ID for tracking
            
        Returns:
            CompressionResult object
        """
        import uuid
        job_id = job_id or str(uuid.uuid4())[:8]
        
        # Create job
        job = CompressionJob(
            id=job_id,
            input_file=Path(input_file),
            output_file=Path(output_file),
            profile=profile,
            parallel_jobs=max(1, int(parallel_jobs or 1)),
        )
        
        self._jobs[job_id] = job
        self._cancelled[job_id] = False
        
        start_time = time.time()
        
        try:
            # Analyze input
            job.status = JobStatus.ANALYZING
            self._report_progress(job)

            media_type = detect_media_type(job.input_file)
            if not media_type:
                raise ValueError(f"Unsupported media type: {input_file}")

            job.media_type = media_type
            media_info = self.analyze_media(job.input_file)
            if not media_info:
                raise ValueError(f"Failed to analyze media: {input_file}")

            job.video_info = media_info

            if self._cancelled.get(job_id, False):
                return self._cancel_result(job)

            if isinstance(media_info, ImageInfo):
                job.status = JobStatus.COMPRESSING
                job.progress = 5
                self._report_progress(job)
                result = self._compress_image(job, self._clone_profile(profile), media_info)
                job.result = result
                return result

            if profile.output_mode == "audio":
                output_ext = get_audio_extension(profile.audio_codec.value)
                job.output_file = job.output_file.with_suffix(f".{output_ext}")
                job.output_file.parent.mkdir(parents=True, exist_ok=True)

                cmd = self._build_audio_ffmpeg_command(job, profile)
                logger.info(f"FFmpeg audio extraction command: {' '.join(cmd)}")

                job.status = JobStatus.COMPRESSING
                self._report_progress(job)
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0
                )
                self._processes[job_id] = process
                stderr_text = process.communicate()[1]
                if self._cancelled.get(job_id, False):
                    return self._cancel_result(job)
                if process.returncode != 0:
                    raise ValueError(stderr_text.strip() or f"FFmpeg failed with code {process.returncode}")
                if not job.output_file.exists():
                    raise ValueError("Output file was not created")

                compressed_size = job.output_file.stat().st_size
                encoding_time = time.time() - start_time
                result = CompressionResult(
                    success=True,
                    input_file=job.input_file,
                    output_file=job.output_file,
                    original_size=media_info.size,
                    compressed_size=compressed_size,
                    duration_seconds=media_info.duration,
                    encoding_time=encoding_time,
                    video_codec="none",
                    audio_codec=profile.audio_codec.value,
                    average_bitrate=int((compressed_size * 8) / media_info.duration) if media_info.duration > 0 else 0,
                    media_type="audio",
                    output_format=job.output_file.suffix.lstrip(".").lower(),
                    encoder_name=profile.audio_codec.ffmpeg_encoder,
                )
                job.status = JobStatus.COMPLETED
                job.progress = 100
                job.result = result
                self._report_progress(job)
                return result

            job.total_frames = media_info.frame_count

            optimized_profile = self._optimize_video_profile(profile, media_info)
            job.profile = optimized_profile
            optimized_profile.video_container = self.codec_manager.get_compatible_container(
                optimized_profile.video_codec,
                optimized_profile.video_container,
            ).value

            desired_suffix = f".{get_video_extension(optimized_profile.video_codec.value, optimized_profile.video_container)}"
            if job.output_file.suffix.lower() != desired_suffix:
                job.output_file = job.output_file.with_suffix(desired_suffix)

            job.output_file.parent.mkdir(parents=True, exist_ok=True)

            target_plan = self._resolve_target_size_plan(optimized_profile, media_info)
            if target_plan and target_plan.target_size_bytes >= media_info.size:
                logger.info(
                    "Skipping %s because target %s MB is not smaller than source %.1f MB",
                    job.input_file,
                    target_plan.target_size_mb,
                    media_info.size / 1_000_000,
                )
                result = self._skip_target_size_result(
                    job=job,
                    media_info=media_info,
                    target_plan=target_plan,
                    video_codec=optimized_profile.video_codec.value,
                    audio_codec=self._effective_audio_codec_name(optimized_profile),
                )
                job.status = JobStatus.COMPLETED
                job.progress = 100
                job.result = result
                self._report_progress(job)
                return result

            if target_plan and target_plan.warning:
                logger.info("Target-size warning for %s: %s", job.input_file, target_plan.warning)

            retry_count = 0
            oversized_retry_done = False
            target_floor_limited = False
            stage_attempt = 0
            total_attempts = 0
            target_video_bitrate = target_plan.video_bitrate if target_plan else None
            target_search_state = TargetSizeSearchState() if target_plan else None
            while True:
                stage_attempt += 1
                total_attempts += 1
                job.attempt_count = total_attempts
                cmd = self._build_video_ffmpeg_command(
                    job,
                    optimized_profile,
                    target_plan=target_plan,
                    target_video_bitrate=target_video_bitrate,
                )
                logger.info(f"FFmpeg command: {' '.join(cmd)}")
                if target_plan:
                    logger.info(
                        "Target-size attempt %s/%s: %s",
                        job.attempt_count,
                        target_plan.max_attempts,
                        describe_target_size_plan(target_plan, media_info.size),
                    )

                job.status = JobStatus.COMPRESSING
                self._report_progress(job)

                stderr_lines: List[str] = []
                returncode = 0
                use_two_pass = self._supports_two_pass_exact(optimized_profile, target_plan)
                if use_two_pass and target_plan is not None and (target_video_bitrate or 0) > 0:
                    with tempfile.TemporaryDirectory(prefix="squishit-pass-") as pass_dir:
                        passlogfile = Path(pass_dir) / "exact-pass"
                        pass1_cmd, pass2_cmd = self._build_two_pass_commands(
                            job,
                            optimized_profile,
                            target_plan=target_plan,
                            target_video_bitrate=target_video_bitrate or target_plan.video_bitrate,
                            passlogfile=passlogfile,
                        )
                        logger.info("Exact two-pass pass 1: %s", " ".join(pass1_cmd))
                        returncode, stderr_lines = self._run_ffmpeg_process(
                            pass1_cmd,
                            job=job,
                            job_id=job_id,
                            media_info=media_info,
                            start_time=start_time,
                            progress_start=0.0,
                            progress_span=48.0,
                            track_output=False,
                        )
                        if returncode == 0 and not self._cancelled.get(job_id, False):
                            if job.output_file.exists():
                                job.output_file.unlink(missing_ok=True)
                            logger.info("Exact two-pass pass 2: %s", " ".join(pass2_cmd))
                            returncode, second_pass_stderr = self._run_ffmpeg_process(
                                pass2_cmd,
                                job=job,
                                job_id=job_id,
                                media_info=media_info,
                                start_time=start_time,
                                progress_start=48.0,
                                progress_span=51.0,
                                track_output=True,
                            )
                            stderr_lines.extend(second_pass_stderr)
                else:
                    returncode, stderr_lines = self._run_ffmpeg_process(
                        cmd,
                        job=job,
                        job_id=job_id,
                        media_info=media_info,
                        start_time=start_time,
                    )

                if self._cancelled.get(job_id, False):
                    return self._cancel_result(job)

                if returncode == 0:
                    if not job.output_file.exists():
                        raise ValueError("Output file was not created")

                    compressed_size = job.output_file.stat().st_size
                    if target_plan:
                        attempted_bitrate = target_video_bitrate or target_plan.video_bitrate
                        exact_under_target = (
                            target_plan.mode == TargetSizeMode.EXACT
                            and compressed_size <= target_plan.target_size_bytes
                        )
                        if exact_under_target and is_within_target_size_tolerance(compressed_size, target_plan):
                            break
                        if target_plan.mode != TargetSizeMode.EXACT and is_within_target_size_tolerance(compressed_size, target_plan):
                            break

                        if stage_attempt >= target_plan.max_attempts:
                            if exact_under_target:
                                logger.info(
                                    "Exact target search for %s stopped after %s attempts because the file is already under target",
                                    job.input_file,
                                    stage_attempt,
                                )
                                break
                            if target_plan.mode == TargetSizeMode.EXACT:
                                fallback_change = self._apply_exact_target_fallback(optimized_profile, media_info)
                                if fallback_change:
                                    logger.info(
                                        "Exact target fallback for %s after %s attempts: %s",
                                        job.input_file,
                                        stage_attempt,
                                        fallback_change,
                                    )
                                    target_plan = self._resolve_target_size_plan(optimized_profile, media_info)
                                    target_video_bitrate = target_plan.video_bitrate if target_plan else None
                                    target_search_state = TargetSizeSearchState() if target_plan else None
                                    stage_attempt = 0
                                    target_floor_limited = False
                                    job.progress = 0.0
                                    job.speed = 0.0
                                    job.eta = 0.0
                                    job.current_frame = 0
                                    if job.output_file.exists():
                                        job.output_file.unlink(missing_ok=True)
                                    self._report_progress(job)
                                    continue
                            logger.info(
                                "Target-size mode exhausted %s attempts for %s; keeping best result",
                                target_plan.max_attempts,
                                job.input_file,
                            )
                            break

                        next_bitrate = self._choose_next_target_video_bitrate(
                            attempted_bitrate=attempted_bitrate,
                            actual_size_bytes=compressed_size,
                            target_plan=target_plan,
                            search_state=target_search_state or TargetSizeSearchState(),
                        )
                        if next_bitrate == attempted_bitrate:
                            if exact_under_target:
                                logger.info(
                                    "Exact target search for %s stopped because bitrate refinement converged under target",
                                    job.input_file,
                                )
                                break
                            if target_plan.mode == TargetSizeMode.EXACT:
                                fallback_change = self._apply_exact_target_fallback(optimized_profile, media_info)
                                if fallback_change:
                                    logger.info(
                                        "Exact target fallback for %s after bitrate stall: %s",
                                        job.input_file,
                                        fallback_change,
                                    )
                                    target_plan = self._resolve_target_size_plan(optimized_profile, media_info)
                                    target_video_bitrate = target_plan.video_bitrate if target_plan else None
                                    target_search_state = TargetSizeSearchState() if target_plan else None
                                    stage_attempt = 0
                                    target_floor_limited = False
                                    job.progress = 0.0
                                    job.speed = 0.0
                                    job.eta = 0.0
                                    job.current_frame = 0
                                    if job.output_file.exists():
                                        job.output_file.unlink(missing_ok=True)
                                    self._report_progress(job)
                                    continue
                            target_floor_limited = bool(
                                target_plan.minimum_size_bytes > target_plan.target_size_bytes
                                and (target_video_bitrate or 0) <= target_plan.min_video_bitrate
                                and compressed_size > target_plan.target_size_bytes
                            )
                            if target_floor_limited:
                                logger.info(
                                    "Target-size retries stopped for %s because the encode hit the plan bitrate floor",
                                    job.input_file,
                                )
                            else:
                                logger.info("Target-size bitrate converged for %s; no further retry needed", job.input_file)
                            break

                        target_video_bitrate = next_bitrate
                        job.progress = 0.0
                        job.speed = 0.0
                        job.eta = 0.0
                        job.current_frame = 0
                        if job.output_file.exists():
                            job.output_file.unlink(missing_ok=True)
                        logger.info(
                            "Retrying target-size encode for %s at %s kbps after %.1f MB output",
                            job.input_file,
                            target_video_bitrate // 1000,
                            compressed_size / 1_000_000,
                        )
                        self._report_progress(job)
                        continue

                    if (
                        compressed_size >= media_info.size
                        and not oversized_retry_done
                        and optimized_profile.video_codec == VideoCodec.AV1
                        and optimized_profile.use_hw_accel
                        and self.codec_manager.is_codec_available(VideoCodec.AV1)
                        and not optimized_profile.target_size_mb
                        and not optimized_profile.target_reduction_percent
                    ):
                        oversized_retry_done = True
                        optimized_profile.use_hw_accel = False
                        optimized_profile.crf = min(46, (optimized_profile.crf or 28) + 4)
                        job.progress = 0.0
                        job.speed = 0.0
                        job.eta = 0.0
                        job.current_frame = 0
                        if job.output_file.exists():
                            job.output_file.unlink(missing_ok=True)
                        logger.warning("Retrying oversized hardware AV1 encode in software mode with stronger compression")
                        continue
                    break

                stderr_text = "\n".join(stderr_lines[-20:]).strip()
                if (
                    retry_count == 0
                    and stderr_text
                    and self._should_retry_with_safe_av1_settings(optimized_profile, stderr_text)
                ):
                    retry_count += 1
                    logger.warning("Retrying AV1 encode with safer preset after encoder option failure")
                    optimized_profile.preset = "veryfast"
                    continue

                if stderr_text:
                    raise ValueError(stderr_text)
                raise ValueError(f"FFmpeg failed with code {returncode}")
            
            # Get output file size
            if not job.output_file.exists():
                raise ValueError("Output file was not created")
            
            compressed_size = job.output_file.stat().st_size
            padding_bytes = 0
            if target_plan and target_plan.mode == TargetSizeMode.EXACT and compressed_size <= target_plan.target_size_bytes:
                padding_bytes = self._pad_output_to_target_size(job.output_file, target_plan.target_size_bytes)
                compressed_size = job.output_file.stat().st_size
            encoding_time = time.time() - start_time
            result_note = ""

            if target_plan:
                actual_size_mb = compressed_size / 1_000_000
                if target_plan.mode == TargetSizeMode.EXACT and compressed_size == target_plan.target_size_bytes:
                    exact_changes = self._describe_exact_target_changes(profile, optimized_profile, media_info, padding_bytes)
                    result_note = (
                        f"Exact target hit {actual_size_mb:.1f} MB after {job.attempt_count} attempt(s)."
                    )
                    if exact_changes:
                        result_note += f" Adjustments: {exact_changes}."
                elif is_within_target_size_tolerance(compressed_size, target_plan):
                    result_note = (
                        f"Target mode {target_plan.mode.value} hit {actual_size_mb:.1f} MB "
                        f"after {job.attempt_count} attempt(s)."
                    )
                elif target_floor_limited:
                    floor_mb = target_plan.minimum_size_bytes / 1_000_000
                    result_note = (
                        f"Target mode {target_plan.mode.value} best-effort finished at {actual_size_mb:.1f} MB "
                        f"after {job.attempt_count} attempt(s); requested {target_plan.target_size_mb} MB is below "
                        f"the estimated floor of {floor_mb:.1f} MB for this clip."
                    )
                elif target_plan.mode == TargetSizeMode.EXACT:
                    exact_changes = self._describe_exact_target_changes(profile, optimized_profile, media_info, padding_bytes)
                    result_note = (
                        f"Exact target could not reach {target_plan.target_size_mb} MB after {job.attempt_count} attempt(s); "
                        f"it finished at {actual_size_mb:.1f} MB."
                    )
                    if exact_changes:
                        result_note += f" Last adjustments: {exact_changes}."
                else:
                    result_note = (
                        f"Target mode {target_plan.mode.value} finished at {actual_size_mb:.1f} MB "
                        f"after {job.attempt_count} attempt(s); target was {target_plan.target_size_mb} MB."
                    )

            if compressed_size >= media_info.size:
                logger.info("Encoded output for %s was larger than the source; preserving original", job.input_file)
                result = self._keep_original_result(
                    job=job,
                    media_info=media_info,
                    encoding_time=encoding_time,
                    video_codec=optimized_profile.video_codec.value,
                    audio_codec=media_info.audio_codec or self._effective_audio_codec_name(optimized_profile),
                    media_type="video",
                )
                job.status = JobStatus.COMPLETED
                job.progress = 100
                job.result = result
                self._report_progress(job)
                return result
            
            # Create result
            result = CompressionResult(
                success=True,
                input_file=job.input_file,
                output_file=job.output_file,
                original_size=media_info.size,
                compressed_size=compressed_size,
                duration_seconds=media_info.duration,
                encoding_time=encoding_time,
                video_codec=optimized_profile.video_codec.value,
                audio_codec=self._effective_audio_codec_name(optimized_profile),
                average_bitrate=int((compressed_size * 8) / media_info.duration) if media_info.duration > 0 else 0,
                media_type="video",
                output_format=job.output_file.suffix.lstrip(".").lower(),
                note=result_note,
                encoder_name=job.encoder_name,
                threads_used=job.threads_used,
                attempt_count=max(1, job.attempt_count),
            )
            
            job.status = JobStatus.COMPLETED
            job.progress = 100
            job.result = result
            self._report_progress(job)
            
            return result
            
        except Exception as e:
            if self._cancelled.get(job_id, False):
                return self._cancel_result(job)
            logger.error(f"Compression failed: {e}")
            job.status = JobStatus.FAILED
            job.error = str(e)
            self._report_progress(job)
            
            return CompressionResult(
                success=False,
                input_file=job.input_file,
                error_message=str(e)
            )
        
        finally:
            # Cleanup
            self._processes.pop(job_id, None)
            self._paused.pop(job_id, None)
            self._cancelled.pop(job_id, None)
    
    def compress_async(
        self,
        input_file: Path,
        output_file: Path,
        profile: CompressionProfile,
        job_id: Optional[str] = None,
        callback: Optional[Callable] = None,
        parallel_jobs: int = 1,
    ) -> str:
        """
        Compress a video file asynchronously.
        
        Args:
            input_file: Input video file path
            output_file: Output video file path
            profile: Compression profile
            job_id: Optional job ID
            callback: Callback function(result: CompressionResult)
            
        Returns:
            Job ID
        """
        import uuid
        job_id = job_id or str(uuid.uuid4())[:8]
        
        def worker():
            result = self.compress(
                input_file,
                output_file,
                profile,
                job_id,
                parallel_jobs=parallel_jobs,
            )
            if callback:
                callback(result)
        
        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        
        return job_id
    
    def pause_job(self, job_id: str):
        """Pause a compression job."""
        self._paused[job_id] = True
        if job_id in self._jobs:
            self._jobs[job_id].status = JobStatus.PAUSED
    
    def resume_job(self, job_id: str):
        """Resume a paused job."""
        self._paused[job_id] = False
        if job_id in self._jobs:
            self._jobs[job_id].status = JobStatus.COMPRESSING
    
    def cancel_job(self, job_id: str):
        """Cancel a compression job."""
        self._cancelled[job_id] = True
        
        # Also terminate the process
        process = self._processes.get(job_id)
        if process:
            try:
                process.terminate()
                process.wait(timeout=0.4)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        
        if job_id in self._jobs:
            self._jobs[job_id].status = JobStatus.CANCELLED
            self._report_progress(self._jobs[job_id])
    
    def get_job(self, job_id: str) -> Optional[CompressionJob]:
        """Get a job by ID."""
        return self._jobs.get(job_id)
    
    def get_all_jobs(self) -> List[CompressionJob]:
        """Get all jobs."""
        return list(self._jobs.values())
    
    def get_active_jobs(self) -> List[CompressionJob]:
        """Get all active (non-completed) jobs."""
        return [
            job for job in self._jobs.values()
            if job.status not in [JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED]
        ]


class BatchProcessor:
    """Handles batch compression of multiple files."""
    
    def __init__(self, compressor: VideoCompressor):
        self.compressor = compressor
        self._queue: queue.Queue = queue.Queue()
        self._results: Dict[str, CompressionResult] = {}
        self._running = False
        self._worker_thread: Optional[threading.Thread] = None
    
    def add_job(
        self,
        input_file: Path,
        output_file: Path,
        profile: CompressionProfile,
        job_id: Optional[str] = None
    ) -> str:
        """Add a job to the batch queue."""
        import uuid
        job_id = job_id or str(uuid.uuid4())[:8]
        
        self._queue.put((job_id, input_file, output_file, profile))
        return job_id
    
    def start(self, max_concurrent: int = 1):
        """Start processing the batch queue."""
        if self._running:
            return
        
        self._running = True
        
        def worker():
            while self._running:
                try:
                    job_id, input_file, output_file, profile = self._queue.get(timeout=1)
                    result = self.compressor.compress(
                        input_file, output_file, profile, job_id
                    )
                    self._results[job_id] = result
                    self._queue.task_done()
                except queue.Empty:
                    continue
                except Exception as e:
                    logger.error(f"Batch processing error: {e}")
        
        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()
    
    def stop(self):
        """Stop batch processing."""
        self._running = False
        if self._worker_thread:
            self._worker_thread.join(timeout=2)
    
    def get_result(self, job_id: str) -> Optional[CompressionResult]:
        """Get result of a completed job."""
        return self._results.get(job_id)
    
    def get_all_results(self) -> Dict[str, CompressionResult]:
        """Get all completed results."""
        return dict(self._results)
    
    @property
    def pending_count(self) -> int:
        """Number of pending jobs in queue."""
        return self._queue.qsize()
    
    @property
    def completed_count(self) -> int:
        """Number of completed jobs."""
        return len(self._results)