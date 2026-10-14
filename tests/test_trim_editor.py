"""Trim editor dialog and timeline widget, driven offscreen with a stub preview."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from video_compressor.core.clips import MAX_JOIN_CLIPS, Clip, ClipMode, ClipPlan
from video_compressor.core.preview import waveform_buckets
from video_compressor.gui.qt_shell import trim_editor as te
from video_compressor.gui.qt_shell.timeline import RULER_H, TRACK_H, WAVE_H


class StubPreview:
    """Stands in for core.preview.PreviewService; answers synchronously."""

    def __init__(self):
        self.frames: list[tuple[float, bool]] = []
        self.filmstrips: list[int] = []
        self.playbacks: list[float] = []
        self.sound: list[dict] = []  # the sound options of each playback
        self.volumes: list[float] = []
        self.waves: list[tuple] = []
        self.wave_callbacks: list[tuple] = []
        self.wave_cancels = 0
        self.stopped = 0
        self.cancelled = 0
        self.on_frame = None
        self.on_end = None
        self.on_audio = None

    def request_frame(self, path, t, size, *, fast, callback, duration=None, fps=None):
        self.frames.append((round(t, 3), fast))
        callback(t, size[0], size[1], bytes(size[0] * size[1] * 3), fast)

    def request_filmstrip(self, path, duration, count, size, on_thumb, on_done=None):
        self.filmstrips.append(count)
        for i in range(count):
            on_thumb(i, (i + 0.5) * duration / count, size[0], size[1], bytes(size[0] * size[1] * 3))
        if on_done:
            on_done()

    def start_playback(self, path, start, size, fps_out, on_frame, on_end, duration=None, **sound):
        self.playbacks.append(start)
        self.sound.append(sound)
        self.on_frame, self.on_end = on_frame, on_end
        self.on_audio = sound.get("on_audio")

    def set_volume(self, volume):
        self.volumes.append(volume)

    def request_waveform(self, path, duration, on_peaks, on_done=None, *, buckets=None):
        self.waves.append((duration, buckets))
        self.wave_callbacks.append((on_peaks, on_done))

    def cancel_waveform(self):
        self.wave_cancels += 1

    def stop_playback(self):
        self.stopped += 1

    def cancel_all(self):
        self.cancelled += 1

    def cancel_filmstrip(self):
        self.cancelled += 1

    def cancel_frames(self):
        self.frames_cancelled = getattr(self, "frames_cancelled", 0) + 1


def _info(duration=60.0, audio=False):
    info = SimpleNamespace(
        video_codec="h264",
        duration=duration,
        fps=30.0,
        avg_fps=30.0,
        width=640,
        height=360,
        size=60_000_000,
        rotation=0,
        sar="1:1",
    )
    if audio:
        info.audio_codec = "aac"
    return info


@pytest.fixture(autouse=True)
def sound_settings(monkeypatch):
    """Default sound settings in memory; saving is recorded and never touches the real config."""

    from video_compressor.config import AppConfig

    config = AppConfig()
    saved: list = []

    class Manager:
        def update_config(self, **changes):
            saved.append(changes)
            for key, value in changes.items():
                setattr(config, key, value)

    monkeypatch.setattr(te, "get_config", lambda: config)
    monkeypatch.setattr(te, "get_config_manager", lambda: Manager())
    monkeypatch.setattr("video_compressor.config.ConfigManager.update_config", lambda self, **kw: None)
    monkeypatch.setattr("video_compressor.config.ConfigManager.save_config", lambda self: None)
    return SimpleNamespace(config=config, saved=saved)


@pytest.fixture
def editor(tmp_path):
    app = QApplication.instance() or QApplication([])
    made = []

    def make(plan=None, duration=60.0, note="", audio=False):
        preview = StubPreview()
        dialog = te.TrimEditorDialog(
            None, tmp_path / "movie.mp4", _info(duration, audio), plan, preview, target_note=note
        )
        dialog.show()
        app.processEvents()
        made.append(dialog)
        return dialog, preview, app

    yield make
    for dialog in made:
        dialog._closed or dialog.done(0)
    app.processEvents()


def _drag(timeline, x0, x1, y=None):
    y = RULER_H + 30 if y is None else y
    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x0), int(y)))
    steps = 6
    for step in range(1, steps + 1):
        x = x0 + (x1 - x0) * step / steps
        QTest.mouseMove(timeline, QPoint(int(x), int(y)))
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x1), int(y)))


# ── dialog basics ────────────────────────────────────────────────────────────


def test_opens_on_the_whole_video_and_starts_the_preview(editor):
    dialog, preview, app = editor()
    assert dialog.model.is_whole_video and dialog.model.count == 1
    assert dialog.result_plan() is None
    assert preview.filmstrips and preview.filmstrips[0] >= 6
    assert (0.0, False) in preview.frames
    assert dialog.video.has_picture
    assert "No sound" not in dialog.status.text()
    assert dialog.mode_box.isHidden()  # only one clip: no export choice needed


def test_split_delete_and_save_returns_a_plan(editor):
    dialog, preview, app = editor()
    dialog.seek(20.0)
    dialog.split()
    assert dialog.model.count == 2
    dialog.model.select(0)
    dialog.delete_clip()
    assert dialog.model.count == 1
    assert dialog.model.clips[0].start == pytest.approx(20.0, abs=0.05)
    dialog.save()
    plan = dialog.result_plan()
    assert plan is not None and plan.clips[0].end == pytest.approx(60.0, abs=0.05)
    assert dialog.result() == dialog.DialogCode.Accepted
    assert preview.cancelled >= 1  # the editor's own background work is stopped
    assert preview.stopped >= 1
    assert getattr(preview, "frames_cancelled", 0) >= 1


def test_use_whole_video_clears_the_trim(editor):
    plan = ClipPlan((Clip(5, 10), Clip(20, 30)), ClipMode.JOIN)
    dialog, preview, app = editor(plan)
    assert dialog.model.count == 2
    dialog.use_whole_video()
    assert dialog.result_plan() is None
    assert dialog.result() == dialog.DialogCode.Accepted


def test_existing_clips_are_shown_and_mode_choice_appears(editor):
    plan = ClipPlan((Clip(5, 10), Clip(20, 30)), ClipMode.JOIN)
    dialog, preview, app = editor(plan)
    assert dialog.table.rowCount() == 2
    assert not dialog.mode_box.isHidden()
    assert dialog.join_radio.isChecked() and not dialog.separate_radio.isChecked()
    dialog.separate_radio.setChecked(True)
    assert dialog.model.mode is ClipMode.SEPARATE
    dialog.join_radio.setChecked(True)
    assert dialog.model.mode is ClipMode.JOIN
    dialog.save()
    assert dialog.result_plan().mode is ClipMode.JOIN
    assert "2 clips" in dialog.summary.text()


def test_summary_mentions_a_size_target(editor):
    plan = ClipPlan((Clip(5, 15),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan, note="Target size: 25 MB per file")
    assert "Target size: 25 MB per file" in dialog.summary.text()


def test_too_many_clips_to_join_blocks_saving(editor):
    clips = tuple(Clip(i * 1.5, i * 1.5 + 1.0) for i in range(MAX_JOIN_CLIPS + 1))
    dialog, preview, app = editor(ClipPlan(clips, ClipMode.JOIN), duration=120.0)
    assert dialog.save_btn.isEnabled() is False
    dialog.separate_radio.setChecked(True)
    assert dialog.save_btn.isEnabled() is True


def test_cancel_asks_before_discarding_changes(editor, monkeypatch):
    dialog, preview, app = editor()
    dialog.seek(30.0)
    dialog.split()
    answers = []

    def fake_question(*args, **kwargs):
        answers.append(args[1])
        return QMessageBox.StandardButton.No

    monkeypatch.setattr(QMessageBox, "question", staticmethod(fake_question))
    dialog.reject()
    assert answers and dialog.isVisible()
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    )
    dialog.reject()
    assert not dialog.isVisible()
    assert dialog.result_plan() is None


def test_cancel_without_changes_closes_quietly(editor, monkeypatch):
    dialog, preview, app = editor()
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: pytest.fail("should not ask"))
    )
    dialog.reject()
    assert not dialog.isVisible()


# ── editing actions ──────────────────────────────────────────────────────────


def test_set_start_and_end_follow_the_playhead(editor):
    dialog, preview, app = editor()
    dialog.seek(10.0)
    dialog.set_in()
    dialog.seek(40.0)
    dialog.set_out()
    clip = dialog.model.clips[0]
    assert (clip.start, clip.end) == (pytest.approx(10.0, abs=0.05), pytest.approx(40.0, abs=0.05))


def test_add_clip_needs_free_space(editor):
    dialog, preview, app = editor()
    dialog.add_clip()  # the whole video is already one clip
    assert dialog.model.count == 1
    assert "no free space" in dialog.status.text().lower()
    dialog.seek(10.0)
    dialog.set_out()  # shrink to 0..10
    dialog.seek(30.0)
    dialog.add_clip()
    assert dialog.model.count == 2
    assert dialog.model.clips[-1].start == pytest.approx(30.0, abs=0.05)


def test_undo_and_redo_buttons_track_the_model(editor):
    dialog, preview, app = editor()
    assert not dialog.undo_btn.isEnabled()
    dialog.seek(20.0)
    dialog.split()
    assert dialog.undo_btn.isEnabled()
    dialog.undo()
    assert dialog.model.count == 1 and dialog.redo_btn.isEnabled()
    dialog.redo()
    assert dialog.model.count == 2


def test_table_edit_changes_the_clip_and_bad_text_is_refused(editor):
    plan = ClipPlan((Clip(5, 15), Clip(30, 40)), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    dialog.table.item(0, 1).setText("0:08")
    assert dialog.model.clips[0].start == pytest.approx(8.0)
    dialog.table.item(1, 0).setText("Goal")
    assert dialog.model.clips[1].label == "Goal"
    dialog.table.item(0, 2).setText("banana")
    app.processEvents()
    assert dialog.model.clips[0].end == pytest.approx(15.0)
    assert "not a time" in dialog.status.text()
    assert dialog.table.item(0, 2).text() == "00:15.000"  # reverted


def test_move_clip_reorders_for_joining(editor):
    plan = ClipPlan((Clip(5, 10), Clip(20, 30)), ClipMode.JOIN)
    dialog, preview, app = editor(plan)
    dialog.model.select(1)
    dialog.move_clip(-1)
    assert dialog.model.clips[0].start == pytest.approx(20.0)


# ── playhead, preview and keys ───────────────────────────────────────────────


def test_frame_stepping_uses_the_video_frame_rate(editor):
    dialog, preview, app = editor()
    dialog.step_frames(3)
    assert dialog._t == pytest.approx(3 / 30.0)
    dialog.step_seconds(1)
    assert dialog._t == pytest.approx(1 + 3 / 30.0)
    assert preview.frames[-1][1] is False  # a settled step shows the exact frame
    dialog.seek(-5)
    assert dialog._t == 0.0
    dialog.seek(999)
    assert dialog._t == pytest.approx(60.0)


def test_typing_a_time_jumps_there(editor):
    dialog, preview, app = editor()
    dialog.time_field.setText("0:45")
    dialog.time_field.returnPressed.emit()
    assert dialog._t == pytest.approx(45.0)
    dialog.time_field.setText("1:05")
    dialog.time_field.returnPressed.emit()
    assert dialog._t == pytest.approx(45.0)  # stays put
    assert "only 01:00.000 long" in dialog.status.text()
    dialog.time_field.setText("nonsense")
    dialog.time_field.returnPressed.emit()
    assert "not a time" in dialog.status.text()


def test_scrubbing_shows_a_quick_frame_then_the_exact_one(editor):
    dialog, preview, app = editor()
    preview.frames.clear()
    dialog.timeline.scrubbing.emit(12.0)
    assert preview.frames == [(12.0, True)]
    QTest.qWait(te.SCRUB_SETTLE_MS + 120)
    assert preview.frames[-1] == (12.0, False)


def test_play_and_pause(editor):
    dialog, preview, app = editor()
    dialog.seek(5.0)
    dialog.toggle_play()
    assert preview.playbacks == [pytest.approx(5.0)]
    assert dialog.play_btn.text() == "Pause"
    preview.on_frame(6.0, bytes(dialog._frame_size[0] * dialog._frame_size[1] * 3))
    app.processEvents()
    assert dialog._t == pytest.approx(6.0)
    dialog.toggle_play()
    assert preview.stopped >= 1 and dialog.play_btn.text() == "Play"
    assert preview.frames[-1] == (pytest.approx(6.0), False)


def test_playback_end_resets_the_button_and_stale_ends_are_ignored(editor):
    dialog, preview, app = editor()
    dialog.toggle_play()
    session = dialog._play_id
    dialog._bridge.play_end.emit(session - 1, "eof")  # an old session
    app.processEvents()
    assert dialog._playing is True
    dialog._bridge.play_end.emit(session, "eof")
    app.processEvents()
    assert dialog._playing is False and dialog.play_btn.text() == "Play"
    assert dialog._t == pytest.approx(60.0)


def test_keyboard_shortcuts(editor):
    dialog, preview, app = editor()
    QTest.keyClick(dialog, Qt.Key.Key_Right)
    assert dialog._t == pytest.approx(1 / 30.0)
    QTest.keyClick(dialog, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier)
    assert dialog._t == pytest.approx(1 + 1 / 30.0)
    QTest.keyClick(dialog, Qt.Key.Key_End)
    assert dialog._t == pytest.approx(60.0)
    QTest.keyClick(dialog, Qt.Key.Key_Home)
    assert dialog._t == 0.0
    dialog.seek(30.0)
    QTest.keyClick(dialog, Qt.Key.Key_S)
    assert dialog.model.count == 2
    QTest.keyClick(dialog, Qt.Key.Key_Z, Qt.KeyboardModifier.ControlModifier)
    assert dialog.model.count == 1
    QTest.keyClick(dialog, Qt.Key.Key_Y, Qt.KeyboardModifier.ControlModifier)
    assert dialog.model.count == 2
    QTest.keyClick(dialog, Qt.Key.Key_Space)
    assert dialog._playing is True
    QTest.keyClick(dialog, Qt.Key.Key_Space)
    assert dialog._playing is False


def test_time_box_does_not_start_with_the_keyboard_focus(editor):
    from PySide6.QtWidgets import QLineEdit

    dialog, preview, app = editor()
    assert not isinstance(dialog.focusWidget(), QLineEdit)
    dialog.seek(14.0)
    assert dialog.time_field.text() == "00:14.000"  # the box follows the playhead


def test_typing_in_the_time_box_does_not_trigger_shortcuts(editor):
    dialog, preview, app = editor()
    dialog.time_field.setFocus()
    app.processEvents()
    QTest.keyClick(dialog.time_field, Qt.Key.Key_S)
    assert dialog.model.count == 1


# ── timeline widget ──────────────────────────────────────────────────────────


def test_dragging_a_clip_edge_trims_it_as_one_undo_step(editor):
    dialog, preview, app = editor()
    timeline = dialog.timeline
    x_end = timeline.view.time_to_x(60.0) - 3
    x_target = timeline.view.time_to_x(30.0)
    _drag(timeline, x_end, x_target)
    clip = dialog.model.clips[0]
    assert clip.end == pytest.approx(30.0, abs=0.6)
    dialog.undo()
    assert dialog.model.clips[0].end == pytest.approx(60.0, abs=0.05)
    assert not dialog.model.can_undo  # the whole drag was a single step


def test_dragging_a_clip_body_moves_it(editor):
    plan = ClipPlan((Clip(10, 20),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    centre = timeline.view.time_to_x(15.0)
    _drag(timeline, centre, centre + timeline.view.time_to_x(10.0) - timeline.view.time_to_x(0.0))
    clip = dialog.model.clips[0]
    assert clip.start == pytest.approx(20.0, abs=0.7)
    assert clip.end - clip.start == pytest.approx(10.0, abs=0.05)


def test_clicking_a_clip_moves_the_playhead_without_editing(editor):
    plan = ClipPlan((Clip(10, 50),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    x = timeline.view.time_to_x(25.0)
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), RULER_H + 30))
    assert dialog._t == pytest.approx(25.0, abs=0.2)
    assert not dialog.model.can_undo


def test_clicking_the_ruler_scrubs_to_that_time(editor):
    dialog, preview, app = editor()
    timeline = dialog.timeline
    x = timeline.view.time_to_x(42.0)
    preview.frames.clear()
    QTest.mouseClick(timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), 10))
    assert dialog._t == pytest.approx(42.0, abs=0.2)
    assert preview.frames  # a frame was asked for


def test_dragging_in_empty_space_makes_a_new_clip(editor):
    plan = ClipPlan((Clip(0, 10),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    _drag(timeline, timeline.view.time_to_x(30.0), timeline.view.time_to_x(40.0))
    assert dialog.model.count == 2
    new = dialog.model.clips[1]
    assert new.start == pytest.approx(30.0, abs=0.7) and new.end == pytest.approx(40.0, abs=0.7)


def test_wheel_zooms_around_the_pointer_and_fit_resets(editor):
    dialog, preview, app = editor()
    timeline = dialog.timeline
    assert timeline.view.is_fit
    x = timeline.width() / 2
    event = QWheelEvent(
        QPointF(x, 40),
        QPointF(x, 40),
        QPoint(0, 0),
        QPoint(0, 240),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    before = timeline.view.x_to_time(x)
    timeline.wheelEvent(event)
    assert not timeline.view.is_fit
    assert timeline.view.x_to_time(x) == pytest.approx(before, abs=0.05)
    timeline.fit()
    assert timeline.view.is_fit


def test_timeline_paints_everything_without_error(editor):
    plan = ClipPlan((Clip(5, 15, "a"), Clip(20, 30)), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    image = dialog.timeline.grab()
    assert not image.isNull() and image.width() > 100
    dialog.timeline.zoom(4.0)
    dialog.timeline.set_playhead(25.0, follow=True)
    assert not dialog.timeline.grab().isNull()


# ── fixes found in review ────────────────────────────────────────────────────


def _press(timeline, x, y=None):
    y = RULER_H + 30 if y is None else y
    QTest.mousePress(timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), int(y)))


def _move(timeline, x, y=None):
    y = RULER_H + 30 if y is None else y
    QTest.mouseMove(timeline, QPoint(int(x), int(y)))


def _release(timeline, x, y=None):
    y = RULER_H + 30 if y is None else y
    QTest.mouseRelease(timeline, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(int(x), int(y)))


def test_escape_during_a_drag_only_cancels_the_drag(editor, monkeypatch):
    plan = ClipPlan((Clip(10, 30), Clip(40, 50)), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    monkeypatch.setattr(
        QMessageBox, "question", staticmethod(lambda *a, **k: pytest.fail("no prompt for a drag"))
    )
    timeline = dialog.timeline
    before = dialog.model.clips
    _press(timeline, timeline.view.time_to_x(30.0) - 3)
    _move(timeline, timeline.view.time_to_x(25.0))
    assert dialog.model.clips != before and timeline.is_dragging
    dialog.reject()  # what Esc does
    assert dialog.isVisible()
    assert dialog.model.clips == before  # the drag was undone
    assert not timeline.is_dragging
    assert not dialog.model.can_undo


def test_closing_after_real_changes_still_asks(editor, monkeypatch):
    dialog, preview, app = editor(ClipPlan((Clip(10, 30),), ClipMode.SEPARATE))
    dialog.seek(20.0)
    dialog.split()
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: (asked.append(1), QMessageBox.StandardButton.No)[1]),
    )
    dialog.reject()
    assert asked and dialog.isVisible()


def test_saving_while_a_table_cell_is_being_typed_keeps_the_text(editor):
    from PySide6.QtWidgets import QLineEdit

    plan = ClipPlan((Clip(5, 15),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    dialog.activateWindow()
    app.processEvents()
    item = dialog.table.item(0, 1)
    dialog.table.setCurrentItem(item)
    dialog.table.editItem(item)
    app.processEvents()
    editor_widget = dialog.table.findChild(QLineEdit)
    assert editor_widget is not None, "the cell editor should be open"
    editor_widget.setText("0:08")
    dialog.save()  # Ctrl+Enter while still typing
    assert dialog.result_plan().clips[0].start == pytest.approx(8.0)


def test_enter_in_the_time_box_jumps_but_does_not_save(editor):
    dialog, preview, app = editor()
    dialog.time_field.setFocus()
    dialog.time_field.setText("0:45")
    QTest.keyClick(dialog.time_field, Qt.Key.Key_Return)
    app.processEvents()
    assert dialog.isVisible(), "Enter in the time box must not close the editor"
    assert dialog._t == pytest.approx(45.0)
    assert dialog.result() != dialog.DialogCode.Accepted


def test_enter_elsewhere_still_saves(editor):
    dialog, preview, app = editor()
    dialog.video.setFocus()
    QTest.keyClick(dialog, Qt.Key.Key_Return)
    assert dialog.result() == dialog.DialogCode.Accepted


def test_a_clip_removed_during_a_drag_does_not_break_the_drag(editor):
    plan = ClipPlan((Clip(10, 20), Clip(30, 40)), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    _press(timeline, timeline.view.time_to_x(15.0))
    _move(timeline, timeline.view.time_to_x(16.0))
    dialog.model.delete(0)  # e.g. a stray Delete key while the mouse is down
    _move(timeline, timeline.view.time_to_x(25.0))  # must not raise or edit the other clip
    _release(timeline, timeline.view.time_to_x(25.0))
    assert dialog.model.count == 1
    clip = dialog.model.clips[0]
    assert (clip.start, clip.end) == (pytest.approx(30.0), pytest.approx(40.0))
    assert not timeline.is_dragging


def test_edit_shortcuts_are_ignored_while_dragging(editor):
    plan = ClipPlan((Clip(10, 20), Clip(30, 40)), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    _press(timeline, timeline.view.time_to_x(15.0))
    _move(timeline, timeline.view.time_to_x(16.0))
    dialog.delete_clip()
    dialog.split()
    dialog.undo()
    assert dialog.model.count == 2
    _release(timeline, timeline.view.time_to_x(16.0))


def test_a_lost_mouse_release_is_cleaned_up_by_the_next_press(editor):
    plan = ClipPlan((Clip(10, 30),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    _press(timeline, timeline.view.time_to_x(30.0) - 3)
    _move(timeline, timeline.view.time_to_x(25.0))
    # ...the release never arrives; a new press must end the old gesture first
    _press(timeline, timeline.view.time_to_x(50.0))
    _release(timeline, timeline.view.time_to_x(50.0))
    assert not dialog.model.in_gesture
    assert dialog.model.can_undo  # the first drag was kept as one step


def test_dragging_an_edge_follows_the_pointer_instead_of_snapping_back(editor):
    plan = ClipPlan((Clip(10, 50),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    timeline = dialog.timeline
    start_x = timeline.view.time_to_x(50.0) - 3
    target = timeline.view.time_to_x(33.0)
    _press(timeline, start_x)
    steps = 40
    for step in range(1, steps + 1):
        _move(timeline, start_x + (target - start_x) * step / steps)
    _release(timeline, target)
    px = 1.0 / timeline.view.px_per_sec
    assert dialog.model.clips[0].end == pytest.approx(33.0, abs=max(0.2, 3 * px))


def test_typed_times_that_overlap_a_neighbour_are_refused(editor):
    plan = ClipPlan((Clip(5, 15), Clip(30, 40)), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    dialog.table.item(0, 2).setText("0:35")  # would run into the second clip
    app.processEvents()
    assert dialog.model.clips[0].end == pytest.approx(15.0)
    assert "overlap" in dialog.status.text()
    dialog.table.item(1, 1).setText("0:50")  # starts after it ends
    app.processEvents()
    assert dialog.model.clips[1].start == pytest.approx(30.0)
    assert "after it starts" in dialog.status.text()
    dialog.table.item(0, 2).setText("0:15.05")  # leaves a 10.05 s clip: fine
    assert dialog.model.clips[0].end == pytest.approx(15.05)


def test_a_clip_cannot_be_typed_shorter_than_the_minimum(editor):
    plan = ClipPlan((Clip(5, 15),), ClipMode.SEPARATE)
    dialog, preview, app = editor(plan)
    dialog.table.item(0, 2).setText("0:05.02")
    app.processEvents()
    assert dialog.model.clips[0].end == pytest.approx(15.0)
    assert "at least" in dialog.status.text()


def test_an_error_message_clears_after_a_change_goes_through(editor):
    dialog, preview, app = editor()
    dialog.add_clip()  # no room: complains
    assert dialog.status.text()
    dialog.seek(30.0)
    dialog.split()  # works
    assert dialog.status.text() == ""


def test_closing_leaves_the_shared_preview_service_usable(editor):
    dialog, preview, app = editor()
    dialog.done(0)
    assert getattr(preview, "frames_cancelled", 0) >= 1
    assert preview.cancelled >= 1  # only the filmstrip, not "everything"


# ── sound ────────────────────────────────────────────────────────────────────


def _frame_bytes(dialog) -> bytes:
    return bytes(dialog._frame_size[0] * dialog._frame_size[1] * 3)


def test_a_video_without_sound_hides_the_sound_controls(editor):
    dialog, preview, app = editor()
    assert dialog.has_audio is False
    for widget in (dialog.sound_btn, dialog.volume_slider, dialog.wave_check, dialog.keep_audio_check):
        assert widget.isHidden()
    assert dialog.sound_note.text() == "This video has no sound."
    assert not dialog.timeline.wave_visible and preview.waves == []
    QTest.keyClick(dialog, Qt.Key.Key_M)  # nothing to mute
    assert dialog._sound_on is True
    dialog.toggle_play()
    assert preview.sound == [{}]  # silent playback exactly as before
    dialog.toggle_play()
    dialog.seek(10.0)
    dialog.set_in()
    dialog.save()
    assert dialog.result_plan().keep_audio is True


def test_a_saved_silent_plan_of_a_video_without_sound_keeps_the_sound_flag_on(editor):
    plan = ClipPlan((Clip(5, 10),), ClipMode.SEPARATE, keep_audio=False)
    dialog, preview, app = editor(plan)
    assert dialog.model.keep_audio is True  # there is no sound to leave out
    assert "no sound" not in dialog.summary.text()


def test_sound_plays_with_the_picture_at_the_remembered_volume(editor, sound_settings):
    sound_settings.config.editor_volume = 40
    dialog, preview, app = editor(audio=True)
    assert not dialog.sound_btn.isHidden() and dialog.sound_btn.text() == "Sound on"
    assert dialog.volume_slider.value() == 40 and dialog.volume_slider.isEnabled()
    assert dialog.sound_note.text() == ""
    dialog.seek(5.0)
    dialog.toggle_play()
    options = preview.sound[-1]
    assert options["audio"] is True and options["volume"] == pytest.approx(0.4)
    preview.on_audio("unavailable")
    app.processEvents()
    assert "No sound device" in dialog.sound_note.text()
    preview.on_audio("playing")
    app.processEvents()
    assert dialog.sound_note.text() == ""
    preview.on_frame(6.0, _frame_bytes(dialog))
    app.processEvents()
    assert dialog._t == pytest.approx(6.0)
    stale = preview.on_audio
    dialog.toggle_play()  # pause
    stale("failed")  # a report from the stopped session
    app.processEvents()
    assert dialog.sound_note.text() == ""
    assert sound_settings.saved == []  # nothing changed, nothing written


def test_m_turns_the_sound_off_and_on_and_is_remembered(editor, sound_settings):
    dialog, preview, app = editor(audio=True)
    QTest.keyClick(dialog, Qt.Key.Key_M)
    assert dialog.sound_on is False
    assert sound_settings.saved == [{"editor_sound_on": False}]
    assert dialog.sound_btn.text() == "Sound off" and not dialog.volume_slider.isEnabled()
    assert dialog.sound_note.text() == "Sound is off. Press M to turn it on."
    dialog.toggle_play()
    assert preview.sound[-1] == {}  # sound off: no sound pipeline at all
    stops = preview.stopped
    QTest.keyClick(dialog, Qt.Key.Key_M)  # while playing: restart from here with sound
    assert sound_settings.saved[-1] == {"editor_sound_on": True}
    assert preview.stopped == stops + 1
    assert preview.sound[-1]["audio"] is True and dialog._playing
    assert len(preview.playbacks) == 2


def test_the_editor_opens_muted_when_it_was_left_muted(editor, sound_settings):
    sound_settings.config.editor_sound_on = False
    sound_settings.config.editor_volume = 15
    dialog, preview, app = editor(audio=True)
    assert dialog.sound_on is False and dialog.sound_btn.text() == "Sound off"
    assert dialog.volume_slider.value() == 15
    dialog.sound_btn.click()
    assert dialog.sound_on is True


def test_the_volume_slider_applies_at_once_and_is_saved_when_it_settles(editor, sound_settings):
    dialog, preview, app = editor(audio=True)
    dialog.volume_slider.setValue(30)
    assert preview.volumes[-1] == pytest.approx(0.3)
    assert sound_settings.saved == []  # not on every step of a drag
    QTest.qWait(te.VOLUME_SAVE_MS + 150)
    assert sound_settings.saved == [{"editor_volume": 30}]
    dialog.volume_slider.setValue(55)
    dialog.done(0)  # closing right away still keeps the last value
    assert sound_settings.saved[-1] == {"editor_volume": 55}


def test_scrubbing_and_frame_steps_stay_silent(editor):
    dialog, preview, app = editor(audio=True)
    dialog.step_frames(3)
    dialog.step_seconds(1)
    dialog.timeline.scrubbing.emit(12.0)
    dialog.timeline.scrubFinished.emit(12.0)
    assert preview.playbacks == [] and preview.sound == []


def test_the_sound_wave_is_measured_and_drawn_under_the_filmstrip(editor):
    dialog, preview, app = editor(audio=True)
    timeline = dialog.timeline
    assert dialog.wave_check.isChecked() and timeline.wave_visible
    assert timeline.height() == RULER_H + TRACK_H + WAVE_H + 6
    count = waveform_buckets(60.0)
    assert preview.waves == [(60.0, count)]
    on_peaks, on_done = preview.wave_callbacks[-1]
    on_peaks(0, [0.5] * 1000)  # from the worker thread, in pieces
    on_peaks(1000, [0.0] * 1000)
    app.processEvents()
    assert timeline.wave_measured == 2000
    assert timeline._wave_message == "Reading the sound…"
    on_peaks(2000, [1.0] * (count - 2000))
    on_done(True)
    app.processEvents()
    assert timeline.wave_measured == count and timeline._wave_message == ""
    assert not timeline.grab().isNull()
    lines = timeline._wave_lines(timeline.width(), WAVE_H)
    assert lines and all(line.x1() == line.x2() for line in lines)
    timeline.zoom(8.0)
    timeline.set_playhead(30.0, follow=True)
    assert not timeline.grab().isNull()


def test_a_sound_wave_that_cannot_be_read_says_so(editor):
    dialog, preview, app = editor(audio=True)
    on_peaks, on_done = preview.wave_callbacks[-1]
    on_done(False)
    app.processEvents()
    assert dialog.timeline._wave_message == "The sound could not be read."
    assert not dialog.timeline.grab().isNull()


def test_hiding_the_sound_wave_stops_the_work_and_is_remembered(editor, sound_settings):
    dialog, preview, app = editor(audio=True)
    old_peaks, old_done = preview.wave_callbacks[-1]
    dialog.wave_check.setChecked(False)
    assert sound_settings.saved == [{"editor_sound_wave": False}]
    assert preview.wave_cancels == 1 and not dialog.timeline.wave_visible
    assert dialog.timeline.height() == RULER_H + TRACK_H + 6
    dialog.wave_check.setChecked(True)  # shown again: measured again from the start
    assert len(preview.waves) == 2 and dialog.timeline.wave_visible
    old_peaks(0, [0.9] * 10)  # late results of the cancelled run are ignored
    old_done(True)
    app.processEvents()
    assert dialog.timeline.wave_measured == 0 and dialog._wave_running


def test_the_sound_wave_stays_hidden_when_it_was_switched_off(editor, sound_settings):
    sound_settings.config.editor_sound_wave = False
    dialog, preview, app = editor(audio=True)
    assert not dialog.wave_check.isChecked() and not dialog.timeline.wave_visible
    assert preview.waves == []  # no work for a lane nobody sees


def test_including_the_sound_is_a_choice_saved_on_the_plan(editor):
    plan = ClipPlan((Clip(5, 15), Clip(20, 30)), ClipMode.JOIN)
    dialog, preview, app = editor(plan, audio=True)
    assert not dialog.keep_audio_check.isHidden() and dialog.keep_audio_check.isChecked()
    dialog.keep_audio_check.setChecked(False)
    assert dialog.model.keep_audio is False
    assert "no sound" in dialog.summary.text()
    dialog.undo()  # an ordinary edit: undo brings the sound back
    assert dialog.model.keep_audio is True and dialog.keep_audio_check.isChecked()
    dialog.redo()
    assert not dialog.keep_audio_check.isChecked()
    dialog.save()
    result = dialog.result_plan()
    assert result.keep_audio is False and result.mode is ClipMode.JOIN and result.count == 2


def test_the_whole_video_without_sound_is_still_a_change(editor, monkeypatch):
    dialog, preview, app = editor(audio=True)
    dialog.keep_audio_check.setChecked(False)
    asked: list = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: (asked.append(1), QMessageBox.StandardButton.No)[1]),
    )
    dialog.reject()
    assert asked  # an unsaved change
    dialog.save()
    plan = dialog.result_plan()
    assert plan is not None and plan.keep_audio is False
    assert plan.count == 1 and plan.clips[0].start == 0.0 and plan.clips[0].end == pytest.approx(60.0)


def test_a_plan_without_sound_reopens_unticked(editor):
    plan = ClipPlan((Clip(5, 15),), ClipMode.SEPARATE, keep_audio=False)
    dialog, preview, app = editor(plan, audio=True)
    assert not dialog.keep_audio_check.isChecked()
    assert "no sound" in dialog.summary.text()
    dialog.save()
    assert dialog.result_plan().keep_audio is False


def test_clip_edges_can_be_dragged_in_the_sound_wave_lane(editor):
    dialog, preview, app = editor(audio=True)
    timeline = dialog.timeline
    lane_y = RULER_H + TRACK_H + WAVE_H // 2
    _drag(timeline, timeline.view.time_to_x(60.0) - 3, timeline.view.time_to_x(40.0), y=lane_y)
    assert dialog.model.clips[0].end == pytest.approx(40.0, abs=0.6)


def test_closing_stops_the_sound_wave_too(editor):
    dialog, preview, app = editor(audio=True)
    dialog.done(0)
    assert preview.wave_cancels >= 1 and preview.stopped >= 1


def test_use_whole_video_keeps_an_unticked_sound_choice(editor):
    plan = ClipPlan((Clip(5, 10), Clip(20, 30)), ClipMode.JOIN)
    dialog, preview, app = editor(plan, audio=True)
    assert "without its sound" not in dialog.whole_btn.toolTip()
    dialog.keep_audio_check.setChecked(False)
    assert "without its sound" in dialog.whole_btn.toolTip()
    dialog.use_whole_video()
    result = dialog.result_plan()
    assert result == ClipPlan((Clip(0.0, 60.0),), ClipMode.SEPARATE, keep_audio=False)
    assert dialog.result() == dialog.DialogCode.Accepted


def test_use_whole_video_with_the_sound_ticked_is_no_change(editor):
    dialog, preview, app = editor(ClipPlan((Clip(5, 10),), keep_audio=False), audio=True)
    dialog.keep_audio_check.setChecked(True)
    dialog.use_whole_video()
    assert dialog.result_plan() is None


@pytest.mark.parametrize(
    "state, words",
    [
        ("past_end", "The sound ends before this point."),
        ("none", "could not be read"),
        ("behind", "cannot show the picture fast enough"),
        ("ended", ""),
    ],
)
def test_the_sound_note_explains_each_kind_of_silence(editor, state, words):
    dialog, preview, app = editor(audio=True)
    dialog.toggle_play()
    preview.on_audio(state)
    app.processEvents()
    if words:
        assert words in dialog.sound_note.text()
    else:
        assert dialog.sound_note.text() == ""
