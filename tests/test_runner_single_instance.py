"""One runner per worker name, per machine.

Written after three agent-runners were found polling as `desktop-4070` for over a day
without anything surfacing it. Duplicates are not a tidiness problem: worker identity
is the token, so every runner sharing one is the same node, and `_release_orphaned_
leases` at startup fails every job leased to that node -- including work a sibling is
running right now. The sibling finishes, gets 409, and discards a completed result,
while `attempts` has already been spent.

The guard is an OS lock rather than a pidfile so it cannot go stale: the kernel drops
it when the process dies, whatever kills it.

These tests point the lock directory at tmp_path. Without that they would contend with
whatever runner is actually running on the machine executing them.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "services" / "agent-runner"))

from agent.single_instance import (  # noqa: E402
    AlreadyRunningError,
    lock_path,
    single_instance,
)


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both are set because `_lock_dir` chooses by platform, and these tests must not
    depend on which one they run under."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))


def _refuse_second(worker_name: str) -> AlreadyRunningError:
    """Take the lock a second time and return the refusal.

    The second acquisition happening *while* the first is held is the whole point, so
    it is written once here rather than repeated as nested `with` blocks in every test
    that needs it.
    """
    with pytest.raises(AlreadyRunningError) as caught, single_instance(worker_name):
        pass
    return caught.value


def test_a_second_runner_with_the_same_name_is_refused() -> None:
    """The invariant. Everything else here is detail."""
    with single_instance("desktop-4070"):
        _refuse_second("desktop-4070")


def test_different_worker_names_do_not_contend() -> None:
    """Two workers on one machine is a legitimate configuration -- the guard is per
    name, not per machine."""
    with single_instance("desktop-4070"), single_instance("desktop-3090"):
        pass


def test_the_lock_is_released_when_the_runner_exits() -> None:
    with single_instance("desktop-4070"):
        pass
    with single_instance("desktop-4070"):
        pass


def test_the_lock_is_released_even_when_the_runner_raises() -> None:
    """A runner that dies mid-loop must not lock its own replacement out. The OS
    guarantees this on process death; this covers the in-process path."""
    with pytest.raises(ZeroDivisionError), single_instance("desktop-4070"):
        _ = 1 / 0
    with single_instance("desktop-4070"):
        pass


def test_the_refusal_names_the_worker_and_the_holder() -> None:
    """An operator seeing this in a log needs to know which process to stop. The pid
    is read from the lock file WHILE it is locked, which is the part that is easy to
    get wrong: Windows byte-range locks block reads of the locked region, so the note
    is written past the locked byte."""
    with single_instance("desktop-4070"):
        message = str(_refuse_second("desktop-4070"))

    assert "desktop-4070" in message
    assert "pid=" in message
    assert str(lock_path("desktop-4070")) in message


def test_names_that_sanitise_alike_get_different_locks() -> None:
    """`desktop/4070` and `desktop_4070` both sanitise to `desktop_4070`. Without the
    hash they would share a lock and one would refuse the other for no reason."""
    assert lock_path("desktop/4070") != lock_path("desktop_4070")
    with single_instance("desktop/4070"), single_instance("desktop_4070"):
        pass


def test_the_lock_file_survives_release() -> None:
    """Unlinking on release would let a second runner create and lock a NEW file at the
    same path while the first still held the old inode -- two runners, both holding
    'the' lock."""
    with single_instance("desktop-4070") as path:
        assert path.exists()
    assert path.exists()


def test_main_exits_3_without_touching_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The integration point.

    The lock is taken before the client is built, so a refused start never reaches the
    VPS -- the API URL here is unroutable on purpose. Exit 3 rather than 2 because a
    supervisor must not treat "wait for the other one" like "this credential will never
    work".
    """
    import agent.__main__ as entry

    monkeypatch.setenv("THEDROP_API_URL", "https://unreachable.invalid")
    monkeypatch.setenv("WORKER_TOKEN", "irrelevant-the-lock-is-checked-first")
    monkeypatch.setenv("WORKER_NAME", "desktop-4070")

    # If the guard ever regresses, main() falls through into the claim loop, which
    # retries an unreachable API forever BY DESIGN -- so this test would hang instead
    # of failing, and stall CI rather than reporting. Replacing the constructor turns
    # that regression into an immediate, legible failure.
    def _must_not_be_reached(_config: object) -> object:
        raise AssertionError("the lock did not refuse; main() proceeded to run a runner")

    monkeypatch.setattr(entry, "build_runner", _must_not_be_reached)

    with single_instance("desktop-4070"):
        assert entry.main([]) == 3
