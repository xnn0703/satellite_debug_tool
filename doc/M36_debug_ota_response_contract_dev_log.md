# M36 DEBUG OTA 响应合同迁移开发记录

## 1. 根因证据

- 上位机 `OtaController` 只接受 `OTA_BEGIN=READY`、`OTA_DATA=<seq>`、`OTA_END=VERIFIED`。
- AFD01C `0.0.130_beta` 的 `debug_ota.c` 实际返回 `OK`、`dup OK`、`OK, rebooting`。
- 用户现场在固件包完成解密和版本准入后停在 0%，最终报告 `OTA_BEGIN timed out`。
- 两端命令号和负载格式一致，失败发生在响应上下文合同。

## 2. 实施

- AFD01 系列 App 版本提升为 `0.0.131_beta`。
- 固件 BEGIN、DATA、END 的成功与失败响应全部带阶段上下文；DATA 始终携带实际序号。
- 修正 `expected_seq == 0` 时 `65535` 被无符号回绕判为重复分片的问题。
- 上位机只对已认证解密的 AFD01 系列客户包且当前设备版本低于 `0.0.131_beta` 启用旧响应合同。
- 工程 raw BIN、未知产品、未知版本以及 `0.0.131_beta` 及以后保持严格合同。
- 应用日志增加 OTA 开始、发送阶段、分片序号、设备响应和最终状态记录。

## 3. 验证记录

- AFD01C `0.0.131_beta` Release App 构建通过，固件大小 634932 B，FLASH 使用率 80.95%。
- AFD01C Boot `0.0.3_beta` + App `0.0.131_beta` factory HEX/BIN 与 App-only `.sfpkg` 生成成功。
- `.sfpkg` 使用内置密钥离线解密和验包通过：产品 AFD01C、策略 `allow_downgrade`、
  固件 784392 B、SHA-256 `b9e5c5f3bd59cacecda40f264cea6ebac7d282f0e8ac29d9095bc47a963e3875`。
- 上位机相关测试 151 项通过，全量测试 1481 项通过。
- YMODEM 对齐检查通过；Host 可执行测试被本机 macOS 27 SDK 的 `libSystem.tbd` 解析错误阻断，
  AFD01C 目标构建已成功编译 YMODEM 及共享安装链路。

## 4. 实板边界

- 软件证据证明响应合同、版本边界、包完整性和构建结果。
- `0.0.130_beta -> 0.0.131_beta` 旧合同迁移以及 `0.0.131_beta` 严格合同 OTA 仍需真机确认。
