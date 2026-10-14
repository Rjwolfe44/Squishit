"""Qt shell dialogs: History, Stats and About (with the update flow).

All offscreen, with no network access and no real installer ever started.
"""

from __future__ import annotations

import io
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

import shiboken6  # noqa: E402
from PySide6.QtCore import QCoreApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from video_compressor.config import APP_VERSION  # noqa: E402
from video_compressor.core import updater  # noqa: E402
from video_compressor.core.history import HistoryEntry, HistoryManager  # noqa: E402
from video_compressor.core.updater import ISSUES_URL, RELEASES_URL, UpdateInfo  # noqa: E402
from video_compressor.gui.qt_shell import about_dialog, history_dialog, stats_dialog  # noqa: E402
from video_compressor.gui.qt_shell.about_dialog import (  # noqa: E402
    AboutDialog,
    UpdateBanner,
    UpdateInstaller,
    release_notes_snippet,
    start_update_check,
)
from video_compressor.gui.qt_shell.history_dialog import (  # noqa: E402
    HistoryDialog,
    format_when,
    friendly_format,
    reveal_in_file_manager,
)
from video_compressor.gui.qt_shell.stats_dialog import StatsDialog, summarize  # noqa: E402

MB = 1024 * 1024


# ── helpers and fixtures ─────────────────────────────────────────


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def wait_until(predicate, timeout=5.0):
    """Run the Qt event loop until *predicate()* is true (or time runs out)."""

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QCoreApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QCoreApplication.processEvents()
    return bool(predicate())


@pytest.fixture(autouse=True)
def isolated(monkeypatch, app):
    """No network, no installer, no browser, and no config writes."""

    def refuse(*_a, **_k):
        raise AssertionError("tests must not reach the network")

    monkeypatch.setattr(updater, "check_for_update", refuse)
    monkeypatch.setattr(updater, "download_update", refuse)
    monkeypatch.setattr(updater.request, "urlopen", refuse)

    def refuse_installer(_path):
        raise AssertionError("tests must not start an installer")

    monkeypatch.setattr(about_dialog, "launch_installer", refuse_installer)
    opened: list = []
    monkeypatch.setattr(about_dialog, "_open_url", opened.append)
    quits: list = []
    monkeypatch.setattr(about_dialog, "quit_application", lambda: quits.append(True))
    monkeypatch.setattr(about_dialog, "INSTALL_QUIT_DELAY_MS", 0)
    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.update_config", lambda self, **kw: None
    )
    monkeypatch.setattr("video_compressor.config.ConfigManager.save_config", lambda self: None)
    yield {"opened": opened, "quits": quits}
    # Let every worker finish and hand over its last message.
    wait_until(lambda: not about_dialog._LIVE_RELAYS, timeout=3.0)


def make_entry(
    name="clip.mp4",
    *,
    orig=5 * MB,
    comp=2 * MB,
    codec="hevc",
    profile="Balanced",
    ts="2020-03-05T09:10:00",
    secs=30.0,
    out=None,
):
    saved_pct = (1 - comp / orig) * 100 if orig else 0.0
    return HistoryEntry(
        input_path=f"C:/videos/{name}",
        output_path=str(out or f"C:/videos/out/{Path(name).stem}_compressed.mp4"),
        original_size_bytes=orig,
        compressed_size_bytes=comp,
        reduction_pct=round(saved_pct, 1),
        codec=codec,
        profile_name=profile,
        duration_seconds=secs,
        timestamp=ts,
    )


class FakeHistory:
    """HistoryManager-like object with just get_entries / clear."""

    def __init__(self, entries):
        self.entries = list(entries)
        self.cleared = 0

    def get_entries(self):
        return list(self.entries)

    def clear(self):
        self.entries = []
        self.cleared += 1


def sample_entries():
    """Oldest first on purpose: the dialog must still list newest first."""

    return [
        make_entry("old_holiday.mp4", ts="2020-01-02T08:00:00", orig=10 * MB, comp=5 * MB,
                   codec="h264", profile="Mobile"),
        make_entry("birthday.mov", ts="2020-03-05T09:10:00", orig=100 * MB, comp=20 * MB,
                   codec="hevc", profile="Balanced", secs=120),
        make_entry("talk.mkv", ts="2020-06-30T22:45:00", orig=40 * MB, comp=30 * MB,
                   codec="svt-av1", profile="Balanced", secs=300),
    ]


def make_info(version="9.9.9", notes="## What's new\n- Faster trims\n- **Bug** fixes"):
    return UpdateInfo(
        latest_version=version,
        download_url="https://example.invalid/SquishIt-Setup.exe",
        release_notes=notes,
    )


def click(button):
    assert button.isEnabled(), button.text()
    button.click()


# ── History ──────────────────────────────────────────────────────


