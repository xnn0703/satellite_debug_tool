# Copilot 工作区指令

本仓库的唯一当前工程约定是根目录 [`AGENTS.md`](../AGENTS.md)。开始修改前必须完整阅读并遵循它；不要在本文件复制架构、协议、运行命令或测试数量，以免形成第二套过期事实。

补充入口：

- 协议权威规范：[`doc/DEBUG设备协议接口规范_v2.md`](../doc/DEBUG设备协议接口规范_v2.md)
- 当前 AFD01C 交付边界：[`doc/M22_AFD01C_upper_pc_adaptation_acceptance.md`](../doc/M22_AFD01C_upper_pc_adaptation_acceptance.md)
- 历史路线基线：[`doc/upper_pc_function_definition_vnext.md`](../doc/upper_pc_function_definition_vnext.md)
- GUI 启动：`python3 -m satellite_debug_tool.main`
- 全量测试：`PYTHONPATH=. pytest satellite_debug_tool/tests`

若本文件与 `AGENTS.md` 或协议权威规范冲突，以后两者为准并修正本文件，不得建立兼容旁路。
