#!/usr/bin/env python3
"""Isolated portable-RFC acceptance benchmark; never uses an existing workspace.

Example: PYTHONPATH=src python tools/check_rfc_service_performance.py
Creates 100 RFCs in five repos and ten distinct authorized users, exercises a
real HTTP server/worker process, then deletes its temporary SQLite and files.
No model or external platform request is made, and no credential is printed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.request import urlopen


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rfcs", type=int, default=100)
    parser.add_argument("--repos", type=int, default=5)
    parser.add_argument("--users", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--read-p95-ms", type=float, default=500)
    parser.add_argument("--pickup-seconds", type=float, default=2)
    args = parser.parse_args()
    if min(args.rfcs, args.repos, args.users, args.rounds) < 1:
        parser.error("workload counts must be positive")
    from infermatrix_copilot.rfc_service.application import build_service
    from infermatrix_copilot.rfc_service.store import encode
    from infermatrix_copilot.rfc_service.drafts import digest
    from infermatrix_copilot.rfc_service.models import SourceRef
    from infermatrix_copilot.sdk.v1.rfc import RFCClient

    with tempfile.TemporaryDirectory(prefix="imrfc-performance-") as temporary:
        service = build_service(Path(temporary) / "state")
        issued = service.bootstrap_admin("Benchmark administrator")
        admin = service.authenticate(issued["token"])
        repos = [service.dispatch(admin, "repositories.create", {"name": f"Repository {index}", "provider": "local"}) for index in range(args.repos)]
        body = "# Benchmark RFC\n\n## Goals\nTrack a validated deployment.\n\n### Delivery\n\n" + "\n\n".join(f"#### F{index}. Task {index}\n\nImplementation detail for task {index}." for index in range(1, 21)) + "\n\n## Acceptance criteria\n\n" + "\n".join(f"- Criterion {index}." for index in range(1, 6))
        rfcs = [service.dispatch(admin, "rfcs.draft", {"repo_id": repos[index % len(repos)]["id"], "title": f"Benchmark RFC {index}", "body": body}) for index in range(args.rfcs)]
        identities = []
        for index in range(args.users):
            user = service.dispatch(admin, "users.create", {"name": f"User {index}"})
            token = service.dispatch(admin, "tokens.create", {"user_id": user["id"]})
            for repo in repos:
                service.dispatch(admin, "grants.set", {"repo_id": repo["id"], "user_id": user["id"], "role": "reader"})
            identities.append((user, token["token"]))
        # Same stable task IDs in every RFC, substantial implementation and
        # evidence histories, and narrowed RFCs for half the users.
        restricted = {item["id"] for index, item in enumerate(rfcs) if index % 7 == 0}
        allowed_users = {user["id"]: "reader" for user, _ in identities[:max(1, len(identities) // 2)]}
        for item in rfcs:
            if item["id"] in restricted:
                service.dispatch(admin, "rfcs.acl", {"rfc_id": item["id"], "restricted": True, "grants": allowed_users})
        with service.store.transaction() as con:
            roots = {row["id"]: dict(row) for row in con.execute("SELECT * FROM repositories")}
            for row in con.execute("SELECT * FROM rfcs").fetchall():
                model = json.loads(row["model"])
                model.update(writer_id=service.writer_id, enrollment={"actor": admin.user_id, "credential_id": admin.credential_id})
                model["freshness"]["verification"] = service.clock()
                for feature in model["features"]:
                    feature["implementation_history"] = [{"at": index, "reason": "Historical implementation record " + "x" * 100} for index in range(20)]
                for criterion in model["criteria"]:
                    criterion["evidence"] = [{"revision": "fixture", "environment": "benchmark", "note": "Historical evidence " + "x" * 100, "stale": True} for _ in range(20)]
                root = roots[row["repo_id"]]
                relative = "fixtures/" + row["id"] + ".md"
                path = Path(root["root"]) / relative
                path.parent.mkdir(exist_ok=True)
                path.write_text(row["body"], encoding="utf-8")
                source = SourceRef("local", root["external_name"], "markdown", relative, path=relative)
                con.execute("UPDATE rfcs SET model=?,enrolled=1,source=? WHERE id=?", (encode(model), encode(source.to_dict()), row["id"]))
                con.execute("INSERT INTO source_snapshots(rfc_id,revision,body,title,created) VALUES (?,?,?,?,?)", (row["id"], digest(row["body"]), row["body"], row["title"], service.clock()))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        endpoint = f"http://127.0.0.1:{port}"
        env = dict(os.environ)
        env.pop("RFC_SERVICE_URL", None)
        env.pop("RFC_TOKEN", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        process = subprocess.Popen([sys.executable, "-m", "infermatrix_copilot.rfc_service.cli", "--state-dir", str(service.store.root), "--local", "serve", "--port", str(port), "--interval", "30"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urlopen(endpoint + "/healthz", timeout=0.3) as response:
                        assert json.load(response) == {"ok": True}
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError("Benchmark HTTP service did not start") from None
                    time.sleep(0.05)
            barrier = threading.Barrier(args.users)
            def workload(identity):
                user, token = identity
                client = RFCClient(service_url=endpoint, token=token)
                client.capabilities()
                visible = [item for item in rfcs if item["id"] not in restricted or user["id"] in allowed_users]
                measurements, failures = [], []
                barrier.wait(timeout=10)
                for index in range(args.rounds):
                    action = "rfcs.list" if index % 2 == 0 else "rfcs.get"
                    payload = {"view": "summary", "limit": 20, "offset": (index * 3) % max(1, len(visible)), "query": "Benchmark"} if action == "rfcs.list" else {"rfc_id": visible[index % len(visible)]["id"], "view": "detail"}
                    started = time.monotonic()
                    try:
                        result = client.dispatch(action, payload)
                        if action == "rfcs.list":
                            assert result["total"] == len(visible)
                            assert all(item["id"] in {entry["id"] for entry in visible} for item in result["rfcs"])
                        else:
                            assert result["id"] == payload["rfc_id"]
                    except Exception as error:
                        failures.append(getattr(error, "code", type(error).__name__))
                    measurements.append((action, (time.monotonic() - started) * 1000))
                return measurements, failures
            with ThreadPoolExecutor(args.users) as pool:
                runs = list(pool.map(workload, identities))
            values = [item for run, _ in runs for item in run]
            errors = [item for _, failures in runs for item in failures]
            medians = {action: {"p50_ms": round(percentile([duration for kind, duration in values if kind == action], 0.5), 2), "p95_ms": round(percentile([duration for kind, duration in values if kind == action], 0.95), 2)} for action in sorted({kind for kind, _ in values})}
            client = RFCClient(service_url=endpoint, token=issued["token"])
            client.capabilities()
            pickups = []
            for index in range(5):
                value = client.draft({"repo_id": repos[0]["id"], "title": "Worker pickup", "body": "# Worker pickup\n\n- [ ] Verify local publication.\n"})
                started = time.monotonic()
                if index == 4:
                    # A separate process cannot signal the server's threading
                    # Event: this measures the <=1s SQLite polling fallback.
                    queued = service.dispatch(admin, "rfcs.publish", {"rfc_id": value["id"], "content_digest": value["content_digest"], "idempotency_key": f"pickup-{index}", "post": True})
                else:
                    queued = client.publish(value["id"], content_digest=value["content_digest"], idempotency_key=f"pickup-{index}", post=True)
                while True:
                    operation = client.operation(queued["operation_id"])
                    if operation["status"] not in {"pending", "running"}:
                        assert operation["status"] == "succeeded", "Benchmark publication failed"
                        pickups.append(time.monotonic() - started)
                        break
                    if time.monotonic() - started > 5:
                        raise RuntimeError("Worker did not pick up the benchmark publication")
                    time.sleep(0.02)
            p95 = percentile([duration for _, duration in values], 0.95)
            result = {"workload": {"rfcs": args.rfcs, "repositories": args.repos, "concurrent_users": args.users, "read_requests": len(values), "features_per_rfc": 20, "history_entries_per_feature": 20, "restricted_rfcs": len(restricted), "enrolled_rfcs": args.rfcs}, "read_p95_ms": round(p95, 2), "actions": medians, "read_errors": errors, "idle_queue_completion_seconds": [round(item, 3) for item in pickups], "idle_queue_max_seconds": round(max(pickups), 3), "cross_process_queue_seconds": round(pickups[-1], 3), "provider": "local", "model_calls": 0, "passed": not errors and p95 < args.read_p95_ms and max(pickups) < args.pickup_seconds}
            print(json.dumps(result, indent=2))
            return 0 if result["passed"] else 1
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    raise SystemExit(main())