def table_column(dialog, column):
    return [dialog._table.item(r, column).text() for r in range(dialog._table.rowCount())]


def test_history_lists_entries_newest_first(app):
    dialog = HistoryDialog(history=FakeHistory(sample_entries()))
    assert table_column(dialog, history_dialog.COL_FILE) == [
        "talk.mkv",
        "birthday.mov",
        "old_holiday.mp4",
    ]
    assert table_column(dialog, history_dialog.COL_FORMAT) == [
        "AV1 (best compression)",
        "HEVC",
        "H.264",
    ]
    assert table_column(dialog, history_dialog.COL_SIZES)[1] == "100.00 MB → 20.00 MB"
    assert table_column(dialog, history_dialog.COL_SAVED)[1] == "80.0%"
    assert table_column(dialog, history_dialog.COL_WHEN)[1] == "Mar 5, 2020, 9:10 AM"
    assert dialog._stack.currentWidget() is dialog._table
    assert "3 files compressed" in dialog._summary.text()
    # 5 + 80 + 10 MB saved
    assert "95.00 MB saved" in dialog._summary.text()
    # No developer jargon in the headings.
    headers = [dialog._table.horizontalHeaderItem(i).text() for i in range(6)]
    assert headers == ["File", "Profile", "Format", "Before → After", "Saved", "When"]


def test_history_reads_the_real_manager_newest_first(app, tmp_path):
    manager = HistoryManager(path=tmp_path / "history.json", max_entries=50)
    for entry in sample_entries():
        manager.add_entry(entry)
    dialog = HistoryDialog(history=manager)
    assert table_column(dialog, history_dialog.COL_FILE)[0] == "talk.mkv"
    assert dialog._table.rowCount() == 3


def test_history_defaults_to_the_apps_history(app, monkeypatch):
    fake = FakeHistory([make_entry("only.mp4")])
    monkeypatch.setattr(history_dialog, "default_history", lambda: fake)
    dialog = HistoryDialog()
    assert table_column(dialog, history_dialog.COL_FILE) == ["only.mp4"]


def test_history_filter_narrows_rows(app):
    dialog = HistoryDialog(history=FakeHistory(sample_entries()))
    dialog._filter.setText("birthday")
    assert [e.input_path.rsplit("/", 1)[1] for e in dialog.visible_rows()] == ["birthday.mov"]
    assert dialog._table.rowCount() == 1
    # Profile and format are searched too, and every word must match.
    dialog._filter.setText("mobile")
    assert dialog._table.rowCount() == 1
    dialog._filter.setText("h.264 mobile")
    assert dialog._table.rowCount() == 1
    dialog._filter.setText("balanced")
    assert dialog._table.rowCount() == 2
    dialog._filter.setText("nothing like this")
    assert dialog._table.rowCount() == 0
    assert dialog._stack.currentWidget() is dialog._empty_page
    assert dialog._empty_title.text() == "No matches"
    dialog._filter.setText("")
    assert dialog._table.rowCount() == 3
    assert dialog._stack.currentWidget() is dialog._table


def test_history_empty_state(app):
    dialog = HistoryDialog(history=FakeHistory([]))
    assert dialog._stack.currentWidget() is dialog._empty_page
    assert dialog._empty_title.text() == "Nothing here yet"
    assert "show up here" in dialog._empty_hint.text()
    assert dialog._table.rowCount() == 0
    assert not dialog._clear_btn.isEnabled()
    assert not dialog._open_btn.isEnabled()
    assert "No files" in dialog._summary.text()


def test_history_clear_asks_first(app, monkeypatch):
    fake = FakeHistory(sample_entries())
    dialog = HistoryDialog(history=fake)
    asked = []

    def answer(button):
        def question(parent, title, text, *args, **kwargs):
            asked.append((title, text, args))
            return button

        return staticmethod(question)

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.No))
    click(dialog._clear_btn)
    assert fake.cleared == 0
    assert dialog._table.rowCount() == 3
    assert asked and asked[0][0] == "Clear history"
    assert "3 entries" in asked[0][1]
    assert "not touched" in asked[0][1]

    monkeypatch.setattr(QMessageBox, "question", answer(QMessageBox.StandardButton.Yes))
    click(dialog._clear_btn)
    assert fake.cleared == 1
    assert dialog._table.rowCount() == 0
    assert dialog._stack.currentWidget() is dialog._empty_page
    assert not dialog._clear_btn.isEnabled()


def test_history_clear_failure_is_reported_not_raised(app, monkeypatch):
    class Stuck(FakeHistory):
        def clear(self):
            raise OSError("disk is read-only")

    dialog = HistoryDialog(history=Stuck(sample_entries()))
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes),
    )
    click(dialog._clear_btn)
    assert "disk is read-only" in dialog._status.text()
    assert dialog._table.rowCount() == 3


