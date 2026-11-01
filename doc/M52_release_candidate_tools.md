# M52 本地构建与候选上传工具

本页记录 M52 阶段实现的候选工具合同及当时的现场证据。下述 M52 现场状态是 2026-11-02 的历史快照，不代表当前 v1.9.0 的发行状态；本轮进度与最终验收以 [M53 发布记录](M53_manager_naming_release_dev_log.md)和[验收标准](M53_manager_naming_release_acceptance.md)为准。源码测试和模拟 SFTP 通过不代表真实 Windows 冻结、平台签名、ECS 网络、账号权限或公开发行验收通过。

2026-11-02 M52 现场快照：ECS 已部署，真实候选 SFTP 上传与回读、上传账号写 public 拒绝均 PASS；IP HTTPS Range 和 Downloader 续传摘要 PASS。GitHub `release-candidate` 环境已配置 `v*` tag 限制、owner reviewer、上传 secrets/config variable。当时工作流源码尚未 commit/push/触发；后续已进入 M53 v1.9.0 候选流程。候选构建与正式发行事实继续记录在 M53 发布记录中。探针结果和 M52 当时待验边界见 [部署记录](M52_server_deployment_task.md)。

## 身份与不可变候选

`build_identity.json` 是身份来源：`app_id=satellite-terminal-tool`、`edition`、`version`、`platform=win|mac`、`architecture=x86_64|arm64`、`build_id=40 位源码 commit`。Windows 文件树 manifest 在协议边界将 `win` 标准化为 `windows`。发行工具校验包内 SHA 与指定干净 checkout 一致，tag 必须与该 commit 的版本源相同。

`scripts/release_artifacts.py` 从最终载荷生成版本化完整包、Windows 外置 `payload-manifest.json` 和 `receipt.json`。调用方先完成系统代码签名，然后冻结文件树和包摘要；候选允许 `unsigned`，回执记录该事实。`--signature authenticode-verified` 执行 signtool 校验；Mac 的 `adhoc-verified` 和 `developer-id-verified` 分别检查 codesign。平台系统签名与正式 TUF 签名属于独立事实。

输出目录已经包含文件时，工具拒绝重建；重试上传使用原 receipt。Windows ops 可以同时提供 `--base-root` 与 `--base-manifest`，生成一跳 `delta.zip`。完整包始终保留，是否发布增量由测量收益和晋升策略决定。

首轮 CI 默认生成完整包，增量候选由发布人员显式提供上一正式版本的完整树及可信清单；客户端会自动选择适用的增量。首版没有正式基线，使用完整包。目前没有自动下载基线的 CI job，不能将这一步称为自动增量发版。生成增量时使用全新的候选目录，随后上传并晋升该候选：

```bash
python scripts/release_artifacts.py \
  --payload /absolute/new-ops-payload --output /absolute/new-candidate \
  --source-commit COMMIT_SHA --version VERSION --signature unsigned \
  --base-root /absolute/previous-official-payload \
  --base-manifest /absolute/previous-official-tree.json
```

`publish-candidate` 校验基线就是当前正式渠道的 build/文件树，并只发布小于完整包的增量。同一版本已经晋升后不再修改其字节或追加增量。

receipt schema 为 `satellite.release-candidate/v1`，包含完整身份、源码 SHA、系统签名事实、每份 artifact 的文件名/长度/SHA-256，以及 Python、主机、全部已安装包版本。普通 build 命令只写本地；仅发行命令包含上传阶段。

## Windows 编排

GitHub workflow 默认 `contents: read`。普通 PR 和主分支 CI 安装约束依赖、测试、构建、冻结 smoke，并保存 GitHub artifact。

tag/dispatch 先由 `scripts/resolve_release.py` 将 tag 解析为确切 SHA；构建与上传 job 都 checkout 该 SHA。Windows 双版完成后，`upload-candidates` 使用 `release-candidate` environment。当前此 environment 已限制 `v*` tag、设置 owner reviewer，并配置：

- secrets：`RELEASE_UPLOAD_SSH_KEY`、独立核验的 `RELEASE_UPLOAD_KNOWN_HOSTS`。
- variable：`RELEASE_UPLOAD_CONFIG`，内容参照 `deploy/release_upload.example.json`。

代码只上传候选，不创建 GitHub Release、不持有 TUF 签名私钥、不推进 stable。Environment 的审批与 tag 保护需要在 GitHub 管理面配置，YAML 无法替代这些服务端设置。

当前 environment 只接受 `v*` tag。手动 dispatch 必须把 workflow ref 和输入 tag 都设为同一个实际 tag，例如：

