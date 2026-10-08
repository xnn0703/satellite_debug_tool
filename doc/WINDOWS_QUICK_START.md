# Satellite Debug Tool - Windows Quick Start / Windows 快速使用说明

## English

1. Download the Windows `.7z` package from GitHub Releases and extract it with 7-Zip.
2. Run `SatelliteDebugTool.exe`.
3. If Microsoft Defender SmartScreen appears, select **More info** and then **Run anyway**.
4. Open **Settings**, choose **System default**, **Simplified Chinese**, or **English**, and confirm with **OK**.
5. In **Live**, select Serial or UDP, configure the connection, and select **Connect**.

The application stores user settings under `%USERPROFILE%\.satellite_debug_tool`.
See `user_manual_en.md` for the full English guide.

### Lingjing motion platform

1. Configure the Windows Ethernet adapter with `192.168.15.101` and connect it to the platform network containing controller `192.168.15.201`.
2. Start the application with `--production`, then open **Motion platform test**.
3. The application verifies and deploys the bundled vendor service, starts `ProConvert.exe` minimized, and waits for its PID to listen on UDP 9800.
4. Confirm physical clearance, emergency stop availability and fixture security before starting a motion session. A listening UDP port confirms only the service process; use MS-6222 and physical observation as motion evidence.

The vendor service requires Microsoft .NET Framework 4.8. Its writable runtime and logs are stored under `%USERPROFILE%\.satellite_debug_tool\vendor_services\lingjing-a6\2604`.

## 简体中文

1. 从 GitHub Releases 下载 Windows `.7z` 压缩包，并使用 7-Zip 解压。
2. 双击运行 `SatelliteDebugTool.exe`。
3. 如果 Microsoft Defender SmartScreen 拦截，请选择“更多信息”，再选择“仍要运行”。
4. 打开“设置”，选择“跟随系统”“简体中文”或“English”，按“确定”立即生效。
5. 在“实时”页选择串口或 UDP，填写连接参数后点击“连接”。

应用配置保存在 `%USERPROFILE%\.satellite_debug_tool`。
完整中文说明见 `user_manual.md`。

### 灵境摇摆台

1. 将 Windows 有线网卡配置为 `192.168.15.101`，并连接包含 `192.168.15.201` 控制器的平台网络。
2. 使用 `--production` 启动本软件，进入“摇摆台测试”。
3. 软件自动校验并部署随附的厂家服务，最小化启动 `ProConvert.exe`，等待该进程监听 UDP 9800，不需要再次双击厂家 EXE。
4. 开始动作会话前确认运动区域、急停和夹具固定状态。UDP 端口监听只证明服务进程就绪，平台实际运动仍以 MS-6222 和现场观察为证据。

厂家服务要求 Microsoft .NET Framework 4.8。可写运行目录和日志位于 `%USERPROFILE%\.satellite_debug_tool\vendor_services\lingjing-a6\2604`。
