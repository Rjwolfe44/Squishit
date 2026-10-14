"""Fast AV1: verified hardware encoders and the CPU speed policy.

The Fast AV1 lane (``VideoCodec.AV1``) runs, in order:

1. a hardware AV1 encoder that passed a real test encode on this PC
   (``av1_nvenc``, then ``av1_qsv``, then ``av1_amf``),
2. ``libsvtav1`` at a preset this PC can run near real time,
3. ``libaom-av1`` in its realtime mode when FFmpeg has no SVT-AV1,
4. HEVC (or H.264) when this FFmpeg has no AV1 encoder at all.

FFmpeg's ``-encoders`` list is not proof that a hardware encoder works. A
Windows build lists all three on every PC, and a GPU name only hints at what
the driver can do. ``HardwareAv1Probe`` runs a few generated frames through
each candidate (no file is written, short timeout, no console window) and
keeps the answer in memory and in a small JSON file keyed by the encoder,
the FFmpeg build, and the display adapters with their driver versions. A
normal launch reads that file. A new driver, GPU, or FFmpeg build probes
again. Max / Archival (``VideoCodec.SVT_AV1``) does not use anything here.
"""

from __future__ import annotations

import atexit
import json
import logging
import math
import os
import platform
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

# Preference order, the same as the H.264 and HEVC lanes: NVENC, QSV, AMF.
HW_AV1_ENCODERS: Tuple[str, ...] = ("av1_nvenc", "av1_qsv", "av1_amf")
# Software encoders for the lane, best first.
SOFTWARE_AV1_ENCODERS: Tuple[str, ...] = ("libsvtav1", "libaom-av1")

PROBE_TIMEOUT_SECONDS = 8.0
# av1_amf rejects frames narrower than 320 px, so the test clip is 640x360.
PROBE_SOURCE = "testsrc2=size=640x360:rate=30"
PROBE_FRAMES = 8

CACHE_FILE_NAME = "av1_hardware_check.json"
_CACHE_VERSION = 1
_CACHE_MAX_ENTRIES = 64
# A pass is trusted for a month and a failure for a week. A runtime failure
# never rewrites the file: the job falls back on its own.
_PASS_TTL_SECONDS = 30 * 24 * 3600
_FAIL_TTL_SECONDS = 7 * 24 * 3600

REASON_OK = "ok"
REASON_FAILED = "failed"
REASON_BUSY = "busy"
REASON_TIMEOUT = "timeout"
REASON_ERROR = "error"
REASON_NOT_LISTED = "not-listed"
# Only definite answers are written to disk. A timeout, a crash of the probe
# itself, or a failure that reads like a busy GPU is kept for this session
# only, so the next launch checks again.
_PERSISTED_REASONS = {REASON_OK, REASON_FAILED}
# FFmpeg/driver wording for a GPU that is there but cannot take another
# session right now (NVENC's session limit reports "out of memory" or
# "incompatible client key"; AMF and QSV say busy or out of memory).
_TRANSIENT_MARKERS = (
    "out of memory",
    "busy",
    "too many",
    "temporarily",
    "try again",
    "incompatible client key",
    "device removed",
    "device lost",
    "device hung",
)

_VENDOR_BY_ENCODER = {
    "av1_nvenc": "nvidia",
    "av1_qsv": "intel",
    "av1_amf": "amd",
}


def _hidden_window_flag() -> int:
    if platform.system() != "Windows":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))


