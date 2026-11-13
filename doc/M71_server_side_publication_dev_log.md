# M71 开发记录

## 2026-11-13 根因证据

- v1.9.10 publication commit 失败于 `scripts/promote_release.py:_stage_public_file()`。
- 遗留 public partial 为 145326080 字节；候选 Mac ops ZIP 为 284071154 字节。
- ECS 旧 timestamp SHA-256 为 `06551d8391d3c65c23cc361c0d4055d6a37ff587924b3f17edf36f28a8e5b77c`，稳定通道未切换。
- 服务器 40 GB 文件系统已用 17 GB、可用 22 GB。
- 失败调用随后用已关闭 SFTP 连接清锁，产生次生 `Socket is closed`，但 journal 保留了首个 timeout 调用链。

## 实施与验证

### 单一数据流

- `scripts/promote_release.py` 把本地已审计 ops receipt 与远端 incoming 路径配对；匹配的完整包和 payload manifest 不再进入 `_stage_public_file()`。
- stage request v2 保存 candidate 相对路径、receipt SHA-256 和 artifact 名称。发布机仍上传小型签名元数据；root/timestamp 保持最后提交。
- `scripts/verify_publication_server.py` 在 ECS 校验真实 incoming 根、每级真实目录、receipt、app/edition/build、artifact 名称、长度及 SHA-256；通过后复制到目标目录临时文件、`fsync`，再以不可覆盖名称封口。
- 复制后的 public 文件与 incoming 候选使用不同 inode；随后服务对 publication 的全部不可变文件重新计算长度及 SHA-256。
- `scripts/release.py` 从同一 v2 journal 向 commit 传入 Windows/Mac 两组候选；前次 `commit_publication=FAILED` 时仅为同一 bundle 增加 `--recover-lock`。
- `_remote_lock()` 保留首个业务异常；连接已断时的清锁失败不再覆盖原 timeout，原 owner 留给同 bundle 恢复。

### ECS 部署

- 实例：`i-2zec7150rri51esjj28f`，北京，公开地址 `60.205.157.141`。
- 新服务脚本以版本化文件 `/opt/satellite-updates/tools/scripts/verify_publication_server_m71_1.py` 部署；systemd 现场覆盖文件为 `/etc/systemd/system/satellite-updates-verify-publication.service.d/m71z.conf`。仓库内正式 unit 直接指向通用 `verify_publication_server.py`，不携带本次迁移文件名。
- 服务保持 `ProtectSystem=strict`、`NoNewPrivileges=true`，incoming 只读，只允许写固定 public app 根和 verification 状态目录，地址族限制为 `AF_UNIX`。
- `systemd-analyze verify` 通过；正式请求在 9 秒内完成，服务完整核验 27 个文件、513907063 字节。
- Mac ops incoming 与 public 文件 inode 分别为 1177372、1177384，证明正式文件不是指向候选的硬链接。

### v1.9.10 正式发布

- 应用 commit/tag：`2ccbc4fd5ee7d0f774e26b03945ba468007eef36` / `v1.9.10`；Windows Actions 43367264208 成功。
- 保持原候选和 publication 不变；publication SHA-256 为 `c618e7abccbacfdac0a51d6b143d4b93674842d42fa688cfc4f6950f553f5aeb`。
- 同一 journal 恢复后稳定发布成功；verification request 为 `4799a1b46ef84b9a861fbfc8c2742bf898af2048d686435f96b55f9dfe128929`，稳定 timestamp SHA-256 为 `62728e09815a4f34c043ea57de4bb81addf2905da4be0921e3cc55676bbcdc0f`。
- Windows/macOS fresh checker 均返回 1.9.10、相同 build ID、`installation_mode=automatic`、`delta_count=0`。
- 四份完整包的首/中/尾 Range 全部通过；下载索引为 `https://60.205.157.141/downloads/v1.9.10/release.json`，摘要索引为 `https://60.205.157.141/downloads/v1.9.10/SHA256SUMS.txt`。
- 机器验证记录：`~/.config/satellite-release/runs/v1.9.10/release-validation.json`，SHA-256 `31ee22cda0028eb675b97b117a79314a9ab5e838b9276b6617c42656df62cd6b`。

### 软件回归

- 发布链路定向测试：47 passed。
- 全量测试：2359 passed，274.78 秒。
- 翻译目录检查：1558 finished、0 unfinished。
- `compileall` 与 `git diff --check`：PASS。

## 保留边界

- 首次失败遗留的未引用 partial 仍位于服务器公开目录，大小 145326080 字节；它不在 TUF 元数据和下载索引中，也不参与当前发布。删除属于破坏性清理，未在本次发布事务中擅自执行。
- v1.9.2～v1.9.8 macOS 运维版的旧 updater 在安装前读取错误身份路径，不能靠远端 v1.9.10 包自举；必须人工安装 v1.9.10 完整包一次。此后版本才走自动更新。
- Windows 实机安装/回滚、Apple notarization 和物理设备/RF/摇摆台验收均为 NOT_RUN，不由发布成功推出。
