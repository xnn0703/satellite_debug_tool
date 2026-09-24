# M28 客户设备独立电源与 iperf3 计划

状态：软件实现完成；真机多设备与 ECS 并发验收待执行

验收标准：`doc/M28_customer_device_accessories_acceptance.md`

开发记录：`doc/M28_customer_device_accessories_dev_log.md`

## 1. 已确认的当前事实

- Customer 最多配置 4 个设备 endpoint，但 `customer.devices` 当前只保存设备 IPv4 和端口。
- 外接电源当前是进程级单例：全局只有一个 `external_power.host`、一个监测 owner、一个 Store 和一个曲线窗口；所有客户 endpoint 消费同一份样本。
- 网络测试当前也是进程级单例：全局只有一份 `iperf.*` 配置、一个控制器、一组 UL/DL 进程和一个证据会话；Network test 页面不绑定当前客户 endpoint。
- 因此当前版本不支持多台客户设备分别配置电源或 iperf3，也不能证明一份功耗/网络证据属于哪台设备。
- 客户外接电源会话错误地采用严格只读 API，并以包含空格的型号字符串精确比较；真机 `PSW80-27` 会被错误拒绝。Production 的 `GwInstekPswAdapter` 已证明设参、启停与回读闭环可用，并采用归一化身份比较。

## 2. 目标

1. 每台客户设备拥有稳定的 accessory identity；编辑设备 endpoint 时保留该设备的电源、iperf3 配置和历史归属，删除设备时按活动资源规则完成收尾。
2. 每台设备可独立配置一台 GW-INSTEK PSW80-27，独立显示实时 V/A/W、30 分钟历史、输出/保护状态，并执行闭环设参、开启和关闭输出。
3. 电源 IP、设定电压和设定电流放入该设备 Overview 的“外接电源”弹窗；全局设置页移除客户外接电源区域。
4. 每台设备拥有独立的 iperf3 服务器、本地绑定 IPv4、协议、方向、UL/DL 端口、速率、持续模式和测试会话；本机 iperf3 可执行文件路径继续作为主机级配置复用。
5. 切换左侧设备只切换呈现，不重绑 Store 或控制器；其他设备已经开始的电源采样和 iperf3 测试继续运行。
6. 允许资源不冲突的多设备测试并行；资源冲突时第二个动作被明确拒绝，不通过共享 Store、复用进程或覆盖配置实现伪并行。
7. 每份客户 SDB 和 iperf3 会话证据只接收其绑定设备对应的电源样本与网络指标，并记录稳定设备 ID、当时 endpoint 和外设身份。

## 3. 权威领域模型

```text
CustomerDeviceRecord (stable device_id + mutable UDP endpoint)
        |
        +-- ExternalPowerProfile
        |       host / fixed port 2268 / voltage_set_v / current_set_a
        |       |
        |       +-- one serialized PowerSession owner
        |               monitor + control + current snapshot + history
        |
        +-- IperfProfile
                server / local_host / protocol / direction / ports / rates / duration
                |
                +-- one IperfTestController + Store + evidence session
                        |
                        +-- consumes only this device's PowerStore
```

- 设备记录增加稳定 `device_id`，endpoint 仍是 UDP Runtime 的唯一连接键；修改 endpoint 不改变 `device_id`。
- accessory 配置和会话以 `device_id` 为归属键，禁止使用“当前选中的 endpoint”作为后台任务的动态 owner。
- 电源监测和控制合并为同一个设备级串行会话；不保留并行的只读 socket 和控制 socket。
- 控制复用 `GwInstekPswAdapter` 的闭环语义，不复制一套简化 SCPI 写路径。
- `MainWindow` 只持有设备 accessory directory；每台设备的 Store、窗口和控制器由固定 bundle 创建并复用。

## 4. 配置与迁移

- `customer.devices[]` 增加稳定 `id`，并保存该设备的 `external_power` 与 `iperf` profile；规范化与原子持久化必须保留所有已知字段。
- 现有设备首次迁移时生成稳定 ID。只有一个客户设备时，把旧全局 `external_power`/`iperf` 配置迁移给该设备。
- 多设备升级时，把旧全局配置迁移给当前 `active_endpoint` 对应设备；其他设备保持明确的“未配置”。迁移一次完成并写入版本标记，不在以后启动时重复覆盖。
- iperf3 可执行文件路径属于运行本工具的主机，保留为全局设置；设备 profile 不重复保存二进制路径。
- IP 留空表示该设备未配置电源；不会连接、发送查询或控制命令。
- 电源端口固定为 `2268`；型号识别使用与 Production 相同的字母数字归一化，接受 `PSW80-27` 与 `PSW 80-27`，仍拒绝其他型号。

## 5. 用户界面

### 5.1 外接电源弹窗

