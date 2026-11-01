# 卫通终端管理工具发行 SOP

适用 M52/M53 发行链路。当前发行版本为 v1.9.0；本文件描述操作流程，逐包公开状态及验收结果见 [发行验证记录](M53_release_validation.json)。源码与 tag 使用 GitHub `github` 远端，Windows 使用 GitHub Actions，Mac 使用本地 Python 3.11，完整包和 ops 可信更新由阿里云 ECS HTTPS 服务分发。

## 版本与迁移合同

- 版本唯一来源是 `satellite_debug_tool/__init__.py.__version__`；tag 必须为 `v<version>`。user/ops、Windows/macOS 四包使用同一干净源码 SHA。
- 文件名称统一由 `satellite_debug_tool/package_names.py` 定义。Windows 主程序为 `SatelliteTerminalManager.exe`，两版目录为 `SatelliteTerminalManager`、`SatelliteTerminalManagerOps`；Mac 使用对应同名 app。客户完整包归档使用 `satellite_terminal_manager-{platform}-{arch}-v{version}.{extension}`，运维全量包增加 `-ops-` 标识；内部 edition 继续用于构建身份校验。
- 客户完整包名为 `satellite_terminal_manager-<windows|macos>-<x86_64|arm64>-v<version>.<7z|zip>`，运维全量版为 `satellite_terminal_manager-ops-<windows|macos>-<x86_64|arm64>-v<version>.<7z|zip>`。历史 <=1.8.1 包保持原样，首次人工安装完整 v1.9.0 后进入新站合同。
- 内部 Python 包、app/repo 信任身份、bundle identifier、配置目录和设备固件认证域保持原合同。
- user 使用人工完整包交付；ops 使用 TUF 签名更新。Windows 支持自动安装，Mac 校验下载后由人员退出旧程序并替换 app。首个正式可信版本没有上一基线，只发布完整包。

## 准备与提交

先完成 plan/验收文档、开发记录、发布说明和本轮差异审计。全量 pytest、翻译 check、两版源码启动与退出通过后，按用户授权提交本轮功能和版本变更；仅暂存已审计范围。

```bash
PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q
.venv/bin/python scripts/update_translations.py check
git status --short
git diff --check
```

确认单一版本源、发布说明及干净 commit 后创建并推送对应 tag，例如本轮 `v1.9.0`。已公开的版本和标签保持不可变，修复使用新版本。

## Windows 候选

推送 tag 或使用 workflow_dispatch 指定已有 tag，`.github/workflows/build.yml` 精确 checkout tag SHA。Windows Runner 使用 Python 3.11 构建 user/ops，两版均完成测试、冻结 smoke、资源/身份/归档校验后，通过受保护 `release-candidate` environment 自动上传 ECS 暂存区。

检查 Actions 两版结果、smoke artifact、候选 receipt 的版本/edition/平台/架构/SHA 和摘要。pytest 与翻译分别作为独立 step 接收退出码，完整测试失败阻止构建。普通分支/PR 只生成 artifact。工作流只上传候选，不创建 GitHub Release，不持有正式签名私钥。

## macOS 候选

在本机干净源码、Python 3.11 环境运行正式发行入口。以下占位路径指向仓库外部署配置、known_hosts 与上传身份，不将私钥写入仓库：

```bash
.venv/bin/python scripts/release_macos.py \
  --source-commit <完整源码SHA> --version 1.9.0 --edition both \
  --release-id <本次唯一发行ID> --config <部署配置JSON> \
  --known-hosts <known_hosts文件> --identity-file <上传身份私钥>
```

脚本执行构建、签名验证、冻结 smoke、候选校验及自动上传；同一 SHA 的已完成候选可根据 receipt 复用。普通 `./scripts/build_macos.sh both` 仅用于本地构建，不替代候选上传流程。

spec 声明 macOS `LSMinimumSystemVersion=14.0`，冻结依赖实际最低系统要求仍需审计。ad-hoc 签名校验与原生系统兼容性分别记录，不声明已通过 Apple 公证。

## 正式签名与公开下载

四份候选完成归档 CRC、SHA-256 和身份核对后，发布人员使用仓库外签名材料执行 ops 晋升。`scripts/promote_release.py` 的 `fetch → prepare → commit` 分别取得候选、离线签名生成发布 bundle、提交服务器。调用前以该脚本 `--help` 确认必填参数；prepare 必须冻结 `--expected-commit`、目标系统 `--min-os`、正式通道和发布说明。`--initial` 只用于服务器尚无仓库元数据的首次建站；已有签名根和空 targets 的站点使用普通 prepare，吸收服务器续签后的 timestamp/snapshot。

ops 晋升 stable 时，TUF target 的 app/repo/edition/平台/架构/版本/源 SHA 必须与候选一致。先提交不可变 target 和版本元数据，最后提交 timestamp，并验证公开 HTTPS 元数据的签名、有效期与包摘要。首轮没有上一基线，不生成或宣称差量更新。

user 完整包公开到服务器下载目录，保留候选身份与摘要，不加入 ops 更新渠道。四份完整包均提供 `/downloads/v1.9.0/<完整归档文件名>` 直链，附 SHA256SUMS 和验证记录；交付采用服务器链接，不把包另存至本机 Downloads。提供直链前实际检查 HTTPS 下载、归档 CRC、包内身份和 SHA-256。

## 交付证据

发行记录逐包保存：版本、edition、系统/架构、文件名、大小、SHA-256、源码 SHA、候选 receipt、CI/本机构建与冻结 smoke 结果。四包与 tag SHA 一致、公开下载及 ops 元数据验证完成后，才报告本轮发布完成并提供直链。

user 包应裁剪应用 updater 和在线源；ops 更新应验证载荷、当前安装与健康回执。Windows 实机升级、原生显卡渲染、设备固件 OTA、RF 与夹具动作独立验收；构建和冻结 smoke 成功不能代替这些结论。

候选上传失败依据 receipt、上传日志及远端状态调查；晋升失败依据发布 bundle、服务器发布锁和 journal 调查。正式签名与上传身份分开管理，恢复发布锁前先确认旧发布进程已经停止。

构建及模块裁剪细节见 [构建指南](../BUILDING.md)，本轮范围与验收见 [M53 计划](M53_manager_naming_release_plan.md) 和 [验收标准](M53_manager_naming_release_acceptance.md)。
