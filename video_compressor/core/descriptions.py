"""
Centralized UI descriptions and tooltips for every compression setting.

GUI widgets read from SETTING_DESCRIPTIONS for hover tooltips and inline help text.
Per-encoder overrides live inside each entry's ``per_encoder`` dict.
"""

from typing import Dict, Any

SETTING_DESCRIPTIONS: Dict[str, Dict[str, Any]] = {
    # ── Video Encoding ─────────────────────────────────────────────
    "encoder": {
        "name": "Encoder",
        "description": (
            "Which encoder to use. CPU encoders (x264, x265, SVT-AV1) are slower "
            "but higher quality. GPU encoders (NVENC, QSV, AMF) are very fast but "
            "slightly lower quality at the same bitrate."
        ),
    },
    "rate_control": {
        "name": "Rate Control",
        "description": (
            "How bitrate is allocated. CRF/CQP = constant quality (file size varies). "
            "CBR = constant bitrate (predictable size). VBR = variable bitrate "
            "(quality varies by scene complexity)."
        ),
    },
    "crf": {
        "name": "Quality (CRF)",
        "description": "Lower = better quality, larger file.",
        "per_encoder": {
            "libx264": "0 = lossless, 18 = visually lossless, 23 = default, 51 = worst.",
            "libx265": "0 = lossless, 20 = visually lossless, 28 = default, 51 = worst.",
            "libvpx-vp9": "0 = lossless, 10 = very high, 31 = default, 63 = worst.",
            "libsvtav1": "0 = lossless, 18 = very high, 35 = default, 63 = worst.",
            "libaom-av1": "0 = lossless, 15 = very high, 30 = default, 63 = worst.",
        },
    },
    "cqp": {
        "name": "Quality (CQP)",
        "description": (
            "Constant quantizer parameter. Lower = better quality, larger file. "
            "Hardware encoder equivalent of CRF."
        ),
    },
    "icq": {
        "name": "Quality (ICQ)",
        "description": (
            "Intelligent Constant Quality (Intel QSV only). Adaptive version of CQP "
            "that allocates more bits to complex scenes. 1 = best, 51 = worst."
        ),
    },
    "qvbr": {
        "name": "Quality (QVBR)",
        "description": (
            "Quality-based VBR (AMD AMF only). AMD's best quality mode — targets a "
            "quality level while allowing variable bitrate. 1 = best, 51 = worst."
        ),
    },
    "video_bitrate": {
        "name": "Bitrate (kbps)",
        "description": (
            "Target average bitrate in kilobits per second. "
            "5000-8000 for 1080p, 15000-40000 for 4K."
        ),
    },
    "max_bitrate": {
        "name": "Max Bitrate",
        "description": (
            "Maximum bitrate ceiling for complex scenes in VBR mode. "
            "Usually 1.5-2x the target bitrate."
        ),
    },
    "buffer_size": {
        "name": "Buffer Size",
        "description": (
            "Rate control buffer. Larger = smoother quality, higher latency. "
            "Default: 2x bitrate."
        ),
    },
    "keyframe_interval": {
        "name": "Keyframe Interval",
        "description": (
            "Seconds between keyframes. 0 = auto (recommended). "
            "Lower = better seeking, larger file."
        ),
    },
    "preset": {
        "name": "Preset",
        "description": (
            "Speed vs quality tradeoff. Faster presets = larger files at same quality. "
            "Does NOT affect decode speed."
        ),
        "per_encoder": {
            "libx264": "ultrafast → placebo. 'medium' is the default; 'slow' is a good balance.",
            "libx265": "ultrafast → placebo. 'medium' is the default; 'slow' is a good balance.",
            "libvpx-vp9": "0 (slowest) → 8 (fastest). '4' is a good default.",
            "libsvtav1": "0 (reference quality) → 13 (fastest). '8' is a good default.",
            "libaom-av1": "0 (reference) → 10 (fastest). '6' is practical.",
            "nvenc": "p1 (fastest) → p7 (highest quality). 'p5' is a good default.",
            "qsv": "veryfast → veryslow. 'medium' is default.",
            "amf": "speed / balanced / quality. 'balanced' is default.",
        },
    },
    "codec_profile": {
        "name": "Profile",
        "description": (
            "Codec complexity level. 'High' uses advanced features for better compression. "
            "Use 'baseline' only for old device compatibility."
        ),
        "per_encoder": {
            "libx264": "baseline (most compatible), main (better compression), high (best quality).",
            "libx265": "main (8-bit), main10 (10-bit HDR).",
        },
    },
    "tune": {
        "name": "Tune",
        "description": (
            "Optimizes encoder for specific content type."
        ),
        "per_encoder": {
            "libx264": (
                "film (live action), animation (cartoons/anime), grain (preserves film grain), "
                "stillimage (slide shows), psnr/ssim (benchmark), fastdecode (low-power playback), "
                "zerolatency (live/webcam)."
            ),
            "libx265": "grain, animation, psnr, ssim, fastdecode, zerolatency.",
            "nvenc": "hq (quality-optimized), ll (low latency), ull (ultra-low latency).",
        },
    },
    "b_frames": {
        "name": "B-Frames",
        "description": (
            "Bidirectionally-predicted frames that reference past AND future frames. "
            "More = better compression, slightly slower."
        ),
    },
    "multipass": {
        "name": "Multipass",
        "description": (
            "Run multiple encoding passes (NVENC only). "
            "'Full Resolution' gives best quality but is slower."
        ),
    },
    "psycho_aq": {
        "name": "Psycho Visual Tuning",
        "description": (
            "Adaptive quantization that mimics human perception (NVENC only) — "
            "allocates more bits to visually important areas."
        ),
    },
    "gpu_index": {
        "name": "GPU Selection",
        "description": "Which GPU to use for encoding. 0 = primary GPU.",
    },
    "film_grain": {
        "name": "Film Grain Synthesis",
        "description": (
            "Adds artificial grain to recover detail lost in compression (SVT-AV1 only). "
            "0 = off, 1-50 = increasing grain."
        ),
    },
    "custom_encoder_opts": {
        "name": "Custom Encoder Options",
        "description": (
            "Raw FFmpeg encoder options for experts "
            "(e.g., '-x264-params rc-lookahead=60:ref=6')."
        ),
    },

    # ── Audio ──────────────────────────────────────────────────────
    "audio_mode": {
        "name": "Audio Mode",
        "description": (
            "Auto: copies compatible audio, re-encodes otherwise. "
            "Copy: bitperfect passthrough. Re-encode: always converts. "
            "Strip: removes audio."
        ),
    },
    "audio_codec": {
        "name": "Audio Encoder",
        "description": (
            "AAC for maximum compatibility. Opus for best quality per bit. "
            "FLAC/ALAC for lossless."
        ),
    },
    "audio_bitrate": {
        "name": "Audio Bitrate",
        "description": (
            "Higher = better quality. 128k good for Opus, 192k for AAC/MP3, "
            "384k for AC-3 surround."
        ),
    },

    # ── Output ─────────────────────────────────────────────────────
    "container": {
        "name": "Container",
        "description": (
            "MP4: universal. MKV: supports everything. "
            "WebM: web-native (VP9/AV1 + Opus only). MOV: Apple ecosystem."
        ),
    },
    "max_resolution": {
        "name": "Resolution Limit",
        "description": (
            "Maximum output height. 'Original' keeps source resolution. "
            "Useful for reducing 4K to 1080p."
        ),
    },
    "frame_rate": {
        "name": "Frame Rate",
        "description": (
            "Maximum output FPS. 'Original' keeps source. "
            "30 fps halves data for 60 fps sources."
        ),
    },
}


def get_description(key: str, encoder: str | None = None) -> str:
    """Return the tooltip text for a setting, optionally specialized for an encoder."""
    entry = SETTING_DESCRIPTIONS.get(key)
    if not entry:
        return ""
    base = entry.get("description", "")
    if encoder:
        per = entry.get("per_encoder", {})
        # Try exact encoder name, then partial family match
        extra = per.get(encoder)
        if extra is None:
            for family in ("nvenc", "qsv", "amf"):
                if family in (encoder or ""):
                    extra = per.get(family)
                    break
        if extra:
            return f"{base}\n\n{extra}"
    return base
