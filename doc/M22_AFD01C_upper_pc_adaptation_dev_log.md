# M22 AFD01C 上位机适配开发记录

对应计划：`doc/M22_AFD01C_upper_pc_adaptation_plan.md`

验收标准：`doc/M22_AFD01C_upper_pc_adaptation_acceptance.md`

## 2026-08-29：实施基线

- 用户已确认按 M22 计划实施；3D 模型明确排除，等待结构同事提供 STL 和坐标基准。
- 上位机基线为 `17cdd15`，工作区已有未提交的 ESA01/Product Service 改动。本轮保留这些改动，不提交、不推送。
- 设备端核对基线为 `fc6ec326`：AFD01C 上报独立产品身份 `AFD01C`，Product Service schema 1 / protocol 8，镜像兼容身份与 AFD01 不同。
- 已证明根因：客户策略与 Fleet 精确写死 `afd01`；Fleet 将支持型号但 SN 缺失错误归为 unsupported；客户 OTA 写死 `expected_product="AFD01"`；试产 recipe 仅允许 `afd01`。

## 2026-08-29：软件实现

- 在产品领域建立单一显式支持策略：AFD01、AFD01C、ESA01 分别保留自己的 Product Identity 与协议范围；AFD01C 只接受 protocol 8，不使用前缀推断。
- 客户产品服务将 Debug `hw_type` 与 Product Identity 精确对齐后才进入 ready；射频、TX、安装姿态及 applied readback 的既有能力/状态门禁保持不变。
- Fleet 新增 `IDENTITY_PENDING`，将“已支持型号但 SN 未配置”与未知硬件分开；保留设备上报的有效零 SNR，不伪造 SN。
- recipe schema 1 接受 `afd01`/`afd01c`，批次注册与启动均按 Product Identity 和 `recipe.product` 精确隔离，缺 recipe、缺 SN、冲突或混插时 fail-close。
- 客户 OTA 从当前连接代际的权威身份派生期望 product/hardware；AFD01 与 AFD01C 包双向交叉选择均被拒绝，身份变化或断线会清除已选包。
- 签名包工具与说明支持 `--product AFD01C`，默认硬件白名单为 `afd01c`。
- 未新增 `afd01c.stl`，模型解析只查找独立 `afd01c.stl`；当前缺失时沿用占位呈现，不别名复用 AFD01 模型。
- 更新中英文用户说明与 8 条新增中文界面翻译。

## 2026-08-29：自动化验收

- 定向回归：`97 passed in 1.83s`。
- 全量回归：`1090 passed in 37.77s`。
- 翻译目录：`Translation catalog OK: 910 messages`。
- 变更格式：`git diff --check` 通过。
- 工作区原有 ESA01/Product Service 未提交改动均保留；本轮未 commit、未 push。

## 待真机与外部输入闭环

- AFD01C UDP 4004 的 identity/FAST/SLOW/capability 连续性、断线重连与 SN 配置前后状态需要真机验证。
- MANUAL、频点/极化 applied、TX fail-close 需要真机与频谱仪/功率计证据；软件发送成功不等于物理发射成立。
- AFD01C 同型号 OTA 需要审批后的签名测试包与受信 key；当前只完成软件合同和双向交叉拒绝回归。
- Windows 125%/150% DPI、正式发行包、KaTR003B 标定、阵面能力和正式试产 recipe 仍待对应责任方验收。
- 3D 模型继续等待结构同事提供 STL 及 up/nose/left 坐标约定，本轮不处理外观验收。

## 2026-08-30：试产录制状态文案收口

- 将槽位中的“未武装/已武装”改为按流程原因呈现：“批次尚未创建”“证据录制中”“证据录制未就绪”。
- 同步移除开始门禁提示中的“武装”术语；状态事实和 SDB 录制门禁不变。
- 试产定向回归 `35 passed`，全量回归 `1090 passed`，翻译检查 `911 messages`，`git diff --check` 通过。

## 2026-08-30：全盘 review 与提交边界更新

- 用户明确要求全盘 review，并将当前本地修改全部提交；该授权包含本轮开始前已存在且在实现中保留的 ESA01/Product Service 修改。
- 本次授权只允许本地 commit，不包含 push、tag、发布包或正式发版。
- 最终提交仍以前置的全量测试、翻译目录检查、`git diff --check` 和 staged diff 审查通过为条件；真机/RF/平台验收继续保持未完成。

