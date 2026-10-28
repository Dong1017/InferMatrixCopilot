# engine/lifecycle.py —— 规范

<!-- verified-against: 2026-10-28 -->

`LOC ~396 · 引擎底座（run 生命周期原语 + 优雅停机） · refactor-status: ok`

## 职责
executor 自己给不了的三条**进程级**保证：同一 run 的互斥锁，run 离开事件
循环时的 finalizer 挂钩点，以及 SIGINT/SIGTERM 到达时**有界的优雅停机**
（run-20261027-193251：引擎从 `nohup … &` 启动器继承了 SIGINT 的 `SIG_IGN`，
GPU 调度器 `--timeout` 发出的 SIGINT 被整个忽略，随后 reservation guard 逐个
杀掉其下的测试进程）。

## 功能
`RunLock` 对 `<run_dir>/.lock` 取排他 advisory `flock`（第二个并发
`--resume` fail fast，而不是交错写进度、共享 checkout）；
`register_finalizer`/`finalize` 维护逐 run 的异步 finalizer 表；
`run_guarded` 包住 executor 协程，在完成/失败/抛异常的**每条**退出路径上、
同一事件循环内执行 finalize，并把自己的 task 绑定给停机路径
（`bind_run_task`），退出时解绑。

停机：`install_run_signal_handlers(run_dir, run_id) -> restorer` 为 SIGINT/
SIGTERM 安装处理器（**无条件** `signal.signal`——继承来的 `SIG_IGN` 正是要
覆盖的状态；`asyncio.Runner` 只在默认处理器仍在时才装自己的，所以本处理器
在 `asyncio.run` 内保持权威）；处理器调用 `request_shutdown(signum, …)`
（幂等：第一次接受，之后只计 `repeats`），它启动守护线程 `imx-shutdown`
并对绑定的 run task 调 `cancel`。工作线程按序：`terminate_children`
（对所有已注册 harness 子进程 `kill_tree`，TERM→宽限→KILL，含其 MCP bridge
后代）→ `cleanup_run_processes(run_dir, run_id)`（带 `IMX_RUN_ID`+`IMX_LOG_DIR`
标记的后台链，含 detached 测试链）→ 注册的 `shutdown hooks`（模块循环用来
把在飞模块标 `interrupted`）→ 等主线程正常退出事件循环；超过
`SHUTDOWN_HARD_DEADLINE_S`（60 s）则再杀一次子进程并 `exit_fn(128+signum)`
（默认 `os._exit`）。

## 公开契约
`RunLock(run_dir).acquire()/release()`（context manager）；`RunLockHeld`；
`register_finalizer(run_dir, fn)`（`fn(outcome)`，outcome 是 RunOutcome，或
executor 在产出之前就抛时为 None）；`finalize(run_dir, outcome)`；
`run_guarded(run, run_dir) -> outcome`；`require_file_locking()` 与
`FileLockingUnavailable`（POSIX advisory 锁不可用时抛出的 fail-closed 门）。
停机面：`ShutdownRequest(signum, run_dir, run_id, requested_at, repeats)`
（`signal_name`、`exit_code = 128 + signum`：SIGINT→130、SIGTERM→143）；
`shutdown_requested() -> ShutdownRequest | None`（协作式轮询点）；
`register_child(proc)/unregister_child(proc)/registered_children()`
（transport 在一次 harness 调用期间注册 Popen，见 `providers/base.py::
run_harness_cli`）；`register_shutdown_hook(fn)/unregister_shutdown_hook(fn)`
（同步、幂等、不得抛）；`terminate_children(grace_s)`；
`request_shutdown(signum, *, run_dir, run_id, hard_deadline_s, grace_s, exit_fn)`；
`install_run_signal_handlers(run_dir, run_id, *, signals) -> restorer`
（仅主线程；其他线程返回 no-op restorer）；`_reset_shutdown_state_for_tests()`。
调用方：`app/workflow_execution.py` 在 run/resume 全程持锁、在
`asyncio.run` 外侧安装/恢复信号处理器并用 `run_guarded` 包 `executor.run`，
捕获 `CancelledError`/`KeyboardInterrupt` 后若 `shutdown_requested()` 非空则走
`_interrupted` 终局协议（run_status `interrupted`、`run_interrupted` trace、
退出码 128+signum）；`rebase_engine/module_rebase.py` 在每次 attempt 后轮询
`shutdown_requested()` 并注册在飞钩子；finalizer 注册者是 rebase 流水线（flock 释放、scratch 清理、
终局报告、CI abort 清理）与 `engine/worktrees.py` 的共享持有释放；
`require_file_locking` 由 `engine/worktrees.py::materialize` 和
`idempotency.py`（`key_lock`/`reap_stale`）消费。

