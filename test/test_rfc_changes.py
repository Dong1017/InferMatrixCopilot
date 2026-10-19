"""Manual saves share the durable reviewed source-write path with Agent edits."""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from infermatrix_copilot.rfc_service.application import RFCService
from infermatrix_copilot.rfc_service.models import RFCError

from test_rfc_source_updates import BODY, setup


def preview(service, actor, rfc_id, **changes):
    rfc = service.dispatch(actor, "rfcs.get", {"rfc_id": rfc_id})
    return service.dispatch(actor, "rfcs.changes.preview", {
        "rfc_id": rfc_id, "expected_revision": rfc["revision"],
        "body": rfc["body"] + "\nReviewed manual design.\n", "reason": "Review manual changes", **changes})


def apply(service, actor, reviewed, **changes):
    return service.dispatch(actor, "rfcs.changes.apply", {
        "rfc_id": reviewed["rfc_id"], "preview_id": reviewed["preview_id"],
        "candidate_digest": reviewed["candidate_digest"], "idempotency_key": "apply-" + reviewed["preview_id"], **changes})


def add_user(service, admin, repo, name, role):
    user = service.dispatch(admin, "users.create", {"name": name})
    issued = service.dispatch(admin, "tokens.create", {"user_id": user["id"]})
    service.dispatch(admin, "grants.set", {"repo_id": repo["id"], "user_id": user["id"], "role": role})
    return service.authenticate(issued["token"]), issued


def test_source_direct_content_update_requires_review_but_workspace_drafts_save_normally(tmp_path):
    service, admin, repo, provider, rfc_id = setup(tmp_path)
    row = service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})
    for content in ({"body": BODY + "Unsynced"}, {"title": "Other title"}):
        with pytest.raises(RFCError) as blocked:
            service.dispatch(admin, "rfcs.update", {"rfc_id": rfc_id, "expected_revision": row["revision"], **content})
        assert blocked.value.code == "review_required"
    assert service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})["body"] == BODY
    draft = service.dispatch(admin, "rfcs.draft", {"repo_id": repo["id"], "title": "Draft", "body": BODY})
    saved = service.dispatch(admin, "rfcs.update", {"rfc_id": draft["id"], "expected_revision": draft["revision"], "body": BODY + "Unpublished"})
    assert saved["body"].endswith("Unpublished") and provider.writes == 0


def test_exact_manual_preview_apply_is_atomic_and_idempotent_across_restart(tmp_path):
    service, admin, _, provider, rfc_id = setup(tmp_path)
    reviewed = preview(service, admin, rfc_id)
    assert reviewed["candidate"]["source_revision"] == "s1"
    assert "Reviewed manual design" in reviewed["diff"] and reviewed["source_diff"]
    assert service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})["body"] == BODY
    assert not service.store.all("SELECT * FROM operations WHERE kind='rfcs.update_source'")
    saved = apply(service, admin, reviewed)
    assert saved["source_update"]["status"] == "pending" and saved["body"] == reviewed["body"]
    restarted = RFCService(tmp_path / "state", {"github": provider})
    assert apply(restarted, admin, reviewed)["operation_id"] == saved["operation_id"]
    assert len(restarted.store.all("SELECT * FROM operations WHERE kind='rfcs.update_source'")) == 1
    assert restarted.process_pending()[0]["status"] == "succeeded"
    assert apply(restarted, admin, reviewed)["source_update"]["status"] == "synced"
    assert provider.writes == 1 and provider.source["body"] == reviewed["body"]


def test_review_freezes_full_candidate_and_current_revision(tmp_path):
    service, admin, _, _, rfc_id = setup(tmp_path)
    reviewed = preview(service, admin, rfc_id)
    with pytest.raises(RFCError) as mismatch:
        apply(service, admin, reviewed, candidate_digest="altered")
    assert mismatch.value.code == "preview_mismatch"
    service.dispatch(admin, "rfcs.work", {"rfc_id": rfc_id, "op": "claim", "feature_id": "F1", "reason": "I own this task"})
    with pytest.raises(RFCError) as stale:
        apply(service, admin, reviewed)
    assert stale.value.code == "preview_mismatch"
    assert not service.store.all("SELECT * FROM operations WHERE kind='rfcs.update_source'")


def test_expiry_cross_actor_and_revocation_cannot_commit(tmp_path):
    service, admin, repo, _, rfc_id = setup(tmp_path)
    actor, token = add_user(service, admin, repo, "Maintainer", "maintainer")
    reviewed = preview(service, actor, rfc_id)
    with pytest.raises(RFCError) as other_actor:
        apply(service, admin, reviewed)
    assert other_actor.value.code == "forbidden"
    original_clock = service.clock
    service.clock = lambda: reviewed["expires"] + 1
    with pytest.raises(RFCError) as expired:
        apply(service, actor, reviewed)
    assert expired.value.code == "preview_expired"
    service.clock = original_clock
    service.dispatch(admin, "tokens.revoke", {"token_id": token["token_id"]})
    with pytest.raises(RFCError) as revoked:
        apply(service, actor, reviewed)
    assert revoked.value.status == 401