def test_history_header_click_sorts_and_toggles(app):
    dialog = HistoryDialog(history=FakeHistory(sample_entries()))
    dialog._on_header_clicked(history_dialog.COL_SIZES)  # biggest first
    assert table_column(dialog, history_dialog.COL_FILE) == [
        "birthday.mov",
        "talk.mkv",
        "old_holiday.mp4",
    ]
    dialog._on_header_clicked(history_dialog.COL_SIZES)  # smallest first
    assert table_column(dialog, history_dialog.COL_FILE)[0] == "old_holiday.mp4"
    dialog._on_header_clicked(history_dialog.COL_FILE)  # A to Z
    assert table_column(dialog, history_dialog.COL_FILE) == [
        "birthday.mov",
        "old_holiday.mp4",
        "talk.mkv",
    ]


def test_history_open_folder_selects_the_file_on_windows(app, monkeypatch, tmp_path):
    out = tmp_path / "my videos" / "result.mp4"
    out.parent.mkdir()
    out.write_bytes(b"x")
    dialog = HistoryDialog(history=FakeHistory([make_entry("a.mp4", out=out)]))
    assert not dialog._open_btn.isEnabled()
    commands = []
    monkeypatch.setattr(history_dialog, "_platform", lambda: "win32")
    monkeypatch.setattr(history_dialog.subprocess, "Popen", lambda cmd, *a, **k: commands.append(cmd))
    dialog._table.selectRow(0)
    click(dialog._open_btn)
    assert len(commands) == 1
    assert commands[0].startswith('explorer /select,"')
    assert commands[0].endswith('result.mp4"')
    assert dialog._status.text() == ""


def test_history_open_folder_guards_missing_files_and_other_platforms(app, monkeypatch, tmp_path):
    folder_urls = []
    monkeypatch.setattr(
        history_dialog.QDesktopServices,
        "openUrl",
        staticmethod(lambda url: folder_urls.append(Path(url.toLocalFile())) or True),
    )
    popen_calls = []
    monkeypatch.setattr(history_dialog.subprocess, "Popen", lambda *a, **k: popen_calls.append(a))

    # File deleted, folder still there: open the folder and say so.
    monkeypatch.setattr(history_dialog, "_platform", lambda: "win32")
    message = reveal_in_file_manager(tmp_path / "gone.mp4")
    assert "moved or deleted" in message
    assert folder_urls == [tmp_path]
    assert popen_calls == []

    # File and folder both gone: nothing opens.
    folder_urls.clear()
    message = reveal_in_file_manager(tmp_path / "nowhere" / "gone.mp4")
    assert "nothing to open" in message
    assert folder_urls == [] and popen_calls == []

    # Linux: no Explorer, just the folder.
    present = tmp_path / "here.mp4"
    present.write_bytes(b"x")
    monkeypatch.setattr(history_dialog, "_platform", lambda: "linux")
    assert reveal_in_file_manager(present) == ""
    assert folder_urls == [tmp_path]
    assert popen_calls == []

    # macOS: Finder reveal.
    monkeypatch.setattr(history_dialog, "_platform", lambda: "darwin")
    assert reveal_in_file_manager(present) == ""
    assert popen_calls and popen_calls[0][0][:2] == ["open", "-R"]


def test_history_open_folder_reports_a_failed_launch(app, monkeypatch, tmp_path):
    present = tmp_path / "here.mp4"
    present.write_bytes(b"x")
    monkeypatch.setattr(history_dialog, "_platform", lambda: "win32")

    def boom(*_a, **_k):
        raise OSError("explorer is busy")

    monkeypatch.setattr(history_dialog.subprocess, "Popen", boom)
    assert "explorer is busy" in reveal_in_file_manager(present)


def test_friendly_format_names():
    assert friendly_format("h264") == "H.264"
    assert friendly_format("HEVC") == "HEVC"
    assert friendly_format("libx265") == "HEVC"
    assert friendly_format("svt-av1") == "AV1 (best compression)"
    assert friendly_format("av1") == "AV1"
    assert friendly_format("") == "—"
    assert friendly_format("prores") == "PRORES"


def test_format_when_is_readable():
    now = datetime(2026, 10, 14, 15, 0)
    assert format_when("2026-10-14T15:45:00", now) == "Today, 3:45 PM"
    assert format_when("2026-10-13T09:10:00", now) == "Yesterday, 9:10 AM"
    assert format_when("2026-10-03T00:05:00", now) == "Oct 3, 2026, 12:05 AM"
    assert format_when("2026-10-03T12:00:00", now) == "Oct 3, 2026, 12:00 PM"
    assert format_when("not a date", now) == "not a date"
    assert format_when("", now) == "—"


# ── Stats ────────────────────────────────────────────────────────


