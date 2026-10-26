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

## 2026-10-19：移除 YMODEM 上位机自加的 4 MiB 文件大小上限

- 需求（用户）：TCP Shell 本身是调试端口，上位机不应限制文件传输；大于 4 MiB 的文件
  （如 LittleFS 大资源）此前直接被拒。
- 根因：`ymodem_sender.py` 实现时自加 `MAX_PAYLOAD_BYTES = 4 MiB` 上限；M37 计划/验收/
  开发记录均无此合同，固件侧合同也无对应值——该上限无任何来源，YMODEM 头包 size 字段
  本身为 ASCII 十进制、协议无固有大小限制。
- 修复：删除 `MAX_PAYLOAD_BYTES` 与上限检查（常量与 `__all__` 导出一并移除，全仓无残留
  引用）。文件能否容纳由设备端权威决定：空间不足时设备 abort 发单个 CAN（M37 已核实的
  固件行为），上位机如实呈现设备拒绝。仅保留"payload 非空"检查——空文件头包与批结束
  帧（空文件名）不可区分，是协议层事实而非上位机策略。
- 回归：新增 2 项（4 MiB+1 spec 构造成功——旧实现稳定抛 "payload size out of range"；
  空 payload 仍被拒绝）；TCP Shell 四测试文件 65 项、全量 `1546 passed`。

## 2026-10-19：修复 XESA01（RT-Thread rym 接收端）YMODEM 批结束失败

- 现象（用户真机）：TCP Shell 给 4-xesa01 项目设备 YMODEM 传输失败，当时只能改用
  WindTerm 绕过；WindTerm 传同一文件成功证明设备侧文件准入放行。
- 根因（固件源码实证）：M37 发送机所有帧固定 STX/1024。4-xesa01 的 YMODEM 接收端是
  RT-Thread `rym` 组件（`ry` 命令写文件系统、`ymodem_ota` 写 OTA 分区，on_begin 均无
  文件名白名单），其 `_rym_do_fin` 对批结束头**硬编码按 SOH 包长读取校验**——无论帧头
  是什么都只读 133 字节、按前 128 字节 payload 算 CRC（`ymodem.c` data_sz 赋值后未用）。
  STX 批结束帧在 RT-Thread 取 CRC 的位置读到的是全零 payload，CRC 必然失败，数据块
  100% ACK 后整批在最后一步失败。WindTerm 的批结束帧是 SOH/128，因此成功。
- 修复（上位机 `ymodem_sender.py`）：块类型合同改为头包与批结束帧 SOH/128、数据块
  STX/1024。AFD01 接收端按帧头字节自适应块长（`ymodem.c` 同时支持 SOH/STX 并分别
  计数），SOH 头包/批结束对既有 AFD01 路径无影响；超长头包（filename+size > 128B）
  回退 STX，两类接收端都接受。`_build_frame` 参数化 marker，新增 `HEADER_BLOCK_SIZE`。
- 回归：新增 3 项（镜像 RT-Thread fin 的固定 133 字节校验、头包 SOH/128、超长头包回退
  STX）；既有帧合同断言更新为按帧头解析；worker 端到端假设备改为按 marker 解析块长。
  TCP Shell 四测试文件 68 项、全量 `1549 passed`。

## 2026-10-19：修复 XESA01 yota 头包 ACK 前擦分区导致的握手超时

- 现象（用户真机，会话 `20261019T071000Z-70f88b40`）：SOH 头包修复后重测 XESA01
  （RT-Thread msh，手动 `yota` 进入接收），进度 0% 失败，报
  `timeout waiting for header acknowledgement`。
- 根因（transcript 字节+时间戳实证）：上位机 `+0.00s` 发出正确的 SOH/133 头包；
  `+5.00s` 按块确认窗口（`ack_timeout_s`）超时发 CAN CAN 放弃；**`+6.87s` 设备的
  ACK+C 才到达**——RT-Thread `yota` 的 `on_begin` 在头包 ACK 前**同步擦除整个
  download 分区**（552 KiB 实测 ~7 s；分区为 NOR flash 1 MiB，见 `fal_cfg.h`）。
  上位机退出后设备每 3 s 发 NAK 等数据块，NAK 用尽后打印 `Update firmware fail.`
  退出。M37 原实现只对 EOT 阶段（AFD01 落盘）设了 20 s 存储窗口，头包 ACK 用的是
  5 s 块窗口——但"设备在确认前做耗时存储操作"是同一类事实，头包 ACK 同样会被推迟。
- 修复（`ymodem_sender.py` `_send_header`）：头包 ACK 等待改用存储操作窗口
  `final_ack_timeout_s`（默认 20 s，覆盖 1 MiB 分区满擦 ~12.5 s），数据块 ACK 保持
  块窗口。AFD01 路由 ACK 即时返回，长窗口无副作用。
