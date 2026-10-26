# M40 客户交付中性命名验收标准

配套：[计划](M40_neutral_product_naming_plan.md)、[开发记录](M40_neutral_product_naming_dev_log.md)。

1. 中英文窗口标题、顶栏名称及语言切换结果使用中性名称。
2. Python 应用元数据、包描述、macOS Info.plist 与两种 InfoPlist.strings 使用中性名称，bundle identifier 包含中性产品标识。
3. 运行时 UI 源码与 TS/QM 中的产品名称去除公司品牌。
4. 既有 EXE/更新包名称、用户配置路径、版本号和已确认图标继续满足现有合同。
5. 报告默认公司名称保持空白；显式用户品牌配置继续生效。
6. 固件包认证域保持已发布格式值，加密包回归通过。
7. 翻译 update/check、相关主窗口/语言/图标/安全测试通过；macOS 原生窗口检查中英文标题和顶栏；差异空白检查通过。
8. 新打包后复核平台显示名称和发布 app 身份；本次记录已完成的软件检查与待验证的发行包边界。
