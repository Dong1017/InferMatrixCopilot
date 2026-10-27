# DRIFT_TRIAGE — dual-source divergences surfaced by the migration

Plan §9 risk 1: the parent orchestrator carried the same facts in multiple
places, and they drifted. Each entry here must receive a pre-cutover DECISION
(which side is truth for v3) recorded in place; unresolved entries block PR6.

## 1. Prompt builder maps vs config.sh maps (found in PR4b)

The parent's `prompts/builder.py` inlined its own copies of the module maps,
and they DIVERGED from `config.sh` (the operational source the shell tasks
used):

| Field | builder.py (prompt flavor) | config.sh (operational flavor) |
|---|---|---|
| `module_import_check` | prints `'OK'`, e.g. model_config imports 2 symbols | prints `'Model Config imports OK'`-style messages |
| `module_test_map.scheduler` | `["tests/distributed/"]` | adds `tests/diffusion/distributed/test_ulysses_uaa_perf.py` |
| `module_test_map.input_output` | 3 entries incl. `tests/engine/` | 4 entries incl. `test_async_omni_engine_input.py` |
| `module_test_map.worker_runner` | 3 entries | 5 entries incl. sleep-mode + qwen3 tts |
| `module_test_map.online_serving` | `tests/entrypoints/` (broad) | 4 explicit entries incl. `openai_api/` |
| `module_test_map.model_executor` / others | equal | equal |

