---
title: "vllm-omni AMD/ROCm CI 流水线规则"
created: 2026-10-22
updated: 2026-10-22
type: rule
tags: [vllm-omni]
sources: ["PR #9227", "PR #9224"]
---

# vllm-omni AMD/ROCm CI 流水线规则

## VLLM-OMNI-PR9227-R2-01-SHARDING — AMD READY R2-01 双分片：pytest-shard 切分、分片独占工件、fail-closed 与超时保留

- 触发：改动 `.buildkite/amd/test-amd-ready.yml`（AMD READY 通道，区别于 PR #8014 覆盖的 MERGE 通道 test-amd-merge.yml）中 `ROCm · Model Executor GPU Coverage (R2-01) · Shard %N/%t` 步骤——`parallelism`、pytest 命令、junit/log/summary 工件路径、超时或 grade——或 `tests/buildkite/test_amd_ready_r2_01.py` 结构回归测试的断言。
- 强制：R2-01 必须保持 `parallelism: 2` 的两个并行 `mi300_1` 分片，label 以 `· Shard %N/%t` 结尾；分片只能经 pytest-shard 旗标 `--num-shards=$$BUILDKITE_PARALLEL_JOB_COUNT`、`--shard-id=$$BUILDKITE_PARALLEL_JOB` 实现，pytest 选择面保持不变（单卡 marker 表达式、`--run-level "core_model"`、`--durations=0`、`-v`、`-ra`）；junit XML、tee 的 log 与 `tail -n 100` 生成的 summary 三类工件文件名都必须嵌入 `$${BUILDKITE_PARALLEL_JOB}-of-$${BUILDKITE_PARALLEL_JOB_COUNT}` 后缀，使并发分片互不覆盖；保留 `timeout --signal=TERM --kill-after=2m 45m` 命令超时（为 summary 工件留约 5 分钟）、`timeout_in_minutes: 50`、`grade: NonBlocking`、`mirror_hardwares: [amdproduction]`、`depends_on: amd-build`，以及 fail-closed 语义：该步骤恰好一条 pytest 命令、无 `--collect-only`、无 `VLLM_CI_ALLOW_NO_TESTS`。结构必须由 `tests/buildkite/test_amd_ready_r2_01.py` 回归守护：断言 `parallelism == 2`、argv 含两个分片旗标与 `-v`/`-ra`/`--durations=0`、三类工件名带分片后缀、单条 pytest 命令、fail-closed 旗标缺失与 45m/50m 超时。动机（PR #9227 记录的实测）：单作业选中 1,811 个用例需 pytest 34m21s、作业 35m05s（build 14388）；双分片实测 19m01s/21m35s、关键路径 22m22s，且两个 AITER MHA kernel 在每个干净 worker 上独立编译（253-254s 与 275-278s），分片不会摊薄每分片的编译成本。
- 禁止：移除 `parallelism` 退回单作业串行；以拆分 marker 表达式或测试选择的方式代替 pytest-shard（改变选择面）；使用不带分片后缀的共享 `pytest.xml`/`pytest.log`/`pytest-summary.txt` 文件名让并发分片互相覆盖工件；缩短 45m 命令超时或 50m 作业超时；改动 `NonBlocking` grade 或 `mirror_hardwares`；引入 `--collect-only` 或 `VLLM_CI_ALLOW_NO_TESTS`；删除或削弱结构回归测试中 parallelism、分片旗标、分片工件名、fail-closed 或超时断言。
- 验收：`.buildkite/amd/test-amd-ready.yml` 的 R2-01 步骤含 `parallelism: 2`、以 `· Shard %N/%t` 结尾的 label、pytest 命令含 `--num-shards` 与 `--shard-id`、三类工件名均含 `-of-` 分片后缀、命令级 `45m` 与 `timeout_in_minutes: 50`、`grade: NonBlocking`；`python3 -m pytest --confcutdir=tests/buildkite tests/buildkite/test_amd_ready_r2_01.py -q` 通过；`pre-commit run --files .buildkite/amd/test-amd-ready.yml tests/buildkite/test_amd_ready_r2_01.py` 通过。^[PR #9227]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR9224-AITER-MHA-ABI-FALLBACK — AMD AITER MHA 缓存：精确可信源在前、可信分支非 PR 回退候选随后追加，注意力套件成功后发布

- 触发：改动 `.buildkite/amd/scripts/aiter_mha_cache.py` 中候选构建来源的发现或生产者校验（`candidate_sources`、`discover_build_sources`、`discover_recent_branch_sources`、`_build_numbers_from_listing`、`provenance_matches_source`）；或改动 `.buildkite/amd/test-amd-ready.yml` 中 FlashAttention/AITER 作业的缓存 prepare、注意力测试编排与发布步骤；或改动 `tests/buildkite/test_aiter_mha_cache.py`、`tests/buildkite/test_amd_pipeline.py` 中对应契约测试。R2-01 lane 的分片契约不在本规则范围（PR #9224 明确不改该 lane，见 VLLM-OMNI-PR9227-R2-01-SHARDING）。
- 强制：`candidate_sources` 必须保持“精确可信来源在前、可信分支回退候选在后”的列表顺序：先收集同 PR/head commit/merge base/精确 checkout 的早期构建与可信 commit 上的非 PR 构建；随后对每个可信分支（`main`；本构建为非 PR release-line push 时含 `release/vX.Y.Z`）调用 `discover_recent_branch_sources` 追加其最近的非 PR 构建（默认 `limit=15`，commit 须为完整 SHA、剔除 PR 构建与当前构建自身，按 build_id 去重）。该追加是无条件的——调用点不检查精确来源是否已找到，优先级完全由列表顺序表达；`discover_recent_branch_sources` 文档所述“精确 merge-base 从未发布（如镜像 ABI 升级后）”只是该函数的动机说明，不是调用前提。兼容性闸门是既有工件校验链（checksum、`gfx942` 设备码、运行时 ABI 身份哈希、Buildkite 来源元数据、provenance），不是 Omni merge-base SHA 等值——`.so` 由 ROCm 镜像内的 AITER 产出，身份哈希才是兼容依据；因此 `provenance_matches_source` 对可信分支非 PR 生产者只要求：分支属于可信分支、非 PR、`VLLM_OMNI_AITER_MHA_TRUSTED_COMMIT` 为完整 SHA。消费方仍获得 job-local 可写副本，缓存未命中时在该目录原地编译。Buildkite 查询/校验失败的日志必须脱敏：不得携带 commit、branch 或异常文本，仅输出固定措辞（可含构建号）。READY 的 FlashAttention/AITER 作业仅在注意力测试套件退出码为 0 时发布该模块，使后续 READY/MERGE 作业可命中同一 ABI。
- 禁止：把可信分支回退候选排在精确可信来源之前；对可信分支来源恢复 `source.commit == VLLM_OMNI_AITER_MHA_TRUSTED_COMMIT` 的等值要求；接受 PR 产生的、当前构建自身的、或未通过校验链的原生模块进入另一构建；在注意力套件非零退出时发布模块；在失败日志中回填异常文本、commit 或 branch 值；删除候选排序、可信分支 provenance、PR 拒绝、不兼容 ABI 拒绝、checksum 校验、敏感值日志或 FA 发布的契约测试。
- 验收：`tests/buildkite/test_aiter_mha_cache.py` 与 `tests/buildkite/test_amd_pipeline.py` 通过；`aiter_mha_cache.py` 的 `candidate_sources` 在精确来源之后无条件追加去重、排除自身构建的可信分支非 PR 候选，可信分支 provenance 保持三项条件，失败日志为脱敏固定措辞；`test-amd-ready.yml` 的 FA/AITER 作业仅在注意力套件成功（退出码 0）后发布模块。^[PR #9224]

<!-- kb:rule status=active since=v0.30.0 -->
