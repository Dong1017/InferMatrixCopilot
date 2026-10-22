---
title: "vllm-omni AMD/ROCm CI 流水线规则"
created: 2026-10-22
updated: 2026-10-22
type: rule
tags: [vllm-omni]
sources: ["PR #9227"]
---

# vllm-omni AMD/ROCm CI 流水线规则

## VLLM-OMNI-PR9227-R2-01-SHARDING — AMD READY R2-01 双分片：pytest-shard 切分、分片独占工件、fail-closed 与超时保留

- 触发：改动 `.buildkite/amd/test-amd-ready.yml`（AMD READY 通道，区别于 PR #8014 覆盖的 MERGE 通道 test-amd-merge.yml）中 `ROCm · Model Executor GPU Coverage (R2-01) · Shard %N/%t` 步骤——`parallelism`、pytest 命令、junit/log/summary 工件路径、超时或 grade——或 `tests/buildkite/test_amd_ready_r2_01.py` 结构回归测试的断言。
- 强制：R2-01 必须保持 `parallelism: 2` 的两个并行 `mi300_1` 分片，label 以 `· Shard %N/%t` 结尾；分片只能经 pytest-shard 旗标 `--num-shards=$$BUILDKITE_PARALLEL_JOB_COUNT`、`--shard-id=$$BUILDKITE_PARALLEL_JOB` 实现，pytest 选择面保持不变（单卡 marker 表达式、`--run-level "core_model"`、`--durations=0`、`-v`、`-ra`）；junit XML、tee 的 log 与 `tail -n 100` 生成的 summary 三类工件文件名都必须嵌入 `$${BUILDKITE_PARALLEL_JOB}-of-$${BUILDKITE_PARALLEL_JOB_COUNT}` 后缀，使并发分片互不覆盖；保留 `timeout --signal=TERM --kill-after=2m 45m` 命令超时（为 summary 工件留约 5 分钟）、`timeout_in_minutes: 50`、`grade: NonBlocking`、`mirror_hardwares: [amdproduction]`、`depends_on: amd-build`，以及 fail-closed 语义：该步骤恰好一条 pytest 命令、无 `--collect-only`、无 `VLLM_CI_ALLOW_NO_TESTS`。结构必须由 `tests/buildkite/test_amd_ready_r2_01.py` 回归守护：断言 `parallelism == 2`、argv 含两个分片旗标与 `-v`/`-ra`/`--durations=0`、三类工件名带分片后缀、单条 pytest 命令、fail-closed 旗标缺失与 45m/50m 超时。动机（PR #9227 记录的实测）：单作业选中 1,811 个用例需 pytest 34m21s、作业 35m05s（build 14388）；双分片实测 19m01s/21m35s、关键路径 22m22s，且两个 AITER MHA kernel 在每个干净 worker 上独立编译（253-254s 与 275-278s），分片不会摊薄每分片的编译成本。
- 禁止：移除 `parallelism` 退回单作业串行；以拆分 marker 表达式或测试选择的方式代替 pytest-shard（改变选择面）；使用不带分片后缀的共享 `pytest.xml`/`pytest.log`/`pytest-summary.txt` 文件名让并发分片互相覆盖工件；缩短 45m 命令超时或 50m 作业超时；改动 `NonBlocking` grade 或 `mirror_hardwares`；引入 `--collect-only` 或 `VLLM_CI_ALLOW_NO_TESTS`；删除或削弱结构回归测试中 parallelism、分片旗标、分片工件名、fail-closed 或超时断言。
- 验收：`.buildkite/amd/test-amd-ready.yml` 的 R2-01 步骤含 `parallelism: 2`、以 `· Shard %N/%t` 结尾的 label、pytest 命令含 `--num-shards` 与 `--shard-id`、三类工件名均含 `-of-` 分片后缀、命令级 `45m` 与 `timeout_in_minutes: 50`、`grade: NonBlocking`；`python3 -m pytest --confcutdir=tests/buildkite tests/buildkite/test_amd_ready_r2_01.py -q` 通过；`pre-commit run --files .buildkite/amd/test-amd-ready.yml tests/buildkite/test_amd_ready_r2_01.py` 通过。^[PR #9227]

<!-- kb:rule status=active since=v0.30.0 -->
