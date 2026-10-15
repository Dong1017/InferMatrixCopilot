"""Frozen, actor-owned previews for manual RFC edits.

Preview preparation may read a provider, but never holds a database transaction
while doing so. The same commit path serves manual edits and Agent proposals.
"""
from __future__ import annotations

import copy
import difflib
import json
import uuid
from collections import Counter

from .drafts import digest, parse
from .models import RFCError, SourceRef
from .store import encode

PREVIEW_SECONDS = 1800


def _diff(before, after, source="saved RFC", target="reviewed RFC"):
    return "".join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True),
                                        fromfile=source, tofile=target))


def _document(title, body):
    return "Title: " + title + "\n\n" + body


def infer_plan_changes(row, body):
    """Project human Markdown edits into explicit, reviewable definition deltas.

    Tracking facts come from the saved model. A removed/replaced acceptance
    definition is archived by apply_plan_changes instead of losing its evidence.
    """
    from .chat import FEATURE_FIELDS, validate_plan_changes
    old = json.loads(row["model"])
    before, after = parse(row["body"]), parse(body)
    if after.get("ambiguities") != before.get("ambiguities"):
        raise RFCError("Resolve the new unknown task dependencies before saving", 409, "invalid_dependencies")
    raw_before = {item["id"]: item for item in before["features"]}
    raw_after = {item["id"]: item for item in after["features"]}
    tracked = {item["id"]: item for item in old["features"]}
    changes = []
    for identity in sorted(raw_before.keys() | raw_after.keys()):
        prior, current = raw_before.get(identity), raw_after.get(identity)
        if current is None:
            changes.append({"op": "drop", "feature_id": identity, "fields": {}})
            continue
        fields = {}
        for name in sorted(FEATURE_FIELDS - {"priority"}):
            if prior is not None and prior.get(name) == current.get(name):
                continue
            value = copy.deepcopy(current.get(name))
            if name == "links" and prior is not None:
                removed = set(prior.get("links", [])) - set(current.get("links", []))
                value = [link for link in tracked.get(identity, {}).get("links", []) if link not in removed]
                value += [link for link in current.get("links", []) if link not in value]
            fields[name] = value
        if prior is None:
            if identity in set(old.get("tombstones", [])) or tracked.get(identity, {}).get("dropped"):
                raise RFCError("A removed task cannot be restored by editing prose; restore it explicitly first", 409, "conflict")
            # A sidecar task may acquire a source description without changing identity.
            op = "update" if identity in tracked else "add"
            if op == "update":
                fields = {key: value for key, value in fields.items() if tracked[identity].get(key) != value}
            if fields:
                changes.append({"op": op, "feature_id": identity, "fields": fields})
        elif fields:
            changes.append({"op": "update", "feature_id": identity, "fields": fields})

    before_counts = Counter(item["title"] for item in before["criteria"])
    after_counts = Counter(item["title"] for item in after["criteria"])
    if before_counts != after_counts:
        if any(count > 1 for count in (*before_counts.values(), *after_counts.values())):
            raise RFCError("Repeated acceptance text requires explicit stable-ID plan changes", 409, "ambiguous_criteria")
        old_by_title = {item["title"]: item for item in old.get("criteria", [])}
        removed = [title for title in before_counts if title not in after_counts]
        added = [title for title in after_counts if title not in before_counts]
        if len(removed) == len(added) == 1 and removed[0] in old_by_title:
            changes.append({"op": "criterion_update", "criterion_id": old_by_title[removed[0]]["id"],
                            "fields": {"title": added[0]}})
        else:
            for title in removed:
                if title in old_by_title:
                    changes.append({"op": "criterion_remove", "criterion_id": old_by_title[title]["id"], "fields": {}})
            for title in added:
                if title not in old_by_title:
                    changes.append({"op": "criterion_add", "criterion_id": "C-" + digest(title)[:12],
                                    "fields": {"title": title}})
    return validate_plan_changes(changes)


