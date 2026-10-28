"""Tool-bridge liveness (run-20261027-193251: `infermatrix-tools` discovery
failed mid-session after a 30-minute synchronous plan review froze the
server's event loop; the harness then treated the server as gone for the
remaining hours, with no record of whether the bridge process had died).

Under test:
  * every registered handler is OFFLOADED to a worker thread, so a slow
    tool does not block another call or a ping (`_offload`);
  * the server process ignores SIGINT/SIGHUP (a stdio server lives as long
    as its stdin) and exits when stdin closes, writing start/exit (with a
    reason) to `bridge/<stem>.lifecycle.jsonl`;
  * slow calls are logged (`slow_tool`), which is the only client-side
    evidence of a deadline cut;
  * the module loop classifies the lifecycle log (`bridge_health`) and
    records `bridge_unavailable` with the bridge's exit reason.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from infermatrix_copilot.rebase_engine.module_rebase import bridge_health
from infermatrix_copilot.scopes import read_only_scope
from infermatrix_copilot.tool_bridge import (
    BridgeLifecycle,
    _offload,
    build_server,
    lifecycle_log_path,
    read_lifecycle_events,
    timed_call,
    write_bridge_spec,
)

ROOT = Path(__file__).resolve().parents[1]


def _spec(tmp_path: Path) -> tuple[Path, Path, Path]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "a.py").write_text("x = 1\n", encoding="utf-8")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scope = read_only_scope()
    scope = type(scope)(name=scope.name, allowed_tools=scope.allowed_tools,
                        read_only=True, root=str(worktree))
    spec = write_bridge_spec(run_dir=run_dir, step_name="rebase.module.x",
                             scope=scope, repo="demo")
    return spec, worktree, run_dir


# -- offloading -----------------------------------------------------------------

def test_offload_keeps_signature_and_runs_off_loop():
    import inspect

    def handler(path: str, offset: int = 0) -> str:
        return f"{threading.current_thread().name}:{path}:{offset}"

    wrapped = _offload(handler)
    assert inspect.iscoroutinefunction(wrapped)
    assert list(inspect.signature(wrapped).parameters) == ["path", "offset"]
    out = asyncio.run(wrapped("p", offset=3))
    assert out.endswith(":p:3")
    assert not out.startswith("MainThread")   # ran on a worker thread


def test_slow_tool_does_not_block_a_concurrent_call(tmp_path, monkeypatch):
    """The regression, through the real FastMCP server: with inline sync
    handlers the second call waits for the first; offloaded, `list_dir`
    returns while `read_file` is still blocked in its handler."""
    import infermatrix_copilot.tool_bridge as tb
    spec, _worktree, _run_dir = _spec(tmp_path)
    release = threading.Event()
    orig_make = tb.make_dispatcher

    def gated_make(*args, **kwargs):
        inner = orig_make(*args, **kwargs)

        def call(name, arguments):
            if name == "read_file":
                assert release.wait(timeout=10), "slow call never released"
            return inner(name, arguments)
        return call
    monkeypatch.setattr(tb, "make_dispatcher", gated_make)
    server = build_server(spec)

    async def scenario():
        slow = asyncio.ensure_future(server.call_tool("read_file", {"path": "a.py"}))
        fast = await asyncio.wait_for(
            server.call_tool("list_dir", {"path": "."}), timeout=10)
        assert not slow.done()          # still blocked — and we got served
        release.set()
        return fast, await asyncio.wait_for(slow, timeout=10)

    fast, slow = asyncio.run(scenario())
    assert "a.py" in str(fast) and "x = 1" in str(slow)


def test_offloaded_handlers_run_concurrently(tmp_path, monkeypatch):
    """Direct proof of concurrency: two offloaded calls into one blocking
    handler overlap in time (both are inside before either leaves)."""
    inside = threading.Barrier(2, timeout=10)

    def blocking(path: str) -> str:
        inside.wait()         # both calls must be inside simultaneously
        return path

    wrapped = _offload(blocking)

    async def both():
        return await asyncio.gather(wrapped("a"), wrapped("b"))

    assert asyncio.run(both()) == ["a", "b"]


# -- lifecycle log ----------------------------------------------------------------

def test_timed_call_logs_only_slow_calls(tmp_path):
    lifecycle = BridgeLifecycle(tmp_path / "x.lifecycle.jsonl")
    calls = []

    def call(name, args):
        calls.append(name)
        time.sleep(0.05 if name == "slow" else 0)
        return "ok"

    bound = timed_call(call, lifecycle, slow_after_s=0.02)
    assert bound("fast", {}) == "ok" and bound("slow", {}) == "ok"
    assert lifecycle.calls == 2
    events = read_lifecycle_events(tmp_path / "x.json")
    assert [e["kind"] for e in events] == ["slow_tool"]
    assert events[0]["tool"] == "slow" and events[0]["elapsed_s"] >= 0.05
    assert timed_call(call, None) is call


def test_lifecycle_write_failure_never_raises(tmp_path):
    lifecycle = BridgeLifecycle(tmp_path / "a.py" / "impossible.jsonl")
    (tmp_path / "a.py").write_text("")  # parent is a file: mkdir fails
    lifecycle.record("start")           # must not raise


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_bridge_process_survives_sigint_sighup_and_exits_on_stdin_close(tmp_path):
    """End-to-end on the real stdio server: SIGINT and SIGHUP are ignored
    (the process stays alive), closing stdin ends it with rc 0, and the
    lifecycle log carries start + exit(reason=stdin_closed)."""
    pytest.importorskip("mcp")
    spec, _worktree, run_dir = _spec(tmp_path)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(ROOT / "src"),
           "HOME": str(tmp_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "infermatrix_copilot.tool_bridge", "--spec", str(spec)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env, start_new_session=True)
    try:
        log = lifecycle_log_path(spec)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(e["kind"] == "start" for e in read_lifecycle_events(spec)):
                break
            time.sleep(0.1)
        else:
            pytest.fail(f"bridge never logged start: {proc.stderr.read()!r}")
        # dispositions are installed before the start event is written, so
        # the signals may go out as soon as it appears
        proc.send_signal(signal.SIGINT)
        proc.send_signal(signal.SIGHUP)
        time.sleep(1.0)
        assert proc.poll() is None, f"bridge died on SIGINT/SIGHUP: rc={proc.returncode}"
        proc.stdin.close()
        rc = proc.wait(timeout=30)
        assert rc == 0, proc.stderr.read()
        events = read_lifecycle_events(spec)
        kinds = [e["kind"] for e in events]
        assert kinds[0] == "start" and kinds[-1] == "exit"
        assert events[0]["pid"] == proc.pid and events[0]["spec"] == str(spec)
        assert events[-1]["reason"] == "stdin_closed"
        assert events[-1]["parent_alive"] is True   # we, the launcher, are alive
        assert log.parent == run_dir / "bridge"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)


# -- module-loop classification ---------------------------------------------------------

def _write_events(spec: Path, events: list[dict]) -> None:
    path = lifecycle_log_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


@pytest.mark.parametrize("events, verdict, detail_has", [
    ([], "never_started", ""),
    ([{"kind": "start"}], "no_exit_recorded", ""),
    ([{"kind": "start"}, {"kind": "exit", "reason": "stdin_closed",
                          "parent_alive": False, "rc": 0}], "ok", ""),
    ([{"kind": "start"}, {"kind": "exit", "reason": "exception", "rc": 1,
                          "detail": "BrokenPipeError: x", "parent_alive": True}],
     "exited_early", "BrokenPipeError"),
    ([{"kind": "start"}, {"kind": "exit", "reason": "stdin_closed",
                          "parent_alive": True, "rc": 0}], "exited_early", "stdin_closed"),
    ([{"kind": "start"}, {"kind": "exit", "reason": "stdin_closed", "rc": 0},
      {"kind": "start"}], "restarted", ""),
])
def test_bridge_health_verdicts(tmp_path, events, verdict, detail_has):
    spec = tmp_path / "bridge" / "s.json"
    now = time.time()
    _write_events(spec, [{"ts": now + i, **e} for i, e in enumerate(events)])
    report = bridge_health(spec, since_ts=now - 5, settle_s=0.0)
    assert report["verdict"] == verdict
    assert detail_has in report["detail"]
    assert report["starts"] == sum(e["kind"] == "start" for e in events)


def test_bridge_health_ignores_events_from_an_earlier_session(tmp_path):
    spec = tmp_path / "bridge" / "s.json"
    now = time.time()
    _write_events(spec, [{"ts": now - 100, "kind": "start"},
                         {"ts": now - 90, "kind": "exit", "reason": "signal"}])
    assert bridge_health(spec, since_ts=now - 5, settle_s=0.0)["verdict"] == "never_started"


def test_bridge_health_counts_slow_tools(tmp_path):
    spec = tmp_path / "bridge" / "s.json"
    now = time.time()
    _write_events(spec, [
        {"ts": now, "kind": "start"},
        {"ts": now + 1, "kind": "slow_tool", "tool": "run_shell", "elapsed_s": 70},
        {"ts": now + 2, "kind": "slow_tool", "tool": "request_plan_review",
         "elapsed_s": 1800},
        {"ts": now + 3, "kind": "exit", "reason": "stdin_closed", "parent_alive": False}])
    report = bridge_health(spec, since_ts=now - 1, settle_s=0.0)
    assert report["verdict"] == "ok"
    assert report["slow_tools"] == 2
    assert report["slowest_tool"] == "request_plan_review"


def test_bridge_health_waits_briefly_for_the_exit_event(tmp_path):
    """The harness returns before its bridge child finishes: a start with
    no exit yet is given `settle_s` to land before being judged."""
    spec = tmp_path / "bridge" / "s.json"
    now = time.time()
    _write_events(spec, [{"ts": now, "kind": "start"}])

    def late_exit():
        time.sleep(0.3)
        with lifecycle_log_path(spec).open("a") as f:
            f.write(json.dumps({"ts": time.time(), "kind": "exit",
                                "reason": "stdin_closed", "parent_alive": False}) + "\n")

    threading.Thread(target=late_exit, daemon=True).start()
    report = bridge_health(spec, since_ts=now - 1, settle_s=3.0)
    assert report["verdict"] == "ok"


def test_harness_attempt_records_bridge_unavailable(tmp_path, monkeypatch):
    """Through `_harness_attempt`: a bridge that exited with an exception
    while the harness ran becomes a `bridge_unavailable` trace event with
    the exit reason and a note in the result text."""
    from infermatrix_copilot.agent_loop import AgentOutcome
    from infermatrix_copilot.providers import registry
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    from infermatrix_copilot.run_trace import RunTrace
    from infermatrix_copilot.scopes import PathScope, ToolScope

    repo = tmp_path / "repo"
    run = tmp_path / "run"
    (run / "signals").mkdir(parents=True)
    repo.mkdir()
    trace = RunTrace(run / "bridge_trace.jsonl")
    scope = ToolScope(name="rebase-module:fixture", allowed_tools=frozenset({"write_file"}),
                      path_scope=PathScope(writable=(f"{run}/*",), primary=(f"{run}/*",)),
                      read_only=False, root=str(repo))

    class Session:
        def run_session(self, req):
            # the bridge the harness launched: started, then died mid-session
            now = time.time()
            _write_events(Path(req.bridge_spec_path), [
                {"ts": now, "kind": "start", "pid": 4242},
                {"ts": now + 1, "kind": "exit", "reason": "exception", "rc": 1,
                 "detail": "RuntimeError: boom", "parent_alive": True, "pid": 4242}])
            (run / "signals" / "module.fixture.done").write_text("MODULE_DONE fixture\n")
            return AgentOutcome(text="finished", iterations=1, tool_calls=1, refusals=[])

    monkeypatch.setattr(registry, "transport_for_id", lambda *a, **kw: Session())
    monkeypatch.setattr(mr, "BRIDGE_EXIT_SETTLE_S", 0.0)
    config = mr.ModuleRunConfig(
        vllm_path=str(repo), omni_path=str(repo), script_dir=str(repo),
        model="fixture-model", log_dir=str(run), signal_dir=str(run / "signals"),
        backend="zcode", paths_spec={"omni_path": str(repo), "vllm_path": str(repo)})
    result = asyncio.run(mr._harness_attempt(
        "fixture", module="fixture", config=config, scope=scope, trace=trace,
        tool_defs=[], plan_prefix=str(run / "plans" / "module-fixture"),
        require_plan_review=False))
    [ev] = list(trace.events("bridge_unavailable"))
    assert ev["verdict"] == "exited_early"
    assert "RuntimeError: boom" in ev["detail"]
    assert ev["step"] == "rebase.module.fixture"
    assert "events" not in ev
    assert "Tool bridge exited early" in result["text"]
