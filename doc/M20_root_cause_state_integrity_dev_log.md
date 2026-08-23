# M20 根因修复与状态完整性开发日志

## 2026-08-23：实施启动

### 基线

- 全量测试：`957 passed, 4 warnings in 54.48s`。
- 四个 warning 均为既有 `datetime.utcnow()` 弃用提示。
- 当前工作区包含 M19-A.2 夹具调试台和协议相关在途修改，本里程碑保持这些内容并在其上继续实现。

### 已确认根因

- Handshake 使用缓存的 `current_hw_type` 绑定新连接定义，连接代际缺失。
- capability 的未知状态被布尔 fallback 折叠，敏感事务会接受无上下文成功响应。
- 发射状态存在快速/慢速两个写入源，客户界面再以阵列在线状态覆盖领域事实。
- 订阅确认和 SNR 来源误用设备级 `service_available`，身份帧被当作实时遥测就绪。
- 状态新鲜度依赖墙钟，未知姿态被转换为零值。
- 夹具发送与证据写入耦合，记录器故障可能丢失已发送事实并中止线程。
- worker 与 Live 层重复解析协议，UDP 未锁定远端 endpoint。
- 更新器和空实现 API 保留了否定式或失效合同。

### 实施状态

- [x] 高优先级根因修复准则写入 `AGENTS.md`。
- [x] M20 计划、验收标准和开发日志落盘。
- [x] 握手连接代际与能力/ACK 合同。
- [x] 产品状态、订阅确认和单调时间。
- [x] 客户呈现与夹具证据失败状态。
- [x] 通信边界和失效接口清理。
- [x] 翻译、专项测试和全量回归。

## 2026-08-24：实施完成

### 握手与事务合同

- Handshake 使用显式连接代际；META 是定义表绑定硬件型号的唯一来源。
- capability 使用 `UNKNOWN / SUPPORTED / UNSUPPORTED`，参数和 OTA 只在设备明确声明能力及上下文响应后启用。
- Debug、参数和 OTA 只接受精确响应；已删除硬件名称启用、通用 `OK`、DATA_REPORT ACK 和无效重试分支。

### 产品状态与客户呈现

- 快速状态帧成为发射开关权威来源，慢速帧只负责首次初始化。
- Product Service 订阅由匹配 ACK 或快速遥测确认；设备身份、遥测就绪、SNR 数据和控制服务使用独立状态。
- DataStore、StateStore、ProductServiceStore 和 legacy 投影统一使用单调时间判断新鲜度。
- 过期状态不再显示成功色；未知姿态进入明确不可用状态；发射开关与发射阵列健康分别呈现。

### 夹具证据与通信边界

- 平台发送结果先上报，证据写入随后执行；写入失败进入单一 `evidence_failed` 状态并生成 `incomplete` 会话。
- 事件、MS 原始帧和最终指标故障均走同一收尾合同，界面显示真实 manifest 状态。
- Serial/Udp worker 只传递原始数据；`FrameReceiverV2` 是唯一协议解析器。
- UDP 锁定配置的远端 endpoint，接收缓冲扩大到完整 UDP 数据报尺寸，并区分正常关闭与读取故障。

### 肯定式接口与测试环境

- updater 内部状态改为 `--relocated`，后台更新能力改为 `SATELLITE_UPDATE_CHECK=0/1`。
- 删除 `set_channel_options()` 和 `set_channels()` 两个失效空接口及周期调用。
- 更新检查时间改为带 UTC 时区的 ISO 时间，同时兼容读取历史无时区值，四个弃用 warning 已消除。
- 测试进程共享一个 QApplication，修复模块间反复销毁 Qt 应用导致的 QThread/UI 死锁。

### 验证结果

- 翻译目录：`863` 条文案全部完成，TS/QM 同步检查通过。
- 语法与工作区：`python3 -m compileall -q satellite_debug_tool`、`git diff --check` 通过。
- 全量测试：`977 passed in 55.85s`，`0 warnings`。
