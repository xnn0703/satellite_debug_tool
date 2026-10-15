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

## 2026-10-12：真机首轮验证与两项修复

### 真机证据（2026-10-12 11:06–11:07 会话）

- TCP Shell `1235` 会话、命令回显、日志观察与 Serial 行为一致。
- YMODEM 传输 `afd01c_app_0.0.132_beta.bin`：784392 B / 4826 ms / 158 KiB/s，1K 块 767、
  128B 块 0、rejected=0；设备 `ymodem_port_thread` 报告 completed。
- 设备日志证明候选写入 raw download 区（`Found new fw in raw download`，非 LittleFS），
  同时验证了固件仓库 `debug_ota_raw_fw_20261010` 的单一候选区修复。
- 重启后 Boot 完整安装链：`app_old.bin` 备份 → DOWNLOAD 安装 → `ota status` 回读
  app version 0.0.132 → jump app。

### 修复一：终端对齐与颜色（对照 Serial 截图）

- 症状：表格列错位、日志无颜色。根因：终端未用等宽字体（Letter Shell 靠空格对齐），
  且 ANSI 序列被整体剥离。
- 修复：`TerminalEdit` 显式等宽字体（systemFont(FixedFont) + Monospace styleHint + fixedPitch，
  13px）；decoder 从"剥离 ANSI"升级为 token 化——SGR 序列解析为颜色指令（16 色前景 +
  粗体，one-dark 系配色，38/48 扩展色跳过保持当前色），非 SGR 控制序列仍丢弃，跨包拆开的
  ESC 序列缓冲重组；`\t` 在等宽字体下按 4 列展开。

### 修复二：YMODEM 进度恒 0%

- 症状：传输完成但进度条保持 0%（真机 784 KB 全程 0%）。
- 根因：`TcpShellWorker.__init__` 里 `self._sender = sender or YmodemSender()` 在生产
  sender=None 时预建了**无回调**默认实例，`_run_ymodem` 的 `self._sender or
  YmodemSender(on_progress=...)` 永远取不到带回调实例——进度信号与逐块证据回调被吞。
- 修复：构造保留 `self._sender = sender`，回调实例仅在 `_run_ymodem` 现场构造；端到端
  测试改走生产路径并断言 `ymodem_progress` 事件真实到达（此前注入 fast sender 掩盖了
  该路径）。

### 验证

- 新增 6 项回归（SGR token 化、跨包 ESC 缓冲、文档颜色、粗体重置、等宽字体、Tab 展开）
  与端到端进度断言；全量 `1532 passed`。
- 渲染对照：表格竖线对齐成列、日志级别 I/E 分色、每行独立、等宽生效。

## 2026-10-15：Boot 阶段进度条碎片修复

- 现象：boot 固件加载进度条（`ota_print_progress_bar`：每帧 `\r` + 整行重打、仅 100% 补
  `\n`）与 shell 提示符并发输出时产生 `123oot:/$` 类半替换碎片。
- 根因：渲染器在每个 TCP 包尾提前决断挂起的 `\r`（回行首 + 覆盖模式），包边界把覆盖
  意图泄漏到后续无关文本（shell 提示符、日志）上；boot 阶段两个并发输出源交错时碎片被放大。
- 修复：包尾 `\r` 不再提前决断，语义推迟到下一字符（`\n`→换行、其他→覆盖），TCP 包
  边界不再改变渲染语义。
- 边界说明：boot shell 提示符与 OTA 进度条在设备侧并发写同一 TCP 流，哑终端下交错拼接
  仍会出现（Serial/WindTerm 同字节流同样如此）；单源进度条已稳定为单行刷新。
- 新增 3 项回归（进度条三帧单行刷新、包尾 `\r` 不决断、`\r|\n` 跨包换行）；全量 `1535 passed`。

## 2026-10-15：App 进度条"每帧多一个 /$ root:"根因修复

- 现象：raw_fw 预擦进度条打印期间，每帧多出一截 `root:/$ `，终端累积大量提示符碎片。
- 根因证据（会话 `tcp_shell_sessions/20261015T070125Z-cf763f8a` transcript 实证）：设备 App 侧
  每帧字节为 `root:/$ ` + `ESC[2K` + `\r\r` + 进度文本（日志行为 `root:/$ ` + `ESC[2K` + `\r` +
  颜色 + 内容 + `\r\n`）——先打提示符再用 Erase-in-Line 清整行重绘。渲染器此前把非 SGR
  控制序列整体丢弃，`ESC[2K` 的清行动作丢失后，提示符尾部逐帧追加累积。
- 修复：decoder 将 `ESC[<n>K` 产出为 `("el", n)` token；渲染按 0/1/2 三种模式擦行
  （2=整行、0=光标到行尾、1=行首到光标），擦行前先 flush 并决断挂起的 `\r`。
- 验证：用该会话完整真实字节流回放，99 个预擦进度帧收敛为单行 `[100%]`，无提示符碎片；
  日志行的提示符前缀被正确清除。新增 4 项回归（decoder EL token、进度帧无残留、日志前缀
  清除、EL0 行尾擦除）；全量 `1539 passed`。
- 同会话证据顺带确认：` 3B initialized=...` 行首截断为设备侧发送的残缺行（transcript 中
  该行本就无 `ESC[2K\r[TS] I transceiver_show: KaTR00` 前缀），非上位机渲染问题。

## 2026-10-15：Shell 会话日志自动保存

- 需求（用户）：开关切换自动保存；开启即创建 `<IP>_YYYYMMDD_HHMMSS.log`。
- 实现：终端标题行"自动保存会话日志"开关；首次开启弹目录选择并记忆
  （`tcp_shell.log_dir`，已配置目录直接使用、缺失时重建）；`ShellLogWriter` 追加写解码后的
  会话文本（`\r` 归一换行、退格丢弃），与 hex 视图/渲染模式无关；关闭开关或弹窗关闭即
  收尾文件，事件区显示文件路径；目标 IP 出现在文件名中（非法字符替换为下划线）。
- 验证：5 项新回归（文件创建与内容、关闭停写保留文件、hex 不影响记录、shutdown 收尾、
  取消目录选择回退开关）；离屏确认 `192.168.1.12_20261015_152304.log` 生成且内容正确；
  全量 `1544 passed`；翻译 1408 条 0 未完成。
