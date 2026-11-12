# 卫通终端管理工具发版操作手册

最后更新：2026-11-12。适用于 M52～M66 之后的发行链路；最近一次完整走通并留存机器可读证据的版本是 v1.9.3。

本手册是当前发版的唯一操作入口。历史设计、部署和故障证据分别见：

- [M52 候选构建与上传](M52_release_candidate_tools.md)
- [M52 签名与正式晋升](M52_signed_publication_tools.md)
- [M56 服务器侧发布复核](M56_server_release_verification_dev_log.md)
- [M57 macOS 自动更新](M57_macos_automatic_update_dev_log.md)
- [v1.9.3 实际发布记录](M62_capture_duration_clock_release_dev_log.md)
- [v1.9.3 机器可读验证结果](M62_release_validation.json)

## 1. 当前发行模型

| 产物 | 构建位置 | 交付方式 | 自动更新 |
|---|---|---|---|
| Windows user | GitHub Actions | ECS `/downloads/v<version>/` 完整 7z | 否，客户人工安装完整包 |
| Windows ops | GitHub Actions | ECS TUF stable + 完整 7z | 是 |
| macOS user | 发布 Mac 本地 Python 3.11 | ECS `/downloads/v<version>/` 完整 ZIP | 否，客户人工安装完整包 |
| macOS ops | 发布 Mac 本地 Python 3.11 | ECS TUF stable + 完整 ZIP | 是 |

约束：

- 版本唯一来源是 `satellite_debug_tool/__init__.py.__version__`，tag 固定为 `v<version>`。
- 四份包必须来自同一个干净源码 commit；包内 `build_identity.json`、candidate receipt、tag 和 TUF catalog 必须指向该 commit。
- 文件名只由 `satellite_debug_tool/package_names.py` 生成。user 包名不含 `user`，ops 包名含 `ops`。
- user 构建裁剪应用更新器和在线更新配置；ops 才进入 TUF 自动更新渠道。
- GitHub Actions 只构建 Windows 并上传隔离候选，不持有正式 TUF 私钥，也不切换 stable。
- Mac 候选在发布 Mac 本地构建并上传；不能用普通 `build_macos.sh` 结果替代正式候选。
- ECS 对正式文件做本机完整 SHA-256；公网只做首/中/尾 HTTPS Range 抽样，不再通过 3 Mbps 公网完整回读大包。
- 已发布 tag、候选字节和 publication bundle 均不可变。任何代码修复使用新 patch 版本，禁止移动旧 tag。

## 2. 发布角色和受控路径

源码远端：

- `github`：正式 tag、Windows Actions 和候选上传。
- `origin` / `gitlab`：不因一次 GitHub 发版自动同步；需要同步时单独授权和验证。

发布 Mac 的受控材料位于仓库外：

```text
~/.config/satellite-release/
  upload.json                 # 候选上传配置
  upload_ed25519              # 候选上传身份
  publish.json                # 正式晋升配置
  publish_ed25519             # 正式发布身份
  known_hosts                 # 固定服务器主机指纹
  repository/                 # 本地 TUF 正式账本
  keys/                       # root/targets/snapshot/timestamp 私钥
  build-venv-3.11/            # Mac 正式构建环境
  venv/                       # 发布工具环境
```

禁止把配置内容、私钥、known_hosts 或本地正式账本复制进仓库。服务器只保留低权限在线 snapshot/timestamp 密钥，不持有 root/targets 私钥。

ECS 固定目录：

```text
/srv/satellite-updates/incoming/                    # 隔离候选，Nginx 不公开
/srv/satellite-updates/public/v1/apps/satellite-terminal-tool/
/srv/satellite-updates/public/downloads/v<version>/ # 四份完整下载包、索引和校验表
/srv/satellite-updates/receipts/                    # 发布锁和服务器复核回执
```

候选上传账号只能写 `incoming`；正式发布账号可读候选、写正式区，但仍是 `internal-sftp`，没有 shell、TTY 或端口转发权限。

## 3. 发布前门禁

### 3.1 冻结范围

1. 完成该版本的 plan、验收标准、开发记录和 `doc/v<version>_release_notes.md`。
2. 审计 `git status` 和完整 diff；发布 commit 只包含已经验收的范围。
3. 修改 `satellite_debug_tool/__init__.py` 的版本号，并同步发布说明。
4. 用户明确授权后才能 commit、tag 和 push。

