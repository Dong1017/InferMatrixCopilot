"""Read projections, connection checks and safe operational service diagnostics."""
from __future__ import annotations

import json

from .models import ProviderError, RFCError
from .store import encode


def _number(value, name, minimum=0, maximum=1000000000):
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    if type(value) is not int or not minimum <= value <= maximum:
        raise RFCError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _boolean(value, name):
    if type(value) is bool:
        return int(value)
    if value in ("true", "1", 1):
        return 1
    if value in ("false", "0", 0):
        return 0
    raise RFCError(f"{name} must be a boolean")


def list_rfcs(service, con, principal, data):
    """Filter and count authorized rows before constructing any tracking views."""
    where, params = [], []
    if not principal.admin:
        where.append("EXISTS (SELECT 1 FROM repository_grants g WHERE g.repo_id=f.repo_id AND g.user_id=? AND g.role IN ('reader','contributor','maintainer'))")
        params.append(principal.user_id)
        where.append("(f.restricted=0 OR EXISTS (SELECT 1 FROM rfc_grants g WHERE g.rfc_id=f.id AND g.user_id=? AND g.role IN ('reader','contributor','maintainer')))")
        params.append(principal.user_id)
    if data.get("repo_id"):
        where.append("f.repo_id=?")
        params.append(data["repo_id"])
    if data.get("query"):
        query = data["query"]
        if not isinstance(query, str) or len(query) > 1000:
            raise RFCError("Search query must contain at most 1000 characters")
        where.append("instr(lower(f.title || char(10) || f.body),lower(?))>0")
        params.append(query)
    if data.get("state"):
        if not isinstance(data["state"], str) or len(data["state"]) > 128:
            raise RFCError("Invalid RFC state filter")
        where.append("COALESCE(json_extract(f.model,'$.state'),'proposed')=?")
        params.append(data["state"])
    if "enrolled" in data:
        where.append("f.enrolled=?")
        params.append(_boolean(data["enrolled"], "enrolled"))
    clause = " WHERE " + " AND ".join(where) if where else ""
    paged = "offset" in data or "limit" in data
    offset = _number(data.get("offset", 0), "offset")
    limit = _number(data.get("limit", 50), "limit", 1, 100)
    total = con.execute("SELECT count(*) FROM rfcs f" + clause, params).fetchone()[0] if paged else None
    sql = "SELECT f.* FROM rfcs f" + clause + " ORDER BY f.updated DESC,f.id"
    if paged:
        sql += " LIMIT ? OFFSET ?"
        params += [limit, offset]
    result = []
    for stored in con.execute(sql, params):
        row = dict(stored)
        model = None
        if data.get("view") == "summary":
            raw = json.loads(row["model"])
            # Summary projections require current facts, not history or raw
            # evidence payloads. Never clone those large arrays per RFC.
            keys = {"state", "features", "criteria", "observations", "suggestions", "freshness", "last_discovery",
                    "ambiguities", "enrollment", "writer_id", "source_update", "legacy_namespace", "namespace"}
            model = {key: raw[key] for key in keys if key in raw}
            model["features"] = [{key: value for key, value in item.items() if key not in
                                  {"implementation_history", "source_quote", "description", "history"}}
                                 for item in raw.get("features", [])]
            model["criteria"] = [{key: value for key, value in item.items() if key != "evidence"}
                                 for item in raw.get("criteria", [])]
            model["suggestions"] = [{"status": item.get("status", "proposed"), "evidence": {"source": item.get("evidence", {}).get("source")}}
                                    for item in raw.get("suggestions", [])]
        view = service._view(con, principal, row, parsed_model=model)
        if data.get("language"):
            view["translations"] = service.translations.snapshot(con, principal, {"id": view["id"], "title": view["title"]}, data["language"])
        result.append(service._compact_view(view, data.get("view")))
    response = {"rfcs": result}
    if paged:
        response.update(total=total, offset=offset, limit=limit)
    return response


