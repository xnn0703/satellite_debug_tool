# M14 ESA01 Device Tab 接入计划

## 目标

让上位机 Device Tab 不再默认假设所有设备都支持参数/OTA，而是根据
`PROFILE_SEMANTICS.capabilities` 启用功能。ESA01 新固件声明 `parameters`
和 `ota` 后，上位机可读取/设置参数表，并通过现有 debug OTA 流程上传 app。

## 实施范围

- `Handshake` 主动请求 `REQUEST_PROFILE_SEMANTICS`，但不把语义帧作为 ready
  条件，兼容旧固件。
- `DeviceView` 接入 Live 页的 `ProfileStore`，按 capability 控制参数管理和
  OTA 控件。未知设备默认禁用；旧 AFD01 保留默认启用。
- 未声明参数能力时不自动发送 `REQUEST_PARA_TABLE`，避免旧 ESA01 固件连接后
  出现无意义超时。
- Device Tab 在不支持能力时显示明确状态文本。

## 验收标准

- 连接未声明 `parameters/ota` 的 ESA01：Device Tab 参数/OTA 控件禁用，且不会
  自动发送参数表请求。
- 连接声明 `parameters=true`、`ota=true` 的 ESA01：控件自动启用，参数读取和
  OTA 入口可用。
- AFD01 旧兼容策略不变：即便没有 capability 缓存，也默认允许参数/OTA。
- `Handshake` 会发送 `REQUEST_PROFILE_SEMANTICS`，但只收到 META/CHANNEL/STATE/EVENT
  仍可进入 ready。
- 全量 pytest 通过。

## 控制链路与异步 OTA 加固（2026-07-14）

### 基线与目标

- 实施前相关测试基线：握手、Dashboard 控制、Device capability 和 v2 codec 共
  **59 passed**。
- 保留 `DEBUG_ENABLE=0/1` 的严格上下文 ACK；普通 `OK` 不能确认 Debug 开关。
- 修复应落在设备调度和上位机交互模型，不再用 15 秒等待或自动重试掩盖设备端阻塞。

### 握手与参数

- 首轮只请求 META/CHANNEL/STATE/EVENT 四张基础表；收到 META 后独立请求
  PROFILE_SEMANTICS，每 1 秒一次、最多 3 次，且不阻塞 ready。
- 新 ESA01 在 semantics 成功后主动上报一次参数表。Device Tab 被动等待 1.5 秒，
  未收到时才发一次兜底请求；自动和手动请求需要合并，避免重复排队。
- `PARA_SET` 只接受 `PARA_SET=<name>` 上下文响应，并以设备主动回报的新参数表
  读回值作为最终成功依据，不做每秒轮询。

### Debug 与 OTA

- Debug ON 可由首个 DATA_REPORT 辅助确认；Debug OFF 必须收到
  `DEBUG_ENABLE=0`。最终 ACK 超时为 3 秒，不自动重试。
- Device OTA 改为 Qt 事件驱动状态机：
  `QUIESCE -> BEGIN -> DATA[n] -> END -> WAIT_REBOOT`。
- OTA 不再接管 worker 私有 socket、不重启 QThread、不调用 `processEvents()`；所有帧
  均经 Live worker 的统一发送和接收链路。
- OTA 期间暂停握手重试、参数请求和其它 Device 控件，并通过 Live 控制接口先关闭 Debug。
- 新固件严格匹配 `OTA_BEGIN=READY`、`OTA_DATA=<seq>`、
  `OTA_END=VERIFIED`；旧 AFD01/旧固件继续兼容通用成功响应。
- 超时固定为：Debug OFF 3 秒，BEGIN 15 秒，DATA 2 秒且最多重试 3 次，
  END 10 秒，重启上线 120 秒。

### 版本策略

- Debug OTA 默认允许同版本和降级，仍要求硬件型号、镜像完整性和 CRC32 正确。
- 设备端 YMODEM 同样允许同版本和降级；旧网络升级仍只接受严格高版本。
- 首次使用回退 OTA 前必须先通过调试器烧录支持新 policy 的 bootloader。

## 实施对照

