# M26 客户页外接电源只读监测计划

状态：实现与软件验收完成；真实 PSW/负载验收待执行

验收标准：`doc/M26_customer_external_power_monitor_acceptance.md`

开发记录：`doc/M26_customer_external_power_monitor_dev_log.md`

## 1. 已核对基线

- Customer Overview 底部当前只有“变频板 / 发射阵列 / 接收阵列”三个状态项；点击后复用 `ComponentTemperatureAnchor` 与 `ComponentTemperatureWindow`，显示最近 30 分钟温度曲线。
- 三个部件状态来自当前设备的 `ProductServiceStore`；外接电源是进程级共享设备，不属于任一 AFD01 endpoint，不能复制到每个 endpoint Runtime 中分别连接。
- 现有 `GwInstekPswAdapter`、电源调试页和批次电源流程属于 Production 的安全控制模型，要求身份、设定值和输出闭环。客户页需求只允许读取，不应借用设定值、OUTPUT ON/OFF 或任意 SCPI 写路径。
- 当前全局设置弹窗无论处于哪个工作区都会显示试产配置和试产报告字段，造成客户页设置混入试产业务。
- 当前定向基线为 `51 passed in 1.47s`：设置、部件温度窗口、Customer Overview 与 PSW 协议测试均通过。
- 工作区已有未提交的 M19/M25 等改动；实施必须保留这些改动，不覆盖、不回退，也不主动提交。

## 2. 目标

1. 在 Customer Overview 底部增加第四项“外接电源”，与现有三个部件保持同一视觉和点击交互。
2. 配置外接电源 IP 后，由一个进程级只读监测 owner 连接 GW Instek PSW 80-27，周期读取真实电压、电流和可读状态；IP 留空时不创建网络连接。
3. 底部状态项显示明确事实：未配置、连接中、在线、离线或读取失败，以及最近一次有效电压、电流和计算功率。
4. 点击“外接电源”打开可复用的独立窗口，同时绘制最近 30 分钟电压和电流曲线；窗口支持原生最大化、最小化和关闭。
5. 客户全量录制进行期间，把有效外接电源样本作为类型化 SDB v3 metadata 记录，便于把实际功耗与设备数据放在同一主机时间线上。
6. 设置弹窗按当前顶层工作区显示业务配置：客户页不显示试产配置和试产报告；Production Workspace 仍可访问原有试产配置和报告设置。

## 3. 权威所有权与数据流

```text
Settings.external_power.ip
        ↓
MainWindow-owned ExternalPowerMonitor (single worker / single TCP transport)
        ↓
ExternalPowerStore (single current snapshot + bounded 30 min history)
        ├─ Customer Overview fourth status anchor
        ├─ one shared voltage/current history window
        └─ active customer SDB v3 recorder metadata event
```

- `MainWindow` 负责创建、重配和关闭唯一监测实例；Customer 多 endpoint 页面只消费同一个 Store，不各自连接电源。
- 后台 worker 串行执行 TCP/SCPI；UI 线程只接收类型化快照，不直接访问 socket。
- 连接先读取 `*IDN?` 并核对 `GW-INSTEK / PSW 80-27`，再按固定周期读取 `MEAS:ALL?`，并读取输出/运行/告警/保护等只读状态。
- 客户监测路径没有输出设定、OUTPUT ON/OFF、返回本地、任意 SCPI 输入或自动写入 API。代码与测试应证明发送命令集合全部是查询命令。
- 有效样本包含主机 wall-clock、monotonic 时间、连接代次、电压、电流、计算功率 `P = V × I`、输出状态和保护/告警事实。无效、非有限或解析失败的响应不会伪造成 `0 V / 0 A`。
- 网络或查询失败后立即撤销“在线”事实并关闭当前 transport；配置仍有效时按明确的重连节拍恢复监测，重连只做身份和只读查询，不重放任何控制动作。
- 进入 Production Workspace 前停止客户只读监测并关闭 transport；离开 Production 返回客户页时按当前设置重新启动，避免与批次/工程电源控制并发占用同一实体电源。

## 4. 客户界面与曲线合同