建议先设置本轮变量；下面所有命令中的值都应由当前仓库重新取得，不能照抄历史版本：

```bash
VERSION=X.Y.Z
TAG="v${VERSION}"
COMMIT="$(git rev-parse HEAD)"
RELEASE_ID="${COMMIT}"
```

### 3.2 源码验证

```bash
PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q
.venv/bin/python scripts/update_translations.py check
.venv/bin/python -m compileall -q satellite_debug_tool scripts
git diff --check
git status --short
```

全量 pytest、翻译或差异检查任一失败都停止发版。修复后应在稳定最终快照上重新执行相关定向测试和全量测试；不能把一次重跑通过当作已定位失败根因。

### 3.3 提交和不可变 tag

仅在工作区范围、版本和验证证据全部确认后执行：

```bash
git commit -m "feat(release): 发布 v${VERSION} ..."
COMMIT="$(git rev-parse HEAD)"
git tag -a "${TAG}" -m "${TAG}"
git push github HEAD:master
git push github "${TAG}"
```

创建 tag 前检查：

```bash
test "$(.venv/bin/python -c 'import satellite_debug_tool; print(satellite_debug_tool.__version__)')" = "${VERSION}"
test "$(git rev-parse "${TAG}^{commit}")" = "${COMMIT}"
git status --porcelain
```

最后一条必须为空。tag 推出后出现的新改动属于后续版本，不追加进本版，也不移动 tag。

## 4. Windows 候选：GitHub Actions

推送 `v*` tag 会触发 `.github/workflows/build.yml`。需要重跑同一不可变 tag 时，也可以显式 dispatch：

```bash
gh workflow run build.yml --repo xnn0703/satellite_debug_tool \
  --ref "${TAG}" -f tag="${TAG}"
gh run list --repo xnn0703/satellite_debug_tool --workflow build.yml --limit 5
gh run watch <RUN_ID> --repo xnn0703/satellite_debug_tool --exit-status
```

不要从 `master` ref dispatch 发行 tag；`release-candidate` environment 只允许受保护的 `v*` ref。

工作流顺序：

1. `prepare-release` 将 tag 解析为唯一 commit，并核对源码版本。
2. Windows user/ops 矩阵分别安装约束依赖、执行全量 pytest、翻译检查、冻结构建和 frozen smoke。
3. `release_artifacts.py` 生成完整 7z、manifest 和 `receipt.json`。
4. `upload-candidates` 等待 `release-candidate` environment 人工审批。
5. 审批后用独立上传身份把两个候选写入 ECS `incoming`，并逐文件按 receipt 回读长度和 SHA-256。

必须确认两个 edition 的源码回归、构建、smoke 和上传 job 全部成功，且 ECS receipt 的版本、平台 `win`、架构 `x86_64`、build ID 和本轮 commit 一致。

远端候选路径模板：

```text
/srv/satellite-updates/incoming/<RELEASE_ID>/satellite-terminal-tool/<user|ops>/win/x86_64/<COMMIT>/
```

CI 失败时停止本版本晋升。若根因需要改源码，提交修复并发布新的 patch 版本；不要重建或覆盖旧 tag 的候选。

## 5. macOS 候选：发布 Mac 本地构建

在 tag 对应的干净 checkout 中使用固定 Python 3.11 环境：

```bash
RELEASE_PY="$HOME/.config/satellite-release/build-venv-3.11/bin/python"

"${RELEASE_PY}" scripts/release_macos.py \
  --source-commit "${COMMIT}" --version "${VERSION}" --edition both \
  --release-id "${RELEASE_ID}" \
  --config "$HOME/.config/satellite-release/upload.json" \
  --known-hosts "$HOME/.config/satellite-release/known_hosts" \
  --identity-file "$HOME/.config/satellite-release/upload_ed25519"
```

该入口完成源码验证、user/ops 构建、codesign 事实检查、Cocoa frozen smoke、完整 ZIP/CRC/身份校验和候选上传。已有相同 receipt 时复用原字节；候选目录已有不同字节则明确失败。