- 操作约定（真机）：XESA01 上设备侧接收命令是 `yota`（msh，无 `rb`）；每次传输
  开始前需在终端手动输入 `yota`，失败/取消后设备已退出，重试前必须重新输入。上位机
  自动发送的 `rb\r` 在 yota 等待期间只是 3 个被跳过的噪声字节，在空闲 msh 下报
  `b: command not found`，均无害。
- 回归：`ScriptedTransport` 支持 (data, delay) 到期交付项（真实 socket 超时语义）；
  新增 3 项（头包 ACK 擦除延迟下完成传输、头包窗口==存储窗口的反向锁定、worker
  端到端延迟传输）；TCP Shell 四测试文件 71 项、全量 `1552 passed`。

## 2026-10-19：弹窗布局改为"终端主区 + 右侧栏"（用户反馈）

- 真机确认：XESA01（192.168.1.6，msh `yota`）YMODEM 传输完成、进度 100%——SOH/128
  帧合同与头包 ACK 存储窗口两项修复均经真机验证。
- 布局重构（用户需求"连接、会话事件、文件传输放到侧边，给终端更大显示区域"）：
  原四段垂直堆叠（终端仅占约 38% 高度）改为水平 QSplitter——左侧 Shell 终端主区
  （全高、约 2/3 宽），右侧栏垂直收拢 TCP Shell 目标（地址/端口/连接/状态三行紧凑）、
  YMODEM 固件传输（路径行 + 开始/取消行 + 信息/进度/结果）、会话事件（占剩余高度）。
  两侧不可折叠，比例可拖动；侧栏内容下限 300px，弹窗最小尺寸 880→960 宽。
- 顺带修复：`retranslate_ui` 此前不刷新三个 QGroupBox 标题（语言切换后残留旧语言）；
  文件路径框补 tooltip 显示完整路径。
- 验证：UI 34 项、全量 `1552 passed`；翻译 check 通过（1408 条，无新增文案，全部复用
  既有源键）；离屏渲染 1040×720 与 960×560 检查——按钮无换行截断、分组无重叠、终端
  在最小窗口仍约 60% 宽。

## 2026-10-19：终端行首接收时间戳与可见光标（WindTerm 风格，用户反馈）

