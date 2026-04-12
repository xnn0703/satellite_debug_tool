# Copilot 工作区指令

此仓库包含一个基于 PySide6 的卫星通信调试工具。主要应用程序入口是 `satellite_debug_tool/main.py`，代码组织为三个主要包：

- `satellite_debug_tool/core` — 通信、协议解析和内部数据管理
- `satellite_debug_tool/io` — 导入、录制和持久数据处理
- `satellite_debug_tool/ui` — Qt 部件、对话框和主窗口

## 推荐工作流程

1. 将仓库根目录作为工作区根目录打开。
2. 从仓库根目录运行应用程序：
   - `python satellite_debug_tool/main.py`
3. 从仓库根目录运行测试：
   - `pytest satellite_debug_tool/tests`
4. 如果使用可编辑安装以进行 IDE 导入：
   - `python -m pip install -e satellite_debug_tool`

## 关键命令

- 安装运行时依赖：
  - `python -m pip install -r satellite_debug_tool/requirements.txt`
- 运行 GUI 应用程序：
  - `python satellite_debug_tool/main.py`
- 运行单元测试：
  - `pytest satellite_debug_tool/tests`

## 项目约定

- UI 逻辑属于 `satellite_debug_tool/ui`；将业务逻辑保留在 `core` 和持久性在 `io` 中。
- `satellite_debug_tool/core/protocol` 是帧解析和校验和处理的主要位置。
- 代码库使用 `satellite_debug_tool` 下的标准 Python 包导入。
- 测试在 `satellite_debug_tool/tests` 下；许多测试名称和注释是中文的。

## 文档

- `doc/开发文档.md` — 一般开发文档
- `doc/DEBUG设备协议接口规范.md` — 设备协议接口规范

## Copilot 帮助笔记

- 优先在 `satellite_debug_tool/` 内工作，而不是更改根级元数据。
- 不要假设根 `README.md`；使用 `doc/` 下的文档文件获取特定领域细节。
- 保持 UI 更改与现有的 `MainWindow`/`ChartWidget` 设计兼容 PySide6。

## 示例提示

- `帮助我在 satellite_debug_tool/core/protocol 中添加新的串行命令解析器。`
- `重构 satellite_debug_tool/ui/main_window.py 以分离 UI 设置和事件处理器。`
- `使用现有的数据录制器格式为 DataImporter 编写 pytest。`
