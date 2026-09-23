# M27 iperf3 功耗测试验收标准

## 软件验收

- 客户左侧依次显示“总览 / 射频控制 / 网络测试 / 回放 / 维护”。
- 零客户设备时仍可进入网络测试页面，切换 endpoint 不重建测试控制器。
- 开始前校验 iperf3 路径、3.18 最低版本、JSON-stream、服务器、本地 IPv4、端口、速率和时长。
- UL/DL 使用独立进程；任一方向失败只令整体进入降级，另一方向继续。
- 本地地址回报必须与 `-B` 地址一致；不一致时不得显示为运行正常。
- UDP 展示吞吐、jitter、丢包、乱序；TCP 展示吞吐和可用重传数据。
- 持续模式循环 600 秒会话；有限模式准确截断最后一段；失败 10 秒后重试。
- 页面隐藏时进程和 Store 继续，绘图停止；重新激活立即呈现最新状态。
- 测试活动期间 PSW 监测跨工作区保持活动；功耗缺失不阻止网络测试并产生事件。
- 手动停止和应用退出均无遗留 QProcess，并产生完成或 incomplete 摘要。
- 会话目录包含配置、原始 UL/DL JSONL、统一测量 CSV、事件 JSONL、summary CSV/JSON。

## 自动化门禁

- M27 单元/UI/生命周期测试通过。
- 翻译 update/check、`compileall`、全量 pytest、`git diff --check` 通过。
- 本机 iperf3 3.18+ loopback 覆盖 UDP/TCP 正向和反向。

## 独立现场验收

- Windows 外部 iperf3.exe 路径和版本验证。
- 实际绑定卫星网卡 IPv4，而非 Wi-Fi。
- ECS 5201/5202、卫星链路和 PSW 80-27 下完成 12/24 小时运行。
- 下行 200K 的丢包、乱序和 jitter 作为结果记录，不以“进程在运行”替代链路判定。

## 当前验收结果

- PASS：软件实现、真实本机 UDP/TCP loopback、相关 69 项回归、全量 1372 项回归。
- PASS：翻译 1296 条/0 unfinished、`compileall`、`git diff --check`。
- BLOCKED：Windows 打包机、ECS、卫星终端和 PSW 实物不在本轮本机环境中；
  12/24 小时现场连续性及下行 200K 链路质量尚未验证。