本地候选路径为 `release/candidates/<COMMIT>/macos/<user|ops>/`，远端路径把平台和架构替换为 `mac/arm64`。当前签名事实必须按 receipt 如实记录；未执行 Apple notarization 时不能写成“已公证”。

## 6. ops 正式更新渠道：fetch → prepare → commit

本节只处理 ops。user 不加入 TUF 更新渠道。

### 6.1 获取候选

macOS ops 使用本地正式候选。Windows ops 从 ECS 下载到全新的本地目录：

```bash
PUBLISH_PY="$HOME/.config/satellite-release/venv/bin/python"
PUBLISH_CONFIG="$HOME/.config/satellite-release/publish.json"
KNOWN_HOSTS="$HOME/.config/satellite-release/known_hosts"
PUBLISH_KEY="$HOME/.config/satellite-release/publish_ed25519"

"${PUBLISH_PY}" scripts/promote_release.py fetch \
  --config "${PUBLISH_CONFIG}" --known-hosts "${KNOWN_HOSTS}" \
  --identity-file "${PUBLISH_KEY}" \
  --remote-candidate "/srv/satellite-updates/incoming/${RELEASE_ID}/satellite-terminal-tool/ops/win/x86_64/${COMMIT}" \
  --output "/absolute/candidates/${RELEASE_ID}-windows-ops"
```

工具按 receipt 核对每个文件的长度和 SHA-256；来源 commit 仍须由发布负责人独立对照 tag。

### 6.2 本地签名准备

Windows 和 macOS 分别 prepare 到全新目录。`--min-os` 必须按本版真实最低系统要求填写：

```bash
"${PUBLISH_PY}" scripts/promote_release.py prepare \
  --config "${PUBLISH_CONFIG}" --known-hosts "${KNOWN_HOSTS}" \
  --identity-file "${PUBLISH_KEY}" \
  --repository "$HOME/.config/satellite-release/repository" \
  --key-dir "$HOME/.config/satellite-release/keys" \
  --candidate "/absolute/candidates/${RELEASE_ID}-windows-ops" \
  --expected-commit "${COMMIT}" \
  --output "/absolute/publications/${RELEASE_ID}-windows-ops" \
  --public-base-url "https://60.205.157.141/v1/apps/satellite-terminal-tool/" \
  --min-os "10.0.19041" --channel stable \
  --notes-file "doc/v${VERSION}_release_notes.md"

"${PUBLISH_PY}" scripts/promote_release.py prepare \
  --config "${PUBLISH_CONFIG}" --known-hosts "${KNOWN_HOSTS}" \
  --identity-file "${PUBLISH_KEY}" \
  --repository "$HOME/.config/satellite-release/repository" \
  --key-dir "$HOME/.config/satellite-release/keys" \
  --candidate "release/candidates/${COMMIT}/macos/ops" \
  --expected-commit "${COMMIT}" \
  --output "/absolute/publications/${RELEASE_ID}-macos-ops" \
  --public-base-url "https://60.205.157.141/v1/apps/satellite-terminal-tool/" \
  --min-os "14.0" --channel stable \
  --notes-file "doc/v${VERSION}_release_notes.md"
```

当前生产仓库已经存在可信元数据，日常发行禁止添加 `--initial`。prepare 只读取服务器状态并在发布 Mac 本地生成不可变 `publication.json`，不会切换 stable。

没有可信上一正式文件树和 manifest 时只发布完整包。当前 CI 不会自动下载旧基线或自动生成差量包；未明确生成、测量并验证 delta 时，验证记录必须写 `delta_count=0`。

### 6.3 原子提交

```bash
"${PUBLISH_PY}" scripts/promote_release.py commit \
  --config "${PUBLISH_CONFIG}" --known-hosts "${KNOWN_HOSTS}" \
  --identity-file "${PUBLISH_KEY}" \
  --bundle "/absolute/publications/${RELEASE_ID}-windows-ops/publication.json"

"${PUBLISH_PY}" scripts/promote_release.py commit \
  --config "${PUBLISH_CONFIG}" --known-hosts "${KNOWN_HOSTS}" \
  --identity-file "${PUBLISH_KEY}" \
  --bundle "/absolute/publications/${RELEASE_ID}-macos-ops/publication.json"
```

