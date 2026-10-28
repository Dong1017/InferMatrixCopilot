"""Graceful shutdown of a run on SIGINT/SIGTERM (run-20261027-193251: the
engine inherited SIG_IGN for SIGINT from its nohup launcher, so the GPU
scheduler's `--timeout` SIGINT did nothing and the reservation guard then
killed the test processes underneath it one by one).

Contract under test (engine/lifecycle.py + app/workflow_execution.py +
rebase_engine/module_rebase.py + engine/steps/rebase_v3.py):
  * handlers are installed over an inherited SIG_IGN and restored after;
  * the shutdown worker terminates registered harness children (whole
    tree), runs the module hooks, and exits `128 + signal` at the hard
    deadline if the run does not unwind;
  * the in-flight module is left `interrupted` (never `running`/`done`)
    with a `module_interrupted` trace event, on both the cooperative and
    the cancelled path;
  * the module step BLOCKS on `interrupted`, the wave gate treats it as
    not-done, and the substate short-circuit re-runs it on resume;
  * the run is marked `interrupted` in run_status.json with this pid.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from infermatrix_copilot import run_status as rs
from infermatrix_copilot.engine import lifecycle
from infermatrix_copilot.rebase_engine.substate import Substate
from infermatrix_copilot.run_trace import RunTrace


@pytest.fixture(autouse=True)
def _clean_shutdown_state():
    lifecycle._reset_shutdown_state_for_tests()
    yield
    lifecycle._reset_shutdown_state_for_tests()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- handler installation -------------------------------------------------------

def test_handlers_override_inherited_sig_ign_and_restore(tmp_path):
    """The launcher's `nohup ... &` leaves SIGINT ignored; Python keeps the
    inherited disposition, so `install_run_signal_handlers` must install
    over SIG_IGN unconditionally — and put it back when the run ends."""
    before_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
    before_term = signal.getsignal(signal.SIGTERM)
    try:
        restore = lifecycle.install_run_signal_handlers(tmp_path, "run-x")
        assert callable(signal.getsignal(signal.SIGINT))
        assert signal.getsignal(signal.SIGINT) is not signal.SIG_IGN
        assert callable(signal.getsignal(signal.SIGTERM))
        restore()
        assert signal.getsignal(signal.SIGINT) is signal.SIG_IGN
        assert signal.getsignal(signal.SIGTERM) == before_term
    finally:
        signal.signal(signal.SIGINT, before_int)


def test_handler_records_request_with_run_identity(tmp_path, monkeypatch, capsys):
    """Delivering the signal to this process accepts exactly one request
    (repeats are counted, not restarted) carrying the run dir and id, and
    the exit code follows the shell convention 128 + signal."""
    monkeypatch.setattr(lifecycle, "request_shutdown",
                        lambda signum, **kw: lifecycle.ShutdownRequest(
                            signum=signum, **{k: v for k, v in kw.items()
                                              if k in ("run_dir", "run_id")}))
    restore = lifecycle.install_run_signal_handlers(tmp_path, "run-sig",
                                                    signals=(signal.SIGTERM,))
    try:
        handler = signal.getsignal(signal.SIGTERM)
        req = handler(signal.SIGTERM, None)
    finally:
        restore()
    assert req.signal_name == "SIGTERM" and req.exit_code == 143
    assert req.run_dir == tmp_path and req.run_id == "run-sig"
    assert "resumable: --resume run-sig" in capsys.readouterr().out


def test_request_is_idempotent_and_counts_repeats():
    exits: list[int] = []
    first = lifecycle.request_shutdown(signal.SIGINT, hard_deadline_s=0.0,
                                       grace_s=0.1, exit_fn=exits.append)
    again = lifecycle.request_shutdown(signal.SIGTERM, hard_deadline_s=0.0,
                                       grace_s=0.1, exit_fn=exits.append)
    assert again is first and first.repeats == 1
    assert first.signum == signal.SIGINT and first.exit_code == 130
    assert lifecycle.shutdown_requested() is first


# -- the shutdown worker --------------------------------------------------------

def test_worker_terminates_registered_child_tree_then_exits(tmp_path):
    """A registered harness child (stand-in for cursor-agent) and ITS child
    (stand-in for the MCP bridge) are both gone after the request; with
    the run task never unwinding, the worker exits with 128 + signal."""
    pidfile = tmp_path / "grandchild.pid"
    proc = subprocess.Popen(
        ["bash", "--norc", "-c",
         f"sleep 300 & echo $! > {pidfile}; wait"],
        start_new_session=True)
    lifecycle.register_child(proc)
    deadline = time.monotonic() + 10
    while not pidfile.exists() or not pidfile.read_text().strip():
        assert time.monotonic() < deadline, "grandchild never started"
        time.sleep(0.05)
    grandchild = int(pidfile.read_text().strip())
    exited = threading.Event()
    codes: list[int] = []

    def fake_exit(code: int) -> None:
        codes.append(code)
        exited.set()

    # a bound loop that never unwinds: the hard deadline must fire
    loop = asyncio.new_event_loop()
    try:
        task_holder = {}

        async def park():
            task_holder["t"] = asyncio.current_task()
            await asyncio.sleep(3600)

        t = loop.create_task(park())
        loop.run_until_complete(asyncio.sleep(0))
        lifecycle.bind_run_task(loop, t)
        lifecycle.request_shutdown(signal.SIGTERM, run_dir=tmp_path, run_id="run-w",
                                   hard_deadline_s=1.0, grace_s=0.5,
                                   exit_fn=fake_exit)
        assert exited.wait(timeout=30), "worker never reached exit_fn"
        assert codes == [143]
        assert proc.wait(timeout=10) < 0  # killed by signal, not exited
        for _ in range(50):
            if not _alive(grandchild):
                break
            time.sleep(0.1)
        assert not _alive(grandchild), "bridge stand-in survived the shutdown"
        # the bound task was asked to cancel
        loop.run_until_complete(asyncio.sleep(0))
        assert t.cancelled() or t.cancelling()
    finally:
        t.cancel()
        try:
            loop.run_until_complete(asyncio.sleep(0))
        except BaseException:  # noqa: BLE001
            pass
        loop.close()


def test_worker_runs_hooks_and_returns_when_run_unwinds():
    """With no bound run task (the run left the loop), the worker runs the
    hooks and returns WITHOUT calling exit — the main thread finishes the
    terminal protocol itself."""
    seen: list[str] = []
    exits: list[int] = []
    lifecycle.register_shutdown_hook(lambda req: seen.append(req.signal_name))
    lifecycle.request_shutdown(signal.SIGINT, hard_deadline_s=5.0, grace_s=0.1,
                               exit_fn=exits.append)
    deadline = time.monotonic() + 5
    while not seen and time.monotonic() < deadline:
        time.sleep(0.02)
    assert seen == ["SIGINT"]
    time.sleep(0.3)
    assert exits == []


def test_run_guarded_binds_and_unbinds_the_run_task(tmp_path):
    """`run_guarded` makes its own task cancellable by the shutdown worker
    and forgets it on exit — the worker's "run left the loop" check."""
    seen = {}

    async def body():
        seen["bound"] = lifecycle._run_loop is not None
        return "ok"

    assert asyncio.run(lifecycle.run_guarded(body(), tmp_path)) == "ok"
    assert seen["bound"] is True
    assert lifecycle._run_loop is None


