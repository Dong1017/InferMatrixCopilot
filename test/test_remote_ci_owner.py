"""Prospective CPU regressions; fixture provenance binds the actual campaign.

Run only in a deliberate isolated future source epoch. No provider, subprocess,
model import, TestRunner, GPU allocation or actual source action is needed.
"""
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import pytest
import yaml

from infermatrix_copilot.engine.steps import rebase_v3
from infermatrix_copilot.rebase_engine import ci_loop
from infermatrix_copilot.rebase_engine.remote_ci_owner import RemoteOwnerResolver
from infermatrix_copilot.rebase_engine.test_manifest import (
    _extract_steps, _label_to_slug, ManifestSpec,
)

FIXTURES = Path(__file__).parent / "fixtures" / "remote_ci_owner"


@pytest.fixture
def campaign(tmp_path):
    manifest = yaml.safe_load((FIXTURES / "adapter_manifest.yaml").read_text())
    native = json.loads((FIXTURES / "native73_manifest.json").read_text())
    expected = json.loads((FIXTURES / "expected106_routes.json").read_text())
    repo = tmp_path / "repo"
    shutil.copytree(FIXTURES / "repo", repo)
    return repo, manifest, native, expected


def test_actual_106_keeps_84_local_routes_and_22_declared_shared_routes(campaign):
    repo, manifest, native, expected = campaign
    original_native = json.dumps(native, sort_keys=True)
    original_manifest = copy.deepcopy(manifest)
    resolver = RemoteOwnerResolver(repo, manifest, expected["active_modules"])
    local = {j["slug"]: j for j in native["jobs"]}
    results = {leaf["expected_leaf_id"]: resolver.resolve(leaf["name_template"], local)
               for leaf in expected["leaves"] if leaf["expected_enabled_under_campaign_env"]}
    assert len(results) == 106  # occurrence IDs, never label-deduplicated
    assert len(local) == 73
    assert sum(j["source"] != "nightly" for j in native["jobs"]) == 40
    missing = set(expected["missing_leaf_ids"])
    assert len(missing) == 22
    assert {key for key, result in results.items() if result.status == "declared_shared"} == missing
    for leaf in expected["leaves"]:
        if not leaf["expected_enabled_under_campaign_env"]:
            continue
        key = leaf["expected_leaf_id"]
        result = results[key]
        if key not in missing:
            original = local[_label_to_slug(leaf["name_template"])]
            assert result.module == original["module"] and result.status == "local"
        else:
            assert result.status == "declared_shared"
            assert result.reason == "explicit_declared_shared_root"
            assert result.module == "platform"  # exact positive tests/ declaration
    assert json.dumps(native, sort_keys=True) == original_native
    assert manifest == original_manifest


def test_actual_doc_glob_expands_both_modes_under_declared_shared_responsibility(campaign):
    repo, manifest, native, expected = campaign
    resolver = RemoteOwnerResolver(repo, manifest, expected["active_modules"])
    leaf = next(x for x in expected["leaves"] if x["expected_leaf_id"] == "nightly:022")
    result = resolver.resolve(leaf["name_template"], {j["slug"]: j for j in native["jobs"]})
    assert result.paths == (
        "tests/examples/offline_inference/test_text_to_image.py",
        "tests/examples/online_serving/test_text_to_image.py",
    )
    assert result.module == "platform" and result.status == "declared_shared"


def test_real_extractor_skip_is_local_only_and_leaf_budget_unchanged(campaign):
    repo, manifest, _, _ = campaign
    spec = ManifestSpec.from_manifest(manifest)
    data = yaml.safe_load((repo / spec.yaml_dir / "test-nightly.yml").read_text())
    local = _extract_steps(data["steps"], "nightly", spec)
    missing = "full_moon_omni_accuracy_test"
    assert missing not in {j.slug for j in local}
    resolver = RemoteOwnerResolver(repo, manifest, ["model_executor", "platform"])
    remote = _extract_steps(data["steps"], "nightly", resolver.spec)
    assert missing in {j.slug for j in remote}
    for job in local:
        equivalent = next(j for j in remote if j.slug == job.slug)
        assert (equivalent.command, equivalent.timeout_sec, equivalent.min_gpus,
                equivalent.hw, equivalent.env) == (
                    job.command, job.timeout_sec, job.min_gpus, job.hw, job.env)


