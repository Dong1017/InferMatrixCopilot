# tool_bridge.py —— 规范

<!-- verified-against: 2026-10-28 -->

`LOC ~783 · 面向 harness 会话的 scoped 工具 MCP server · refactor-status: ok`

## 职责
以 stdio MCP server 的形式，把本次 run 的 scoped 工具暴露给 harness 会话 ——
好让"把一个 step 委托给厂商 agent"**不等于把权限闸也一并委托出去**。

## 功能
`python -m infermatrix_copilot.tool_bridge --spec <bridge_spec.json>` ——
一个由 harness 从它的 MCP 配置里拉起的 server。它提供本次 run 的内置工具、
`doc_search`/`doc_read`、按需的 `repo_map`，以及只读的变更考古工具组
（`diff_stat`、`file_at_base`、`show_commit`、`search_history`、`calc`）。

## 公开契约
`write_bridge_spec(...)`、`load_bridge_spec(path)`、`make_dispatcher(...)`、
`build_server(spec_path, lifecycle=None)`、`run_bridge(spec_path) -> rc`、
`main(argv)`；存活面：`BridgeLifecycle(path).record(kind, **fields)`、
`lifecycle_log_path(spec_path)`、`read_lifecycle_events(spec_path)`、
`timed_call(call, lifecycle, slow_after_s)`、`_offload(fn)`、
`install_bridge_signal_dispositions()`、`SLOW_TOOL_CALL_S`（60 s）。

## 不变量（**C1**、**C2**、**E2**）
- **每一次内置工具调用都经过 `tools.dispatch`**，带着该 step 反序列化出来的
  `ToolScope`，因此拒绝行为、相对路径对 PR-time worktree 的解析、以及结果上限，
  都与进程内循环**表现一致**。
- **读取受容纳约束 —— 比进程内还严。** `ToolScope` 只对**写**做路径守卫；而 harness
  是持有不可信 PR diff 的**低信任调用方**，所以桥会拒绝容纳根（scope 根 + run 目录）
  之外的 read/list/grep 目标。**这就是防 `.env` 外泄的那道守卫。**
- **独立的 trace 文件。** 工具事件追加到 spec 旁边的 `bridge_trace.jsonl`；
  第二个进程**绝不能**与父进程的 `run_trace.jsonl` 交错。
- **`repo_map` 失败只降级，绝不崩溃** —— 记一条 `capability_gap`（`bridge.repo_map`）。
- **skill/memory 检索刻意不桥接。** 那两个工具会提出知识 candidate；
  开一条跨进程写入路径的方案**已被否决**。harness 会话可以**读**本仓库的知识，
  **永远不能往里加**。

## 边界 —— 不属于这里
不调用 transport（那归各 `providers/<id>.py`）；不构造 scope（那归 step）；
**永不写知识**。

## 依赖（允许）
stdlib + `mcp`（`[mcp]` extra）+ `..tools` + `..scopes` + `..run_trace` +
knowledge/repo-map 工厂。

## 测试
`test_tool_bridge.py`；`test_tool_bridge_lifecycle.py`（offload 保签名且在工作
线程跑、慢调用不阻塞并发调用（真 FastMCP server）、慢调用记录、真 stdio
进程忽略 SIGINT/SIGHUP 且 stdin 关闭后 rc 0 并记 `exit(stdin_closed)`、
`bridge_health` 判定矩阵、`_harness_attempt` 记 `bridge_unavailable`）。

## 重构备注
**并非每个后端都够得到这个 server**：`providers/deepseek.py` 跑在 harness 原生 bash 上，
因为它捆绑的运行时不带 MCP client，拿到无法履约的 spec 时会记一条 `capability_gap`。
任何"这次 run 走了工具桥"的说法，都必须**对照那个标志核实**，
而不是从"后端是 harness"推定。

## 向 harness 会话提供 rebase 工具面（2026-09-19）

