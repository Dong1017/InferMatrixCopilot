"""Rebase module agents over a harness backend (provider-registry M1).

The in-process loop owns two things a harness cannot inherit: the 20-tool
adapter surface and the plan gate. These tests pin that both survive the move
into the bridge process, and that the gate's semantics are IDENTICAL to
`agent_loop`'s — refuse gated tools, confine writes to the plan dir, and open
only on a successful decision-file write.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from infermatrix_copilot.scopes import PathScope, ToolScope
from infermatrix_copilot.tool_bridge import (PlanGate, _fn_from_schema,
                                             load_bridge_spec, make_dispatcher,
                                             write_bridge_spec)
from infermatrix_copilot.tools import ToolDef

GATED = ("edit_file", "run_pytest", "run_precommit")


def _scope(root):
    return ToolScope(name="module-worker_runner",
                     allowed_tools=frozenset({"read_file", "write_file",
                                              "edit_file"}),
                     path_scope=PathScope(writable=(f"{root}/*",),
                                          primary=(f"{root}/*",)),
                     read_only=False, root=str(root))


def _rebase_section(plan_prefix):
    return {"tool_schemas": "/adapter/rebase/tool_schemas.json",
            "manifest_path": "/adapter/manifest.yaml",
            "model": "some-model",
            "paths": {"omni_path": "/omni", "vllm_path": "/vllm"},
            "plan_write_prefix": str(plan_prefix),
            "gated_tools": list(GATED)}


def test_spec_carries_rebase_section_and_no_credentials(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = write_bridge_spec(run_dir=run_dir, step_name="rebase.module.x",
                             scope=_scope(tmp_path), repo="vllm-omni",
                             rebase=_rebase_section(tmp_path / "plans"))
    _, raw = load_bridge_spec(path)
    assert raw["rebase"]["paths"]["omni_path"] == "/omni"
    # credentials and the child env stay in the bridge process's environment
    blob = json.dumps(raw).lower()
    assert "api_key" not in blob and "sk-" not in blob


def test_spec_without_rebase_section_is_unchanged(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    path = write_bridge_spec(run_dir=run_dir, step_name="agent.review",
                             scope=_scope(tmp_path), repo="vllm-omni")
    _, raw = load_bridge_spec(path)
    assert "rebase" not in raw


@pytest.mark.parametrize("name", GATED)
def test_gate_refuses_every_gated_tool_until_decision(name):
    gate = PlanGate("/plans", GATED)
    assert gate.refusal(name, {}) is not None
    gate.observe("write_file", {"file_path": "/plans/p.decision.md"},
                 json.dumps({"ok": True}))
    assert gate.open is True
    assert gate.refusal(name, {}) is None


def test_gate_confines_writes_to_plan_dir_while_closed():
    gate = PlanGate("/plans", GATED)
    assert gate.refusal("write_file", {"file_path": "/repo/prod.py"}) is not None
    assert gate.refusal("write_file",
                        {"file_path": "/plans/p.decision.md"}) is None
    # traversal out of the plan dir must not open a back door
    assert gate.refusal("write_file",
                        {"file_path": "/plans/../repo/prod.py"}) is not None


def test_failed_decision_write_leaves_gate_shut():
    gate = PlanGate("/plans", GATED)
    gate.observe("write_file", {"file_path": "/plans/p.decision.md"},
                 json.dumps({"error": "disk full"}))
    assert gate.open is False
    assert gate.refusal("edit_file", {}) is not None


def test_gate_restores_existing_decision_after_bridge_restart(tmp_path):
    from infermatrix_copilot.run_trace import RunTrace

    plans = tmp_path / "plans" / "module-x" / "dispatch_initial"
    plans.mkdir(parents=True)
    decision = plans / "plan-v0-002.decision.md"
    decision.write_text("accepted\n")
    trace_path = tmp_path / "trace.jsonl"

    gate = PlanGate(str(tmp_path / "plans" / "module-x"), GATED,
                    trace=RunTrace(trace_path))

    assert gate.open is True
    assert gate.refusal("edit_file", {}) is None
    restored = [json.loads(line) for line in trace_path.read_text().splitlines()]
    assert restored[-1]["kind"] == "plan_gate_opened"
    assert restored[-1]["decision"] == str(decision)
    assert restored[-1]["restored"] is True


def test_gate_records_opening_for_the_parent(tmp_path):
    from infermatrix_copilot.rebase_engine.module_rebase import _plan_gate_opened
    from infermatrix_copilot.run_trace import RunTrace

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    assert _plan_gate_opened(run_dir) is False
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    gate = PlanGate("/plans", GATED, trace=trace)
    gate.observe("write_file", {"file_path": "/plans/p.decision.md"},
                 json.dumps({"ok": True}))
    assert _plan_gate_opened(run_dir) is True


def test_dispatcher_serves_extra_tools_and_enforces_gate(tmp_path):
    from infermatrix_copilot.run_trace import RunTrace

    calls = []
    extra = {"reproduce": ToolDef(
        name="reproduce", description="d", input_schema={},
        handler=lambda **kw: (calls.append(kw) or json.dumps({"ok": True})))}
    trace = RunTrace(tmp_path / "t.jsonl")
    gate = PlanGate(str(tmp_path / "plans"), GATED)
    call = make_dispatcher(_scope(tmp_path), (str(tmp_path),), trace,
                           extra=extra, gate=gate)

    # an adapter tool that is NOT gated runs even before the decision
    assert call("reproduce", {"x": 1})
    assert calls == [{"x": 1}]
    # a gated one is refused at dispatch, not merely unadvertised
    with pytest.raises(RuntimeError, match="locked until the plan-review"):
        call("run_pytest", {})


def test_generated_signature_matches_schema():
    fn = _fn_from_schema(
        "run_shell",
        {"type": "object",
         "properties": {"command": {"type": "string"},
                        "timeout": {"type": "integer", "default": 60},
                        "workdir": {"type": "string"}},
         "required": ["command"]},
        lambda name, args: json.dumps({"name": name, "args": args}))
    sig = inspect.signature(fn)
    assert sig.parameters["command"].default is inspect.Parameter.empty
    assert sig.parameters["timeout"].default == 60
    assert sig.parameters["workdir"].default is None
    # keyword-only by construction (see the interleaved-schema test below)
    assert json.loads(fn(command="ls"))["args"]["command"] == "ls"


def test_schema_may_interleave_required_and_optional():
    """`record_debug_memory` declares an optional property BEFORE a required
    one. Emitted as positionals that is a SyntaxError, so the generated
    parameters are keyword-only (MCP calls by name regardless)."""
    fn = _fn_from_schema(
        "record_debug_memory",
        {"type": "object",
         "properties": {"module": {"type": "string"},
                        "root_cause": {"type": "string"},
                        "fix": {"type": "string"}},
         "required": ["module", "fix"]},
        lambda name, args: json.dumps(args))
    got = json.loads(fn(module="worker_runner", fix="patched"))
    assert got["module"] == "worker_runner" and got["fix"] == "patched"
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY
               for p in inspect.signature(fn).parameters.values())


def test_tool_with_no_parameters_generates():
    """A zero-property schema (git_diff_tests_upstream) must not emit a bare
    `*` — "named arguments must follow bare *"."""
    fn = _fn_from_schema("git_diff_tests_upstream",
                         {"type": "object", "properties": {}},
                         lambda name, args: name)
    assert inspect.signature(fn).parameters == {}
    assert fn() == "git_diff_tests_upstream"


def test_unset_optionals_are_omitted_not_passed_as_none():
    """A schema optional with no default must be DROPPED when unset, not
    forwarded as None: the handler has its own Python default, and
    `read_file` did `offset + int` on the None (caught in live smoke)."""
    seen = {}
    fn = _fn_from_schema(
        "read_file",
        {"type": "object",
         "properties": {"file_path": {"type": "string"},
                        "offset": {"type": "integer"},
                        "limit": {"type": "integer", "default": 200}},
         "required": ["file_path"]},
        lambda name, args: seen.update(args) or "ok")
    fn(file_path="x")
    assert "offset" not in seen          # unset, no schema default -> dropped
    assert seen["limit"] == 200          # schema default -> forwarded
    seen.clear()
    fn(file_path="x", offset=5)
    assert seen["offset"] == 5           # explicit value -> forwarded


def test_tier_model_is_never_forwarded_to_a_harness(monkeypatch):
    """The tier model names a RAW-API model (e.g. deepseek-flash) that a
    harness CLI does not have; forwarding it would override the harness's
    own model selection with an invalid id."""
    import asyncio
    import types as _types

    from infermatrix_copilot import tool_bridge
    from infermatrix_copilot.providers import registry
    from infermatrix_copilot.rebase_engine.module_rebase import _harness_attempt

    seen = {}

    class _T:
        def run_session(self, req):
            seen["model"] = req.model
            return _types.SimpleNamespace(truncated=False, text="ok",
                                          iterations=0)

    monkeypatch.setattr(registry, "transport_for_id", lambda *a, **k: _T())
    monkeypatch.setattr(tool_bridge, "write_bridge_spec", lambda **k: None)

    cfg = _types.SimpleNamespace(
        log_dir="/tmp", backend="cursor", backend_model="", settings=None,
        model="deepseek-flash", script_dir="/tmp", manifest_path="",
        repo="r", max_turns=10, harness_timeout_s=1.0, test_timeout_sec=7200,
        paths_spec={}, state_slice={}, baseline_ref="origin/main")
    scope = ToolScope(name="s", allowed_tools=frozenset(), path_scope=None,
                      read_only=True, root="/tmp")
    asyncio.run(_harness_attempt(
        "p", module="m", config=cfg, scope=scope, trace=None, tool_defs=[],
        plan_prefix="/tmp/plans", require_plan_review=False))

    assert seen["model"] == "", seen            # empty, NOT "deepseek-flash"


def test_bridge_passes_repo_ROOT_not_repo_name(tmp_path, monkeypatch):
    """`build_backends(repo=...)` is a filesystem path — it becomes
    TestRunner(repo_root=Path(repo)). The spec's "repo" is the repo NAME and
    belongs in state.task_spec.repo. Passing the name made run_pytest,
    run_precommit and reproduce die with FileNotFoundError('vllm-omni')."""
    from infermatrix_copilot import tool_bridge
    from infermatrix_copilot.run_trace import RunTrace

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    root = tmp_path / "checkout"
    root.mkdir()
    seen = {}

    def _fake_build_backends(**kw):
        seen.update(kw)
        return object()

    monkeypatch.setattr(tool_bridge, "_rebase_extra",
                        tool_bridge._rebase_extra)  # keep real
    monkeypatch.setattr("infermatrix_copilot.engine.steps.rebase_v3."
                        "build_backends", _fake_build_backends)
    monkeypatch.setattr(tool_bridge, "build_rebase_tools",
                        lambda defs, paths, backends: {}, raising=False)

    scope = ToolScope(name="m", allowed_tools=frozenset(), path_scope=None,
                      read_only=False, root=str(root))
    spec = {"run_dir": str(run_dir), "repo": "vllm-omni",
            "rebase": {"tool_schemas": "", "manifest_path": "", "model": "m",
                       "backend": "zcode", "backend_model": "GLM-5.3",
                       "paths": {"omni_path": str(root),
                                 "vllm_path": str(root)}}}
    try:
        tool_bridge._rebase_extra(spec, RunTrace(run_dir / "t.jsonl"), scope)
    except Exception:
        pass  # load_tool_schemas("") fails after build_backends is called

    assert seen.get("repo") == str(root), seen.get("repo")
    assert seen["state"]["task_spec"]["repo"] == "vllm-omni"
    assert seen["settings"].rebase_backend == "zcode"
    assert seen["settings"].rebase_backend_model == "GLM-5.3"


def test_failed_tool_records_the_reason(tmp_path):
    """A failed bridge call must trace WHY: without it a missing path and a
    broken tool are indistinguishable in bridge_trace.jsonl."""
    from infermatrix_copilot.run_trace import RunTrace

    trace = RunTrace(tmp_path / "bridge_trace.jsonl")
    extra = {"reproduce": ToolDef(
        name="reproduce", description="d", input_schema={},
        handler=lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))}
    call = make_dispatcher(_scope(tmp_path), (str(tmp_path),), trace,
                           extra=extra)
    with pytest.raises(RuntimeError):
        call("reproduce", {})
    kinds = [json.loads(l) for l in
             (tmp_path / "bridge_trace.jsonl").read_text().splitlines() if l.strip()]
    errs = [d for d in kinds if d.get("kind") == "tool_error"]
    assert errs and "boom" in errs[0]["error"]


def test_native_writes_detected_and_pre_gate_ones_are_fatal(tmp_path,
                                                            monkeypatch):
    """A harness keeping its built-in file tools can edit the checkout
    without touching the bridge, missing BOTH the scope guard and the plan
    gate. Where sandboxing is unavailable, detection is the ceiling: report
    every native write, and treat a PRE-GATE one as a contract breach."""
    import time

    from infermatrix_copilot.rebase_engine import module_rebase as mr

    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    now = time.time()
    gate_ts = now - 5
    bridged = root / "via_bridge.py"
    after = root / "native_after.py"
    before_gate = root / "native_before.py"

    (run_dir / "bridge_trace.jsonl").write_text("\n".join(json.dumps(d) for d in [
        {"ts": gate_ts, "kind": "plan_gate_opened", "decision": "d"},
        {"ts": gate_ts + 1, "kind": "tool_call", "tool": "edit_file",
         "path": str(bridged)},
    ]))

    # mtimes: bridged + one native AFTER the gate, one native BEFORE it
    monkeypatch.setattr(mr, "_changed_files", lambda r: {
        str(bridged): int((gate_ts + 1) * 1e9),
        str(after): int((gate_ts + 2) * 1e9),
        str(before_gate): int((gate_ts - 2) * 1e9),
    })

    native, pre_gate = mr._native_writes(str(root), run_dir, {}, now - 60)
    assert str(bridged) not in native          # went through the bridge
    assert str(after) in native                # native, but post-gate
    assert str(before_gate) in native
    assert pre_gate == [str(before_gate)]      # only this one breaks the gate


def test_unchanged_files_are_not_reported_as_native(tmp_path, monkeypatch):
    """A file already dirty before the session (same mtime) is not a write
    by this harness."""
    import time

    from infermatrix_copilot.rebase_engine import module_rebase as mr

    root = tmp_path / "repo"
    run_dir = tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    stale = root / "already_dirty.py"
    (run_dir / "bridge_trace.jsonl").write_text("")
    monkeypatch.setattr(mr, "_changed_files", lambda r: {str(stale): 777})

    native, pre_gate = mr._native_writes(str(root), run_dir,
                                         {str(stale): 777}, time.time() - 60)
    assert native == [] and pre_gate == []


def test_stale_self_written_mcp_config_is_replaced(tmp_path):
    """A killed session skips the transport's `finally`, leaving OUR
    .cursor/mcp.json behind. Honouring it binds the next session to a DEAD
    run's spec — wrong scope, wrong plan-gate prefix, traces appended to the
    old run. A repo-committed config must still be left alone."""
    from infermatrix_copilot.config import Settings
    from infermatrix_copilot.providers.cursor import CursorTransport

    t = CursorTransport(Settings())
    cwd = tmp_path / "checkout"
    (cwd / ".cursor").mkdir(parents=True)
    config = cwd / ".cursor" / "mcp.json"

    # ours, pointing at a dead run
    config.write_text(json.dumps({"mcpServers": {"infermatrix-tools": {
        "command": "python",
        "args": ["-m", "infermatrix_copilot.tool_bridge", "--spec",
                 "/runs/DEAD/bridge/old.json"]}}}))
    assert t._is_our_stale_config(config) is True
    t._write_mcp_config(cwd, Path("/runs/LIVE/bridge/new.json"))
    assert "/runs/LIVE/bridge/new.json" in config.read_text()

    # someone else's config: untouched
    foreign = json.dumps({"mcpServers": {"their-tool": {"command": "x"}}})
    config.write_text(foreign)
    assert t._is_our_stale_config(config) is False
    t._write_mcp_config(cwd, Path("/runs/LIVE/bridge/new.json"))
    assert config.read_text() == foreign


@pytest.mark.parametrize("signal,expected", [
    ("missing", False), ("stale", False), ("wrong_module", False),
    ("malformed", False), ("fresh", True), ("rewritten", True),
    ("failure", False), ("truncated", False),
])
def test_harness_requires_current_attempt_completion_signal(tmp_path, monkeypatch,
                                                           signal, expected):
    import asyncio
    from types import SimpleNamespace

    from infermatrix_copilot import tool_bridge
    from infermatrix_copilot.providers import registry
    from infermatrix_copilot.rebase_engine.module_rebase import (
        ModuleRunConfig, _harness_attempt,
    )

    root = tmp_path / "repo"
    run = tmp_path / "run"
    signals = run / "signals"
    root.mkdir()
    signals.mkdir(parents=True)
    done = signals / "module.fixture.done"
    if signal in ("stale", "rewritten"):
        done.write_text("MODULE_DONE fixture\n")

    class Session:
        def run_session(self, req):
            # The module contract (including the authoritative completion
            # signal) rides the USER channel: a --system-prompt argv element
            # is bound by the 128KiB per-arg limit evidence packs exceed.
            assert str(done) in req.prompt
            assert req.system == ""
            if signal == "wrong_module":
                done.write_text("MODULE_DONE other\n")
            elif signal == "malformed":
                done.write_text("withheld MODULE_DONE fixture\n")
            elif signal in ("fresh", "rewritten", "failure", "truncated"):
                done.write_text("MODULE_DONE fixture\nOUT_OF_SCOPE_EDITS none\n")
            if signal == "failure":
                (signals / "module.fixture.fail").write_text("verification failed\n")
            return SimpleNamespace(truncated=signal == "truncated",
                                   text="withheld MODULE_DONE", iterations=1)

    monkeypatch.setattr(registry, "transport_for_id", lambda *a, **k: Session())
    monkeypatch.setattr(tool_bridge, "write_bridge_spec", lambda **k: None)
    cfg = ModuleRunConfig(vllm_path=str(root), omni_path=str(root),
                          script_dir=str(root), model="m", log_dir=str(run),
                          signal_dir=str(signals), backend="zcode")
    scope = ToolScope(name="fixture", allowed_tools=frozenset(), path_scope=None,
                      read_only=True, root=str(root))
    result = asyncio.run(_harness_attempt(
        "debug attempt", module="fixture", config=cfg, scope=scope, trace=None,
        tool_defs=[], plan_prefix="", require_plan_review=False))
    assert result["done"] is expected
    if not expected:
        assert "completion rejected" in result["text"]


@pytest.mark.parametrize("done,expected", [(True, "done"), (False, "not_done")])
def test_ci_debug_harness_has_its_own_completion_signal(tmp_path, monkeypatch,
                                                       done, expected):
    import asyncio
    from types import SimpleNamespace
    from infermatrix_copilot.engine.steps import rebase_v3 as rv
    from infermatrix_copilot.rebase_engine import module_rebase as mr
    from infermatrix_copilot.rebase_engine import rebase_tools as rt
    from infermatrix_copilot.rebase_engine import prompt_builder as pb

    data = SimpleNamespace(debug_prompt_template_live="MODULE={module}\n{traceback}",
                           debug_prompt_template="")
    monkeypatch.setattr(pb.ModulePromptData, "load", lambda *a: data)
    monkeypatch.setattr(rt, "load_tool_schemas", lambda *a: [])
    monkeypatch.setattr(rt, "build_rebase_tools", lambda *a: {})
    monkeypatch.setattr(rv, "_tier_client", lambda ctx: (
        None, SimpleNamespace(kind="harness", provider_id="zcode", model="m")))
    monkeypatch.setattr(rv, "_agent_shell_env", lambda *a: {})
    monkeypatch.setattr(rv, "_build_backends", lambda *a: None)
    monkeypatch.setattr(rv, "_module_scope", lambda *a, **k: None)
    async def attempt(prompt, *, config, **kwargs):
        assert config.signal_dir == str(tmp_path / "signals/debug/failing_job")
        assert kwargs["module"] == "platform"
        return {"done": done, "session_completed": True, "text": "unresolved"}
    monkeypatch.setattr(mr, "_harness_attempt", attempt)
    ctx = SimpleNamespace(
        state={"task_spec": {"repo": "vllm-omni"}, "repo_path": str(tmp_path)},
        settings=SimpleNamespace(adapters_dir=str(tmp_path)), run_dir=tmp_path,
        trace=SimpleNamespace(record=lambda *a, **k: None))
    monkeypatch.setattr(rv, "_task_params", lambda *a: {})
    assert asyncio.run(rv._run_debug_agent(
        ctx, {}, "platform", "failing_job", "test failed")) == expected
