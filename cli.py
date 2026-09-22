#!/usr/bin/env python3
"""Command-line interface for SquishIt."""

import argparse
import sys
from pathlib import Path
from typing import List, Optional
import logging

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
            'AV1 is available as svt-av1 (archival lane) or av1 (libaom).'
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
        help='How target-size mode should chase the requested MB; exact will force software, degrade aggressively, and pad when needed'
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
    
    # Hardware acceleration
    parser.add_argument(
        '--no-hw-accel',
        action='store_true',
        help='Disable hardware acceleration'
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
    
    if info.has_hw_encoder:
        print(f"\n  Hardware Encoding: Available ({info.preferred_hw_encoder})")
    else:
        print(f"\n  Hardware Encoding: Not Available")
    
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


def process_file(
    input_path: Path,
    output_dir: Optional[Path],
    suffix: str,
    profile: CompressionProfile,
    compressor: VideoCompressor,
    dry_run: bool = False
) -> Optional[CompressionResult]:
    """Process a single file."""
    media_type = detect_media_type(input_path)
    if media_type == "image":
        ext = f".{get_image_extension(profile.image_format)}"
    else:
        ext = f".{get_video_extension(profile.video_codec.value, profile.video_container)}"

    # Determine output path
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / f"{input_path.stem}{suffix}{ext}"
    else:
        output_path = input_path.parent / f"{input_path.stem}{suffix}{ext}"
    
    if dry_run:
        print(f"Would compress: {input_path.name}")
        print(f"  -> Output: {output_path.name}")
        print(f"  -> Profile: {profile.name}")
        print(f"  -> Type: {media_type or 'unknown'}")
        if media_type == 'video':
            print(f"  -> Codec: {profile.video_codec.value} in {profile.video_container}")
            if profile.target_size_mb or profile.target_reduction_percent:
                analyzed = compressor.analyze_media(input_path)
                if analyzed and hasattr(analyzed, 'video_codec'):
                    plan = compressor._resolve_target_size_plan(profile, analyzed)
                    if plan:
                        print(f"  -> Target: {plan.target_size_mb} MB ({plan.mode.value})")
                        print(f"  -> Preview: {plan.video_bitrate // 1000} kbps video, ±{int(plan.tolerance_ratio * 100)}% tolerance")
                        if plan.target_size_bytes >= analyzed.size:
                            print("  -> Skip: target is not smaller than the source file")
        else:
            print(f"  -> Format: {profile.image_format}")
        return None
    
    print(f"\nCompressing: {input_path.name}")
    print(f"  Output: {output_path.name}")
    print(f"  Profile: {profile.name}")
    
    result = compressor.compress(
        input_file=input_path,
        output_file=output_path,
        profile=profile
    )
    
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
        if result.output_format:
            print(f"    Output: {result.output_format.upper()}")
    else:
        print(f"  ✗ Failed: {result.error_message}")
    
    return result


def main(argv: Optional[List[str]] = None) -> int:
    """Main entry point."""
    parser = create_parser()
    args = parser.parse_args(argv)
    
    # Setup logging
    if args.quiet:
        logging.getLogger().setLevel(logging.ERROR)
    elif args.verbose >= 2:
        logging.getLogger().setLevel(logging.DEBUG)
    elif args.verbose >= 1:
        logging.getLogger().setLevel(logging.INFO)
    
    # Initialize compressor
    compressor = VideoCompressor()
    
    # Handle info commands
    if args.hardware:
        show_hardware()
        return 0
    
    if args.list_codecs:
        list_codecs(compressor)
        return 0
    
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
    
    for filepath in input_files:
        if not filepath.exists():
            print(f"Warning: File not found: {filepath}")
            continue
        
        result = process_file(
            input_path=filepath,
            output_dir=output_dir,
            suffix=args.suffix,
            profile=profile,
            compressor=compressor,
            dry_run=args.dry_run
        )
        
        if result:
            results.append(result)
    
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
            
            print(f"  Successful: {len(successful)}")
            print(f"  Failed: {len(failed)}")
            print(f"  Total Original: {format_size(total_original)}")
            print(f"  Total Compressed: {format_size(total_compressed)}")
            print(f"  Total Saved: {format_size(total_original - total_compressed)}")
            print(f"  Total Time: {format_time(total_time)}")
        
        if failed:
            print(f"\nFailed files:")
            for r in failed:
                print(f"  • {r.input_file.name}: {r.error_message}")
    
    return 0 if all(r.success for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())