@dataclass(frozen=True)
class RunOutput:
    """What a probe runner reports for one FFmpeg run."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


# A runner takes the argv and a timeout in seconds. It raises
# ``subprocess.TimeoutExpired`` on timeout and ``OSError`` when FFmpeg is missing.
Runner = Callable[[Sequence[str], float], RunOutput]


# FFmpeg test encodes still running, so app exit can stop them. A probe runs
# on a daemon thread; without this a hung driver's FFmpeg would outlive the app.
_RUNNING: Set[subprocess.Popen] = set()
_RUNNING_LOCK = threading.Lock()


def subprocess_runner(cmd: Sequence[str], timeout: float) -> RunOutput:
    """Run FFmpeg with no stdin and no console window. The real runner.

    A run past ``timeout`` is killed and ``subprocess.TimeoutExpired`` raised.
    """

    proc = subprocess.Popen(
        list(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_hidden_window_flag(),
    )
    with _RUNNING_LOCK:
        _RUNNING.add(proc)
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
        return RunOutput(proc.returncode, stdout or "", stderr or "")
    finally:
        with _RUNNING_LOCK:
            _RUNNING.discard(proc)


def kill_running_probes() -> int:
    """Kill any test-encode FFmpeg still running. Registered with ``atexit``."""

    with _RUNNING_LOCK:
        running = list(_RUNNING)
    killed = 0
    for proc in running:
        try:
            if proc.poll() is None:
                proc.kill()
                killed += 1
        except OSError:
            continue
    return killed


atexit.register(kill_running_probes)


# Looked up at call time, so tests/conftest.py can swap in a fake and no test
# starts a real hardware encoder by accident.
default_runner: Runner = subprocess_runner


def default_cache_path() -> Optional[Path]:
    """``%APPDATA%\\SquishIt\\av1_hardware_check.json``. Tests replace this."""

    try:
        from ..config import CONFIG_DIR
    except Exception:  # pragma: no cover - config import never fails in the app
        return None
    return Path(CONFIG_DIR) / CACHE_FILE_NAME


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one hardware AV1 test encode."""

    encoder: str
    ok: bool
    reason: str
    detail: str = ""
    seconds: float = 0.0
    cached: bool = False

    @property
    def vendor(self) -> str:
        return _VENDOR_BY_ENCODER.get(self.encoder, "")

    def describe(self) -> str:
        """Short plain text for ``cli.py --hardware``."""

        if self.ok:
            text = "works"
        elif self.reason == REASON_NOT_LISTED:
            text = "not in this FFmpeg"
        elif self.reason == REASON_TIMEOUT:
            text = "no answer before the time limit"
        elif self.reason == REASON_BUSY:
            text = "busy right now; checked again next launch"
        elif self.reason == REASON_ERROR:
            text = "could not run the check"
        else:
            text = "does not work on this PC"
        if self.detail and not self.ok and self.reason != REASON_NOT_LISTED:
            text = f"{text} ({self.detail})"
        if self.cached:
            text = f"{text} [saved result]"
        return text


def probe_command(ffmpeg_path: str, encoder: str) -> List[str]:
    """Encode a few generated frames with ``encoder`` and throw them away."""

    return [
        ffmpeg_path,
        "-hide_banner",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-f",
        "lavfi",
        "-i",
        PROBE_SOURCE,
        "-frames:v",
        str(PROBE_FRAMES),
        "-an",
        "-c:v",
        encoder,
        "-f",
        "null",
        "-",
    ]


def _frames_written(stdout: str) -> int:
    frames = 0
    for line in (stdout or "").splitlines():
        key, _, value = line.strip().partition("=")
        if key == "frame":
            try:
                frames = int(value.strip())
            except ValueError:
                continue
    return frames


def _short_error(stderr: str) -> str:
    """The most telling FFmpeg line, without the ``[encoder @ 0x...]`` prefix."""

    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return ""
    generic = (
        "could not open encoder",
        "error while opening encoder",
        "task finished with error",
        "terminating thread",
        "nothing was written",
        "error sending frames",
        "conversion failed",
    )
    chosen = next(
        (line for line in lines if not any(token in line.lower() for token in generic)),
        lines[0],
    )
    if chosen.startswith("[") and "]" in chosen:
        chosen = chosen.split("]", 1)[1].strip()
    return chosen[:160]


def remote_session() -> bool:
    """True inside a Remote Desktop session on Windows.

    AMF and QSV often cannot open the GPU over Remote Desktop. The check's
    saved answers are kept apart for remote and local sessions, so one remote
    launch does not turn hardware AV1 off for the next local one.
    """

    if platform.system() != "Windows":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.user32.GetSystemMetrics(0x1000))  # SM_REMOTESESSION
    except Exception:
        return os.environ.get("SESSIONNAME", "").upper().startswith("RDP")


def ffmpeg_build_id(ffmpeg_path: str) -> str:
    """Path, size, and modified time of the FFmpeg binary. Changes on any update."""

    resolved = ffmpeg_path
    if not os.path.isabs(resolved) and os.path.dirname(resolved) == "":
        resolved = shutil.which(ffmpeg_path) or ffmpeg_path
    try:
        stat = os.stat(resolved)
    except OSError:
        return f"{ffmpeg_path}|missing"
    return f"{os.path.normcase(os.path.abspath(resolved))}|{stat.st_size}|{int(stat.st_mtime)}"


