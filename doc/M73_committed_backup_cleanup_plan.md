# M73 自动更新已提交备份清理计划

## 已确认现象与根因

- v1.9.11 → v1.9.12 的真实 macOS 自动更新已经完成健康试运行与提交，当前
  `SatelliteTerminalManagerOps.app` 为 v1.9.12。
- `/Applications/SatelliteTerminalManagerOps.app.bak.<transaction_id>` 是目录切换时保留的
  v1.9.11 回滚副本；试运行未提交前必须保留。
- 当前成功路径在写入 `journal.state=committed`、投递健康提交回执和写入
  `update-status.state=completed` 后，仅移除登录恢复入口，没有释放 `backup_dir`。
- 因此备份在事务已经不可回滚后仍长期占用约一份完整应用空间。该问题不是下载、打包或
  macOS Finder 显示问题，而是安装事务缺少“提交后释放备份”终态。

## 单一状态模型

- `InstallJournal` 继续作为安装目录、备份目录和恢复操作的唯一 owner。
- 备份只使用以下肯定式事实：
  - `recovery_ready`：旧安装完整保留，可供试运行失败时回滚；
  - `released`：提交完成，旧安装目录已经删除；
  - `new_install`：安装前没有旧安装，不产生回滚副本。
- 只有持久化的 `journal.state=committed` 才允许进入 `released` 或 `new_install`。
- `main.py` 只编排提交顺序，不直接拼接路径或独立删除目录。

## 实施范围

1. 在 `InstallJournal` 增加幂等的已提交备份释放接口：
   - 校验 journal 已提交；
   - 使用 journal 中经过路径约束验证的 `backup_dir`；
   - 有旧安装时递归删除回滚副本并回写 `backup_state=released`；
   - 新安装回写 `backup_state=new_install`；
   - 重入时根据文件事实稳定返回相同结果。
2. 成功事务顺序统一为：
   - 新版健康回执为 `ready`；
   - 持久化 `journal.state=committed`；
   - 向同一试运行进程投递 `committed` 回执；
   - 释放旧安装备份并持久化 `backup_state`；
   - 写入最终完成状态；
   - 最后移除登录恢复入口。
3. 已提交事务在上述步骤间中断时，`--recover` 入口先恢复健康提交投递，再调用同一个备份释放
   接口；不得回退已经提交的新版。
4. 备份删除失败时保存明确的 `cleanup_required` 状态、错误详情和受约束备份路径，保留恢复入口；
   不把它写成 `completed`，也不把已经提交的新版本错误回滚。
5. 删除或改写“成功后备份仍存在”的旧测试断言；新增旧实现稳定失败、新实现稳定通过的回归。
6. 本次不清理下载缓存、候选 ZIP、SDB、设置目录或安装锁文件；这些对象不属于旧安装备份生命周期。

## 回归范围

- 正常升级：健康提交前备份存在，完整提交后备份消失且 journal/status 都记录 `released`。
- 首次安装：不创建备份，终态为 `new_install`。
- 健康失败：旧安装仍能从备份恢复，不能提前删除。
- 提交后中断：恢复入口幂等补发提交回执并释放备份，不回滚新版。
- 清理失败：新版保持已提交，状态明确为 `cleanup_required`，恢复入口保留。
- 路径防护：未提交 journal、异常备份路径和安装目录重叠均拒绝删除。

## 验证与交付边界

- 先运行 updater applier/main/recovery/health 定向回归，再运行全量 pytest、翻译检查、
  `compileall` 和 `git diff --check`。
- 软件测试证明事务顺序和目录清理行为；正式 `/Applications` 包的实际释放仍以一次真实自动更新后
  的文件事实和 journal/status 为最终验收证据。
- 当前 v1.9.11 备份已由 v1.9.12 的 committed journal 和健康回执证明不再承担回滚职责；待计划确认后，
  可在实现验收阶段对该精确目录执行一次清理，不扩展到其他应用或更新事务。
- 本次不提交、不打 tag、不发版；这些动作继续等待用户明确指令。
