# M72 验收标准

- [x] 冻结健康检查使用 `main_executable(install_dir)` 作为唯一入口事实源。
- [x] Windows 正确 `.exe` 通过，错误可执行路径被明确拒绝。
- [x] macOS `Info.plist` 声明入口通过，错误可执行路径被明确拒绝。
- [x] macOS 自动更新 fixture 的入口名称与正式构建一致。
- [x] 失败复现中的 Windows 路径硬编码已从健康模块删除。
- [x] 定向回归、全量 pytest、翻译检查、`compileall` 和 `git diff --check` 通过。
- [ ] 真实正式包完成一次应用内自动更新，产生提交后的健康回执且不触发回滚。
- [ ] Apple notarization、Windows 现场安装、设备与 RF 验收继续独立记录。