def test_run_guarded_cancellation_still_finalizes(tmp_path):
    """The forced path: the task is cancelled mid-run; finalizers (GPU lock
    release, scratch teardown) still run, then CancelledError propagates."""
    done = []
    lifecycle.register_finalizer(tmp_path, lambda outcome: done.append(outcome))

    async def body():
        lifecycle.request_shutdown(signal.SIGINT, hard_deadline_s=30, grace_s=0.1,
                                   exit_fn=lambda code: None)
        await asyncio.sleep(30)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(lifecycle.run_guarded(body(), tmp_path))
    assert len(done) == 1 and done[0] is None


# -- harness transport child registration -----------------------------------------

def test_run_harness_cli_registers_child_and_kills_tree_on_timeout():
    from infermatrix_copilot.providers.base import run_harness_cli

    registered: list[int] = []
    orig = lifecycle.register_child

    def spy(proc):
        registered.append(proc.pid)
        orig(proc)
    lifecycle.register_child = spy
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_harness_cli(
                ["bash", "--norc", "-c", "sleep 300 & wait"],
                input="", cwd=None, env=None, timeout=0.5)
    finally:
        lifecycle.register_child = orig
    assert len(registered) == 1
    assert lifecycle.registered_children() == []  # unregistered after


def test_run_harness_cli_returns_completed_process_and_unregisters():
    from infermatrix_copilot.providers.base import run_harness_cli
    cp = run_harness_cli([sys.executable, "-c",
                          "import sys; print(sys.stdin.read().upper())"],
                         input="hello", cwd=None, env=None, timeout=30)
    assert cp.returncode == 0 and cp.stdout.strip() == "HELLO"
    assert lifecycle.registered_children() == []


# -- run status + workflow terminal protocol ----------------------------------------