## 不变量
- `flock` 争用按 open file description 计：同进程内对同一路径的第二次
  `acquire` 也失败 —— 抛 `RunLockHeld`，绝不静默共存。
- 锁文件释放后**留在原地**：它的存在不携带语义，只有 flock 本身算数。
- 非 POSIX（无 fcntl，与 `run_status.py` 同款守卫）：无 advisory 锁，
  `acquire` 直接成功 —— 注释声明的刻意降级（保住 run 可用；无 trace 事件）。
- **降级与 fail-closed 是两条刻意分开的路径**：`RunLock` 的静默降级只对
  "单进程 CLI run 可用性"成立；对**正确性就是锁本身**的路径（worktree
  materialize、idempotency 索引与 reaper），`require_file_locking()` 直接抛
  `FileLockingUnavailable` —— 宁可拒绝，也不在无保护下运行并谎报保证
  （不变量 7）。`contract.capabilities()` 会如实上报
  `supports_file_locking=False`。
- finalizer **恰好一次**：`finalize` 先 pop 再跑，第二次调用是 no-op；
  按注册顺序执行。
- 一个抛异常的 finalizer（**含 `CancelledError`** —— BaseException，不点名
  就会漏）绝不掩盖 run 自身的 outcome、绝不阻断兄弟 finalizer。
- `run_guarded` 的 teardown **不可被取消丢弃**：shield + 循环 re-await 直到
  finalize 真正完成，然后才让取消向外传播 —— 光 shield 会让外层先抛、
  loop 关闭时把孤儿 finalizer 拦腰取消。
- 什么都没注册时整个挂钩点是 no-op（现有非 rebase playbook 行为不变）。
- 停机**恰好一次**：第二个信号（任一种）只计 `repeats`，不重启序列、不改
  退出码；信号处理器只做"记状态 + 起线程 + `call_soon_threadsafe(cancel)`"，
  不在处理器内做 I/O 等待。
- 协作式与强制式两条路径留下**同一份**持久痕迹：模块 `interrupted`、run
  `interrupted`——前者由步骤自己在看到请求后返回 BLOCKED，后者由取消穿过
  finalizer（GPU 锁释放、scratch 保留）后在 `workflow_execution` 收口。
- 硬截止是兜底不是常态：主线程正常退出事件循环（`_run_loop is None`）时
  工作线程直接返回，由主线程写终局并返回退出码；只有卡死的 executor 才会
  被 `os._exit(128+signum)` 终结。
- 子进程注册面向 **cursor-agent/claude/codex 整棵树**：`subprocess.run` 隐藏
  Popen，所以 transport 必须经 `run_harness_cli` 注册；`terminate_children`
  用 `kill_tree` 走后代，bridge 不会被 reparent 到 init 后继续持有 run 的
  工具作用域。

## 边界 —— 不属于这里
不含 teardown 的内容本身（各注册方自带）；不做进度/checkpoint 持久化
（executor）；不是 run 状态查询（`run_status.py`）；不含 step 逻辑。

## 依赖（允许）
stdlib（asyncio/os/signal/subprocess/threading/pathlib/fcntl-守卫）；停机
工作线程**延迟导入** `testing/process_tree.py::kill_tree` 与
`rebase_engine/processes.py::cleanup_run_processes`。零 engine 内 import。

## 扩展点
新的 run 级资源清理 = 调用方去 `register_finalizer`，不改这里；只有新的
**进程级** run 保证才落在这里。

## 测试
`test_run_lifecycle.py`（同进程/跨进程锁争用、release 后可重取、无 fcntl
降级、finalizer 恰好一次 / 异常与取消隔离 / 失败路径也执行）；
`test_run_shutdown.py`（覆盖继承的 SIG_IGN 并恢复、请求幂等与 128+signum、
工作线程杀注册子进程整棵树后在硬截止 `exit_fn`、无绑定 task 时跑钩子即返回、
`run_guarded` 绑定/解绑与取消仍 finalize、`run_harness_cli` 注册/超时杀树、
模块协作式/取消式 `interrupted`、步骤 BLOCKED、wave gate、resume 重跑）。

## 重构备注
小而承重。`_finalizers` 是模块级进程状态 —— pop-before-run 就是它的自清
机制；若将来单进程要并行多 run，把表挂到 run 对象而不是模块全局。
`run_guarded` 的 shield 循环微妙（取消期间的 teardown 存活是评审加固过的）
—— 改动前先重跑取消路径的测试。停机状态（`_shutdown`/`_children`/
`_shutdown_hooks`/`_run_loop`）同样是模块级进程状态，与"一进程一 run"的
前提绑定；测试经 `_reset_shutdown_state_for_tests()` 清场。
