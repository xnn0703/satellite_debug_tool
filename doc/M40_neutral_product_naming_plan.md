# M40 客户交付中性命名计划

用户要求客户交付版本去除“软赫 / SoftHertz”公司名称。本次为既有产品文案与应用元数据的直接修改，按该指令执行。

## 命名

- 中文应用名：卫星通信终端工具。
- 英文应用名：Satellite Terminal Tool。
- 顶栏简称：终端工具 / Terminal Tool。
- 图标：沿用已确认的卫星图标。
- EXE、压缩包、更新目录和用户配置目录：沿用已有中性标识 `SatelliteDebugTool` / `satellite_debug_tool`。

## 范围与实施顺序

先固定本计划与[验收标准](M40_neutral_product_naming_acceptance.md)，再修改顶栏、窗口标题、包元数据、macOS 本地化名称和 bundle identifier，更新 TS/QM，最后完成相关回归和原生显示复核，并维护[开发记录](M40_neutral_product_naming_dev_log.md)。

## 事实边界

报告公司名称默认空白，由用户的工位档案自行设置；保留用户配置。
固件包 `PACKAGE_AAD_DOMAIN` 是已发布加密文件的认证合同，保持原值并增加边界说明；它属于文件格式数据，应用界面只使用中性产品名称。
历史开发文档保留历史事实，新截图与当前使用说明采用中性名称。