def test_stats_numbers_from_sample_entries(app):
    dialog = StatsDialog(history=FakeHistory(sample_entries()))
    assert not dialog.is_empty
    values = dialog.card_values()
    details = dialog.card_details()
    assert values[stats_dialog.CARD_FILES] == "3"
    # 150 MB before, 55 MB after
    assert values[stats_dialog.CARD_SAVED] == "95.00 MB"
    assert "150.00 MB became 55.00 MB" in details[stats_dialog.CARD_SAVED]
    # (50 + 80 + 25) / 3
    assert values[stats_dialog.CARD_AVERAGE] == "51.7%"
    assert values[stats_dialog.CARD_TIME] == "7m 30s"
    assert values[stats_dialog.CARD_BEST] == "birthday.mov"
    assert "Saved 80.00 MB" in details[stats_dialog.CARD_BEST]
    # Two Balanced, one Mobile; the three formats are all different, so a tie
    # goes to the most recent entry.
    assert values[stats_dialog.CARD_PROFILE] == "Balanced"
    assert details[stats_dialog.CARD_PROFILE] == "Used for 2 of 3 files"
    assert values[stats_dialog.CARD_FORMAT] == "AV1 (best compression)"
    assert values[stats_dialog.CARD_WEEK] == "0 files"


def test_stats_summarize_week_window_and_favourites():
    now = datetime(2026, 10, 14, 12, 0)
    entries = [
        make_entry("a.mp4", ts="2026-10-13T10:00:00", orig=10 * MB, comp=4 * MB, codec="hevc"),
        make_entry("b.mp4", ts="2026-10-09T10:00:00", orig=20 * MB, comp=10 * MB, codec="hevc"),
        make_entry("c.mp4", ts="2026-09-01T10:00:00", orig=30 * MB, comp=15 * MB, codec="h264"),
        make_entry("d.mp4", ts="garbage", orig=1 * MB, comp=1 * MB, codec="hevc", profile=""),
    ]
    stats = summarize(entries, now=now)
    assert stats["count"] == 4
    assert stats["week_count"] == 2
    assert stats["week_saved"] == 6 * MB + 10 * MB
    assert stats["top_format"] == ("HEVC", 3)
    assert stats["top_profile"] == ("Balanced", 3)
    assert stats["best"].input_path.endswith("c.mp4")
    assert stats["total_saved"] == 6 * MB + 10 * MB + 15 * MB


def test_stats_prefers_the_managers_headline_numbers(app):
    class WithStats(FakeHistory):
        def get_stats(self):
            return {"count": 7, "total_saved_bytes": 3 * MB}

    dialog = StatsDialog(history=WithStats(sample_entries()))
    values = dialog.card_values()
    assert values[stats_dialog.CARD_FILES] == "7"
    assert values[stats_dialog.CARD_SAVED] == "3.00 MB"


def test_stats_empty_state(app):
    dialog = StatsDialog(history=FakeHistory([]))
    assert dialog.is_empty
    assert dialog.card_values() == {stats_dialog.EMPTY_TITLE: "No stats yet"}
    assert "show up here" in dialog.card_details()[stats_dialog.EMPTY_TITLE]


def test_stats_with_the_real_manager(app, tmp_path):
    manager = HistoryManager(path=tmp_path / "history.json", max_entries=50)
    dialog = StatsDialog(history=manager)
    assert dialog.is_empty
    for entry in sample_entries():
        manager.add_entry(entry)
    dialog.refresh()
    assert not dialog.is_empty
    assert dialog.card_values()[stats_dialog.CARD_FILES] == "3"


def test_stats_and_history_copy_avoids_developer_jargon(app):
    import re

    jargon = re.compile(r"\b(crf|bitrate|codec|preset|ffmpeg args)\b", re.IGNORECASE)
    history = HistoryDialog(history=FakeHistory(sample_entries()))
    stats = StatsDialog(history=FakeHistory(sample_entries()))
    texts = [
        history._table.horizontalHeaderItem(i).text() for i in range(history._table.columnCount())
    ]
    texts += list(stats.card_values()) + list(stats.card_details().values())
    for text in texts:
        assert jargon.search(text) is None, text


# ── About: layout, links ─────────────────────────────────────────


def test_about_shows_version_licence_hardware_and_links(app, isolated):
    dialog = AboutDialog(hardware_summary="Intel Core i7 · RTX 4070 · NVENC ready")
    dialog.show()
    app.processEvents()
    assert dialog.windowTitle() == "About SquishIt"
    assert dialog._version.text() == f"Version {APP_VERSION}"
    assert "PolyForm Noncommercial License 1.0.0" in dialog._licence.text()
    assert dialog._hardware.text() == "Intel Core i7 · RTX 4070 · NVENC ready"
    assert dialog._hardware_card.isVisible()
    assert not dialog._install_btn.isVisible()
    assert not dialog._fallback_btn.isVisible()
    assert not dialog._progress.isVisible()

    click(dialog._link_buttons["Download page"])
    click(dialog._link_buttons["GitHub issues"])
    click(dialog._link_buttons["Project page"])
    assert isolated["opened"][:2] == [RELEASES_URL, ISSUES_URL]
    assert isolated["opened"][2].startswith("https://github.com/")
    dialog.close()


