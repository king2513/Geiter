"""Geiter: the store composition root.

The runtime keeps its state, analysis, actions, gate, and iteration
responsibilities in focused modules and composes them here. GeiterStore
remains the single public entry point, so importing from ``geiter.core`` and
calling any store method behaves exactly as it did before the split.
"""

from __future__ import annotations
import json
import hashlib
import time
import uuid
import functools
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from . import __version__
DEFAULT_ACTION_LEASE_SECONDS = 3600
try:  # pragma: no cover - platform import guard
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore
try:  # pragma: no cover - platform import guard
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - POSIX fallback
    msvcrt = None  # type: ignore
class FileLock:
    """A reentrant, cross-process advisory lock backed by a sidecar lock file.

    The lock protects the read-modify-write cycle around ``state.json`` so two
    agents (or two processes) cannot interleave and lose an update. It uses
    ``msvcrt.locking`` on Windows and ``fcntl.flock`` elsewhere, writing to a
    dedicated ``.geiter/state.lock`` file so the state file itself is never
    held open across the critical section.

    Reentrancy is tracked with thread-local state, not instance-wide state, so
    two threads sharing one store still contend for the real file lock while a
    single thread can safely nest calls such as ``iterate`` -> ``inspect`` ->
    ``add``.
    """

    def __init__(self, path: Path, timeout: float = 10.0, poll_interval: float = 0.01):
        self.path = path
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._local = threading.local()

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @_depth.setter
    def _depth(self, value: int) -> None:
        self._local.depth = value

    @property
    def _handle(self) -> Any:
        return getattr(self._local, "handle", None)

    @_handle.setter
    def _handle(self, value: Any) -> None:
        self._local.handle = value

    def _acquire_file(self, handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows path
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)

    def _release_file(self, handle: Any) -> None:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        elif msvcrt is not None:  # pragma: no cover - Windows path
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    def __enter__(self) -> FileLock:
        if self._depth > 0:
            self._depth += 1
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        handle = open(self.path, "a+b")
        while True:
            try:
                self._acquire_file(handle)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise TimeoutError(f"could not acquire geiter state lock: {self.path}")
                time.sleep(self.poll_interval)
        self._handle = handle  # type: ignore[misc]
        self._depth = 1
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._depth == 0:
            return
        self._depth -= 1
        if self._depth == 0:
            handle = self._handle
            if handle is not None:
                try:
                    self._release_file(handle)
                finally:
                    handle.close()
                    self._handle = None
def synchronized(method: Any) -> Any:
    """Serialize a mutating store method across processes.

    The wrapped method runs while holding the store's state lock, so a full
    read-modify-write cycle is atomic with respect to other processes.
    """

    @functools.wraps(method)
    def wrapper(self: GeiterStore, *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return method(self, *args, **kwargs)

    return wrapper


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Record:
    id: str
    created_at: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


from .actions import ActionsMixin
from .analysis import AnalysisMixin
from .baselines import BaselinesMixin
from .experiments import ExperimentsMixin
from .gate import GateMixin
from .iteration import IterationMixin
from .observations import ObservationsMixin
from .persistence import PersistenceMixin
from .runs import RunsMixin
from .surfaces import SurfacesMixin


class GeiterStore(
    PersistenceMixin,
    ObservationsMixin,
    RunsMixin,
    ActionsMixin,
    AnalysisMixin,
    BaselinesMixin,
    ExperimentsMixin,
    GateMixin,
    IterationMixin,
    SurfacesMixin,
):
    """The durable GEO store.

    Behavior is composed from the mixins above; this class exists to keep one
    import path and one public surface for agents and the CLI.
    """