def test_permissions_rechecked_for_preview_apply_and_queued_write(tmp_path):
    service, admin, repo, provider, rfc_id = setup(tmp_path)
    reader, _ = add_user(service, admin, repo, "Reader", "reader")
    with pytest.raises(RFCError):
        preview(service, reader, rfc_id)
    actor, token = add_user(service, admin, repo, "Maintainer", "maintainer")
    reviewed = preview(service, actor, rfc_id)
    service.dispatch(admin, "grants.set", {"repo_id": repo["id"], "user_id": actor.user_id, "role": "reader"})
    with pytest.raises(RFCError) as denied:
        apply(service, actor, reviewed)
    assert denied.value.status == 403
    service.dispatch(admin, "grants.set", {"repo_id": repo["id"], "user_id": actor.user_id, "role": "maintainer"})
    saved = apply(service, actor, reviewed)
    service.dispatch(admin, "tokens.revoke", {"token_id": token["token_id"]})
    assert service.process_pending()[0]["status"] == "failed"
    assert provider.writes == 0 and service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})["body"] == saved["body"]


def test_concurrent_apply_queues_one_source_write_and_rejects_different_retry(tmp_path):
    service, admin, _, provider, rfc_id = setup(tmp_path)
    reviewed = preview(service, admin, rfc_id)
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: apply(service, admin, reviewed), range(2)))
    assert results[0]["operation_id"] == results[1]["operation_id"]
    with pytest.raises(RFCError) as key_changed:
        apply(service, admin, reviewed, idempotency_key="another-key")
    assert key_changed.value.code == "conflict"
    assert service.process_pending()[0]["status"] == "succeeded" and provider.writes == 1


def test_manual_markdown_definition_changes_preserve_facts_and_stable_acceptance(tmp_path):
    service, admin, _, _, rfc_id = setup(tmp_path)
    row = service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})
    criterion_id = row["criteria"][0]["id"]
    service.dispatch(admin, "rfcs.work", {"rfc_id": rfc_id, "op": "mark_done", "feature_id": "F1", "reason": "Confirmed implementation"})
    service.dispatch(admin, "rfcs.decision", {"rfc_id": rfc_id, "criterion_id": criterion_id, "verdict": "passing", "evidence": {
        "revision": "v1", "environment": "test", "result": "Verified output"}})
    changed = BODY.replace("F1. Implement", "F1. Implement reliably").replace("Verify output.", "Verify reliable output.")
    reviewed = preview(service, admin, rfc_id, body=changed)
    assert {change["op"] for change in reviewed["plan_diff"]} == {"update", "criterion_update"}
    saved = apply(service, admin, reviewed)
    assert saved["features"][0]["title"] == "Implement reliably"
    assert saved["features"][0]["implementation"] == "implemented"
    assert saved["criteria"][0]["id"] == criterion_id and saved["criteria"][0]["verdict"] == "pending"
    assert saved["criteria"][0]["evidence"][0]["stale"] is True
    assert saved["criteria_history"][0]["evidence"][0]["result"] == "Verified output"


def test_upgrade_preserves_old_unsynced_edits_and_sync_cannot_erase_them(tmp_path):
    service, admin, _, provider, rfc_id = setup(tmp_path)
    # Reproduce the previous unguarded rfcs.update result in a pre-upgrade DB.
    with service.store.transaction() as con:
        row = service._rfc(con, admin, rfc_id)
        service._save(con, admin, row, json.loads(row["model"]), BODY + "\nUnsynced human edit.\n")
    restarted = RFCService(tmp_path / "state", {"github": provider})
    protected = restarted.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})
    assert protected["source_update"]["status"] == "review_required"
    restarted.dispatch(admin, "rfcs.sync", {"rfc_id": rfc_id})
    assert restarted.process_pending()[0]["status"] == "succeeded"
    assert restarted.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})["body"] == protected["body"]
    reviewed = preview(restarted, admin, rfc_id, body=protected["body"])
    assert reviewed["source_conflict"] is True and "Unsynced human edit" in reviewed["source_diff"]
    saved = apply(restarted, admin, reviewed)
    assert "source_reconciliation" not in saved
    assert restarted.process_pending()[0]["status"] == "succeeded"
    assert provider.source["body"] == protected["body"]


def test_sync_preserves_pre_upgrade_local_difference_before_service_restart(tmp_path):
    service, admin, _, _, rfc_id = setup(tmp_path)
    with service.store.transaction() as con:
        row = service._rfc(con, admin, rfc_id)
        service._save(con, admin, row, json.loads(row["model"]), BODY + "Manual local draft")
    service.dispatch(admin, "rfcs.sync", {"rfc_id": rfc_id})
    assert service.process_pending()[0]["status"] == "succeeded"
    current = service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})
    assert current["body"].endswith("Manual local draft") and current["source_update"]["status"] == "review_required"


