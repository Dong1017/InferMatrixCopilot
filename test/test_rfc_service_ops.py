"""Usable service contracts: read concurrency, filtered paging and safe recovery data."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import pytest

from infermatrix_copilot.rfc_service.application import RFCService
from infermatrix_copilot.rfc_service.models import RFCError
from infermatrix_copilot.rfc_service.providers import GitHubProvider, LocalProvider
from infermatrix_copilot.rfc_service.runtime import WorkerMonitor
from test_rfc_http import request, running

BODY = "# Delivery\n\n### Engine\n\n#### F1. Implementation\n\n## Acceptance criteria\n\n- Verify deployed behavior.\n"


def setup(tmp_path, provider=None, clock=time.time):
    service = RFCService(tmp_path, {"local": LocalProvider({}), "github": provider} if provider else {"local": LocalProvider({})}, clock=clock)
    issued = service.bootstrap_admin("Owner")
    admin = service.authenticate(issued["token"])
    repo = service.dispatch(admin, "repositories.create", {"name": "Visible", "provider": "github" if provider else "local", "external_name": "owner/project"})
    created = service.dispatch(admin, "users.create", {"name": "Reader"})
    token = service.dispatch(admin, "tokens.create", {"user_id": created["id"]})
    service.dispatch(admin, "grants.set", {"repo_id": repo["id"], "user_id": created["id"], "role": "reader"})
    return service, admin, repo, service.authenticate(token["token"]), issued, token


def draft(service, admin, repo, title):
    return service.dispatch(admin, "rfcs.draft", {"repo_id": repo["id"], "title": title, "body": BODY})


def test_filtered_paging_counts_only_accessible_rfcs_and_is_backward_compatible(tmp_path):
    service, admin, repo, reader, _, _ = setup(tmp_path)
    visible = [draft(service, admin, repo, "Search Delivery " + str(index)) for index in range(4)]
    hidden = draft(service, admin, repo, "Search Secret")
    service.dispatch(admin, "rfcs.acl", {"rfc_id": hidden["id"], "restricted": True, "grants": {}})
    secret_repo = service.dispatch(admin, "repositories.create", {"name": "Hidden", "provider": "local"})
    draft(service, admin, secret_repo, "Search Cross Repository")
    full = service.dispatch(reader, "rfcs.list")
    assert set(full) == {"rfcs"} and len(full["rfcs"]) == 4
    page = service.dispatch(reader, "rfcs.list", {"query": "Search", "offset": "1", "limit": "2", "view": "summary", "enrolled": "false", "state": "draft"})
    assert (page["total"], page["offset"], page["limit"]) == (4, 1, 2)
    assert len(page["rfcs"]) == 2
    assert all("body" not in item and "features" not in item for item in page["rfcs"])
    assert "Secret" not in json.dumps(page)
    assert service.dispatch(reader, "rfcs.list", {"limit": 1, "query": "none"})["total"] == 0
    for values in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": True}, {"enrolled": "maybe"}):
        with pytest.raises(RFCError):
            service.dispatch(reader, "rfcs.list", values)


def test_authentication_and_reads_do_not_wait_for_an_unrelated_writer(tmp_path):
    service, admin, repo, reader, _, token = setup(tmp_path)
    draft(service, admin, repo, "Concurrent")
    statements = []
    connect = service.store.connect
    def traced():
        con = connect()
        con.set_trace_callback(statements.append)
        return con
    service.store.connect = traced
    with service.store.transaction() as con:
        con.execute("UPDATE users SET name=name WHERE id=?", (admin.user_id,))
        with ThreadPoolExecutor(1) as pool:
            def read():
                principal = service.authenticate(token["token"])
                return service.dispatch(principal, "rfcs.list", {"view": "summary", "limit": 10})
            result = pool.submit(read).result(timeout=1)
    assert len(result["rfcs"]) == 1
    assert statements.count("BEGIN IMMEDIATE") == 1
    assert statements.count("BEGIN") >= 2


def test_connection_check_reads_provider_outside_transaction_and_rechecks_access(tmp_path):
    observations = []
    holder = {}
    def transport(method, url, headers, payload):
        observations.append((method, url, payload))
        # This independent write would block if network I/O held the writer.
        service = holder["service"]
        service.dispatch(holder["admin"], "grants.set", {"repo_id": holder["repo"]["id"], "user_id": holder["actor"].user_id, "role": ""})
        return 200, {}, {"id": 1, "permissions": {"push": True}}
    provider = GitHubProvider(token="never-log-provider-token", transport=transport)
    service, admin, repo, actor, _, _ = setup(tmp_path, provider)
    service.dispatch(admin, "grants.set", {"repo_id": repo["id"], "user_id": actor.user_id, "role": "contributor"})
    holder.update(service=service, admin=admin, repo=repo, actor=actor)
    with pytest.raises(RFCError) as exc:
        service.dispatch(actor, "repositories.check", {"repo_id": repo["id"]})
    assert exc.value.code == "forbidden"
    assert observations == [("GET", "https://api.github.com/repos/owner/project", None)]
    assert service.store.one("SELECT value FROM metadata WHERE key=?", ("repository_check:" + repo["id"],)) is None


def test_grants_status_and_diagnostics_have_current_authorization(tmp_path):
    service, admin, repo, reader, _, _ = setup(tmp_path)
    access = service.dispatch(admin, "grants.list")
    assert access["grants"][1]["user_name"] == "Reader"
    checked = service.dispatch(admin, "repositories.check", {"repo_id": repo["id"]})
    assert checked["supported"] and checked["configured"] and checked["verified"] and checked["readable"]
    assert "root" not in checked
    for action in ("grants.list", "service.diagnostics"):
        with pytest.raises(RFCError) as exc:
            service.dispatch(reader, action)
        assert exc.value.status == 403
    status = service.dispatch(reader, "service.status")
    assert "queues" not in status and "repository_checks" not in status
    stop = threading.Event()
    monitor = WorkerMonitor(service, stop)
    monitor.register("operations", "operations")
    monitor.flush()
    assert service.dispatch(reader, "service.status")["workers"]["operations"]["available"]
    monitor.flush(stopped=True)
    assert not service.dispatch(reader, "service.status")["workers"]["operations"]["available"]
    assert service.dispatch(admin, "service.diagnostics")["database"]["journal_mode"] == "wal"
    service.dispatch(admin, "service.configure", {"workspace_v2": False})
    assert service.dispatch(reader, "me")["workspace_v2"] is False


def test_browser_session_reports_and_uses_actual_token_expiry(tmp_path):
    now = [1000.0]
    service, admin, repo, reader, _, _ = setup(tmp_path, clock=lambda: now[0])
    token = service.dispatch(admin, "tokens.create", {"expires_days": 10 / 86400})
    secret, principal = service.create_session(token["token"])
    assert principal.expires == principal.credential_expires == principal.session_expires == 1010
    assert service.dispatch(principal, "me")["expires"] == 1010
    with running(service) as (server, _):
        status, headers, raw = request(server, "POST", "/api/v1/session", {"token": token["token"]}, headers={"Origin": server.public_origin})
        assert status == 200 and "Max-Age=10" in headers["set-cookie"]
        assert json.loads(raw)["principal"]["expires"] == 1010
    now[0] = 1010
    with pytest.raises(RFCError):
        service.authenticate_session(secret)


def test_health_and_error_request_id_reveal_no_private_data(tmp_path):
    service, admin, repo, _, token, _ = setup(tmp_path)
    draft(service, admin, repo, "Private title")
    with running(service) as (server, _):
        status, headers, raw = request(server, "GET", "/healthz")
        assert status == 200 and json.loads(raw) == {"ok": True}
        assert headers["x-request-id"]
        status, headers, raw = request(server, "GET", "/api/v1/rfcs")
        error = json.loads(raw)["error"]
        assert status == 401 and error["request_id"] == headers["x-request-id"]
        status, headers, raw = request(server, "GET", "/api/v1/service/status", headers={"Authorization": "Bearer " + token["token"]})
        assert status == 200 and "Private title" not in raw.decode()


def test_work_wake_is_after_commit_and_rollback_does_not_wake(tmp_path):
    service, admin, repo, _, _, _ = setup(tmp_path)
    value = draft(service, admin, repo, "Queued")
    service.store.work_available.clear()
    with service.store.transaction() as con:
        service._enqueue(con, admin, "rfcs.sync", repo["id"], value["id"], {})
        assert not service.store.work_available.is_set()
    assert service.store.work_available.is_set()
    service.store.work_available.clear()
    with pytest.raises(RuntimeError):
        with service.store.transaction() as con:
            service._enqueue(con, admin, "rfcs.sync", repo["id"], value["id"], {})
            raise RuntimeError("rollback")
    assert not service.store.work_available.is_set()
