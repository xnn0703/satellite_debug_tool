# M19.1 AFD01 设备身份开发日志

## 2026-08-09：基线与设计审查

- 用户确认 FRAM 仅保存序列后缀，例如 `202607N001`；完整 SN 由设备型号与后缀组合。
- 当前源码审查发现 W5500 MAC 仍固定为 `00:08:DC:12:34:56`，当前 Product Service SN 仍为 MCU UID 临时字符串。
- 参数系统按 `sizeof(AFD01_DEV_ATTR)` 原样读写 FRAM；直接在 `DeviceType` 后插入字段会破坏 boot 锁定偏移和现有参数。
- 决定复用 21 字节未使用 App 预留区，拆为 `dev_sn[11] + reserved[10]`，参数表中逻辑放在 DeviceType 后。
- MAC 采用 UID 派生、每次启动计算、全接口只读，不把可复制的 MAC 值作为 FRAM 配置。
- 上位机基线：`725 passed, 4 warnings`。
- AFD01 app debug 与 boot debug 基线构建成功；现有 boot 构建包含历史 warning，后续不得新增身份相关 warning。

## 2026-08-09：实施与软件验证

- AFD01 参数表新增逻辑项 `dev_sn` 和只读 `MAC`。`dev_sn` 物理复用历史 21 字节预留区中的 11 字节，结构总长保持 116 字节，`nav_gnss_source` 仍在 offset 68。
- `dev_sn` 严格使用 `YYYYMMNddd`，例如 `202607N001`；FRAM 不保存型号前缀，对外 SN 统一组合为 `DeviceType-dev_sn`。未配置时 Product Service 不设置 SN 有效位。
- Shell 可写 `dev_sn`，Debug 参数表只读；`DeviceType` 对上位机只读，UID 派生 `MAC` 对 Shell 和上位机均只读。`para reset` 保留合法 `dev_sn`。
- 新增 boot/app 共用的 UID 身份模块：以 STM32 96-bit UID、固定命名空间和 FNV-1a64 派生本地管理单播 MAC。完整 UID 同时上报，批量测试发现重复 UID、SN、MAC 或绑定冲突时阻止继续。
- Product Service 协议升至 5，新增 `SERVICE_HARDWARE_IDENTITY (0x29)`；上位机协议、ProductStore、客户维护页、SDB metadata、Fleet 和 SQLite schema v2 已同步。
- 批量会话优先以 UID 续接，旧固件缺少 UID 时回退完整 SN；归档目录优先使用完整 SN，未配置时回退 UID。

### 验证结果

- 固件身份主机测试：`1/1 passed`，覆盖固定向量 `12345678/9ABCDEF0/0BADBEEF -> 4A:65:A6:99:9E:4B`、位属性、SN 校验和 128 个 UID 去重样本。
- 上位机身份/Fleet/SQLite 定向测试：`44 passed`；全量回归：`730 passed, 4 warnings in 39.66s`。warning 均为既有 `datetime.utcnow()` 弃用提示。
- 翻译目录：`641 messages`，TS/QM 同步，无 unfinished、空翻译或占位符错误。
- AFD01 app debug：FLASH `708072 B / 768 KB (90.04%)`，RAM `423160 B / 512 KB (80.71%)`；相对基线增加 2880 B FLASH、256 B RAM。
- AFD01 boot debug：FLASH `193124 B / 256 KB (73.67%)`，RAM `234168 B / 512 KB (44.66%)`；相对基线增加 1856 B FLASH、280 B RAM。
- AFD01 app release：FLASH `644432 B / 768 KB (81.94%)`，RAM `417208 B / 512 KB (79.58%)`。
- AFD01 boot release：FLASH `190604 B / 256 KB (72.71%)`，RAM `234168 B / 512 KB (44.66%)`。
- 四机并网、Shell 持久化、boot/app 同 MAC、原始远程写入拒绝和跨工位工程续接仍需实机验收，未在验收表中勾选。

## 2026-08-09：共享布局 v2 设计修订

- 用户确认 `dev_sn` 必须物理位于 boot/app 共享区并紧随 `DeviceType[16]`，`mac[6]` 必须紧随 `dns[4]`；此前“复用 App 预留区且不改变 offset”的方案作废。
- 新布局固定 `dev_sn` offset 16、`mac` offset 44，总尺寸继续保持 116 字节；新增的 17 字节由原 `app_layout_reserved[21]` 抵消，剩余 4 字节。
- 由于 `Login/IP/子网/网关/DNS/DHCP/Debug_Port` 等历史偏移改变，参数布局版本升为 2，并在参数系统增加 boot/app 共用迁移入口。
- 迁移按 v1 结构逐字段复制，保留网络和 App 参数；试用版固件若已把合法 `dev_sn` 写到旧预留区，也会迁入新字段。
- MAC 保存在共享字段但保持只读，每次启动由 MCU UID 校正；FRAM 克隆不会造成两台设备使用同一 MAC。
- 首次部署要求新版 boot 与新版 app 配套烧录，明确不支持新旧 boot/app 混用。

### 布局 v2 软件验证

- 新增 `ParaAttrMigrate` 与 `ParaAttrPostLoad` 可选钩子；未注册钩子的其它设备沿用原初始化行为。
- AFD01 v1 迁移按旧 116 字节结构逐字段复制；主机测试同时覆盖独立缓冲区和真实启动使用的原地迁移、合法/非法旧 SN、错误版本拒绝及 MAC 校正。
- 身份/布局主机测试：`1/1 passed`。
- 上位机全量回归：`730 passed, 4 warnings in 44.80s`；warning 均为既有 `datetime.utcnow()` 弃用提示。
- AFD01 app debug：FLASH `708800 B / 768 KB (90.13%)`，RAM `423192 B / 512 KB (80.72%)`。
- AFD01 boot debug：FLASH `194276 B / 256 KB (74.11%)`，RAM `234184 B / 512 KB (44.67%)`。
- AFD01 app release：FLASH `645152 B / 768 KB (82.04%)`，RAM `417240 B / 512 KB (79.58%)`。
- AFD01 boot release：FLASH `191772 B / 256 KB (73.16%)`，RAM `234184 B / 512 KB (44.67%)`。
- 四组 AFD01 构建均成功；共享参数组件回归构建 `demo_app`、`ufd45_app`、`ka_rf_unit_app` 也全部成功，未注册迁移钩子的目标保持原行为。
- 新 `afd01_para_layout.c` 未产生编译 warning；构建日志中的 `LOG_TAG`、newlib syscall 和既有 unused/double-promotion warning 为原工程遗留。
- 旧 FRAM 实机迁移、断电重启持久化和 boot/app 同 MAC 仍保留为硬件验收项。