```bash
gh workflow run build.yml --ref vX.Y.Z -f tag=vX.Y.Z
```

实际执行前将示例替换为已存在且与版本源匹配的 tag。从 master dispatch 即使成功解析输入 tag，上传 job 仍会被 environment 的 ref 规则阻断；继续使用 tag ref，不放宽该权限。以上为工具操作说明；M52 记录的“本轮未触发”只描述当时快照，当前 M53 v1.9.0 候选状态见本页开头链接。

## Mac 本地发行

准备已提交且干净的指定 SHA，使用项目 Python 环境执行（以下参数使用实际值替换）：

```bash
.venv/bin/python scripts/release_macos.py \
  --source-commit COMMIT_SHA --version v1.8.1 --edition both \
  --release-id RELEASE_ID --config /absolute/upload.json \
  --known-hosts /absolute/known_hosts --identity-file /absolute/upload_key
```

命令检查当前 SHA/工作区，恢复约束依赖，完成源码测试、翻译、构建、冻结 smoke 和候选归档，然后使用独立 `.venv-deploy` 执行相同上传 CLI。候选 receipt 已存在时复用原字节，不重新构建。`scripts/build_macos.sh` 继续只构建本地。

独立重试任一平台候选：

```bash
python scripts/upload_release.py \
  --config /absolute/upload.json --receipt /absolute/candidate/receipt.json \
  --release-id RELEASE_ID --known-hosts /absolute/known_hosts \
  --identity-file /absolute/upload_key
```

上传解释器需要 `requirements/deploy.txt`，其依赖与应用运行环境分开。

当前本机上传/发布工具配置、known_hosts、SSH keys、repository 和 venv 位于 `~/.config/satellite-release`。upload.json 与 publish.json 分别用于候选上传和正式发布；私钥内容不写入文档或源码。

## 服务器目录与权限

唯一允许的根为 `/srv/satellite-updates/incoming`，候选位于：

```text
/srv/satellite-updates/incoming/<release_id>/
  satellite-terminal-tool/<user|ops>/<win|mac>/<x86_64|arm64>/<commit>/
    <版本化完整包.7z|zip>
    payload-manifest.json    # Windows
    delta.zip                # 可选 Windows ops
    receipt.json             # 全部文件远端回读校验后最后上传
```

此目录位于 Nginx web root 和正式 TUF 仓库之外。上传账号只允许写候选根，SSH 限制为 SFTP；正式区由独立发布身份维护，Nginx 只读正式区。模板不包含主机私钥、口令、真实域名或真实 IP。

现场上传 SSH 策略已核验为 publickey only/internal-sftp，TCP/agent/TTY/tunnel/X11 均关闭。生产公开 app 目录最初因 0750 导致 404，已根因修复为新公开目录 0755 并增加回归；该公开读取权限不赋予上传账号 public 写权限。机器可读现场回执见 [M52_server_deployment_evidence.json](M52_server_deployment_evidence.json)。

客户端使用指定 known_hosts 和 RejectPolicy；主机指纹变化直接失败。通过纯 SFTP API 创建白名单路径，逐层 lstat 拒绝链接。`.part` 续传先核对远端前缀，再写剩余内容；远端全文件流式长度和 SHA-256 一致后才 rename。已有同名字节相同则复用，不同则拒绝。

上传在本地持有进程锁，以 immutable receipt 摘要标识远端上传 owner；同一 receipt 的中断重试可恢复该 owner，不同 receipt 的并发请求明确失败。GitHub 同一 SHA 的上传另有 concurrency 约束。`.upload-local-lock` 属于本地控制文件，不上传。远端 `.upload-lock` 在完成或可正常收尾时移除；断线后保留，下一次相同 receipt 继续恢复。

## 依赖证据边界

`requirements/build-{windows,macos}.txt` 引用相同直接依赖 constraints 和运行依赖，固定 PyInstaller、pytest 与 TUF 配套版本。直接依赖版本来自当前 macOS `.venv`；部署工具的 Paramiko/bcrypt/PyNaCl/invoke 固定版本已核对 wheel metadata。receipt 记录实际完整传递环境。

这属于固定直接约束与完整环境回执，尚未完成各平台全部传递依赖的带 hash lock、Windows wheel 还原或真实冻结构建验收。首次实际 Windows / Mac 发行应分别冻结并归档可恢复的传递依赖集合和工具链证据。

服务器工具已在独立 Python 3.11 venv 运行，原系统 Python 3.6 保留；这不代表 Windows/Mac 构建依赖已完成相同验收。上线配置后的集成组合为 253 passed（10.16 秒），与历史全量 2039 passed、历史集成 252 passed 存在重叠，不相加统计。
