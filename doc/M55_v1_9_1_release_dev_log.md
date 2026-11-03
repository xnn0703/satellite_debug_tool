# M55 v1.9.1 发版记录

## 发布基线

- 开始日期：2026-11-02。
- 发布前版本：1.9.0；HEAD `3ce6228`，GitHub `master` 同步，Gitee `origin/master` 落后 15 个提交。
- 本轮范围：M54 自动化跟踪判据及其配套配置、报告、测试、翻译和文档。
- 当前 GitHub Actions 最近一次 v1.9.0 正式候选运行 `41346531510` 成功；v1.9.0 二进制由阿里云分发，GitHub Release 列表仍以 v1.8.1 为 Latest，符合当前发行 SOP。

## 验证与发布进度

### 发布前门禁

- 版本源已更新为 1.9.1；本地及 GitHub 均不存在 `v1.9.1` tag。
- 翻译检查：1505 条完成，exit 0。
- `compileall`、`git diff --check`：exit 0。
- M54 核心专项：113 passed / 5.50 s。
- 当前源码全量：2238 passed / 279.57 s，exit 0；命令 `PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q --tb=short -o faulthandler_timeout=120`。
- user 源码 smoke：1.9.1、manual update、operations modules 空、Shell/总览 PASS，正常退出。
- ops 源码 smoke：1.9.1、online update、运维模块清单存在、Shell/总览 PASS，正常退出。
- offscreen smoke 的 OpenGL context 警告来自无图形上下文平台；进程返回 0 且结构化 smoke 证据完整，不作为原生 GPU 验收。

### 发布提交与构建

- 正式源码提交及 tag：`3f89817f6e9b31dbc77c798b2f63e9d58697e244` / `v1.9.1`；tag 保持指向冻结源码，发布记录在后续文档提交中补齐。
- GitHub `master` 检查 `41449643670` 成功；tag 构建与上传 `41449699594` 成功，Windows user/ops 两个作业均完成测试、冻结构建、smoke、归档身份验证和候选上传。
- macOS 发布脚本再次完成 2238 passed / 275.91 s；user/ops 原生 smoke、ad-hoc 签名、归档 CRC/身份和候选上传通过。

### 正式晋升与公开下载

- macOS ops 初次公网完整回读因长流不完整而安全拒绝，timestamp 未切换；服务器文件 SHA、长度及首/中/尾分段一致。无并发大流量后，同一不可变 bundle `8e120d...46549` 完成完整公网 SHA 并原子发布。
- Windows ops 标准晋升两次分别在公网长流和 SFTP 大文件重用摘要阶段断开，旧 timestamp 保持。随后公网直连完整读取 217338488 字节且 SHA 为 `2f5ee632...f50fa`，服务器对 bundle 全部不可变文件重新核对大小、SHA 和 mtime；同 bundle `30ee4c...4995` 只续接 root/timestamp 原子提交并完成本地账本。
- 发布说明最初把“已实现差量能力”误写为本版已提供差量。实际 Windows 候选没有 `delta.zip`；说明已改为本版只提供完整包。macOS 同版目录复用已完整公网校验的同一包，仅上传并验证新的小型目录/元数据后提交修正版说明。
- fresh `ReleaseChecker` 分别验证 macOS/Windows 的签名目录、1.9.1、源码 SHA、完整包摘要及安装模式；两个目录 `delta_count=0`。四份直链由服务器重新计算完整 SHA；ops 直链与可信目标为同 inode 硬链接。`release.json`、`SHA256SUMS.txt` 及四包首/中/尾 HTTPS Range 校验通过。

### 下载链路事件

- 用户在 macOS 下载到 9.25 MiB 后，三个 TLS 握手均超过客户端 8 秒限制。当时正式发布的完整包公网复核占满 ECS 3 Mbps 出口；发布流量结束后，同机同网络连续 10 次真实 TLS/Range 请求均在 0.12–0.20 秒完成，服务器负载为 0 且 Nginx 无错误。
- `.part` 与大小/SHA 身份绑定，可在“重试”后断点续传。后续发布流程应把完整包 SHA/公开映射复核放在服务器内部，并让外部只验证签名目录与小范围采样，避免复核流量与用户下载争抢出口。

### 证据边界

- 机器可读结果见 `M55_release_validation.json`；公开索引为 `/downloads/v1.9.1/release.json`。
- Windows 系统签名仍为 unsigned，macOS 为 ad-hoc 签名且未做 Apple notarization。软件测试、归档、可信目录和 HTTPS 证据不替代 Windows 真实升级恢复、真实设备、供电、摇摆台及 RF 现场验收。
