# M67 单入口发版编排器计划

日期：2026-11-12。

## 目标

在不改变 M52～M57 既有信任边界的前提下，把 [发版操作手册](RELEASING.md) 中分散的命令收敛为一个本地入口。发布人员从已经完成版本修改、发布说明和代码审查的干净 commit 出发，执行一条命令即可完成 Windows/macOS 的 user/ops 候选、ops TUF 晋升、四份完整包交付和发布证据生成。

“一键”不等于绕过审批。编排器保留两个显式确认点和 GitHub Environment 审批：

1. tag/push 前确认本轮版本、完整 commit 和工作区状态。
2. stable timestamp 切换前确认四包身份、摘要和两个 publication bundle。

## 根因与范围

当前发布能力已经存在，但事实分散在 GitHub Actions、`release_macos.py`、`promote_release.py`、`publish_manual_delivery.py`、服务器复核服务和人工验证记录中。操作人员需要手工维护相同的 version/commit/release ID、候选路径和验证结果，容易发生参数漂移、漏掉平台或重复大文件校验。

本功能只增加编排和证据汇总，不重写这些权威 owner：

- GitHub Actions 继续独占 Windows 构建。
- `release_macos.py` 继续独占 Mac 正式候选构建。
- `promote_release.py` 继续独占 TUF prepare/commit。
- ECS 本机服务继续独占正式文件完整 SHA-256。
- `ReleaseChecker` 继续独占客户端签名目录解析。
- 包内 `build_identity.json`、candidate receipt、TUF catalog 和 server receipt 继续分别作为对应阶段事实。

## 交付设计

### 1. 单入口

新增 `scripts/release.py`：

```bash
.venv/bin/python scripts/release.py run --version X.Y.Z
```

辅助命令：

```bash
.venv/bin/python scripts/release.py status --version X.Y.Z
.venv/bin/python scripts/release.py resume --version X.Y.Z
.venv/bin/python scripts/release.py verify --version X.Y.Z
```

不提供自动 `git add` 或自动源码 commit。入口要求当前 HEAD 已是待发布的干净 commit，避免把未知本地修改带入发行。

### 2. 单一发布清单与 journal

首次运行生成仓库外 journal：

```text
~/.config/satellite-release/runs/vX.Y.Z/release-run.json
```

清单冻结：version、tag、source commit、release ID、仓库 remote、四平台身份、Actions run、候选 receipt SHA、publication bundle ID、服务器回执和公开下载结果。

每个阶段只消费前一阶段已核验事实；已完成阶段按摘要复用，摘要不同则停止，禁止静默重建或覆盖。journal 不保存私钥、配置内容或访问令牌。

### 3. 阶段

1. `preflight`：检查版本源、发布说明、计划/验收、干净工作区、完整测试、翻译和 diff。
2. `tag`：显示 commit/版本，确认后创建并推送不可变 tag。
3. `windows_candidate`：发现或触发 tag workflow，等待 user/ops 构建；需要 GitHub Environment 审批时明确提示并继续等待。
4. `macos_candidate`：调用 `release_macos.py --edition both`，复用一致 receipt。
5. `candidate_audit`：聚合四个 receipt，核对 version/commit/edition/platform/architecture 和归档摘要。
6. `prepare_publications`：Windows/macOS ops 分别调用 `promote_release.py prepare`，保存不可变 bundle ID。
7. `stable_confirmation`：展示即将切换的两个 ops catalog 和四包摘要，要求第二次确认。
8. `commit_publications`：提交两个 bundle；ECS 本机完整 SHA、HTTPS Range 和 timestamp 原子切换保持原合同。
9. `publish_downloads`：请求 ECS 本机从两个 user 候选和两个已晋升 ops target 生成四份友好完整包、`SHA256SUMS.txt`、`release.json` 和只读回执。
10. `post_verify`：fresh ReleaseChecker、服务器回执、归档 CRC、下载索引和公网 Range。
11. `evidence`：生成 `doc/Mxx_release_validation.json` 候选内容和中文完成摘要；只有人工确认后才写入仓库文档。

### 4. 服务器侧下载目录收口

新增服务器本地发布工具和受限 request/result 目录，复用 M56 systemd path/service 模式：

- 请求绑定 app ID、version、source commit、四个候选/正式 target 摘要和唯一 request ID。
- root 服务拒绝链接、路径穿越、非正则文件、身份不一致及已存在不同字节。
- user 只从隔离候选发布；ops 只从已进入正式 TUF catalog 的 target 发布。
- ECS 本机执行完整 SHA-256 和归档 CRC，原子生成四包、`SHA256SUMS.txt`、`release.json`，最后写 root 回执。
- 发布账号只能投递请求和读取结果，不能伪造 root 回执或直接改下载目录。

这一步消除当前手工汇总索引的缺口，并避免 Workbench OAuth 成为日常发版依赖。

### 5. 交互与非交互边界

- 默认交互模式保留两次确认。
- CI/恢复场景可提供 `--confirm-tag <commit>` 与 `--confirm-stable <bundle-set-sha>`，值必须精确匹配编排器展示的摘要；不提供通用 `--yes`。
- GitHub Environment 审批不通过脚本模拟或绕过。
- 私钥仍从 `~/.config/satellite-release` 固定路径加载，不通过命令行正文、环境回显或 journal 传递。

## 不在本轮范围

- 自动选择或自动暂存源码改动。
- 自动修改版本号和自动生成发布说明正文。
- Apple notarization、Windows Authenticode 证书采购或签名策略变更。
- 自动生成 Windows delta 基线；增量仍需可信上一正式文件树和收益验证。
- 用软件发布证据替代 Windows/macOS 实机、设备、RF、电源或夹具验收。

## 实施顺序

1. 先实现纯读取的 manifest/journal、阶段判定和 `status`。
2. 接入 preflight、tag 和 GitHub Actions 等待，使用假远端/夹具测试。
3. 接入现有 Mac candidate 与 TUF prepare/commit，不复制其内部逻辑。
4. 实现服务器下载目录 request/service 和本地调用端。
5. 接入 fresh checker、Range 和验证 JSON 生成。
6. 对照 [验收标准](M67_one_command_release_orchestrator_acceptance.md)执行定向与全量回归，最后用一个非 stable 测试渠道或受控 dry-run 演练；真实 stable 发版单独授权。

## 风险控制

- 默认 `--dry-run` 只生成计划并验证依赖，不创建 tag、不上传、不签名、不切换渠道。
- tag 和 stable 两个破坏性边界都有内容绑定确认，确认值不能跨版本复用。
- journal 采用原子写入和 schema/version；损坏时停止，不从外部状态猜测成功。
- 任何平台失败都停止后续 stable；普通重试只复用一致候选和 bundle。
- 已公开 tag 或 version 发现不同 commit 时直接拒绝。
