# M45 Shell 终端有界消费与按帧投影计划

日期：2026-10-28。两轮评审已确认：性能问题与解耦方向成立（第一轮），六项合同修订与 P0/P1/P2 边界成立（第二轮）。本计划把已确认的合同具体化；实现基于当前未提交的 M43 工作树。交付方式为源码修复 + 运行命令；打包与发版另行确认。

## 1. 事实与根因

- 满载微基准（offscreen，1440 B/块，20 行/块）：空文档阶段 ~3 ms/块（~0.4 MB/s）；5000 行满载后 ~109 ms/块（~13 KB/s）；满载仅模型层 ~2.2 ms/块（~0.6 MB/s）；profile ~94% 采样集中在 `QTextCursor.endEditBlock()`。已确认的慢路径是"满载状态下的文档提交及其触发工作"；内部成因（块重索引/布局/信号级联）未隔离，不预设结论。
- worker 读循环与 UI 消费完全解耦，逐块 emit `rx_payload`；积压点为无界 Qt 事件队列，TCP 侧无背压，串口未配置流控。
- 相邻缺陷 1：`TerminalEdit.__init__` 的 widget 级 `setUndoRedoEnabled(False)` 对 `_build_ui` 随后 `setDocument()` 换入的文档无效。探针复核：正文文档 undo 历史开启（`isUndoAvailable() == True`）；HEX 文档因已设 `setMaximumBlockCount(5000)` 被隐式关闭。两份文档显式关闭 undo 的实施要求保持。
- 相邻缺陷 2：`controller.py` 将 worker `disconnected`/`ymodem_finished` 只连接到私有槽，controller 同名信号从未发射；UI 订阅死信号，真实断链时 `decoder.finish()` 收尾、断开事件与 YMODEM 完成文案不触发。
- HEX 开启时正文文档仍全量更新（双文档成本）；`ShellLogWriter` 在 UI 线程逐包 write+flush。两者 P1 处理。

## 2. 合同与实现范围（P0）

**A. RxRing（新模块 `core/tcp_shell/rx_bridge.py`，纯 Python，无 Qt）**
- 有界段缓冲 + `threading.Condition`；容量 B = 512 KiB（实测后冻结）；入队、排空、pending 标志在同一把锁内协调；pending 边沿只在"非 pending → 有数据"翻转时置位，杜绝漏唤醒。
- `push(data)`：常规路径以有界等待片段（≤ 20 ms）循环等待容量，每片段返回调用方处理停止与命令；单次超容量输入分段接纳，不整块拒绝，返回已接纳字节数与完成状态。
- 读取合同：TCP 与串口每次读取统一限定 ≤ 64 KiB（`MAX_READ_CHUNK`），worker 任一时刻最多持有一个未入环的读取块；串口按 `min(in_waiting, MAX_READ_CHUNK)` 分批读取。
- `push_final(data)`：停止后的已读残余移交，非阻塞、不占容量预算，只接收当前读取块尚未入环的后缀；移交后禁止继续生产（不再读取、不再入环）。`drain(max_bytes)` 先消费环内字节再消费 residual。
- 每次连接一个 bridge，由该连接的 worker 独占创建持有；`stop_and_join()` 调用 `wake_stop()` 主动唤醒容量等待。
- worker 在满载等待片段间处理发送与停止命令；`_StartYmodemCommand` 不在 push 等待栈内执行，记为待办、由外层循环调度。YMODEM 二进制 recv 不经 ring（既有合同）；recorder 全量记录不变。
- 清屏合同（以清屏代际划界）：UI 立即清呈现；清屏命令由 worker 按命令序处理——丢弃手中已读未入环后缀，并在 bridge 锁内清空环内容、推进清屏代际；UI drain 观察到代际推进时重置 decoder 与模型，边界前的在途字节（含已入环与 worker 手中）不再进入新屏幕。跨清屏的 UTF-8/CSI 分片从边界后首字节重新解码，遗留续片按无效序列呈现；transcript 保持全量。边界之"新"仅指应用确定的接收边界，不代表设备实际产生时间。
- 串口现场边界：环有界仅约束应用内存；无流控下暂停读取仍可能驱动或设备侧溢出，dev_log 明示实测结果。

**B. 帧合并坐标合同（不引入绝对行号）**
- `TerminalUpdate.first_row` 属于该次 feed 淘汰之后的坐标系。帧内多个更新合并：`dirty_first = min(max(0, dirty_first - update.removed_rows), update.first_row)`、`removed_total += update.removed_rows`（先把已有脏位置平移过本次淘汰，再吸收新更新）。
- 投影淘汰 owner（实现修订，P0 验证门槛触发）：语义淘汰 owner 是 `TerminalScreen`；文档**物理**淘汰由 `maximumBlockCount(=模型 max_rows)` 拥有——手动从头删除是 O(文档) 且与删除块数无关（满载实测删 8 块与 512 块均 ~160ms；Qt 内部淘汰 ~4-5ms/帧），该手动路径已删除。投影只做"从 `dirty_first + offset` 重写至末尾 + 追加"；寻址偏移 `offset = min(removed_total, rows_before)`，只计帧前已存在的被淘汰行（帧内先创建后被淘汰的行从未进入文档）；寻址越界（帧内淘汰超过旧文档内容）时追加新块而非并入末块。
- 回归覆盖：帧内多次淘汰、单帧淘汰超过旧文档行数、空 tokens、纯 SGR 变化、跨包同行覆盖。

