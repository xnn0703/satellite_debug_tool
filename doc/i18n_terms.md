# Satellite Debug Tool 中英术语表

本表约束第一方界面和英文用户手册用词。设备上报字段、协议标识和用户内容不按本表改写。

| 中文 | English | 说明 |
|------|---------|------|
| 实时 | Live | 主实时数据页 |
| 回放 | Playback | SDB 文件回放页 |
| 日志 | Log | WindTerm 日志解析页 |
| 设备 | Device | 参数与 OTA 页 |
| 跟随系统 | System default | 语言设置 |
| 简体中文 | Simplified Chinese | 语言设置 |
| 连接 / 断开 | Connect / Disconnect | 通信链路操作 |
| 远端地址 | Remote address | UDP 目标地址 |
| 本地端口 | Local port | UDP 绑定端口 |
| 调试开关 | Debug on/off | 对应 `DEBUG_ENABLE` |
| 等待设备握手 | Waiting for device handshake | Profile 定义尚未齐全 |
| 链路正常 | Link OK | 仅表示当前链路状态 |
| 心跳 | Heartbeat | 不翻译协议字段 |
| 通道 | Channel | Profile 数据通道 |
| 通道使能 | Channel enable | 上报 mask 控制 |
| 单图 | Single chart | 所有曲线同图 |
| 分组 | Grouped | 按分组显示曲线 |
| 归一化 | Normalize | 显示变换，不改原始数据 |
| 自动 Y 轴 | Auto Y | 自动缩放 |
| 复位视角 | Reset view | 3D 视角 |
| 状态 | State | 设备状态定义与值 |
| 事件 | Event | 设备事件或用户标记 |
| 用户标记 | User mark | 曲线时间标记 |
| 采样率 | Sample rate | `SET_SAMPLE_RATE` |
| 参数管理 | Parameter management | Device Tab |
| 读取全部 | Read all | 请求参数表 |
| 恢复出厂 | Restore defaults | 设备端参数恢复 |
| 应用 | Apply | 写入单项参数 |
| 需要重启 | Reboot required | 参数属性 |
| 固件升级 | Firmware update | 界面功能标题 |
| 上传并升级 | Upload and update | OTA 主操作 |
| 正在擦除 | Erasing | OTA 阶段 |
| 正在传输 | Transferring | OTA 阶段 |
| 正在校验 | Verifying | OTA 阶段 |
| 等待设备重启 | Waiting for device reboot | OTA 阶段 |
| 检查更新 | Check for updates | 上位机自身更新 |
| 已是最新版本 | Up to date | 上位机自身更新 |
| 离线地图 | Offline map | Leaflet 本地瓦片 |
| 起点 / 终点 | Start / End | 轨迹标记 |
| 卫星数 | Satellites | GNSS 统计 |
| 信噪比 | SNR | 保留工程缩写 |
| 载噪比 | C/N0 | 保留工程符号 |
| 定位状态 | Fix status | GNSS fix |

## 风格约束

- English 使用简洁的工程术语和 sentence case。
- 动态文案必须使用完整模板和占位符，不拼接可见句子。
- `Serial/UDP/GNSS/OTA/RTK/SDB`、协议版本、硬件型号和参数键保持原样。
- 小数点、单位和 24 小时时间格式不随应用语言改变。
- 同一动作固定使用同一动词，例如连接统一为 `Connect`，参数写入统一为 `Apply`。
