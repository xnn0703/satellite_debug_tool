# M45 Shell 终端有界消费与按帧投影开发记录

## 2026-10-28：范围与基线

- 基线代码：当前 master 工作树（1.8.0，含未提交的 M43 Shell 终端可靠性改动：`core/tcp_shell/terminal.py`、相关测试与文档）。
- 基线度量（两轮独立探针，offscreen，Python 3.13 / PySide6 6.11 / macOS）：
  - 空文档 1440 B/块（20 行）：~3.0–3.5 ms/块（~416–481 KB/s）；
  - 5000 行满载：~109 ms/块（~13 KB/s）；关闭光标/滚动同步无变化；仅 decoder+screen ~2.2 ms/块（~668 KB/s）；
  - profile：满载 ~94% 采样集中在 `QTextCursor.endEditBlock()`；
  - recorder JSONL 落盘 ~27 MB/s，非瓶颈。
- 已确认根因与相邻缺陷见 plan §1（无界 Qt 事件队列、undo 历史开启、controller 死信号）。
- 第三轮评审修正（2026-10-28）：读取统一 ≤ 64 KiB 且 residual 只收当前块后缀；清屏改代际划界（含 worker 在途字节）；持续速率改为注入期间实测消费速率 + 注入端实际发送进度双端度量；应用退出纳入异步收尾。事实修正：HEX 文档 undo 已由 maximumBlockCount 隐式关闭；ymodem_finished 仅限实际传输路径验收。
- 待验证假设（非承诺→已验证）：满载按帧投影达到 92,160 B/s 持续消费 ✅；交互延迟达标 ✅；0.6 MB/s 量级吞吐 ✅（实测可持续 ~756 KiB/s，超出假设）。

## 实现与 owner 核对（2026-10-28）

- `core/tcp_shell/rx_bridge.py`（新增，纯 Python 无 Qt）：有界段接收环。单生产者（worker）/单消费者（UI flush 帧）；容量 512 KiB、等待片段 ≤20 ms；入队/排空/通知边沿/清屏代际同一把锁协调；`push` 分段接纳返回 `(accepted, status, notify)`；`push_final` 非阻塞残余移交（≤一个读取块，移交后生产冻结）；`clear` 丢弃已入环字节并推进代际；`wake_stop` 供 `stop_and_join` 主动唤醒。
- `core/tcp_shell/transport.py`：新增 `MAX_READ_CHUNK = 65536`；TCP `recv` 与串口 `recv`（`min(in_waiting, MAX_READ_CHUNK)`）统一读取上限。
- `core/tcp_shell/worker.py`：`rx_payload(bytes)` 信号删除，改为 ring push + `rx_pending()` 边沿通知。`_shell_loop`：读取 → `_feed_ring`（RETRY 片段间 `_pump_commands_in_push`：发送立即执行、YMODEM 记待办回队、断开/清屏中断入环）→ `_handle_commands`。停止/退出统一 `_hand_over_residual`；命令式断开与泵内断开共用 `_disconnect_now`。每连接在 `_connect` 重建 ring（注入参数保留）。`submit_clear_rx`/`_ClearRxCommand` 新增。
- `core/tcp_shell/controller.py`：`rx_pending` 透传、`rx_ring`/`session_generation` 暴露、`clear_display()`；**缺陷 2 修复**——`disconnected`/`ymodem_finished` 私有槽先执行（租约/回读状态）再 signal-to-signal 直连转发；`disconnect_device` 把操作员原因传入 worker（修复断开原因丢失：原路径固定记 `worker-stop` 且证据状态恒 incomplete）。
- `ui/tcp_shell_workspace.py`：`_on_rx_payload`/`_consume_terminal_tokens` 删除。新链路：`_on_rx_pending`（33ms 单次定时器，起点间隔 ≥33ms）→ `_flush_frame`（16 KiB 预算、4 KiB 分段 drain、8ms 解析软预算、代际观察、每帧一次 HEX 追加/文本日志缓冲写/光标滚动条同步/backlog 指示）→ `_project_updates`（平移合并公式 + 单 edit block）。`feed_rx` 同步入口（分段接纳 + 先消费再接纳，不无界等待）。`_complete_final_drain` 排空后 `decoder.finish`；`shutdown` 顺序修订：controller 先收尾 → 泵事件分帧排空（≤10s 上限）→ 日志最后关。清屏：UI 立即清呈现 + `controller.clear_display()`，代际观察帧重置解码/模型。两文档创建即 `setUndoRedoEnabled(False)`（文档级）。
- **投影淘汰 owner（P0 升级 1）**：初版实现保留"每帧手动从头删除淘汰行"，满载实测 123ms/帧 → 可持续仅 ~24 KiB/s，验证门槛失败。归因实验：满载"有头删"123ms vs"仅尾部重写"6.5ms；头删成本与删除块数无关（8 块与 512 块均 ~160ms）；QPlainTextEdit 同样 ~170ms（控件方向排除）。改由 `maximumBlockCount(=模型 max_rows)` 拥有文档物理淘汰（Qt 内部实测 ~4-5ms/帧），投影删除全部手动头删路径；寻址偏移 `min(removed_total, rows_before)`；寻址越界时追加新块（修复首行并入末块的合并缺陷）。
- **帧预算（P0 升级 2）**：64 KiB 帧预算在积压下把单帧推到 ~141ms（键 p99 156ms 超标）。修订为 16 KiB/帧 + 4 KiB 分段，单帧回落至均值 21.2ms / 最大 34.8ms。

