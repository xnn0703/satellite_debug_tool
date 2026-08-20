# M19.1 AFD01 设备身份与唯一 MAC 计划

日期：2026-08-09

## 目标

- AFD01 在下线测试前通过 Shell 写入生产序列后缀，例如 `202607N001`。
- 对外完整设备序列号统一由 `DeviceType + "-" + dev_sn` 生成，例如 `AFD01-202607N001`。
- `dev_sn` 对 Debug/Product Service/上位机只读，参数恢复不得清除已写入的生产身份。
- bootloader 与 application 使用同一个 MCU UID 派生算法生成稳定、单播、本地管理的 W5500 MAC。
- Product Service 上报 MCU UID、完整 SN 和当前 MAC；批测试会话以 UID 为不可变主键，以 SN 为操作员可读标识。

## 基线

- 上位机：`PYTHONPATH=. pytest satellite_debug_tool/tests -q`，`725 passed, 4 warnings`。
- AFD01：`./build.sh afd01 app debug` 与 `./build.sh afd01 boot debug` 均成功。
- 当前 AFD01 W5500 MAC 仍硬编码为 `00:08:DC:12:34:56`；当前 Product Service 的 SN 是 MCU UID 拼接出的临时值。
- 上位机仓库已有 M19 批测工作区未提交修改，本阶段只叠加身份相关改动，不回退现有工作。

## 设备端设计

### FRAM 兼容

- AFD01 参数布局升为 v2：`dev_sn[11]` 在 boot/app 共享区物理紧随 `DeviceType[16]`，固定 offset 16。
- `mac[6]` 物理紧随 `dns[4]`，固定 offset 44；后续依次为 `dhcp` 和 `dbg_port`。
- 新增字段物理位于共享区；为保持总尺寸不变，从原 21 字节 App 预留容量中扣除 17 字节，剩余 `app_layout_reserved[4]`，`sizeof(AFD01_DEV_ATTR)` 仍为 116 字节。
- boot 与 app 共用同一份 v1 -> v2 迁移：按旧布局读取所有字段，保留旧网络/App 参数和旧预留区内格式合法的试用版 `dev_sn`，再写入 v2 版本号与新布局。
- v1 数据没有合法 `dev_sn` 时迁移为空；`mac` 无条件由当前 MCU UID 重新派生。
- 新布局改变了 boot 共享字段 offset，首次部署必须同时更新 boot 与 app；不支持旧 boot/new app 或新 boot/旧 app 混用。

### SN 规则

- `dev_sn` 必须为 10 个 ASCII 字符：六位年月 `YYYYMM`、字母 `N`、三位流水号。
- 月份限制 `01..12`，流水号限制 `001..999`。
- Shell 写入命令：`para dev_sn 202607N001`。
- Debug 参数表可读取 `dev_sn`，但 `PARA_SET` 必须返回只读错误。
- META_INFO 和 SERVICE_IDENTITY 只发送完整 SN；未写 SN 时保持空值并标记无效，不再伪装为生产 SN。

### MAC 规则

- 使用 `HAL_GetUIDw0/1/2()` 的 96-bit UID，按固定小端字节序与命名空间 `SOFTHZ/AFD01/MAC/V1` 输入 FNV-1a 64。
- 取低 48 bit，并执行 `mac[0] = (mac[0] & 0xFC) | 0x02`，保证单播和本地管理属性。
- 派生结果保存到共享字段 `mac[6]`；每次启动用 MCU UID 校正，发现 FRAM 值不一致时覆盖并持久化。
- MAC 不允许 Shell 或上位机修改，复制 FRAM 也不能把另一台设备的 MAC 带过来。
- `para ls` 和 Product Service 均可只读查看 MAC。
- 以固定 UID/MAC 测试向量锁定算法；boot/app 共用同一实现。

### 参数权限与恢复

- ParaEntry 增加只读、远程只读和可选校验器元数据。
- `DeviceType`、`dev_sn` 对 Debug 远程写入只读；MAC 对所有写入只读。
- AFD01 参数恢复回到编译默认值，但保留合法 `dev_sn`；UID/MAC 无需恢复。

## 协议与上位机

- 新增可选 `SERVICE_HARDWARE_IDENTITY` 记录，固定上报 UID、MAC 和 MAC 派生版本，不修改现有 SERVICE_IDENTITY 帧布局。
- 上位机对旧固件保持兼容：没有硬件身份记录时仍可连接，但生产批次按配方决定是否阻止开始。
- ProductStore/SDB/SQLite 保存 `device_uid`、`serial_number`、`mac_address` 和 `mac_source`。
- DeviceSession 使用 UID 作为首选身份键；旧固件仅在 UID 缺失时回退 SN。
- 同 UID 不同 SN、同 SN 不同 UID、重复 MAC 均形成显式冲突并阻止批次开始。

## 实施顺序

1. 参数表权限、验证、共享布局 v2 及 boot/app 共用的 v1 数据迁移。
2. 公共 UID/MAC 派生模块及 boot/app 接入。
3. AFD01 META/Product Service 正式 SN 与硬件身份上报。
4. 上位机协议、Store、SDB/SQLite 和 DeviceSession 身份绑定。
5. 协议文档、Host/pytest、四组固件构建和硬件验收。

## 非目标

- 本阶段不自动分配 IP；IP 继续由下线 Shell 流程写入。
- 不把 MCU UID 当作密钥或安全认证凭据。
- 不修改 ESA01。
- 不自动提交 Git。
