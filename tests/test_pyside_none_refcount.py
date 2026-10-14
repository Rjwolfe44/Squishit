"""Guard against a PySide6 build that mishandles ``None`` on Python 3.10/3.11.

PySide6 6.12.0 drops one reference to ``None`` on every call that returns
nothing (``setText``, ``update``, ``setPen``...). ``None`` has a large count, so
nothing happens for a while, then the interpreter aborts with
``none_dealloc: deallocating None`` or a crash while exiting. Python 3.12 made
``None`` immortal, which hides the bug there.

The dependency lists skip 6.12.0. This test catches a bad build that gets
installed anyway, with a message that names the cause.
"""

from __future__ import annotations

import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PySide6")

pytestmark = pytest.mark.skipif(
    sys.version_info >= (3, 12),
    reason="None is immortal on Python 3.12+, so a reference-count slip cannot show",
)


def test_calls_that_return_nothing_leave_nones_reference_count_alone():
    import PySide6
    from PySide6.QtWidgets import QApplication, QLabel

    app = QApplication.instance() or QApplication([])
    label = QLabel()
    label.setText("warm up")
    calls = 200
    before = sys.getrefcount(None)
    for _ in range(calls):
        label.setText("x")
        label.setToolTip("y")
    after = sys.getrefcount(None)
    assert after - before > -20, (
        f"PySide6 {PySide6.__version__} on Python {sys.version_info.major}.{sys.version_info.minor} "
        f"lost {before - after} references to None in {calls * 2} calls. Install a different PySide6 "
        "(for example 6.11.2) or run Python 3.12 or newer; otherwise the app aborts after a while."
    )
    assert app is not None  # keeps the application alive for the whole test
