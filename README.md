# Satellite Debug Tool

卫星通信相控阵设备调试工具，基于 PySide6 的桌面应用程序。

## 功能特性

- 实时调试：串口/UDP 连接、DEBUG v2 握手、profile 驱动 Dashboard / Chart / State / Event / 3D
- 数据录制与回放：`.sdb v2`，文件头内嵌 profile，回放 Tab 独立 DataStore
- WindTerm 日志解析：`.log` 转虚拟 profile + 曲线
- 设备管理：设备信息、参数表读写、OTA 固件升级
- 离线地图：Playback / Log 检测 `gps_lat` / `gps_lon` 后显示轨迹和事件 marker
- 仿真入口：MockModem / fake-device 对星闭环仿真
- 自动更新：Gitee/GitHub release 检查、下载、替换
- 主题：深色 / 深色高对比 / 浅色

## 安装与运行

```bash
# 建议先建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r satellite_debug_tool/requirements.txt

# 必须使用 -m 方式运行
python3 -m satellite_debug_tool.main

# 或先安装 editable 包
pip3 install -e satellite_debug_tool
python3 -m satellite_debug_tool.main
```

## 运行测试

```bash
PYTHONPATH=. pytest satellite_debug_tool/tests
pytest satellite_debug_tool/tests/test_frame_v2.py -v
pytest satellite_debug_tool/tests -k "crc"
```

没有 linter / typecheck / formatter 配置，pytest 是当前唯一自动化验证手段。

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

## 关键文档

- `doc/upper_pc_function_definition_vnext.md` — 当前上位机功能定义与后续路线
- `doc/DEBUG设备协议接口规范_v2.md` — DEBUG v2 协议权威规范
- `doc/user_manual.md` — 用户手册
- `doc/acceptance_log.md` — 验收日志
- `doc/RELEASING.md` — 发版流程
- `BUILDING.md` — 打包指南