def test_mark_interrupted_stamps_this_pid_and_run_id(tmp_path):
    run_dir = tmp_path / "run-20261027-000000-abcdef"
    run_dir.mkdir()
    rec = rs.mark_interrupted(run_dir, note="SIGINT")
    assert rec["state"] == rs.INTERRUPTED
    assert rec["child_pid"] == os.getpid()      # not a never-launched reservation
    assert rec["run_id"] == run_dir.name
    assert rec["note"] == "SIGINT"
    assert rs.read_status(run_dir)["state"] == rs.INTERRUPTED


def test_workflow_interrupted_protocol(tmp_path, capsys):
    from infermatrix_copilot.app.workflow_execution import WorkflowExecution
    run_dir = tmp_path / "run-20261027-000000-abcdef"
    run_dir.mkdir()
    trace = RunTrace(run_dir / "run_trace.jsonl")
    req = lifecycle.ShutdownRequest(signum=signal.SIGINT, run_dir=run_dir,
                                    run_id=run_dir.name)
    outcome = SimpleNamespace(step_results={
        "a": SimpleNamespace(ok=True, summary="done"),
        "b": SimpleNamespace(ok=False, summary="interrupted")})
    result = WorkflowExecution._interrupted(run_dir, trace, req, outcome)
    assert result.exit_code == 130
    assert "resumable" in result.blocked_reason
    assert rs.read_status(run_dir)["state"] == rs.INTERRUPTED
    [ev] = trace.events("run_interrupted")
    assert ev["signal"] == "SIGINT" and ev["exit_code"] == 130
    assert ev["steps_completed"] == ["a"]
    out = capsys.readouterr().out
    assert "interrupted (SIGINT)" in out


# -- module loop: interrupted substate --------------------------------------------------

def _module_setup(tmp_path, monkeypatch, attempts):
    """`rebase_module` with the harness attempt replaced: `attempts` is a
    list of coroutine factories consumed in order."""
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    from infermatrix_copilot.rebase_engine.prompt_builder import ModulePromptData

    run_dir = tmp_path / "run"
    (run_dir / "signals").mkdir(parents=True)
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "module.tmpl").write_text("module {MODULE_NAME}\n")
    (templates / "debug.tmpl").write_text("debug {module}: {traceback}\n")
    data = ModulePromptData(
        templates_dir=templates, template="module.tmpl",
        pytest_wrapper="pytest", template_live="module.tmpl",
        debug_prompt_template="debug {module}: {traceback}",
        module_vllm_paths={"mod": "vllm/x"}, module_omni_files={"mod": "omni/x"},
        module_test_map={"mod": ["tests/x"]})
    config = mr.ModuleRunConfig(
        vllm_path=str(tmp_path), omni_path=str(tmp_path), script_dir=str(tmp_path),
        model="m", log_dir=str(run_dir), signal_dir=str(run_dir / "signals"),
        backend="cursor", max_debug_retries=3)
    queue = list(attempts)

    async def fake_attempt(prompt, **kw):
        return await queue.pop(0)()
    monkeypatch.setattr(mr, "_harness_attempt", fake_attempt)
    monkeypatch.setattr(mr, "build_module_prompt", lambda *a, **kw: "prompt")
    monkeypatch.setattr(mr, "build_debug_prompt", lambda *a, **kw: "debug")
    sub = Substate(run_dir, "run-int")
    trace = RunTrace(run_dir / "run_trace.jsonl")

    def run():
        return asyncio.run(mr.rebase_module(
            "mod", client=None, config=config, prompt_data=data, tool_defs=[],
            extra_tools={}, substate=sub, trace=trace))
    return run, sub, trace


def test_module_cooperative_interrupt_marks_interrupted_without_debug_retry(
        tmp_path, monkeypatch):
    """The attempt fails while a shutdown is pending: no debug retry, the
    module is `interrupted` (not failed/running) with the trace event."""
    async def failing():
        lifecycle.request_shutdown(signal.SIGTERM, hard_deadline_s=30, grace_s=0.1,
                                   exit_fn=lambda code: None)
        return {"done": False, "text": "cut", "turns": 3, "plan_done": False}

    async def never():
        raise AssertionError("debug retry ran during shutdown")

    run, sub, trace = _module_setup(tmp_path, monkeypatch, [failing, never])
    outcome = run()
    assert outcome["status"] == "interrupted"
    assert outcome["interrupted_by"] == "SIGTERM"
    assert sub.read()["modules"]["mod"]["status"] == "interrupted"
    [ev] = trace.events("module_interrupted")
    assert ev["signal"] == "SIGTERM" and ev["step"] == "rebase.module.mod"
    assert "interrupted by SIGTERM" in (tmp_path / "run" / "agents" /
                                        "module-mod.log").read_text()


