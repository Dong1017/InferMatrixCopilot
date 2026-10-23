---
title: "vllm-omni · Diffusion — Wan SDPA 对齐测试规则"
created: 2026-10-23
updated: 2026-10-23
type: rule
tags: [vllm-omni, components, diffusion]
sources: ["PR #9303"]
---

# vllm-omni · Diffusion — Wan SDPA 对齐测试规则

## VLLM-OMNI-PR9303-WAN-SDPA-MATH — Wan SDPA 对齐测试须在 MATH 后端下逐位比较并保留负向对照

- 触发：改动 `tests/diffusion/distributed/test_wan_attention_sdpa_alignment.py` 中对 `install_wan_attention_sdpa_alignment` 后前向与 `_reference_attention`（diffusers 0.40.0 数学参考）的取值比较——特别是 `torch.equal` 断言是否包在 `sdpa_kernel(SDPBackend.MATH)` 上下文中，或增删 `_math_sdpa` / `_assert_matches_unaligned_math` 辅助函数与 `test_math_reference_rejects_value_changing_alignment` 负向对照。
- 强制：所有“重排后前向与未对齐参考逐位相等”的断言必须在 `torch.nn.attention.sdpa_kernel(SDPBackend.MATH)` 上下文内执行两侧前向，且仍要求 `torch.equal` 精确相等。依据：ROCm 默认 SDPA 后端对错位的 1x1 query 与重打包副本不逐位一致（AMD build 14494 中 shards 3/5–5/5 失败）；测试要验证的是重排保值（realignment preserves values），不是厂商 kernel 的布局不变性，生产 kernel 不变。比较器必须保留负向对照：monkeypatch `wan_sdpa_alignment.align_sdpa_qkv` 令返回的 value 加 1 后，同一断言必须抛 AssertionError，证明精确比较器能拒绝改变取值的重排。对未验证 diffusers 版本的告警用例同样经 `_assert_matches_unaligned_math` 比较取值。
- 禁止：在默认（ROCm 实现相关的）SDPA 后端下直接做 `torch.equal` 取值断言；把断言放宽为近似容差（如 allclose）来绕开后端差异；删除 MATH 上下文、`_assert_matches_unaligned_math` 或篡改 value 的负向用例；把本规则解读为要求生产路径改用 MATH 后端——它只约束测试比较方式。
- 验收：`tests/diffusion/distributed/test_wan_attention_sdpa_alignment.py` 中取值断言均经 `_assert_matches_unaligned_math`（内部 `sdpa_kernel(SDPBackend.MATH)` + `torch.equal`）；`test_math_reference_rejects_value_changing_alignment` 存在并以 `pytest.raises(AssertionError)` 期望失败；`pytest tests/diffusion/distributed/test_wan_attention_sdpa_alignment.py` 通过（PR #9303 记录本地 32 passed；AMD READY diffusion 分片按 PR 计划在 CI 复核）。^[PR #9303]

<!-- kb:rule status=active since=v0.30.0 -->