- 计划内握手、参数、严格 Debug ACK、异步 OTA、上下文兼容和同版重连均已落地。
- 为避免 END ACK 后的旧 META 被误认作同版新 app，上位机额外增加 1 秒 META 隔离窗。
- 上位机自动化与固件四种构建均通过；硬件时延、栈/CPU、真实 OTA 和网络升级矩阵保留为上板验收，
  未用软件测试结果替代硬件结论。

## 握手回归恢复与取证（2026-07-14）

- 撤回“ESA01 WIZnet UDP RX 已被确定为根因”的结论。DATA_REPORT 正常只能证明设备上行可用，
  不能确定反向 CONTROL 在哪一层丢失。
- 设备先恢复通用 WIZnet/UDPServer 基线；上位机保持握手超时、请求顺序、ready 判定和严格 Debug ACK
  不变，避免诊断版本同时改变行为与观测条件。
- 增加 `SATELLITE_DEBUG_LINK_TRACE=1` 追踪开关，覆盖 LiveView 的 Connect/Disconnect/Debug 点击、
  UdpWorker 原始收发、FrameReceiver 解码结果、Handshake 首发/重试/定义应用/ready 和 Debug ACK。
  DATA_REPORT 只输出首帧及周期汇总。
- 请求与响应日志记录帧类型、CONTROL 子命令、长度、帧 CRC 和单调时间；与设备 `debug_trace`
  联合后可区分网络接收、debug parser、`dbg_ctl`、设备 TX、上位机 decoder 与 Handshake。
- 本阶段不依据超时现象直接修改 WIZnet、`dbg_ctl` 或上位机重试策略；硬件日志明确最后阶段后，
  再实施被证据锁定的最小修复。

### 取证结果（2026-07-15）

- 四类请求在上位机 `TX_UDP` 与设备 `LINK_RX` 的 CRC 完全一致，并继续到 `CTL_ENQUEUE`；
  `enqueue=41/dequeue=0` 将根因锁定为设备 `dbg_ctl` 消费逻辑。
- RT-Thread 5.x `rt_mq_recv()` 成功返回消息长度，设备端错误地用 `== RT_EOK` 判断，消息出队后未处理。
- 设备采用返回长度校验完成最小修复；上位机握手、超时、ready 条件和通用网络层保持不变。

### 第三轮复测与下行积压定位（2026-07-15）

- 管理队列修复后，握手约 1 秒 ready，semantics 与首次参数表均正常，所以上位机 Handshake 和
  Device 参数展示路径已恢复。
- 同一 socket 在稍后发送两次 `DEBUG_ENABLE=1` 及一次手动参数请求，`sendto()` 都返回完整长度，
  设备仍持续上行 Heartbeat/定义，但这些后续请求无响应。
- 设备历史日志显示每次点击先执行一个旧参数请求，多次点击后才执行 Debug；结合固件源码，根因是
  W5500 UDP `recvfrom()` 在读取 `Sn_RX_RSR` 前无条件等待一次 RECV 信号，无法排空一次中断合并的
  多个数据报。
- 上位机保持现状：不增加重试、不放宽 ACK、不延长超时。设备只修复 UDP 硬件缓存排空语义。

## AFD01/ESA01 共用 3D 模型朝向修正（2026-07-15）

- AFD01 与 ESA01 使用相同硬件结构，本地分别放置内容一致的 `afd01.stl`、`esa01.stl`，继续按
  `hw_type` 精确加载，不增加设备类型特判。
- 实测 STL 原始包围盒为 `X=241、Y=179.5、Z=52.1`。当前原始 `+X` 机头配置会让机头端面落在
  179.5 的短边；实际机头位于从当前方向俯视逆时针 90 度的长边，即原始 `+Y` 方向。
- 模型归一化改为原始 `+Y -> widget +X`（机头）、原始 `-X -> widget +Y`（左侧）、原始
  `+Z -> widget +Z`（天顶），保持 FLU 右手坐标系，避免用简单轴交换造成模型镜像。
- AFD01 与 ESA01 共用该朝向配置；姿态、波束反解和设备上报通道绑定不作修改。