def test_about_hides_the_hardware_section_when_there_is_no_summary(app):
    dialog = AboutDialog()
    dialog.show()
    app.processEvents()
    assert not dialog._hardware_card.isVisible()
    assert not dialog._hardware_section.isVisible()
    dialog.close()


# ── About: update check ──────────────────────────────────────────


def test_about_check_says_up_to_date(app, monkeypatch):
    calls = []

    def fake_check(version, force=False):
        calls.append((version, force, threading.current_thread() is threading.main_thread()))
        return None

    monkeypatch.setattr(updater, "check_for_update", fake_check)
    dialog = AboutDialog()
    dialog.show()
    click(dialog._check_btn)
    assert dialog._status.text() == "Looking for updates…"
    assert not dialog._check_btn.isEnabled()
    assert wait_until(lambda: dialog._check_btn.isEnabled())
    assert calls == [(APP_VERSION, True, False)]  # forced, and off the GUI thread
    assert dialog._status.text().startswith("You're up to date")
    assert dialog._check_btn.text() == "Check for updates"
    assert not dialog._install_btn.isVisible()
    assert not dialog._error.isVisible()
    dialog.close()


def test_about_check_finds_an_update(app, monkeypatch):
    monkeypatch.setattr(updater, "check_for_update", lambda version, force=False: make_info())
    dialog = AboutDialog()
    dialog.show()
    click(dialog._check_btn)
    assert wait_until(lambda: dialog._install_btn.isVisible())
    assert dialog._status.text() == f"Version 9.9.9 is available. You have version {APP_VERSION}."
    assert dialog._notes_heading.text() == "WHAT'S NEW IN 9.9.9"
    assert dialog._notes.isVisible()
    assert "Faster trims" in dialog._notes.text()
    assert "**" not in dialog._notes.text() and "#" not in dialog._notes.text()
    assert dialog._install_btn.text() == "Download and install"
    assert dialog._install_btn.isEnabled()
    assert not dialog._check_btn.isVisible()
    dialog.close()


def test_about_check_error_shows_message_and_fallback(app, monkeypatch, isolated):
    def broken(version, force=False):
        raise RuntimeError("DNS exploded")

    monkeypatch.setattr(updater, "check_for_update", broken)
    dialog = AboutDialog()
    dialog.show()
    click(dialog._check_btn)
    assert wait_until(lambda: dialog._error.isVisible())
    assert "Couldn't check for updates" in dialog._error.text()
    assert dialog._fallback_btn.isVisible()
    assert dialog._check_btn.isEnabled()
    click(dialog._fallback_btn)
    assert isolated["opened"] == [RELEASES_URL]
    # A later good check clears the problem.
    monkeypatch.setattr(updater, "check_for_update", lambda version, force=False: None)
    click(dialog._check_btn)
    assert wait_until(lambda: dialog._status.text().startswith("You're up to date"))
    assert not dialog._error.isVisible()
    assert not dialog._fallback_btn.isVisible()
    dialog.close()


def test_start_update_check_delivers_on_the_gui_thread(app, monkeypatch):
    seen = []
    info = make_info()
    monkeypatch.setattr(updater, "check_for_update", lambda version, force=False: info)
    start_update_check(
        None,
        lambda result: seen.append((result, threading.current_thread() is threading.main_thread())),
        force=True,
    )
    assert wait_until(lambda: seen)
    assert seen == [(info, True)]


def test_start_update_check_error_goes_to_on_error_or_none(app, monkeypatch):
    def broken(version, force=False):
        raise OSError("offline")

    monkeypatch.setattr(updater, "check_for_update", broken)
    errors, results = [], []
    start_update_check(None, results.append, on_error=lambda m: errors.append(
        (m, threading.current_thread() is threading.main_thread())))
    assert wait_until(lambda: errors)
    assert errors == [("offline", True)] and results == []
    # Without on_error a failed check looks like "no update".
    start_update_check(None, results.append)
    assert wait_until(lambda: results)
    assert results == [None]


def test_start_update_check_passes_force_through(app, monkeypatch):
    seen = []

    def fake(version, force=False):
        seen.append((version, force))
        return None

    monkeypatch.setattr(updater, "check_for_update", fake)
    results = []
    start_update_check(None, results.append)
    start_update_check(None, results.append, force=True)
    assert wait_until(lambda: len(results) == 2)
    assert sorted(seen) == sorted([(APP_VERSION, False), (APP_VERSION, True)])


