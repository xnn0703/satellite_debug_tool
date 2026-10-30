# 外接电源开启准入状态修复计划

日期：2026-10-29。状态：用户以“修复”确认实施；源码修复与专项回归已完成，全量验证结果见开发记录。

## 1. 事实与根因

- 现场录屏：PSW80-27，电源地址 192.168.1.16:2268，界面设定 24 V / 5 A。应用参数提示成功后，开启报错 `power supply must be READY_OFF before enabling output`。
- 修复前 adapter 的 prepare_output_off() 完成关闭与参数回读后设置 READY_OFF；inspect() 在输出关闭且电压满足关闭条件时改为 OFF_CONFIRMED；enable_output() 仅准入 READY_OFF。
- ExternalPowerWorker 周期调用 read_sample()，最终进入 inspect()；用户停留及确认弹窗期间的正常轮询即可清除准入状态。
- 使用现有 ScriptedTransport 脚本插入一次 inspect() 稳定复现同一报错，并确认未发送 OUTP 1。本证据证明软件拦截，不证明其他开启条件或物理输出正常。
- 原有 prepare → enable 测试未插入后台轮询，遗漏真实调用序列。

## 2. 修复范围

- GwInstekPswAdapter 保持唯一权威 owner，区分本连接参数配置验证事实与实时输出观测，统一开启准入判断；正常轮询保留有效配置事实。
- 开启前验证当前连接、设定值、输出关闭与保护状态；参数漂移、保护及通信异常使对应准入证据失效。重连不继承此前配置授权，也不自动重放开启。
- 清理被替代的互相覆盖状态分支；不直接把任意 OFF_CONFIRMED 当作允许开启，不通过停轮询或自动重复应用参数规避。
- 检查 Customer、独立电源调试与 Production 三条 adapter 调用入口。保持现有 SCPI 命令及人工开启确认；真实状态与错误原因由领域层提供。
- 补 adapter 与真实 PswControlledSession/worker 调用序列回归；维护验收和开发记录。源码验证，打包和发版另行确认。

## 3. 工作顺序

1. 用户确认计划与验收标准。
2. 增加旧实现可复现故障的回归。
3. 修改权威模型及相邻入口，更新测试。
4. 执行电源相关与全量 pytest，按验收逐项记录证据。