## 回归证据（2026-10-28）

- `test_shell_rx_bridge.py` 14 项：容量/分段/有界等待/唤醒/残余冻结/锁内边沿/生产消费竞争（流完整性 + 边沿状态）。
- `test_shell_rx_pipeline.py` 10 项：controller 三路径转发（自然断链/主动断开含原因/重连代际）+ YMODEM 取消路径转发；TCP 洪水停止保序（环满 + 残余，transcript 逐字节一致）；清屏代际端到端（跨清屏 UTF-8 分片不进入新屏幕）；帧合并坐标（多次淘汰/淘汰超旧文档/空与纯 SGR）；串口读取上限 + 串口 stub 停止回归。
- `test_shell_throughput_budget.py` 8 项（性能与专项）：持续 92,160 B/s（独立线程注入，消费 92,160 / 注入 93,082 B/s，积压峰值 10 KiB，停后归零）；突发 4×64KB；20B 小包；ANSI/≥4KB 长行/HEX/文本日志矩阵（83 KiB 0.25s）；512 KiB 宽松门槛 1.38s；洪水键→命令入队 p99 23.6ms（max 24.5）；洪水断开 45ms；满环关停分帧排空 300 KiB 0.78s。
- 既有套件迁移与回归：`test_tcp_shell_ui.py`（34 处）、`test_shell_terminal_reliability.py`（18 处）、`test_ui_color_contract.py`（1 处）`_on_rx_payload(` → `feed_rx(`；`test_tcp_shell_worker.py` 信号清单更新。Shell 相关全套件 149 项通过；全量回归见下。
- 三项度量（满载 5000 行，16 KiB 帧，offscreen）：解析均值 13.7 ms / 投影均值 7.5 ms / 整帧均值 21.2 ms（max 34.8 ms）→ 33ms 调度下可持续 ~756 KiB/s。

## 计划逐项 review

- §2A RxRing：已实现，14 项合同回归通过；读取上限合同进入 transport 并有串口 stub 回归。
- §2B 帧合并：公式实现并回归；投影淘汰 owner 按 P0 门槛升级为 maximumBlockCount（见实现记录，plan §2B 已同步修订）。
- §2C 终排空：停止残余移交、分帧排空、日志最后关闭已实现并回归；主窗口 closeEvent 全路径待人工验证（acceptance 未勾）。
- §2D feed_rx：分段接纳 + 共用实现，UI 线程不无界等待；真实信号链测试为必测项（pipeline/throughput 套件均为真 socket + 真 worker）。
- §2E 预算：16 KiB/4 KiB/8ms/33ms 冻结；验证门槛两次升级记录在案；三项度量已记录。
- §2F 代际与通知：rx_pending 边沿 + 会话代际 + 迟到通知丢弃；清屏代际端到端回归；controller 转发修复三路径回归。
- §2G undo：两文档显式关闭，持续投影后恒 False 回归。
- §2H 可见性与打包：backlog 指示（i18n 已入目录，1424 条全完成）；`USER_SHELL_MODULES` 含 `rx_bridge`。
- 非目标维持：QTextEdit/QPlainTextEdit 切换（已实测排除方向，无需执行）、HEX 延后投影（D2）、日志移线程（D3）、cell 去对象化（P2）。

## 源码运行

```bash
python3 -m satellite_debug_tool.main            # 运维版
python3 -m satellite_debug_tool.main_user       # 用户版
PYTHONPATH=. pytest satellite_debug_tool/tests  # 全量回归
```
