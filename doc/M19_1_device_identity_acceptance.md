# M19.1 AFD01 设备身份验收标准

日期：2026-08-09

## 静态与单元测试

- [x] `sizeof(AFD01_DEV_ATTR) == 116`，`dev_sn == offset 16`，`mac == offset 44`，boot/app 使用同一头文件与布局版本。
- [x] v1 -> v2 迁移保留 DeviceType、网络参数、App 参数和格式合法的试用版 `dev_sn`；参数系统成功后写入布局版本 2。
- [x] v1 数据预留区无合法 SN 时迁移为空，不把随机字节误识别为生产 SN。
- [x] `dev_sn` 可容纳 10 字符后缀和结尾 `\0`，物理及参数表顺序均位于 `DeviceType` 后。
- [x] 合法 `202607N001` 可写入；月份、流水号、字符或长度非法时在复制前拒绝。
- [x] Debug 参数表能读取 `dev_sn`，远程设置 `DeviceType/dev_sn/MAC` 被设备端拒绝。
- [x] 参数恢复路径保留已写入且格式合法的 `dev_sn`。
- [x] 固定 UID 测试向量生成固定 MAC；MAC 的 multicast bit 为 0、local bit 为 1。
- [x] 装载后共享字段 `mac[6]` 与 UID 派生值不一致会被校正并触发写回，W5500 使用该共享字段。
- [x] 128 个测试 UID 无重复 MAC，boot/app 链接同一公共实现。
- [x] SERVICE_IDENTITY 使用 `DeviceType-dev_sn`，未配置时不声称有效 SN。
- [x] SERVICE_HARDWARE_IDENTITY 可解码 UID、MAC 和派生版本，旧协议记录仍可解码。
- [x] DeviceSession UID 优先、SN 回退；UID/SN/MAC 冲突均有测试。

## 软件验证

- [x] `PYTHONPATH=. pytest satellite_debug_tool/tests -q`：`730 passed, 4 warnings`，warning 均为既有弃用提示。
- [x] `./build.sh afd01 app debug` 成功。
- [x] `./build.sh afd01 app release` 成功。
- [x] `./build.sh afd01 boot debug` 成功。
- [x] `./build.sh afd01 boot release` 成功。
- [x] 共享参数组件回归构建 `demo_app`、`ufd45_app`、`ka_rf_unit_app` 成功。
- [x] 新增身份源文件无新增编译 warning；资源结果已记录在开发日志。

## 硬件验收

- [ ] Shell 执行 `para dev_sn 202607N001` 后重启仍保持。
- [ ] `para ls` 显示 `DeviceType=AFD01`、`dev_sn=202607N001` 和只读派生 MAC。
- [ ] 上位机显示完整 SN `AFD01-202607N001`、96-bit UID 和 MAC。
- [ ] 上位机参数页不能编辑 `dev_sn` 和 MAC，构造原始 PARA_SET 也被设备拒绝。
- [ ] 恢复参数后 SN 保留，网络等普通参数恢复默认。
- [ ] 同一设备 bootloader 与 application 阶段 MAC 完全一致。
- [ ] 旧 v1 FRAM 数据配套烧录新版 boot/app 后网络地址及业务参数保持；首次启动完成一次性 v2 迁移。
- [ ] 四台设备同时上电时 UID、SN、MAC 均不同，不出现交换机 MAC 漂移或随机丢包。
- [ ] 四台设备仍使用各自下线配置的唯一 IP；唯一 MAC 不替代 IP 配置。
- [ ] 导出/导入批次后按 UID 续接，IP 改变不影响；SN/UID 绑定冲突阻止继续。

## 交付边界

- 软件测试通过仅代表源码和主机验证完成。
- 四机并网、boot/app 同 MAC、Shell 持久化与批次移交必须在实机完成后才能标记硬件验收。
- 布局 v2 必须配套更新 boot 和 app；旧 boot/new app 或新 boot/旧 app 的混合版本不属于兼容范围。
