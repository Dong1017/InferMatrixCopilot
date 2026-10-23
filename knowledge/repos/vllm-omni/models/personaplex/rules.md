---
title: "PersonaPlex 规则"
created: 2026-09-02
updated: 2026-10-23
type: rule
tags: [vllm-omni, models, model-executor]
sources: ["PR #4771", "PR #6318", docs/design/fullduplex-personaplex.md, vllm_omni/model_executor/models/personaplex/duplex/serving_adapter.py, vllm_omni/model_executor/stage_input_processors/personaplex.py, tests/e2e/features/fullduplex/, tests/entrypoints/openai_api/test_duplex_handler.py, "PR #7399", "PR #7481", "PR #8670", "PR #8751", "PR #9113"]
confidence: high
---

# PersonaPlex 规则

## Direct 代码快速入口

| PR 描述信号 | 规则 |
|---|---|
| frame/de-delay、session codec、persona/voice mutation | `PPLEX-1a`–`PPLEX-1c` |
| standalone lease | `PPLEX-2a` |
| checkpoint config 或 sampling capability | `PPLEX-3a`、`PPLEX-3b` |

只有 `PPLEX-数字字母` 是可审计规则 ID。

## PPLEX-1a — frame 与 de-delay 边界必须跨 chunk 连续

- 触发：PCM append、Mimi encode/decode、talker→code2wav processor 或 chunk flush。
- 强制：输入严格是 24 kHz mono float PCM；每个物理 append unit 是 1920 samples/80 ms，
  reservation 失败回滚相同 bytes 且不推进 frame cursor。acoustic de-delay 使用下一 frame
  构造 cb1..7，所以非 final chunk 必须保留最后 raw code frame 给下一 chunk。
- 禁止：按任意 client packet 直接推进模型；每 chunk 丢弃边界 frame；把空/全零/非有限
  audio 或只含 listen event 视为成功。
