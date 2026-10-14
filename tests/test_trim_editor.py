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
from video_compressor.gui.qt_shell import trim_editor as te
from video_compressor.gui.qt_shell.timeline import RULER_H


class StubPreview:
    """Stands in for core.preview.PreviewService; answers synchronously."""

    def __init__(self):
        self.frames: list[tuple[float, bool]] = []
        self.filmstrips: list[int] = []
        self.playbacks: list[float] = []
        self.stopped = 0
        self.cancelled = 0
        self.on_frame = None
        self.on_end = None

    def request_frame(self, path, t, size, *, fast, callback, duration=None, fps=None):
        self.frames.append((round(t, 3), fast))
        callback(t, size[0], size[1], bytes(size[0] * size[1] * 3), fast)

    def request_filmstrip(self, path, duration, count, size, on_thumb, on_done=None):
        self.filmstrips.append(count)
        for i in range(count):
            on_thumb(i, (i + 0.5) * duration / count, size[0], size[1], bytes(size[0] * size[1] * 3))
        if on_done:
            on_done()

    def start_playback(self, path, start, size, fps_out, on_frame, on_end, duration=None):
        self.playbacks.append(start)
        self.on_frame, self.on_end = on_frame, on_end

    def stop_playback(self):
        self.stopped += 1

    def cancel_all(self):
        self.cancelled += 1


def _info(duration=60.0):
    return SimpleNamespace(
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


@pytest.fixture
def editor(tmp_path):
    app = QApplication.instance() or QApplication([])
    made = []

    def make(plan=None, duration=60.0, note=""):
        preview = StubPreview()
        dialog = te.TrimEditorDialog(
            None, tmp_path / "movie.mp4", _info(duration), plan, preview, target_note=note
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
    assert preview.cancelled >= 1  # background work is stopped when it closes


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