def _recipe(tmp_path, command, modules):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".ci").mkdir()
    (repo / ".ci/jobs.yml").write_text(yaml.safe_dump({"steps": [{
        "label": "Accuracy check", "command": command, "timeout_in_minutes": 9,
    }]}))
    manifest = {"modules": modules, "rebase": {"test_manifest": {
        "yaml_dir": ".ci", "pipelines": {"jobs.yml": "nightly"},
        "skip_patterns": ["Accuracy"],
        "file_ref_prefixes": ["tests", "pkg"],
        "file_tree_roots": ["tests", "pkg"],
        "assignment_paths": {"platform": ["tests/"]},
    }}}
    return repo, manifest


def test_missing_remote_specific_route_and_equal_specificity_ambiguity(tmp_path):
    modules = {"model_executor": {"test_paths": ["tests/models/"]},
               "platform": {"test_paths": ["tests/"]}}
    repo, manifest = _recipe(tmp_path, "pytest tests/models/test_model.py", modules)
    (repo / "tests/models").mkdir(parents=True)
    (repo / "tests/models/test_model.py").write_text("assert True\n")
    resolver = RemoteOwnerResolver(repo, manifest, list(modules))
    result = resolver.resolve("Accuracy check", {})
    assert result.module == "model_executor" and result.status == "declared"
    manifest["modules"]["worker_runner"] = {"test_paths": ["tests/models/"]}
    result = RemoteOwnerResolver(repo, manifest, list(manifest["modules"])).resolve("Accuracy check", {})
    assert result.module == "" and result.status == "ambiguous"
    assert set(result.candidate_modules) == {"model_executor", "worker_runner"}


@pytest.mark.parametrize("command", [
    "pytest tests/../outside/test.py", "pytest tests/**/test.py", "pytest tests/missing.py",
])
def test_unsafe_recursive_or_missing_reference_is_explicit_unmapped(tmp_path, command):
    repo, manifest = _recipe(tmp_path, command, {"model_executor": {"test_paths": ["tests/"]}})
    result = RemoteOwnerResolver(repo, manifest, ["model_executor"]).resolve("Accuracy check", {})
    assert not result.module and result.reason == "remote_routing_view_unavailable"



def test_malformed_pipeline_and_cross_source_conflict_are_not_arbitrary_winners(tmp_path):
    modules = {"models": {"test_paths": ["tests/models/"]},
               "worker": {"test_paths": ["tests/worker/"]}}
    repo, manifest = _recipe(tmp_path, "pytest tests/models/test.py", modules)
    for kind in ("models", "worker"):
        (repo / f"tests/{kind}").mkdir(parents=True)
        (repo / f"tests/{kind}/test.py").write_text("assert True\n")
    path = repo / ".ci/second.yml"
    path.write_text((repo / ".ci/jobs.yml").read_text().replace("tests/models/", "tests/worker/"))
    manifest["rebase"]["test_manifest"]["pipelines"]["second.yml"] = "ready"
    resolver = RemoteOwnerResolver(repo, manifest, list(modules))
    result = resolver.resolve("Accuracy check", {})
    assert result.status == "ambiguous" and not result.module
    assert set(result.sources) == {"nightly", "ready"}
    path.write_text("- malformed\n")
    assert resolver.resolve("Accuracy check", {}).reason == "remote_routing_view_unavailable"

def test_symlink_cannot_route_outside_repo(tmp_path):
    repo, manifest = _recipe(tmp_path, "pytest tests/link.py", {"models": {"test_paths": ["tests/link.py"]}})
    (repo / "tests").mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("assert True\n")
    (repo / "tests/link.py").symlink_to(outside)
    assert RemoteOwnerResolver(repo, manifest, ["models"]).resolve("Accuracy check", {}).module == ""


