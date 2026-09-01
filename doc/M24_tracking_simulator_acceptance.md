# M24 Tracking Simulator 验收

- [x] Engineering 顶层增加第五个延迟构建页面，客户工作区不出现。
- [x] 场景 JSON schema 1 可保存/加载，空场景、乱序和越界关键帧被拒绝。
- [x] 插值、最短 yaw、速度/body rate、KA256 loss 与闭环 SNR 纯算法测试通过。
- [x] 只使用共享 `DeviceSessionCore` 和一个 20 ms `Qt::PreciseTimer`。
- [x] START/SAMPLE/STOP 使用固定小端载荷和随机非零 session ID。
- [x] 启动流程分别等待设备仿真 active、TX gate 关闭、RF 响应和遥测回读。
- [x] pause 保持样本；seek/编辑仅 stopped；场景结束自动 STOP。
- [x] 断线停止本地发送并释放租约，设备由 2 s timeout 退出；UI 明示正常 TX 策略可能恢复。
- [x] 复用现有会话发送/接收和 SDB v3，不增加 schema。
- [x] 翻译资源 check 通过；场景算法、视图生命周期和 i18n 专项共 26 项 pytest 通过。

## 待设备联调

- [ ] Debug 设备确认 START 后上报 `TRACKING_SIM_ACTIVE=1`，且 Product Service 回读 TX gate 关闭。
- [ ] 场景姿态和闭环 SNR 驱动真实 Tracking/阵面，模拟姿态不进入 Locate/ESKF。
- [ ] STOP、场景结束和断流超时均恢复真实输入；物理 TX 安全由 RF 仪表确认。
