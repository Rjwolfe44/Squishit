"""Encode resource governor.

One owner for CPU thread budgets and hardware encode sessions. Callers ask
this module how many jobs may run and how many threads each job gets, then
apply that decision to the FFmpeg argv once.

Budget
    ``resolve_governor_mode`` maps the profile's ``auto|low|balanced|max``
    preference onto a concrete mode. ``recommend_thread_share`` is that mode's
    slice of the machine for one job in a wave of ``n_parallel``.
    ``threads_for_job`` applies the per-codec ceiling on top of that share.
    An explicit thread override is kept for a single job and is divided across
    the wave when more than one job runs, so a batch cannot give every job the
    full override.

Admission
    ``EncodeResourceGovernor`` is the live ledger. ``max_concurrent`` is the
    queue cap (software jobs share ``cpu_threads // 2`` slots; NVENC 3, QSV 2,
    AMF 2, VideoToolbox 2, and never more than the CPU cap). ``reserve`` blocks
    until a slot is free, then records the lease used to size that job's
    threads. ``release`` returns the slot.

Argv
    ``apply_thread_policy`` writes one thread control for the encoder.
    libsvtav1 gets ``lp=`` and no ``-threads``. libx264 and libx265 get one
    ``-threads`` value; ``threads``, ``pools``, ``frame-threads``,
    ``lookahead-threads``, and ``numa-pools`` are removed from their param
    strings so those flags cannot fight ``-threads``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple, Union


# Consumer GPUs stay stable with a small number of concurrent encode sessions.
# The CPU cap below can only lower these, never raise them.
HW_SESSION_LIMITS = {
    "nvenc": 3,
    "qsv": 2,
    "amf": 2,
    "videotoolbox": 2,
}

_GOVERNOR_SCALE = {
    "low": 0.45,
    "balanced": 0.7,
    "max": 1.0,
}

_X26X_THREAD_KEYS = {
    "threads",
    "pools",
    "frame-threads",
    "lookahead-threads",
    "numa-pools",
}

_HW_FAMILY_TOKENS = ("nvenc", "qsv", "amf", "videotoolbox")


@dataclass(frozen=True)
class MachineResources:
    """CPU size the governor budgets against. RAM only affects mode selection."""

    cpu_threads: int
    cpu_cores: int
    total_ram_gb: float


@dataclass(frozen=True)
class EncodeLease:
    """One running encode's share of the machine."""

    job_id: str
    threads: int
    hw_encoder: Optional[str]
    family: Optional[str]
    resolved_governor: str
    parallel_jobs: int


class EncodeCancelled(Exception):
    """Raised when a job is cancelled while waiting for an encode slot."""


ResourceSource = Union[MachineResources, Callable[[], MachineResources]]


