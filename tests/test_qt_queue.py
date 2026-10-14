"""Queue behaviour of the Qt window: clips become jobs, files survive a run, row actions."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from video_compressor.core.clips import Clip, ClipMode, ClipPlan
from video_compressor.core.profiles import CompressionProfile, build_quick_compress_profiles
from video_compressor.gui.qt_shell.encode_form import EncodeForm, output_path_for
from video_compressor.gui.qt_shell.rows import clip_summary, clock, describe_media


class _FakeCompressor:
    """Records compress_async calls; never runs ffmpeg."""

    codec_manager = SimpleNamespace(
        get_compatible_container=lambda codec, container: SimpleNamespace(value="mp4")
    )

    def __init__(self, *args, **kwargs):
        self.calls: list[dict] = []
        self.cancelled: list[str] = []
        self.compare_calls: list[tuple] = []
        self.compare_error: str = ""
        self.plan_durations: list[float] = []

    def set_progress_callback(self, callback):
        self.progress = callback

    def set_software_fallback_callback(self, callback):
        self.fallback = callback

    def analyze_media(self, path):
        return None

    def compress_async(self, **kwargs):
        self.calls.append(kwargs)
        return f"job-{len(self.calls)}"

    def get_job(self, job_id):
        return None

    def queue_slots(self, *args, **kwargs):
        return 8

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)

    def get_active_jobs(self):
        return []

    def _select_hw_encoder(self, codec):
        return None

    def _resolve_target_size_plan(self, profile, info):
        self.plan_durations.append(info.duration)
        return None

    def compare_sample(self, input_file, output_dir, profiles, sample_seconds=20.0):
        self.compare_calls.append((input_file, Path(output_dir), [p.name for p in profiles]))
        if self.compare_error:
            raise ValueError(self.compare_error)
        gate = getattr(self, "compare_gate", None)
        if gate is not None:
            gate.wait(10)
            if getattr(self, "compare_cancelled", False):
                return [
                    SimpleNamespace(success=False, skipped=False, cancelled=True, input_file=input_file,
                                    output_file=None, note="", error_message="")
                ]
        return [
            SimpleNamespace(
                success=True,
                skipped=False,
                cancelled=False,
                kept_original=False,
                input_file=input_file,
                output_file=Path(output_dir) / f"sample{i}.mp4",
                original_size=1000,
                compressed_size=400 + i * 100,
                reduction_percent=50.0,
                encoding_time=1.0,
                encoder_name="",
                note="Sample compare",
                error_message="",
                video_codec="h264",
            )
            for i, _ in enumerate(profiles)
        ]


@pytest.fixture
def quiet_config(monkeypatch):
    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.update_config",
        lambda self, **kwargs: None,
    )
    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.save_config",
        lambda self: None,
    )


@pytest.fixture
def window(monkeypatch, quiet_config):
    from PySide6.QtWidgets import QApplication

    from video_compressor.gui.qt_shell import main_window as mw

    monkeypatch.setattr("video_compressor.core.compressor.VideoCompressor", _FakeCompressor)
    monkeypatch.setattr(mw.SquishItWindow, "_start_hw_probe", lambda self: None)
    app = QApplication.instance() or QApplication([])
    win = mw.SquishItWindow()
    # No real history file, no desktop toast, no Explorer in tests.
    win._history = SimpleNamespace(add_entry=lambda entry: None)
    win._notify_done = lambda ok, total: None
    opened: list = []
    monkeypatch.setattr(mw, "reveal_in_folder", lambda path: opened.append(path))
    win._test_opened = opened
    win.select_quick("Quick Lite")
    try:
        yield win, app
    finally:
        win.close()
        app.processEvents()


def _add(win, app, tmp_path: Path, name: str = "movie.mp4") -> str:
    path = tmp_path / name
    path.write_bytes(b"not-a-real-video")
    win.add_paths([str(path)])
    app.processEvents()
    return str(path.resolve())


def _result(call: dict, ok: bool = True):
    out = Path(call["output_file"])
    return SimpleNamespace(
        success=ok,
        skipped=False,
        cancelled=False,
        kept_original=False,
        input_file=call["input_file"],
        output_file=out,
        original_size=1000,
        compressed_size=400,
        reduction_percent=60.0,
        encoding_time=2.0,
        encoder_name="libx264",
        video_codec="h264",
        note="",
        error_message="",
    )


def _finish_all(win, app):
    compressor = win._compressor
    for call in list(compressor.calls):
        win._queue.put(("result", _result(call)))
    win._pump()
    app.processEvents()


# ── clips become jobs ────────────────────────────────────────────────────────


def test_separate_clips_start_one_job_per_clip(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    plan = ClipPlan((Clip(1, 3), Clip(5, 6.5, "Intro"), Clip(8, 9)), ClipMode.SEPARATE)
    win.set_clips(key, plan)
    win.start_compression()
    app.processEvents()
    calls = win._compressor.calls
    assert len(calls) == 3
    names = [Path(c["output_file"]).name for c in calls]
    assert names == [
        "movie_compressed_clip01.mp4",
        "movie_compressed_Intro.mp4",
        "movie_compressed_clip03.mp4",
    ]
    assert len(set(names)) == 3
    for call, clip in zip(calls, plan.clips):
        assert call["clips"].clips == (clip,)
        assert call["overwrite"] is False


def test_join_clips_start_a_single_job(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    plan = ClipPlan((Clip(1, 3), Clip(5, 6.5)), ClipMode.JOIN)
    win.set_clips(key, plan)
    win.start_compression()
    calls = win._compressor.calls
    assert len(calls) == 1
    assert calls[0]["clips"] == plan
    assert Path(calls[0]["output_file"]).name == "movie_compressed.mp4"


def test_clips_without_sound_keep_that_choice_in_every_job(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    plan = ClipPlan((Clip(1, 3), Clip(5, 6.5)), ClipMode.SEPARATE, keep_audio=False)
    win.set_clips(key, plan)
    assert win._files[key]["row"]._badge.text() == "2 clips · saved separately · 0:04 · no sound"
    win.start_compression()
    calls = win._compressor.calls
    assert [call["clips"].keep_audio for call in calls] == [False, False]


def test_the_whole_video_without_sound_reads_as_such_on_the_row(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win._files[key]["info"] = SimpleNamespace(video_codec="h264", size=1000, duration=60.0)
    win.set_clips(key, ClipPlan((Clip(0, 60),), keep_audio=False))
    assert win._files[key]["row"]._badge.text() == "Whole video · no sound"
    win.start_compression()
    assert win._compressor.calls[0]["clips"].keep_audio is False


def test_without_clips_nothing_extra_is_passed(window, tmp_path):
    win, app = window
    _add(win, app, tmp_path)
    win.start_compression()
    call = win._compressor.calls[0]
    assert "clips" not in call
    assert call["overwrite"] is False
    assert isinstance(call["profile"], CompressionProfile)
    assert call["profile"].trim_enabled is False


def test_saved_profile_trim_no_longer_cuts_every_file():
    form = EncodeForm()
    base = build_quick_compress_profiles()["Quick Lite"]
    base.trim_enabled = True
    base.trim_start, base.trim_end = 5.0, 10.0
    form.apply_profile(base, quick_name="Quick Lite")
    built = form.build_profile("Fast", base)
    assert built.trim_enabled is False and built.trim_start is None


# ── files survive a run ──────────────────────────────────────────────────────


def test_files_and_clips_survive_a_finished_run(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    plan = ClipPlan((Clip(1, 3), Clip(5, 6)), ClipMode.SEPARATE)
    win.set_clips(key, plan)
    win.start_compression()
    assert win._running is True
    assert win._files[key]["row"]._busy is True
    _finish_all(win, app)
    assert win._running is False
    assert key in win._files and win._files[key]["clips"] == plan
    assert win._files[key]["row"]._busy is False
    assert win._results_box.count() == 3  # header + two results
    assert win._activity.count() == 0
    assert win._test_opened == []  # Explorer is not opened unasked
    # and the same files can run again
    win.start_compression()
    assert len(win._compressor.calls) == 4


def test_open_folder_when_done_is_opt_in(window, tmp_path, monkeypatch):
    win, app = window
    monkeypatch.setattr(
        "video_compressor.gui.qt_shell.main_window.get_config",
        lambda: SimpleNamespace(
            open_folder_when_done=True,
            notify_on_completion=False,
            last_output_folder="",
            batch_queue_order="manual",
        ),
    )
    _add(win, app, tmp_path)
    win.start_compression()
    _finish_all(win, app)
    assert len(win._test_opened) == 1


def test_clear_queue_removes_everything(window, tmp_path):
    win, app = window
    _add(win, app, tmp_path)
    win.start_compression()
    _finish_all(win, app)
    win.clear_queue()
    assert not win._files
    assert win._results_box.count() == 0 and win._rows.count() == 0


# ── row actions ──────────────────────────────────────────────────────────────


def test_move_to_top_reorders_files_and_rows(window, tmp_path):
    win, app = window
    first = _add(win, app, tmp_path, "a.mp4")
    second = _add(win, app, tmp_path, "b.mp4")
    third = _add(win, app, tmp_path, "c.mp4")
    assert list(win._files) == [first, second, third]
    win.move_to_top(Path(third))
    assert list(win._files) == [third, first, second]
    assert win._rows.itemAt(0).widget() is win._files[third]["row"]


def test_compress_only_runs_just_that_file(window, tmp_path):
    win, app = window
    _add(win, app, tmp_path, "a.mp4")
    second = _add(win, app, tmp_path, "b.mp4")
    win.compress_only(Path(second))
    calls = win._compressor.calls
    assert [Path(c["input_file"]).name for c in calls] == ["b.mp4"]


def test_row_actions_are_ignored_while_running(window, tmp_path):
    win, app = window
    first = _add(win, app, tmp_path, "a.mp4")
    second = _add(win, app, tmp_path, "b.mp4")
    win.start_compression()
    win.move_to_top(Path(second))
    win.remove_file(Path(first))
    win.clear_trim(Path(first))
    assert list(win._files) == [first, second]


def test_trim_button_waits_for_a_readable_video(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    row = win._files[key]["row"]
    assert row.can_trim is False
    info = SimpleNamespace(
        video_codec="h264", duration=60.0, width=1280, height=720, size=5_000_000
    )
    win._queue.put(("info", key, info))
    win._pump()
    assert row.can_trim is True
    row.set_clip_summary("3 clips · saved separately · 0:20")
    assert row._badge.isHidden() is False
    win.start_compression()
    assert row.can_trim is False  # locked while running


def test_unreadable_file_cannot_be_trimmed(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, None))
    win._pump()
    row = win._files[key]["row"]
    assert row.can_trim is False
    assert "Can't read" in row._meta.text()


def test_late_info_for_a_removed_row_is_harmless(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win.remove_file(Path(key))
    win._queue.put(("info", key, None))
    win._pump()  # must not raise


# ── run order and settings ───────────────────────────────────────────────────


def test_queue_order_sorts_by_size_and_length(window, tmp_path):
    win, app = window
    small = _add(win, app, tmp_path, "small.mp4")
    big = _add(win, app, tmp_path, "big.mp4")
    mid = _add(win, app, tmp_path, "mid.mp4")
    for key, size, length in ((small, 10, 30.0), (big, 900, 5.0), (mid, 100, 60.0)):
        win._files[key]["info"] = SimpleNamespace(video_codec="h264", size=size, duration=length)
    win.form.queue_order = "manual"
    assert win._ordered_keys() == [small, big, mid]
    win.form.queue_order = "largest-first"
    assert win._ordered_keys() == [big, mid, small]
    win.form.queue_order = "smallest-first"
    assert win._ordered_keys() == [small, mid, big]
    win.form.queue_order = "longest-first"
    assert win._ordered_keys() == [mid, small, big]
    win.form.queue_order = "shortest-first"
    assert win._ordered_keys() == [big, small, mid]


def test_extra_settings_reach_the_profile(window):
    win, app = window
    pane = win._settings
    pane._audio_kbps.setCurrentIndex(pane._audio_kbps.findData(192))
    pane._audio_handling.setCurrentIndex(pane._audio_handling.findData("strip"))
    pane._threads.setCurrentIndex(pane._threads.findText("4"))
    pane._image_format.setCurrentIndex(pane._image_format.findText("png"))
    pane._extra_args.setText("-tune film")
    pane._extra_args.editingFinished.emit()
    pane._output_type.setCurrentIndex(pane._output_type.findData("audio"))
    pane._queue_order.setCurrentIndex(pane._queue_order.findData("largest-first"))
    profile = win.job_profile()
    assert profile.audio_bitrate == 192_000
    assert profile.audio_mode == "strip"
    assert profile.threads == 4
    assert profile.image_format == "png"
    assert profile.extra_ffmpeg_args == "-tune film"
    assert profile.output_mode == "audio"
    assert win.form.queue_order == "largest-first"
    # and back: loading a form shows those values again
    pane.load(win.form)
    assert pane._audio_kbps.currentData() == 192
    assert pane._threads.currentText() == "4"
    assert pane._extra_args.text() == "-tune film"


def test_saved_output_folder_is_remembered(monkeypatch, tmp_path):
    from video_compressor.gui.qt_shell import main_window as mw

    monkeypatch.setattr(
        mw, "get_config", lambda: SimpleNamespace(last_output_folder=str(tmp_path))
    )
    assert mw.SquishItWindow._saved_output_folder() == tmp_path
    monkeypatch.setattr(
        mw, "get_config", lambda: SimpleNamespace(last_output_folder=str(tmp_path / "gone"))
    )
    assert mw.SquishItWindow._saved_output_folder() is None


def test_closing_during_a_run_cancels_the_jobs(window, tmp_path):
    win, app = window
    _add(win, app, tmp_path)
    win.start_compression()
    compressor = win._compressor
    win.close()
    assert compressor.cancelled == ["job-1"]


# ── naming and badges ────────────────────────────────────────────────────────


def _profile():
    return build_quick_compress_profiles()["Quick Lite"]


def test_clip_token_is_appended_when_template_has_no_field(tmp_path):
    path = output_path_for(Path("C:/v/trip.mov"), _profile(), tmp_path, "{name}_small", "clip02")
    assert path.name.startswith("trip_small_clip02.")


def test_clip_field_places_the_token(tmp_path):
    path = output_path_for(Path("C:/v/trip.mov"), _profile(), tmp_path, "{clip}-{name}", "Intro")
    assert path.name.startswith("Intro-trip.")


def test_clip_field_without_a_clip_leaves_no_stray_separators(tmp_path):
    path = output_path_for(Path("C:/v/trip.mov"), _profile(), tmp_path, "{name}_{clip}_small", "")
    assert path.name.startswith("trip_small.")
    only = output_path_for(Path("C:/v/trip.mov"), _profile(), tmp_path, "{name}_{clip}", "")
    assert only.name.startswith("trip.")


def test_no_token_leaves_the_name_alone(tmp_path):
    path = output_path_for(Path("C:/v/trip.mov"), _profile(), tmp_path, "{name}_compressed")
    assert path.name.startswith("trip_compressed.")


def test_clip_summary_wording():
    assert clip_summary(None) == ""
    assert clip_summary(ClipPlan((Clip(10, 52),))) == "Trimmed to 0:42"
    three = ClipPlan((Clip(0, 20), Clip(30, 50), Clip(60, 103)), ClipMode.SEPARATE)
    assert clip_summary(three) == "3 clips \u00b7 saved separately \u00b7 1:23"
    joined = ClipPlan(three.clips, ClipMode.JOIN)
    assert clip_summary(joined) == "3 clips joined \u00b7 1:23"


def test_clip_summary_says_when_the_sound_is_left_out():
    one = ClipPlan((Clip(10, 52),), keep_audio=False)
    assert clip_summary(one) == "Trimmed to 0:42 \u00b7 no sound"
    assert clip_summary(one, 120.0) == "Trimmed to 0:42 \u00b7 no sound"
    whole = ClipPlan((Clip(0, 119.98),), keep_audio=False)
    assert clip_summary(whole, 120.0) == "Whole video \u00b7 no sound"
    assert clip_summary(whole) == "Trimmed to 2:00 \u00b7 no sound"  # length unknown
    three = ClipPlan((Clip(0, 20), Clip(30, 50), Clip(60, 103)), ClipMode.JOIN, keep_audio=False)
    assert clip_summary(three) == "3 clips joined \u00b7 1:23 \u00b7 no sound"


def test_clock_and_media_description():
    assert clock(42) == "0:42"
    assert clock(3725) == "1:02:05"
    info = SimpleNamespace(
        video_codec="h264", duration=83, width=1920, height=1080, size=48_000_000
    )
    text = describe_media(info)
    assert text.startswith("1:23 \u00b7 1920\u00d71080 \u00b7 ")
    assert text.endswith("h264")


# ── compare sample and size preview ──────────────────────────────────────────


def _video_info(duration=60.0):
    from video_compressor.core.compressor import VideoInfo

    return VideoInfo(
        filepath=Path("movie.mp4"),
        duration=duration,
        size=60_000_000,
        width=1280,
        height=720,
        fps=30.0,
        avg_fps=30.0,
        video_codec="h264",
        audio_codec="aac",
        frame_count=int(duration * 30),
    )


def _wait_until(app, predicate, seconds=5.0):
    import time

    deadline = time.time() + seconds
    while time.time() < deadline:
        win_pump = getattr(predicate, "pump", None)
        if win_pump:
            win_pump()
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_compare_sample_encodes_three_samples_and_lists_them(window, tmp_path):
    win, app = window
    recorded = []
    win._history = SimpleNamespace(add_entry=lambda entry: recorded.append(entry))
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info()))
    win._pump()
    win.compare_sample(Path(key))
    assert win._running is True and win._compare_btn.isEnabled() is False

    def done():
        win._pump()
        return not win._running

    assert _wait_until(app, done)
    calls = win._compressor.compare_calls
    # one sample per call, so Cancel can stop between samples
    assert len(calls) == 3
    assert {Path(c[0]) for c in calls} == {Path(key)}
    assert {c[1].name for c in calls} == {"_compare_samples"}
    assert all(len(c[2]) == 1 for c in calls)
    assert "Smallest" in win._queue_label.text()
    assert win._results_box.count() == 4  # header + three samples
    assert recorded == []  # samples never count toward the savings history
    assert win._compare_btn.isEnabled() is True
    assert win._files[key]["row"]._busy is False


def test_compare_sample_needs_a_readable_video(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win.compare_sample(Path(key))  # still analysing / unreadable
    assert win._running is False
    assert win._compressor is None or not win._compressor.compare_calls
    win._compare_first()
    assert "Add a video first" in win._queue_label.text()


def test_compare_sample_failure_is_explained(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info()))
    win._pump()
    win._get_compressor().compare_error = "disk is full"
    win.compare_sample(Path(key))

    def done():
        win._pump()
        return not win._running

    assert _wait_until(app, done)
    assert "disk is full" in win._queue_label.text()
    assert win._results_box.count() == 0


def test_compare_header_button_uses_the_first_video(window, tmp_path):
    win, app = window
    first = _add(win, app, tmp_path, "a.mp4")
    second = _add(win, app, tmp_path, "b.mp4")
    win._queue.put(("info", first, None))
    win._queue.put(("info", second, _video_info()))
    win._pump()
    win._compare_first()

    def done():
        win._pump()
        return not win._running

    assert _wait_until(app, done)
    assert Path(win._compressor.compare_calls[0][0]).name == "b.mp4"


def test_size_preview_is_judged_against_the_clip_not_the_whole_video(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info(60.0)))
    win._pump()
    win.form.target_size_enabled = True
    win.form.target_size_mb = 8
    win._refresh_preview()
    compressor = win._get_compressor()
    assert compressor.plan_durations[-1] == pytest.approx(60.0)
    win.set_clips(key, ClipPlan((Clip(10, 22), Clip(30, 45)), ClipMode.SEPARATE))
    assert compressor.plan_durations[-1] == pytest.approx(12.0)  # the first clip
    win.set_clips(key, ClipPlan((Clip(10, 22), Clip(30, 45)), ClipMode.JOIN))
    assert compressor.plan_durations[-1] == pytest.approx(27.0)  # the joined total


# ── fixes found in review ────────────────────────────────────────────────────


def test_cancelling_a_comparison_waits_for_the_worker_and_never_ends_a_later_run(window, tmp_path):
    import threading

    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info()))
    win._pump()
    compressor = win._get_compressor()
    compressor.compare_gate = threading.Event()
    win.compare_sample(Path(key))
    assert _wait_until(app, lambda: bool(compressor.compare_calls), 5)  # first sample is "encoding"

    win.cancel_all()
    assert win._running is True and win._compare_mode is True  # not finished early
    assert win._cancel_btn.isEnabled() is False
    assert "Stopping" in win._queue_label.text()
    win.start_compression()  # must be refused while the comparison is still stopping
    assert not compressor.calls

    compressor.compare_cancelled = True
    compressor.compare_gate.set()

    def done():
        win._pump()
        return not win._running

    assert _wait_until(app, done)
    assert len(compressor.compare_calls) == 1  # the remaining samples never started
    assert win._queue_label.text() == "Comparison cancelled."
    assert win._results_box.count() == 0
    win.start_compression()  # and a normal run works afterwards
    assert len(compressor.calls) == 1


def test_cancel_repeats_until_a_sample_that_was_still_starting_is_stopped(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info()))
    win._pump()
    compressor = win._get_compressor()
    cancelled = []
    compressor.get_active_jobs = lambda: [SimpleNamespace(id="late-sample")] if len(cancelled) < 1 else []
    compressor.cancel_job = lambda job_id: cancelled.append(job_id)
    win._compare_mode = True
    win._set_running(True)
    win.cancel_all()
    assert cancelled == ["late-sample"]
    assert win._compare_cancel.is_set()


def test_output_folder_can_be_reset_to_next_to_the_originals(window, tmp_path):
    win, app = window
    win._output_folder = tmp_path
    win._refresh_output_button()
    assert win._output_btn.text() == f"Output: {tmp_path.name}"
    win.reset_output_folder()
    assert win._output_folder is None
    assert win._output_btn.text() == "Output: next to source"


def test_dialogs_are_freed_after_they_close(window, tmp_path, monkeypatch):
    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info()))
    win._pump()
    freed = []

    class FakeEditor:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            return 0

        def result_plan(self):
            return None

        def deleteLater(self):
            freed.append(1)

    monkeypatch.setattr("video_compressor.gui.qt_shell.trim_editor.TrimEditorDialog", FakeEditor)
    win.open_trim_editor(Path(key))
    assert freed == [1]

    class FakeDialog(FakeEditor):
        pass

    win._run_dialog(FakeDialog())
    assert freed == [1, 1]


def test_a_video_too_short_to_trim_cannot_be_trimmed(window, tmp_path):
    win, app = window
    key = _add(win, app, tmp_path)
    win._queue.put(("info", key, _video_info(0.2)))
    win._pump()
    assert win._files[key]["row"].can_trim is False
    win.open_trim_editor(Path(key))
    assert "too short" in win._queue_label.text()


def test_ctrl_enter_takes_the_text_being_typed_before_it_starts(window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication

    win, app = window
    _add(win, app, tmp_path)
    box = win._settings._extra_args
    box.setText("-tune film")
    # An offscreen window gets no real keyboard focus, so stand in for it: the box
    # "has focus", and losing focus makes it commit exactly as a real one does.
    monkeypatch.setattr(QApplication, "focusWidget", staticmethod(lambda: box))
    monkeypatch.setattr(box, "clearFocus", lambda: box.editingFinished.emit())
    win._start_from_shortcut()
    assert win._compressor.calls[0]["profile"].extra_ffmpeg_args == "-tune film"


def test_clicking_into_and_out_of_the_ffmpeg_box_keeps_the_quick_preset(window):
    win, app = window
    assert win.form.quick_name == "Quick Lite"
    win._settings._extra_args.editingFinished.emit()  # focus in and out, nothing typed
    assert win.form.quick_name == "Quick Lite"
    assert win.job_profile().name == "Quick Lite"
    win._settings._extra_args.setText("-tune film")
    win._settings._extra_args.editingFinished.emit()  # a real edit still counts
    assert win.form.quick_name is None


def test_settings_the_lists_do_not_know_are_shown_not_replaced_by_the_first_entry(window):
    win, app = window
    win.form.threads = 3
    win.form.image_format = "heic"
    win._settings.load(win.form)
    assert win._settings._threads.currentText() == "3"
    assert win._settings._image_format.currentText() == "heic"
    # and "Auto" really is a choice that can be made again
    win._settings._threads.setCurrentIndex(win._settings._threads.findText("Auto"))
    assert win.form.threads is None


def test_audio_track_choice_is_off_for_audio_only_output(window):
    win, app = window
    pane = win._settings
    assert pane._audio_handling.isEnabled()
    pane._output_type.setCurrentIndex(pane._output_type.findData("audio"))
    assert not pane._audio_handling.isEnabled()
    pane._output_type.setCurrentIndex(pane._output_type.findData("video"))
    assert pane._audio_handling.isEnabled()


def test_a_template_that_cannot_be_filled_in_still_names_each_clip_apart(tmp_path):
    names = {
        output_path_for(Path("C:/v/trip.mov"), _profile(), tmp_path, "{name}_{size}", token).name
        for token in ("clip01", "clip02")
    }
    assert len(names) == 2
    assert all(n.startswith("trip_compressed_clip") for n in names)


def test_a_clip_field_does_not_eat_double_underscores_elsewhere_in_the_name(tmp_path):
    path = output_path_for(Path("C:/v/my__video.mov"), _profile(), tmp_path, "{name}_{clip}_small", "")
    assert path.name.startswith("my__video_small.")
    named = output_path_for(Path("C:/v/my__video.mov"), _profile(), tmp_path, "{name}_{clip}_small", "Intro")
    assert named.name.startswith("my__video_Intro_small.")
