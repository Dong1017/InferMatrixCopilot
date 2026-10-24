---
title: "Host Weight Runtime 测试评审规则"
created: 2026-10-24
updated: 2026-10-24
type: rule
tags: [vllm-omni, components, model-executor]
sources: ["PR #9339"]
---

# Host Weight Runtime 测试评审规则

## VLLM-OMNI-PR9339-WAITER-EXIT — Host Weight Runtime 等待进程收尾与 0.1 秒域锁协调期限分离：60 秒完成 / 5 秒清理双上限

- 触发：改动 `tests/host_weight_runtime/test_runtime_resolution.py` 中 `test_domain_lock_timeout_and_fresh_runtime_recovery` 的子进程收尾结构：`_PROCESS_COMPLETION_TIMEOUT_SECONDS`（60 秒）、`_PROCESS_CLEANUP_TIMEOUT_SECONDS`（5 秒）、`_CONTROLLED_FINALIZATION_DELAY_SECONDS`（8 秒）三个常量，`_initialize_contended_runtime` 的 `finalizing` 握手与受控收尾延迟参数，或正常完成与强制清理两条 `join` 路径的超时取值与断言。本规则只覆盖该测试文件的结构契约；Host Weight Runtime 本体行为不在范围内（PR #9339 为 test-only 改动）。
- 强制：子进程完成等待必须与 0.1 秒域锁协调期限解耦——锁超时及其时序断言保持 0.1 秒口径不变。正常完成路径：先 `reader.poll(5)` 等到子进程发来的 `finalizing` 握手并 `recv` 校验，再以 60 秒完成上限 `join`（与其他 filesystem-store 多进程测试一致），`exitcode == 0` 断言的失败信息须含 pid 与 exitcode。强制清理路径保持有界：`kill` 后以 5 秒上限 `join`，并显式断言 `not process.is_alive()`（无存活子进程）。受控收尾延迟 8 秒必须大于旧的 5 秒完成等待、又远低于 60 秒完成上限，使两个参数化用例都真正覆盖“收尾慢于 5 秒”的场景（此前 AMD post-merge build #14579 即因 `join(5)` 到期、exitcode=None 而在功能断言全过后假失败）。
- 禁止：把正常完成的 join 期限改回 5 秒，或将其与 0.1 秒协调期限重新耦合，使重载 runner 上子进程收尾超过 5 秒时产生功能断言全部通过后的假失败；删除 `finalizing` 握手 poll、pid/exitcode 失败信息或清理后的无存活断言；把受控延迟调到 ≤5 秒（不再触发回归路径）或逼近/超过 60 秒完成上限；以收紧测试超时为名改动 0.1 秒锁超时语义。
- 验收：该测试正常完成路径以 `_PROCESS_COMPLETION_TIMEOUT_SECONDS` join 并断言 `exitcode == 0`（失败信息含 pid 与 exitcode）；清理路径以 `_PROCESS_CLEANUP_TIMEOUT_SECONDS` join 后断言无存活子进程；0.1 秒锁超时相关断言未变；`_CONTROLLED_FINALIZATION_DELAY_SECONDS` 严格介于 5 秒与 60 秒之间。^[PR #9339]

<!-- kb:rule status=active since=v0.30.0 -->