Where each lives now:
- builder flavor → `adapters/vllm_omni/rebase/prompt_data.yaml` (prompt-render
  byte-parity; the goldens pin it against the parent builder's output).
- config.sh flavor → the manifest's `modules.*.{upstream_paths,test_paths}`
  (operational: commit assignment, path-sync, test selection).

**DECISION (PR5, recorded; owner may veto at the PR6 gate):** keep both
flavors frozen through the §8 validation comparison — the prompt flavor is
byte-parity EVIDENCE (goldens pin it against the parent builder's own
output) and changing it mid-validation would invalidate the cache-parity
claim. Operational truth is the manifest (now pinned byte-for-byte against
the parent's LIVE §11 arrays by `test_shell_golden.py`). Unify: regenerate
`prompt_data.yaml`'s maps from the manifest in the first post-cutover soak
stage (PR7 cleanup), retiring the dual source.

## 2. `local_paths` granularity (PR2/PR4b audit — DECIDED)

Rev 8 called for "the parent's authoritative module paths copied in". Audit
result: `local_paths` is consumed by prefix-matching scoping (review
attribution, `module_for_path`); replacing the coarse directory prefixes with
the parent's file lists would NARROW attribution and regress review coverage.
Decision (recorded in the manifest comment): `local_paths` = UNION of the
coarse prefixes and every parent `MODULE_OMNI_FILES` entry not covered by
them — a strict superset of the parent's rebase scope, no consumer loses
coverage. Pinned by `test_manifest_local_paths_cover_parent_module_map`.

## 3. `[[:space:]]` vs `[ \t]` in wheel-pin regexes (PR2 — accepted)

The shell's character class also matched `\r` (CRLF Dockerfiles). The port
uses `[ \t]`; unobservable on LF-normalized repos. Accepted; revisit only if
a CRLF `docker/Dockerfile.ci` ever appears.

## 4. config.sh §10 false-pass (pre-existing, from Rev 8) — DECIDED (PR5)

Manifest slug missing from config.sh §10 → empty command → rc=0 "pass" in
the parent. The PR5 shell-golden enumeration (live `.buildkite/cuda`
pipelines vs the captured §10 arrays, 2026-08-01) quantified it:

- live yaml builds **54** jobs; the parent's §10 defines **49**;
- **25 live slugs are ABSENT from §10** — every one FALSE-PASSES in the
  parent today (diffusion_batch/cache/cosmos3/distributed/gguf_plugin/
  model/offloader tests, entrypoints_test, 10 `full_moon_*` suites,
  tiny-model tests, tts_higgs-audio-v3/soulx-singer/voxcpm2, …);
- **20 §10 entries are stale** (no longer in the live yaml);
- 7 of the 29 intersecting slugs differ in command text (quoting churn
  plus real drift: e.g. `distributed_test` gained an `and L4` marker
  upstream; two multi-GPU X2I suites split their file lists).

**DECISION:** the live-yaml manifest builder is the ONLY operational
command source for v3 (the §10 static map is demonstrably ~46% stale).
The false-pass mechanism is structurally dead here — the builder DROPS
labeled steps with no runnable command and the run side classifies an
empty command as an infrastructure failure (`test_empty_command_never_
passes`). PR6 consequence: the §8 outcome-equivalence comparison must be
run over the MANIFEST-BUILT slug set; the parent's "pass" on the 25
missing slugs is vacuous and must not count as a baseline outcome.


## 5. Buildkite pipeline location: `.buildkite/cuda/` vs `.buildkite/` (PR4c)

The live vllm-omni tree nests its per-accelerator pipelines under
`.buildkite/cuda/` (siblings: `amd/`, `intel/`, `npu/`, `release/`); the
parent's `test_manifest.py` hardcodes `yaml_dir = omni / ".buildkite"` and
would find ZERO jobs against today's tree — every manifest it built there
would be empty. Our `yaml_dir` is adapter DATA
(`rebase.test_manifest.yaml_dir: .buildkite/cuda`), pinned to the live
layout; fixtures reproduce the nested layout. If the PR5 shell-golden
capture runs the parent against the live tree, expect its manifest step to
produce an empty set — compare against the parent's recorded run instead.

## 6. PR5 golden-capture sweep corrections (DECIDED in place)

The one-time capture surfaced adapter-data drifts, all fixed in the PR5
commits and pinned by `test_shell_golden.py`:

- `watchdog_patterns.yaml` was missing the parent's post-PR1 noise entry
  `"Released CuMem memory pool during shutdown"` (inventory bijection now
  enforced per tier, with the documented POSIX-class/escape translation).
- `modules.*.import_check` (parent MODULE_IMPORT_CHECK per-module smoke
  snippets) was absent from the manifest — added parent-verbatim.
- **THE ROUTING FLAVOR IS ITS OWN MAP.** `test_paths` carried two entries
  beyond §11 (`tests/engine/`, `tests/e2e/online_serving/`); a first
  attempt to trim them to §11-verbatim broke job→module ROUTING — the
  parent's `_assign_modules` scores against its OWN inline map (a third
  flavor whose broad prefixes are load-bearing: without
  `tests/e2e/online_serving/`, `platform`'s `tests/` swallowed 37/54 live
  jobs). Final resolution (reverses PR4c's "third copy unified onto the
  operational flavor"): the assignment map lives as its own adapter datum,
  `rebase.test_manifest.assignment_paths` (parent test_manifest.py inline
  map VERBATIM); `modules.*.test_paths` is §11-verbatim for shell test
  selection; the two flavors are never merged — merging in either
  direction changes scores. Pinned three ways: `assignment_paths` ==
  golden `assignment_map` (bytes); routing over a golden-derived fixture
  == the recorded behavioral replay (verified identical to the parent's
  own `_assign_modules` output at capture time); and a histogram guard
  that fails if `platform` ever swallows the set again.

## 7. §10 `CI_TEST_MODULE` declared vs computed routing (parent-internal)

The parent holds job→module in TWO places: §10's hand-maintained
`CI_TEST_MODULE` (consumed by the shell's failure routing) and the
computed `_assign_modules` output (consumed by module test plans). At
capture time they DISAGREE on 13 of 28 comparable slugs (e.g. the
`full_moon_*` doc/function suites: declared `online_serving`, computed
`platform`; `tts_qwen3-tts_base_test`: declared `worker_runner`, computed
`online_serving`). v3 reproduces the COMPUTED side (that is what fed
module plans and prompts). PR6 consequence: when comparing per-module
outcomes against parent artifacts that used the declared labels, map
through the golden's `assignment_routing`, not `CI_TEST_MODULE`.

## 8. Tier-1 `FATAL` false kill on "non-fatal" prose (live run 2026-08-23) — DECIDED

Watchdog critical patterns compile with `re.I`, so the parent's bare
`FATAL` entry matches the benign vllm-omni WARNING `Dropping
uncorrelated non-fatal RPC error ... request failed` and instantly
kills a healthy job (observed: `simple_engineentrypoints_test` killed
mid-pass during the v0.28.0 full run; surrounding tests were PASSING).
Tier 1 matches the RAW tail — the learned-noise overlay only guards
Tier 2 — so the seed itself must not match "non-fatal". Deliberate
divergence: seed and golden both carry `(?<![\w-])FATAL(?!\w)` (the
`simulation_allowlist` `non_fatal` entry only covers simulated TEST
NAMES, not engine log prose). The parent carries the same false-kill
latently; parity here would preserve a bug.

The v0.31.0 full platform run exposed a second form on 2026-10-03:
`_raise_nonfatal_error_message` in an expected HTTP-400 traceback matched
the older `(?<!non-)FATAL` pattern and killed pytest after the negative
test passed. Word boundaries now exclude identifiers and both spellings
of nonfatal prose, while standalone fatal messages still trigger Tier 1.

## 9. Precommit red-on-baseline gates every push (live run 2026-08-23) — DECIDED

The parent (and Rev 8 §2.3 as merged) treated ANY red phase-3.2
precommit as a structural push block. The live v0.28.0 full run proved
the failure mode: `pre-commit run --all-files` is red on the UNTOUCHED
tree — 31 test files missing the CI-collection pytest marks, 30 of
them byte-identical to upstream `origin/main` (upstream CI only checks
changed files, so main stays green while `--all-files` is red). A
repo-wide debt the run did not create would structurally block every
push forever. Deliberate divergence, mirroring the doctrine the engine
already applies to tests (pre-existing-failure baseline) and remote CI
(baseline pipeline): when the retried precommit is red, the same
command runs once more on a detached worktree of HEAD (the pre-run
tree — module changes are uncommitted at phase 3.2). Baseline red ⇒
`failed_preexisting`: the gate FLAGS it (blocking under
`strict_push_gate=true`); baseline green ⇒ `failed` stays structural.
The marks debt itself belongs upstream (vllm-omni main), not on the
rebase branch.

## 10. Live worker coverage and retired checker commands (2026-10-01)

The main-commit campaign found a removed upstream `RoutedExpertsTensors`
import in `worker_v2`, outside the original worker scope. Operational worker
paths, tests, routing and smoke imports now include both implementations.
Historical prompt maps and goldens stay unchanged; `live_module_overrides`
selects the expanded scope only for live renders. The separate routing and
test-map flavors remain separate, with the same explicit coverage addition.

The live agent also stalled searching the shared filesystem for the retired
41/42 shell scripts. Live guidance now names the shipped `api_drift_guard.py`
and an AST-derived upstream import audit through `run_import_check`. Both
checks must be repeated after fixes and fail on mismatches or broken imports.
Parent-parity templates remain unchanged.

## 11. Resume must restore the recorded upstream commit (2026-10-01)

Teardown removes the disposable upstream clone, while resume replays the
completed wheel and assignment steps. Module/debug entry now re-ensures the
scratch checkout and restores `upstream_commit`; checkout failure blocks.
Recreated and surviving scratch checkouts are tested with a canonical HEAD
different from the recorded pin, including the module-to-agent handoff.

## 12. Module signals must stay inside the run directory (2026-10-01)

The module config omitted `signal_dir`, rendering completion markers at the
filesystem root. The live worker hit the scope refusal and a permission error.
Assembly now supplies `run_dir/signals`; live guidance gives the same fallback
through `IMX_LOG_DIR` for already-running processes with an empty value. The
module-scope integration test verifies the marker is writable inside the run.

## 13. Current renderer, EngineClient and custom-pipeline checks (2026-10-01)

The live guard skipped the deleted `InputPreprocessor` import and missed a new
abstract `EngineClient.release_kv_cache_memory` method that prevented every
`AsyncOmni` construction. It now checks the dynamic Omni subclass of
`BaseRenderer` and rejects missing concrete EngineClient implementations.
Regressions cover renderer parameter drift and a newly required abstract API.
Operational custom-pipeline paths now point to `tests/e2e/features/`; the
historical prompt maps and goldens retain the old offline-inference paths.
Assignment routing retains both prefixes so older baseline pipeline jobs
continue to belong to the worker module.
The mapped renderer check exposed missing `tok_params` overrides in Omni.
Input/output upstream assignment and live prompts now include `vllm/renderers/`
and the actual `tests/inputs/` regressions. Historical prompts stay unchanged.

## 14. Scheduler queue and request-output owners (2026-10-01)

The target scheduler replaces `skipped_waiting` with `kv_holding_waiting` and
`deferred_waiting`, while keeping distinct running-slot and active-request
limits. The original scheduler test map omitted `tests/core/sched/`, where
real queue, streaming and cleanup failures surfaced. Operational scheduler
tests and live prompts now include that directory and the shared CFG pairing
tests. Historical prompt maps and stored golden files stay unchanged.

The same transition adds fixed-token prompt scores to model-runner, engine-core
and request outputs. Import checks cannot detect lost result fields; validation
must exercise real upstream output construction and asynchronous device-to-host
delivery, then verify serialization and the final request-output wrapper.

## 15. Local baseline failures require matching evidence (2026-10-02)

The old local loop treated any nonzero baseline exit as proof that the
current failure pre-existed. During main API alignment, the baseline could
not import the retired `RoutedExpertsLists` symbol against the new runtime;
it never ran the six failing metrics tests. That import crash therefore
cannot establish that those metrics failures existed on main.

The local loop now requires every current pytest failure/error node and
normalized exception line to appear in the baseline log before calling a
job pre-existing. Missing logs, bootstrap errors, different tests, and
different causes remain actionable regressions. Tests cover real log-file
inputs and a baseline containing additional old failures. The original
job commands, timeouts, and stored prompt goldens are unchanged.

## 16. SSH Bash startup must not override the selected runtime (2026-10-03)

Bash started with inherited SSH metadata and `SHLVL=0` can read `~/.bashrc`
even for `bash -c`. On this host that file prepends `~/.local/bin`, so a
release-runtime pytest launched a foreign vLLM 0.28.0 CLI in the benchmark
subprocess. A controlled SSH fixture reproduces the path change; the same
unmodified benchmark test passes when the selected release CLI runs.

The test runner now uses `bash --norc -c` for both setup and test commands.
Commands, job environments, markers, timeouts, and GPU geometry stay intact.
The golden command checks still require the captured command byte-for-byte;
only the shell startup flag changes. A real Bash regression test checks setup
and test CLI resolution with an adversarial startup file.
