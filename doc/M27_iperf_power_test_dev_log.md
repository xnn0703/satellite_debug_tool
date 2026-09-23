# M27 iperf3 功耗测试开发日志

## 2026-09-23

- 已确认实施计划与验收边界。
- 已核对当前 `master`、客户全局页面结构、懒加载生命周期、设置持久化、
  `ExternalPowerStore` 和关闭顺序。
- 当前工作区已有 M19/M26 未提交改动；M27 采用增量集成，不回退既有内容。
- 已新增进程级 `IperfTestController`/Store：双 QProcess 独立运行 UL/DL，
  校验 iperf3 3.18+ 与 JSON-stream，强制 `-B` 地址回读一致，支持 600 秒
  循环、有限时长、独立失败重试和有界实时历史。
- 已新增会话证据目录：配置、UL/DL 原始 JSONL、网络/功耗 CSV、事件、
  summary JSON/CSV；启动即写 incomplete，正常停止后原子更新结果。
- 已新增客户全局“网络测试”懒加载页面，支持 UDP/TCP、UL/DL/BOTH、
  外部程序路径、本地 IPv4、速率/端口/时长、实时状态、曲线与事件。
- 已将测试活动状态并入 PSW 监测 owner：切换到工程/生产工作区后，活动
  测试仍持续采集功耗；应用退出先确认并收尾 iperf3，再关闭电源监测。
- 已完成中英文资源：1296 messages，0 unfinished；1024×600 offscreen 页面
  截图确认第五导航项、配置、状态和图表可访问，内容通过滚动区承载。
- 本机 iperf3 v3.21 loopback 已覆盖 UDP/TCP、UL/DL 双进程、正向/反向、
  JSON-stream、有限时长、连接拒绝重试、启动失败去重及活动进程退出收尾：
  M27 专项 11 passed。
- 相关客户/生命周期/电源/i18n 回归：69 passed。
- 全量回归：1372 passed in 60.09s；`compileall`、翻译 check、
  `git diff --check` 通过。
- 未执行：Windows 外部 iperf3.exe 冒烟、ECS 5201/5202、卫星终端路由、
  PSW 80-27 实物联动及 12/24 小时连续测试；这些保持为现场验收门禁。
