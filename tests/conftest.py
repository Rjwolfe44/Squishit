"""Shared test isolation.

Fast AV1 picks a hardware AV1 encoder only after a real test encode passes
(``video_compressor.core.fast_av1``). No test should start a GPU encoder by
accident or write the real ``%APPDATA%\\SquishIt`` check cache, so for the
whole session the probe runner reports "does not work" and there is no disk
cache. That is patched when this file is imported, before any test module or
module-scoped fixture can build a ``VideoCompressor``, and the probe reads
both hooks when it uses them, not when it is built. A test that wants a
working hardware AV1 encoder passes its own probe or runner.

At the end of the session the real cache file is checked: if any test
created or changed it, the run fails.
"""

from __future__ import annotations

import pytest

from video_compressor.core import fast_av1

REAL_DEFAULT_CACHE_PATH = fast_av1.default_cache_path
REAL_DEFAULT_RUNNER = fast_av1.default_runner


def _no_hardware_av1(cmd, timeout):
    return fast_av1.RunOutput(1, "", "hardware AV1 checks are off in tests")


def _no_cache_path():
    return None


def _stamp(path):
    try:
        stat = path.stat()
    except (OSError, AttributeError, TypeError):
        return None
    return (stat.st_size, stat.st_mtime_ns)


_REAL_CACHE_FILE = REAL_DEFAULT_CACHE_PATH()
_REAL_CACHE_BEFORE = _stamp(_REAL_CACHE_FILE)

# Session-wide, from import time on.
fast_av1.default_runner = _no_hardware_av1
fast_av1.default_cache_path = _no_cache_path


@pytest.fixture(autouse=True)
def _isolate_fast_av1_hardware_check(monkeypatch):
    # A test that swapped the hooks itself gets them back afterwards.
    monkeypatch.setattr(fast_av1, "default_runner", _no_hardware_av1)
    monkeypatch.setattr(fast_av1, "default_cache_path", _no_cache_path)


def pytest_sessionfinish(session, exitstatus):
    after = _stamp(_REAL_CACHE_FILE)
    if after != _REAL_CACHE_BEFORE:
        print(
            f"\nA test touched the real Fast AV1 check cache: {_REAL_CACHE_FILE} "
            f"(before {_REAL_CACHE_BEFORE}, after {after})"
        )
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def working_av1_hardware():
    """Build a memory-only probe whose test encode passes for the named encoders.

    ``working_av1_hardware("av1_amf")`` is a PC where only AMF AV1 works:
    av1_nvenc and av1_qsv fail the check the way they do on a Radeon PC.
    """

    def make(*encoders: str, ffmpeg: str = "ffmpeg") -> fast_av1.HardwareAv1Probe:
        passing = set(encoders)

        def runner(cmd, timeout):
            encoder = cmd[cmd.index("-c:v") + 1]
            if encoder in passing:
                return fast_av1.RunOutput(0, "frame=8\nprogress=end\n", "")
            return fast_av1.RunOutput(
                4294967274,
                "frame=0\nprogress=end\n",
                f"[{encoder} @ 0000] Cannot load the encoder driver\n"
                "Could not open encoder before EOF",
            )

        return fast_av1.HardwareAv1Probe(ffmpeg, runner=runner, cache_path=None)

    return make
