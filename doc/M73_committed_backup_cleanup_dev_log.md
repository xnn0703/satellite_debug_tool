# M73 自动更新已提交备份清理开发记录

## 目标

- 让安装事务在健康提交后自动释放旧应用备份。
- 保持健康试运行失败时的原安装回滚能力。
- 让成功、首次安装、清理失败和提交后恢复都保存单义状态。

## 实施记录

- 2026-11-14：完成现有成功、失败与恢复调用链复核；确认缺口位于 committed 后未释放
  `InstallJournal.backup_dir`。
- 2026-11-14：计划与验收标准经用户确认，开始实现。
- 2026-11-14：`InstallJournal.release_committed_backup()` 成为提交后备份释放的唯一入口；接口校验
  committed 状态、当前安装目录和受约束备份类型，成功保存 `released/new_install`。
- 2026-11-14：正常安装在健康提交回执后释放备份，随后才写 completed 并移除登录恢复入口。
  删除失败保存 `cleanup_required`，新版保持提交且登录恢复入口保留。
- 2026-11-14：已提交事务的 `--recover` 路径复用相同释放接口；恢复补发健康提交回执后释放
  遗留备份，不回滚新版。
- 2026-11-14：项目 `AGENTS.md` 更新 M73 事务合同。

## 验证记录

- updater/release 定向回归：`295 passed, 2072 deselected in 5.89s`。
- 当前工作树全量回归：`2367 passed in 283.29s`。
- 翻译目录校验：1558 条消息全部完成，`Translation catalog OK`。
- `compileall -q satellite_debug_tool`：通过。
- `git diff --check`：通过。
- 当前 v1.9.12 真实事务复核：journal 为 committed、当前 app 身份为 v1.9.12，
  `/Applications` 已无对应 v1.9.11 备份目录；未再执行删除。使用新接口按文件事实将旧 journal
  补记为 `backup_state=released`。
- 真实 macOS 下一次自动更新后的 `/Applications` 备份释放仍待正式包验收；未用软件测试替代。
