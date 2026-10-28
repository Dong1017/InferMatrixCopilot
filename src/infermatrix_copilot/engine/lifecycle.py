"""Run-lifecycle primitives: the same-run lock and the finalizer hook point.

Two guarantees the executor itself cannot give, because it knows nothing about
processes:

* `RunLock` — an exclusive advisory `flock` on `<run_dir>/.lock`. Two
  invocations of the same run (e.g. a second `--resume` while the first is
  still working) would interleave progress writes and share the checkout; the
  second holder fails fast instead.
* finalizers — per-run async callables invoked exactly once when the run
  leaves the event loop, whether it completed, failed, or raised. With nothing
  registered (every current playbook) the hook is a no-op; the rebase pipeline
  registers teardown (locks, scratch clones, store flushes) here so that
  resume, crash, and success all release resources the same way.
* graceful shutdown — `install_run_signal_handlers` turns SIGINT/SIGTERM into
  a bounded, ordered teardown of the run (harness children first, then the
  run's marked background processes, then the run's own finalizers via task
  cancellation), with a hard `os._exit` deadline so the process cannot hang
  on a child that ignores signals. A GPU scheduler ends a reservation with
  SIGINT; a launcher started with `nohup ... &` hands the process an
  INHERITED `SIG_IGN` for SIGINT, which Python keeps — so a run that does not
  install its own handler is simply unkillable by the scheduler
  (run-20261027-193251: `kill -INT` was a no-op; SIGTERM then orphaned the
  harness session and the detached test chain on the GPUs).
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

try:  # same guard as run_status.py: fcntl is POSIX-only
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore

Finalizer = Callable[[Any], Awaitable[None]]

_finalizers: dict[str, list[Finalizer]] = {}


class FileLockingUnavailable(RuntimeError):
    """This platform has no `fcntl`, so cross-process locking cannot be honored."""


def require_file_locking() -> None:
    """Raise unless POSIX advisory locking is available.

    The `fcntl = None` fallback above makes every lock in this file a silent
    no-op. That is tolerable for a single-process CLI run, where no cross-process
    lock was protecting anything anyway, but not for the paths whose *correctness*
    is the lock — worktree materialization, and (later) the idempotency index and
    its reaper. Those must fail closed rather than run unprotected and report a
    guarantee they are not providing (invariant 7)."""
    if fcntl is None:
        raise FileLockingUnavailable(
            "POSIX file locking (fcntl) is unavailable on this platform, so "
            "concurrent-safe worktrees and run reservations cannot be "
            "guaranteed; this surface refuses to run unprotected")


class RunLockHeld(RuntimeError):
    """Another process already holds this run's lock."""


class RunLock:
    """Exclusive advisory lock on `<run_dir>/.lock` for the duration of a run.

    `flock` contention is per open file description, so a second `acquire` on
    the same path — even inside the same process — fails while the first is
    held. The lock file is left in place after release: its existence carries
    no meaning, only the flock does."""

    def __init__(self, run_dir: Path):
        self.path = Path(run_dir) / ".lock"
        self._fd: int | None = None

    def acquire(self) -> "RunLock":
        if fcntl is None:  # non-POSIX: no advisory locking; keep runs working
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise RunLockHeld(
                f"run lock {self.path} is held by another process — "
                "is this run already executing (a concurrent --resume)?")
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "RunLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


def register_finalizer(run_dir: Path, fn: Finalizer) -> None:
    """Register an async finalizer for `run_dir`, called as `fn(outcome)` where
    outcome is the RunOutcome, or None if the executor raised before producing
    one. Finalizers run in registration order and exactly once per run."""
    _finalizers.setdefault(str(Path(run_dir)), []).append(fn)


async def finalize(run_dir: Path, outcome: Any) -> None:
    """Run and consume `run_dir`'s finalizers. Popping before running makes a
    second call a no-op, and a finalizer that raises never suppresses the
    run's own outcome or blocks its siblings."""
    for fn in _finalizers.pop(str(Path(run_dir)), []):
        try:
            await fn(outcome)
        except (Exception, asyncio.CancelledError):
            # CancelledError is BaseException: without naming it, a finalizer
            # awaiting an already-cancelled task would mask the run's outcome
            # and skip its siblings.
            pass


