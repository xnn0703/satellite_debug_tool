# M70 验收标准

- [x] macOS 唯一构建身份路径为 `Contents/Resources/satellite_debug_tool/build_identity.json`，Windows 路径保持不变。
- [x] v1.9.8 正式 macOS ops 归档在新代码下通过候选根和身份校验，旧代码稳定复现截图错误。
- [x] 测试 fixture 使用真实 bundle 布局，错误的 `Contents/MacOS/_internal` 布局不再被当作正式 macOS app。
- [x] 冻结包和候选发布门禁按平台正式路径核验身份，任意位置的同名 JSON 不能替代正式身份。
- [x] macOS 完整包准备、安装目录身份比对、切换、健康确认和回滚定向测试通过。
- [ ] 全量 pytest、翻译检查、真实 macOS user/ops 构建与签名检查通过。（源码全量与翻译已通过，正式 v1.9.9 构建待提交后执行）
- [ ] v1.9.9 两平台发布事务、服务器回执、fresh checker、完整包摘要及 Range 检查通过。
- [x] 文档明确 v1.9.2～v1.9.8 macOS 需要一次完整包引导；引导后自动更新合同生效。
- [ ] Windows 安装现场、Apple notarization、设备与 RF 验收继续独立记录，不由软件回归代替。