def test_glob_bound_and_imported_shared_helper_do_not_create_owner(tmp_path):
    repo, manifest = _recipe(tmp_path, "pytest tests/accuracy/*.py", {
        "model_executor": {"local_paths": ["pkg/models/"]},
        "platform": {"test_paths": ["tests/"]},
    })
    (repo / "tests/accuracy").mkdir(parents=True)
    test = repo / "tests/accuracy/test_accuracy.py"
    test.write_text("from pkg.models.shared import metadata_helper\n")
    resolver = RemoteOwnerResolver(repo, manifest, ["model_executor", "platform"])
    result = resolver.resolve("Accuracy check", {})
    assert result.module == "platform" and result.status == "declared_shared"
    assert result.module != "model_executor"  # import of metadata_helper is incidental
    for index in range(512):
        (test.parent / f"test_{index}.py").write_text("assert True\n")
    assert resolver.resolve("Accuracy check", {}).reason == "remote_routing_view_unavailable"


def test_local_route_survives_absent_remote_recipe_and_no_stale_remote_cache(tmp_path):
    assert RemoteOwnerResolver(tmp_path, {}, []).resolve(
        "Local", {"local": {"module": "worker_runner"}}).module == "worker_runner"
    modules = {"models": {"test_paths": ["tests/models/"]},
               "worker": {"test_paths": ["tests/worker/"]}}
    repo, manifest = _recipe(tmp_path, "pytest tests/models/test.py", modules)
    for kind in ("models", "worker"):
        (repo / f"tests/{kind}").mkdir(parents=True)
        (repo / f"tests/{kind}/test.py").write_text("assert True\n")
    resolver = RemoteOwnerResolver(repo, manifest, list(modules))
    assert resolver.resolve("Accuracy check", {}).module == "models"
    path = repo / ".ci/jobs.yml"
    path.write_text(path.read_text().replace("tests/models/", "tests/worker/"))
    assert resolver.resolve("Accuracy check", {}).module == "worker"



def test_unknown_label_no_test_path_and_paths_outside_positive_footprints_remain_unmapped(tmp_path):
    repo, manifest = _recipe(tmp_path, "echo collecting", {
        "model_executor": {"test_paths": ["tests/models/"]},
    })
    resolver = RemoteOwnerResolver(repo, manifest, ["model_executor"])
    assert resolver.resolve("Unknown job", {}).reason == "no_declared_remote_label"
    assert resolver.resolve("Accuracy check", {}).module == ""
    (repo / "tests/accuracy").mkdir(parents=True)
    (repo / "tests/accuracy/test.py").write_text("assert True\n")
    path = repo / ".ci/jobs.yml"
    path.write_text(path.read_text().replace("echo collecting", "pytest tests/accuracy/test.py"))
    result = resolver.resolve("Accuracy check", {})
    assert result.module == "" and result.status == "unmapped"


@pytest.mark.parametrize("command", [
    "pytest tests/accuracy/$CASE",
    "pytest tests/accuracy/${CASE}",
    'pytest tests/accuracy/"$CASE"',
    "pytest tests/accuracy/test.py$SUFFIX",
    "pytest tests/accuracy/test.py$(printf x)",
    "pytest tests/accuracy/test.py::test_case[$PARAM]",
])
def test_complete_dynamic_reference_never_degrades_to_existing_directory(tmp_path, command):
    repo, manifest = _recipe(tmp_path, command, {"platform": {"test_paths": ["tests/"]}})
    (repo / "tests/accuracy").mkdir(parents=True)
    (repo / "tests/accuracy/test.py").write_text("assert True\n")
    result = RemoteOwnerResolver(repo, manifest, ["platform"]).resolve("Accuracy check", {})
    assert not result.module and result.status == "unmapped"
    assert result.reason == "remote_routing_view_unavailable"


