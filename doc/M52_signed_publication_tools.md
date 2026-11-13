# M52 本地签名与正式晋升

正式发行使用 `scripts/promote_release.py fetch / prepare / commit`。签名发生在发布者本机；M71 起 ops 大包与 payload manifest 只在候选阶段上传一次，正式 commit 仅通过 SFTP 传公开 TUF 小型元数据和 stage request，ECS 从 incoming 本机晋升大包。root/targets 私钥、本地发布账本、publication.json 均不上传。构建账号只有 incoming 权限，正式发布使用独立 SFTP 账号；两者使用独立核验的 known_hosts 和固定 SSH key。

本页“当前现场状态”及各处“本轮”描述是 M52 阶段的 2026-11-02 历史快照。当前 v1.9.0 发行状态以 [M53 开发记录](M53_manager_naming_release_dev_log.md)和[验收标准](M53_manager_naming_release_acceptance.md)为准。

## M52 现场快照（2026-11-02）

北京 ECS `i-2zec7150rri51esjj28f` 已部署，生产应用根为 `https://60.205.157.141/v1/apps/satellite-terminal-tool/`。空生产仓库已通过实际客户端校验，记录 root1/targets1/snapshot2/timestamp3；已有远端 timestamp，当前仍没有正式应用包。本机受控目录为 `~/.config/satellite-release`，其中包含 upload.json、publish.json、known_hosts、SSH keys、repository、venv；root/targets 私钥仅在其中的 keys 目录，服务端仅持 timestamp/snapshot 低角色密钥。密钥和凭据内容不进入本页或源码。

实际候选 SFTP 上传/读回、上传账号写 public 拒绝、HTTPS Range/Downloader 续传 SHA 均 PASS。候选权限测试不替代首份真实应用包的签名 prepare/commit 与原子渠道切换验收。完整现场状态见 [部署记录](M52_server_deployment_task.md)与[机器可读回执](M52_server_deployment_evidence.json)。

## 一次性准备

发布机使用 Python 3.10 或更新版；实际构建环境为 Python 3.11。创建独立工具环境并安装 `requirements/publish.txt`，其中不包含 GUI 依赖：

```bash
python3 -m venv .venv-publish
.venv-publish/bin/python -m pip install -r requirements/publish.txt
```

本次部署已由发布负责人在服务器外产生并保管四角色 PEM 签名密钥，私钥文件权限为仅属主可读写；源码只包含经实际客户端核验的生产公开 root，不包含私钥。root 平时离线；每次日常发布仅加载 targets/snapshot/timestamp。服务器每日续签只需 timestamp key，每周 snapshot 续签另需 snapshot key；服务器始终不持 root/targets key。

下例仅供新仓库的一次性初始化；当前生产仓库及本地账本已存在，继续使用 `~/.config/satellite-release/repository`，不重复 init：

```bash
.venv-publish/bin/python scripts/update_repository.py init \
  --repository /absolute/signing/repository --key-dir /absolute/private/keys \
  --app-id satellite-terminal-tool --repo-id satellite-terminal-tool-production
```

保留输出的公开 root 及完整本地目录备份。经核验后，将公开 root 配入应用的 release.config.json；密钥不进入源码。首次部署前确保远端创建 `/srv/satellite-updates/public/v1/apps` 和 `/srv/satellite-updates/receipts`，正式账号可写这两个目录、可读 incoming，Nginx 只读 public。

复制 `deploy/release_publish.example.json` 到本机受控配置目录，填写已核对的服务器地址和正式发布用户名。此 SSH 配置与候选上传配置独立。

## 取得候选

Mac 完成真实构建并上传后，本地候选可直接作为 `--candidate`；工具会忽略候选上传工具自身的 `.upload-local-lock`，该文件不属于发行字节。

Windows Actions 实际构建上传完成后，产物位于 ECS incoming，发布机使用只读 SFTP 下载回执与其指定文件；本轮尚无此类真实应用包：

```bash
.venv-publish/bin/python scripts/promote_release.py fetch \
  --config /absolute/publish.json --known-hosts /absolute/known_hosts \
  --identity-file /absolute/publish_ssh_key \
  --remote-candidate /srv/satellite-updates/incoming/RELEASE_ID/satellite-terminal-tool/ops/win/x86_64/COMMIT_SHA \
  --output /absolute/candidates/RELEASE_ID
```

下载逐项验证长度和 SHA-256，并在全部成功后原子交付本地候选目录。来源 commit 仍须由发布负责人独立确认，候选回执本身不能替代发布批准。

## 准备和提交

`--initial` 只适用于远端完全没有 timestamp 的仓库初始化情形，不能根据“第一份应用包”判断。当前远端空生产仓库已经有 timestamp，因此第一份真实应用包 prepare **不加 `--initial`**。`--public-base-url` 使用实际 HTTPS 应用根；本阶段只读取远端公开状态，不改远端，也不推进本地正式账本。

```bash
.venv-publish/bin/python scripts/promote_release.py prepare \
  --config /absolute/publish.json --known-hosts /absolute/known_hosts \
  --identity-file /absolute/publish_ssh_key \
  --repository ~/.config/satellite-release/repository --key-dir ~/.config/satellite-release/keys \
  --candidate /absolute/candidates/RELEASE_ID --expected-commit COMMIT_SHA \
  --output /absolute/publications/RELEASE_ID \
  --public-base-url https://60.205.157.141/v1/apps/satellite-terminal-tool/ \
  --min-os 10.0.19041 --channel stable
```