def test_update_check_result_is_dropped_when_cancelled(app, monkeypatch):
    gate = threading.Event()
    done = threading.Event()

    def slow(version, force=False):
        gate.wait(5)
        done.set()
        return make_info()

    monkeypatch.setattr(updater, "check_for_update", slow)
    results = []
    handle = start_update_check(None, results.append)
    assert handle.running
    handle.cancel()
    gate.set()
    assert done.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)
    assert results == []


def test_closing_about_while_the_check_runs_is_safe(app, monkeypatch):
    gate = threading.Event()
    done = threading.Event()

    def slow(version, force=False):
        gate.wait(5)
        done.set()
        return make_info()

    monkeypatch.setattr(updater, "check_for_update", slow)
    dialog = AboutDialog()
    dialog.show()
    click(dialog._check_btn)
    assert dialog._check is not None and dialog._check.running
    dialog.close()
    gate.set()
    assert done.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)
    # The late answer never reached the closed dialog.
    assert not dialog._install_btn.isVisible()
    assert dialog._status.text() == "Looking for updates…"


def test_deleting_about_while_the_check_runs_is_safe(app, monkeypatch):
    gate = threading.Event()
    done = threading.Event()

    def slow(version, force=False):
        gate.wait(5)
        done.set()
        return make_info()

    monkeypatch.setattr(updater, "check_for_update", slow)
    dialog = AboutDialog()
    click(dialog._check_btn)
    shiboken6.delete(dialog)
    assert not shiboken6.isValid(dialog)
    gate.set()
    assert done.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)


# ── About: download and install ──────────────────────────────────


def test_about_download_and_install(app, monkeypatch, isolated, tmp_path):
    installer = tmp_path / "SquishIt-Setup.exe"
    installer.write_bytes(b"setup")
    downloads, launched = [], []
    thread_flags = []

    def fake_download(url, progress_cb=None):
        downloads.append(url)
        thread_flags.append(threading.current_thread() is threading.main_thread())
        progress_cb(25, 100)
        progress_cb(100, 100)
        return installer

    monkeypatch.setattr(updater, "download_update", fake_download)
    monkeypatch.setattr(about_dialog, "launch_installer", launched.append)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    app.processEvents()
    assert dialog._install_btn.isVisible()
    click(dialog._install_btn)
    assert dialog._install_btn.text() == "Downloading…"
    assert not dialog._install_btn.isEnabled()
    assert dialog._progress.isVisible()
    assert wait_until(lambda: isolated["quits"])
    assert downloads == ["https://example.invalid/SquishIt-Setup.exe"]
    assert thread_flags == [False]
    assert launched == [str(installer)]
    assert dialog._progress.maximum() == 100 and dialog._progress.value() == 100
    assert "Installing the update" in dialog._status.text()
    assert dialog._install_btn.text() == "Installing…"
    dialog.close()


def test_about_download_reports_progress(app, monkeypatch, tmp_path):
    gate = threading.Event()
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"x")

    def fake_download(url, progress_cb=None):
        progress_cb(5 * MB, 20 * MB)
        gate.wait(5)
        return None

    monkeypatch.setattr(updater, "download_update", fake_download)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    click(dialog._install_btn)
    assert wait_until(lambda: dialog._progress.value() == 25)
    assert "5.00 MB of 20.00 MB" in dialog._status.text()
    gate.set()
    assert wait_until(lambda: dialog._error.isVisible())
    dialog.close()


def test_about_download_that_returns_nothing_offers_the_download_page(app, monkeypatch, isolated):
    monkeypatch.setattr(updater, "download_update", lambda url, progress_cb=None: None)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    click(dialog._install_btn)
    assert wait_until(lambda: dialog._error.isVisible())
    assert "download didn't finish" in dialog._error.text()
    assert dialog._fallback_btn.isVisible()
    assert dialog._install_btn.text() == "Try again"
    assert dialog._install_btn.isEnabled()
    assert not dialog._progress.isVisible()
    click(dialog._fallback_btn)
    assert isolated["opened"] == [RELEASES_URL]
    assert isolated["quits"] == []
    dialog.close()