bridge 把 run 的 20 工具面 + `doc_search`/`doc_read` 暴露给 harness 会话。
生成的 MCP 工具函数必须是 **keyword-only**（schema 会交错 required/optional），
零属性工具不能带裸 `*`，且必须**丢掉**未设置的 optional——传 None 会打断
每一次 `read_file`。失败的 bridged 调用记录失败**原因**。

## 自进化接入（2026-10-04）

bridge spec 序列化 `read_roots`、`deny_prefixes`、`strict_extras`、`executables`，并携带父进程 store 路径和单元上下文。`traced_call` 在每次工具调用时绑定上下文，使跨进程 trace 连续；不传递凭据。

## 宿主事实经 spec 进入桥进程（2026-10-27）

harness CLI 以白名单 env 启动，桥进程的 `os.environ` 里没有调度器授予本
run 的 `CUDA_VISIBLE_DEVICES`，也没有 `HF_HOME`——`_agent_shell_env` 与
`_target_test_env` 都从 `os.environ` 读它们，于是桥内 run_pytest/reproduce
看到的是整机 GPU。spec 的 `rebase.host_env`（仅这两个非机密键，str→str，
否则 `TypeError`）在重建后端前写进桥进程 env。`rebase.target_venv` 同理
携带已解析的目标 venv；凭据与完整子环境仍不进 spec。

## 存活：工具下线到工作线程 + 生命周期日志（2026-10-28）

run-20261027-193251：`infermatrix-tools` 在第一次 attempt 的中途"discovery
failed"（`record_debug_memory` 不可用），此前同一会话里已成功调用 27 次。
根因：桥里**每个**工具都是同步 handler，而 FastMCP 对同步 handler 是在事件
循环上**内联**调用的（`call_fn_with_arg_validation` 直接 `fn(**args)`），所以
一次 `request_plan_review`（上限 `strict_backend_timeout_s`=1800 s）或 600 s 的
`run_shell` 会让整个 server 在那几分钟里不应答 ping、不应答任何其他调用；
cursor-agent 的每调用截止先到期，待排队的调用在它返回时集中冲刷，随后
client 把 server 判为失败，余下数小时不再路由到它。桥进程本身是否死掉，
以前没有任何记录可查。

- **所有** handler 经 `_offload` 注册为 `async def` 包装（`functools.wraps`
  保留原签名——FastMCP 由签名推 input schema——内部
  `anyio.to_thread.run_sync`），事件循环在长调用期间保持响应；
  `test_slow_tool_does_not_block_a_concurrent_call` 用真 server 证明。
- `run_bridge` 为每个 spec 写 `<run_dir>/bridge/<stem>.lifecycle.jsonl`
  （与 `bridge_trace.jsonl` 分离：后者记工具调用、全 run 共享；前者记**这个
  进程**）：`start`（pid/ppid/sid/pgid/spec/argv）、`slow_tool`
  （≥ `SLOW_TOOL_CALL_S` 的调用：tool/elapsed_s，是 client 截止切断的唯一旁证）、
  `exit`（`reason` ∈ stdin_closed / signal / system_exit / exception /
  import_error / build_error，`rc`、`calls`、`parent_alive`）。写失败绝不抛。
- 进程对 SIGINT/SIGHUP/SIGPIPE 置 `SIG_IGN`（`install_bridge_signal_dispositions`，
  在 `run_bridge` **最先**执行——SDK 导入在高负载宿主上要数秒，期间到达的信号
  不得先杀掉还没开始的 server）：stdio server 的寿命就是它的 stdin，终端 Ctrl-C
  只应到引擎、由引擎按序停会话；harness 关闭 stdin 是唯一正常退出。
  与 `mcp_server.py`/`thin_mcp_server.py` 的 SIGINT 忽略同一原则。
- 消费方：`rebase_engine/module_rebase.py::bridge_health` 在会话结束后读该
  日志并分类（见 rebase_engine 规范），非 `ok` 时记 `bridge_unavailable`。
