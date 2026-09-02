"""One runner per worker name, enforced by the operating system.

Worker identity is the token, not the process (see `Runner._release_orphaned_leases`),
so two runners started with the same credentials are ONE node as far as the VPS is
concerned. That is not merely wasteful:

  * `_release_orphaned_leases` runs at startup and fails every job leased to the node.
    A sibling runner mid-job has its lease pulled out from under it, its `complete`
    call answered with 409, and its result discarded -- after the GPU work was done.
  * `attempts` was already incremented by the claim, so a job bounced this way a few
    times reaches `max_attempts` and is marked FAILED permanently, having never
    actually failed.
  * `current_job_count` becomes whichever process beat last, so the admin's worker
    panel reports a number that belongs to no one.

Observed on the desktop: three runners polling as `desktop-4070` for over a day. Two
were orphans -- one started by hand, one left behind when the Scheduled Task was
re-registered without stopping the running instance. Nothing surfaced it. The VPS sees
one worker name heartbeating, and the log is append-only so three writers just
interleave.

An OS lock rather than a pidfile: the kernel drops it when the process dies, however it
dies, so there is no stale lock to detect and no PID-reuse race to get wrong.

SCOPE: this is a single-machine guard. Two runners on two machines sharing one token
would still collide, and no amount of local locking can see that. Closing it needs the
VPS to arbitrate -- an instance id on `worker_nodes`, with a superseded runner told to
exit -- which is a schema and protocol change. Deliberately not done here.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

#: Byte 0 is the lock; everything from byte 1 is a human-readable note about who holds
#: it. They are separated because Windows byte-range locks block *reads* of the locked
#: region -- a second runner could not read the pid it was about to report if the two
#: shared a byte. On POSIX `flock` locks the whole file but does not affect reads, so
#: the same layout is harmless there.
_LOCK_BYTE = 1


class AlreadyRunningError(RuntimeError):
    """Another runner holds this worker name on this machine."""


def _lock_dir() -> Path:
    """A per-user directory, not /tmp: the lock name is predictable, and a world
    writable location would let any local account pre-create it and keep the runner
    from ever starting."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
        return Path(base) / "thedrop"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "thedrop"
    # Matches where the VPS keeps runtime state (see infrastructure/pm2).
    return Path.home() / ".local" / "state" / "thedrop"


def lock_path(worker_name: str) -> Path:
    """Deterministic path for a worker name.

    The readable part is truncated and sanitised for the filesystem, so the hash is
    what actually distinguishes two names -- otherwise `desktop/4070` and `desktop_4070`
    would collide and one runner would lock the other out for no reason.
    """
    slug = re.sub(r"[^A-Za-z0-9_.-]", "_", worker_name)[:40] or "worker"
    digest = hashlib.sha256(worker_name.encode("utf-8")).hexdigest()[:8]
    return _lock_dir() / f"runner-{slug}-{digest}.lock"


def _acquire(fd: int) -> None:
    """Raise OSError if another handle holds the lock. Never blocks."""
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def _read_holder(path: Path) -> str:
    """Best effort. The note is for a human reading an error message, so every failure
    to read it returns a vaguer message rather than masking the real one."""
    try:
        with path.open("rb") as handle:
            handle.seek(_LOCK_BYTE)
            return handle.read(200).decode("utf-8", "replace").strip()
    except OSError:
        return ""


@contextmanager
def single_instance(worker_name: str) -> Iterator[Path]:
    """Hold the lock for `worker_name`, or raise `AlreadyRunningError`.

    Only wraps the claim loop. `--check` deliberately does not take the lock: it makes
    no claims and changes nothing, and refusing to diagnose a worker because it is
    running would be exactly backwards.
    """
    path = lock_path(worker_name)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire(fd)
    except OSError as exc:
        holder = _read_holder(path)
        os.close(fd)
        raise AlreadyRunningError(
            f"another runner is already running as {worker_name!r} on this machine"
            + (f" ({holder})" if holder else "")
            + f". Lock: {path}"
        ) from exc

    note = (
        f"pid={os.getpid()} name={worker_name} "
        f"since={datetime.now(UTC).isoformat(timespec='seconds')}\n"
    ).encode()
    try:
        os.lseek(fd, _LOCK_BYTE, os.SEEK_SET)
        os.write(fd, note)
        os.ftruncate(fd, _LOCK_BYTE + len(note))
        yield path
    finally:
        # The file is left behind on purpose. Unlinking it would let a second runner
        # create and lock a NEW file at the same path while this one still holds the
        # old inode -- two runners, both holding "the" lock.
        try:
            _release(fd)
        finally:
            os.close(fd)
