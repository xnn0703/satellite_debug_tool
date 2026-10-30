# M43 Shell 输入与终端回显开发记录

## 2026-10-27：范围与基线

用户确认三项修复范围后建立计划及验收标准。当前版本 1.8.0；工作树起点干净。已读取原始会话 TX/RX 并定位 UTF-8 输入、字节退格和历史编辑链；旧实现重复解码缺陷已通过 `E2` / `80 94` 探针复现。

实现、旧实现失败证据、专项与全量测试结果、计划逐项 review 及现场状态在开发过程中继续记录。

## 实现与 owner 核对

- `core/tcp_shell/terminal.py` 集中 ASCII 文字提交、增量 UTF-8/CSI/OSC 解析和终端行单元格模型；Qt 文档为有界模型投影，原始 TX/RX 仍由 recorder 保存。屏幕保留 5000 行，单行 16384 格，控制序列缓冲 1024 B。
- 普通文字的键盘、输入法 commit、平台粘贴快捷键、右键粘贴共享整次校验；拒绝提示只包含代码点，整次发送字节为空。合法 `_` 保持 5F。控制键、TCP/串口和 YMODEM 使用原有各自入口。
- 每批 RX 解码一次；tokens 同时供日志与屏幕消费。正文/HEX 各自持有 Qt 文档，HEX 可见时正文模型持续更新。断线 flush 截断 UTF-8/转义序列；新连接开始新的解码状态并清理旧反馈。
- 删除 `_TerminalDecoder`、正则转义管线、`_pending_cr`、`_overwrite_mode`、`_line_stamped`、UI SGR 状态和以 QTextDocument 删除 BS 的旧模型。已有 BS 测试改为“光标左移、后续覆盖”，移除 `or True` 无效断言。
- 新增纯模型与 UI 回归以及 132 条脱敏 TX/RX fixture，仅保存参数失败窗口，登录凭据和参数列表排除。用户模块合同增加共享 terminal 模块，AGENTS 和中英文客户指南同步。

## 回归证据

- 旧实现：`test_shell_terminal_reliability.py` 首批五项回归 **5 failed in 1.10s**，对应 UTF-8 分包、BS、水平光标跨包、输入法破折号按键、混合粘贴。日志 `/tmp/sdt-m43-red.log`。
- 同五项新实现全部通过。模型/UI/主题/edition 初步专项 **119 passed in 2.67s**；补充 TCP/串口、worker/controller 与 YMODEM 专项后 **169 passed in 8.83s**，日志 `/tmp/sdt-m43-shell-final.log`。
- 首轮全量：`PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests -q` → **1679 passed in 202.90s**，日志 `/tmp/sdt-m43-full.log`。最终审查发现末列 EL1 清除多扩展一格，新增回归先复现失败后修复；最终全量为 **1680 passed in 201.52s**（`/tmp/sdt-m43-full-final.log`），专项为上述 169 项通过。
- 翻译 update/check：**1423 finished / 0 unfinished**；TS/QM 同步。compileall 与差异检查通过。
- 两版源码离屏启动：`main_user` / `main` 的总览、Shell 和退出均 PASS；用户版 operations_modules 为空。输出 `/tmp/sdt-m43-user-smoke.json`、`/tmp/sdt-m43-ops-smoke.json`。
- 中英文提示已按全局深色调色板生成预览，路径 `/tmp/sdt-m43-input-feedback-{zh,en}.png`；输入提示位于终端上方并完整显示。离屏布局检查不替代原生输入法现场验收。

## 计划逐项 review

1. 单次解析与原始字节 owner：通过；二分全切点、逐字节、SGR/OSC、异常字节和日志共享解码覆盖。
2. 终端模型替换：通过；BS/CR/LF/EL/水平移动、宽字符/组合字符、主题、选区、时间戳、清屏和有界历史覆盖。
3. 统一输入预检：通过；键盘/IME/快捷键与菜单粘贴、整次拒绝、ASCII 下划线、控制键与传输中禁用输入覆盖。
4. 双版共享及相邻功能：通过；用户模块合同、源码两版启动、TCP/串口、原始证据与 YMODEM 专项覆盖。
5. 验证交付：软件项通过；现场 macOS/Windows 输入法和设备 TCP/串口编辑待用户验证。参数持久化、运行坐标及 RF 使用各自设备/物理证据。

## 源码运行

```bash
# 用户版
.venv/bin/python -m satellite_debug_tool.main_user
# 运维版
.venv/bin/python -m satellite_debug_tool.main
```

本轮按用户约定完成源码验证，打包和发版由后续明确指令触发。
