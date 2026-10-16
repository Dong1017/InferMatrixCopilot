---
title: "vllm-omni · Scheduler · Prefix-cache write-boundary rules"
created: 2026-10-13
updated: 2026-10-13
type: rule
tags: [vllm-omni, components, scheduler]
sources: ["PR #8835"]
---

# vllm-omni · Scheduler · Prefix-cache write-boundary rules

## VLLM-OMNI-PR8835-WRITE-BOUNDARY — 前缀缓存写边界：写任务构造与资源管理收敛到 controller 拥有的抽象

- 触发：改动 `vllm_omni/core/prefix_cache/manager.py` 与 `vllm_omni/core/prefix_cache/controller.py` 之间写任务构造、预算票据与 staging 资源管理的职责划分，或 `tests/core/test_prefix_cache.py` 中依赖 `StepWriteContext`、`WriteTaskHandle`、`StepSnapshot`（`d2h_snapshot`）、`lookup`/`fetch_host`、`_tasks`/`_dispatch`、`_staged_bytes` 的断言。
- 强制：manager 不得自行构造写任务或直接操纵 controller 内部状态，写路径必须经 controller 拥有的抽象：`StepWriteContext` 拥有每步注册与共享预留，manager 在发布自身状态时注册或追加请求行，并在释放状态锁之后才调用 `start_writes()`（急切拷贝保持在状态锁之外）；`WriteTaskHandle` 只提供任务元数据、不暴露可变任务内部，并在 controller 簿记排空后仍保留对 deferred 行的访问；`StepSnapshot`（step context 的 `d2h_snapshot`）封装暂存 host 行、拷贝完成（`copy_to_cpu()`）与该步 staging hold 的释放。保存失败语义：写入注册后保存失败时，`StepWriteContext.__exit__` 使该步触及的所有任务失败，包括仅被本步追加的更早步骤 deferred 任务（其早期行被丢弃），下一次 facade 入口经 `_fatal_write_failure` 抛出，而不是让已注册未派发的任务之后以 join 超时浮现；该语义由 `test_manager_partial_publish_failure_releases_resources` 回归守护。未消费 step context 超时清理后 controller 暂存字节必须归零（`_staged_bytes == 0`）。
- 禁止：把写任务构造、票据操纵或 staging 协调逻辑移回 manager 内联实现；在 manager 仍持有状态锁时调用 `start_writes()` 派发写；让 `WriteTaskHandle` 暴露可变任务内部或令 deferred 行在 controller 簿记排空后不可访问；删除部分发布失败释放资源或 `_staged_bytes` 清零的回归断言。
- 验收：`python -m pytest -o addopts='' -p no:cacheprovider -q --confcutdir=tests/core tests/core/test_prefix_cache.py tests/core/test_prefix_cache_runner_mixin.py tests/platforms/npu/test_npu_prefix_cache_wiring.py` 通过，且套件中保留 `test_manager_partial_publish_failure_releases_resources`。^[PR #8835]

<!-- kb:rule status=active since=v0.30.0 -->
