# M29 AFD01A / AFD01B2 上位机开发记录

日期：2026-09-28。状态：上位机软件实现完成；真机、RF、OTA 安装和 Windows 平台验收待执行。

对应计划：`doc/M29_AFD01A_B2_upper_pc_plan.md`；验收：`doc/M29_AFD01A_B2_upper_pc_acceptance.md`。

## 基线与设备合同

- 上位机工作树起点 `7c8690b`；本次开始时无既有修改。未执行 Git commit、push 或发版。
- 相邻设备仓 HEAD `691c793f`；A/B2 target、身份和变频板适配位于其未提交工作树。本次只读取设备源码与文档，没有修改设备仓。
- 设备端构建身份：A=`afd01a` / `AFD01A` / `0x41464403`，B2=`afd01b2` / `AFD01B2` / `0x41464404`；Product Service protocol 8。旧上位机 `afd01` 注册继续保留。设备端 A/B2 实板与 RF 仍无本次验收证据。

## 实施与计划对照

1. 在 `core/product/support_policy.py` 的单一注册表新增 A/B2 独立客户、试产、OTA 产品合同；旧 AFD01 与 C/ESA01 保持原有策略。新增注册产品枚举供试产模板选项使用，移除该对话框原有的 AFD01/C 型号硬编码。
2. 客户会话、RF 控件、安装姿态、Fleet/recipe、客户 OTA 均消费既有状态流或注册策略；没有增加型号前缀匹配、重复状态源或绕过控制门禁的分支。缺 SN 和产品错配仍按原领域状态机处理。
3. AFD01A 模型候选按 A 专属用户资源、A 包内资源、旧 AFD01 用户资源、旧 AFD01 包内资源查找；B2 保持独立模型键，缺模型时显示占位。更新签名包说明、协议注册段和工程总约定。
4. 增加客户身份及协议、RF 门禁、试产 Fleet/recipe/模板与批次、签名包制作及交叉选择、模型覆盖回归。新客户准入测试在修改注册表前稳定失败于 `afd01a` protocol 8 被判不支持。

## 软件验证

- A/B2 相关定向回归：195 passed；最终追加的身份错配和模型覆盖定向回归：72 passed。
- 最终全量 `PYTHONPATH=. .venv/bin/python -m pytest -q satellite_debug_tool/tests`：1406 passed。
- `.venv/bin/python scripts/update_translations.py update` 与 `check`：1303 messages；TS 位置信息随 UI 文件行号更新，QM 内容未变化。
- `git diff --check`：通过。计划与验收项已逐项核对。

## 待验边界

- A/B2 真机 UDP 身份、协议、能力、重连和多设备隔离；RF 指令发送、设备接受、更新遥测应用及仪表输出各自取证。
- 使用批准的 A/B2 签名测试包验证实板安装、重启回读、回滚和跨产品拒绝；仓内没有批准的密钥或正式包。
- B2 独立 STL、坐标基准与外观验收，以及 Windows 目标包显示缩放验证。
- A/B2 正式试产 recipe、供电/测试阈值必须由产品和工艺确认；本次没有代拟。