commit 的准入顺序：上传不可变 target/编号元数据但不切 timestamp；投递服务器复核请求；ECS 本机计算完整长度和 SHA-256；发布机核对 root 回执；公网只比对首/中/尾最多 4 KiB Range；全部通过后原子替换 timestamp 并推进本地账本。

这套复核不会通过公网重新下载完整包。任一回执、Range、权限、摘要或 URL 失败都不切换 timestamp。

## 7. 四份完整安装包和下载索引

### 7.1 user 完整包

user 候选在 ECS 本机通过固定工具发布：

```bash
/opt/satellite-updates/venv/bin/python \
  /opt/satellite-updates/tools/scripts/publish_manual_delivery.py \
  --candidate "/srv/satellite-updates/incoming/${RELEASE_ID}/satellite-terminal-tool/user/<platform>/<arch>/${COMMIT}" \
  --expected-commit "${COMMIT}" \
  --public-base-url "https://60.205.157.141/downloads/"
```

Windows 使用 `win/x86_64`，macOS 使用 `mac/arm64`。工具在服务器本机验证 receipt、完整 SHA-256、归档 CRC、唯一包内身份和 user 裁剪边界，再复制到 `/downloads/v<version>/`；不占用公网出口。

### 7.2 ops 完整包与索引

ops 完整包已经是 TUF 正式 target。下载目录中的友好文件名必须从该正式 target 建立服务器本地不可变链接或复制，并再次核对大小和 SHA-256；不能从未晋升的临时候选直接冒充正式 ops 包。

同一版本目录最终必须包含：

```text
satellite_terminal_manager-windows-x86_64-v<version>.7z
satellite_terminal_manager-ops-windows-x86_64-v<version>.7z
satellite_terminal_manager-macos-arm64-v<version>.zip
satellite_terminal_manager-ops-macos-arm64-v<version>.zip
SHA256SUMS.txt
release.json
```

M67 由 `scripts/release.py` 统一投递服务器请求，服务器端
`publish_release_downloads_server.py` 只消费两份已验证 user receipt 和当前 signed ops catalog，原子生成四包、
`SHA256SUMS.txt` 与 `release.json`。版本、edition、平台、架构、URL、大小、SHA-256 和源码 commit
均来自已验证机器事实，不需要也不允许手工抄写。

## 8. 发布后复核

### 8.1 fresh 更新目录检查

用独立、未缓存的 `ReleaseChecker` 分别检查 Windows x86_64 和 macOS arm64 ops：版本等于 `${VERSION}`、build ID 等于 `${COMMIT}`、`installation_mode=automatic`、完整包大小/SHA 与正式 target 一致、delta 数量符合真实发布结果。

“发现新版本”只证明签名目录和平台选择成功；真正自动更新还要独立验证下载、安装事务、重启健康回执和失败回滚。

### 8.2 服务器与公网检查

ECS 本机：

- 四包完整 SHA-256 与 receipt/TUF catalog、`SHA256SUMS.txt`、`release.json` 一致。
- ZIP 使用 `zipfile.testzip()`，7z 使用 `py7zr.SevenZipFile.testzip()` 做 CRC。
- ops 下载别名与正式 target 是相同字节；优先记录 inode/硬链接事实。

公网：

- `release.json`、`SHA256SUMS.txt` 和四包均可访问。
- 每个文件做首/中/尾 Range，检查 `206`、`Content-Range` 和总长度。
- 不做另一轮公网完整包下载；ECS 本机完整 SHA-256 已是大文件完整性门禁。

### 8.3 机器可读验证记录

每版在 `doc/` 保存 `Mxx_release_validation.json`，至少记录版本、源码 commit、tag、Actions run、两个 ops publication bundle ID、四个下载 URL/大小/SHA-256，以及以下门禁：

```json
{
  "source_tests": "PASS",
  "windows_actions": "PASS",
  "macos_frozen_smoke": "PASS",
  "server_sha256": "PASS",
  "archive_crc": "PASS",
  "fresh_release_checker": "PASS",
  "https_range": "PASS"
}
```

发布完成报告必须同时列出仍未执行的操作系统、Apple 公证和物理设备验收边界。

## 9. 失败处理

