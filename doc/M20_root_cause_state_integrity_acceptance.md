# M20 根因修复与状态完整性验收标准

## A. 握手与能力

- [x] 缓存中已有旧设备 profile 时，新连接的 META 前定义不会写入旧 profile。
- [x] 首轮只请求 META；META 到达后请求三张定义表和语义表。
- [x] 同一连接的硬件型号变化会重建连接代际，旧定义不能使新代际 ready。
- [x] capability 可区分未知、支持和不支持，未知设备的参数/OTA 控件保持禁用。
- [x] Debug、参数和 OTA 事务只接受精确上下文响应；无上下文成功响应不能推进状态机。
- [x] 旧的 AFD01 名称 fallback、DATA_REPORT ACK 和死重试路径已经删除。

## B. 产品状态与遥测

- [x] 快速状态帧确认的发射状态不会被缺字段的慢速帧覆盖。
- [x] 未知 trace mode 保持 UNKNOWN；任意 state ID 0 不会自动获得 trace-mode 控制能力。
- [x] Product Service 身份帧不会终止订阅重试；匹配 ACK 或有效快速遥测会确认订阅。
- [x] 身份帧存在但 Service SNR 尚未到达时，客户曲线继续使用可用的 legacy SNR。
- [x] 新鲜度判断只使用单调时间，系统墙钟跳变不改变在线/过期判定。

## C. 客户界面

- [x] 过期的锁定、跟踪、导航、发射和 modem 状态不会显示为绿色成功。
- [x] 未知或过期姿态显示为不可用，不渲染为 `0/0/0` 水平姿态。
- [x] 发射阵列离线和发射开关状态分别呈现，界面不修改设备上报事实。

## D. 夹具证据

- [x] 平台发送成功后，即使记录器失败也会发出“指令已发送”结果。
- [x] 命令、事件或 MS 帧写入失败均进入单一 `evidence_failed` 状态并停止后续动作。
- [x] 记录失败不会递归写事件或终止 worker 异常处理线程。
- [x] incomplete 会话明确显示未完成，不能显示为保存成功。

## E. 通信与接口

- [x] Serial/Udp worker 输出原始接收块，协议只由一个 `FrameReceiverV2` 解析。
- [x] UDP 丢弃非配置远端 endpoint 的数据，完整数据报不会被截断或跨来源拼接。
- [x] BaseWorker 重复解析器和无调用空实现 API 已删除。
- [x] 更新器 relocation 和更新检查使用肯定式配置，测试环境不再依赖否定式变量。

## F. 自动化验证

- [x] 每项根因具有能在旧实现稳定失败、在新实现稳定通过的回归测试。
- [x] 翻译 update/check 通过，无 unfinished、空翻译或占位符不一致。
- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q` 全绿且无 warning。
- [x] `git diff --check` 与 `compileall` 通过。