@pytest.mark.parametrize("command", [
    "pytest 'tests/accuracy/test.py'::test_case[plain]",
    "pytest tests/accuracy/test.py::TestClass::test_case[plain]",
    "pytest --test-file=tests/accuracy/test.py",
    "pytest tests/accuracy/test.py; echo complete",
])
def test_complete_literal_quoted_path_and_pytest_node_are_preserved(tmp_path, command):
    repo, manifest = _recipe(tmp_path, command, {"platform": {"test_paths": ["tests/"]}})
    (repo / "tests/accuracy").mkdir(parents=True)
    (repo / "tests/accuracy/test.py").write_text("assert True\n")
    result = RemoteOwnerResolver(repo, manifest, ["platform"]).resolve("Accuracy check", {})
    assert result.module == "platform" and result.status == "declared_shared"
    assert result.paths == ("tests/accuracy/test.py",)


@pytest.mark.parametrize("field,value", [
    (None, True), (("modules",), ["platform"]),
    (("modules", "platform"), True),
    (("modules", "platform", "test_paths"), "tests/"),
    (("modules", "platform", "local_paths"), [["pkg/platform/"]]),
    (("rebase",), True), (("rebase", "test_manifest"), True),
    (("rebase", "test_manifest", "assignment_paths"), []),
    (("rebase", "test_manifest", "file_ref_prefixes"), [True]),
    (("rebase", "test_manifest", "file_tree_roots"), "tests"),
    (("rebase", "test_manifest", "pipelines"), {"jobs.yml": True}),
])
def test_malformed_manifest_does_not_destroy_existing_local_route(tmp_path, field, value):
    repo, manifest = _recipe(tmp_path, "pytest tests/accuracy/test.py", {
        "platform": {"test_paths": ["tests/"]},
    })
    if field is None:
        invalid_manifest = value
    else:
        invalid_manifest = copy.deepcopy(manifest)
        target = invalid_manifest
        for key in field[:-1]:
            target = target[key]
        target[field[-1]] = value
    resolver = RemoteOwnerResolver(repo, invalid_manifest, ["platform"])
    local = resolver.resolve("Local", {"local": {"module": "worker_runner"}})
    assert local.module == "worker_runner" and local.status == "local"
    missing = resolver.resolve("Remote", {})
    assert not missing.module and missing.reason == "remote_routing_view_unavailable"


def test_non_string_remote_labels_and_bad_step_shape_are_unmapped(tmp_path):
    repo, manifest = _recipe(tmp_path, "pytest tests/accuracy/test.py", {"platform": {"test_paths": ["tests/"]}})
    (repo / "tests/accuracy").mkdir(parents=True)
    (repo / "tests/accuracy/test.py").write_text("assert True\n")
    path = repo / ".ci/jobs.yml"
    path.write_text("steps:\n- label: true\n  command: pytest tests/accuracy/test.py\n")
    resolver = RemoteOwnerResolver(repo, manifest, ["platform"])
    assert resolver.resolve("Accuracy check", {}).reason == "remote_routing_view_unavailable"
    assert resolver.resolve(True, {}).reason == "invalid_remote_label"
    assert resolver.resolve("Local", {"local": {"module": "worker_runner"}}).module == "worker_runner"
    path.write_text("steps:\n- group: bad\n  steps: true\n")
    assert resolver.resolve("Accuracy check", {}).reason == "remote_routing_view_unavailable"