- 验收：覆盖 partial-frame final padding、prepare/submit/commit rollback、重复 operation id、
  跨两个 chunk 的 codebook 对齐与 frame/audio 时长计数。^[PR #4771]

## PPLEX-1b — codec 状态和容量以 session 身份隔离

- 触发：并发 session、slot reuse、abort/close、Stage 0/1 codec pool 或 admission 配置。
- 强制：Stage 0 encoder 按完整 `(session_id, incarnation)`、Stage 1 decoder 按稳定 request id
  独占；`duplex_session.max_sessions` 是 admission 与所有 codec pool 的单一容量源。cleanup
  只回收目标身份，replacement session 必须重建 voice/persona、PCM tail、Mimi state 与
  delayed frame。
- 禁止：在 connector extra 复制容量；共享卷积/KV state；timeout 后工作仍在运行却提前
  释放 slot。
- 验收：两 session 交错产生独立非空输出，第三个在上限处拒绝；关闭一条不改变另一条，
  replacement 用不同 persona 且无 state leak；异常、timeout、abort 路径无遗留。^[PR #4771]

## PPLEX-1c — persona 与 voice 在 session 创建后不可变

- 触发：PersonaPlex `session.update` 修改 `instructions` 或 `voice`。
- 强制：runtime adapter 对 changed persona/voice 分别返回 `persona_update_unsupported`/
  `voice_update_unsupported`；相同值可保留。PersonaPlex 还必须拒绝 MiniCPM-o 专属的
  `minicpmo45_native_duplex` client flag，不能让它改变 append path 或绕过 adapter。
- 禁止：ACK 变更却沿用旧 `personaplex_prefill_slots`；让 MiniCPM-o 的 `ref_audio` 检查抢先产生
  错误码；在未原子重建 persona/voice 派生 reservation 前开放更新。
- 验收：覆盖不同长度 persona、changed/unchanged voice 和 MiniCPM-o opt-out flag，断言命中
  PersonaPlex 专属 typed error，且并发 session、容量、reuse 与音频输出不回归。^[PR #6318]

## PPLEX-2a — standalone 单会话 lease 必须覆盖不可取消的线程调用

- 触发：standalone `DuplexServer`（`batch_size=1`）的 `/api/chat` 或
  `/v1/audio/duplex` disconnect、task cancellation 或快速重连；batched server 另由
  slot manager 的 lock/epoch 隔离并发会话。
- 强制：`asyncio` task 取消后，`asyncio.to_thread(session.feed)` 的 engine call 仍可能运行；
  lease 只能在 out-of-band completion event 证明调用结束后释放，否则继续持有/拒绝新连接。
  `open()`/`feed()` 不能与前 session 的 `step()` 在同一 engine 上并发。
- 禁止：把 `cancel(); await task` 返回当作 worker thread 已停止；在 finally 直接清 active
  lease，使新连接 reset 正被旧线程修改的 KV/cache。
- 验收：阻塞 feed 后 disconnect，并在其真实完成前 fast reconnect；新会话必须被拒绝或
  等待，完成后才能 acquire，且没有 CUDA crash 或跨 session state bleed。^[PR #4771]

## PPLEX-3a — empty checkpoint config 必须从 architecture 恢复本地 config

- 触发：HF config auto-detection、registry/pipeline 重命名或 staged arch override。
- 强制：两个 PersonaPlex architecture 都在 `_ARCH_TO_MODEL_TYPE` 映射到 `personaplex`，
  注册本地 `PersonaPlexConfig`；talker config 无 architectures 时补 stage-0 talker arch，
  code2wav arch 由 pipeline stage 显式给出。
- 禁止：假设空 `config.json` 能自动给出 model_type/architecture；要求用户手写 override。
- 验收：空 config fixture 从 talker/code2wav arch 都解析到 personaplex pipeline，两个 stage
  分别装载正确 class；未知 arch 明确失败。^[PR #4771]

## PPLEX-3b — sampling capability 必须如实保持 greedy-only

- 触发：serving/config CLI、runtime sampling params、text head 或 depformer decoding。
- 强制：该 pin 的 text 与 depformer 都走 greedy；unified Stage 0 固定 temperature 0、top-k 1、
  每 segment max_tokens 1。只有真实 sampling consumer 和质量验证到位后才暴露 temperature、
  top-k 或 seed。
- 禁止：保留无法关闭的 `--greedy` 或未被消费的 sampling knobs；把 continuous overlapping
  speech 宣称成 `supports_barge_in=true`。
- 验收：public capability/config 不出现 dead sampling controls，非 greedy 请求明确拒绝或
  归一化；capability 保持 `supports_barge_in=false`。^[PR #4771]

## PPLEX-1d — Mimi encode 只计算被消费的 codebook prefix

- 触发：修改 `PersonaPlexMimiCodec.encode_frame()` 的HF quantizer调用或返回codec layout。
- 强制：向真实 `quantizer.encode` 传 `num_quantizers=CODEBOOKS`（当前8），只计算semantic+所需
  acoustic prefix；返回仍是contiguous long `[batch,CODEBOOKS]`，不得修改checkpoint加载、decoder
  的32-codebook capacity或streaming front-end state。前8的residual选择不依赖后24层。
- 禁止：先算32再slice并声称避免suffix工作；重排prefix浮点运算；以synthetic quantizer-only
  speedup推断full codec/LM、audio quality、GPU memory或并发session收益。
- 验收：非零real HF quantizer fixture与旧full32路径逐token相等，spy断言prefix层执行且suffix
  零调用；batch1/2/9下跨ring-wrap、slot reset与full reset仍prefix相等，decoder容量不变。
  stateless quantizer之后重复断言所有front-end内部state不是prefix优化必要证据；GPU微测需
  单独记录版本、warmup、交替order、source hash与synthetic范围。^[PR #7399]

## PPLEX-1e — prefill vectorization 必须保留逐 token embedding 语义

- 触发：优化talker `_build_prefill_embed` 或duplex first-append的voice/persona prefill。
- 强制：非负offset/span用一次 `[1,1+n_q,span]` stack和一次现有embedding调用；text超过末尾
  用zero_text=3，agent行用有效silence prefix，user行用有效user_sine或silence fallback，过短
  code tensor按原分支处理。embedding仍按既有固定table-add顺序，不作沿span的浮点reduction。
- 禁止：每token `.item()` 将device scalar读回Python；改padding、audio fallback、dtype/device/
  contiguous layout或输入tensor；重复first-append prefill。helper速度不能外推serving整体速度。
- 验收：faithful旧loop和手算layout在CPU/GPU FP32/BF16/FP16下逐byte相等，覆盖nonzero offset、
  partial/at-end/past-end/empty text、strided/short/absent audio与chunk拼接；拒绝所有scalar read
  时仍只有一次embedding调用。Stage0真实embedding integration证明first append只一次prefill，
  second append只含live frame；组合stack的E2E结果不能单独归因此PR。^[PR #7481]

## VLLM-OMNI-PR8670-AMD-PPLEX-NIGHTLY — PersonaPlex temporal 压力用例只在 AMD nightly 串行承接

- 触发：改动 `.buildkite/amd/test-amd-ready.yml`、`.buildkite/amd/test-amd-merge.yml`、
  `.buildkite/amd/test-amd-nightly.yml` 的 Model Executor CPU shard 或 “Simple · PersonaPlex
  Temporal Streaming Test” 步骤；重命名 `tests/model_executor/models/personaplex/
  test_temporal_streaming_hoist.py` 中的 `*_matches_legacy_end_to_end` 用例；或修改
  `tests/buildkite/test_amd_pipeline.py` 的 AMD pipeline contract。
- 强制：AMD READY/MERGE 的 Model Executor 步骤保持 `-m 'core_model and cpu and not omni'`
  与 `--num-shards=$$BUILDKITE_PARALLEL_JOB_COUNT`/`--shard-id=$$BUILDKITE_PARALLEL_JOB`
  分片，只允许两条精确到 node 的 `--deselect` 前缀（`test_temporal_streaming_step_matches_
  legacy_end_to_end` 与 `test_mimi_streaming_step_matches_legacy_end_to_end`），同文件其余
  快速节点继续留在阻塞 shard。被移出的 stress 用例必须由 nightly 的 PersonaPlex Temporal
  步骤承接：`mi300_1`、`grade: NonBlocking`、`timeout_in_minutes: 120`，内层用
  `timeout --signal=TERM --kill-after=2m 110m` 留出 artifact 落盘与 teardown 余量，先
  `--collect-only` 落盘再串行执行，collection 输出、JUnit XML、完整 pytest log 与 tail
  summary 全部作为 artifact 上传。
- 禁止：用 `--ignore` 整文件、slow marker 或改写 marker 表达式替代精确 deselect；为该
  nightly 步骤引入 retry、xdist、空 collection override 或掩盖失败的 fallback；把该
  deselection 传播到 CUDA READY/MERGE（CUDA 管线不得过滤该文件的任何节点）；deselect 后
  不在 nightly 承接，静默丢失 oracle 覆盖。
- 验收：`tests/buildkite/test_amd_pipeline.py` 的 contract 必须同时锁定 ready/merge（恰一个
  Model Executor CPU shard、marker 不变、`parallelism == 3`、无 `--ignore`、deselect 恰为
  两条前缀）与 nightly（选择同样前缀、NonBlocking、120/110 超时结构、预期 artifact 路径）；
  重命名测试函数时必须同步 deselect 前缀与 contract。^[PR #8670]

<!-- kb:rule status=retired since=v0.30.0 retired_at=v0.30.0 reason=superseded evidence="PR #8751 diff：.buildkite/amd/test-amd-merge.yml 与 test-amd-ready.yml 各删除 temporal --deselect 行、仅保留 mimi 行；test-amd-nightly.yml 的 collect-only 与 run 命令都只剩 test_mimi_streaming_step_matches_legacy_end_to_end；tests/buildkite/test_amd_pipeline.py 同步更新；changed_files 含 tests/model_executor/models/personaplex/test_ring_per_row_offsets.py，其 diff 删除了 test_ring_slot_recycle_masks_old_history。PR body 明确：“#8670 added AMD pipeline steps that name the temporal parity test removed here. Those entries are dropped, and the Mimi streaming stress case keeps its nightly owner.” 且 “The ring-KV and RoPE primitives that Mimi uses stay in personaplex_temporal.py.”" superseded_by=VLLM-OMNI-PR8751-AMD-PPLEX-NIGHTLY -->

## VLLM-OMNI-PR8751-AMD-PPLEX-NIGHTLY — PersonaPlex Mimi 压力用例只在 AMD nightly 串行承接

- 触发：改动 `.buildkite/amd/test-amd-ready.yml`、`.buildkite/amd/test-amd-merge.yml`、`.buildkite/amd/test-amd-nightly.yml` 的 Model Executor CPU shard 或 “Simple · PersonaPlex Temporal Streaming Test” 步骤；重命名或删除 `tests/model_executor/models/personaplex/test_temporal_streaming_hoist.py` 中的 `test_mimi_streaming_step_matches_legacy_end_to_end`；或修改 `tests/buildkite/test_amd_pipeline.py` 的 AMD pipeline contract。
- 强制：AMD READY/MERGE 的 Model Executor 步骤保持 `-m 'core_model and cpu and not omni'` 与 `--num-shards=$$BUILDKITE_PARALLEL_JOB_COUNT`/`--shard-id=$$BUILDKITE_PARALLEL_JOB` 分片，只允许一条精确到 node 的 `--deselect` 前缀（`test_mimi_streaming_step_matches_legacy_end_to_end`），同文件其余快速节点继续留在阻塞 shard。被移出的 Mimi stress 用例由 nightly 的 PersonaPlex Temporal 步骤串行承接（collect-only、110m timeout、junitxml artifact）。三份 yml 与 `tests/buildkite/test_amd_pipeline.py` 断言的 contract 必须同步修改。
- 禁止：恢复 `test_temporal_streaming_step_matches_legacy_end_to_end` 的 `--deselect` 或 nightly 条目——该用例已随 dead temporal stepper（`PersonaPlexTemporalStreaming`/`_TemporalLayer`）在 PR #8751 删除；把 `--deselect` 放宽为文件级或通配前缀；只改 yml 不改 `test_amd_pipeline.py`（或反之）；因 stepper 已删而连带删除 `personaplex_temporal.py` 中 Mimi 仍消费的 ring-KV/RoPE 原语。
- 验收：READY/MERGE 只剩 Mimi 一条 `--deselect`；nightly 步骤的 collect 与 run 都只含 Mimi 用例；`tests/buildkite/test_amd_pipeline.py` 与三份 yml 当前内容一致并通过；`personaplex_temporal.py` 保留的 ring-KV/RoPE 原语仍由 `test_ring_per_row_offsets.py` 的存活用例覆盖——注意 PR #8751 同时删除了该文件中的 `test_ring_slot_recycle_masks_old_history`，不得把该文件的覆盖说成完全不变；原语的检查义务另受 PPLEX-HOIST-1a 约束。 ^[PR #8670] ^[PR #8751]

<!-- kb:rule status=active since=v0.30.0 supersedes=VLLM-OMNI-PR8670-AMD-PPLEX-NIGHTLY -->

## VLLM-OMNI-PR9113-CODE2WAV-DELTA — 整 session 复用的 Code2Wav 请求必须以 DELTA 音频流出

- 触发：修改 PersonaPlex duplex plugin 的 `configure_sampling_params` 或任一 stage 的 `RequestOutputKind`；修改共享 `CumulativeAudioTextDataPlane`（`vllm_omni/model_executor/common/duplex/data_plane.py`）的 `audio_output_is_delta` 投影；或发现整 session 复用的 duplex stage 请求仍在用默认 CUMULATIVE 输出——那会让 output processor 在 API server 进程内每帧重 consolidate 全部历史 multimodal tensors，每帧成本随通话长度增长。
- 强制：只为 final stage（`configured[-1]`，即 Code2Wav）在 clone 出的 sampling params 上设 `output_kind=RequestOutputKind.DELTA`，不得改写共享默认值；Stage 0 与中间 stage 保持默认 CUMULATIVE。PersonaPlex 数据平面声明 `audio_output_is_delta = True`：delta 音频按新 chunk 整段透传，不再对 running sample count 切片；DELTA 下 output processor 不做 consolidate，一次输出可能携带多个 step 的 chunk list，必须先 join 成单个 waveform 再计数与编码。文本继续走 cumulative text cursor，只发新 suffix。共享数据平面的 `audio_output_is_delta` 默认保持 `False`，其他 duplex 模型维持现有 cumulative 切片行为。
- 禁止：直接改写共享或默认 sampling params；给非 final stage 设 DELTA；delta 模式下仍按 sample cursor 切音频，或未声明 delta 就跳过切片；假设每个输出恰好一个音频 chunk；只凭机制推理、不在目标并发下做端到端测量就宣称每帧成本不再随通话长度增长。
- 验收：`tests/model_executor/executor_common/duplex/test_data_plane.py` 覆盖 delta 音频整段透传（短于先前 chunk 的 chunk 也整段编码；空 chunk 且无新文本不产出）与同批到达的 ragged chunks 以 numpy 与 torch tensor 形态 join 成单个 waveform（`test_delta_audio_passes_through_unsliced`、`test_delta_audio_chunks_that_arrive_together_are_joined`），且既有 cumulative 投影用例不回归；`tests/model_executor/models/personaplex/duplex/test_plugin.py` 断言 Stage 1 是 DELTA clone、Stage 0 与共享默认值未被污染，并覆盖只有 final stage 切到 DELTA（`test_only_the_final_stage_switches_to_delta_output`）；`tests/engine/duplex/test_session_runner_personaplex.py` 以 delta Code2Wav 输出驱动投影；性能收益须在目标并发（1–4 session）下端到端实测（PR #9113：4-session 下 API server CPU 0.8→0.2 cores）。^[PR #9113]

<!-- kb:rule status=active since=v0.30.0 -->