def test_about_download_exception_shows_error_text(app, monkeypatch):
    def broken(url, progress_cb=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(updater, "download_update", broken)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    click(dialog._install_btn)
    assert wait_until(lambda: dialog._error.isVisible())
    assert "disk full" in dialog._error.text()
    dialog.close()


def test_about_installer_that_cannot_start_shows_error(app, monkeypatch, isolated, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"x")
    monkeypatch.setattr(updater, "download_update", lambda url, progress_cb=None: installer)

    def cannot_start(_path):
        raise OSError("blocked by antivirus")

    monkeypatch.setattr(about_dialog, "launch_installer", cannot_start)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    click(dialog._install_btn)
    assert wait_until(lambda: dialog._error.isVisible())
    assert "installer couldn't start" in dialog._error.text()
    assert "blocked by antivirus" in dialog._error.text()
    assert dialog._fallback_btn.isVisible()
    assert isolated["quits"] == []
    dialog.close()


def test_closing_about_mid_download_cancels_and_never_installs(app, monkeypatch, isolated):
    started = threading.Event()
    stopped = threading.Event()
    launched = []

    def endless(url, progress_cb=None):
        started.set()
        try:
            while True:
                progress_cb(1, 100)  # raises once the dialog is closed
                time.sleep(0.005)
        finally:
            stopped.set()

    monkeypatch.setattr(updater, "download_update", endless)
    monkeypatch.setattr(about_dialog, "launch_installer", launched.append)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    click(dialog._install_btn)
    assert started.wait(5)
    assert dialog._installer.running
    dialog.close()
    assert stopped.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)
    assert launched == [] and isolated["quits"] == []
    assert not dialog._installer.running


def test_finished_download_is_deleted_if_the_dialog_closed_meanwhile(app, monkeypatch, tmp_path):
    """No Content-Length means no progress calls, so the cancel is seen at the end."""

    gate = threading.Event()
    finished = threading.Event()
    installer = tmp_path / "late.exe"
    installer.write_bytes(b"x")
    launched = []

    def late(url, progress_cb=None):
        gate.wait(5)
        finished.set()
        return installer

    monkeypatch.setattr(updater, "download_update", late)
    monkeypatch.setattr(about_dialog, "launch_installer", launched.append)
    dialog = AboutDialog(update_info=make_info())
    dialog.show()
    click(dialog._install_btn)
    dialog.close()
    gate.set()
    assert finished.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)
    assert not installer.exists()
    assert launched == []


def test_real_download_update_cancel_removes_the_partial_file(app, monkeypatch, tmp_path):
    """Run the real ``download_update`` against a fake response (no sockets)."""

    class FakeResponse(io.BytesIO):
        @property
        def headers(self):
            return {"Content-Length": str(300_000)}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.undo()  # drop the autouse refusals for this test only ...
    monkeypatch.setattr(updater.request, "urlopen", lambda *a, **k: FakeResponse(b"z" * 300_000))
    monkeypatch.setattr(
        "video_compressor.config.ConfigManager.update_config", lambda self, **kw: None
    )
    created = []
    real_mkstemp = updater.tempfile.mkstemp

    def tracked_mkstemp(**kwargs):
        fd, name = real_mkstemp(dir=str(tmp_path), **kwargs)
        created.append(Path(name))
        return fd, name

    monkeypatch.setattr(updater.tempfile, "mkstemp", tracked_mkstemp)
    seen = []
    cancel = threading.Event()

    def progress(done, total):
        seen.append(done)
        if cancel.is_set():
            raise about_dialog._Cancelled()

    # A normal run keeps the file.
    path = updater.download_update("https://example.invalid/a.exe", progress_cb=progress)
    assert path is not None and path.exists() and path.stat().st_size == 300_000
    # A cancelled run deletes it and the error comes out of download_update.
    cancel.set()
    with pytest.raises(about_dialog._Cancelled):
        updater.download_update("https://example.invalid/a.exe", progress_cb=progress)
    assert len(created) == 2
    assert created[0].exists() and not created[1].exists()


def test_installer_without_a_download_url_fails_cleanly(app):
    installer = UpdateInstaller()
    failures = []
    installer.failed.connect(failures.append)
    assert installer.start(UpdateInfo("9.9.9", "", "")) is False
    assert failures and "no installer" in failures[0]
    assert not installer.running


def test_installer_ignores_a_second_start_while_running(app, monkeypatch, tmp_path):
    gate = threading.Event()
    installer_path = tmp_path / "s.exe"
    installer_path.write_bytes(b"x")
    calls = []

    def slow(url, progress_cb=None):
        calls.append(url)
        gate.wait(5)
        return installer_path

    monkeypatch.setattr(updater, "download_update", slow)
    monkeypatch.setattr(about_dialog, "launch_installer", lambda path: None)
    installer = UpdateInstaller()
    launched = []
    installer.launched.connect(launched.append)
    assert installer.start(make_info()) is True
    assert installer.running
    assert installer.start(make_info()) is False
    gate.set()
    assert wait_until(lambda: launched)
    assert len(calls) == 1
    assert not installer.running


# ── Update banner ────────────────────────────────────────────────


def test_banner_shows_and_hides(app):
    banner = UpdateBanner()
    assert banner.isHidden()
    banner.set_update(make_info("3.1.0"))
    assert not banner.isHidden()
    assert banner._title.text() == "Version 3.1.0 is available"
    assert banner._install_btn.text() == "Download and install"
    assert banner._later_btn.text() == "Later"
    assert banner.update_info.latest_version == "3.1.0"
    banner.set_update(None)
    assert banner.isHidden()
    assert banner.update_info is None