async def run_guarded(run: Awaitable[Any], run_dir: Path) -> Any:
    """Await the executor coroutine, then ALWAYS finalize — on completion,
    failure, or exception — inside the same event loop.

    Cancellation during teardown must not abandon it: `shield` alone raises in
    the outer task while the finalizer keeps running, and `asyncio.run`'s loop
    shutdown would then cancel that orphan mid-flight. So keep re-awaiting the
    shielded future until teardown actually completes, then let the
    cancellation propagate."""
    outcome: Any = None
    task = asyncio.current_task()
    if task is not None:  # a signal cancels THIS task, unwinding the executor
        bind_run_task(asyncio.get_running_loop(), task)
    try:
        outcome = await run
        return outcome
    finally:
        try:
            fin = asyncio.ensure_future(finalize(run_dir, outcome))
            cancelled = False
            while not fin.done():
                try:
                    await asyncio.shield(fin)
                except asyncio.CancelledError:
                    cancelled = True  # teardown still owed; keep waiting
        finally:
            _unbind_run_task()
        if cancelled:
            raise asyncio.CancelledError()


# ── graceful shutdown ─────────────────────────────────────────────────────────
#
# Process-wide by design: one CLI process runs one run, and a signal is a
# process-level event. Everything here is idempotent so the cooperative path
# (the module loop notices the request and records `interrupted`) and the
# forced path (task cancellation + hard deadline) may both run.

# Grace the shutdown worker gives harness children between SIGTERM and SIGKILL,
# and the hard ceiling after which the process `os._exit`s whatever is still
# pending. Both are deliberately short: the scheduler that sent the signal is
# about to reclaim the GPUs regardless.
CHILD_TERM_GRACE_S = 15.0
SHUTDOWN_HARD_DEADLINE_S = 60.0


@dataclass
class ShutdownRequest:
    """The one shutdown this process accepted: which signal, when, and the
    run it belongs to. Later signals of either kind are recorded but do not
    restart the sequence."""
    signum: int
    run_dir: Path | None = None
    run_id: str = ""
    requested_at: float = field(default_factory=time.time)
    repeats: int = 0

    @property
    def signal_name(self) -> str:
        try:
            return signal.Signals(self.signum).name
        except ValueError:  # pragma: no cover - unknown number
            return f"signal {self.signum}"

    @property
    def exit_code(self) -> int:
        """Shell convention: 128 + signal (130 for SIGINT, 143 for SIGTERM)."""
        return 128 + int(self.signum)


_shutdown: ShutdownRequest | None = None
_shutdown_lock = threading.Lock()
_children: dict[int, subprocess.Popen] = {}
_shutdown_hooks: list[Callable[[ShutdownRequest], None]] = []
_run_loop: tuple[asyncio.AbstractEventLoop, asyncio.Task] | None = None
_previous_handlers: dict[int, Any] = {}


def shutdown_requested() -> ShutdownRequest | None:
    """The accepted shutdown request, or None while the run is undisturbed.
    Long-running loops poll this between units of work (cooperative path)."""
    return _shutdown


def register_child(proc: subprocess.Popen) -> None:
    """Make a harness child (cursor-agent and its MCP bridge, claude, codex)
    reachable by the shutdown worker. `subprocess.run` hides the Popen, so
    transports spawn with Popen and register it for the call's duration."""
    with _shutdown_lock:
        _children[proc.pid] = proc


def unregister_child(proc: subprocess.Popen) -> None:
    with _shutdown_lock:
        _children.pop(proc.pid, None)


def registered_children() -> list[int]:
    with _shutdown_lock:
        return sorted(_children)


def register_shutdown_hook(fn: Callable[[ShutdownRequest], None]) -> None:
    """A synchronous hook the shutdown worker calls once (after the children
    are stopped). The module loop registers "record the in-flight module as
    interrupted" here so the forced path leaves the same durable mark as the
    cooperative one. Hooks must be idempotent and must never raise."""
    with _shutdown_lock:
        if fn not in _shutdown_hooks:
            _shutdown_hooks.append(fn)


def unregister_shutdown_hook(fn: Callable[[ShutdownRequest], None]) -> None:
    with _shutdown_lock:
        if fn in _shutdown_hooks:
            _shutdown_hooks.remove(fn)


