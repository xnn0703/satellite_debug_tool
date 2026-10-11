# M35 固件包版本策略验收

- [x] 上位机制包默认生成 `allow_downgrade`。
- [x] 固件 Shell、Windows 和 Python 发布入口默认生成 `allow_downgrade`。
- [x] 默认包允许降级、同版本重装和升级。
- [x] 显式版本策略仍受 manifest 认证并按原语义执行。
- [x] 旧 `upgrade_only` 包不被修改或绕过。
- [x] 上位机专项测试通过。
- [x] 固件发布工具测试通过。
