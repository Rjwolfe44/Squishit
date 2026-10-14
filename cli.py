#!/usr/bin/env python3
"""Command-line interface for SquishIt."""

import argparse
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, List, NamedTuple, Optional, Sequence
import logging

from video_compressor.core.clips import (
    MAX_JOIN_CLIPS,
    MIN_CLIP_SECONDS,
    Clip,
    ClipJob,
    ClipMode,
    ClipPlan,
    effective_media_info,
    expand_clip_jobs,
    normalize_clips,
    parse_clip_range,
    resolve_clip_plan,
)
from video_compressor.core.compressor import VideoCompressor, CompressionResult
from video_compressor.core.profiles import (
    ProfileManager,
    CompressionProfile,
    apply_cli_quality_ladder,
)
from video_compressor.core.hardware import get_hardware_detector
from video_compressor.core.codecs import VideoCodec, AudioCodec, ImageFormat, CodecManager
from video_compressor.core.utils import (
    detect_media_type,
    format_size,
    format_time,
    format_timecode,
    get_image_extension,
    get_video_extension,
    SUPPORTED_MEDIA_EXTENSIONS,
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def create_parser() -> argparse.ArgumentParser:
    """Create the argument parser."""
    parser = argparse.ArgumentParser(
        prog='squishit',
        description='SquishIt - power-user media compression for video and images.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Compress a single video with default settings
  %(prog)s video.mp4
  
  # Compress with specific profile
  %(prog)s video.mp4 --profile fast
  
  # Compress with target size
  %(prog)s video.mp4 --target-size 100
  
  # Batch compress all videos in a folder
  %(prog)s *.mp4 --output ./compressed/
  
  # Compress to stronger HEVC output
  %(prog)s video.mp4 --codec hevc --preset slow

  # Max / Archival: SVT-AV1, hardware off
  %(prog)s video.mp4 --profile max

  # Quick Compress Max: HEVC, not archival SVT-AV1
  %(prog)s video.mp4 --profile max --codec hevc

  # Keep two parts of a video, each saved as its own file (..._clip01.mp4, ..._clip02.mp4)
  %(prog)s video.mp4 --clip 1:30-2:45 --clip 5:00-5:30

  # Keep the first 30 seconds and everything from 10:00 on, joined into one file
  %(prog)s video.mp4 --clip -30 --clip 10:00- --join
        """
    )
    
    # Input files
    parser.add_argument(
        'inputs',
        nargs='*',
        help='Input media file(s), folder(s), or glob pattern'
    )
    
    # Output options
    parser.add_argument(
        '-o', '--output',
        type=str,
        default=None,
        help='Output directory (default: same as input, with _compressed suffix)'
    )
    
    parser.add_argument(
        '--suffix',
        type=str,
        default='_compressed',
        help='Suffix for output files (default: _compressed)'
    )
    
    # Profile selection
    parser.add_argument(
        '-p', '--profile',
        type=str,
        choices=['fast', 'balanced', 'max', 'youtube', 'mobile', 'streaming'],
        default='balanced',
        help=(
            'Compression profile (default: balanced). '
            'max is Max / Archival (SVT-AV1, hardware off). '
            'Quick Compress Max is HEVC: --profile max --codec hevc. '
            'Those are two different Max settings.'
        ),
    )
    
    # Codec options
    parser.add_argument(
        '-c', '--codec',
        type=str,
        choices=['hevc', 'h264', 'vp9', 'svt-av1', 'av1'],
        default=None,
        help=(
            'Video codec (default: depends on profile). '
            'svt-av1 is Max / Archival SVT-AV1 (best compression, CPU). '
            'av1 is fast hardware AV1 (NVENC, then QSV, then AMF, else libaom).'
        ),
    )

    parser.add_argument(
        '--container',
        type=str,
        choices=['mp4', 'mkv', 'webm', 'mov', 'avi'],
        default=None,
        help='Preferred output video container'
    )

    parser.add_argument(
        '--image-format',
        type=str,
        choices=['webp', 'avif', 'jpg', 'png', 'jxl'],
        default=None,
        help='Preferred output image format'
    )
    
    parser.add_argument(
        '--audio-codec',
        type=str,
        choices=['aac', 'opus', 'mp3', 'flac'],
        default=None,
        help='Audio codec (default: depends on profile)'
    )
    
    # Quality options
    parser.add_argument(
        '--crf',
        type=int,
        default=None,
        help='Constant Rate Factor (0-51, lower is better quality)'
    )
    
    parser.add_argument(
        '--preset',
        type=str,
        choices=['ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 
                 'medium', 'slow', 'slower', 'veryslow'],
        default=None,
        help='Encoding preset (default: depends on profile)'
    )
    
    # Target size mode
    parser.add_argument(
        '-t', '--target-size',
        type=int,
        default=None,
        help='Target file size in MB'
    )
    
    parser.add_argument(
        '--target-percent',
        type=int,
        default=None,
        help='Target size as percentage of original (e.g., 50 for 50%% smaller)'
    )

    parser.add_argument(
        '--target-mode',
        type=str,
        choices=['auto', 'fast', 'balanced', 'strict', 'exact'],
        default=None,
        help=(
            'How target-size mode should chase the requested MB. '
            'Exact tries hardware first, then may lower audio, frame rate, and resolution. '
            'Padding a short hardware file is ask-gated and is not a silent exact hit. '
            'A software retry requires --allow-software-fallback'
        )
    )

    parser.add_argument(
        '--exact-audio-policy',
        type=str,
        choices=['keep', 'reduce', 'drop'],
        default=None,
        help='When --target-mode exact is used, keep audio intact, reduce it, or allow it to be dropped'
    )

    parser.add_argument(
        '--exact-two-pass',
        action='store_true',
        help='Use a slower two-pass bitrate analysis path when --target-mode exact is enabled'
    )
    
    # Resolution and frame rate
    parser.add_argument(
        '-r', '--resolution',
        type=str,
        default=None,
        choices=['4k', '1440p', '1080p', '720p', '480p'],
        help='Maximum output resolution'
    )
    
    parser.add_argument(
        '-f', '--fps',
        type=int,
        default=None,
        help='Target frame rate'
    )
    
    # Clips (trim)
    parser.add_argument(
        '--clip',
        action='append',
        default=None,
        metavar='START-END',
        help=(
            'Keep only this part of each video; repeat for several parts. '
            'Times are seconds or [h:]mm:ss[.mmm]. Leave one end open: '
            '-30 is the first 30 seconds, 1:00- is from 1:00 to the end. '
            'Several clips are saved as separate files '
            '(name_compressed_clip01.mp4, ...) unless --join is used. '
            'Ignored for images'
        )
    )

    parser.add_argument(
        '--join',
        action='store_true',
        help=(
            'Join all --clip parts, in the order given, into one output file '
            f'instead of one file per clip (needs 2 to {MAX_JOIN_CLIPS} --clip options)'
        )
    )

    # Hardware acceleration
    parser.add_argument(
        '--no-hw-accel',
        action='store_true',
        help='Disable hardware acceleration'
    )

    parser.add_argument(
        '--allow-software-fallback',
        action='store_true',
        help=(
            'Target-size mode: retry on the software encoder when hardware misses '
            'the size band, fails, or finishes under an exact target that would '
            'be padded. Without this flag the hardware result is kept (unpadded, '
            'when it was short) and the run reports that confirmation is required'
        )
    )
    
    # Processing options
    parser.add_argument(
        '-j', '--jobs',
        type=int,
        default=1,
        help='Number of parallel encoding jobs (default: 1)'
    )

    parser.add_argument(
        '--batch-order',
        type=str,
        choices=['manual', 'largest-first', 'smallest-first', 'longest-first', 'shortest-first'],
        default='manual',
        help='Order queued inputs before processing'
    )

    parser.add_argument(
        '--threads',
        type=int,
        default=None,
        help='Override encoder thread count'
    )

    parser.add_argument(
        '--resource-governor',
        type=str,
        choices=['auto', 'low', 'balanced', 'max'],
        default=None,
        help='Overall CPU usage preference when threads are auto-managed'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without actually compressing'
    )

    parser.add_argument(
        '--compare',
        action='store_true',
        help='Run short sample encodes for a few codec paths instead of a full encode'
    )

    parser.add_argument(
        '--compare-seconds',
        type=int,
        default=20,
        help='Length of each sample encode when --compare is used'
    )
    
    # Verbosity
    parser.add_argument(
        '-v', '--verbose',
        action='count',
        default=0,
        help='Increase verbosity (-v, -vv, -vvv)'
    )
    
    parser.add_argument(
        '-q', '--quiet',
        action='store_true',
        help='Suppress all output except errors'
    )
    
    # Info commands
    parser.add_argument(
        '--info',
        action='store_true',
        help='Show info about input file(s) without compressing'
    )
    
    parser.add_argument(
        '--hardware',
        action='store_true',
        help='Show detected hardware information'
    )
    
    parser.add_argument(
        '--list-codecs',
        action='store_true',
        help='List available codecs'
    )
    
    return parser


PROFILE_NAMES = {
    'fast': 'Fast',
    'balanced': 'Balanced',
    'max': 'Max / Archival',
    'youtube': 'YouTube Upload',
    'mobile': 'Mobile',
    'streaming': 'Streaming',
}


def get_profile(
    args: argparse.Namespace,
    manager: Optional[ProfileManager] = None,
) -> CompressionProfile:
    """Get compression profile from arguments."""
    manager = manager or ProfileManager()

    profile_name = PROFILE_NAMES.get(args.profile)
    if profile_name is None:
        known = ", ".join(sorted(PROFILE_NAMES))
        raise ValueError(f"Unknown profile {args.profile!r}. Choose one of: {known}")

    base_profile = manager.get_profile(profile_name)
    if not base_profile:
        raise ValueError(f"Profile {profile_name!r} is not available")

    profile = CompressionProfile.from_dict(base_profile.to_dict())

    codec_overridden = args.codec is not None
    crf_overridden = args.crf is not None
    preset_overridden = bool(args.preset)
    hw_overridden = bool(args.no_hw_accel)

    # Apply overrides
    if args.codec:
        profile.video_codec = VideoCodec(args.codec)
    if args.audio_codec:
        profile.audio_codec = AudioCodec(args.audio_codec)
    if args.crf is not None:
        profile.crf = args.crf
    if args.preset:
        profile.preset = args.preset
    if args.target_size:
        profile.target_size_mb = args.target_size
    if args.target_percent:
        profile.target_reduction_percent = args.target_percent
    if args.target_mode:
        profile.target_size_mode = args.target_mode
    if args.exact_audio_policy:
        profile.exact_audio_policy = args.exact_audio_policy
    if args.exact_two_pass:
        profile.exact_two_pass = True
    if args.resolution:
        res_map = {'4k': 2160, '1440p': 1440, '1080p': 1080, '720p': 720, '480p': 480}
        profile.max_resolution = res_map[args.resolution]
    if args.fps:
        profile.frame_rate = args.fps
    if args.container:
        profile.video_container = args.container
    if args.image_format:
        profile.image_format = args.image_format
    if args.threads is not None:
        profile.threads = args.threads
    if args.resource_governor:
        profile.resource_governor = args.resource_governor
    if args.no_hw_accel:
        profile.use_hw_accel = False

    apply_cli_quality_ladder(
        profile,
        args.profile,
        codec_overridden=codec_overridden,
        crf_overridden=crf_overridden,
        preset_overridden=preset_overridden,
        hw_overridden=hw_overridden,
    )

    # Keep the CLI profile on a container/audio pair the encoder can actually mux.
    codec_manager = CodecManager()
    profile.video_container = codec_manager.get_compatible_container(
        profile.video_codec,
        profile.video_container,
    ).value
    if not profile.disable_audio:
        profile.audio_codec = codec_manager.coerce_audio_codec(
            profile.audio_codec,
            profile.video_container,
        )

    return profile


def show_info(filepath: Path, compressor: VideoCompressor):
    """Show information about a media file."""
    info = compressor.analyze_media(filepath)
    
    if not info:
        print(f"Error: Could not analyze {filepath}")
        return
    
    media_type = detect_media_type(filepath) or "media"

    print(f"\n{'='*50}")
    print(f"File: {filepath.name}")
    print(f"{'='*50}")
    print(f"  Type:         {media_type}")
    print(f"  Size:         {format_size(info.size)}")
    print(f"  Resolution:   {info.resolution} ({info.resolution_label})")
    if media_type == "video":
        print(f"  Duration:     {format_time(info.duration)}")
        print(f"  Frame Rate:   {info.fps:.2f} fps")
        print(f"  Video Codec:  {info.video_codec}")
        print(f"  Audio Codec:  {info.audio_codec}")
        print(f"  Video Bitrate: {info.video_bitrate // 1000} kbps")
        print(f"  Audio Bitrate: {info.audio_bitrate // 1000} kbps")
    else:
        print(f"  Format:       {info.image_format.upper()}")
        print(f"  Mode:         {info.mode}")
        print(f"  Alpha:        {'yes' if info.has_alpha else 'no'}")


def show_hardware():
    """Show hardware information."""
    detector = get_hardware_detector()
    info = detector.info
    
    print("\n" + "="*50)
    print("System Hardware Information")
    print("="*50)
    print(f"  OS:           {info.os_name}")
    print(f"  CPU:          {info.cpu_name}")
    print(f"  CPU Threads:  {info.cpu_threads}")
    print(f"  RAM:          {info.total_ram_gb:.1f} GB")
    
    if info.gpus:
        print(f"\n  GPUs:")
        for gpu in info.gpus:
            print(f"    - {gpu.name}")
    
    print(f"\n  Hardware Encoding: {info.encoding_label()}")
    
    print(f"\n  Recommended Threads: {info.recommended_threads}")


def list_codecs(compressor: VideoCompressor):
    """List available codecs."""
    print("\nAvailable Video Codecs:")
    print("-" * 30)
    for codec in compressor.codec_manager.get_supported_codecs():
        hw = " (HW accelerated)" if compressor.hw_detector.info.has_hw_encoder else ""
        print(f"  • {codec.value}: {codec.display_name}{hw}")

    print("\nVideo Containers:")
    print("-" * 30)
    for container in compressor.codec_manager.get_supported_containers():
        print(f"  • {container.value}")
    
    print("\nAvailable Audio Codecs:")
    print("-" * 30)
    for codec in compressor.codec_manager.get_supported_audio_codecs():
        print(f"  • {codec.value}: {codec.display_name}")

    print("\nImage Formats:")
    print("-" * 30)
    for image_format in ImageFormat:
        print(f"  • {image_format.value}")


def sort_input_files(
    input_files: List[Path],
    batch_order: str,
    compressor: VideoCompressor,
) -> List[Path]:
    """Sort inputs according to the requested batch strategy."""

    if batch_order == 'manual':
        return input_files

    def _size_for(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def _duration_for(path: Path) -> float:
        info = compressor.analyze_media(path)
        return float(getattr(info, 'duration', 0.0) or 0.0) if info else 0.0

    reverse = batch_order in {'largest-first', 'longest-first'}
    if batch_order in {'largest-first', 'smallest-first'}:
        return sorted(input_files, key=_size_for, reverse=reverse)
    if batch_order in {'longest-first', 'shortest-first'}:
        return sorted(input_files, key=_duration_for, reverse=reverse)
    return input_files


def build_compare_profiles(base_profile: CompressionProfile, compressor: VideoCompressor) -> List[CompressionProfile]:
    """Create a small comparison matrix around the current CLI profile."""

    codec_manager = compressor.codec_manager
    candidates: List[CompressionProfile] = []
    seen: set[tuple[str, str, bool]] = set()

    def _candidate(name: str, codec: VideoCodec, preset: str, use_hw: bool) -> CompressionProfile:
        profile = CompressionProfile.from_dict(base_profile.to_dict())
        profile.name = name
        profile.video_codec = codec
        profile.preset = preset
        profile.use_hw_accel = use_hw
        profile.target_size_mb = None
        profile.target_reduction_percent = None
        profile.video_container = codec_manager.get_compatible_container(codec, base_profile.video_container).value
        return profile

    candidate_specs = [
        (f"Current · {base_profile.video_codec.value.upper()}", base_profile.video_codec, base_profile.preset, base_profile.use_hw_accel),
        ('HEVC Balanced', VideoCodec.HEVC, 'medium', True),
        ('HEVC Smaller', VideoCodec.HEVC, 'slow', True),
        ('H264 Fast', VideoCodec.H264, 'fast', True),
    ]

    for name, codec, preset, use_hw in candidate_specs:
        key = (codec.value, preset, bool(use_hw))
        if key in seen:
            continue
        seen.add(key)
        candidates.append(_candidate(name, codec, preset, use_hw))
        if len(candidates) == 3:
            break

    return candidates


def get_clip_plan(args: argparse.Namespace) -> Optional[ClipPlan]:
    """Build the clip plan requested with --clip / --join, or None without --clip.

    Raises ValueError with a one-line message for bad input.
    """
    specs = list(getattr(args, 'clip', None) or [])
    join = bool(getattr(args, 'join', False))
    if not specs:
        if join:
            raise ValueError("--join needs at least two --clip options")
        return None

    clips: List[Clip] = []
    for spec in specs:
        try:
            clips.append(parse_clip_range(spec))
        except ValueError as exc:
            raise ValueError(f"Invalid --clip value: {exc}") from None

    if join:
        if len(clips) < 2:
            raise ValueError(
                "--join needs at least two --clip options (use a single --clip to just trim)"
            )
        if len(clips) > MAX_JOIN_CLIPS:
            raise ValueError(f"--join can join at most {MAX_JOIN_CLIPS} clips (got {len(clips)})")

    return ClipPlan(tuple(clips), ClipMode.JOIN if join else ClipMode.SEPARATE)


_CLIP_VALUE_START = re.compile(r'-[\d.]')


def attach_clip_values(argv: Sequence[str]) -> List[str]:
    """Rewrite ``--clip -1:30`` as ``--clip=-1:30``.

    argparse reads a value like ``-1:30`` as a new option (``-30`` already works),
    and an open start such as "the first 1:30" is a natural thing to type.
    """
    items = list(argv)
    fixed: List[str] = []
    i = 0
    while i < len(items):
        item = items[i]
        if item == '--':
            fixed.extend(items[i:])
            break
        if item == '--clip' and i + 1 < len(items) and _CLIP_VALUE_START.match(items[i + 1]):
            fixed.append(f'{item}={items[i + 1]}')
            i += 2
            continue
        fixed.append(item)
        i += 1
    return fixed


def _timecode(seconds: float) -> str:
    return "end" if math.isinf(seconds) else format_timecode(seconds)


def _length_text(seconds: float) -> str:
    return "to the end" if math.isinf(seconds) else format_timecode(seconds)


def _clip_range_text(clip: Clip) -> str:
    return f"{_timecode(clip.start)} - {_timecode(clip.end)}"


def clip_warnings(requested: Sequence[Clip], duration: float) -> List[str]:
    """Say which requested clips fall outside the video (dropped) or run past its end (cut)."""
    if duration <= 0:
        return []
    notes: List[str] = []
    for number, clip in enumerate(requested, start=1):
        label = f"clip {number} ({_clip_range_text(clip)})"
        if not normalize_clips([clip], duration):
            notes.append(
                f"{label} is outside the video or shorter than {MIN_CLIP_SECONDS:g} s, "
                "so it was skipped"
            )
        elif not math.isinf(clip.end) and clip.end > duration + 0.001:
            notes.append(
                f"{label} runs past the end of the video, so it was cut at "
                f"{format_timecode(duration)}"
            )
    return notes


class ClipRun(NamedTuple):
    """The encodes to run for one video: resolved plan (None = whole file), jobs, source info."""

    plan: Optional[ClipPlan]
    jobs: List[ClipJob]
    info: Any


def plan_clip_run(
    input_path: Path,
    clips: ClipPlan,
    profile: CompressionProfile,
    compressor: VideoCompressor,
) -> ClipRun:
    """Analyze a video and expand the requested clips into jobs.

    Raises ValueError when the file cannot be read or no clip falls inside it.
    """
    info = compressor.analyze_media(input_path)
    if not info:
        raise ValueError(f"Could not analyze {input_path.name}")
    duration = float(getattr(info, 'duration', 0.0) or 0.0)

    for note in clip_warnings(clips.clips, duration):
        print(f"Warning: {input_path.name}: {note}")
    if not normalize_clips(clips.clips, duration):
        raise ValueError(
            f"none of the --clip ranges fall inside this video ({format_timecode(duration)} long)"
        )

    plan = resolve_clip_plan(clips, profile, duration)
    if plan is None:
        print(f"Note: the clip covers all of {input_path.name}; compressing the whole video")
    # Name files after the clips the user typed: when some were dropped the
    # survivors keep their own numbers, and a lone survivor of several
    # requested clips still gets its clipNN suffix instead of the whole-file name.
    numbers = [n for n, clip in enumerate(clips.clips, start=1) if normalize_clips([clip], duration)]
    if plan is None or len(numbers) != len(plan.clips):
        numbers = None
    jobs = expand_clip_jobs(
        plan,
        force_tokens=clips.mode is ClipMode.SEPARATE and len(clips.clips) >= 2,
        numbers=numbers,
    )
    return ClipRun(plan, jobs, info)


def _print_target_preview(
    compressor: VideoCompressor,
    profile: CompressionProfile,
    analyzed: Any,
    prefix: str = "  -> ",
    warn_skip: bool = True,
) -> None:
    """Dry-run lines for target-size mode (the size band the encode will aim for)."""
    if not (profile.target_size_mb or profile.target_reduction_percent):
        return
    if analyzed and hasattr(analyzed, 'video_codec'):
        plan = compressor._resolve_target_size_plan(profile, analyzed)
        if plan:
            print(f"{prefix}Target: {plan.target_size_mb} MB ({plan.mode.value})")
            print(f"{prefix}Preview: {plan.video_bitrate // 1000} kbps video, ±{int(plan.tolerance_ratio * 100)}% tolerance")
            if warn_skip and plan.target_size_bytes >= analyzed.size:
                print(f"{prefix}Skip: target is not smaller than the source file")


def _job_note(job: ClipJob) -> str:
    """Short description of one encode, for the dry-run output list."""
    plan = job.plan
    length = _length_text(plan.total_duration)
    if job.title:
        return f"{job.title}, {length}"
    if plan.count > 1:
        return f"{plan.count} clips joined, {length}"
    return length


def _print_clip_dry_run(
    run: ClipRun,
    profile: CompressionProfile,
    compressor: VideoCompressor,
    output_for: Callable[[ClipJob], Path],
) -> None:
    """Dry-run lines for a clipped video: each clip, the mode, the total length, each output."""
    plan = run.plan
    if plan.count == 1:
        mode_text = "single clip"
    elif plan.mode is ClipMode.JOIN:
        mode_text = "join (all clips in one file)"
    else:
        mode_text = "separate (one file per clip)"
    source_length = float(getattr(run.info, 'duration', 0.0) or 0.0)
    total = effective_media_info(run.info, plan).duration

    print(f"  -> Mode: {mode_text}")
    print(f"  -> Clips: {plan.count}")
    for number, clip in enumerate(plan.clips, start=1):
        print(f"       {number}. {_clip_range_text(clip)} (length {_length_text(clip.duration)})")
    if source_length > 0:
        print(f"  -> Duration: {format_timecode(total)} of {format_timecode(source_length)}")
    else:
        print(f"  -> Duration: {format_timecode(total)}")
    for job in run.jobs:
        print(f"  -> Output: {output_for(job).name} ({_job_note(job)})")
        _print_target_preview(
            compressor,
            profile,
            effective_media_info(run.info, job.plan),
            prefix="       ",
            warn_skip=False,
        )


def _report_result(result: CompressionResult) -> None:
    """Print the outcome of one encode."""
    if result.success:
        if result.skipped:
            print("  - Skipped")
        else:
            print(f"  ✓ Completed!")
        print(f"    Original:  {format_size(result.original_size)}")
        print(f"    Compressed: {format_size(result.compressed_size)}")
        print(f"    Reduction: {result.reduction}")
        print(f"    Time: {format_time(result.encoding_time)}")
        if result.note:
            print(f"    Note: {result.note}")
        if result.software_fallback_required:
            print(
                "    Software fallback was not run. "
                "Re-run with --allow-software-fallback to confirm it."
            )
        if result.output_format:
            print(f"    Output: {result.output_format.upper()}")
    else:
        print(f"  ✗ Failed: {result.error_message}")
        if result.software_fallback_required:
            print(
                "    Software fallback was not run. "
                "Re-run with --allow-software-fallback to confirm it."
            )


def process_file(
    input_path: Path,
    output_dir: Optional[Path],
    suffix: str,
    profile: CompressionProfile,
    compressor: VideoCompressor,
    dry_run: bool = False,
    parallel_jobs: int = 1,
    clips: Optional[ClipPlan] = None,
) -> List[CompressionResult]:
    """Process a single file.

    Returns one result per encode: one for a whole file, a joined run or a single
    clip, one per clip when several clips are saved as separate files. A dry run
    returns an empty list.
    """
    media_type = detect_media_type(input_path)
    if media_type == "image":
        ext = f".{get_image_extension(profile.image_format)}"
    else:
        ext = f".{get_video_extension(profile.video_codec.value, profile.video_container)}"

    # Determine output path
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        out_dir = output_dir
    else:
        out_dir = input_path.parent

    def output_for(job: ClipJob) -> Path:
        tag = f"_{job.token}" if job.token else ""
        return out_dir / f"{input_path.stem}{suffix}{tag}{ext}"

    run = ClipRun(None, [ClipJob(None)], None)
    if clips is not None:
        if media_type == "image":
            print(f"Warning: --clip is ignored for images ({input_path.name})")
        else:
            try:
                run = plan_clip_run(input_path, clips, profile, compressor)
            except ValueError as exc:
                print(f"Error: {input_path.name}: {exc}")
                return [
                    CompressionResult(
                        success=False,
                        input_file=input_path,
                        error_message=str(exc),
                        media_type=media_type or "video",
                    )
                ]
    clipped = run.plan is not None

    if dry_run:
        print(f"Would compress: {input_path.name}")
        if not clipped:
            print(f"  -> Output: {output_for(run.jobs[0]).name}")
        print(f"  -> Profile: {profile.name}")
        print(f"  -> Type: {media_type or 'unknown'}")
        if media_type == 'video':
            print(f"  -> Codec: {profile.video_codec.value} in {profile.video_container}")
            if not clipped:
                if profile.target_size_mb or profile.target_reduction_percent:
                    _print_target_preview(compressor, profile, compressor.analyze_media(input_path))
        else:
            print(f"  -> Format: {profile.image_format}")
        if clipped:
            _print_clip_dry_run(run, profile, compressor, output_for)
        return []

    results: List[CompressionResult] = []
    for job in run.jobs:
        output_path = output_for(job)
        title = job.title
        if not title and job.plan is not None and job.plan.count > 1:
            title = f"joining {job.plan.count} clips"

        print(f"\nCompressing: {input_path.name}" + (f" ({title})" if title else ""))
        print(f"  Output: {output_path.name}")
        print(f"  Profile: {profile.name}")
        if job.plan is not None:
            label = "Clips" if job.plan.count > 1 else "Clip"
            print(f"  {label}: " + ", ".join(_clip_range_text(clip) for clip in job.plan.clips))

        compress_args = dict(
            input_file=input_path,
            output_file=output_path,
            profile=profile,
            parallel_jobs=max(1, int(parallel_jobs or 1)),
        )
        if job.plan is not None:
            compress_args['clips'] = job.plan
        result = compressor.compress(**compress_args)

        _report_result(result)
        results.append(result)

    if len(run.jobs) > 1:
        done = sum(1 for r in results if r.success)
        print(f"\n{input_path.name}: {done} of {len(run.jobs)} clips encoded")

    return results


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    # A piped or legacy Windows console cannot print the check marks below;
    # show "?" for those instead of crashing after the encode has finished.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = create_parser()
    args = parser.parse_args(attach_clip_values(sys.argv[1:] if argv is None else argv))
    
    # Setup logging
    if args.quiet:
        logging.getLogger().setLevel(logging.ERROR)
    elif args.verbose >= 2:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.verbose >= 1:
        logging.getLogger().setLevel(logging.INFO)
    
    # Initialize compressor
    compressor = VideoCompressor()
    if args.allow_software_fallback:
        compressor.set_software_fallback_callback(lambda _request: True)
    
    # Handle info commands
    if args.hardware:
        show_hardware()
        return 0
    
    if args.list_codecs:
        list_codecs(compressor)
        return 0

    # Clips (--clip / --join)
    try:
        clip_plan = get_clip_plan(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1
    
    # Process input files
    input_files: List[Path] = []
    for input_spec in args.inputs:
        path = Path(input_spec)
        if path.is_dir():
            for ext in SUPPORTED_MEDIA_EXTENSIONS:
                input_files.extend(path.glob(f'*{ext}'))
                input_files.extend(path.glob(f'*{ext.upper()}'))
        elif '*' in input_spec:
            # Glob pattern
            input_files.extend(Path().glob(input_spec))
        else:
            input_files.append(path)

    # Remove duplicates and sort
    input_files = sorted(set(input_files))
    
    if not input_files:
        print("Error: No input files found.")
        return 1
    
    # Show info mode
    if args.info:
        for filepath in input_files:
            show_info(filepath, compressor)
        return 0
    
    # Get profile
    try:
        profile = get_profile(args)
    except ValueError as exc:
        print(f"Error: {exc}")
        return 1

    # Apply batch strategy
    input_files = sort_input_files(input_files, args.batch_order, compressor)
    
    # Get output directory
    output_dir = Path(args.output) if args.output else None

    if args.compare:
        if clip_plan is not None:
            print("Note: --clip is ignored with --compare")
        compare_profiles = build_compare_profiles(profile, compressor)
        overall_success = True
        for filepath in input_files:
            if detect_media_type(filepath) != 'video':
                print(f"Skipping compare mode for non-video input: {filepath.name}")
                continue
            compare_dir = (output_dir or filepath.parent) / "_compare_samples"
            compare_dir.mkdir(parents=True, exist_ok=True)
            print(f"\nComparing sample presets for: {filepath.name}")
            results = compressor.compare_sample(
                input_file=filepath,
                output_dir=compare_dir,
                profiles=compare_profiles,
                sample_seconds=max(1, args.compare_seconds),
            )
            for result in results:
                if result.success:
                    print(f"  ✓ {result.note}")
                    print(f"    Output: {result.output_file.name if result.output_file else 'n/a'}")
                    print(f"    Size: {format_size(result.compressed_size)}  ·  Time: {format_time(result.encoding_time)}")
                else:
                    overall_success = False
                    print(f"  ✗ {result.note or result.video_codec}: {result.error_message}")
            print(f"  Samples saved to: {compare_dir}")
        return 0 if overall_success else 1
    
    # Process files
    results: List[CompressionResult] = []
    
    print(f"\nSquishIt")
    print(f"{'='*50}")
    print(f"Files: {len(input_files)}")
    print(f"Profile: {profile.name}")
    print(f"Codec: {profile.video_codec.value}")
    if clip_plan is not None:
        if clip_plan.count == 1:
            print("Clips: 1")
        elif clip_plan.mode is ClipMode.JOIN:
            print(f"Clips: {clip_plan.count} (joined into one file)")
        else:
            print(f"Clips: {clip_plan.count} (saved as separate files)")

    existing_files = [filepath for filepath in input_files if filepath.exists()]
    for filepath in input_files:
        if filepath not in existing_files:
            print(f"Warning: File not found: {filepath}")

    slots = 1
    if existing_files and not args.dry_run:
        slots = compressor.queue_slots(
            profile,
            requested_parallel=max(1, int(args.jobs or 1)),
            queued_files=len(existing_files),
        )

    def _run_one(filepath: Path) -> List[CompressionResult]:
        return process_file(
            input_path=filepath,
            output_dir=output_dir,
            suffix=args.suffix,
            profile=profile,
            compressor=compressor,
            dry_run=args.dry_run,
            parallel_jobs=slots,
            clips=clip_plan,
        )

    if slots <= 1 or args.dry_run:
        for filepath in existing_files:
            results.extend(_run_one(filepath))
    else:
        with ThreadPoolExecutor(max_workers=slots) as pool:
            for file_results in pool.map(_run_one, existing_files):
                results.extend(file_results)
    
    # Summary
    if results and not args.dry_run:
        print(f"\n{'='*50}")
        print("Summary")
        print(f"{'='*50}")
        successful = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        
        if successful:
            total_original = sum(r.original_size for r in successful)
            total_compressed = sum(r.compressed_size for r in successful)
            total_time = sum(r.encoding_time for r in successful)
            
            sources = {r.input_file for r in results}
            if len(sources) != len(results):
                # Several clips per file: say how many files and how many outputs.
                print(f"  Source files: {len(sources)}  ·  Outputs: {len(results)}")
            print(f"  Successful: {len(successful)}")
            print(f"  Failed: {len(failed)}")
            print(f"  Total Original: {format_size(total_original)}")
            print(f"  Total Compressed: {format_size(total_compressed)}")
            print(f"  Total Saved: {format_size(total_original - total_compressed)}")
            print(f"  Total Time: {format_time(total_time)}")
        
        if failed:
            print(f"\nFailed files:")
            for r in failed:
                where = f" ({r.output_file.name})" if clip_plan is not None and r.output_file else ""
                print(f"  • {r.input_file.name}{where}: {r.error_message}")
    
    return 0 if all(r.success for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())