class ReviewedChanges:
    def __init__(self, service):
        self.service = service

    def preview(self, principal, data):
        from .application import content_digest
        from .chat import apply_plan_changes, validate_plan_changes
        service = self.service
        with service.store.read() as con:
            principal = service._current(con, principal)
            row = service._rfc(con, principal, data.get("rfc_id", ""), "contributor")
            if data.get("expected_revision") != row["revision"]:
                raise RFCError("RFC changed; refresh and review before saving", 409, "preview_mismatch")
            if json.loads(row["source"]) or row["enrolled"]:
                service._require(con, principal, row["repo_id"], "maintainer", row["id"])
            if con.execute("SELECT 1 FROM operations WHERE rfc_id=? AND kind IN ('rfcs.publish','rfcs.enroll','rfcs.update_source') AND status IN ('pending','running','uncertain')", (row["id"],)).fetchone():
                raise RFCError("Recover the existing source operation before reviewing another edit", 409, "write_in_progress")
            title, body = data.get("title", row["title"]), data.get("body", row["body"])
            if (not isinstance(title, str) or title != title.strip() or not title or len(title) > 256
                    or "\n" in title or "\r" in title or not isinstance(body, str) or len(body) > 2_000_000):
                raise RFCError("Invalid reviewed RFC content")
            if json.loads(row["source"]):
                from .providers import MAX_DOCUMENT_BYTES
                if len(body.encode("utf-8")) > MAX_DOCUMENT_BYTES:
                    raise RFCError("Reviewed source content exceeds the provider document size limit")
            changes = (validate_plan_changes(data["plan_changes"]) if "plan_changes" in data
                       else infer_plan_changes(row, body))
            # Validate before provider access and again against the live model at apply.
            apply_plan_changes(parse(body, json.loads(row["model"])), changes, previous=json.loads(row["model"]))
            reason = data.get("reason", "")
            if not isinstance(reason, str) or len(reason) > 2000:
                raise RFCError("Invalid change reason")
            candidate = {"title": title, "body": body, "plan_changes": changes,
                         "base_revision": row["revision"], "content_digest": content_digest(row["title"], row["body"]),
                         "source_binding": digest(row["source"]), "draft_digest": ""}
        baseline = service._chat_source_baseline(principal, row["id"], row["revision"], refresh=True)
        candidate.update(baseline)
        if (title == row["title"] and body == row["body"] and not changes
                and not baseline.get("source_conflict") and not json.loads(row["model"]).get("source_reconciliation")):
            raise RFCError("There are no changes to review", 409, "no_changes")
        with service.store.transaction() as con:
            principal = service._current(con, principal)
            current = service._rfc(con, principal, row["id"], "maintainer" if json.loads(row["source"]) or row["enrolled"] else "contributor")
            if current["revision"] != row["revision"] or current["source"] != row["source"]:
                raise RFCError("RFC changed while loading the source; review again", 409, "preview_mismatch")
            if json.loads(current["source"]):
                service._source_write_access(con, principal, current["repo_id"], SourceRef.from_dict(json.loads(current["source"])))
            now, identity, candidate_digest = service.clock(), "change-" + uuid.uuid4().hex, digest(encode(candidate))
            con.execute("INSERT INTO reviewed_changes(id,actor,repo_id,rfc_id,candidate,candidate_digest,reason,created,expires,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (identity, principal.user_id, row["repo_id"], row["id"], encode(candidate), candidate_digest, reason.strip(), now, now + PREVIEW_SECONDS, now))
        source_text = _document(candidate.get("source_title", row["title"]), candidate.get("source_body", row["body"]))
        candidate_text = _document(title, body)
        source = json.loads(row["source"])
        # Paths are execution data; the public RFC source view already redacts absolute paths.
        target = {key: value for key, value in source.items() if key != "path"}
        return {"rfc_id": row["id"], "preview_id": identity, "candidate_digest": candidate_digest,
                "base_revision": row["revision"], "title": title, "body": body, "candidate": candidate,
                "reason": reason.strip(), "plan_changes": changes, "plan_diff": changes,
                "diff": _diff(_document(row["title"], row["body"]), candidate_text),
                "source_diff": _diff(source_text, candidate_text, "current source RFC") if source else "",
                "source_conflict": baseline.get("source_conflict", False), "source_update": bool(source),
                "write_target": target if source else {"provider": "workspace"}, "expires": now + PREVIEW_SECONDS}

    def apply(self, con, principal, data):
        service = self.service
        row = service._rfc(con, principal, data.get("rfc_id", ""), "contributor")
        preview = con.execute("SELECT * FROM reviewed_changes WHERE id=? AND actor=? AND rfc_id=?",
                              (data.get("preview_id", ""), principal.user_id, row["id"])).fetchone()
        if not preview:
            raise RFCError("Resource not found or access denied", 403, "forbidden")
        if json.loads(row["source"]) or row["enrolled"]:
            service._require(con, principal, row["repo_id"], "maintainer", row["id"])
            if json.loads(row["source"]):
                service._source_write_access(con, principal, row["repo_id"], SourceRef.from_dict(json.loads(row["source"])))
        key = data.get("idempotency_key")
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise RFCError("A bounded idempotency key is required")
        if data.get("candidate_digest") != preview["candidate_digest"]:
            raise RFCError("Apply requires the exact reviewed candidate", 409, "preview_mismatch")
        reason = data.get("reason", preview["reason"])
        if not isinstance(reason, str) or len(reason) > 2000:
            raise RFCError("Invalid change reason")
        request_digest = digest(encode({"preview_id": preview["id"], "candidate_digest": preview["candidate_digest"], "reason": reason}))
        reused = con.execute("SELECT id FROM reviewed_changes WHERE actor=? AND apply_key=? AND id!=?",
                             (principal.user_id, key, preview["id"])).fetchone()
        if reused:
            raise RFCError("Idempotency key reused for a different reviewed change", 409, "conflict")
        if preview["status"] == "applied":
            if preview["apply_key"] != key or preview["request_digest"] != request_digest:
                raise RFCError("Applied change must be retried with its original request", 409, "conflict")
            result = json.loads(preview["result"])
            operation = con.execute("SELECT * FROM operations WHERE id=?", (result.get("operation_id", ""),)).fetchone()
            return {**service._view(con, principal, row), "preview_id": preview["id"],
                    **({"operation_id": operation["id"], "operation": service._operation(operation)} if operation else {})}
        if preview["expires"] <= service.clock():
            raise RFCError("The review expired; preview the current draft again", 409, "preview_expired")
        candidate = json.loads(preview["candidate"])
        result = service._apply_reviewed_candidate(con, principal, row, candidate, preview["id"], reason,
                                                    audit_action="rfcs.changes.apply", source_key="reviewed-source:")
        recovery = {name: result[name] for name in ("operation_id",) if name in result}
        con.execute("UPDATE reviewed_changes SET status='applied',apply_key=?,request_digest=?,result=?,updated=? WHERE id=?",
                    (key, request_digest, encode(recovery), service.clock(), preview["id"]))
        return {**result, "preview_id": preview["id"]}
