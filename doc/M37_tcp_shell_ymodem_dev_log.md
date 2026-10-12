# M37 TCP Shell + YMODEM 三工作区弹窗开发记录

## 2026-10-11：实施

- 用户确认方案：TCP Shell 弹窗在 Customer / Engineering / Production 三个工作区都有入口并复用
  同一实例（摇摆台弹窗模式）；YMODEM 准入采用"有会话则持锁、无会话允许直传"；版本回读
  首版包含最小显示（呈现事实，不自动判 PASS）。
- 设备端合同从固件仓库核实（`ymodem_port.c` / `ymodem.c`）：端口默认 1235、`rb` 切换
  YMODEM、头包被拒不回 NAK 而重发 `C`、abort 发单个 `CAN`、EOT#2 后先落盘再 ACK 且
  尾随 `C` 在下一轮询超时才发出、文件名路由 DOWNLOAD/FACTORY/LittleFS/跨产品拒绝。

## 实施

- 新增 `core/tcp_shell/` 四模块：
  - `ymodem_sender.py`：纯状态机。`_ControlReader` 只认协议控制字节并跳过 shell 回显
    噪声；任何 `CAN` 立即取消；帧构造 STX/1024 + 序号补码 + CRC16-CCITT 大端；末块
    0x1A 填充；头包 `filename\0size\0`；批结束空文件名帧。
  - `worker.py`：`TcpShellWorker(QThread)` 唯一 socket 读写 owner；`rb\r` 后 0.4s echo
    drain（对齐固件 `C` ≥2s 延迟合同）；YMODEM 期间字节直达 sender 不经终端文本解码；
    recorder 全部收发字节进 transcript。
  - `controller.py`：准入（attachment 网关 `tcp-shell-ymodem` / gate 空闲 / 未管理直传
    三态 + busy 拒绝）；完成后订阅 Runtime 握手 `MetaInfo` 做最小版本回读。
  - `recorder.py`：`tcp_shell_sessions/<id>/` 六文件证据，`.tmp`+原子 rename，ENGINEERING_ONLY。
- 新增 `ui/tcp_shell_workspace.py` + `ui/customer_tcp_shell_dialog.py`；三入口：
  Customer 侧栏（摇摆台按钮之后）、Engineering 顶栏 ghost 按钮（仅工程工作区可见）、
  Production 子页导航按钮（`open_tcp_shell` 回调注入）；`CustomerWorkspace` 持缓存实例，
  弹窗跨工作区切换存活，关闭走 `confirm_shutdown`（传输中提示 CAN CAN 取消）。
- 终端交互（按用户验收意见从"独立输入框"改为哑终端）：`TerminalEdit(QTextEdit)` 只读
  显示 + 按键拦截——可打印字符 UTF-8 直发、回车 `\r`、退格 `0x08`、↑↓←→/Delete/Home/End
  发 ANSI/emacs 序列（Letter Shell 设备端行编辑与历史）、Ctrl+C 无选中发 `0x03` 中断、
  有选中保留本地复制、Ctrl+U/K/L/D、粘贴文本直发。渲染：增量 UTF-8、`\b` 退格、`\r`
  行覆盖（KeepAnchor 选区替换）、ANSI 剥离、hex 视图。
- `Settings` 记忆上次目标（`tcp_shell.last_host/last_port`）；无设置弹窗项。
- AGENTS.md 更新包结构、工作区、约定、持久化路径与文档索引。

## 验证记录

- YMODEM sender 纯状态机：14 项（含 CRC 已知向量 0x31C3、头包 C 重发、NAK 重传、
  重传上限、设备/操作员取消、EOT NAK 合同、分片/粘包、噪声字节跳过）。
- worker 端到端（真 socket + 假 AFD01 设备，镜像固件接收时序）：5 项（shell 收发、
  连接失败、完整分片传输 2500B、取消发 CAN CAN 且保持连接、证据文件与 incomplete 收尾）。
- controller 准入与回读：10 项（三态准入、busy 拒绝、版本解析、路由提示、回读门控）。
- UI：14 项（弹窗缓存复用、三入口、终端解码、按键映射合同、Ctrl+C 本地复制、原始字节转发、控件状态、关闭语义、Production 回调）。
- 翻译：`update` + `check` 通过，`1406 messages`、`0 unfinished`。
- 离屏渲染：1024×640 与 1440×900 检查通过，四个分区完整、无截断重叠。
- 全量回归：`PYTHONPATH=. pytest satellite_debug_tool/tests -q`，`1524 passed`（实施前
  基线 1481，新增 43 项）。

## 尚待真机验收

- 真实 AFD01 `1235` Shell 会话与 Serial/CoolTerm 行为一致性。
- 真机 YMODEM 传输 `afd01c_app_0.0.131_beta.bin`：设备日志证明候选写入 DOWNLOAD 而非
  LittleFS（对应固件仓库 `debug_ota_raw_fw_20261010` 验收剩余项）。
- 重启后 Boot 安装链与版本回读显示一致性；跨产品文件名被设备拒绝的证据记录。
- YMODEM 持锁期间客户 OTA 被互斥拒绝；结束后恢复正常。

## 2026-10-12：真机反馈修复（终端渲染两处根因）

- 症状一"输入回车不换行、所有打印挤在一行"：渲染把 `\r` 当行首覆盖、随后紧跟的 `\n`
  又按"`\r` 已处理"跳过，导致 `\r\n` 整体不产生换行；而固件日志每行均以 `\r\n` 结尾。
  修复为 pending-CR 状态机：`\r\n` = 一次换行，单独 `\r` = 回行首覆盖（进度条语义保留），
  跨 TCP 包边界的 `\r`|`\n` 拆分同样正确。
- 症状二"Tab 跳到下一个输入框"：Qt 在 `event()` 层把 Tab/Backtab 截走做焦点遍历，
  `keyPressEvent` 收不到；改为在 `event()` 层拦截并发送 `\t` / `\x1b[Z`。构造期间的
  Qt 事件可能先于实例属性赋值进入覆写，`_input_enabled` 提升为类级默认值。
- 新增回归：`\r\n` 换行、跨包拆分、单独 `\r` 覆盖、Tab 键值发送；全量 `1527 passed`。
