# M68 平台相关连续轨迹周期开发记录

## 2026-11-13 根因确认

- 供应商有效抓包 `供应商上位机测试(1).pcapng` 的三轴连续摇摆为 527 条位置帧，命令周期和帧内完成时间均为 50 ms。
- 控制器反馈周期约 21 ms，属于控制器状态流。
- M61 引用的 `/Users/xmac/Desktop/摇摆台.pcapng` 在 M58 中绑定本项目夹具会话，不是供应商原上位机抓包。
- 用户确认 macOS 维持已实测的 20 ms，Windows 参考供应商程序使用 50 ms。

## 实现

- `core/motion_platform/trajectory.py` 成为平台周期唯一 owner：macOS/非 Windows 为 20 ms，Windows 为 50 ms。
- 轨迹对象、预编译帧、指纹、缓停、生产配方和报告继续消费同一运行平台常量。
- Windows 时间线恢复 `timeBeginPeriod(1)` 生命周期和有界 sleep + 1 ms guard，删除全程忙等待；迟到后的不跳点、不追赶和留痕合同保持。
- 组合姿态包线继续以 20 ms 分辨率计算，只服务严格逆运动学安全。
- Windows loopback 门禁按当前平台周期验证，不再要求共享 runner 满足亚 25 ms P95。

## 验证记录

- 平台周期、运动组件、夹具 worker、夹具页面和生产配方定向回归：`72 passed in 7.18s`。
- 翻译目录检查通过：1,558 条文案，0 unfinished；`git diff --check` 通过。
- 全量回归通过：`2327 passed in 275.62s`。
- 真实 Windows 工位抓包、控制器反馈和六轴伺服错误验收待执行，不能由软件回归替代。
