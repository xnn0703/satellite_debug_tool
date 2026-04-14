# Satellite Debug Tool

卫星通信相控阵设备调试工具，基于 PySide6 的桌面应用程序。

## 功能特性

- 实时数据曲线显示
- 串口/UDP 通信支持
- 数据录制与回放（`.sdb` 二进制格式 / `.csv` 格式）
- 3D 姿态显示（Roll/Pitch/Yaw）
- Dark/Light 主题切换

## 运行方式

```bash
# 必须使用 -m 方式运行
python3 -m satellite_debug_tool.main

# 或先安装 editable 包
pip3 install -e satellite_debug_tool --user
python3 -m satellite_debug_tool.main
```

## 运行测试

```bash
pytest satellite_debug_tool/tests
```

## 项目结构

```
satellite_debug_tool/
├── core/           # 业务逻辑（协议、通信、数据）
│   ├── protocol/   # 帧解析、CRC16校验、数据结构
│   ├── comm/       # QThread 通信工作线程
│   └── data/       # 环形缓冲区、数据存储
├── ui/             # PySide6 界面组件
└── io/             # 数据录制器、数据导入器
```
