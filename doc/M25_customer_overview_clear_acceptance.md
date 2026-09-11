# M25 客户总览清除显示数据验收标准

## A. 界面

- [x] 客户总览顶部“录制”按钮后显示一个垃圾桶图标按钮，尺寸、颜色和 hover/ghost 风格与现有 Live 清除按钮一致。
- [x] tooltip 明确说明只清除当前设备的本地显示数据。
- [x] 主题切换后图标颜色正确更新，语言切换后 tooltip 正确更新。
- [x] Playback 模式不显示实时会话清理按钮。

## B. 单一清理逻辑

- [x] `LiveView` 只有一个公开 `clear_display_data()` 清理入口。
- [x] 原 Live 垃圾桶与客户总览垃圾桶均委托该入口，不复制 Store/UI 清理代码。
- [x] 点击后立即刷新当前总览，旧曲线、仪表、事件、状态缓存、GNSS 缓存、计数和通道选择被清除。

## C. 会话边界

- [x] 清理只作用于按钮所属的固定 endpoint，不清除其他设备的 Store 或页面。
- [x] 清理产生 0 个设备 datagram，不断开设备，不增加 generation。
- [x] 清理不停止录制，不释放或获取 operation owner，不改变 OTA、参数、RF、Tracking 或 Production 状态。
- [x] 清理不删除 SDB、日志、Profile、设置或客户设备配置。
- [x] 设备后续新数据可正常重新显示。

## D. 无设备与退出回归

- [x] 客户总览使用延迟构建的 `LiveView` 会话壳时，点击清除不会访问尚未创建的 `_chart`、`_attitude` 等工程呈现控件。
- [x] 未连接设备时点击清除正常完成，不触发异常，也不强制构建工程 Live 呈现树。
- [x] 窗口退出前有界停止后台更新检查线程，避免父窗口销毁仍在运行的 `QThread`。
- [x] 后台线程未能在边界内停止时保留窗口并明确提示，不进入部分拆除状态。

## E. 自动化验证

- [x] 客户总览、Live 清理、多设备固定 bundle 和 i18n 专项测试通过。
- [x] `python3 scripts/update_translations.py update` 与 `check` 通过。
- [x] `python3 -m compileall -q satellite_debug_tool` 通过。
- [x] `git diff --check` 通过。
- [x] 按用户当前要求，不把未执行的全量回归登记为通过。

## F. 真机边界

- [ ] AFD01C 与 ESA01 真机分别验证：清理当前设备后另一设备画面与录制连续。
- [ ] 真机验证清理期间无设备控制报文，并确认新遥测可重新填充画面。

Host/UI 自动化不能代替 F 项真机验收。