- 从当前设备 Overview 底部“外接电源”进入；标题明确显示设备身份或 endpoint，防止控制错设备。
- 弹窗内包含电源 IPv4、固定端口、设定电压、设定电流、连接/重新识别、读取状态、应用参数并确认关闭、开启输出、关闭输出，以及 V/A/W 当前值和 30 分钟 V/A 曲线。
- 修改配置必须显式保存并完成连接代次切换；仅编辑文本、切换设备、关闭弹窗或应用重启都不得自动发送控制命令。
- 开启输出必须经过身份确认、设定值回读和输出前关闭确认；成功文案区分“指令已发送”“设定值已确认”“输出已确认”“电压已确认”。
- 关闭弹窗不改变输出。应用退出时若任一设备最后证据为输出开启，逐台列出并要求用户选择关闭输出或保持现状；无法确认关闭时不得显示“已关闭”。

### 5.2 Network test 页面

- Network test 改为与 Overview/RF/Maintenance 相同的设备级固定页面栈；页面标题显示绑定设备。
- 当前设备页面只读写当前设备的 iperf profile、Store 和会话；切换设备不停止其他设备测试。
- 设备列表显示后台网络测试状态，使用户无需逐页打开即可看到运行、降级、失败或完成。
- 电源未配置或暂时不可用时允许网络测试继续，但页面和证据明确标记“无对应功耗样本”，不得用其他设备样本补位。

## 6. 资源冲突与并发规则

- 同一个电源 IPv4:2268 同一时刻只允许归属一个客户设备；保存重复配置时直接阻止并指明占用设备。
- Customer 电源 owner 与 Production 电源租约使用同一个物理资源键。进入 Production 不再按“全局停止一个监测器”处理，而是先检查并释放所有客户电源会话；存在控制动作或无法安全释放时阻止切换。
- 每台设备最多一场活动 iperf3 测试；同一设备的 UL/DL 仍使用独立进程。
- 活动测试之间的 `(server, protocol, port)` 不得重复；发生冲突时第二场测试保持未启动并指出冲突设备、方向和端口。
- 多场测试可以绑定不同本地 IPv4。绑定同一本地 IPv4 虽可启动多个客户端，但无法保证链路与功耗归属，本轮按资源冲突阻止并行；停止冲突任务后可顺序执行。
- 应用退出时逐个停止全部 iperf3 控制器并完成证据收尾，再关闭各电源会话。

## 7. 记录和证据合同

- 客户 SDB 的 `external_power_sample/v1` 只写入同一 `device_id` 的活动录制，不再把一个全局样本广播给全部 endpoint。
- 每条电源事件增加 `device_id`、采样时 endpoint、电源身份和连接代次；设备 endpoint 后续修改不改变历史证据归属。
- iperf 会话目录和 `summary.json` 增加 `device_id`、启动时 endpoint、iperf profile、电源 profile 摘要和实际电源身份。
- 电源配置变更、控制意图、命令发送、回读确认、保护状态和失败均写入对应设备事件流。
- 软件通过只能证明 owner、状态、互斥和记录合同；各台真机的功耗一致性、网络路由和并发带宽仍需现场验收。

## 8. 实施顺序

1. 增加稳定设备 ID、accessory profile、迁移和原子持久化测试。
2. 将外接电源重构为设备级串行 monitor/control session，复用现有闭环 adapter，删除被取代的严格只读会话和全局状态源。
3. 把电源配置和控制加入设备级弹窗，移除 Customer 全局设置中的电源区域，修复型号归一化。
4. 将 iperf controller/store/page 改为设备固定 bundle，增加主机级资源仲裁和多控制器退出收尾。
5. 修正 SDB 与 iperf evidence 的设备绑定，增加无串台和迁移回归测试。
6. 更新翻译、`AGENTS.md`、开发记录，执行专项、全量测试和现场验收矩阵。

## 9. 本轮边界

- 包含：最多 4 台客户设备的独立电源配置/监测/控制、独立 iperf3 配置/运行/证据、受控并发和旧配置迁移。
- 不包含：一台物理电源同时绑定多台设备、一个 iperf 会话同时代表多台设备、任意 SCPI 控制台、其他电源品牌、ECS 服务端部署或自动分配服务端端口。
- 不改变设备 DEBUG/Product Service 协议，也不把外设状态放入 `DeviceSessionCore`。

## 10. 完成定义

- 四台设备可分别保存并恢复各自外设配置；切换、编辑 endpoint、重启应用后归属不串台。
- 每台设备的外接电源弹窗完成配置、实时状态、曲线和闭环控制；真机 `PSW80-27` 可识别。
- 每台设备的 Network test 页面只呈现自己的配置和会话；不冲突资源可并行，冲突资源被明确拒绝。
- SDB、iperf 会话和控制事件均可通过 `device_id + endpoint + 外设身份` 追溯。
- 被取代的全局 `external_power.*`、设备无关 `iperf.*` 业务状态和严格只读电源 owner 已完成迁移并从运行期删除。
- 定向测试、全量 pytest、翻译检查和 `git diff --check` 通过；真机多设备验收项单独记录。