def bind_run_task(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    """Tell the shutdown path which task to cancel: `run_guarded` binds its
    own task so a signal unwinds the executor through the finalizers."""
    global _run_loop
    _run_loop = (loop, task)


def _unbind_run_task() -> None:
    global _run_loop
    _run_loop = None


def terminate_children(*, grace_s: float = CHILD_TERM_GRACE_S) -> list[int]:
    """SIGTERM every registered child and its descendants, wait `grace_s`,
    SIGKILL the survivors. Returns pids still alive afterwards. The
    descendant walk matters: cursor-agent's MCP bridge is its child and
    would otherwise be reparented to init with the run's tool scope."""
    from ..testing.process_tree import kill_tree

    with _shutdown_lock:
        procs = list(_children.values())
    pids = [p.pid for p in procs if p.poll() is None]
    if not pids:
        return []
    return kill_tree(pids, term_grace=grace_s, kill_grace=2.0)


def _shutdown_worker(req: ShutdownRequest, *, hard_deadline_s: float,
                     grace_s: float, exit_fn: Callable[[int], None]) -> None:
    """Ordered teardown off the main thread: children → marked processes →
    hooks. Then wait for the main thread to unwind normally; past the hard
    deadline, exit the process regardless (a wedged executor thread must not
    turn a SIGINT into an immortal process)."""
    started = time.monotonic()
    try:
        terminate_children(grace_s=grace_s)
    except Exception:  # noqa: BLE001 - teardown never aborts on one failure
        pass
    if req.run_dir is not None and req.run_id:
        try:
            from ..rebase_engine.processes import cleanup_run_processes
            cleanup_run_processes(req.run_dir, req.run_id)
        except Exception:  # noqa: BLE001
            pass
    with _shutdown_lock:
        hooks = list(_shutdown_hooks)
    for fn in hooks:
        try:
            fn(req)
        except Exception:  # noqa: BLE001 - a hook must not stop the others
            pass
    remaining = hard_deadline_s - (time.monotonic() - started)
    deadline = time.monotonic() + max(0.0, remaining)
    while time.monotonic() < deadline:
        if _run_loop is None:  # the run left the event loop normally
            return
        time.sleep(0.2)
    try:
        terminate_children(grace_s=1.0)
    except Exception:  # noqa: BLE001
        pass
    exit_fn(req.exit_code)


def request_shutdown(signum: int, *, run_dir: Path | None = None,
                     run_id: str = "", hard_deadline_s: float = SHUTDOWN_HARD_DEADLINE_S,
                     grace_s: float = CHILD_TERM_GRACE_S,
                     exit_fn: Callable[[int], None] = os._exit) -> ShutdownRequest:
    """Accept a shutdown (idempotent): record it, start the worker thread and
    cancel the bound run task. Safe to call from a signal handler — it only
    sets state, starts a thread and schedules a thread-safe callback."""
    global _shutdown
    with _shutdown_lock:
        if _shutdown is not None:
            _shutdown.repeats += 1
            return _shutdown
        req = ShutdownRequest(signum=signum, run_dir=run_dir, run_id=run_id)
        _shutdown = req
    threading.Thread(
        target=_shutdown_worker, args=(req,),
        kwargs={"hard_deadline_s": hard_deadline_s, "grace_s": grace_s,
                "exit_fn": exit_fn},
        name="imx-shutdown", daemon=True).start()
    bound = _run_loop
    if bound is not None:
        loop, task = bound
        try:
            loop.call_soon_threadsafe(task.cancel)
        except RuntimeError:  # loop already closed
            pass
    return req


def install_run_signal_handlers(run_dir: Path, run_id: str, *,
                                signals: tuple[int, ...] = (signal.SIGINT, signal.SIGTERM),
                                ) -> Callable[[], None]:
    """Install SIGINT/SIGTERM handlers for a run and return the restorer.

    `signal.signal` is used unconditionally: an inherited `SIG_IGN` (nohup /
    non-interactive `&` launchers) is exactly the state this must override,
    and `asyncio.Runner` only installs its own SIGINT handler when the
    default one is still in place, so ours stays authoritative inside
    `asyncio.run`. Only the main thread may install handlers; elsewhere this
    is a no-op restorer."""
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    def _handler(signum, _frame):
        req = request_shutdown(signum, run_dir=run_dir, run_id=run_id)
        if req.repeats == 0:
            try:
                print(f"\n[run] {signal.Signals(signum).name} received — "
                      "stopping the harness session and this run's background "
                      f"processes (resumable: --resume {run_id})", flush=True)
            except (OSError, ValueError):  # stdout gone with the launcher
                pass
        return req

    previous: dict[int, Any] = {}
    for signum in signals:
        try:
            previous[signum] = signal.signal(signum, _handler)
        except (ValueError, OSError):  # pragma: no cover - unsupported signal
            continue
    _previous_handlers.update(previous)

    def _restore() -> None:
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (ValueError, OSError):  # pragma: no cover
                pass
            _previous_handlers.pop(signum, None)

    return _restore


def _reset_shutdown_state_for_tests() -> None:
    """Test seam: forget any accepted request, children and hooks."""
    global _shutdown, _run_loop
    with _shutdown_lock:
        _shutdown = None
        _children.clear()
        _shutdown_hooks.clear()
    _run_loop = None
