---
title: "Model Executor — Dynamo/cudagraph compile guard rules"
created: 2026-10-13
updated: 2026-10-13
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #8846"]
---

# Model Executor — Dynamo/cudagraph compile guard rules

## VLLM-OMNI-PR8846-DICT-UPDATE-GUARD — Dynamo 编译区 forward 内禁止 dict.update，字典合并必须单表达式 `**` 展开

- 触发：修改会经 Dynamo/`torch.compile` 变换的模型 forward（本例 `vllm_omni/model_executor/models/qwen3_omni/qwen3_omni_moe_thinker.py` 非 last PP rank 分支把 captured hidden states 并入 `IntermediateTensors`），或在其中新增/保留 `.update` 调用。
- 强制：最终字典必须在单个 dict 显示表达式中用 `**` 展开完成合并（`IntermediateTensors({"hidden_states": hidden_states, "residual": residual, **captured})`）；PR 记录 `{**base, **captured}` 编译为 map-merge 字节码、无方法调用，捕获项可先按原 comprehension 构建局部 `captured` 字典再展开。重写只改构造方式，键与取值来源与原实现相同（PR 记录 contents、key order 与 capture semantics 不变）。审查此类改动时同步审计该编译 forward 内其余 `.update` 调用：PR #8846 审计确认这是 `vllm_omni/model_executor/models/` 编译 forward 内唯一一处，weight loading、input processor、sampler grouping、cache 对象方法等不经 Dynamo 变换 forward 的路径不受影响。
- 禁止：在编译区 forward 内调用 `tensors.update(...)`——vLLM 的 cudagraph-in-compiler guard（`vllm/compilation/wrapper.py` 的 `TorchCompileWithNoGuardsWrapper.bytecode_hook`）对 transformed `co_names` 按名拒绝任何 `update`（视为可能的 nn.Module buffer mutation），plain dict 的 `update` 同样命中，自 #7345 起使每个 `cudagraph_mode != NONE` 的 Thinker 部署在编译期硬失败并报误导性 buffer-mutation 错误；不得借绕过 guard 之名改变返回的键集合或 capture 语义。
- 验收：按 `tests/model_executor/models/qwen3_omni/test_qwen3_omni_compile_guard.py` 的两层回归：(1) 源码级 pin 断言 `"update" not in Qwen3MoeLLMModel.forward.__code__.co_names`；(2) 用 toy decoder 层、first-but-not-last PP rank、captures enabled 的真实 forward 在 `torch.compile(fullgraph=True)` 下运行该 bytecode_hook（`CUDAGraphMode.FULL_AND_PIECEWISE`），断言 guard 接受 transformed 字节码、`_compiled_bytecode.co_names` 无 `update`、返回的 `IntermediateTensors` 键集合仍为 `{hidden_states, residual, capture_0}`，并附 stash 源码改动后两测试均失败的 red/green 记录。^[PR #8846]

<!-- kb:rule status=active since=v0.30.0 -->