- 需求：终端跟 WindTerm 一样——每行行首显示本地时间、显示光标。
- 实现（`tcp_shell_workspace.py`）：
  - 每行行首插入接收时刻时间戳 `[HH:MM:SS.mmm] `（灰色 `#7f848e`，等宽定长 15 字符），
    时刻取该行首个字节到达时间；hex 视图每条 hex 行同样加时间戳。
  - 时间戳是本地装饰，设备侧行编辑动作只作用于时间戳之后：`\r` 行首覆盖与 ESC[K
    清行的"行首"重定义为时间戳之后（进度条反复重绘不会吃掉时间戳，且该逻辑行保持
    初次创建时刻）；设备退格在时间戳边界停止。
  - `TerminalEdit` 设 `cursorWidth=2`——哑终端光标跟随设备回显的写入位置，渲染后
    停在末尾闪烁。
  - 会话日志 `.log` 保持纯设备文本合同，不写入时间戳。
- 验证：新增 7 项回归（每行时间戳格式、`\r` 覆盖单一时间戳、EL2 清行保留时间戳、
  退格停在时间戳、hex 行时间戳、日志无时间戳、caret 宽度）；既有整行断言改为剥时间
  戳后比较、SGR 颜色位置断言加时间戳偏移。UI 41 项、全量 `1559 passed`；离屏渲染
  确认真实字节流（banner/EL2 日志/进度条四帧覆盖）时间戳统一对齐、无重复残留。

## 2026-10-20：光标改自绘（用户反馈"看不到光标"根因）

- 现象：真机看不到终端光标。根因不是颜色——**Qt 只读 QTextEdit 不绘制 text caret**，
  `setCursorWidth` 对只读控件无效（上一版仅设宽度，光标从未画出来过）。
- 修复（`TerminalEdit`）：viewport 自绘闪烁光标——530ms 半周期定时器切换 `_caret_on`，
  `paintEvent` 尾声在 `cursorRect()` 画 2px 竖条，颜色取 palette Text 色（深背景白色、
  浅背景黑色，自动反差）。焦点进入启动闪烁、离开停止并清除残影；位置跟随设备回显
  写入位置（渲染后 textCursor 停在末尾）。连接成功即 `setFocus(terminal)`，键盘输入
  与光标立即可用。
- 验证：caret 回归更新为完整合同（focusIn 启动定时器、翻转点亮、paint 冒烟、focusOut
  停止清除）；离屏像素级验证——点亮时 cursorRect 区域从背景色变为文字色。UI 41 项、
  全量 `1559 passed`。

## 2026-10-20：终端右键菜单增加"清屏"（用户反馈）

- 需求：右键目前只有全选/复制（Qt 只读 QTextEdit 默认菜单），补 WindTerm 同级的清屏。
- 实现：`TerminalEdit` 覆写 `contextMenuEvent`——默认菜单（全选/复制）后加分隔线与
  "清屏"，emit `clear_requested`；workspace `_on_clear_terminal` 清空终端并整体复位
  渲染管线状态（增量 UTF-8 解码器、挂起 `\r`、覆盖模式、SGR 颜色、行时间戳状态），
  防止残留的半个 ANSI 序列或颜色污染清屏后的新内容；事件区记录"终端已清屏"。
- 验证：新增 2 项回归（菜单含清屏项 + 分隔线；清屏后状态复位、新行时间戳正常、事件
  记录）；翻译 update + check（1410 条 0 未完成，新增"清屏"/"终端已清屏"中文）。
  UI 43 项、全量 `1561 passed`。

## 2026-10-22：Shell 弹窗支持串口连接（UDP 拒绝），入口更名 "Shell…"

- 需求（用户）：目前只支持 TCP，扩展 UDP/串口；入口按钮名称改为 Shell。
- 设备侧事实（固件源码实证）：AFD01 letter shell 与 XESA01 msh 的 shell 都在 TCP 1235
  和串口（letter shell UART / msh 控制台）两路；UDP 口只跑 DEBUG v2 协议，无 UDP shell
  服务——UDP 明确不做，`create_link` 对未知 transport 抛 `unsupported`。
- 架构：新增 `core/tcp_shell/transport.py` 字节流 link 层（`ShellLink` 接口 +
  `TcpShellLink` / `SerialShellLink`，pyserial 8N1）；`TcpShellWorker` 保持命令队列单
  owner 模型，`_ConnectCommand` 改 target dict（`{"transport": "tcp"|"serial", ...}`），
  socket 操作全部委托 link；`connected` 信号 (host, port) → (label)；controller
  `connect_device(target)`；recorder `config.json`/`summary.json` 记完整 target。串口
  target 无管理 endpoint（unmanaged 直传，与手填 TCP 同语义）。
- UI：连接区改类型下拉（TCP/串口）+ `QStackedWidget` 两页（TCP：地址+端口；串口：端口
  枚举+手填、波特率 8 档默认 115200）；settings 新键 `tcp_shell.last_transport` /
  `serial_last_port` / `serial_baudrate`；会话日志文件名 TCP 用地址、串口用串口名；三
  工作区入口按钮与弹窗标题更名 "Shell…" / "Shell"。
- 测试：新增 19 项（transport 11：TCP 真 socket 对拍、Serial mock pyserial、UDP 拒绝、
  controller 串口开路失败端到端；UI 6：两页互斥、target 组装、串口枚举保留手填、
  settings 记忆恢复、串口 endpoint=None、日志文件名随类型；入口文案 1）；既有 TCP
  socket 假设备端到端全保留。worker 层不为 serial 另做端到端——命令循环/YMODEM 与
  物理口无关（全经 link 接口），TCP 路径已覆盖。全量 `1578 passed`；翻译 1413 条
  0 未完成。


## 2026-10-23 Shell Shift 字符诊断（源码验证）

用户反馈 Shift+- 后出现乱码及减号。对现有 TerminalEdit 的原生 macOS 键盘事件和 bytes_entered 信号进行无设备探针记录：纯 Shift text 为空且不发送数据；Shift+- 的 Qt key 为 U+2014、text 为两个中文破折号 U+2014，实际发送 e28094e28094；Shift+1 产生全角 U+FF01，发送 efbc81；Shift+A 产生 A，发送 41。终端保留输入法产出的字符，Shift 修饰键没有丢失。设备若按单字节处理上述中文标点，其显示可呈乱码；本轮未在实际设备上确认乱码的具体解码路径。

英文文本事件的本地回归：Shift+_、Shift+A、Shift++ 分别发出 5f、41、2b；纯 Shift 不产生字节。建议使用英文/ABC 输入法输入 Shell 命令，保留现有 UTF-8 输入语义。终端代码保持原实现。探针原始记录位于 /tmp/sdt-shell-native-keys.jsonl；测试窗口已关闭，过程未连接设备。

按用户新约定，本轮仅提供源码运行命令；未执行打包。用户版：`.venv/bin/python -m satellite_debug_tool.main_user`；运维版：`.venv/bin/python -m satellite_debug_tool.main`。