class HardwareAv1Probe:
    """Thread-safe, cached test encodes for the hardware AV1 encoders.

    ``runner`` defaults to :data:`default_runner`. ``cache_path`` defaults to
    :func:`default_cache_path`, looked up each time the file is used (so a
    probe built before a test patched the path still honours the patch);
    pass ``None`` to keep results in memory only. ``gpu_signature`` returns
    the display adapters and driver versions so a driver update invalidates
    the saved answers.

    :meth:`check` may run FFmpeg and waits for a probe in flight, so it is for
    worker threads. :meth:`first_verified` and :meth:`peek` never run FFmpeg
    or wait for one, so the UI thread uses those.
    """

    _USE_DEFAULT = object()

    def __init__(
        self,
        ffmpeg_path: str,
        *,
        runner: Optional[Runner] = None,
        cache_path=_USE_DEFAULT,
        gpu_signature: Optional[Callable[[], str]] = None,
        timeout: float = PROBE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self._runner = runner
        self._cache_setting = cache_path
        self._gpu_signature = gpu_signature
        self.timeout = float(timeout)
        self._clock = clock
        self._results: Dict[str, ProbeResult] = {}
        # Guards _results and _encoder_locks only; never held during FFmpeg
        # or file I/O, so peek() never waits.
        self._lock = threading.Lock()
        self._file_lock = threading.Lock()
        self._encoder_locks: Dict[str, threading.Lock] = {}
        self._in_flight: Set[str] = set()
        self._build_id: Optional[str] = None

    @property
    def cache_path(self) -> Optional[Path]:
        if self._cache_setting is self._USE_DEFAULT:
            return default_cache_path()
        return self._cache_setting

    # -- public --------------------------------------------------------------

    def check(self, encoder: str, *, refresh: bool = False) -> ProbeResult:
        """Return the cached answer for ``encoder``, probing once if needed.

        ``refresh`` ignores both caches and probes again. Concurrent callers
        for the same encoder wait for the one probe in flight, so never call
        this on the UI thread. A broken cache file only costs a re-probe.
        """

        with self._lock_for(encoder):
            if not refresh:
                known = self.peek(encoder)
                if known is not None:
                    return known
            with self._lock:
                self._in_flight.add(encoder)
            try:
                return self._check_locked(encoder, refresh)
            finally:
                with self._lock:
                    self._in_flight.discard(encoder)

    def _check_locked(self, encoder: str, refresh: bool) -> ProbeResult:
        if not refresh:
            saved = self._safe_read_saved(encoder)
            if saved is not None:
                with self._lock:
                    self._results[encoder] = saved
                return saved
        result = self._probe(encoder)
        with self._lock:
            self._results[encoder] = result
        if result.reason in _PERSISTED_REASONS:
            try:
                self._write_saved(result)
            except Exception:
                logger.debug("Could not save the AV1 hardware check", exc_info=True)
        return result

    def peek(self, encoder: str) -> Optional[ProbeResult]:
        """The in-memory answer, or None. Never runs FFmpeg or waits for it."""

        with self._lock:
            return self._results.get(encoder)

    def in_flight(self, candidates: Sequence[str]) -> bool:
        """True while another caller is checking one of ``candidates``."""

        with self._lock:
            return any(encoder in self._in_flight for encoder in candidates)

    def first_working(self, candidates: Sequence[str]) -> Optional[str]:
        """First encoder in ``candidates`` whose test encode passed. May probe."""

        for encoder in candidates:
            if self.check(encoder).ok:
                return encoder
        return None

    def first_verified(self, candidates: Sequence[str]) -> Optional[str]:
        """``first_working`` from what is already known, without waiting.

        Returns None ("not verified yet") as soon as a candidate ahead of the
        first passing one has no answer, so the preference order holds.
        """

        for encoder in candidates:
            known = self.peek(encoder)
            if known is None:
                return None
            if known.ok:
                return encoder
        return None

    # -- probing -------------------------------------------------------------

    def _lock_for(self, encoder: str) -> threading.Lock:
        with self._lock:
            lock = self._encoder_locks.get(encoder)
            if lock is None:
                lock = self._encoder_locks[encoder] = threading.Lock()
            return lock

    def _probe(self, encoder: str) -> ProbeResult:
        runner = self._runner or default_runner
        cmd = probe_command(self.ffmpeg_path, encoder)
        started = time.perf_counter()
        try:
            output = runner(cmd, self.timeout)
        except subprocess.TimeoutExpired:
            result = ProbeResult(
                encoder,
                False,
                REASON_TIMEOUT,
                f"no answer after {self.timeout:g} s",
                time.perf_counter() - started,
            )
        except Exception as exc:  # FFmpeg missing, access denied, a runner bug
            result = ProbeResult(
                encoder,
                False,
                REASON_ERROR,
                str(exc)[:160],
                time.perf_counter() - started,
            )
        else:
            seconds = time.perf_counter() - started
            frames = _frames_written(output.stdout)
            if output.returncode == 0 and frames > 0:
                result = ProbeResult(encoder, True, REASON_OK, "", seconds)
            else:
                detail = _short_error(output.stderr)
                if not detail:
                    detail = (
                        f"FFmpeg exit code {output.returncode}"
                        if output.returncode
                        else "no frames were encoded"
                    )
                stderr = (output.stderr or "").lower()
                transient = any(marker in stderr for marker in _TRANSIENT_MARKERS)
                reason = REASON_BUSY if transient else REASON_FAILED
                result = ProbeResult(encoder, False, reason, detail, seconds)
        logger.info(
            "Hardware AV1 check %s: %s%s (%.2f s)",
            encoder,
            result.reason,
            f" - {result.detail}" if result.detail else "",
            result.seconds,
        )
        return result

    # -- disk cache ----------------------------------------------------------

    def _key(self, encoder: str) -> str:
        if self._build_id is None:
            self._build_id = ffmpeg_build_id(self.ffmpeg_path)
        signature = ""
        if self._gpu_signature is not None:
            try:
                signature = str(self._gpu_signature() or "")
            except Exception:
                logger.debug("GPU signature for the AV1 check failed", exc_info=True)
        return "\n".join((encoder, self._build_id, signature))

    def _load(self) -> dict:
        path = self.cache_path
        if path is None:
            return {}
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            return {}
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return {}
        return {key: entry for key, entry in entries.items() if isinstance(key, str)}

    @staticmethod
    def _number(value) -> Optional[float]:
        """A finite int/float from JSON, or None (bools and strings are not numbers)."""

        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else None

    def _valid_entry(self, entry, now: float) -> bool:
        """A fresh, well-formed saved answer. Anything else is ignored."""

        if not isinstance(entry, dict):
            return False
        ok = entry.get("ok")
        reason = entry.get("reason")
        checked_at = self._number(entry.get("checked_at"))
        if not isinstance(ok, bool) or reason not in _PERSISTED_REASONS or checked_at is None:
            return False
        if ok != (reason == REASON_OK):
            return False
        if not isinstance(entry.get("detail", ""), str):
            return False
        if "seconds" in entry and self._number(entry.get("seconds")) is None:
            return False
        age = now - checked_at
        ttl = _PASS_TTL_SECONDS if ok else _FAIL_TTL_SECONDS
        return 0 <= age <= ttl

    def _safe_read_saved(self, encoder: str) -> Optional[ProbeResult]:
        try:
            return self._read_saved(encoder)
        except Exception:
            logger.debug("Ignoring an unreadable AV1 hardware check entry", exc_info=True)
            return None

    def _read_saved(self, encoder: str) -> Optional[ProbeResult]:
        if self.cache_path is None:
            return None
        entry = self._load().get(self._key(encoder))
        if not self._valid_entry(entry, self._clock()):
            return None
        seconds = self._number(entry.get("seconds")) or 0.0
        return ProbeResult(
            encoder=encoder,
            ok=entry["ok"],
            reason=entry["reason"],
            detail=entry.get("detail", ""),
            seconds=max(0.0, seconds),
            cached=True,
        )

    def _write_saved(self, result: ProbeResult) -> None:
        path = self.cache_path
        if path is None:
            return
        now = self._clock()
        with self._file_lock:
            entries = {
                key: entry
                for key, entry in self._load().items()
                if self._valid_entry(entry, now)
            }
            entries[self._key(result.encoder)] = {
                "encoder": result.encoder,
                "ok": result.ok,
                "reason": result.reason,
                "detail": result.detail,
                "seconds": round(result.seconds, 3),
                "checked_at": now,
            }
            if len(entries) > _CACHE_MAX_ENTRIES:
                newest = sorted(entries.items(), key=lambda item: item[1]["checked_at"])
                entries = dict(newest[-_CACHE_MAX_ENTRIES:])
            target = Path(path)
            temp = target.with_name(f"{target.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                temp.write_text(
                    json.dumps({"version": _CACHE_VERSION, "entries": entries}, indent=1),
                    encoding="utf-8",
                )
                # Windows refuses the swap while another process reads the
                # file; a short retry covers that, then the answer stays in
                # memory only.
                for attempt in range(3):
                    try:
                        os.replace(temp, target)
                        break
                    except PermissionError:
                        if attempt == 2:
                            raise
                        time.sleep(0.05)
            except OSError:
                logger.debug("Could not save the AV1 hardware check", exc_info=True)
            finally:
                try:
                    temp.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# What Fast AV1 will do on this PC
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FastAv1Route:
    """The encoder Fast AV1 uses on this PC, for the UI and the CLI.

    ``kind`` is ``hardware`` (a verified GPU encoder), ``software``
    (``libsvtav1`` or ``libaom-av1``), or ``fallback`` (no AV1 encoder in
    this FFmpeg, so ``fallback_codec`` is used instead).
    """

    kind: str
    encoder: str
    fallback_codec: str = ""
    hardware_checked: bool = False

    @property
    def vendor(self) -> str:
        return _VENDOR_BY_ENCODER.get(self.encoder, "")


# ---------------------------------------------------------------------------
# CPU speed policy
# ---------------------------------------------------------------------------

_PIXELS_1080P = 1920 * 1080

# Slowest SVT-AV1 preset for a given number of "1080p-equivalent" logical
# CPUs. Measured with SVT-AV1 4.2 on Zen 5, then halved per core so an older
# CPU still lands near real time: at 1080p, preset 8 runs 1.1x on 2 CPUs,
# 2.2x on 4, 4.0x on 8 and 6.4x on 16; preset 9 1.5x / 3.0x / 5.4x / 8.2x;
# preset 10 2.3x / 4.6x / 7.6x / 10.4x. 4K counts as a quarter of the CPUs.
_SVT_PRESET_FLOORS: Tuple[Tuple[float, int], ...] = (
    (8.0, 8),
    (4.0, 9),
    (2.0, 10),
)
_SVT_FASTEST = 12

# Peak FFmpeg memory (GB) of a 1080p SVT-AV1 encode by level of parallelism
# (SVT-AV1 2.0+ reads lp as a level 1-6). 4K measured about 3.3x of these.
_SVT_MEMORY_GB_1080P = {1: 0.5, 2: 0.6, 3: 0.75, 4: 0.95, 5: 1.3, 6: 1.8}
_SVT_MEMORY_SHARE = 0.45


def output_pixels(width: int, height: int, max_height: Optional[int] = None) -> int:
    """Pixels per frame after the ``max_resolution`` cap; 1080p when unknown."""

    w = int(width or 0)
    h = int(height or 0)
    if w <= 0 or h <= 0:
        return _PIXELS_1080P
    if max_height and h > max_height:
        w = int(round(w * max_height / h))
        h = int(max_height)
    return max(1, w * h)


def svt_preset_floor(cpu_threads: int, pixels: int) -> int:
    """Slowest SVT-AV1 preset this CPU can run near real time at ``pixels``."""

    scale = max(0.25, float(pixels or _PIXELS_1080P) / _PIXELS_1080P)
    effective = max(1, int(cpu_threads or 1)) / scale
    for minimum_cpus, preset in _SVT_PRESET_FLOORS:
        if effective >= minimum_cpus:
            return preset
    return _SVT_FASTEST


def fast_av1_svt_preset(requested: int, cpu_threads: int, pixels: int) -> int:
    """The ladder preset, made faster when this PC needs it. Never slower."""

    return max(0, min(13, max(int(requested), svt_preset_floor(cpu_threads, pixels))))


def svt_lp_memory_cap(pixels: int, total_ram_gb: float) -> Optional[int]:
    """Highest SVT-AV1 ``lp`` that fits in about 45% of RAM, or None for no cap.

    Only large frames on small machines are capped: a 4K encode at full
    parallelism peaks above 5 GB.
    """

    ram = float(total_ram_gb or 0.0)
    if ram <= 0:
        return None
    budget = ram * _SVT_MEMORY_SHARE
    scale = max(1.0, float(pixels or _PIXELS_1080P) / _PIXELS_1080P) ** 0.86
    if _SVT_MEMORY_GB_1080P[6] * scale <= budget:
        return None
    for level in (5, 4, 3, 2):
        if _SVT_MEMORY_GB_1080P[level] * scale <= budget:
            return level
    return 1


def aom_tiles(width: int, height: int) -> Optional[str]:
    """``-tiles`` for libaom row/tile threading: more tiles for larger frames."""

    w = int(width or 0)
    h = int(height or 0)
    if w <= 0 or h <= 0:
        return "2x2"
    if w >= 3000 or h >= 1700:
        return "4x2"
    if w >= 1200 or h >= 700:
        return "2x2"
    if w >= 640:
        return "2x1"
    return None