- 底部四项等宽排列，顺序为“变频板 / 发射阵列 / 接收阵列 / 外接电源”。
- 外接电源摘要建议为：`在线 · 12.04 V · 1.36 A · 16.37 W`；没有有效证据时分别显示“未配置 / 连接中 / 离线 / 读取失败”，旧读数不得继续显示成实时值。
- 第四项复用现有可点击锚点的键盘可达、hover/focus 和邻近定位体验，但电源窗口使用外接电源 Store，不混入 Product Service 部件温度 Store。
- 电源窗口显示当前连接/身份/输出/保护事实、当前 V/A/W，以及最近 30 分钟两条曲线。双 Y 轴分别标注 `V` 与 `A`，横轴使用主机采样时间。
- 同一进程只保留一个电源历史窗口；切换客户 endpoint 后再次点击仍激活同一窗口，不创建重复窗口或清空电源历史。
- 主题和中英文切换同步更新状态项、窗口标题、当前值和坐标轴。

## 5. 设置整理

- `SettingsDialog` 接收明确的工作区 scope，由 `MainWindow` 当前顶层工作区传入。
- Customer scope：保留通用设置并新增“外接电源”区域，只配置 IPv4；端口固定显示为 `2268` 且不可编辑，说明该功能只读取、不控制输出。
- Engineering scope：保留现有工程通用设置，不显示试产配置和试产报告。
- Production scope：保留“管理试产配置”和完整试产报告设置；外接电源客户监测配置不作为批次电源档案，Production 仍以版本化电源 profile 为权威。
- IP 清空并确定后立即停止监测并显示“未配置”；IP 格式无效时阻止保存并给出明确提示。
- 隐藏的 scope 字段不写回默认值，也不改变原有配置。

## 6. 记录合同

- 有效样本只在客户全量录制 `ACTIVE` 且格式为 SDB v3 时写入 `SDB_RECORD_METADATA`，事件类型固定为 `external_power_sample/v1`。
- 每条事件至少包含电源身份、连接代次、电压、电流、计算功率、输出状态及保护/告警状态；事件时间使用同一次采样的主机纳秒时间。
- 多台客户设备可同时录制时，每个活动录制文件各自接收同一共享电源样本；未录制的 endpoint 不产生文件写入。
- 记录入队失败沿用 Recorder 的丢记录统计，不阻塞 UI 或电源采样线程。
- 本轮不在 Playback 页面新增电源曲线入口；SDB metadata 可由现有导入/取证路径读取，Playback 呈现另立范围。

## 7. 实施步骤

1. 新增外接电源只读配置、快照、Store 与单 worker，并以脚本化 transport 固定查询集合和失败语义。
2. 在 `MainWindow` 建立进程级 owner，接入工作区生命周期、设置变更、Customer bundle 广播和应用关闭等待。
3. 在 Customer Overview 增加第四状态项，新增共享电压/电流曲线窗口并接入主题、语言和 endpoint 切换。
4. 为 Customer SDB v3 录制增加类型化外接电源 metadata 入口，不改变设备协议帧和 Product Service Store。
5. 按 scope 整理 SettingsDialog，新增外接电源 IP，保持 Production 配置的现有持久化合同。
6. 更新中英文翻译、`AGENTS.md` 当前架构说明和开发记录。
7. 对照验收标准执行定向测试、翻译检查、差异检查，再执行全量 pytest。

## 8. 本轮边界

- 包含：PSW 80-27 只读实时监测、V/A/W 当前值、V/A 30 分钟曲线、客户 SDB v3 样本记录、设置 scope 整理、生命周期与回归测试。
- 不包含：客户页电源控制、设定电压/电流、任意 SCPI、告警阈值配置、跨应用重启保留曲线、Playback 电源曲线、其他品牌/型号电源、安装包发布。
- 软件测试只能证明查询集合、状态流、记录和 UI 合同；真实 IP 连通性、面板读数一致性、长时稳定性和实际功耗精度仍需真实 PSW/负载验收。

## 9. 完成定义

- 客户页底部第四项可稳定显示外接电源的明确状态和 V/A/W，点击打开唯一的 V/A 曲线窗。
- IP 留空时零网络活动；配置有效 IP 后只发送白名单查询，任何客户入口都不能改变电源输出或设定值。
- 多 endpoint 共享一个监测 owner、一份历史和一个窗口；进入 Production 时不存在客户监测与试产控制并发。
- 客户录制文件包含可校验的 `external_power_sample/v1` 事件，时间和值与 Store 样本一致。
- 客户/工程设置不再显示试产报告，Production 设置仍完整可用。
- 新增回归测试、相关既有测试、翻译检查、`git diff --check` 和全量 pytest 通过；真机边界明确保留为待验证。