def test_fresh_source_preview_and_conflict_never_overwrite_local_candidate(tmp_path):
    service, admin, _, provider, rfc_id = setup(tmp_path)
    provider.source.update(body=BODY + "\nExternal edit.\n", revision="external-2")
    reviewed = preview(service, admin, rfc_id)
    assert reviewed["source_conflict"] is True and "External edit" in reviewed["source_diff"]
    saved = apply(service, admin, reviewed)
    provider.source.update(body=BODY + "A newer edit", revision="external-3")
    assert service.process_pending()[0]["status"] == "failed"
    retained = service.dispatch(admin, "rfcs.get", {"rfc_id": rfc_id})
    assert retained["body"] == saved["body"] and retained["source_update"]["status"] == "conflict"
    assert provider.writes == 0


def test_preview_provider_read_runs_without_sqlite_write_transaction(tmp_path):
    service, admin, _, provider, rfc_id = setup(tmp_path)
    original = provider.get_source
    def assert_unlocked(ref):
        con = service.store.connect()
        try:
            con.execute("PRAGMA busy_timeout=1")
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
        finally:
            con.close()
        return original(ref)
    provider.get_source = assert_unlocked
    assert preview(service, admin, rfc_id)["candidate"]["source_revision"] == "s1"


def test_http_review_and_apply_obey_sessions_acl_and_source_recovery(tmp_path):
    from test_rfc_http import request, running
    service, admin, repo, provider, rfc_id = setup(tmp_path)
    actor, token = add_user(service, admin, repo, "Browser maintainer", "maintainer")
    row = service.dispatch(actor, "rfcs.get", {"rfc_id": rfc_id})
    with running(service) as (server, _):
        status, headers, _ = request(server, "POST", "/api/v1/session", {"token": token["token"]},
                                     headers={"Origin": server.public_origin})
        assert status == 200
        browser = {"Cookie": headers["set-cookie"].split(";", 1)[0], "Origin": server.public_origin}
        content = {"rfc_id": rfc_id, "expected_revision": row["revision"], "body": BODY + "\nBrowser edit.\n", "reason": "Reviewed in browser"}
        assert request(server, "POST", "/api/v1/actions/rfcs.changes.preview", content)[0] == 401
        status, _, raw = request(server, "POST", "/api/v1/actions/rfcs.changes.preview", content, headers=browser)
        assert status == 200
        reviewed = json.loads(raw)
        payload = {"rfc_id": rfc_id, "preview_id": reviewed["preview_id"], "candidate_digest": reviewed["candidate_digest"], "idempotency_key": "browser-apply"}
        # Cookie writes still require their trusted origin, including reviewed saves.
        assert request(server, "POST", "/api/v1/actions/rfcs.changes.apply", payload, headers={"Cookie": browser["Cookie"]})[0] == 403
        status, _, raw = request(server, "POST", "/api/v1/actions/rfcs.changes.apply", payload, headers=browser)
        saved = json.loads(raw)
        assert status == 200 and saved["source_update"]["status"] == "pending"
        assert request(server, "POST", "/api/v1/actions/rfcs.changes.apply", payload, headers=browser)[0] == 200
        assert service.process_pending()[0]["status"] == "succeeded"
        assert provider.writes == 1 and provider.source["body"] == content["body"]
        service.dispatch(admin, "rfcs.acl", {"rfc_id": rfc_id, "restricted": True, "grants": {}})
        assert request(server, "POST", "/api/v1/actions/rfcs.changes.apply", payload, headers=browser)[0] == 403


def test_sdk_reviewed_methods_use_public_http_and_preserve_preview_reason(tmp_path):
    from infermatrix_copilot.sdk.v1 import RFCClient, RFCClientError
    from test_rfc_http import running
    service, admin, repo, provider, rfc_id = setup(tmp_path)
    _, token = add_user(service, admin, repo, "SDK maintainer", "maintainer")
    with running(service) as (server, _):
        client = RFCClient(service_url=server.public_origin, token=token["token"])
        row = client.get(rfc_id)
        reviewed = client.changes_preview(rfc_id, expected_revision=row["revision"],
            body=BODY + "\nSDK reviewed update.\n", reason="Review the SDK candidate")
        arguments = {"preview_id": reviewed["preview_id"], "candidate_digest": reviewed["candidate_digest"], "idempotency_key": "sdk-reviewed-apply"}
        saved = client.changes_apply(rfc_id, **arguments)
        assert client.changes_apply(rfc_id, **arguments)["operation_id"] == saved["operation_id"]
        assert service.process_pending()[0]["status"] == "succeeded"
        assert provider.writes == 1 and provider.source["body"] == reviewed["body"]
        with pytest.raises(RFCClientError) as mismatch:
            client.changes_apply(rfc_id, **{**arguments, "candidate_digest": "wrong"})
        assert mismatch.value.code == "preview_mismatch" and mismatch.value.status == 409
        service.dispatch(admin, "tokens.revoke", {"token_id": token["token_id"]})
        with pytest.raises(RFCClientError) as revoked:
            client.changes_apply(rfc_id, **arguments)
        assert revoked.value.status == 401
