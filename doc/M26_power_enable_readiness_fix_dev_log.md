# 外接电源开启准入状态修复开发记录

## 2026-10-29 定位

- 已查看用户录屏，匹配到 READY_OFF 准入报错。
- 复用 test_power_supply.py 的 ScriptedTransport，在既有成功脚本的 prepare 与 enable 间插入一次 inspect。
- 修复前复现结果：after prepare=ready_off；after polling=off_confirmed；开启抛出录屏同文报错；OUTP 1 sent=False。
- 定位阶段未修改业务代码；随后用户以“修复”确认实施。整个源码验证过程没有向现场电源发送任何指令。

## 实现与逐项核对

- GwInstekPswAdapter 独占本连接的 `_configuration_verified` 事实。READY_OFF 是“已验证配置 + 回读关闭”的组合状态；普通关闭观测保持 OFF_CONFIRMED，正常后台轮询保留 READY_OFF。
- 每次开启前重新 inspect，确认输出关闭、放电与保护状态，再回读电压和电流设定值，验证通过才发送 OUTP 1。没有自动重试开启，也没有通过停止轮询规避。
- 参数漂移、轮询读失败或解析失败、保护触发、重连与归还面板控制均撤销相应配置授权。开启输出失败及超时同样撤销授权。
- 关闭完成且配置授权仍有效时，返回 READY_OFF；归还面板控制后返回普通 OFF_CONFIRMED。独立调试测试按同一语义更新，Production 的安全关闭判断原本已同时接纳两者。
- 新增 adapter 的一次/多次轮询回归，旧实现两项稳定失败，修复后通过；新增电压/电流漂移、开启前保护/输出状态变化、初次关闭观测、重连、轮询通信/解析失败回归。
- 新增真实 ExternalPowerWorker → PswControlledSession → GwInstekPswAdapter 调用链测试：模拟 SCPI 电源，应用 24 V / 5 A 后继续至少三轮轮询再开启，确认仅发送一次 OUTP 1、仅配置一次电压。模拟设备替代物理电源，保留真实线程和 Qt 信号路径。

## 验证

- 电源专项：39 passed in 1.22s。
- 翻译 check：1424 finished / 0 unfinished；本轮未修改 UI 翻译源文案。
- 首轮全量 1744 passed / 1 failed（222.24s）；失败来自试产集成测试旧 SCPI 脚本缺少新增开启前回读响应，补齐输出/保护/12 V/16 A 回读后，该文件 9 passed in 0.80s。没有放宽业务校验或测试断言。
- 最终全量 pytest：1745 passed in 220.25s（退出码 0）；最终 git diff --check 通过。
- 原生确认弹窗、现场电源实际输出及负载行为待用户验证；本轮没有打包、提交或推送。

## 源码验证命令

```bash
.venv/bin/python -m satellite_debug_tool.main
PYTHONPATH=. .venv/bin/python -m pytest satellite_debug_tool/tests
```
