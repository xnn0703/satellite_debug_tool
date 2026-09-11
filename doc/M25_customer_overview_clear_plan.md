# M25 客户总览清除显示数据计划

## 文档信息

| 项目 | 内容 |
|------|------|
| 日期 | 2026-09-03 |
| 状态 | 已实现，软件专项验证通过 |
| 验收标准 | `doc/M25_customer_overview_clear_acceptance.md` |
| 开发记录 | `doc/M25_customer_overview_clear_dev_log.md` |

## 1. 用户目标

在客户工作区“总览”页顶部操作栏增加与现有 Live/运维界面一致的垃圾桶图标，用于清除当前设备页面上的旧显示数据。

## 2. 事实与范围

- 现有 `LiveView` 已有垃圾桶按钮和唯一清理实现，负责清除本地数据曲线、仪表显示、事件、状态缓存、GNSS 缓存、计数和通道选择。
- 客户总览通过固定 endpoint bundle 复用对应 `LiveView`、Runtime/Core/Store；新按钮只作用于当前总览绑定的 endpoint。
- 清除是纯本地显示动作，不发送 UDP/串口命令，不断开设备，不改变连接 generation，不停止录制，不取消 OTA/参数/RF 等设备事务，不删除 SDB、日志、配置或 Profile。
- 当前仍由设备持续上报的数据会在清除后重新显示；按钮只清除点击时已有的本地显示数据。

## 3. 最小实现

1. 将 `LiveView` 现有私有清理槽收敛为一个可复用的公开 `clear_display_data()` 方法；原 Live 垃圾桶和客户总览按钮都调用该方法。
2. 在 `CustomerOverviewView` 顶部连接栏的“录制”按钮后增加 `30×29` 图标按钮，使用现有 `icons.icon("trash")`、ghost 样式和主题语义色。
3. 点击后只调用当前固定 endpoint 的 `LiveView.clear_display_data()`，随后立即刷新总览呈现。
4. Playback 模式不显示该实时会话清理按钮，避免把离线回放数据与当前设备会话混为一体。
5. 增加中英文 tooltip/状态文案并更新 TS/QM。

## 4. 不做的事项

- 不新增独立 Store、缓存副本、跨设备广播或第二套清理方法。
- 不把清除动作解释为设备复位、设备状态清零或命令已应用。
- 不删除录制文件、导入文件、Profile、设备配置或其他 endpoint 数据。
- 不修改多设备 Broker、Directory、Runtime 和固定 bundle 架构。

## 5. 验证

- 回归测试证明按钮使用垃圾桶图标并委托唯一清理方法。
- 回归测试证明清理当前 endpoint 不影响另一 endpoint。
- 回归测试证明清理不发送命令、不改变连接与录制状态。
- 运行客户总览、Live 清理、固定 bundle 和 i18n 专项测试。
- 运行翻译 update/check、`compileall` 与 `git diff --check`；按当前约定不执行全量回归。
