---
title: "vLLM-Omni 硬门禁"
created: 2026-07-10
updated: 2026-10-14
type: rule
tags: [vllm-omni]
sources: [benchmark/incidents/_index.md, ci/incidents/_index.md, git/incidents/_index.md, remote/incidents/_index.md, "PR #8014", "PR #8786", "PR #8770", "PR #8645", "PR #8897", "PR #8673", "PR #8910", "PR #8871"]
---

# vLLM-Omni 硬门禁

只在当前任务明确属于 `vllm-project/vllm-omni` 时应用本页。先遵守根目录 `CLAUDE.md` 的通用 P0，再执行这里的仓库专属门禁。其他仓库不得继承本页的模型、远端、测试、PR 或身份假设。

## 1. 开工

1. 确认真实 vLLM-Omni checkout、分支和目标 worktree。主基准 checkout 默认只读，业务修改使用专用 worktree。
2. 写代码前读 [code taste](../../general/review/guides/code-taste.md)。
3. 新模型、新 pipeline、新 public entrypoint 或性能 claim 先读 [mini spec](../../general/planning/guides/mini-spec.md)；涉及模型或 checkpoint 再读 [model adaptation guardrails](review/guides/model-adaptation-guardrails.md)。
4. bug、crash 或行为异常先检查下面的场景触发器；直接匹配时按链接进入 owner。没有直接匹配时才进入 [vLLM-Omni 调试入口](debug/_index.md)，再通过仓库主题、`components/_index.md` 或 `models/_index.md` 确认 owner；不能停在通用规则，也不能先猜 incident 路径。
5. benchmark、profiling、GPU、serving 或远端验证先读 [benchmark 入口](benchmark/_index.md) 和 [远端入口](remote/_index.md)。只有规则明确提示、出现高度相似错误或用户要求历史复盘时才查错题。
6. 当前机器地址、worktree、venv、cache、容器和账号只从 ignored `local/` 获取，并用本轮 live 命令验证。

## 2. 场景触发器

