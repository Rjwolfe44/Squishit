"""Visual profile editor dialog — create, edit, duplicate, and delete profiles."""

from __future__ import annotations

from typing import Any, Callable, List, Optional
from pathlib import Path
from tkinter import filedialog, messagebox

import yaml

import customtkinter as ctk

from .copy import profile_ui_text
from .scaling import apply_dialog_geometry
from .widgets import COLORS, _lbl
from ..core.codecs import VideoCodec, AudioCodec, RateControl, AudioMode, ENCODER_REGISTRY
from ..core.profiles import CompressionProfile, ProfileManager, ProfileType


_CODEC_LABELS = {
    "hevc": "HEVC (H.265)",
    "h264": "H.264 (AVC)",
    "vp9": "VP9",
    "svt-av1": "SVT-AV1 (Fast AV1)",
    "av1": "AV1 (AOMedia)",
}
_CODEC_MAP = {
    "HEVC (H.265)": VideoCodec.HEVC,
    "H.264 (AVC)": VideoCodec.H264,
    "VP9": VideoCodec.VP9,
    "SVT-AV1 (Fast AV1)": VideoCodec.SVT_AV1,
    "AV1 (AOMedia)": VideoCodec.AV1,
}
_CODEC_REVERSE = {v: k for k, v in _CODEC_MAP.items()}

_AUDIO_CODEC_MAP = {
    "AAC": AudioCodec.AAC,
    "Opus": AudioCodec.OPUS,
    "MP3": AudioCodec.MP3,
    "Vorbis": AudioCodec.VORBIS,
    "FLAC": AudioCodec.FLAC,
    "ALAC": AudioCodec.ALAC,
    "AC-3": AudioCodec.AC3,
}
_AUDIO_CODEC_REVERSE = {v: k for k, v in _AUDIO_CODEC_MAP.items()}

_RATE_CONTROL_MAP = {
    "CRF (Constant Rate Factor)": RateControl.CRF,
    "CQP (Constant QP)": RateControl.CQP,
    "CBR (Constant Bitrate)": RateControl.CBR,
    "ABR (Average Bitrate)": RateControl.ABR,
    "VBR (Variable Bitrate)": RateControl.VBR,
    "ICQ (Intelligent CQ)": RateControl.ICQ,
    "QVBR (Quality VBR)": RateControl.QVBR,
    "Lossless": RateControl.LOSSLESS,
}
_RATE_CONTROL_REVERSE = {v: k for k, v in _RATE_CONTROL_MAP.items()}

_AUDIO_MODE_MAP = {
    "Auto": AudioMode.AUTO,
    "Copy (passthrough)": AudioMode.COPY,
    "Re-encode": AudioMode.REENCODE,
    "Strip (no audio)": AudioMode.STRIP,
}
_AUDIO_MODE_REVERSE = {v: k for k, v in _AUDIO_MODE_MAP.items()}

_PRESETS = [
    "ultrafast", "superfast", "veryfast", "fast",
    "medium", "slow", "slower", "veryslow",
]
_AUDIO_KBPS = ["320", "256", "192", "128", "96"]
_RESOLUTIONS = ["No limit", "4K", "1440p", "1080p", "720p", "480p"]
_RES_MAP = {"No limit": None, "4K": 2160, "1440p": 1440, "1080p": 1080, "720p": 720, "480p": 480}
_RES_REVERSE = {v: k for k, v in _RES_MAP.items()}
_FPS_OPTIONS = ["No limit", "60", "30", "24"]
_FPS_MAP = {"No limit": None, "60": 60, "30": 30, "24": 24}
_FPS_REVERSE = {v: k for k, v in _FPS_MAP.items()}
_TARGET_MODES = ["auto", "fast", "balanced", "strict", "exact"]
_RESOURCE_GOVERNORS = ["auto", "low", "balanced", "max"]
_EXACT_AUDIO_POLICIES = ["keep", "reduce", "drop"]