def test_actual_phase4_dispatches_duplicate_job_ids_and_shared_routes_without_pass_credit(campaign, tmp_path, monkeypatch):
    """Exercise the production _v3_ci callback, not a copied dispatch body."""
    repo, manifest, native, expected = campaign
    events, dispatched, restored = [], [], []
    settings = SimpleNamespace(buildkite_api_token="cpu-fixture-only",
                               protected_branches=["main"], allow_push=False,
                               rebase_ci_retries=1, rebase_ci_settle_sec=0,
                               rebase_ci_poll_sec=0, rebase_ci_timeout_sec=1,
                               rebase_ci_job_retry_max=0)
    state = {"task_spec": {"params": {"rebase_mode": "full"}},
             "upstream_commit": expected["target_commit"], "run_id": "cpu-route-fixture",
             "active_modules": expected["active_modules"]}
    ctx = SimpleNamespace(settings=settings, state=state, run_dir=tmp_path / "run",
                          trace=SimpleNamespace(record=lambda event, **kw: events.append((event, kw))))
    ctx.run_dir.mkdir()
    sub = SimpleNamespace(read=lambda: {}, update=lambda patch: None)
    monkeypatch.setattr(rebase_v3, "require_repo", lambda _: str(repo))
    monkeypatch.setattr(rebase_v3, "_adapter_manifest", lambda _: manifest)
    monkeypatch.setattr(rebase_v3, "_ensure_checkout_locks", lambda *args: None)
    monkeypatch.setattr(rebase_v3, "_substate", lambda _: sub)
    monkeypatch.setattr(rebase_v3, "_make_ci_client", lambda *args, **kw: object())
    monkeypatch.setattr(rebase_v3, "_tier_client", lambda _: (object(), "cpu"))
    monkeypatch.setattr(rebase_v3, "build_manifest", lambda *args: SimpleNamespace(to_dict=lambda: native))
    monkeypatch.setattr(rebase_v3, "_worktree_digest", lambda _: "unchanged")
    monkeypatch.setattr(rebase_v3, "capture_patch_policy", lambda _: object())
    monkeypatch.setattr(rebase_v3.tl, "snapshot_worktree", lambda _: (None, []))
    monkeypatch.setattr(rebase_v3.tl, "restore_worktree", lambda *args: restored.append(args))
    from infermatrix_copilot.engine import lifecycle
    monkeypatch.setattr(lifecycle, "register_finalizer", lambda *args: None)
    import subprocess
    def git_result(argv, **kwargs):
        assert argv[0] == "git"  # never execute a real subprocess/provider
        output = "dev/vllm-align\n" if "--abbrev-ref" in argv else "a" * 40
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    monkeypatch.setattr(subprocess, "run", git_result)
    monkeypatch.setattr(ci_loop, "fetch_baseline_failures", lambda *args: ())
    async def fail_debug(_ctx, _manifest, module, slug, detail):
        dispatched.append((module, slug))
        return "failed"  # route resolution never fabricates an accepted fix
    monkeypatch.setattr(rebase_v3, "_run_debug_agent", fail_debug)
    async def fake_ci(**kwargs):
        for leaf in expected["leaves"]:
            if not leaf["expected_enabled_under_campaign_env"]:
                continue
            job = ci_loop.JobResult(name=leaf["name_template"],
                                    job_id=leaf["expected_leaf_id"], state="failed")
            assert await kwargs["debug_fn"](job) is False
        return SimpleNamespace(result="failed", reason="fixture failures preserved",
                               fixed_jobs=[], unfixed_jobs=[], rounds=[])
    monkeypatch.setattr(ci_loop, "run_ci_rounds", fake_ci)
    result = asyncio.run(rebase_v3._v3_ci(ctx))
    assert result.outputs["state_updates"]["ci_result"] == "failed"
    routes = [kw for event, kw in events if event == "ci_debug_owner_route"]
    assert len(routes) == len(dispatched) == len(restored) == 106
    assert len({r["job_id"] for r in routes}) == 106
    assert {r["job_id"] for r in routes if r["status"] == "declared_shared"} == set(expected["missing_leaf_ids"])
    pair = [r for r in routes if r["job_id"] in {"ready:000", "merge:000"}]
    assert len(pair) == 2 and pair[0]["slug"] == pair[1]["slug"]
    assert all(r["status"] == "local" for r in pair)
    # Neither routing coverage nor two matching labels is a full106 pass.
    assert result.summary.startswith("CI failed:")