def test_banner_signals(app):
    banner = UpdateBanner(handle_install=False)
    info = make_info()
    banner.set_update(info)
    requested, dismissed = [], []
    banner.installRequested.connect(requested.append)
    banner.dismissed.connect(lambda: dismissed.append(True))
    click(banner._install_btn)
    assert requested == [info]
    assert dismissed == []
    assert not banner._installer.running  # handle_install=False does no work
    click(banner._later_btn)
    assert dismissed == [True]
    assert banner.isHidden()


def test_banner_installs_by_itself(app, monkeypatch, isolated, tmp_path):
    installer = tmp_path / "setup.exe"
    installer.write_bytes(b"x")
    launched = []

    def fake_download(url, progress_cb=None):
        progress_cb(40, 100)
        return installer

    monkeypatch.setattr(updater, "download_update", fake_download)
    monkeypatch.setattr(about_dialog, "launch_installer", launched.append)
    banner = UpdateBanner()
    banner.set_update(make_info())
    banner.show()
    requested, launched_signals = [], []
    banner.installRequested.connect(requested.append)
    banner.installLaunched.connect(lambda: launched_signals.append(True))
    click(banner._install_btn)
    assert len(requested) == 1
    assert banner._later_btn.text() == "Cancel"
    assert wait_until(lambda: isolated["quits"])
    assert launched == [str(installer)]
    assert launched_signals == [True]
    assert "SquishIt will close" in banner._detail.text()
    assert banner._install_btn.text() == "Installing…"


def test_banner_failure_offers_retry_and_download_page(app, monkeypatch, isolated):
    monkeypatch.setattr(updater, "download_update", lambda url, progress_cb=None: None)
    banner = UpdateBanner()
    banner.set_update(make_info())
    banner.show()
    click(banner._install_btn)
    assert wait_until(lambda: banner._fallback_btn.isVisible())
    assert "download didn't finish" in banner._detail.text()
    assert banner._install_btn.text() == "Try again"
    assert banner._install_btn.isEnabled()
    click(banner._fallback_btn)
    assert isolated["opened"] == [RELEASES_URL]
    banner.close()


def test_banner_cancel_during_download_returns_to_idle(app, monkeypatch, isolated):
    started = threading.Event()
    stopped = threading.Event()
    launched = []

    def endless(url, progress_cb=None):
        started.set()
        try:
            while True:
                progress_cb(1, 100)
                time.sleep(0.005)
        finally:
            stopped.set()

    monkeypatch.setattr(updater, "download_update", endless)
    monkeypatch.setattr(about_dialog, "launch_installer", launched.append)
    banner = UpdateBanner()
    dismissed = []
    banner.dismissed.connect(lambda: dismissed.append(True))
    banner.set_update(make_info())
    banner.show()
    click(banner._install_btn)
    assert started.wait(5)
    click(banner._later_btn)  # "Cancel" while downloading
    assert stopped.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)
    assert dismissed == []
    assert not banner.isHidden()
    assert banner._install_btn.text() == "Download and install"
    assert banner._install_btn.isEnabled()
    assert launched == [] and isolated["quits"] == []


def test_deleting_the_banner_mid_download_is_safe(app, monkeypatch, isolated):
    started = threading.Event()
    stopped = threading.Event()

    def endless(url, progress_cb=None):
        started.set()
        try:
            while True:
                progress_cb(1, 100)
                time.sleep(0.005)
        finally:
            stopped.set()

    monkeypatch.setattr(updater, "download_update", endless)
    monkeypatch.setattr(about_dialog, "launch_installer", lambda path: pytest.fail("installer ran"))
    banner = UpdateBanner()
    banner.set_update(make_info())
    click(banner._install_btn)
    assert started.wait(5)
    shiboken6.delete(banner)
    assert stopped.wait(5)
    assert wait_until(lambda: not about_dialog._LIVE_RELAYS)
    assert isolated["quits"] == []


# ── release notes snippet ────────────────────────────────────────


def test_release_notes_snippet_strips_markdown_and_trims():
    body = (
        "## What's new\r\n\r\n"
        "- **Trim** your clips ([details](https://example.com))\r\n"
        "- `Faster` startup\r\n"
        "---\r\n"
        "<!-- note -->\r\n"
        "Thanks for using it!"
    )
    assert release_notes_snippet(body) == (
        "What's new\n• Trim your clips (details)\n• Faster startup\nThanks for using it!"
    )
    many = "\n".join(f"line {i}" for i in range(20))
    snippet = release_notes_snippet(many, max_lines=3)
    assert snippet == "line 0\nline 1\nline 2…"
    assert release_notes_snippet("x" * 900, max_chars=50) == "x" * 50 + "…"
    assert release_notes_snippet("") == ""
    assert release_notes_snippet(None) == ""
