# M29 AFD01A / AFD01B2 上位机接入计划

状态：用户已确认；上位机软件实现与自动化验证完成，A/B2 真机、RF、OTA 安装及 Windows 验收待执行。

验收标准：`doc/M29_AFD01A_B2_upper_pc_acceptance.md`。

实施时维护：`doc/M29_AFD01A_B2_upper_pc_dev_log.md`。

## 1. 已核对的事实

- 用户明确：AFD01A 是原 AFD01 的后续出货名称，目的是区分型号；本轮目标为 AFD01A、AFD01B2，没有独立的 AFD01B。
- 本仓当前只注册 `afd01`、`afd01c`、`esa01` 客户产品；试产产品选择仅列出 AFD01、AFD01C。旧 `afd01` 需继续服务已出货设备。
- 相邻设备仓当前分别构建 `afd01a_app/bootloader` 和 `afd01b2_app/bootloader`。A 的 Debug 项目键 / Product Identity / 硬件兼容 ID 是 `afd01a` / `AFD01A` / `0x41464403`；B2 是 `afd01b2` / `AFD01B2` / `0x41464404`。两者 Product Service 均为 protocol 8，镜像身份各自独立，也与 C 独立。
- AFD01A 绑定原 AFD01 的 KauDC003A 和 KA256 legacy，可显式复用现有 AFD01 几何与姿态约定。B2 绑定 KaTR003B2 与 KA256 V2，尚无独立验收的 3D 资产。
- 设备仓 A/B2 改动目前尚未提交，软件构建验收已有记录；上位机接入需要以实际设备回报或固定的设备端合同测试再次核对身份。实板、RF、Windows 与 OTA 安装/回滚仍是独立验收。

## 2. 目标与范围

1. 按权威身份把 AFD01A、AFD01B2 接入客户 Product Service、能力驱动的 RF/安装姿态控制、维护页和多设备会话。
2. 两种新型号在试产工作区各自识别、显示和配置产品模板及 recipe；只有有效 SN、型号匹配和现有门禁全部成立才可入批。
3. 客户 OTA 分别匹配 `AFD01A/afd01a` 和 `AFD01B2/afd01b2` 的验签包；旧 `AFD01/afd01`、A、B2、C 保持独立包身份，交叉选择被拒绝。
4. AFD01A 的客户呈现复用原 AFD01 的几何和已确认业务能力合同；旧设备身份与用户配置继续有效。B2 未取得结构模型前保持占位模型。

本轮仅改上位机及其文档、测试、翻译；不修改设备端固件，不推断 A/B2 正式试产阈值，不生成签名私钥，不复制 AFD01 或 AFD01C 的 3D 模型作为 B2 模型，不执行提交、推送或发版。

## 3. 根因与实现路径

1. 在 `core/product/support_policy.py` 的单一产品注册表中为 A/B2 定义独立 `afd01a` / `AFD01A` 和 `afd01b2` / `AFD01B2` 条目，均只接受已核对的 protocol 8。旧 `afd01` 保留其现有身份和协议范围；客户服务、OTA、Fleet 和 recipe 复用注册事实，不增加前缀匹配。
2. 核对 Debug Meta、Product Identity、能力、FAST/SLOW 遥测、连接代际的状态流。客户控制维持在线、MANUAL 回读、有效能力位、事务租约和更新遥测匹配门禁；UI 仅呈现权威状态。
3. 试产产品选择由注册产品策略生成，避免新增 A/B2 后注册表与 UI 选项分叉。SN 缺失、身份冲突、混插、recipe 不匹配及证据收尾沿用当前领域状态机。不给新型号预置未经批准的正式 recipe。
4. 客户 OTA 使用各自注册策略精确校验签名 manifest、当前 Debug/Product 身份和 artifact token。扩展包制作说明及交叉拒绝测试。新 A 镜像有独立硬件 ID，不能因外形和业务相同而接受旧 AFD01 包。
5. AFD01A 在模型资源层显式复用已验收 `afd01.stl`，保留 A 专属用户模型覆盖优先级；B2 使用独立模型键和占位呈现。界面文案变更后运行翻译 update/check。
6. 对照本计划维护开发记录，完成类型化回归、全量 pytest、翻译检查、`git diff --check` 和软件 review；真机项目单列 BLOCKED/PASS。

## 4. 验收边界

- Host 回归需在旧实现稳定复现 A/B2 客户准入失败及试产不识别，新实现通过；同时覆盖旧 AFD01、AFD01C、ESA01 与未知产品。
- 旧 AFD01 与新 AFD01A 共享已确认的产品使用方式，但型号、设备身份、recipe 和 OTA 包保持精确匹配。既有旧设备配置不自动改写为 A。
- UDP 指令发送、设备接受、更新遥测证明应用、物理 RF 输出分别记录。软件测试不代替 A/B2 实板、OTA 安装/回滚和 RF 仪表验收。
