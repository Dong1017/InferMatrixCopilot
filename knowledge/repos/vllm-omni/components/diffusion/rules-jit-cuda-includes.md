---
title: "Diffusion JIT CUDA 构建 include 顺序规则"
created: 2026-10-11
updated: 2026-10-11
type: rule
tags: [vllm-omni, components, diffusion]
sources: ["PR #7599"]
---

# Diffusion JIT CUDA 构建 include 顺序规则

## VLLM-OMNI-PR7599-JIT-CUDA-ISYSTEM — JIT CUDA 扩展的 vendor 头文件必须以 -isystem 追加在 toolkit 之后

- 触发：修改 `vllm_omni/diffusion/distributed/a2a_permute.py` 的 `_ensure_built()` JIT load（`extra_include_paths`/`extra_cflags`/`extra_cuda_cflags`）、NVIDIA wheel 头文件目录的传递方式，或任何经 PyTorch `cpp_extension.load()` 构建 CUDA 扩展时的 include flag 顺序。
- 强制：NVIDIA wheel include 目录（如 `nvidia/cu13/include`、`nvidia/nccl/include`）必须以 `shlex.quote` 包裹的 `-isystem <path>` 追加到 `extra_cflags` 与 `extra_cuda_cflags` 末尾，位于 PyTorch 为选定 toolkit 注入的 `-isystem` 默认值之后；`extra_include_paths` 保持为空。NCCL 库与 `nccl.h` 的存在性检查及 `extra_ldflags` 不变，wheel 路径继续为 toolkit 中缺失的头文件（cuSPARSE、NCCL）提供回退。
- 禁止：把 wheel 目录经 `extra_include_paths` 作为 `-I` 传入——`-I` 优先于 `-isystem`，混合安装下 wheel CUDA 头会覆盖选定 nvcc 的配套头并在生成的 launch stub 中触发不兼容的 `__cudaLaunch` 宏编译错误；把 toolkit 目录加到 `-I` 列表前端当作修复——同时出现在 `-isystem` 的目录仍被编译器按系统目录处理，wheel `-I` 条目仍在其前；用 `CUDA_HOME` 环境覆盖或整体移除 cu13 wheel 包替代该方案。
- 验收：参数化测试同时覆盖 toolkit 提供 cuSPARSE 与 wheel-only 两种 fixture，断言 `extra_include_paths` 为空、每个 vendor `-isystem` flag 经 `shlex.split` 解析后同时出现在 host 与 CUDA flags、`cuda_runtime.h` 选中 toolkit 版本、`cusparse.h` 按 toolkit→wheel 顺序回退、`extra_ldflags` 仍指向 NCCL 库。真实硬件证据仅覆盖 H20/nvcc 13.0.88/PyTorch 2.13.0+cu130/wheel 13.4.59 上的扩展编译加载与 CPU fixture 测试，不得据此声称新的分布式吞吐或 H3 质量结论。^[PR #7599]

<!-- kb:rule status=active since=v0.30.0 -->