def check_repository(service, principal, data):
    """A real read-only provider request, with permission checked on both sides."""
    repo_id = data.get("repo_id", "")
    with service.store.read() as con:
        principal = service._current(con, principal)
        service._require(con, principal, repo_id, "contributor")
        repo = dict(con.execute("SELECT * FROM repositories WHERE id=?", (repo_id,)).fetchone())
    provider = service.providers.get(repo["provider"])
    result = {"repo_id": repo_id, "provider": repo["provider"],
              "supported": repo["provider"] in {"local", "github", "atomgit"}, "configured": provider is not None,
              "verified": False, "checked": service.clock(), "code": "provider_unavailable"}
    if provider is not None:
        try:
            provider = service._provider(repo)
            if not hasattr(provider, "check_repository"):
                raise RFCError("This provider does not support repository connection checks", 503, "check_unsupported")
            result.update(provider.check_repository(repo["external_name"]))
            result.update(verified=True, code="ok")
        except RFCError as exc:
            result.update(code=exc.code, message=str(exc) if not isinstance(exc, ProviderError) else "Repository access could not be verified")
        except Exception:
            result.update(code="provider_failure", message="Repository access could not be verified")
    with service.store.transaction() as con:
        principal = service._current(con, principal)
        service._require(con, principal, repo_id, "contributor")
        con.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", ("repository_check:" + repo_id, encode(result)))
        service.store.audit(con, principal.user_id, "repositories.check", service.clock(), repo_id,
                            detail={"verified": result["verified"], "code": result["code"]})
    return result


def _model_readiness(component):
    if not component.enabled:
        return {"enabled": False, "available": False, "verified": False, "status": "disabled"}
    agent = getattr(component, "agent", None) or getattr(component, "translator", None)
    transport = getattr(agent, "transport", None)
    result = {"enabled": True, "available": False, "verified": False, "status": "unverified"}
    if transport is None:
        result.update(available=True, status="configured")
        return result
    try:
        cli = transport.cli_path()
        gap = transport.auth_gap() if cli else None
        result.update(available=bool(cli) and not gap, status="available_unverified" if cli and not gap else "authentication_required" if gap else "cli_missing")
    except Exception:
        result["status"] = "unavailable"
    return result


def service_status(service, con, *, diagnostics=False):
    now = service.clock()
    workers = []
    for row in con.execute("SELECT value FROM metadata WHERE key LIKE 'worker:%'"):
        try:
            worker = json.loads(row["value"])
        except (ValueError, TypeError):
            continue
        worker["fresh"] = not worker.get("stopped") and now - worker.get("updated", 0) <= 10
        workers.append(worker)
    worker_state = {}
    for kind in ("operations", "chat", "translations"):
        group = [worker for worker in workers if worker.get("kind") == kind]
        worker_state[kind] = {"available": any(worker["fresh"] for worker in group),
                              "updated": max((worker.get("updated", 0) for worker in group), default=None),
                              "busy": any(worker["fresh"] and worker.get("busy") for worker in group)}
    chat, translations = _model_readiness(service.chat), _model_readiness(service.translations)
    last_chat = con.execute("SELECT status,updated FROM chat_jobs WHERE status IN ('succeeded','failed') ORDER BY updated DESC LIMIT 1").fetchone()
    last_translation = con.execute("SELECT status,updated FROM translations WHERE status IN ('ready','failed') ORDER BY updated DESC LIMIT 1").fetchone()
    for readiness, row, success in ((chat, last_chat, "succeeded"), (translations, last_translation, "ready")):
        if row:
            readiness.update(last_result="succeeded" if row["status"] == success else "failed", last_checked=row["updated"])
            readiness["verified"] = row["status"] == success and now - row["updated"] <= 86400
    for component, readiness in (("chat", chat), ("translations", translations)):
        row = con.execute("SELECT value FROM metadata WHERE key=?", ("model_status:" + component,)).fetchone()
        state = json.loads(row["value"]) if row else {}
        error = state.get("last_model_error")
        readiness["last_model_error"] = error
        if error and state.get("updated", 0) >= readiness.get("last_checked", 0):
            readiness.update(status=error["code"], verified=False, last_result="failed", last_checked=state["updated"])
        readiness["ready"] = bool(readiness.get("enabled") and readiness.get("available") and readiness.get("verified") and worker_state[component]["available"])
    result = {"ok": True, "checked": now, "workers": worker_state,
              "chat": chat, "translations": translations, "public_registration": False, "workspace_v2": service.settings()["workspace_v2"]}
    if diagnostics:
        result["queues"] = {}
        for table in ("operations", "chat_jobs", "translations"):
            result["queues"][table] = {row["status"]: row["count"] for row in con.execute(f"SELECT status,count(*) count FROM {table} GROUP BY status")}
        result["providers"] = [{"provider": name, "supported": name in ("local", "github", "atomgit"),
                                "configured": True, "credential_present": bool(getattr(provider, "_token", "")) if name != "local" else None}
                               for name, provider in sorted(service.providers.items())]
        result["repository_checks"] = [json.loads(row["value"]) for row in con.execute("SELECT value FROM metadata WHERE key LIKE 'repository_check:%'")]
        result["workers_detail"] = workers
        result["database"] = {"journal_mode": con.execute("PRAGMA journal_mode").fetchone()[0], "read_transactions": True}
    return result
