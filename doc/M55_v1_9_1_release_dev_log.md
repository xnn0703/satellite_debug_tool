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

### 待完成

发布提交、tag、Windows/macOS 四包构建与候选上传、正式晋升、HTTPS/CRC/SHA 回验。
