"""Module test wrapper process contract (run-20261027-193251: four wrapper
runs on `model_executor` ended at ~30 s with child rc -15/-15/-9/-2,
`timed_out=false`, `watchdog_triggered=false` — a GPU reservation guard
outside the process tree killed the test processes, and the receipt could
not say so).

Under test (rebase_engine/module_pytest.py + testing/runner.py):
  * the real wrapper re-sessions itself (sid == pid) and ignores SIGHUP,
    so a harness tool's deadline kill of its process group and the loss of
    the tool's stdin do not end a detached test;
  * SIGTERM/SIGINT to the wrapper stop the test tree and are recorded as
    `signal_origin: wrapper_signal`;
  * a child killed by a signal nobody in the tree sent is classified
    `signal_origin: external` with a diagnosis in receipt AND log;
  * in-process callers (`main(argv)`) never re-session the calling process;
  * the runner spawns tests with stdin from /dev/null.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from infermatrix_copilot.rebase_engine import module_pytest
from infermatrix_copilot.rebase_engine.module_pytest import (
    RECEIPT_SCHEMA_VERSION,
    WrapperProcess,
)
from infermatrix_copilot.testing.runner import TestOutcome as Outcome

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process model")


@pytest.fixture
def wrapper_env(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return {
        "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.defpath}",
        "PYTHONPATH": str(ROOT / "src"),
        "IMX_TARGET_REPO": str(repo),
        "IMX_LOG_DIR": str(tmp_path / "logs"),
        "IMX_RUN_ID": "run-20200101-000000-abcdef",
        "IMX_GPU_MUTEX": "0",
        "CUDA_VISIBLE_DEVICES": "",
        "TEST_TIMEOUT_SEC": "60",
    }


def _receipt(env) -> dict:
    files = sorted((Path(env["IMX_LOG_DIR"]) / "tests" / "outcomes").glob("*.json"))
    assert len(files) == 1, files
    return json.loads(files[0].read_text())


def _log(env) -> str:
    [log] = list((Path(env["IMX_LOG_DIR"]) / "tests").glob("*.log"))
    return log.read_text(encoding="utf-8", errors="replace")


def _spawn_wrapper(env, code: str, **popen_kw) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", module_pytest.__name__, "python", "-c", code],
        env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, **popen_kw)


def _wait_for_child(wrapper_pid: int, marker: Path, timeout: float = 20) -> int:
    """The test command writes its pid to `marker` once it is running."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists() and marker.read_text().strip():
            return int(marker.read_text().strip())
        time.sleep(0.05)
    raise AssertionError("test child never started")


def _sleeper(marker: Path) -> str:
    return (f"import os, time; open({str(marker)!r}, 'w').write(str(os.getpid()));"
            " time.sleep(120)")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# -- unit: classification --------------------------------------------------------------

@pytest.mark.parametrize("outcome, received, expected", [
    (Outcome(rc=0), [], (None, None)),
    (Outcome(rc=1), [], (None, None)),
    (Outcome(rc=124, timed_out=True), [], (None, "timeout")),
    (Outcome(rc=-15, watchdog_triggered=True), [], ("SIGTERM", "watchdog")),
    (Outcome(rc=-15), ["SIGTERM"], ("SIGTERM", "wrapper_signal")),
    (Outcome(rc=-15), [], ("SIGTERM", "external")),
    (Outcome(rc=-9), [], ("SIGKILL", "external")),
    (Outcome(rc=-2), [], ("SIGINT", "external")),
])
def test_classify_signal_origin(outcome, received, expected):
    process = WrapperProcess()
    process.received = [{"signal": s, "ts": 0.0} for s in received]
    verdict = process.classify(outcome)
    assert (verdict["signal"], verdict["signal_origin"]) == expected
    if expected[1] == "external":
        assert "external supervisor" in verdict["signal_diagnosis"]
        assert "not reserved for this process tree" in verdict["signal_diagnosis"]
    else:
        assert verdict["signal_diagnosis"] is None


def test_in_process_main_never_resessions_the_caller(tmp_path, monkeypatch, wrapper_env):
    """Tests (and any in-process caller) pass argv: the wrapper must not
    `setsid` or install handlers in THEIR process."""
    for k, v in wrapper_env.items():
        monkeypatch.setenv(k, v)
    detached = []
    monkeypatch.setattr(WrapperProcess, "detach",
                        lambda self: detached.append("detach"))
    monkeypatch.setattr(WrapperProcess, "install_signal_handlers",
                        lambda self, runner: detached.append("handlers"))
    assert module_pytest.main(["python", "-c", "pass"]) == 0
    assert detached == []
    receipt = _receipt(wrapper_env)
    assert receipt["schema_version"] == RECEIPT_SCHEMA_VERSION
    assert receipt["wrapper_detached"] is False
    assert receipt["signal"] is None and receipt["signal_origin"] is None
    assert receipt["wrapper_pid"] == os.getpid()
    assert receipt["wrapper_ppid"] == os.getppid()


# -- real wrapper process -------------------------------------------------------------