Mac 版按已确认的最低 macOS 版本设置 `--min-os`；其安装方式仍为完整 ZIP 手工安装。可用 `--notes-file`、`--min-updater-version`、`--config-schema-min/max` 明确发行合同。

准备过程用本地可信 root 验证服务器当前 root 链、timestamp、snapshot、targets，吸收服务器低权续签的版本；再本地签名新发行，封口 `publication.json` 与 bundle 内的公开文件。输出必须是新目录。保留整个 bundle，重试使用原字节，不重新签名生成同名候选。

这里的发布者持有 targets 签名权，允许通过同一受保护流程恢复已过期的 targets/timestamp/snapshot；root 必须仍有效。客户端检查和服务器低权续签继续拒绝过期 targets，不能用低权 key 延长 targets 有效期。没有新版本时可使用当前正式版本的原候选重新 prepare/commit，只续签元数据，保持包和清单字节不变。

审核本地候选身份和发布清单后执行：

```bash
.venv-publish/bin/python scripts/promote_release.py commit \
  --config /absolute/publish.json --known-hosts /absolute/known_hosts \
  --identity-file /absolute/publish_ssh_key \
  --bundle /absolute/publications/RELEASE_ID/publication.json
```

提交持有服务器排他锁，先确认 timestamp 仍与 prepare 时一致，再上传缺失的小型签名文件和编号元数据。M71 的请求把已审计本地 receipt 与固定 incoming 相对目录绑定；ECS `satellite-updates-verify-publication.path/service` 重新核对 receipt、身份、文件名、长度和 SHA-256，把候选复制为独立公开 inode，再对全部不可变文件计算完整长度和 SHA-256，由 root 写入发布账号只读的回执。发布机校验回执的 request/bundle/文件数/总字节，再从实际 HTTPS 地址对每个目标做首/中/尾最多 4 KiB Range 对比；全部完成后，通过 OpenSSH `posix-rename` 原子替换 timestamp，最后推进本地正式账本。未支持原子替换的 SFTP 服务明确失败。包缺失、服务器回执缺失/不匹配、权限错误、公开 URL 错误、HTTP 降级或 Range 不一致都不推进渠道。该改动不给发布账号开放 shell/TTY/转发，root/targets 私钥仍只留在发布机。

正常失败会释放锁，原 bundle 可重试；已经提交的同 bundle 返回 `reused`，并补齐可能因本地异常而尚未更新的账本。另一发布或服务器续签改变了 timestamp 时明确拒绝；重新 prepare 到新目录获取新状态。更新服务器续签与正式提交共用排他锁，避免互相覆盖。

硬断线可能留下 `/srv/satellite-updates/receipts/satellite-terminal-tool.publication.lock`。先确认旧发布进程已经结束，再对同一 bundle 加 `--recover-lock`；此选项只允许恢复同 bundle owner，不能抢占其它发布或续签。不要因为经过一段时间就删除陌生 owner 的锁。

## 服务器低权续签

服务器已安装最小工具 bundle 和独立 Python 3.11 venv，没有复制发布机的 root/targets 私钥目录或账本。每日任务使用 timestamp key；每周任务使用 snapshot/timestamp key，四个维护 timer 已 enabled，单次服务运行 PASS。两种元数据续签都指定相同的发布锁：

```bash
/opt/satellite-updates/venv/bin/python /opt/satellite-updates/tools/scripts/update_repository.py refresh-timestamp \
  --repository /srv/satellite-updates/public/v1/apps/satellite-terminal-tool \
  --key-dir /etc/satellite-updates/online-keys \
  --promotion-lock /srv/satellite-updates/receipts/satellite-terminal-tool.publication.lock

/opt/satellite-updates/venv/bin/python /opt/satellite-updates/tools/scripts/update_repository.py refresh-snapshot \
  --repository /srv/satellite-updates/public/v1/apps/satellite-terminal-tool \
  --key-dir /etc/satellite-updates/online-keys \
  --promotion-lock /srv/satellite-updates/receipts/satellite-terminal-tool.publication.lock
```

锁占用时任务失败并保留现有元数据，下一次计划运行再执行。targets 有效期 90 天、snapshot 30 天、timestamp 7 天；低权续签不越过 targets/root 到期时间。发布负责人应在 targets 到期前通过受保护发布流程续签，root 续签和轮换使用离线 root 负责人。root 轮换工具与客户端旧/新根链已经有本地回归；跨服务器的根轮换和 IP 迁移仍需独立现场演练。

## 验证边界

软件回归使用真实 TUF 签名、模拟 SFTP 文件系统与 HTTPS fetcher，覆盖首次提交、服务器续签后发布、缺包、同 bundle 重试、timestamp 中断、渠道冲突、锁恢复、远端回滚、签名错误及候选获取。其后真实 ECS 的生产空仓库、IP HTTPS、低权续签、SFTP 权限和探针下载已通过；上线配置后更新/构建/晋升/翻译组合复测 **253 passed，10.16 秒**，日志 `/tmp/m52-update-integration-deployed.log`。真实应用包的签名 prepare/commit、SFTP 原子渠道切换、冻结构建、Windows 实机更新，以及密钥/仓库备份恢复仍待验收。
