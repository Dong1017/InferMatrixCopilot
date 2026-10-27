"""The serialized rebase bridge must reconstruct the agent's target runtime."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import venv
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.config import Settings
from infermatrix_copilot.engine.steps.rebase_v3 import _agent_shell_env
from infermatrix_copilot.rebase_engine.module_rebase import (
    ModuleRunConfig,
    _harness_attempt,
)
from infermatrix_copilot.rebase_engine.rebase_tools import (
    RebaseBackends,
    RebasePaths,
    build_rebase_tools,
    load_tool_schemas,
)
from infermatrix_copilot.run_trace import RunTrace
from infermatrix_copilot.scopes import ToolScope
from infermatrix_copilot.tool_bridge import (
    _rebase_extra,
    load_bridge_spec,
    make_dispatcher,
)


@pytest.fixture
def target_python(tmp_path):
    root = tmp_path / "target-venv"
    venv.EnvBuilder(with_pip=False).create(root)
    return root


@pytest.mark.parametrize("parent_override", [False, True])
@pytest.mark.parametrize("keep_hf_token", [False, True])
def test_harness_spec_to_real_shell_keeps_target_runtime_and_scrub(
    tmp_path, monkeypatch, target_python, parent_override, keep_hf_token
):
    """Exercise the real spec, reconstruction, dispatch and child interpreter.

    Only the vendor CLI is replaced: its session dispatches the real bridge
    tool to a real CPU shell. The wrapper probe checks the same environment
    contract without launching pytest, taking a GPU lock or allocating CUDA.
    """
    from infermatrix_copilot import config
    from infermatrix_copilot.providers import registry
    from infermatrix_copilot.providers.base import sanitized_env
    from infermatrix_copilot.providers.zcode import ZCodeTransport

    repo = tmp_path / "checkout"
    run = tmp_path / "run"
    adapter = tmp_path / "adapter"
    for root in (repo, run, adapter / "rebase"):
        root.mkdir(parents=True)
    schemas = (
        Path(__file__).resolve().parents[1]
        / "adapters/vllm_omni"
        / "rebase/tool_schemas.json"
    )
    manifest = {
        "repo": {"venv": "${VLLM_OMNI_VENV}"},
        "validation": {"requires_hf_token": keep_hf_token},
    }
    manifest_path = adapter / "manifest.yaml"
    manifest_path.write_text(yaml.safe_dump(manifest))
    # A launcher-only override must survive even when the bridge's local
    # Settings fallback points at a different runtime.
    fallback = tmp_path / "wrong-fallback" if parent_override else target_python
    monkeypatch.setattr(
        Settings, "expansion_env", lambda self: {"VLLM_OMNI_VENV": str(fallback)}
    )
    monkeypatch.delenv("VLLM_OMNI_VENV", raising=False)
    if parent_override:
        monkeypatch.setenv("VLLM_OMNI_VENV", str(target_python))
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "2,3,4,5")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "models"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fixture-api-secret")
    monkeypatch.setenv("UNKNOWN_SESSION_TOKEN", "fixture-unknown-secret")
    monkeypatch.setenv("PYTHON_API_KEY", "fixture-prefixed-secret")
    monkeypatch.setenv("HF_TOKEN", "fixture-gated-secret")
    settings = Settings(
        _env_file=None,
        memory_db=tmp_path / "memory.db",
        adapters_dir=tmp_path,
        rebase_backend="zcode",
        rebase_backend_model="GLM-5.3",
    )
    state = {"task_spec": {"repo": "fixture-repo"}, "run_id": "release-fixture"}
    ctx = SimpleNamespace(settings=settings, state=state, run_dir=run)
    expected = _agent_shell_env(ctx, manifest, str(repo), adapter)
    scope = ToolScope(
        name="rebase-module:fixture",
        allowed_tools=frozenset({"run_shell"}),
        path_scope=None,
        read_only=False,
        root=str(repo),
    )
    defs = load_tool_schemas(schemas)
    fields = (
        "VIRTUAL_ENV",
        "CUDA_VISIBLE_DEVICES",
        "HF_HOME",
        "IMX_TARGET_REPO",
        "IMX_LOG_DIR",
        "IMX_ADAPTER_REBASE",
        "IMX_GPU_MUTEX",
        "IMX_RUN_ID",
        "IMX_WATCHDOG_OVERLAY",
        "TEST_TIMEOUT_SEC",
    )
    probe = (
        "import json,os,shutil,sys; "
        f"print(json.dumps({{'env': {{k: os.environ.get(k) for k in {fields!r}}}, "
        "'prefix': sys.prefix, 'python': shutil.which('python'), "
        "'wrapper': shutil.which('imx-omni-pytest'), "
        "'credential_names': sorted(k for k in os.environ if k in "
        "('ANTHROPIC_API_KEY','UNKNOWN_SESSION_TOKEN','PYTHON_API_KEY','HF_TOKEN'))}))"
    )
    wrapper = target_python / "bin/imx-omni-pytest"
    wrapper.write_text(
        f"#!{target_python / 'bin/python'}\nimport os,sys\n"
        "assert all(os.environ.get(k) for k in ('IMX_TARGET_REPO', 'IMX_LOG_DIR', "
        "'IMX_ADAPTER_REBASE', 'IMX_GPU_MUTEX', 'IMX_RUN_ID', 'IMX_WATCHDOG_OVERLAY'))\n"
        "assert sys.argv[1:] == ['--fixture-probe']\nprint('wrapper-contract-ok')\n"
    )
    wrapper.chmod(0o755)
    command = "python -c " + shlex.quote(probe)
    api_tools = build_rebase_tools(
        defs,
        RebasePaths(omni_path=str(repo), vllm_path=str(repo), env=expected),
        RebaseBackends(),
    )
    api_call = make_dispatcher(
        scope, (str(repo), str(run)), RunTrace(run / "api.jsonl"), extra=api_tools
    )
    api_result = json.loads(api_call("run_shell", {"command": command, "timeout": 30}))
    assert api_result["exit_code"] == 0
    captured = {}

    def bridge_settings(**kwargs):
        return settings.model_copy(update=kwargs)

    monkeypatch.setattr(config, "Settings", bridge_settings)

    class Session:
        def run_session(self, req):
            assert req.model == "GLM-5.3" and req.timeout_s == 7200.0
            restored_scope, spec = load_bridge_spec(req.bridge_spec_path)
            blob = req.bridge_spec_path.read_text()
            assert '"env"' not in blob
            assert all(
                value not in blob
                for value in (
                    "fixture-api-secret",
                    "fixture-unknown-secret",
                    "fixture-prefixed-secret",
                    "fixture-gated-secret",
                )
            )
            captured["target_venv_recipe"] = spec["rebase"].get("target_venv")
            session_dir = tmp_path / "session"
            session_dir.mkdir()
            ZCodeTransport._write_mcp_config(session_dir, req.bridge_spec_path)
            config_blob = (session_dir / ".zcode/config.json").read_text()
            assert all(
                value not in config_blob
                for value in (
                    "fixture-api-secret",
                    "fixture-unknown-secret",
                    "fixture-prefixed-secret",
                    "fixture-gated-secret",
                )
            )
            server_env = json.loads(config_blob)["mcp"]["servers"]["infermatrix-tools"][
                "env"
            ]
            # Reproduce the real CLI + MCP environment; it contains neither
            # the launcher's expansion override nor its gated-model token.
            inherited = {**sanitized_env(), **server_env}
            assert "VLLM_OMNI_VENV" not in inherited and "HF_TOKEN" not in inherited
            with monkeypatch.context() as bridge_env:
                bridge_env.setattr(os, "environ", inherited)
                bridge_manifest = {**manifest, "repo": {"venv": str(target_python)}}
                same_process_env = _agent_shell_env(
                    ctx, bridge_manifest, str(repo), adapter
                )
                comparison_tools = build_rebase_tools(
                    defs,
                    RebasePaths(
                        omni_path=str(repo), vllm_path=str(repo), env=same_process_env
                    ),
                    RebaseBackends(),
                )
                comparison = make_dispatcher(
                    restored_scope,
                    (str(repo), str(run)),
                    RunTrace(run / "comparison.jsonl"),
                    extra=comparison_tools,
                )
                captured["same_process_api"] = json.loads(
                    comparison("run_shell", {"command": command, "timeout": 30})
                )
                tools, _ = _rebase_extra(
                    spec, RunTrace(run / "bridge.jsonl"), restored_scope
                )
                call = make_dispatcher(
                    restored_scope,
                    (str(repo), str(run)),
                    RunTrace(run / "dispatch.jsonl"),
                    extra=tools,
                )
                captured["shell"] = json.loads(
                    call("run_shell", {"command": command, "timeout": 30})
                )
                captured["wrapper"] = json.loads(
                    call(
                        "run_shell",
                        {"command": "imx-omni-pytest --fixture-probe", "timeout": 30},
                    )
                )
            signals = run / "signals"
            signals.mkdir(exist_ok=True)
            (signals / "module.fixture.done").write_text("MODULE_DONE fixture\n")
            return SimpleNamespace(
                truncated=False, text="fixture complete", iterations=1
            )

    monkeypatch.setattr(registry, "transport_for_id", lambda *args, **kwargs: Session())
    cfg = ModuleRunConfig(
        vllm_path=str(repo),
        omni_path=str(repo),
        log_dir=str(run),
        signal_dir=str(run / "signals"),
        settings=settings,
        backend="zcode",
        backend_model="GLM-5.3",
        script_dir=str(schemas.parent),
        manifest_path=str(manifest_path),
        model="api-model",
        # as the module step does: the host facts the bridge must inherit
        # through the spec, since its own environment is allowlisted
        cuda_devices=os.environ["CUDA_VISIBLE_DEVICES"],
        hf_home=os.environ["HF_HOME"],
        paths_spec={"omni_path": str(repo), "vllm_path": str(repo)},
        target_venv=expected["VIRTUAL_ENV"],
        state_slice=state,
        repo="fixture-repo",
        max_turns=400,
        harness_timeout_s=7200.0,
    )
    outcome = asyncio.run(
        _harness_attempt(
            "fixture",
            module="fixture",
            config=cfg,
            scope=scope,
            trace=RunTrace(run / "t.jsonl"),
            tool_defs=defs,
            plan_prefix="",
            require_plan_review=False,
        )
    )
    assert outcome["done"]
    assert captured["shell"]["exit_code"] == 0
    assert captured["shell"]["stdout"] == captured["same_process_api"]["stdout"]
    result = json.loads(captured["shell"]["stdout"])
    assert captured["target_venv_recipe"] == str(target_python)
    assert result["prefix"] == str(target_python)
    assert result["python"] == str(target_python / "bin/python")
    assert result["wrapper"] == str(wrapper)
    assert result["env"] == {k: expected.get(k) for k in fields}
    assert result["env"]["CUDA_VISIBLE_DEVICES"] == "2,3,4,5"
    assert result["env"]["IMX_LOG_DIR"] == str(run)
    assert result["env"]["IMX_GPU_MUTEX"] == "1"
    assert result["env"]["TEST_TIMEOUT_SEC"] == expected.get("TEST_TIMEOUT_SEC")
    assert json.loads(api_result["stdout"])["credential_names"] == (
        ["HF_TOKEN"] if keep_hf_token else []
    )
    # Reconstructing the shell policy does not invent credentials absent
    # from the actual harness transport's environment.
    assert result["credential_names"] == []
    assert captured["wrapper"]["exit_code"] == 0
    assert captured["wrapper"]["stdout"].strip() == "wrapper-contract-ok"
    assert os.environ["ANTHROPIC_API_KEY"] == "fixture-api-secret"
    assert "IMX_GPU_MUTEX" not in os.environ


def test_env_builders_prefer_explicit_host_env_over_process_env(tmp_path, monkeypatch):
    """In a bridge process the inherited env is allowlisted; the spec's host
    facts must win, and an absent fact falls back to the process env."""
    from infermatrix_copilot.engine.steps.rebase_v3 import _target_test_env
    from infermatrix_copilot.config import Settings

    settings = Settings(_env_file=None)
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    monkeypatch.setenv("HF_HOME", "/proc-env/hf")
    env = _target_test_env(settings, {}, host_env={"CUDA_VISIBLE_DEVICES": "1,3,4,7"})
    assert env["CUDA_VISIBLE_DEVICES"] == "1,3,4,7"
    assert env["HF_HOME"] == "/proc-env/hf"
    monkeypatch.delenv("CUDA_VISIBLE_DEVICES")
    assert "CUDA_VISIBLE_DEVICES" not in _target_test_env(settings, {})
    assert _target_test_env(settings, {}, host_env={"CUDA_VISIBLE_DEVICES": "5"})[
        "CUDA_VISIBLE_DEVICES"] == "5"
