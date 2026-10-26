# M40 客户交付中性命名开发记录

配套：[计划](M40_neutral_product_naming_plan.md)、[验收标准](M40_neutral_product_naming_acceptance.md)。

## 2026-10-22：范围与证据

- 用户要求客户面向其客户使用时去除本公司名称。
- 当前品牌位于应用元数据、窗口标题、顶栏、TS/QM、macOS 本地化显示名和 bundle identifier。
- 报告默认公司名为空；`release.config.json` 使用中性仓库名；Windows EXE 与发行压缩包名称已为中性标识。
- `PACKAGE_AAD_DOMAIN` 含历史品牌字符串，是已发固件包的认证域，保留该格式值。
- 本次保留现有 M37/M38/M39 本地改动，沿用已确认图标。

## 实施与验证

当前结果：中性命名软件修改、94 项相关回归和 macOS 原生中英文显示检查完成。

### 实施

- 窗口标题改为“卫星通信终端工具 / Satellite Terminal Tool”，顶栏改为“终端工具 / Terminal Tool”；构造与 `retranslate_ui()` 使用一致源键。
- `__init__.py` 的应用名称与 pyproject 描述同步修改；移除已确认无消费者的公司名称常量。
- macOS BUNDLE 与 en/zh_CN 的 InfoPlist.strings 显示名同步；bundle identifier 改为 `com.satellite-debug-tool.desktop`，实际发行 app 的系统身份随下次打包更新。
- TS/QM 经 update/check 重新生成，移除旧品牌源键，1413 条翻译全部完成。
- 报告测试中的公司示例改为“客户公司 / Customer Company”，继续证明显式用户品牌配置可用；默认公司名保持空白。
- 固件包认证域保留原有字节并添加原因说明；EXE/更新包命名、用户配置目录、版本号、图标资源保持现有合同。
- `AGENTS.md` 与 `BUILDING.md` 同步当前命名入口。

### 对照验收

| 验收项 | 证据与结果 |
|---|---|
| 窗口与顶栏 | 新增 `test_neutral_product_name_survives_runtime_language_switch`，检查英文→中文→英文的标题与顶栏名称 |
| 翻译 | `scripts/update_translations.py update` 与 `check` 通过，**1413 messages**，0 unfinished |
| 元数据 | AST 检查 BUNDLE DisplayName 与中英文应用常量一致，包描述及 InfoPlist.strings 匹配；中性 bundle identifier 生效于配置 |
| 品牌残留 | 扫描运行时 Python/TOML/InfoPlist.strings/TS，唯一保留的历史公司字符串为固件包 `PACKAGE_AAD_DOMAIN` |
| 报告 | 默认 company_name 为空；用户配置 Customer Company 原样解析；报告与工位相关回归通过 |
| 回归 | `test_i18n.py`、`test_customer_main_window_integration.py`、`test_application_icon.py`、`test_firmware_package.py`、`test_production_reporting.py`、`test_production_configuration.py`、`test_settings_dialog.py`：**94 passed in 7.96s** |
| 原生显示 | macOS Qt `cocoa` 创建真实窗口，zh_CN/en_US × light/dark 共 4 组标题与顶栏断言通过并保存截图 |
| 差异检查 | `git diff --check` 通过 |

### 视觉证据与发行边界

`doc/screenshots/M40_neutral_name_{zh_CN,en_US}_{light,dark}.png` 保存 4 组原生窗口快照。

本次完成源码、运行时与打包配置，尚未重新生成或发布发行包；Windows/macOS 发行包显示名称与 macOS Finder/Dock 中的新 bundle identity 留待打包后验证。历史发布包、旧开发截图与历史开发记录保留原有事实。
