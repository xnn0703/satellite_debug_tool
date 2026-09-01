# M22 AFD01C 上位机适配验收标准

状态：软件验收完成；G 章真机与平台验收待执行

对应计划：`doc/M22_AFD01C_upper_pc_adaptation_plan.md`

## A. 产品身份与兼容策略

- [x] AFD01C 以独立 `hw_type=afd01c`、Product Identity `AFD01C` 注册，不伪装成 AFD01。
- [x] AFD01C 仅在 Product Service protocol 8 时受支持；未知协议保持不支持。
- [x] Debug hw_type 与 Product Identity model 不一致时客户控制和 OTA fail-close；试产按 Product Identity 与 recipe.product 精确匹配后入批。
- [x] 未注册型号不会因名称前缀或共享能力被推断为支持。
- [x] AFD01 protocol 2..8、ESA01 protocol 6 和未知设备既有策略不回归。

## B. 客户工作区

- [x] AFD01C 在线且 FAST_STATE/能力有效时显示“产品服务在线”，不再停留在“等待产品服务”。
- [x] 自动/手动、RF 参数和 TX 控件仍分别受在线、MANUAL 回读、能力 valid mask、独立极化、事务占用和 pending 状态约束。
- [x] APPLY/SET 的发送、accepted、applied readback 与物理 RF 证据没有被合并。
- [x] protocol 8 且 mount capability 有效才允许安装姿态编辑；缺能力时保持禁用并显示明确原因。
- [x] 设备未上报有效 SN 时继续显示“不支持/—”，上位机不生成或借用 AFD01 SN。
- [x] 设备将 SNR `0.0` 标为有效时原样显示；valid bit 缺失或数据 stale 时才显示不可用/陈旧。
- [x] 设备已声明 TX 控制能力、但 TX 状态 valid bit 缺失时显示“设备回读不可用”，不误写成“不支持”，也不推导为“开”。

## C. 试产工作区

- [x] AFD01C 型号有效但 SN 缺失时显示“身份待配置”或等价肯定式状态，不显示“不支持的硬件”。
- [x] 身份待配置设备可显示 endpoint、型号和有效 SNR，但不能加入批次或开始测试。
- [x] AFD01C 型号和有效 SN 到达后进入 `IDENTIFIED`；未知型号进入 `UNSUPPORTED`。
- [x] SN、UID、MAC 冲突仍进入 `CONFLICT`，不得被型号适配绕过。
- [x] recipe schema 1 接受 `product=afd01c`，并继续接受 `product=afd01`。
- [x] AFD01C recipe 只允许 AFD01C 参与；AFD01 recipe 只允许 AFD01 参与；混插设备有明确拒绝证据。
- [x] 未选择 recipe、产品不匹配、SN 缺失或身份冲突时批次启动门禁保持关闭。
- [x] 未提供经确认的 AFD01C recipe 时不声明正式试产放行。

## D. OTA 安全

- [x] AFD01C 只接受签名 manifest 中 `product=AFD01C` 且 `hardware_types` 包含 `afd01c` 的包。
- [x] AFD01 只接受 AFD01 包；A/C 两个方向的交叉包均稳定拒绝为产品或硬件不匹配。
- [x] 身份缺失、身份不一致、Debug/Product 序列号或固件事实冲突、未知产品、断线或连接代际变化时不能载入或启动客户 OTA。
- [x] Ed25519、SHA-256、大小、版本策略和受信 key 校验保持不变。
- [x] 工程 raw BIN 与客户签名包使用不同 artifact 类型；替换选择会使旧 token 失效，活跃传输中不能替换字节。
- [x] 参数、Debug、错误分片序号或其他阶段的响应不能被当前 OTA 阶段误消费。
- [x] `OTA_END=VERIFIED` 不作为应用成功证据；WAIT_REBOOT 窗口内只有同 endpoint 返回且传输前已捕获的每项 Debug SN、Product SN、UID 都重新匹配，每个 Debug/Product 固件来源也都重新回报、语义一致并匹配签名包目标版本，才显示目标固件已确认。
- [x] 不同 endpoint、返回身份变化或目标版本未回读时明确失败；同版本包显示“应用未独立确认”。
- [x] WAIT_REBOOT 可以本地取消，不向重启中设备发送失效的 OTA_ABORT 帧。

## E. 3D 模型边界

- [x] 本轮不新增 `afd01c.stl`，不把 AFD01 STL 静默映射给 AFD01C。
- [x] 未安装用户自定义 AFD01C 模型时继续使用占位模型且界面不崩溃。
- [ ] 后续模型适配需要结构同事提供 STL、up/nose/left 坐标约定和外观验收。

## F. 自动化验证

- [x] 新增 AFD01C 客户策略、RF/安装姿态、Fleet 身份分类、recipe 产品隔离、响应上下文、会话事务和 OTA 身份/目标证据回归。
- [x] 最终定向回归通过（307 passed）。
- [x] `PYTHONPATH=. pytest -q satellite_debug_tool/tests` 全量通过（1190 passed），无固定允许失败 baseline。
- [x] `python3 scripts/update_translations.py update` 后 `check` 通过（929 messages），TS/QM 同步。
- [x] `git diff --check` 通过；实现保留既有 ESA01/Product Service 工作。2026-08-30 用户已明确授权全盘 review 后提交当前本地修改；未授权 push、tag 或发版。

## G. 真机与发布边界

- [ ] AFD01C UDP 4004 实测 Product Identity、protocol 8、FAST/SLOW/capabilities 连续更新，重连后状态正确恢复。
- [ ] 未配置 SN 与配置有效 SN 两种状态均按 C 章显示和门禁。
- [ ] RF 控制实测至少覆盖 MANUAL 回读、频点/极化 applied 回读、TX fail-close；频谱仪/功率计证据单独记录。
- [x] 设备端将 TX gate GPIO 读回写入 FAST/SLOW `tx_enabled`，并只在读回有效时置对应 valid bit。
- [ ] 真机确认客户总览随 gate GPIO 读回显示“开/关”；软件链路完成不替代该项实板验收。
- [ ] 使用审批后的 AFD01C 签名测试包验证同型号接受与 A/C 交叉拒绝；没有受信 key/测试包时本项保持未完成。
- [ ] Windows 125%/150% DPI 与目标发行包完成平台验证。
- [ ] KaTR003B 标定、阵面能力、正式 AFD01C recipe、3D 模型和正式量产放行均未被本 M22 软件验收代替。
