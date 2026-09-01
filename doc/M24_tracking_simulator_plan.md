# M24 Tracking Simulator 计划

状态：软件实施完成，待设备联调（2026-09-01）

## 目标与边界

- Engineering 工作区增加延迟构建的 Tracking Simulator 页面。
- 复用唯一 `DeviceSessionCore`、事务租约、Profile/Data/State Store 和 SDB v3 记录链路。
- 只使用一个 20 ms `QTimer`；不增加线程、进程、socket、Mock Modem 或第二套设备会话。
- 场景 JSON schema 1 支持 GEO、RF/极化、阵面安装、姿态和链路关键帧。
- 上位机发送 Debug v2 `0x11` START/SAMPLE/STOP；设备运行真实 Tracking 和真实阵面。

## 数据模型

- 数值关键帧线性插值，yaw 走最短角；布尔量保持到下一关键帧。
- 速度和 body rate 从姿态关键帧推导。
- SNR 使用真实设备 `antenna_az/antenna_el` 命令反馈计算 pointing loss，并叠加 KA256 scan loss、遮挡、雨衰和可复现噪声。
- 波束反馈超过 250 ms、缺失或越过硬限时发送 `snr_valid=0`。

## 控制流程

1. 获取设备事务租约并发送 START。
2. 等待 `TRACKING_SIM_ACTIVE` 和 TX gate 关闭读回。
3. 通过现有 Product Service `APPLY_RF` 设置频率/极化并等待响应与遥测。
4. 启动 20 ms SAMPLE；暂停时继续发送当前样本。
5. STOP、场景结束或页面关闭释放会话；断线由设备 2 s timeout 收口。

STOP/timeout 后设备恢复原 TX 策略，UI 必须明确警告原请求可能重新开启 TX。
