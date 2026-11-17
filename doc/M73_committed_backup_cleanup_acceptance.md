# M73 验收标准

- [x] `InstallJournal` 是备份生命周期和受约束删除路径的唯一 owner。
- [x] 试运行健康提交前，旧安装备份完整存在并可用于失败回滚。
- [x] 只有持久化 `committed` 的事务允许释放旧安装备份。
- [x] 正常升级结束后备份目录不存在，journal 与 status 均记录 `backup_state=released`。
- [x] 首次安装不生成回滚副本，journal 与 status 均记录 `backup_state=new_install`。
- [x] 提交后中断由 `--recover` 幂等完成健康提交投递和备份释放，不回滚新版。
- [x] 备份清理失败时新版保持提交，状态为 `cleanup_required`，错误和精确路径可追溯，恢复入口保留。
- [x] 未提交事务调用释放接口、异常路径或目录重叠均被明确拒绝。
- [x] 旧的“成功事务保留备份”测试已改为稳定验证自动释放，并新增清理失败和恢复补全回归。
- [x] updater 定向回归、全量 pytest、翻译检查、`compileall` 与 `git diff --check` 通过。
- [ ] 真实 macOS 自动更新后 `/Applications` 不再残留对应 `.app.bak.<transaction_id>` 目录。
- [ ] Windows 正式包目录切换与提交后清理行为由 CI 回归覆盖；现场安装验收独立记录。
