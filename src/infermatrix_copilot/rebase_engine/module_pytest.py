"""`imx-omni-pytest` — the module agents' gated test wrapper, replacing the
parent's `run_module_pytest.sh` on the PR1 substrate (GPU mutex, two-tier
watchdog, layered timeouts, artifact cleanup — one implementation, not a
parallel shell copy).

Env contract (the agent shell provides these; all neutral):
  IMX_TARGET_REPO    repo the tests run in (the parent's OMNI_PATH)
  IMX_LOG_DIR        run log dir (logs under <dir>/tests, lock under
                     <dir>/gpu_lock)
  IMX_ADAPTER_REBASE optional adapter rebase-data dir (watchdog patterns at
                     ../testing/watchdog_patterns.yaml relative to it)
  IMX_GPU_MUTEX=1    hold the GPU lock for EVERY invocation (Phase-2 module
                     agents serialize all GPU work); otherwise the lock is
                     taken only when an arg targets tests/e2e/ or
                     tests/examples/ (parent parity)
  TEST_TIMEOUT_SEC   primary timeout (default 7200)
  CUDA_VISIBLE_DEVICES  passed through to the child

Invocation parity: `imx-omni-pytest -vv -s tests/x/` runs pytest;
`imx-omni-pytest python -c '...'` runs that command under the same gating
(the parent wrapper accepted both shapes).  An adapter may declare
``rebase.testing.module_test_platform`` so broad module runs do not execute
tests that CI routes exclusively to a different accelerator platform."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
import tempfile
from pathlib import Path

from ..testing.runner import TestJob, TestOutcome, TestRunner
from ..testing.watchdog import WatchdogPatterns


_ACCELERATOR_PLATFORMS = frozenset({"cuda", "musa", "npu", "rocm", "xpu"})


def _needs_gpu_lock(args: list[str]) -> bool:
    if os.environ.get("IMX_GPU_MUTEX") == "1":
        return True
    return any(("tests/e2e/" in a or "tests/examples/" in a)
               for a in args if not a.startswith("-"))


def _job_key(args: list[str]) -> str:
    for a in args:
        if not a.startswith("-") and "tests/" in a:
            return a.strip("/").replace("/", "_").replace(".py", "")
    return "module_pytest"


def _platform_filtered_args(args: list[str], testing: dict) -> tuple[list[str], str | None]:
    """Add the adapter's platform selector to otherwise unfiltered pytest.

    Main's hardware marks are CI routing metadata: an NPU-only item is not
    expected to run merely because a CUDA job invokes a broad directory.
    Keep platform-neutral and current-platform items, including tests marked
    for several platforms.  A caller-provided ``-m`` expression is explicit
    and therefore remains authoritative.
    """
    platform = testing.get("module_test_platform")
    if platform is None or args[0] == "python" or args[0].startswith("python3"):
        return args, None
    if not isinstance(platform, str) or platform not in _ACCELERATOR_PLATFORMS:
        choices = ", ".join(sorted(_ACCELERATOR_PLATFORMS))
        raise ValueError(f"rebase.testing.module_test_platform must be one of: {choices}")
    if any(arg == "-m" or arg.startswith("-m=") for arg in args):
        return args, None
    others = sorted(_ACCELERATOR_PLATFORMS - {platform})
    expression = f"{platform} or (" + " and ".join(f"not {name}" for name in others) + ")"
    return [*args, "-m", expression], platform


def _log_hash(log_file: str) -> tuple[str | None, str]:
    """Hash bytes observed after runner return; later rotation is still possible."""
    if not log_file:
        return None, "missing"
    try:
        with open(log_file, "rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        def identity(st):
            return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns
        if identity(before) != identity(after):
            return digest.hexdigest(), "changed_during_hash"
        if identity(after) != identity(Path(log_file).stat()):
            return digest.hexdigest(), "path_changed_during_hash"
        return digest.hexdigest(), "stable_observation"
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"


def _write_outcome_receipt(log_dir: Path, *, args: list[str], run_id: str,
                           attempt: str, timeout: int, cuda: str,
                           outcome: TestOutcome, effective_args: list[str],
                           platform_marker: str | None) -> None:
    """Atomic per-attempt runner outcome, without commands, paths, or env dumps.

    Requested argv is not effective inner argv/scope proof (pytest defaults
    and the runner's existing coverage fallback still apply). A missing receipt
    cannot certify success, including when a runner or receipt write raises.
    """
    starttime = None
    try:
        # Linux self identity only; no other process or environment is read.
        stat = Path("/proc/self/stat").read_text(encoding="utf-8")
        starttime = int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        pass
    canonical = json.dumps(args, ensure_ascii=False, separators=(",", ":"))
    effective_canonical = json.dumps(effective_args, ensure_ascii=False,
                                     separators=(",", ":"))
    cuda_subset = [part.strip() for part in cuda.split(",") if part.strip()]
    # Arbitrary env strings must not become plaintext in the receipt.
    valid_cuda = all(re.fullmatch(
        r"-?\d+|GPU-[0-9a-fA-F-]+|MIG-[0-9a-fA-F-]+(?:/\d+/\d+)?", part
    ) for part in cuda_subset)
    log_sha, log_status = _log_hash(outcome.log_file)
    receipt = {
        "schema_version": 1,
        "run_id": (run_id if re.fullmatch(r"run-\d{8}-\d{6}-[0-9a-f]{6}", run_id)
                   else None),
        "run_id_sha256": hashlib.sha256(run_id.encode()).hexdigest(),
        "attempt_id": attempt,
        "wrapper_pid": os.getpid(), "wrapper_starttime_ticks": starttime,
        "requested_argv_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        "effective_argv_sha256": hashlib.sha256(effective_canonical.encode()).hexdigest(),
        "platform_marker_injected": platform_marker,
        "timeout_sec": timeout,
        "cuda_visible_devices": cuda_subset if valid_cuda else None,
        "cuda_selection_sha256": hashlib.sha256(cuda.encode()).hexdigest(),
        "rc": outcome.rc, "watchdog_triggered": outcome.watchdog_triggered,
        "timed_out": outcome.timed_out, "skipped": outcome.skipped,
        "log_path_sha256": (hashlib.sha256(outcome.log_file.encode()).hexdigest()
                            if outcome.log_file else None),
        "log_sha256": log_sha, "log_hash_status": log_status,
    }
    directory = log_dir / "tests" / "outcomes"
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{attempt}.", suffix=".tmp",
                                     dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, directory / f"{attempt}.json")
    finally:
        Path(temporary).unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    repo = os.environ.get("IMX_TARGET_REPO", "")
    log_dir = os.environ.get("IMX_LOG_DIR", "")
    if not repo or not log_dir:
        print("imx-omni-pytest: IMX_TARGET_REPO and IMX_LOG_DIR must be set",
              file=sys.stderr)
        return 2
    if not args:
        print("imx-omni-pytest: no test paths or command given",
              file=sys.stderr)
        return 2

    patterns = None
    artifact_globs: list[str] = []
    testing: dict = {}
    rebase_dir = os.environ.get("IMX_ADAPTER_REBASE", "")
    # runtime overlay (design D4): the agent env contract may hand the
    # learned-noise overlay path down; absent ⇒ seed-only, as before
    overlay = os.environ.get("IMX_WATCHDOG_OVERLAY", "")
    if rebase_dir:
        pat_file = Path(rebase_dir).parent / "testing" / \
            "watchdog_patterns.yaml"
        if pat_file.is_file():
            patterns = WatchdogPatterns.from_yaml(
                pat_file, overlay=Path(overlay) if overlay else None)
        manifest = Path(rebase_dir).parent / "manifest.yaml"
        if manifest.is_file():
            import yaml
            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
            testing = (data.get("rebase") or {}).get("testing") or {}
            artifact_globs = list(testing.get("artifact_globs") or [])

    try:
        effective_args, platform_marker = _platform_filtered_args(args, testing)
    except ValueError as exc:
        print(f"module test wrapper: {exc}", file=sys.stderr)
        return 2
    if effective_args[0] == "python" or effective_args[0].startswith("python3"):
        command = " ".join(shlex.quote(a) if i else a
                           for i, a in enumerate(effective_args))
    else:
        command = "python -m pytest " + " ".join(
            shlex.quote(a) for a in effective_args)

    if "module_max_timeout_sec" in testing:
        cap = testing["module_max_timeout_sec"]
        if type(cap) is not int or cap <= 0:
            print("module test wrapper: rebase.testing.module_max_timeout_sec "
                  "must be a positive integer", file=sys.stderr)
            return 2
        try:
            timeout = int(os.environ.get("TEST_TIMEOUT_SEC", "7200"))
        except ValueError:
            timeout = 0
        if timeout <= 0 or timeout > cap:
            print("module test wrapper: TEST_TIMEOUT_SEC must be a positive "
                  f"integer at most {cap}; keep the adapter's planned budget "
                  "and use short read-only polls for long tests",
                  file=sys.stderr)
            return 2
    else:
        timeout = int(os.environ.get("TEST_TIMEOUT_SEC", "7200"))
    cuda = os.environ.get("CUDA_VISIBLE_DEVICES", "")

    # two-tier watchdog wiring: learning + report artifacts land under the
    # run's log dir (parent parity). The eco-tier REVIEW callback needs an
    # LLM client the standalone keyless wrapper deliberately does not own —
    # review-tier matches take the documented default-CONTINUE until the
    # assembly PR threads the copilot client through; recorded divergence.
    from ..testing import watchdog_learn

    decisions = Path(log_dir) / "watchdog_decisions.jsonl"
    # stable harvest identity (design D4): run id from the agent env
    # contract when present, else synthesized from the log dir; seq is a
    # per-process counter — the curator's exactly-once harvest dedups on
    # (run, attempt, job_key, seq)
    run_ident = os.environ.get("IMX_RUN_ID", "") or Path(log_dir).name
    import itertools
    import uuid
    _seq = itertools.count(1)
    # the seq counter restarts per PROCESS, so two wrapper invocations for
    # the same test would collide on (run, "", test, 1) and the harvest
    # would wrongly dedup a real second decision — a collision-resistant
    # attempt id makes the identity process-unique (pid+seconds can
    # collide across same-second retries or pid reuse)
    _attempt = f"wrap-{uuid.uuid4().hex}"

    def record_fn(pattern: str, verdict: str, test_name: str) -> None:
        # LogWatchdog's contract is (matched line, verdict, test name);
        # watchdog_learn.record takes keyword-only fields
        watchdog_learn.record(decisions, pattern=pattern, verdict=verdict,
                              test=test_name, run=run_ident,
                              attempt=_attempt, job_key=test_name,
                              seq=next(_seq))

    def report_fn(test_name: str, pattern: str, detail: str) -> None:
        report = Path(log_dir) / "tests" / f"{test_name}.watchdog_report"
        report.parent.mkdir(parents=True, exist_ok=True)
        with open(report, "a", encoding="utf-8") as f:
            f.write(f"pattern={pattern}\n{detail}\n")

    def review_fn(test_name: str, snippet: str) -> str:
        # explicit no-LLM reviewer BY DESIGN: this standalone keyless
        # wrapper owns no LLM client, so every tier-2 match takes the
        # documented default-CONTINUE, RECORDED (the learning pipeline
        # promotes noise from accumulated CONTINUEs — a silent
        # short-circuit would starve it). The assembly's own test loop
        # wires the eco-tier LLM reviewer (rebase_v3._watchdog_collaborators).
        return "CONTINUE"

    runner = TestRunner(
        repo_root=Path(repo), tests_dir=Path(log_dir) / "tests",
        patterns=patterns, gpu_lock_dir=Path(log_dir) / "gpu_lock",
        artifact_globs=artifact_globs,
        review_fn=review_fn, record_fn=record_fn, report_fn=report_fn,
        cuda_visible_devices=cuda)
    # the mutex SERIALIZES; it is not a hardware requirement — min_gpus=0
    # so a GPU-less box still RUNS the command (a skip would be a false
    # pass), while gpu_lock=True holds the lock for the run
    job = TestJob(key=_job_key(args), command=command, timeout_sec=timeout,
                  min_gpus=0, gpu_lock=_needs_gpu_lock(args), index=0)
    outcome = runner.run(job, dict(os.environ))
    _write_outcome_receipt(Path(log_dir), args=args, run_id=run_ident,
                           attempt=_attempt, timeout=timeout, cuda=cuda,
                           outcome=outcome, effective_args=effective_args,
                           platform_marker=platform_marker)
    if outcome.skipped:
        print(f"imx-omni-pytest: skipped — {outcome.skip_reason}",
              file=sys.stderr)
    return outcome.rc


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