| 用户提到 | 必读 | 硬约束 |
|---|---|---|
| profiling、trace、Perfetto、Nsight、算子或时序 | [profiling 状态机](benchmark/guides/ar-graph-profiling.md) 和 [benchmark incidents](benchmark/incidents/_index.md) | 默认交付 trace artifact 和质量摘要；benchmark stats 不能冒充 trace |
| graph mode、`enforce_eager=false` | 同上 | trace、目标请求和 server 日志必须证明来自同一轮 graph run |
| e2e、QPS、latency 或性能对比 | [benchmark scope](benchmark/guides/benchmark-scope.md) 和 [evidence gate](benchmark/guides/evidence-gate.md) | 先固定完整 metric contract，再跑正式 sweep |
| CI 红、check fail、CI 炸了 | [外部 CI 自动调查](ci/guides/ci-gotchas.md#4-ci-红时自动调查外部-check) | 自动读取所有失败 job 的真实日志并完成 diff 归因；不能因为是 Buildkite 等外部 provider 就停下来问用户 |
| config、deploy、pipeline、CLI、strict schema、默认 factory 或 direct kwargs | [Configuration 规则](components/configuration/rules.md) | 基础修改执行 `1a`–`1c`；scope 膨胀加 `1d`；strict/value-state 改动加 `1e`–`1f`；rebase/schema 扩张加 `1g`；只有规则无法解释来源加工链时再读配置审计 guide |
| stage config、并行度、设备映射或 worker 启动 | [Model Executor 规则](components/model-executor/rules.md) | 展开最终配置，并在 worker 前验证并行 world size 和可见设备 |
| runner `_preprocess` 生产的逐请求 phase metadata，例如 `_omni_prompt_len`、`_omni_num_computed_tokens`、`_omni_is_prefill`，或由它们决定的 preprocess/MTP 路由 | [Model Executor 规则](components/model-executor/rules.md#runner-到模型的预处理合同) | 先查共享 worker runner 的 producer 和路由，再查具体模型 consumer |
| 模型适配、checkpoint 或 HF 对齐 | [model guardrails](review/guides/model-adaptation-guardrails.md) 和对应模型入口 | plumbing 绿灯不等于语义正确 |
| 共享 diffusion pipeline、denoise loop 或**噪声/采样调度器**接入 | [Diffusion 组件](components/diffusion/_index.md) | 多模型共享代码的结论放 components，单模型问题放 models |
| AR/生成**请求调度**、`OmniARScheduler`、KV transfer 调度面、prefix cache 截断 | [Scheduler 组件](components/scheduler/_index.md) | 请求调度 ≠ diffusion 噪声调度 |
| connector、mooncake、跨 stage 传输损坏或 `Address already in use` | [Distributed 组件](components/distributed/_index.md) | 数据面与端口分配的坑先查这里 |
| deploy YAML/CLI 合并语义、stage 显存预算（`gpu_memory_utilization`/KV pin） | [Configuration 规则](components/configuration/rules.md) | 争议以展开后的最终配置为准（CONF-3a） |
| 对齐 upstream vLLM：rebase、API 漂移、模块波次 | [rebase 主题](rebase/_index.md) | 漂移模式两页 + 工作流 |
| CLI、HTTP、OpenAI-compatible API 或 offline/online 请求行为不一致 | [Serving 组件](components/serving/_index.md) | 先查入口到 engine 的边界，再进模型内部 |
| PR review | 先从 PR title/body 的声明目标命中组件或模型 owner 的快速入口，再用 changed files 和公开入口闭合 scope；owner 不明时才看 [组件职责地图](components/_index.md) 或 [模型列表](models/_index.md) | PR 描述只用于导航、不能证明 finding；命中 owner 后停止文档漫游，绑定当前 head、diff 和真实代码路径 |
| reviewer follow-up 且需要读取或操作 live thread | [Git/PR 入口](git/_index.md) | 绑定当前 head、diff、live review thread 和真实代码路径；不需要 live thread 时不读 |

## 3. 代码和模型

- Algorithm 决策前先查 upstream 主入口，包括 `modeling_*`、`generation_*`、tokenizer、scheduler、denoising loop、special token、attention mask 和 KV 路径。
- Crash 或 `AttributeError` 不是停止点；必须追到错误类型为什么进入当前路径。
- 仓库场景触发器未直接命中 owner 时，仓库专有 bug 必须完成第二次路由：从 [debug 入口](debug/_index.md) 进入仓库主题，再通过 [组件职责地图](components/_index.md) 或 [模型列表](models/_index.md) 找真实 owner。owner 已有 `rules.md` 时必须读取；没有时以 live 源码为准，复盘确认规则具有复用价值后再补，不为单次 issue 预建目录。
- 新模型的 `0 missing / 0 unexpected`、shape smoke、无 NaN/Inf 和 mock 权重只证明 plumbing；必须补 semantic parity matrix。
- L2 只覆盖 CPU/mock 功能且不能触发真实 stage、device 或 GPU 初始化；真实权重、精度、性能和 profiling 属于 L4。
- 新参数必须有单一 owner、公开 contract 和对应测试；禁止用 `dict.get(...) or fallback`、`hasattr`、随手 `getattr(default)` 或 `generator=None` 掩盖新旧路径不一致。
- 用户可见的 API、CLI、日志、文档或输出变更，必须按 [用户可见验收](../../general/docs/guides/user-visible-acceptance.md) 跑真实路径。

## 4. CI、测试和性能证据

- 修改测试至少实跑目标测试文件或目标函数；`compileall`、`ruff` 和 `git diff --check` 不能替代语义验证。
- Python 最后一次改动后运行 touched files 的 `ruff check` 和格式检查，提交前按仓库现有 pre-commit 入口复核。
- benchmark 开跑前写 scope lock：代码 commit、模型 snapshot、pipeline/class、请求入口、分辨率、帧数/fps、steps、并发/请求数、warmup、seed、guidance、compile/eager、GPU 和指标公式。
- 对齐其他 PR 或框架时，PR body 只是摘要；必须核对实际 config、runner/client、请求 payload、结果 JSON 和 server log。
- 异常数字先审请求链路：`config -> runner -> dataset -> request object -> payload -> server log`。禁止先加 `ignore-first`、settle 或放宽 baseline 掩盖字段未传递。
- L4 baseline 更新前先枚举完整同组 config；性能数字只能来自对应 perf runner/result JSON，functional smoke 或 accuracy 不能替代 perf baseline。
- 性能结果必须标成 `strict apples-to-apples`、`workload-aligned only` 或 `smoke only`。不满足 strict 时不能解释为框架性能差异。
- profiling 只有真实请求完成、trace 导出、artifact 来源一致且资源释放都可证明时才算完成。`ttfc_count=0`、`tpot_count=0` 等缺失指标必须写 unavailable。

## 5. 远端、容器和 GPU

- 远端 key 使用完整 `user@host:port`。先验证 repo、worktree、venv、版本、环境变量和模型 snapshot，再运行 pytest、serve、generate 或 benchmark。
- 共享机器上他人 repo、venv 和 cache 默认只读；需要修改时使用本轮自己的 clone/worktree、venv、cache 和 run directory。
- GPU 或大模型任务启动前记录 `pwd`、磁盘、GPU、`CUDA_VISIBLE_DEVICES`、HF/XDG cache 环境和目标 snapshot 是否存在。
- Hugging Face 加载使用验证过的本地绝对 snapshot，并启用 offline 模式；cache miss、需要下载或需要写他人 cache 时停止。
- SGLang diffusion 任务先跑 diffusion detection preflight，并记录 `sglang`、`torch`、cache、`PYTHONPATH` 和 GPU 环境；判定失败不进入正式运行。
- 安装、下载、编译、serve、generate、pytest 和 sweep 必须在远端自身带 timeout/watchdog、状态文件和日志，不能只依赖本地工具超时。
- 启动服务先验证 CLI，随后同时监控 health、PID 和错误日志；单请求 smoke 通过前不进入 sweep。
- 清理只针对本轮 PGID、run directory 和能够证明归属的进程；禁止宽泛 `pkill -f python`。大目录优先同盘移动到本轮 trash，再异步删除。
- 详细规则见 [live environment](../../general/remote/guides/live-environment.md)、[HF offline](../../general/remote/guides/hf-offline-mandatory.md) 和 [remote validation gates](benchmark/guides/remote-validation-gates.md)。

## 6. Git、PR 和公开证据

- 业务修改只在专用 worktree 完成；写入前确认 Git root、branch 和与目标 PR 的关系。
- vLLM-Omni commit 使用 DCO sign-off。提交和 push 前重读本节及 [Git/PR 入口](git/_index.md)。
- PR 前检查相对目标分支的 commit 列表和 diff，避免把历史实验、其他任务或临时 artifact 带入。
- PR body 先定证据合同：小修复写真实最小回归；性能 PR 绑定当前 head、最终 config、正式 result JSON 和 server log；reviewer follow-up 只回答当前仍成立的 thread。
- PR 的测试计划和结果只写语义测试、端到端、远端实测或 CI；lint、compile、diff hygiene 不能充当行为证据。
- 多个候选 PR 必须选唯一 merge vehicle。integration PR 确认后，窄 PR 只作历史参考并及时关闭 superseded 状态。
- 提交 PR 前运行仓库提供的 scope gate；触碰 tests、examples、entrypoints、model executor 或 diffusion 时逐文件说明 owner、必要性和测试绑定。
- 推送前确认 PR head owner、目标 remote 和当前 GitHub/SSH 身份。TaffyOfficial 拥有的分支只使用本机 `local/` 中登记并已验证的 Taffy SSH remote；不得回退到默认 HTTPS 身份或自造分支名。

## 7. 相关入口

- [rebase（upstream 对齐）](rebase/_index.md)
- [review](review/_index.md)
- [CI](ci/_index.md)
- [docs 和 RFC](docs/_index.md)
- [debug](debug/_index.md)
- [Configuration 规则](components/configuration/rules.md)
- [Git 和 PR](git/_index.md)
- [benchmark 和 profiling](benchmark/_index.md)
- [remote](remote/_index.md)
- [components](components/_index.md)
- [models](models/_index.md)

## VLLM-OMNI-PR8014-WAN22-COLDSTART — Wan2.2 冷启动 e2e 拆分独立 CI 作业并保持分层超时

- 触发：改动 `.buildkite/amd/test-amd-merge.yml` 中 `Diffusion · Wan22` 作业的定义或编排，或调整 `tests/e2e/offline_inference/test_wan22_t2v.py` / `tests/e2e/online_serving/test_wan22_t2v.py` 中 `init_timeout`、`stage_init_timeout`、`startup_timeout` 或外层 `timeout` 的取值（本规则仅覆盖 MERGE 通道；READY 通道的 Wan2.2 作业不在范围内）。
- 强制：Wan2.2 离线与在线两条 e2e 路径必须是两个独立的 Blocking Buildkite 作业（`Diffusion · Wan22 Offline Test` 与 `Diffusion · Wan22 Online Test`），各自拥有独立的 `timeout 40m` 进程期限，使任一路径的冷 ROCm 启动（可近 30 分钟）不挤占另一条路径；测试内层超时取 2100 秒（35 分钟），比外层作业期限少约 5 分钟留给清理（离线作业设置 `init_timeout`/`stage_init_timeout`，在线作业另设 `startup_timeout`）；拆分结构必须由 `tests/buildkite/test_upload_pipeline.py::test_amd_wan22_merge_jobs_have_independent_cold_start_budgets` 回归守护，断言每个作业命令只包含自己的测试文件。
- 禁止：把两条 Wan2.2 e2e 测试文件合并进单个作业命令串行执行；令内层超时大于或等于外层 `timeout 40m`；删除该 YAML 结构回归测试，或使任一作业重新包含另一条路径的测试文件。
- 验收：`.buildkite/amd/test-amd-merge.yml` 恰好包含上述两个 Wan2.2 作业，均含 `timeout 40m` 且互不引用对方测试文件；`pytest tests/buildkite/test_upload_pipeline.py` 通过。^[PR #8014]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8786-INTENT-GATE — 贡献者自检意图门槛：四项人类回答、PR 必要性与 bug fix 真实失败证据

- 触发：以贡献者身份准备、实现或提交自己的 vllm-omni 贡献（按 `.claude/skills/precheck-pr/SKILL.md` 的 `## Contributor intent gate` 在实现前运行，意图或范围变化时提交前复检），或 PR 改动 `.github/PULL_REQUEST_TEMPLATE.md`、`.claude/skills/precheck-pr/SKILL.md`、`CONTRIBUTING.md`、`docs/contributing/README.md` 中该门槛的小节、锚点或入口链接。该门槛只约束贡献者自检：SKILL.md 明确把它限定于 contributor self-review，排除对他人 PR 的独立评审与自动评审选择。
- 强制：四个人类必答问题——想达成什么结果、对该结果杠杆最高的变量、这项工作如何推动该变量、解决谁的客户/用户的哪些需求——须由人类贡献者作答，或附其明确回答的公开安全来源引用；任务或 issue 中已有明确人类回答时直接复用，不让贡献者重复作答；agent 不得自造回答、不得从 diff 推断认可；回答缺失或明显含糊时询问人类并等待，等待期间只可继续只读调查，实现与提交均被阻断。提交前核对 PR 描述按现行模板回答全部四个必答小节：空白小节、未改动的占位注释或 agent 写的猜测都不通过，并阻断提交与 ready-for-review。答案明确后先判断达成结果是否真的需要 PR（优先既有能力、配置、文档、支持性答复或不改动；文档改动本身也可能需要 PR；不需要时停止提交流程并说明更小的下一步；需要时把窄范围与验证计划挂钩到所述结果与变量）。bug fix 须先建立真实失败：记录期望与实际行为、受影响的修订版本（revision）与环境，并附最小复现、失败的回归测试或观测到的失败 trace 之一；失败未确立时报告证据缺口并继续调查，而非宣称已确认修复。自查报告中保留这些回答及其来源。改动上述四个文件时须保留模板四个必答小节、SKILL.md 的门槛小节（锚点 `#contributor-intent-gate`）及 `CONTRIBUTING.md` 与 `docs/contributing/README.md` 指向该锚点的两处链接。
- 禁止：agent 编写、推测或预填意图回答，或让空白小节、未改动的占位注释蒙混过关；把合理诊断或未执行过的复现脚本当作已验证失败证据；未经人类授权把私有任务对话内容复制进公开 issue/PR（意图摘要只能在授权的受众与信息共享范围内分享）；删除或改名必答小节、SKILL.md 门槛小节或锚点导致入口链接失效；把该门槛当作 GitHub/CI 对模板的机械强制，或将它套用于对他人 PR 的独立评审与自动评审选择——SKILL.md 与贡献文档明确排除了这两种用法。
- 验收：贡献者自查时，四个意图问题均有人类回答或公开安全引用并已回答“是否需要 PR”，bug fix 附可核验的失败证据（期望与实际行为、受影响修订版本与环境、最小复现/失败回归测试/失败 trace 之一）；改动四个文件的 PR 中，模板仍含四个带 `Required` 占位注释的小节，SKILL.md 含 `Contributor intent gate` 节，两份贡献文档指向 `#contributor-intent-gate` 的链接有效。^[PR #8786]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8770-NPU-RECORD-SHAPES — NPU profiler 必须显式转发 torch_profiler_record_shapes

- 触发：改动 `vllm_omni/platforms/npu/profiler.py` 中 `NPUOmniProfiler._create_profiler` 传给 `torch_npu.profiler.profile(...)` 的参数集合，特别是 `record_shapes` 一项的取舍。
- 强制：该调用必须保留 `record_shapes=profiler_config.torch_profiler_record_shapes` 的显式转发。依据 PR #8770 随该行新增的代码注释：缺省此参数时 NPU profiler 会静默忽略 `torch_profiler_record_shapes=True`。
- 禁止：从 `_create_profiler` 删除 `record_shapes` 转发，或将其改为不读取 `profiler_config.torch_profiler_record_shapes` 的常量，使该配置项重新被静默忽略。
- 验收：`vllm_omni/platforms/npu/profiler.py` 的 `_create_profiler` 中，`torch_npu.profiler.profile(...)` 调用包含 `record_shapes=profiler_config.torch_profiler_record_shapes`。^[PR #8770]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8645-DUPLEXEVAL-BENCH — Omni-DuplexEval 并入 bench serve：独占 run 目录、计时后评分与取消语义

- 触发：改动 `vllm_omni/benchmarks/duplex/omni_duplex_eval_serving.py`、`omni_duplex_eval_runner.py`、`omni_duplex_eval_judge.py`，或 `vllm_omni/entrypoints/cli/benchmark/`（cli_args.py / serve.py）中 `omni-duplex-eval` 数据集与 `--duplex-eval-*` 旗标在 `vllm bench serve` 的接线，或 `tests/benchmarks/duplex/test_omni_duplex_eval_serving.py` 与 `docs/cli/bench/serve.md` 的对应小节。
- 强制：`--duplex-eval-response-root` 仅是父目录，每次调用必须在其下创建独占 `run-*` 子目录（使并发/QPS 扫描不覆盖旧结果），实际路径写日志并存入保存结果的 `omni_duplex_eval.response_root`；只有成功的测量会话在基准计时结束后发布 `<split>/<id>.json` 与 `<id>.meta.json` 工件，工件错误记入 `omni_duplex_eval` 摘要而不丢弃服务测量。可选评分（`--duplex-eval-evaluate` 及 judge 旗标）复用既有独立评测器且必须在性能计时结束后运行，按 total/evaluated/skipped/failed 报告覆盖：零 evaluated 记为 `failed` 而非合法准确率，warmup 永不评分，judge 失败不得使已完成的性能测量作废。会话语义：一个视频即一个会话/请求；正常 barge-in 取消是回合结局而非样本失败，其已收集文本仍可交给 judge；关闭会话前排空观察到的活跃响应，关闭边界产生的响应记入元数据 `unfinished_response_ids`，由结果的 `omni_duplex_eval.published_with_unfinished_responses` 计数、非零时告警；服务器错误、失败响应与排空/关闭超时仍使样本失败。媒体准备在各并发槽的 worker 线程内懒惰执行并在请求完成时释放解码输入，参考音频只编码一次共享；准备不计入单会话时延但计入总时长与吞吐；`--omni-request-timeout-s`（默认 900 秒）只覆盖准备后的完整会话。
- 禁止：把工件直接写入 response-root 根层或复用/覆盖既有 `run-*` 目录；在计时结束前评分、给 warmup 评分，或把零 evaluated 报成有效准确率；因 judge 失败丢弃性能结果；把正常 barge-in 取消计为样本失败；在事件循环上同步解码全部媒体或令驻留解码片段数突破 `--max-concurrency`。
- 验收：`tests/benchmarks/duplex/test_omni_duplex_eval_serving.py` 通过；同一 response-root 下重复调用生成互不相同的 `run-*` 目录；保存结果含 `omni_duplex_eval.response_root`、带覆盖计数的 `omni_duplex_eval.accuracy` 与 `published_with_unfinished_responses` 字段。^[PR #8645]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8897-MERGE-LOOP — PR merge loop bible：匹配平台 L2 是唯一放行门槛，L3/L4 不阻塞合并

- 触发：改动 `docs/contributing/ci/pr_merge_loop.md`（维护者 PR 合并循环指南），或 `CONTRIBUTING.md`、`docs/contributing/README.md` 中指向该指南的入口链接，或 `docs/contributing/ci/.nav.yaml` 的 `pr_merge_loop.md` 导航项；以及在评审/合并判定中应用该指南的门槛语义（合并资格、平台 L2 匹配、docs-only 跳过记录、CUDA merge-test 回归处置）。
- 强制：合并资格 = tip 匹配的批准 + 适用的 GitHub checks 绿 + 匹配平台的 L2 ready 绿，或如实的 docs-only 跳过记录；L3/L4 不计入合并资格。L3 merge-test（`.buildkite/cuda/test-merge.yml`）可由 `merge-test` label 触发，也可合并后在 main 上运行；L4 nightly（`.buildkite/cuda/test-nightly.yml`）由 `NIGHTLY=1`/`nightly-test` 触发；两者供给 quality watch 与 LESSONS（L3 红当日 LESSONS、L4 深度 LESSONS），无论以何种方式触发都不得变成合并前门槛或逐 PR 的例行税。vendor/平台 PR 必须用匹配平台门：CUDA `.buildkite/cuda/test-ready.yml`、AMD `.buildkite/amd/test-amd-ready.yml`、NPU `.buildkite/npu/test-npu-ready.yml`（NPU 有 L2/L4、无独立 L3）、Intel 静态 `.buildkite/intel/pipeline-intel.yml`（Intel 无独立 L3/L4 文件）；严禁以 CUDA-only L2 给 Ascend/AMD 等平台标绿。docs-only 变更的 GPU/wheel 检查跳过必须记录为跳过而非“绿”，label 请求不覆盖该跳过，main 的定时 nightly 覆盖仍运行。CUDA merge-test 合并后变红时：先判定是否新合并变更所致，确认因果后立即 revert 该变更（不等同类堆叠或次日评审）；须区分基础设施失败与 flaky，先定位因果 commit 再 revert；失败检查、因果变更、revert 与后续写入 LESSONS；L3 仍是合并后健康检查，不是新增的合并前门槛。每 6 小时的自动 merge-readiness 刷新只是补充每日检查点，每日上限仍为 ≤10 triage / 1 deep review。首评清单验收线保持：bug 须有指名的、无该变更即失败的回归且 tip 上变绿；perf 数字必须来自同硬件 + 同 prompt 集的 tip vs baseline A/B 配对；验证者必须写明在该 tip SHA 实际运行了什么，未运行的检查列 “not run”，不得省略。
- 禁止：把 L3/L4 当作每 PR 的合并前阻塞；用不匹配平台的 L2 结果为 vendor PR 放行或标绿；把 docs-only 跳过表述为检查通过；未排除 infra/flake、未定位因果 commit 就 revert；删除或改写指南中上述门槛语义，或移除两个入口链接与 `.nav.yaml` 导航项。
- 验收：`docs/contributing/ci/pr_merge_loop.md` 存在并保留 CI ladder（含 L3/L4 触发方式：L3 为 `merge-test` 或合并后于 main 运行、L4 为 `NIGHTLY=1`/`nightly-test`）、High-priority selection、Daily clock、Quality watch（含 CUDA merge-test regressions 小节）与 First-comment checklist 各节的上述语义；`CONTRIBUTING.md` 与 `docs/contributing/README.md` 的入口链接有效，`.nav.yaml` 含 `pr_merge_loop.md` 且 YAML 可解析；对该指南的改动不与上述门槛相矛盾。^[PR #8897]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8673-CODEQL-SINGLE-JOB — CodeQL 单作业三语言扫描：仅 main 触发、组合语言 init 不带 build-mode

- 触发：改动 `.github/workflows/codeql.yml`（作业拆分、`github/codeql-action/init`/`analyze` 的语言列表与输入、`on` 触发器、作业 `permissions`），或提议为该工作流增删语言、恢复定时扫描、改回每语言单作业结构。
- 强制：全部 CodeQL 分析在单个 `ubuntu-latest` 作业（`analyze`）中完成，一次 `init` 以 `languages: actions,javascript-typescript,python` 组合初始化三种语言；触发器仅 `push: branches: [main]` 与 `pull_request: branches: [main]`；作业 `permissions` 保持合并时的 `contents: read` 与 `security-events: write`（PR #8673 自检核对的权限集）；组合语言 `init` 不携带单语言专用的 `build-mode` 输入，`analyze` 不指定 categories 而沿用 action 自动 categories。该文件为纯 CI 配置：验收以 pre-commit（YAML 校验、actionlint 等）与结构核对为准，不要求新增 pytest。PR #8673 合并时 hosted CodeQL 的实际运行（提取与 SARIF 上传）尚属 pending，本地 lint 通过不得被当作扫描运行成功的证据；运行性结论只能来自实际 CI 运行。
- 禁止：把 C/C++ 加回语言列表——PR #8673 记录此前 C/C++ 扫描在仓库中找不到实现源文件（仅一个头文件）并以 `No source files found` 提取失败，除非仓库实际新增 C/C++ 实现源文件；恢复每周 `schedule` 触发或未使用的 manual-build 模板；给组合语言 `init` 添加 `build-mode` 或把扫描拆成每语言一个作业；删除或缩减作业 `permissions` 块中的 `contents: read`、`security-events: write` 声明。
- 验收：`.github/workflows/codeql.yml` 恰含一个 `analyze` 作业；init 的 `languages` 为 `actions,javascript-typescript,python` 且无 `build-mode` 输入；`on` 仅含指向 `main` 的 push 与 pull_request、无 `schedule`；permissions 为 `contents: read` + `security-events: write`；`pre-commit run --files .github/workflows/codeql.yml` 通过。^[PR #8673]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8910-REPLY-CHECKLIST — 多动作评审回复用贡献者自有清单；CUDA L2/Buildkite 结果永远不向贡献者索取

- 触发：改动 `.claude/skills/review-pr/SKILL.md` 的交付指示（多动作合并回复引用 contributor-owned checklist、省略例行流程与维护者 CI 叙述的句子），或 `.claude/skills/review-pr/references/delivery/maintainer-style-study.md`（开头适用范围句，或 `## Consolidated replies with multiple actions` / `### Reviewer CI responsibilities` 两节），或 `docs/contributing/ci/pr_merge_loop.md` 合并循环指南中 consolidated 回复段及其指向 review skill 的链接。
- 强制：多条独立后续动作的合并回复必须用简短行动清单：证据允许时 2-4 项，单项动作可保持一句话，零发现也合法；每个 `- [ ]` 项写成动词加贡献者自有的具体交付物或完成条件，必要理由或证据放在其下；发起前先读既有评论与回复，只保留仍未解决的相关动作，不重复已解决请求；相关修订可链接短 SHA 指向完整 commit。已验证缺陷、证据缺口与可选建议须彼此区分，每个缺陷保留位置、触发、影响与修复方向。不得在给贡献者的回复中添加例行维护者/CI 状态小节、流程解释或编辑历史样板；实质性测试限制仅在与请求或结论相关时简短提及，绝不暗示未发生的评审或验证。评审者 CI 职责：永远不得要求贡献者提供项目的 CUDA L2/Buildkite 结果——维护者负责 CI admission 并检查匹配 head 的结果，这些检查保留在评审记录里而非例行转述给贡献者；须核对实际 label 与运行状态，区分未触发 admission、排队/运行、失败、通过与未知；不得在触发 label 之前（或 `ready` admission 之前）把尚未触发的 L2 结果当前置条件；当前 head 的适用 L2 仍在既有批准/合并门槛处必需（受既有 docs-only 跳过约束）；该节不替代正当的本地回归、复现或配对 A/B 证据，也不授予施加 label、重跑 CI 或启动算力的任何权限；撤回误发的贡献者向 L2 请求只需一行。`docs/contributing/ci/pr_merge_loop.md` 的合并循环段必须保留指向 maintainer-style-study.md `#consolidated-replies-with-multiple-actions` 锚点的链接及上述 CUDA L2 归属语义。
- 禁止：为凑清单数量捏造动作或省略真实 blocker；在贡献者回复中恢复例行 CI/流程叙述或编辑历史样板；要求贡献者提供 CUDA L2/Buildkite 结果，或在触发 label/`ready` admission 之前要求 L2 结果；以该格式为名放宽或绕过既有批准/合并门槛、docs-only 跳过、posting 权限与 label/CI 权限；删除或改写 SKILL.md 的清单引用、两节的清单与 CI 职责语义，或移除合并循环指南中的对应段落与链接。
- 验收：`maintainer-style-study.md` 保留 `Consolidated replies with multiple actions` 与 `Reviewer CI responsibilities` 两节及上述语义，SKILL.md 交付指示引用其 contributor-owned checklist；`docs/contributing/ci/pr_merge_loop.md` 合并循环段含指向该锚点的有效链接并保留“不向贡献者索取 CUDA L2/Buildkite 结果”的语义。^[PR #8910]

<!-- kb:rule status=active since=v0.30.0 -->

## VLLM-OMNI-PR8871-DUPLEXEVAL-CI — Omni-DuplexEval 夜间守卫走 bench serve 单调用：退出码不足为凭、断言前先落盘摘要

- 触发：改动 `tests/e2e/accuracy/omni_duplex_eval/` 下的守卫文件（`test_omni_duplex_eval_ci.py`、`test_omni_duplex_eval_ci_mock.py`、`conftest.py`、`README.md`），或 `.buildkite/common/ci_source_file_dependencies.yml` 中该守卫的源依赖过滤器；本规则覆盖既有 CUDA/NPU 夜间 Omni-DuplexEval 评分回归的调用方式、结果落盘与断言结构，不覆盖 `vllm bench serve --duplex-eval-*` 实现本身（见 VLLM-OMNI-PR8645-DUPLEXEVAL-BENCH）。
- 强制：守卫必须以单次 `vllm bench serve --omni --trust-remote-code --duplex-eval-evaluate --save-result` 调用完成生成、评判与结果保存（PR #8645 引入的路径）。样本选择取配置的全量 ID（`--num-prompts 0`）、`--disable-shuffle`、`--max-concurrency 1`，禁用 warmup 与基准自身的请求式就绪探测（`omni_server` fixture 已等待启动）。完整结果写 `results/bench-result.json`（含 `omni_duplex_eval.accuracy`、发布错误与 `published_with_unfinished_responses`），其 `omni_duplex_eval.accuracy` 对象复制为 `results/result_acc_{model}_{dataset}_{date}_{random_number}.json`（model/dataset 取配置 ID/路径的 basename，UTC `YYYYMMDD-HHMMSS` 加 8 位随机后缀）供既有 `results/*.json` 工件上传一并收集，不得另跑第二次评测或汇总。CLI 成功退出不足以判通过：必须核对 completed/published/evaluated 计数与 request、artifact、judge 错误（基准在评判失败时仍保留性能结果），摘要须在断言之前保存以便追溯失败细节；调用基准前先清除旧的 `bench-result.json` 与 `result_acc_*.json`，防止 CLI 早退后陈旧结果被当作当次结果上传。保留协议 pin（config、个体分数与摘要均匹配 `PROTOCOL_PIN`）与分数阈值断言（当前 record mode 0.0），并核对分数文件数量与 split/ID 集合同 CI 选择一致。fixture 须双设备隔离：judge 起在第一个分配设备、Omni server 起在第二个，健康的预启动 judge 可经 `VLLM_DUPLEX_EVAL_JUDGE_URL` 复用；未完成响应的样本仍可参评，其计数可从完整结果读取。CPU mock 测试必须驱动真实守卫：`_run_cli` 替换为记录 argv 并保存合成基准结果的 spy，用真实 `bench serve` 解析器解析拼装命令，验证工件持久化，并在 CLI 成功退出时仍拒绝不完整的生成、发布或评判。
- 禁止：把守卫改回独立的 generate→evaluate→summarize 三段式 `vllm bench omni-duplex-eval` 调用，或引入第二评测/汇总流程；仅凭 CLI 退出码放行而不核对覆盖与错误计数；先断言后保存摘要；跑基准前不清除 results 目录中的旧结果文件；删除协议 pin、分数阈值断言或 split/ID 集合核对；把 mock spy 改回只记录 argv 不落盘合成结果，使覆盖断言失去真实驱动。
- 验收：`tests/e2e/accuracy/omni_duplex_eval/test_omni_duplex_eval_ci_mock.py` 以 `core_model` + `cpu` 标记通过；`.buildkite/common/ci_source_file_dependencies.yml` 中该守卫的源依赖指向 `vllm_omni/benchmarks/serve.py`、`vllm_omni/benchmarks/patch/**` 与 `vllm_omni/entrypoints/cli/benchmark/**`；README 记录双设备 fixture（CUDA 为 `full_model and cuda and (H100 or B200) and cards_2`，NPU 为 `full_model and npu and A3 and cards_2`）与结果布局（`results/bench-result.json`、`<response-root>/run-*/evaluation/evaluation_summary.json`、`result_acc_*.json`）。^[PR #8871]

<!-- kb:rule status=active since=v0.30.0 -->
