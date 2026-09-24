# M28 客户设备独立电源与 iperf3 开发记录

状态：软件实现与自动化回归完成；现场验收 BLOCKED

计划：`doc/M28_customer_device_accessories_plan.md`

验收标准：`doc/M28_customer_device_accessories_acceptance.md`

## 2026-09-24 基线核对

- Customer 最多支持 4 个 UDP endpoint，但持久化记录目前只有 `ip`、`port`，没有稳定设备 ID 或 accessory profile。
- 外接电源为 `MainWindow` 进程级单例，配置来自全局 `external_power.host`，样本广播给所有客户 bundle。
- iperf3 为 `MainWindow` 进程级单例，配置来自全局 `iperf.*`，Customer Network test 页面不绑定 endpoint；控制器明确拒绝第二场活动测试。
- 用户确认客户电源应可读可控，并提出把电源设置放入当前设备的外接电源弹窗，以支持多设备分别配置。
- 当前只创建计划、验收标准和本开发记录；尚未修改运行代码、翻译或测试。

## 2026-09-24 实施记录

- `customer.devices[]` 增加稳定 UUID、设备级 `external_power` 与 `iperf` profile；编辑 endpoint 保留 UUID 和外设归属，旧全局配置按单设备/活动设备规则一次迁移。
- Customer 全局设置页移除外接电源；Overview 外接电源弹窗增加设备标识、IPv4、固定 2268 端口、电压/电流设定、连接、状态读取、闭环应用、开启和关闭控制。
- 删除严格只读 PSW socket owner，监测和控制统一复用 `GwInstekPswAdapter` 的一个串行会话；身份比较统一为字母数字归一化，真机返回的 `PSW80-27` 与 `PSW 80-27` 等价。
- 每个 endpoint bundle 独立持有 Power Store/Monitor、iperf Store/Controller 和页面；电源样本只进入对应设备录制，iperf 证据目录按稳定设备 ID 分组并记录启动 endpoint 与电源身份。
- Network test 改为设备级页面；资源仲裁拒绝相同本地 IPv4或重叠服务端协议/端口的并行测试，不冲突的设备 controller 可独立运行。
- 进入 Production 前检查客户网络测试与已确认开启的电源；退出应用时遍历所有设备 controller，并对已确认开启的输出提供逐台关闭确认。
- 更新 `AGENTS.md`、TS/QM 和回归测试；经用户确认后纳入 Windows 发版提交。

## 软件验收

- PASS：`python3 scripts/update_translations.py check`，1303 条翻译全部完成。
- PASS：M28 专项回归覆盖配置迁移、稳定 ID、endpoint 编辑、重复电源 IP、型号归一化、串行控制动作、iperf 资源冲突、SDB 设备归属、设置页隔离和生命周期。
- PASS：`PYTHONPATH=. pytest -q satellite_debug_tool/tests`，最终 1376 项通过，用时 59.46 秒。
- PASS：修改的 Python 文件 `py_compile` 与 `git diff --check` 均通过。

## 现场验收

- BLOCKED：本轮没有接入两台客户设备与两台 PSW80-27，无法证明真机并行采样、闭环控制及无串台。
- BLOCKED：本轮没有使用实际 ECS 服务端端口和两条独立本地链路，无法证明现场路由、吞吐、并发带宽及功耗一致性。