## 2026-08-31：全盘 review 的状态模型收敛

- 将用户提出的“根因修复、单一正确模型、肯定式事实、每修复必有回归”固化到项目 `AGENTS.md`，并将 CLAUDE/Copilot 说明收口为指向该权威入口的薄层。
- 产品支持收敛为 AFD01/AFD01C/ESA01 单一显式注册表，客户工作台、试产 recipe 和客户 OTA 共用同一事实；未知产品和未注册协议保持 fail-close。
- Product Service 结果码 `0` 重命名为 `ACCEPTED`，只表示请求已接受；控制器继续以更新且匹配的 FAST_STATE/capability 回读确认 applied。
- `DeviceSessionCore` 作为唯一连接代际、端点、身份和设备事务 owner；参数、Product 控制、TX、mount 与 OTA 都在会话改变时终止旧操作，界面直接呈现取消原因。
- 客户 OTA 改为类型化不可变 artifact：已验签 token 与工程 raw BIN 分离，活跃传输期间不可替换；只消费当前阶段且序号精确匹配的响应。Debug/Product 序列号或固件事实冲突、缺失不可变身份或会话变化均拒绝启动。
- `OTA_END=VERIFIED` 只记录传输校验被接受；WAIT_REBOOT 内要求传输前捕获的 Debug SN、Product SN、UID 全部在同 endpoint 重新匹配，传输前捕获的 Debug/Product 固件来源也全部重新回报、语义一致并命中签名包目标版本。同版本包只显示“应用未独立确认”，WAIT_REBOOT 可本地取消且不发送失效帧。
- 签名包解析增加 ZIP 异常归一、解压数量/大小上限、跨平台安全文件名、manifest 版本语法、受信 key 身份与 32-byte Ed25519 公钥一致性校验。
- 原始 OTA 的分片容量收口到 `u16 seq` 的协议上限：512 B 分片最大 33,554,432 B；选包阶段拒绝超限镜像，编码越界也会终止事务并释放会话租约。
- 相邻入口 review 进一步修复：参数错误响应上下文和 VERIFYING 读回发送失败；mount 重启返回后事务重获取；mount valid bit 缺失时的“不可用”呈现；recipe 产品规范化；试产占位 SN 拒绝；异常路径的直接证据录制原因。
- 参数控制器的断线与连接代际变化复用同一会话终止路径；活动读写会发布“会话已变化”终态，重连后同名参数不再重现旧会话的等待状态。
- READY 批次后接入的设备也由 Fleet 维护同一录制前置条件；首次创建失败时启动门保持关闭，后续设备数据驱动重试，录制就绪后再放行，不再留下永久无法启动的 READY 批次。
- 最终定向回归 `307 passed`；全量 `1190 passed in 39.92s`；翻译目录 `929 messages`；`git diff --check` 通过。软件验收完成，真机/RF/Windows/签名发布包/3D 边界保持未完成。

## 2026-08-31：AFD01C TX 回读缺失的显示修正

- 用户观察到最终 TX gate 已开启，但客户总览显示“发射：不支持”。链路核对证明 AFD01C `SERVICE_CAPABILITIES` 已声明 TX control；设备端 `transceiver_get_product_status()` 仍固定输出 `tx_enabled=0`、`tx_output_feedback_available=0`，所以 FAST/SLOW 均不置 TX valid bit。最终 gate 状态与物理 RF 输出继续作为两项不同证据。
- 上位机 codec 没有丢字段；`ProductServiceStore` 按 valid bit 正确拒绝把 payload 中的无效零提升为回读事实。错误只在最终文案：字段缺失被通用 placeholder 写成了“不支持”。
- 客户总览现在同时检查权威 capability 与 operation readback：TX control 已明确支持而状态 valid bit 缺失时显示“设备回读不可用”。它不会用 accepted、command-sent 或操作者观察补造“开”。
- 固件本轮已把有效 TX gate readback 接入 `transceiver_product_status_t.tx_enabled/tx_gate_readback_available`，Product Service 据此置 FAST bit5 / SLOW bit7。该状态只证明 gate GPIO 读回，不替代频谱仪或功率计的物理 RF 验收；真机界面联调仍待执行。
- 定向执行客户总览测试 `21 passed`；翻译目录仍为 `929 messages` 且检查通过；`git diff --check` 通过。未运行无关全量回归。
