---
title: "Diffusion attention dense 4D mask routing"
created: 2026-10-23
updated: 2026-10-23
type: rule
tags: [vllm-omni, components, diffusion]
sources: ["PR #9248"]
---

# Diffusion attention dense 4D mask routing

## VLLM-OMNI-PR9248-DENSE4D-MASK-ROUTE — dense 4D mask 只允许按调用回退 SDPA，显式选择与 paged KV 必须失败关闭

- 触发：修改 `vllm_omni/diffusion/attention/` 的 backend capability 位（如 `supports_dense_4d_mask`）、`Attention._run_local_attention` 的 attn_mask 路由/回退，或模型（如 Qwen-Image-2.1 在 right-padded batch 下）构造 dense `[B, 1, S, S]` block-causal mask 后送入共享 attention layer 的路径。
- 强制：capability 必须显式声明并与 shipped backend 绑定——abstract 默认 `supports_dense_4d_mask() is False`，SDPA、cuDNN、Ascend `npu_fusion_attention`（即 NPU 上的 FlashAttention 分派）为 `True`。自动选择的 backend 声明不支持而本次 mask 为 dense 4D 时，仅把该次调用转发 `sdpa_fallback`（query/key/value/metadata 原样传递），不得持久切换 backend；回退须经 `warning_once` 发出去重告警报告该情形，不得每次调用重复输出；piecewise 路径保持不物化 mask。
- 强制：显式选择的 backend、active paged-KV、缺失 `sdpa_fallback` 三种情形必须 fail closed，抛 `NotImplementedError`（分别说明 "does not support a dense 4D attn_mask" / "paged_kv=True"），且 backend forward 与 fallback forward 都不得被调用。
- 禁止：让只接受 2D key-validity mask 的 backend（FlashAttention masked-varlen 断言 `attention_mask must be 2D`）在 kernel wrapper 深处 assert 崩溃并把 HTTP 500 暴露给请求；为自动选择加静默回退的同时放过显式选择或 paged KV 场景；把 capability 位写成与平台判定脱钩的常量使路由分支变成死代码；对 2D mask 或 capability=True 的 backend 多余改道。
- 验收：mock-backend 单测覆盖六条路径：自动选择+不支持 → 恰好一次 `sdpa_fallback` 且 backend forward 未被调用；显式选择、paged KV、无 fallback → `NotImplementedError` 且两个 forward 均未被调用；2D mask 与 capability=True → 走原 backend 不改道；并 monkeypatch `current_omni_platform.is_npu` 断言 `FlashAttentionBackend.supports_dense_4d_mask() is is_npu`，防止 capability 与平台分派脱钩。^[PR #9248]

<!-- kb:rule status=active since=v0.30.0 -->
