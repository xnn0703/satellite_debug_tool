# M37 TCP Shell + YMODEM 验收标准

## A. YMODEM Sender 协议合同

- [x] 发送 `rb` 后仅在收到 `C` 时开始文件头包；收到 `CAN`、超时或非预期控制字节立即终止并给出原因。
- [x] 文件头包携带原始文件名与十进制大小；数据块使用 1024 字节 STX 帧，末块不足 1024 时补齐。
- [x] 每块序号 mod 256 + 补码 + CRC16-CCITT 大端；NAK 触发重传同一块，重传上限 10。
- [x] EOT 两阶段：首个 EOT 收 NAK 后重发 EOT，第二个 EOT 期待 ACK+`C`；随后发送空文件名批次结束包并期待 ACK。
- [x] 取消发送 `CAN CAN`，传输以 cancelled 收尾，不残留半开状态。
- [x] 接收字节按帧头与期望长度累积，粘包与半包均正确处理（单元测试覆盖分片交付）。
- [x] 文件名原样传递：`<target>_app_X.Y.Z_release|beta.bin`、`app_factory.bin` 与普通文件均不被改名。
- [x] 传输前校验文件 ≤ 4 MiB 并计算 SHA-256。

## B. 设备事务互斥

- [x] Shell 观察/输入不获取设备事务租约。
- [x] endpoint 存在已管理 Runtime 时，YMODEM 开始前必须以 `tcp-shell-ymodem` 获取操作网关；被占用时明确拒绝并提示。
- [x] YMODEM 完成、取消、失败、断线后租约全部释放；租约被持有时 OTA/参数/批次入口行为不变。
- [x] 无 Runtime 的手填目标允许直传，config 与 summary 记录 `managed_session: false`。

## C. 会话与证据

- [x] 会话目录 `~/.satellite_debug_tool/tcp_shell_sessions/<session_id>/` 含 config/transcript/ymodem_records/events/summary/manifest。
- [x] transcript.jsonl 完整记录 TX/RX 方向、时间戳与 hex；YMODEM 期间二进制字节不进入终端文本。
- [x] summary.json 记录文件名、大小、SHA-256 与结果；manifest 为原子写、状态与 summary 一致。
- [x] 传输中取消/断线时以 incomplete 收尾，不产生误导性 complete。

## D. UI 与三入口

- [x] Customer 侧栏、Engineering 顶栏、Production 导航旁三个入口打开同一个弹窗实例。
- [x] 弹窗跨工作区切换存活；关闭时若有活动连接或传输，明确确认后收尾（传输中提示 CAN CAN 取消）。
- [x] 终端 UTF-8 回显、退格生效、ANSI 转义剥离；hex 视图可切换；输入框支持 ↑↓ 历史并以 `\r` 结尾发送。
- [x] YMODEM 区显示文件名、大小、SHA-256、预期路由提示、进度（块/字节/速度/重传）与设备响应。
- [x] 传输完成后显示"等待设备重启"；endpoint 有 Runtime 时并排显示回读 `fw_ver` 与目标版本，不自动判 PASS。
- [x] 全部第一方文案过 `update_translations.py update` + `check`，中英文资源同步。

## E. 回归与集成

- [x] 假设备 TCP server 端到端：`rb` → `C` → 文件头 → 全部数据块 → EOT 两阶段 → 批次结束全程通过（含分片交付）。
- [x] worker 生命周期：连接、断开、socket 错误、中断退出均不悬挂线程、不泄漏 socket。
- [x] 全量 `PYTHONPATH=. pytest satellite_debug_tool/tests` 通过。

## F. 真机验收（现场）

- [ ] 真实 AFD01 Shell 会话：连接 `1235`、命令回显、日志观察与 Serial/CoolTerm 行为一致。
- [ ] 真机 YMODEM 传输 `afd01c_app_0.0.131_beta.bin` 成功，设备日志显示候选写入 DOWNLOAD 而非 LittleFS。
- [ ] 传输后设备重启、Boot 安装、目标版本回读与弹窗显示一致。
- [ ] 跨产品文件名（如对 AFD01C 发 `afd01a_app_*.bin`）被设备拒绝且上位机证据如实记录。
- [ ] YMODEM 期间对同一 endpoint 发起客户 OTA 被互斥拒绝；结束后可正常发起。