**C. 终排空与关停顺序（冻结）**
```
UI 请求停止并 wake_stop
→ worker 将已读未入环残余 push_final 移交 → worker 退出（不在满环上阻塞）
→ UI 分帧排空 ring + residual（每帧预算内，可跨多帧，呈现"收尾中 N KB"）
→ decoder.finish() → 末帧投影 + 文本日志全量 flush
→ 关闭文本日志 → 解绑 worker、释放会话与 bridge
```
- `workspace.shutdown()` 顺序修订：controller 先收尾（含残余移交），文本日志最后关闭（现状顺序相反）。
- 终排空禁止单次主线程回调清空全部积压；dialog 与 workspace 的销毁推迟到排空完成。
- 关闭 Shell 弹窗与关闭整个应用进入同一异步收尾状态机：主窗口 `closeEvent` 泵事件驱动收尾帧，完成末帧投影与文本日志关闭后才允许最终退出；事件循环结束后不依赖 QTimer 继续排空。满环状态下关闭主窗口配专项回归。

**D. 同步入口 `feed_rx(data)`**
- 分段接纳；接纳前 ring 达高水位则先执行一次预算内消费再继续接纳；完成该次输入的消费与投影后返回；与真实路径共用解码、模型更新与投影实现。
- UI 线程不对满环做无界等待。真实 worker 与真实信号链测试为验收必测项；同步入口只是确定性辅助，不得掩盖线程与通知缺陷。

**E. 预算与调度语义（初值按实现修订冻结）**
- 8 ms 是解析分段之间检查的软预算（不切断 UTF-8/CSI 跨界状态），不约束 `endEditBlock`；帧字节预算 P = 16 KiB（对 92,160 B/s 目标 5 倍余量），drain 按 4 KiB 分段取用（初值 64 KiB 预算实测把单帧回调推到 ~141ms，已修订）。
- 33 ms 是帧起点间隔：帧回调完成后重排单次定时器，相邻帧起点间隔 ≥ 33 ms，无重入。
- 帧回调总耗时为验收目标：解析/投影/整回调三项度量记入 dev_log；实测满载均值 21.2 ms、最大 34.8 ms（退化上限 150 ms 内）。
- 验证门槛结论：初版帧投影实测仅 ~24 KiB/s 不达标，触发 P0 升级两次——归因"手动头删 O(文档)"并移交 `maximumBlockCount`；64 KiB 帧预算产生 ~141ms 长帧，修订为 16 KiB/4 KiB 分段。终测：持续消费 92,160 B/s（积压峰值 ~10 KiB）、洪水中键→命令入队 p99 ~24ms、可持续 ~756 KiB/s。控件对比（QPlainTextEdit）经实测排除：其头删成本同为 ~170ms，非瓶颈方向。

**F. 代际与通知合同**
- `rx_pending` 通知与会话代际绑定：workspace 在连接建立时绑定 (bridge, generation)，flush 帧先验证代际；迟到的旧 pending 与旧 disconnected 一律丢弃；重连前旧 bridge 按合同 C 完成收尾。
- pending 竞争（生产与消费交错于末次 drain 附近）配专项回归。
- controller 信号修复：`disconnected`/`ymodem_finished` 改为 signal-to-signal 直连转发，私有槽先完成租约释放与回读状态更新，再向外通知。断开通知与 YMODEM 通知分别验收：无 YMODEM 事务的断链只要求 `disconnected` 送达（自然断链、主动断开、重连三路径）；`ymodem_finished` 仅在实际传输的完成、取消、失败路径要求送达。

**G. undo（P0）**
- 两份文档创建即 `setUndoRedoEnabled(False)`（文档级开关；widget 级对后换入文档无效）。回归断言持续投影后 `isUndoAvailable()` 恒为 False。

**H. 可见性与打包**
- ring 积压超阈值时终端头部显示积压 KB 数（i18n 新文案，TS/QM 同步提交）。`scripts/edition_policy.py` 的 `USER_SHELL_MODULES` 增 `rx_bridge`。

非目标：淘汰 owner 移交文档 maximumBlockCount（需绝对行号改造，P1 评估）；QTextEdit/QPlainTextEdit 切换（P1 实测对比）；HEX 在位时正文延后投影（P1）；`ShellLogWriter` 移出 UI 线程（P1，P0 先做到每帧缓冲写一次）；cell 模型去对象化（P2）。

## 3. 工作顺序

- 本计划与 acceptance 经用户确认后，RxRing 单元测试先行：容量、分段接纳、有界等待、wake_stop、residual、锁内边沿、生产消费竞争。
- worker 与 controller 改造，含信号转发修复与三路径测试。
- workspace 按帧投影、`feed_rx`、关停顺序、清屏合同；现有 53 处测试对 `_on_rx_payload(` 的直接调用迁移为 `feed_rx(`；M43 全部用例保持通过。
- 性能、端到端与专项回归，三项度量记入 dev_log；AGENTS.md、翻译、edition_policy、全量回归与源码运行命令收尾。

## 4. 验证与现场边界

软件验证：pytest 全量（含新增合同回归、负载矩阵、独立线程注入的端到端回环）；30 分钟 soak、真机 921600 串口洪水与设备 TCP 洪水为现场人工验收，结果记入 dev_log。Windows 冻结烟测不触 rx 路径（已核对 bootstrap 断言），不受影响。吞吐与交互指标以 acceptance 文档冻结值为准。