def cpu_concurrency_cap(cpu_threads: int) -> int:
    """How many encodes may run before the CPU is split thinner than two threads."""

    threads = max(1, int(cpu_threads or 1))
    if threads < 2:
        return 1
    return max(1, threads // 2)


def encoder_family(hw_encoder: Optional[str]) -> Optional[str]:
    """Hardware session family, or None for a software encode."""

    if not hw_encoder:
        return None
    name = hw_encoder.lower()
    for token in _HW_FAMILY_TOKENS:
        if token in name:
            return token
    return "other"


def resolve_governor_mode(
    *,
    governor: str = "auto",
    n_parallel: int = 1,
    codec: Optional[str] = None,
    hw_encoder: Optional[str] = None,
    frame_height: int = 0,
    cpu_threads: int = 1,
    total_ram_gb: float = 0.0,
) -> str:
    """Resolve ``auto`` to ``low``, ``balanced``, or ``max`` for this workload."""

    normalized = (governor or "auto").lower()
    if normalized in {"low", "balanced", "max"}:
        return normalized

    codec_name = (codec or "").lower()
    heavy_cpu_codec = codec_name in {"av1", "svt-av1", "libaom-av1", "libsvtav1"} and not hw_encoder
    high_resolution = frame_height >= 2160
    logical = max(1, int(cpu_threads or 1))

    if n_parallel >= 4:
        return "low" if logical <= 12 else "balanced"

    if hw_encoder:
        if n_parallel >= 3:
            return "low"
        if high_resolution or total_ram_gb < 16:
            return "balanced"
        return "low" if codec_name in {"h264", "libx264"} else "balanced"

    if heavy_cpu_codec:
        if n_parallel == 1 and logical >= 12 and total_ram_gb >= 16:
            return "max"
        return "balanced"

    if codec_name in {"hevc", "libx265"}:
        return "balanced" if n_parallel <= 2 else "low"

    return "balanced"


def recommend_thread_share(
    *,
    cpu_threads: int,
    total_ram_gb: float,
    n_parallel: int = 1,
    governor: str = "auto",
    codec: Optional[str] = None,
    hw_encoder: Optional[str] = None,
    frame_height: int = 0,
) -> int:
    """Fair thread count for one job when ``n_parallel`` jobs run together."""

    resolved = resolve_governor_mode(
        governor=governor,
        n_parallel=n_parallel,
        codec=codec,
        hw_encoder=hw_encoder,
        frame_height=frame_height,
        cpu_threads=cpu_threads,
        total_ram_gb=total_ram_gb,
    )
    scale = _GOVERNOR_SCALE[resolved]
    if resolved == "max" and not hw_encoder and total_ram_gb < 16:
        scale = 0.85

    total_budget = max(1, int(max(1, int(cpu_threads or 1)) * scale))
    return max(1, int(total_budget / max(1, n_parallel)))


def threads_for_job(
    *,
    cpu_threads: int,
    cpu_cores: int,
    total_ram_gb: float,
    profile_threads: Optional[int],
    resource_governor: str,
    codec: str,
    hw_encoder: Optional[str],
    parallel_jobs: int = 1,
    frame_height: int = 0,
) -> int:
    """Thread count for one job. The fair share is a ceiling, not a suggestion."""

    codec_name = (codec or "").lower()
    n_parallel = max(1, int(parallel_jobs or 1))
    logical = max(1, int(cpu_threads or 1))
    x265_cap = 16 if (not hw_encoder and codec_name in {"hevc", "libx265"}) else None

    if profile_threads:
        requested = max(1, int(profile_threads))
        if n_parallel > 1:
            requested = min(requested, max(1, logical // n_parallel))
        if x265_cap is not None:
            requested = min(requested, x265_cap)
        return requested

    share = recommend_thread_share(
        cpu_threads=logical,
        total_ram_gb=total_ram_gb,
        n_parallel=n_parallel,
        governor=resource_governor,
        codec=codec_name,
        hw_encoder=hw_encoder,
        frame_height=frame_height,
    )
    share = max(1, share)
    physical_cores = max(1, min(max(1, int(cpu_cores or 1)), share))

    if hw_encoder:
        requested = max(2 if share >= 2 else 1, min(share, max(2, physical_cores)))
    elif codec_name in {"svt-av1", "libsvtav1"}:
        requested = max(2, share)
    elif codec_name in {"av1", "libaom-av1"}:
        requested = max(2, min(share, max(physical_cores, int(share * 0.85))))
    elif codec_name in {"hevc", "libx265"}:
        requested = max(physical_cores, int(share * 0.75))
    elif codec_name in {"h264", "libx264"}:
        requested = max(1, int(share * 0.9))
    elif codec_name in {"vp9", "libvpx-vp9"}:
        requested = max(1, min(share, 12))
    else:
        requested = share

    if x265_cap is not None:
        requested = min(requested, x265_cap)
    # Codec floors must not hand a job more threads than its share of the machine.
    return max(1, min(requested, share))


def apply_thread_policy(argv: Sequence[str], *, threads: int, encoder: str) -> List[str]:
    """Return argv with one thread control for ``encoder``.

    libsvtav1 uses ``lp`` inside ``-svtav1-params`` and does not also take
    ``-threads``. Every other encoder takes a single ``-threads`` value.
    x264/x265 param strings lose keys that would start a second thread pool.
    """

    threads = max(1, int(threads or 1))
    args = list(argv)
    if _is_svt(encoder):
        args = _drop_option(args, "-threads")
        return _upsert_svt_lp(args, threads)

    args = _strip_param_keys(args, "-x264-params", _X26X_THREAD_KEYS)
    args = _strip_param_keys(args, "-x265-params", _X26X_THREAD_KEYS)
    return _set_single_threads(args, threads)


class EncodeResourceGovernor:
    """Live CPU and hardware-session ledger for encodes on one machine."""

    def __init__(self, resources: ResourceSource):
        self._resources = resources
        self._leases: Dict[str, EncodeLease] = {}
        self._cv = threading.Condition()

    def machine(self) -> MachineResources:
        source = self._resources
        info = source() if callable(source) else source
        return MachineResources(
            cpu_threads=max(1, int(info.cpu_threads or 1)),
            cpu_cores=max(1, int(info.cpu_cores or 1)),
            total_ram_gb=float(info.total_ram_gb or 0.0),
        )

    def session_limit(self, hw_encoder: Optional[str] = None) -> int:
        """Concurrent encodes allowed for this encoder on the current machine."""

        info = self.machine()
        cpu_cap = cpu_concurrency_cap(info.cpu_threads)
        family = encoder_family(hw_encoder)
        if family is None:
            return cpu_cap
        family_cap = HW_SESSION_LIMITS.get(family, 1)
        return max(1, min(family_cap, cpu_cap, info.cpu_threads))

    def max_concurrent(
        self,
        requested: int,
        hw_encoder: Optional[str] = None,
        queued: Optional[int] = None,
    ) -> int:
        """How many jobs from this queue may run at once."""

        slots = min(max(1, int(requested or 1)), self.session_limit(hw_encoder))
        if queued is not None:
            slots = min(slots, max(1, int(queued or 1)))
        return slots

    def active_count(self) -> int:
        with self._cv:
            return len(self._leases)

    def holds(self, job_id: str) -> bool:
        with self._cv:
            return job_id in self._leases

    def reserve(
        self,
        job_id: str,
        *,
        declared_parallel: int,
        hw_encoder: Optional[str],
        codec: str,
        frame_height: int = 0,
        explicit_threads: Optional[int] = None,
        resource_governor: str = "auto",
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> EncodeLease:
        """Block until ``job_id`` can encode, then return its thread lease.

        Re-calling with the same id updates the encoder family (hardware to
        software, for example) without counting the job twice. The thread
        count uses ``max(declared_parallel, active leases)`` so a wave shares
        one budget and a job that starts beside an existing wave does not
        assume the whole machine.

        ``cancel_check`` must not call back into the governor.
        """

        while True:
            if cancel_check and cancel_check():
                with self._cv:
                    if self._leases.pop(job_id, None) is not None:
                        self._cv.notify_all()
                raise EncodeCancelled(job_id)

            with self._cv:
                previous = self._leases.pop(job_id, None)
                if self._can_run_locked(hw_encoder):
                    lease = self._assign_locked(
                        job_id,
                        declared_parallel=declared_parallel,
                        hw_encoder=hw_encoder,
                        codec=codec,
                        frame_height=frame_height,
                        explicit_threads=explicit_threads,
                        resource_governor=resource_governor,
                    )
                    self._leases[job_id] = lease
                    return lease
                if previous is not None:
                    # Could not switch families yet; the slot we just freed
                    # stays free so another job can take it.
                    self._cv.notify_all()
                self._cv.wait(timeout=0.25)

    def release(self, job_id: str) -> None:
        with self._cv:
            if self._leases.pop(job_id, None) is not None:
                self._cv.notify_all()

    def _cpu_cap(self) -> int:
        return cpu_concurrency_cap(self.machine().cpu_threads)

    def _can_run_locked(self, hw_encoder: Optional[str]) -> bool:
        """Caller holds the condition lock. ``job_id`` is not in ``_leases``."""

        if len(self._leases) >= self._cpu_cap():
            return False
        family = encoder_family(hw_encoder)
        if family is None:
            return True
        limit = self.session_limit(hw_encoder)
        running = sum(1 for lease in self._leases.values() if lease.family == family)
        return running < limit

    def _assign_locked(
        self,
        job_id: str,
        *,
        declared_parallel: int,
        hw_encoder: Optional[str],
        codec: str,
        frame_height: int,
        explicit_threads: Optional[int],
        resource_governor: str,
    ) -> EncodeLease:
        info = self.machine()
        active = len(self._leases) + 1
        effective = max(1, int(declared_parallel or 1), active)
        resolved = resolve_governor_mode(
            governor=resource_governor,
            n_parallel=effective,
            codec=codec,
            hw_encoder=hw_encoder,
            frame_height=frame_height,
            cpu_threads=info.cpu_threads,
            total_ram_gb=info.total_ram_gb,
        )
        threads = threads_for_job(
            cpu_threads=info.cpu_threads,
            cpu_cores=info.cpu_cores,
            total_ram_gb=info.total_ram_gb,
            profile_threads=explicit_threads,
            resource_governor=resource_governor,
            codec=codec,
            hw_encoder=hw_encoder,
            parallel_jobs=effective,
            frame_height=frame_height,
        )
        return EncodeLease(
            job_id=job_id,
            threads=threads,
            hw_encoder=hw_encoder,
            family=encoder_family(hw_encoder),
            resolved_governor=resolved,
            parallel_jobs=effective,
        )


def _is_svt(encoder: str) -> bool:
    name = (encoder or "").lower()
    return "svtav1" in name or name == "svt-av1"


def _drop_option(argv: List[str], flag: str) -> List[str]:
    output: List[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == flag and index + 1 < len(argv):
            index += 2
            continue
        output.append(argv[index])
        index += 1
    return output


def _split_params(text: str) -> List[Tuple[str, str]]:
    items: List[Tuple[str, str]] = []
    for part in text.split(":"):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            items.append((key, value))
        else:
            items.append((part, ""))
    return items


def _join_params(items: Sequence[Tuple[str, str]]) -> str:
    parts: List[str] = []
    for key, value in items:
        parts.append(f"{key}={value}" if value != "" else key)
    return ":".join(parts)


def _strip_param_keys(argv: List[str], flag: str, keys: set) -> List[str]:
    output: List[str] = []
    index = 0
    while index < len(argv):
        if argv[index] == flag and index + 1 < len(argv):
            kept = [(key, value) for key, value in _split_params(argv[index + 1]) if key not in keys]
            joined = _join_params(kept)
            if joined:
                output.extend([flag, joined])
            index += 2
            continue
        output.append(argv[index])
        index += 1
    return output


def _set_single_threads(argv: List[str], threads: int) -> List[str]:
    output: List[str] = []
    index = 0
    seen = False
    while index < len(argv):
        if argv[index] == "-threads" and index + 1 < len(argv):
            if not seen:
                output.extend(["-threads", str(threads)])
                seen = True
            index += 2
            continue
        output.append(argv[index])
        index += 1
    if seen:
        return output
    if output:
        output.insert(len(output) - 1, "-threads")
        output.insert(len(output) - 1, str(threads))
        return output
    return ["-threads", str(threads)]


def _upsert_svt_lp(argv: List[str], threads: int) -> List[str]:
    merged: List[Tuple[str, str]] = []
    positions: List[int] = []
    index = 0
    while index < len(argv):
        if argv[index] == "-svtav1-params" and index + 1 < len(argv):
            positions.append(index)
            for key, value in _split_params(argv[index + 1]):
                if key == "lp":
                    continue
                merged.append((key, value))
            index += 2
            continue
        index += 1

    merged.append(("lp", str(threads)))
    joined = _join_params(merged)
    if not positions:
        updated = list(argv)
        if updated:
            updated.insert(len(updated) - 1, "-svtav1-params")
            updated.insert(len(updated) - 1, joined)
            return updated
        return ["-svtav1-params", joined]

    output: List[str] = []
    index = 0
    placed = False
    first = positions[0]
    while index < len(argv):
        if argv[index] == "-svtav1-params" and index + 1 < len(argv):
            if index == first and not placed:
                output.extend(["-svtav1-params", joined])
                placed = True
            index += 2
            continue
        output.append(argv[index])
        index += 1
    return output
