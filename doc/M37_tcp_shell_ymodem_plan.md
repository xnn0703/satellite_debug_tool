# M37 TCP Shell + YMODEM 三工作区弹窗计划

## 1. 背景与目标

M36 收口后，`0.0.130_beta` 设备必须先通过 YMODEM（或 J-Link）安装 `0.0.131_beta` 修复固件，
才能继续客户 `.sfpkg` OTA 与严格合同验收。当前 macOS 下 WindTerm 可用性差，Serial 已验证
AFD01 Raw TCP Shell 与标准 YMODEM 可以正常配合；本里程碑把这条链路做成上位机正式工程功能。

目标：

- Customer / Engineering / Production 三个工作区各自入口打开**同一个非模态弹窗**（摇摆台弹窗模式）。
- 弹窗内置 AFD01 Raw TCP Shell 终端（UTF-8 回显、退格、ANSI 剥离、hex 视图、命令历史）。
- 内置严格 YMODEM Batch Sender：`rb` → 等 `C` → 文件头 → 数据块 → EOT 两阶段 → 空文件名批次结束。
- 定位：工程调试、救援升级、旧版本引导升级路径；`.sfpkg` 客户升级路径不变。

## 2. 设备端合同（已核实，不修改固件）

- TCP Shell 端口默认 `1235`（设备参数 `Debug_Port` 可改；上位机不自动读取，允许手改）。
- 普通状态 TCP 字节交给 Letter Shell；`rb` / `ymodem` 命令把同一连接切换给 YMODEM。
- YMODEM 期间固件屏蔽普通日志；接收侧按帧头拼包（128/1024 字节块），已处理 TCP 拆包。
- 文件名路由（`ymodem_port.c`）：
  - `<target>_app_<X>.<Y>.<Z>_<release|beta>.bin` → `RAW_FW_DOWNLOAD`（Boot 可安装候选）；
  - `app_factory.bin` → `RAW_FW_FACTORY`；
  - 其他文件名 → LittleFS 普通文件；
  - 跨产品命名（AFD01A/B2/C/D 互跨）→ 设备拒绝。
- 协议细节（`ymodem.c`）：CRC16-CCITT 大端、序号+补码、`C` 握手、EOT 两阶段（首个 EOT 收 NAK）、
  批次结束 = 序号 0 + 空文件名。

## 3. 新增模块

```
core/tcp_shell/__init__.py
core/tcp_shell/ymodem_sender.py   # 纯 YMODEM Batch sender 状态机（可注入 transport，独立可测）
core/tcp_shell/worker.py          # TcpShellWorker(QThread)：原生 socket、50ms 可中断读循环、命令队列
core/tcp_shell/controller.py      # TcpShellController(QObject)：唯一会话 owner（连接/YMODEM/租约/recorder）
core/tcp_shell/recorder.py        # TcpShellSessionRecorder（照抄 PowerDebugSessionRecorder 原子收尾）
ui/tcp_shell_workspace.py         # TcpShellWorkspace(QWidget)
ui/customer_tcp_shell_dialog.py   # CustomerTcpShellDialog(QDialog)（照抄 CustomerFixtureDialog 模式）
```

无新第三方依赖（标准库 socket/threading/queue/hashlib）；`satellite_debug_tool.spec` 无需修改。

## 4. 三入口 → 同一弹窗

- `CustomerWorkspace._tcp_shell_dialog` 懒创建缓存；`open_tcp_shell_dialog()` 懒创建并前置。
- Customer 侧栏"TCP Shell…"按钮位于"摇摆台…"按钮之后。
- Engineering 顶栏 ghost 按钮 → `self._customer.open_tcp_shell_dialog()`，仅工程工作区可见。
- Production 子页导航旁非 checkable 按钮 → 经构造注入的 `open_tcp_shell` 回调打开同一实例。
- 弹窗跨工作区切换存活；关闭 = `confirm_shutdown` → 断开 TCP + finalize 证据；
  YMODEM 传输中关闭提示"发送 CAN CAN 取消并保存不完整证据"。
- `CustomerWorkspace.shutdown()` 与 `MainWindow.closeEvent` 兜底关停（摇摆台同款）。

## 5. 互斥与准入（已确认方案）

- Shell 观察/输入不持租约（等同物理调试串口的工程属性；全部 TX/RX 进证据可追溯）。
- YMODEM 开始前：endpoint 在 `EndpointSessionDirectory` 有 Runtime 时，经其操作网关
  `try_acquire_operation(owner, purpose="tcp-shell-ymodem")`（与 OTA/参数/Product/批次冻结同一 CAS）；
  占用时明确提示。无 Runtime（手填未管理设备）允许直传，证据记录 `managed_session: false`。
- 完成、取消、失败、断线或连接代际变化即释放租约。

## 6. 传输完成语义与最小版本回读（已确认方案）

- "YMODEM 完成"只表示字节已被设备接收并校验；升级生效需要独立证据。
- 完成后显示"等待设备重启"；endpoint 有 Runtime 时订阅该会话握手元数据，把重启后回读的
  `fw_ver` 与文件名解析出的目标版本并排显示（只呈现事实，不自动判 PASS）。

## 7. 证据会话

`~/.satellite_debug_tool/tcp_shell_sessions/<session_id>/`：

- `config.json`：host/port/managed_session/`result_class: ENGINEERING_ONLY`。
- `transcript.jsonl`：TX/RX 字节（方向、时间戳、hex、长度）。
- `ymodem_records.jsonl`：块序号、ACK/NAK、重传、阶段事件。
- `events.jsonl`、`summary.json`（文件名/大小/SHA-256/结果）、`manifest.json`
  （`.tmp` + 原子 rename，schema `satellite-debug-tool/tcp-shell-session` v1）。

## 8. 首版明确不做

通用 xterm / 全 ANSI 彩色、ZMODEM/XMODEM、自动读取 `Debug_Port` 参数、自动 J-Link、
修改设备固件、YMODEM 自动重连。

## 9. 实施顺序

1. 本计划与验收标准落盘。
2. `ymodem_sender.py` 纯状态机 + 单元测试。
3. `worker.py` + `controller.py`（准入/租约）+ 单元测试。
4. `recorder.py` + 单元测试。
5. `tcp_shell_workspace.py` / `customer_tcp_shell_dialog.py` / 三入口 + i18n + UI 测试。
6. 假设备端到端集成测试 + 全量回归。
7. AGENTS.md / dev_log / 验收软件项勾选；真机项留现场。
