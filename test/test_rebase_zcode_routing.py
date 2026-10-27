"""The rebase bridge must keep the selected shared harness and GPU lease."""

import json
from types import SimpleNamespace

import pytest

from infermatrix_copilot.config import Settings
from infermatrix_copilot.engine.steps.rebase_v3 import build_backends
from infermatrix_copilot.llm import Block, Reply
from infermatrix_copilot.providers.registry import transport_for_id


def test_harness_tier_uses_native_auth_instead_of_requiring_an_api_key(
        tmp_path, monkeypatch):
    from infermatrix_copilot.engine.steps import rebase_v3

    settings = Settings(_env_file=None, strict_backend="zcode",
                        strict_backend_model="GLM-5.3", rebase_backend="zcode")
    transport = SimpleNamespace(cli_path=lambda: "/bin/zcode", auth_gap=lambda: None)
    monkeypatch.setattr("infermatrix_copilot.providers.registry.transport_for_id",
                        lambda settings, backend: transport)
    ctx = SimpleNamespace(settings=settings, state={}, trace=None)
    client, target = rebase_v3._tier_client(ctx)
    assert client is transport and target.model == "GLM-5.3"
    assert target.kind == "harness" and target.api_key == ""


def test_watchdog_uses_shared_harness_with_its_existing_time_limit(
        tmp_path, monkeypatch):
    from infermatrix_copilot.engine.steps import rebase_v3

    calls = []

    class Transport:
        def complete(self, **kwargs):
            calls.append(kwargs)
            return Reply(blocks=[Block(type="text", text="CONTINUE")])

    def resolve(settings, backend):
        assert backend == "zcode"
        assert settings.strict_backend_timeout_s == 90.0
        return Transport()

    monkeypatch.setattr("infermatrix_copilot.providers.registry.transport_for_id",
                        resolve)
    ctx = SimpleNamespace(settings=Settings(
        _env_file=None, strict_backend="zcode", strict_backend_model="GLM-5.3"),
        state={}, run_dir=tmp_path)
    _, review, _ = rebase_v3._watchdog_collaborators(ctx)
    assert review("test", "suspicious output") == "CONTINUE"
    assert calls[0]["model"] == "GLM-5.3"


def test_mutating_session_uses_bridge_without_plan_mode_blocking_it(
        tmp_path):
    from test_provider_zcode import _capture, _request, _transport

    # Reuse the merged provider's fake CLI and request fixture.
    transport = _transport(tmp_path)
    req = _request(tmp_path)
    req.scope = type(req.scope)(name=req.scope.name,
                               allowed_tools=req.scope.allowed_tools,
                               root=req.scope.root, read_only=False)
    transport.run_session(req)
    argv = _capture(tmp_path)["argv"]
    assert argv[argv.index("--mode") + 1] == "yolo"
    denied = next(a for a in argv if a.startswith("--disallowed-tools="))
    assert all(tool in denied.split("=", 1)[1].split(",")
               for tool in ("Bash", "Write", "Edit", "Agent"))


@pytest.mark.parametrize("stop_reason", ["end_turn", "max_tokens"])
def test_plan_review_uses_shared_harness_and_rejects_partial_result(
        tmp_path, monkeypatch, stop_reason):
    calls = []

    class Transport:
        def complete(self, **kwargs):
            calls.append(kwargs)
            return Reply(blocks=[Block(type="text", text=json.dumps({
                "verdict": "approve", "summary": "Reviewed", "concerns": []}))],
                stop_reason=stop_reason, model="GLM-5.3")

    def resolve(settings, backend):
        assert backend == "zcode"
        return Transport()

    monkeypatch.setattr("infermatrix_copilot.providers.registry.transport_for_id",
                        resolve)
    # The bridge may retain API defaults in its settings file. The explicit
    # rebase selection must still control the reviewer without an API client.
    settings = Settings(_env_file=None, rebase_backend="zcode",
                        rebase_backend_model="GLM-5.3", agent_model="deepseek-v4-pro")
    backends = build_backends(settings=settings, state={}, run_dir=tmp_path,
                             trace=None, manifest={}, repo=str(tmp_path),
                             model="deepseek-v4-pro", api_key="")
    plan = tmp_path / "plan.json"
    plan.write_text('{"steps": ["check compatibility"]}')
    plan.with_suffix(".md").write_text("Check compatibility.")
    result = backends.request_plan_review(plan_json_path=str(plan))
    assert calls[0]["model"] == "GLM-5.3"
    assert "Check compatibility." in calls[0]["messages"][0]["content"]
    if stop_reason == "end_turn":
        assert result["verdict"] == "approve"
    else:
        assert "error" in result and "verdict" not in result
        assert not (tmp_path / "plan.review.json").exists()


def test_bridge_keeps_runtime_visibility_without_exposing_api_credentials(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "4,5,6,7")
    monkeypatch.setenv("LD_LIBRARY_PATH", "/usr/local/cuda/compat")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-be-in-config")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    transport = transport_for_id(Settings(_env_file=None, strict_backend="zcode"),
                                 "zcode")
    session = tmp_path / "session"
    session.mkdir()
    transport._write_mcp_config(session, tmp_path / "spec.json")
    config = json.loads((session / ".zcode/config.json").read_text())
    env = config["mcp"]["servers"]["infermatrix-tools"]["env"]
    assert env["CUDA_VISIBLE_DEVICES"] == "4,5,6,7"
    assert env["LD_LIBRARY_PATH"] == "/usr/local/cuda/compat"
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_BASE_URL" not in env