| 故障 | 正确处理 |
|---|---|
| tag 后源码/测试失败 | 停止晋升；修复后发布新 patch 版本，不移动旧 tag |
| Actions 候选上传中断 | 使用同一 receipt 和 release ID 续传；不重建候选 |
| macOS 候选已存在 | receipt 完全一致则复用；不同字节明确失败 |
| prepare 失败 | 修复原因后输出到新的空目录；不要覆盖旧 publication |
| commit 在 timestamp 切换前失败 | 保留同一 bundle 重试；已提交 bundle 应返回 `reused` |
| 远端 timestamp 已变化 | 重新 prepare 到新目录，吸收最新远端状态 |
| 发布锁残留 | 先确认旧进程已结束；仅同一 bundle owner 使用 `--recover-lock` |
| TLS CA 路径错误 | 不重建/重签；给同一命令显式设置发布 venv 的 certifi CA 后重试 |
| ECS SHA 或 Range 不一致 | stable 保持旧 timestamp；调查正式文件、Nginx 路径和回执，不绕过门禁 |
| 服务器带宽慢 | 保留 ECS 本机完整 SHA；公网只做 Range，不删除完整性门禁 |

示例 CA 设置：

```bash
SSL_CERT_FILE="$("${PUBLISH_PY}" -c 'import certifi; print(certifi.where())')" \
  "${PUBLISH_PY}" scripts/promote_release.py commit ...
```

## 10. 一键编排入口

M67 提供单入口：

```bash
# 只读计划，不创建 journal，不执行任何外部写操作
.venv/bin/python scripts/release.py plan --version X.Y.Z
.venv/bin/python scripts/release.py run --version X.Y.Z --dry-run

# 第一次运行；tag 确认值必须是计划显示的完整 commit
.venv/bin/python scripts/release.py run --version X.Y.Z \
  --confirm-tag <40位源码SHA>

# stable 前脚本会停止并显示 bundle-set SHA；确认后从 journal 续跑
.venv/bin/python scripts/release.py resume --version X.Y.Z \
  --confirm-tag <40位源码SHA> \
  --confirm-stable <64位bundle-set-SHA>

# 只读状态；发布后重新执行 fresh checker、索引和 Range 复核
.venv/bin/python scripts/release.py status --version X.Y.Z
.venv/bin/python scripts/release.py verify --version X.Y.Z
```

journal 位于 `~/.config/satellite-release/runs/vX.Y.Z/release-run.json`，阶段状态为 `PENDING/RUNNING/WAITING_APPROVAL/PASSED/FAILED`。同版本进程使用内核锁互斥；进程异常退出后锁自动释放。摘要一致的 candidate/publication 复用原字节，摘要变化时停止。

编排器自动完成预检、版本核对、测试、tag、Actions 等待、Mac 候选、fetch/prepare/commit、四包下载请求、fresh checker、Range 和验证记录。但继续保留以下人工或受保护门禁：

1. 用户明确授权 commit/tag/push。
2. GitHub `release-candidate` environment 审批。
3. stable 切换前显示不可变 commit、四包身份和 publication 摘要，并要求显式确认。
4. 私钥继续只由现有受控路径加载，不进入参数、日志或仓库。
5. 每个阶段保存状态，可用同一 release ID 续跑；禁止失败后从头盲目重建候选。

服务器首次启用 M67 还需安装并启用：

```text
deploy/updates/satellite-updates-publish-downloads.path
deploy/updates/satellite-updates-publish-downloads.service
```

并预建 `/srv/satellite-updates/receipts/downloads/{requests,results,archive}`。服务器工具只从已核验 user candidate 和当前 signed ops catalog 生成下载目录，完整 SHA/CRC 在 ECS 本机执行。

## 11. 结论边界

- pytest、翻译、构建和 frozen smoke：软件与包结构证据。
- candidate receipt、TUF、ECS SHA、CRC、Range：发行字节和公开渠道证据。
- Windows/macOS 实机安装、自动重启、健康确认和回滚：操作系统现场证据。
- Apple notarization：平台信任证据。
- 真实终端、RF、供电、摇摆台和车辆测试：设备与物理系统证据。

前两类通过不能自动推出后三类通过。发布完成时应明确列出 `PASS`、`NOT_RUN` 和阻塞项，禁止用“构建成功”替代现场验收。