_BUILTIN_TYPES = {ProfileType.FAST, ProfileType.BALANCED, ProfileType.MAX}

_CRF_TIERS = [
    (0,  "Lossless"),
    (15, "Near-lossless"),
    (18, "Very high quality"),
    (22, "High quality"),
    (26, "Good quality"),
    (30, "Medium quality"),
    (36, "Low quality"),
    (45, "Very low quality"),
]


def _crf_tier(crf: int) -> str:
    for threshold, label in reversed(_CRF_TIERS):
        if crf >= threshold:
            return label
    return "Lossless"


class ProfileEditorDialog(ctk.CTkToplevel):
    """Modal dialog to create / edit / duplicate / delete compression profiles."""

    def __init__(
        self,
        master: Any,
        profile_manager: ProfileManager,
        on_close: Optional[Callable[[], None]] = None,
        **kwargs,
    ):
        super().__init__(master, **kwargs)
        self.title("Profile Editor")
        self.configure(fg_color=COLORS["bg"])
        self.transient(master)
        self.grab_set()

        self._pm = profile_manager
        self._on_close = on_close
        self._selected_name: Optional[str] = None
        self._dirty = False

        self._build()
        apply_dialog_geometry(self, master, base_width=560, base_height=600, min_width=500, min_height=500)

        self.protocol("WM_DELETE_WINDOW", self._close)

    # ── layout ───────────────────────────────────────────────────

    def _build(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=16)

        # Toolbar
        tb = ctk.CTkFrame(body, fg_color="transparent", height=36)
        tb.pack(fill="x")
        tb.pack_propagate(False)

        for text, cmd in [
            ("New", self._new), ("Duplicate", self._duplicate), ("Delete", self._delete), ("Import", self._import_profile), ("Export", self._export_profile),
        ]:
            ctk.CTkButton(
                tb, text=text, width=72, height=28, corner_radius=6,
                fg_color=COLORS["surface_raised"], hover_color=COLORS["border"],
                text_color=COLORS["text_dim"], font=ctk.CTkFont(size=12),
                command=cmd,
            ).pack(side="left", padx=(0, 6))

        self._save_btn = ctk.CTkButton(
            tb, text="Save", width=72, height=28, corner_radius=6,
            fg_color=COLORS["accent"], hover_color=COLORS["accent_hover"],
            text_color="#ffffff", font=ctk.CTkFont(size=12, weight="bold"),
            command=self._save,
        )
        self._save_btn.pack(side="right")

        # Main content — left list + right form
        content = ctk.CTkFrame(body, fg_color="transparent")
        content.pack(fill="both", expand=True, pady=(10, 0))

        # Left list
        left = ctk.CTkScrollableFrame(
            content, fg_color=COLORS["surface"], corner_radius=10, width=160,
            scrollbar_button_color=COLORS["surface_raised"],
            scrollbar_button_hover_color=COLORS["border"],
        )
        left.pack(side="left", fill="y")
        self._list_frame = left

        # Right form
        right = ctk.CTkScrollableFrame(
            content, fg_color="transparent",
            scrollbar_button_color=COLORS["surface_raised"],
            scrollbar_button_hover_color=COLORS["border"],
        )
        right.pack(side="left", fill="both", expand=True, padx=(12, 0))
        self._form = right

        self._populate_list()
        self._build_form()

        # Select the first profile
        profiles = self._pm.get_all_profiles()
        if profiles:
            self._select(profiles[0].name)

    # ── profile list ─────────────────────────────────────────────

    def _populate_list(self):
        for w in self._list_frame.winfo_children():
            w.destroy()
        self._list_btns: dict[str, ctk.CTkButton] = {}

        for p in self._pm.get_all_profiles():
            is_builtin = p.profile_type in _BUILTIN_TYPES
            prefix = "🔒 " if is_builtin else ""
            label, _blurb = profile_ui_text(p.name, p.description)
            btn = ctk.CTkButton(
                self._list_frame,
                text=f"{prefix}{label}",
                height=30, corner_radius=6, anchor="w",
                fg_color="transparent", hover_color=COLORS["surface_raised"],
                text_color=COLORS["text_dim"], font=ctk.CTkFont(size=12),
                command=lambda n=p.name: self._select(n),
            )
            btn.pack(fill="x", pady=(0, 2))
            self._list_btns[p.name] = btn

    def _select(self, name: str):
        self._selected_name = name
        for n, btn in self._list_btns.items():
            if n == name:
                btn.configure(fg_color=COLORS["accent"], text_color="#ffffff")
            else:
                btn.configure(fg_color="transparent", text_color=COLORS["text_dim"])
        self._load_profile(name)

    def _import_profile(self):
        profile_path = filedialog.askopenfilename(
            title="Import Profile",
            filetypes=[("YAML files", "*.yaml *.yml"), ("All files", "*.*")],
        )
        if not profile_path:
            return
        try:
            with open(profile_path, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            profile = CompressionProfile.from_dict(data)
            if not profile.name.strip():
                raise ValueError("Profile file is missing a name")
            profile.created_by = "import"
            if not self._pm.save_profile(profile):
                raise ValueError("Could not save imported profile")
            self._populate_list()
            self._select(profile.name)
        except Exception as exc:
            messagebox.showerror("Profile Import", str(exc), parent=self)

    def _export_profile(self):
        if not self._selected_name:
            return
        profile = self._pm.get_profile(self._selected_name)
        if not profile:
            return
        destination = filedialog.asksaveasfilename(
            title="Export Profile",
            defaultextension=".yaml",
            initialfile=f"{profile.name.lower().replace(' ', '_')}.yaml",
            filetypes=[("YAML files", "*.yaml"), ("All files", "*.*")],
        )
        if not destination:
            return
        try:
            with open(Path(destination), "w", encoding="utf-8") as fh:
                yaml.safe_dump(profile.to_dict(), fh, sort_keys=False)
        except Exception as exc:
            messagebox.showerror("Profile Export", str(exc), parent=self)

    # ── form ─────────────────────────────────────────────────────

    def _build_form(self):
        f = self._form

        # Name
        _lbl(f, "Name", size=12, color=COLORS["text_dim"]).pack(anchor="w", pady=(0, 3))
        self._name_var = ctk.StringVar()
        self._name_entry = ctk.CTkEntry(
            f, textvariable=self._name_var, height=30, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=12),
        )
        self._name_entry.pack(fill="x", pady=(0, 10))

        # Description
        _lbl(f, "Description", size=12, color=COLORS["text_dim"]).pack(anchor="w", pady=(0, 3))
        self._desc_var = ctk.StringVar()
        self._desc_entry = ctk.CTkEntry(
            f, textvariable=self._desc_var, height=30, corner_radius=7,
            fg_color=COLORS["surface_raised"], border_color=COLORS["border"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=12),
        )
        self._desc_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkFrame(f, height=1, fg_color=COLORS["border"]).pack(fill="x", pady=6)
        _lbl(f, "VIDEO", size=10, weight="bold", color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 6))

        # Codec
        self._codec_var = ctk.StringVar()
        self._menu_row(f, "Codec", self._codec_var, list(_CODEC_MAP.keys()))
        self._codec_var.trace_add("write", lambda *_: self._on_codec_changed())

        # Rate control
        self._rate_control_var = ctk.StringVar()
        self._rc_menu = self._menu_row(f, "Rate control", self._rate_control_var, list(_RATE_CONTROL_MAP.keys()), return_menu=True)

        # Preset
        self._preset_var = ctk.StringVar()
        self._preset_menu = self._menu_row(f, "Preset", self._preset_var, _PRESETS, return_menu=True)

        # Tune
        self._tune_var = ctk.StringVar(value="(none)")
        self._tune_menu = self._menu_row(f, "Tune", self._tune_var, ["(none)"], return_menu=True)

        # CRF slider
        self._crf_frame = ctk.CTkFrame(f, fg_color="transparent")
        self._crf_frame.pack(fill="x", pady=(0, 10))
        _lbl(self._crf_frame, "CRF", size=12, color=COLORS["text_dim"], width=90).pack(side="left")
        self._crf_slider = ctk.CTkSlider(
            self._crf_frame, from_=0, to=51, number_of_steps=51,
            width=110, height=14,
            fg_color=COLORS["progress_track"], progress_color=COLORS["accent"],
            command=self._on_crf,
        )
        self._crf_slider.pack(side="left")
        self._crf_lbl = _lbl(self._crf_frame, "22 — High quality", size=11, color=COLORS["text_dim"], width=130)
        self._crf_lbl.pack(side="left", padx=(8, 0))

        # Container
        self._container_var = ctk.StringVar()
        self._menu_row(f, "Container", self._container_var, ["mp4", "mkv", "webm", "mov", "avi"])

        # Resolution
        self._res_var = ctk.StringVar()
        self._menu_row(f, "Max resolution", self._res_var, _RESOLUTIONS)

        # FPS
        self._fps_var = ctk.StringVar()
        self._menu_row(f, "Max FPS", self._fps_var, _FPS_OPTIONS)

        ctk.CTkFrame(f, height=1, fg_color=COLORS["border"]).pack(fill="x", pady=6)
        _lbl(f, "AUDIO", size=10, weight="bold", color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 6))

        # Audio mode
        self._audio_mode_var = ctk.StringVar()
        self._menu_row(f, "Audio mode", self._audio_mode_var, list(_AUDIO_MODE_MAP.keys()))

        # Audio codec
        self._audio_codec_var = ctk.StringVar()
        self._menu_row(f, "Audio codec", self._audio_codec_var, list(_AUDIO_CODEC_MAP.keys()))

        # Audio bitrate
        self._audio_var = ctk.StringVar()
        self._menu_row(f, "Bitrate (kbps)", self._audio_var, _AUDIO_KBPS)

        ctk.CTkFrame(f, height=1, fg_color=COLORS["border"]).pack(fill="x", pady=6)
        _lbl(f, "ADVANCED", size=10, weight="bold", color=COLORS["text_muted"]).pack(anchor="w", pady=(0, 6))

        # HW accel
        self._hw_var = ctk.BooleanVar(value=True)
        ctk.CTkSwitch(
            f, text="  Hardware acceleration",
            variable=self._hw_var,
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
            button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent_dim"],
        ).pack(anchor="w", pady=(0, 6))

        self._target_mode_var = ctk.StringVar(value="auto")
        self._menu_row(f, "Target mode", self._target_mode_var, _TARGET_MODES)

        self._exact_audio_policy_var = ctk.StringVar(value="reduce")
        self._menu_row(f, "Exact audio", self._exact_audio_policy_var, _EXACT_AUDIO_POLICIES)

        self._exact_two_pass_var = ctk.BooleanVar(value=False)
        ctk.CTkSwitch(
            f,
            text="  Two-pass exact sizing",
            variable=self._exact_two_pass_var,
            font=ctk.CTkFont(size=12), text_color=COLORS["text_dim"],
            button_color=COLORS["accent"], button_hover_color=COLORS["accent_hover"],
            progress_color=COLORS["accent_dim"],
        ).pack(anchor="w", pady=(0, 6))

        self._resource_governor_var = ctk.StringVar(value="auto")
        self._menu_row(f, "Resource use", self._resource_governor_var, _RESOURCE_GOVERNORS)

    def _menu_row(self, parent, label: str, var: ctk.StringVar, values: list, return_menu: bool = False):
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=(0, 10))
        _lbl(row, label, size=12, color=COLORS["text_dim"], width=100).pack(side="left")
        menu = ctk.CTkOptionMenu(
            row, variable=var, values=values,
            width=150, height=28, corner_radius=7,
            fg_color=COLORS["surface_raised"], button_color=COLORS["border"],
            button_hover_color=COLORS["accent"],
            text_color=COLORS["text"], font=ctk.CTkFont(size=12),
        )
        menu.pack(side="left")
        if return_menu:
            return menu

    def _on_crf(self, val):
        crf = int(round(float(val)))
        tier = _crf_tier(crf)
        self._crf_lbl.configure(text=f"{crf} — {tier}")

    def _on_codec_changed(self):
        """Update preset, tune, rate control, and CRF range when codec changes."""
        codec = _CODEC_MAP.get(self._codec_var.get())
        if not codec:
            return
        encoder = codec.ffmpeg_encoder
        reg = ENCODER_REGISTRY.get(encoder, {})

        # Update presets
        presets = reg.get("presets", _PRESETS)
        if presets and self._preset_menu:
            self._preset_menu.configure(values=presets)
            if self._preset_var.get() not in presets:
                self._preset_var.set(reg.get("default_preset", presets[0]))

        # Update tunes
        tunes = ["(none)"] + reg.get("tunes", [])
        if self._tune_menu:
            self._tune_menu.configure(values=tunes)
            if self._tune_var.get() not in tunes:
                self._tune_var.set("(none)")

        # Update rate controls
        rc_list = reg.get("rate_controls", [RateControl.CRF])
        rc_labels = [k for k, v in _RATE_CONTROL_MAP.items() if v in rc_list]
        if rc_labels and self._rc_menu:
            self._rc_menu.configure(values=rc_labels)
            current_rc = _RATE_CONTROL_MAP.get(self._rate_control_var.get())
            if current_rc not in rc_list:
                self._rate_control_var.set(rc_labels[0])

        # Update CRF range
        crf_range = reg.get("crf_range", (0, 51))
        self._crf_slider.configure(from_=crf_range[0], to=crf_range[1], number_of_steps=crf_range[1] - crf_range[0])
        current_crf = int(round(self._crf_slider.get()))
        if current_crf > crf_range[1]:
            self._crf_slider.set(crf_range[1])
            self._on_crf(crf_range[1])

    # ── data ↔ form ──────────────────────────────────────────────

    def _load_profile(self, name: str):
        p = self._pm.get_profile(name)
        if not p:
            return
        is_builtin = p.profile_type in _BUILTIN_TYPES

        self._name_var.set(p.name)
        self._desc_var.set(p.description)
        self._codec_var.set(_CODEC_REVERSE.get(p.video_codec, "HEVC (H.265)"))
        self._on_codec_changed()  # update presets/tunes/rate controls for this codec
        self._preset_var.set(p.preset or "medium")
        self._tune_var.set(p.tune if p.tune else "(none)")
        rc_enum = RateControl(p.rate_control) if p.rate_control else RateControl.CRF
        self._rate_control_var.set(_RATE_CONTROL_REVERSE.get(rc_enum, "CRF (Constant Rate Factor)"))
        self._crf_slider.set(p.crf)
        self._on_crf(p.crf)
        self._container_var.set(p.video_container or "mp4")
        self._res_var.set(_RES_REVERSE.get(p.max_resolution, "No limit"))
        self._fps_var.set(_FPS_REVERSE.get(p.frame_rate, "No limit"))
        am_enum = AudioMode(p.audio_mode) if p.audio_mode else AudioMode.AUTO
        self._audio_mode_var.set(_AUDIO_MODE_REVERSE.get(am_enum, "Auto"))
        ac_enum = p.audio_codec if isinstance(p.audio_codec, AudioCodec) else AudioCodec.AAC
        self._audio_codec_var.set(_AUDIO_CODEC_REVERSE.get(ac_enum, "AAC"))
        self._audio_var.set(str(p.audio_bitrate // 1000) if str(p.audio_bitrate // 1000) in _AUDIO_KBPS else "192")
        self._hw_var.set(p.use_hw_accel)
        self._target_mode_var.set(p.target_size_mode or "auto")
        self._exact_audio_policy_var.set(getattr(p, "exact_audio_policy", "reduce") or "reduce")
        self._exact_two_pass_var.set(bool(getattr(p, "exact_two_pass", False)))
        self._resource_governor_var.set(p.resource_governor or "auto")

        # Lock built-in fields
        state = "disabled" if is_builtin else "normal"
        self._name_entry.configure(state=state)
        self._desc_entry.configure(state=state)

    def _form_to_profile(self) -> CompressionProfile:
        crf = int(round(self._crf_slider.get()))
        tune_val = self._tune_var.get()
        rc = _RATE_CONTROL_MAP.get(self._rate_control_var.get(), RateControl.CRF)
        am = _AUDIO_MODE_MAP.get(self._audio_mode_var.get(), AudioMode.AUTO)
        ac = _AUDIO_CODEC_MAP.get(self._audio_codec_var.get(), AudioCodec.AAC)
        return CompressionProfile(
            name=self._name_var.get().strip(),
            profile_type=ProfileType.CUSTOM,
            description=self._desc_var.get().strip(),
            video_codec=_CODEC_MAP.get(self._codec_var.get(), VideoCodec.HEVC),
            crf=crf,
            preset=self._preset_var.get(),
            video_container=self._container_var.get(),
            max_resolution=_RES_MAP.get(self._res_var.get()),
            frame_rate=_FPS_MAP.get(self._fps_var.get()),
            audio_codec=ac,
            audio_bitrate=int(self._audio_var.get()) * 1000,
            use_hw_accel=self._hw_var.get(),
            target_size_mode=self._target_mode_var.get(),
            exact_audio_policy=self._exact_audio_policy_var.get(),
            exact_two_pass=self._exact_two_pass_var.get(),
            resource_governor=self._resource_governor_var.get(),
            rate_control=rc.value,
            audio_mode=am.value,
            tune=tune_val if tune_val != "(none)" else None,
            created_by="user",
        )

    # ── actions ──────────────────────────────────────────────────

    def _new(self):
        name = "New Profile"
        idx = 1
        existing = {p.name for p in self._pm.get_all_profiles()}
        while name in existing:
            idx += 1
            name = f"New Profile {idx}"

        p = CompressionProfile(
            name=name,
            profile_type=ProfileType.CUSTOM,
            description="Custom profile",
            created_by="user",
        )
        self._pm.save_profile(p)
        self._populate_list()
        self._select(name)
        self._dirty = True

    def _duplicate(self):
        if not self._selected_name:
            return
        src = self._pm.get_profile(self._selected_name)
        if not src:
            return

        existing = {p.name for p in self._pm.get_all_profiles()}
        new_name = f"{src.name} (copy)"
        idx = 1
        while new_name in existing:
            idx += 1
            new_name = f"{src.name} (copy {idx})"

        data = src.to_dict()
        data["name"] = new_name
        data["profile_type"] = ProfileType.CUSTOM.value
        data["created_by"] = "user"
        dup = CompressionProfile.from_dict(data)
        self._pm.save_profile(dup)
        self._populate_list()
        self._select(new_name)
        self._dirty = True

    def _delete(self):
        if not self._selected_name:
            return
        p = self._pm.get_profile(self._selected_name)
        if not p or p.profile_type in _BUILTIN_TYPES:
            return
        self._pm.delete_profile(self._selected_name)
        self._populate_list()
        profiles = self._pm.get_all_profiles()
        if profiles:
            self._select(profiles[0].name)
        self._dirty = True

    def _save(self):
        if not self._selected_name:
            return
        old = self._pm.get_profile(self._selected_name)
        if old and old.profile_type in _BUILTIN_TYPES:
            return  # cannot overwrite built-in

        profile = self._form_to_profile()
        if not profile.name:
            return

        # If name changed, remove old file
        if self._selected_name != profile.name:
            self._pm.delete_profile(self._selected_name)

        self._pm.save_profile(profile)
        self._populate_list()
        self._select(profile.name)
        self._dirty = True

    def _close(self):
        if self._dirty and self._on_close:
            self._on_close()
        self.destroy()