def test_module_completed_before_signal_stays_done(tmp_path, monkeypatch):
    """A module whose attempt finished `done` just as the signal arrived
    keeps its result — interrupting must not discard verified work."""
    async def done():
        lifecycle.request_shutdown(signal.SIGINT, hard_deadline_s=30, grace_s=0.1,
                                   exit_fn=lambda code: None)
        return {"done": True, "text": "ok", "turns": 2, "plan_done": True}

    # cooperative path only: the worker thread's hook would otherwise race
    # the attempt's return (the hook guards on in-flight; see the hook test)
    monkeypatch.setattr(lifecycle, "_shutdown_worker", lambda *a, **kw: None)
    run, sub, trace = _module_setup(tmp_path, monkeypatch, [done])
    assert run()["status"] == "done"
    assert sub.read()["modules"]["mod"]["status"] == "done"
    assert list(trace.events("module_interrupted")) == []


def test_module_cancelled_mid_attempt_marks_interrupted_and_reraises(
        tmp_path, monkeypatch):
    """The forced path: the run task is cancelled while the harness attempt
    is in flight; the module is marked and CancelledError propagates."""
    async def hanging():
        lifecycle.request_shutdown(signal.SIGINT, hard_deadline_s=30, grace_s=0.1,
                                   exit_fn=lambda code: None)
        raise asyncio.CancelledError()

    run, sub, trace = _module_setup(tmp_path, monkeypatch, [hanging])
    with pytest.raises(asyncio.CancelledError):
        run()
    assert sub.read()["modules"]["mod"]["status"] == "interrupted"
    assert sub.read()["modules"]["mod"]["interrupted_by"] == "SIGINT"
    assert len(list(trace.events("module_interrupted"))) == 1
    assert lifecycle._shutdown_hooks == []        # hook unregistered on exit


def test_module_shutdown_hook_marks_in_flight_module_once(tmp_path, monkeypatch):
    """While an attempt is in flight the module loop has a hook registered;
    the shutdown worker calling it leaves the same durable mark, and a
    second call (hook + cooperative path) does not duplicate the event."""
    captured = {}

    async def in_flight():
        [hook] = list(lifecycle._shutdown_hooks)
        req = lifecycle.ShutdownRequest(signum=signal.SIGTERM)
        hook(req)
        hook(req)
        captured["during"] = Substate(tmp_path / "run", "run-int").read()
        lifecycle.request_shutdown(signal.SIGTERM, hard_deadline_s=30, grace_s=0.1,
                                   exit_fn=lambda code: None)
        return {"done": False, "text": "", "turns": 0, "plan_done": False}

    monkeypatch.setattr(lifecycle, "_shutdown_worker", lambda *a, **kw: None)
    run, sub, trace = _module_setup(tmp_path, monkeypatch, [in_flight])
    assert run()["status"] == "interrupted"
    assert captured["during"]["modules"]["mod"]["status"] == "interrupted"
    assert len(list(trace.events("module_interrupted"))) == 1


def test_module_shutdown_hook_ignores_an_attempt_that_already_returned(
        tmp_path, monkeypatch):
    """After the attempt returned, the hook defers to the cooperative path:
    a late worker thread must not flip a finished module."""
    hooks = {}

    async def finished():
        hooks["hook"] = list(lifecycle._shutdown_hooks)[0]
        return {"done": True, "text": "ok", "turns": 1, "plan_done": True}

    monkeypatch.setattr(lifecycle, "_shutdown_worker", lambda *a, **kw: None)
    run, sub, trace = _module_setup(tmp_path, monkeypatch, [finished])
    assert run()["status"] == "done"
    hooks["hook"](lifecycle.ShutdownRequest(signum=signal.SIGTERM))
    assert sub.read()["modules"]["mod"]["status"] == "done"
    assert list(trace.events("module_interrupted")) == []


# -- step + wave gate + resume --------------------------------------------------------

def test_wave_gate_treats_interrupted_as_not_done(tmp_path, settings, trace):
    from infermatrix_copilot.engine.registry import StepRegistry
    from infermatrix_copilot.engine.step import StepContext
    from infermatrix_copilot.engine.steps import register_builtin_steps
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    Substate(run_dir, "run-g").update({"modules": {
        "a": {"status": "done"}, "b": {"status": "interrupted"}}})
    registry = register_builtin_steps(StepRegistry())
    ctx = StepContext(settings=settings, params={}, run_dir=run_dir, trace=trace,
                      state={"task_spec": {"repo": "vllm-omni", "params": {}},
                             "run_id": "run-g", "wave1_modules": ["a", "b"],
                             "wave2_modules": ["c"]})
    r = asyncio.run(registry.get("rebase.v3_wave_gate").handler(ctx))
    assert r.ok and r.outputs["state_updates"]["wave2_modules"] == []
    assert "b" in r.summary