def test_wrapper_detaches_and_ignores_sighup(tmp_path, wrapper_env):
    marker = tmp_path / "child.pid"
    proc = _spawn_wrapper(wrapper_env, _sleeper(marker))
    try:
        child = _wait_for_child(proc.pid, marker)
        # own session: a tool-shell group kill cannot reach it
        assert os.getsid(proc.pid) == proc.pid
        assert os.getsid(child) != proc.pid      # the test has its own session too
        proc.send_signal(signal.SIGHUP)
        proc.stdin.close()                       # the launching tool's pipe goes away
        time.sleep(1.0)
        assert proc.poll() is None and _alive(child)
        # now stop it the sanctioned way
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    receipt = _receipt(wrapper_env)
    assert receipt["wrapper_detached"] is True
    assert receipt["wrapper_session_leader"] is True
    assert receipt["wrapper_sid"] == receipt["wrapper_pid"] == proc.pid
    assert "SIGHUP" in receipt["wrapper_signals_ignored"]
    assert receipt["wrapper_signals_received"] == ["SIGTERM"]


def test_wrapper_sigterm_stops_tree_and_records_wrapper_signal(tmp_path, wrapper_env):
    marker = tmp_path / "child.pid"
    proc = _spawn_wrapper(wrapper_env, _sleeper(marker))
    try:
        child = _wait_for_child(proc.pid, marker)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=60)
        for _ in range(100):
            if not _alive(child):
                break
            time.sleep(0.1)
        assert not _alive(child), "test child outlived the signalled wrapper"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    stderr = proc.stderr.read().decode()
    receipt = _receipt(wrapper_env)
    assert receipt["rc"] == -signal.SIGTERM              # the child's signal rc
    assert rc == (-signal.SIGTERM) & 0xFF                # wrapper exit mirrors it
    assert receipt["timed_out"] is False and receipt["watchdog_triggered"] is False
    assert receipt["signal"] == "SIGTERM"
    assert receipt["signal_origin"] == "wrapper_signal"
    assert receipt["signal_diagnosis"] is None
    assert receipt["wrapper_signals_received"] == ["SIGTERM"]
    assert "stopped by SIGTERM" in stderr
    assert "signal postmortem" in _log(wrapper_env)


def test_external_kill_of_the_test_is_diagnosed(tmp_path, wrapper_env):
    """The evidence case: something outside the tree SIGKILLs the test
    process. The wrapper survives to write the receipt, which names the
    signal, classifies it `external` and carries the diagnosis; the log
    gets the same footer for the debug agent."""
    marker = tmp_path / "child.pid"
    proc = _spawn_wrapper(wrapper_env, _sleeper(marker))
    try:
        child = _wait_for_child(proc.pid, marker)
        os.kill(child, signal.SIGKILL)
        rc = proc.wait(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
    receipt = _receipt(wrapper_env)
    assert receipt["rc"] == -signal.SIGKILL and rc == (-signal.SIGKILL) & 0xFF
    assert receipt["timed_out"] is False and receipt["watchdog_triggered"] is False
    assert receipt["signal"] == "SIGKILL"
    assert receipt["signal_origin"] == "external"
    assert "external supervisor" in receipt["signal_diagnosis"]
    assert receipt["wrapper_signals_received"] == []
    log = _log(wrapper_env)
    assert "signal=SIGKILL origin=external" in log
    assert "external supervisor" in log
    assert "external supervisor" in proc.stderr.read().decode()
    # the receipt's log hash covers the footer (written before the receipt)
    import hashlib
    [log_path] = list((Path(wrapper_env["IMX_LOG_DIR"]) / "tests").glob("*.log"))
    assert receipt["log_sha256"] == hashlib.sha256(log_path.read_bytes()).hexdigest()


def test_foreground_opt_out_keeps_the_callers_session(tmp_path, wrapper_env):
    env = dict(wrapper_env, IMX_WRAPPER_FOREGROUND="1")
    proc = _spawn_wrapper(env, "pass", start_new_session=True)
    proc.wait(timeout=60)
    receipt = _receipt(env)
    assert receipt["wrapper_detached"] is False
    assert receipt["wrapper_detach_error"] == "IMX_WRAPPER_FOREGROUND=1"
    assert "SIGHUP" in receipt["wrapper_signals_ignored"]   # still hangup-safe
    assert receipt["rc"] == 0


def test_test_child_gets_devnull_stdin(tmp_path, wrapper_env):
    """A test must never inherit the wrapper's stdin (the tool pipe)."""
    marker = tmp_path / "stdin.txt"
    code = (f"import sys; open({str(marker)!r}, 'w').write("
            "repr(sys.stdin.read()) if sys.stdin else 'none')")
    proc = _spawn_wrapper(wrapper_env, code)
    proc.stdin.write(b"from the tool pipe\n")
    proc.stdin.flush()
    proc.wait(timeout=60)
    assert marker.read_text() == "''"          # /dev/null: EOF, not our bytes
    assert _receipt(wrapper_env)["rc"] == 0
