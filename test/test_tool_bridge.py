"""Tool-bridge spec round-trip and the read-containment guard
(doc/features/provider-registry.md — the `.env`-exfiltration case)."""

from pathlib import Path

import pytest

from infermatrix_copilot.run_trace import RunTrace
from infermatrix_copilot.scopes import ToolScope, read_only_scope
from infermatrix_copilot.tool_bridge import (
    load_bridge_spec,
    make_dispatcher,
    write_bridge_spec,
)


def _scoped(tmp_path: Path) -> tuple[ToolScope, Path, Path]:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scope = read_only_scope()
    scope = type(scope)(name=scope.name, allowed_tools=scope.allowed_tools,
                        read_only=True, root=str(worktree))
    return scope, worktree, run_dir


def test_bridge_spec_round_trips(tmp_path):
    scope, _worktree, run_dir = _scoped(tmp_path)
    spec_path = write_bridge_spec(run_dir=run_dir,
                                  step_name="agent.review_diff#lens0",
                                  scope=scope, repo="vllm-omni")
    loaded, raw = load_bridge_spec(spec_path)
    assert loaded == scope
    assert raw["repo"] == "vllm-omni"
    assert raw["run_dir"] == str(run_dir)
    assert "lens0" in spec_path.name and "#" not in spec_path.name


def test_bridged_read_inside_root_passes_and_relative_resolves(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    (worktree / "mod.py").write_text("x = 1\n", encoding="utf-8")
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    assert call("read_file", {"path": str(worktree / "mod.py")}) == "x = 1\n"
    # repo-relative paths resolve against the worktree, as in-process
    assert call("read_file", {"path": "mod.py"}) == "x = 1\n"
    assert any(e["tool"] == "read_file"
               for e in trace.events("tool_call"))


def test_bridged_read_outside_roots_is_refused_and_traced(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    secret = tmp_path / "home" / ".infermatrix-copilot" / ".env"
    secret.parent.mkdir(parents=True)
    secret.write_text("ANTHROPIC_API_KEY=leak-me\n", encoding="utf-8")
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    with pytest.raises(RuntimeError, match="refused"):
        call("read_file", {"path": str(secret)})
    refusals = list(trace.events("tool_refused"))
    assert refusals and "outside session roots" in refusals[0]["reason"]


def test_scope_tool_allowlist_still_enforced(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    # read_only_scope has no run_shell — dispatch refuses by tool name; the
    # bridge surfaces it as an error, identically to the in-process loop
    with pytest.raises(RuntimeError, match="not allowed"):
        call("run_shell", {"cmd": "echo hi"})


def test_bridged_write_refused_in_read_only_scope(tmp_path):
    scope, worktree, run_dir = _scoped(tmp_path)
    scope = type(scope)(name=scope.name,
                        allowed_tools=scope.allowed_tools | {"write_file"},
                        read_only=True, root=str(worktree))
    trace = RunTrace(run_dir / "bridge_trace.jsonl")
    call = make_dispatcher(scope, (str(worktree), str(run_dir)), trace)

    with pytest.raises(RuntimeError, match="read-only"):
        call("write_file", {"path": str(worktree / "a.txt"), "content": "x"})
    assert not (worktree / "a.txt").exists()


def test_bridge_applies_spec_host_env_before_building_backends(tmp_path, monkeypatch):
    """The parent's GPU grant and model cache reach the bridge through the
    spec, not the (allowlisted) inherited environment."""
    import os

    from infermatrix_copilot import tool_bridge
    from infermatrix_copilot.scopes import PathScope, ToolScope

    monkeypatch.delenv("CUDA_VISIBLE_DEVICES", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    captured: dict = {}

    def fake_agent_shell_env(ctx, manifest, repo_root, adapter_dir, **kw):
        captured["shell"] = dict(kw.get("host_env") or {})
        return {}

    def fake_build_backends(**kw):
        captured["backends"] = dict(kw.get("host_env") or {})
        return object()

    def fake_build_rebase_tools(defs, paths, backends):
        return {}

    import infermatrix_copilot.engine.steps.rebase_v3 as rv3
    import infermatrix_copilot.rebase_engine.rebase_tools as rt
    monkeypatch.setattr(rv3, "_agent_shell_env", fake_agent_shell_env)
    monkeypatch.setattr(rv3, "build_backends", fake_build_backends)
    monkeypatch.setattr(rt, "build_rebase_tools", fake_build_rebase_tools)
    monkeypatch.setattr(rt, "load_tool_schemas", lambda p: [])
    schemas = tmp_path / "adapter" / "rebase" / "tool_schemas.json"
    schemas.parent.mkdir(parents=True)
    schemas.write_text("[]", encoding="utf-8")
    scope = ToolScope(name="rebase-module:x", allowed_tools=frozenset(),
                      path_scope=PathScope(writable=(), primary=()),
                      read_only=False, root=str(tmp_path))
    spec = {"run_dir": str(tmp_path), "repo": "vllm-omni",
            "rebase": {"tool_schemas": str(schemas),
                       "paths": {"omni_path": str(tmp_path),
                                 "vllm_path": str(tmp_path)},
                       "host_env": {"CUDA_VISIBLE_DEVICES": "1,3,4,7",
                                    "HF_HOME": "/models"}}}
    tool_bridge._rebase_extra(spec, trace=None, scope=scope)
    expected = {"CUDA_VISIBLE_DEVICES": "1,3,4,7", "HF_HOME": "/models"}
    assert captured == {"shell": expected, "backends": expected}
    # handed over explicitly: the bridge never mutates its own process env
    assert "CUDA_VISIBLE_DEVICES" not in os.environ and "HF_HOME" not in os.environ

    bad = dict(spec, rebase={**spec["rebase"], "host_env": {"CUDA_VISIBLE_DEVICES": 7}})
    import pytest
    with pytest.raises(TypeError):
        tool_bridge._rebase_extra(bad, trace=None, scope=scope)
