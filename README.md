# 卫通终端管理工具 / Satellite Terminal Manager

卫星通信相控阵设备上位机，基于 PySide6，统一承载客户操作、内部工程诊断和自动化测试。客户交付名称为“卫通终端管理工具 / Satellite Terminal Manager”。

当前工程架构、运行约定和验证要求以根目录 [`AGENTS.md`](AGENTS.md) 为唯一权威入口。

## 功能特性

- 三工作区：默认客户工作台、会话内解锁的工程工作区、批量试产与夹具调试工作区
- 单一设备会话：同一 endpoint 只由一个 `DeviceSessionCore` 持有连接、协议解析和权威 Store；试产 Fleet 通过 `SessionRegistry` 复用会话
- 已注册客户产品：AFD01、AFD01A、AFD01B2、AFD01C、AFD01D、ESA01；按精确产品身份、协议版本和能力声明开放 Product Service，未知组合明确拒绝
- 实时调试：串口/UDP 连接、DEBUG v2 握手、profile 驱动 Dashboard / Chart / State / Event / 3D
- 数据录制与回放：流式 `.sdb v2/v3` 导入，磁盘型 `PlaybackSeriesProvider` 按时间窗口和像素预算读取大文件
- WindTerm 日志解析：`.log` 转虚拟 profile + 曲线
- 设备管理：设备信息、参数表读写、工程 OTA，以及客户签名固件包 OTA
- 自动化测试：产品隔离的 recipe、设备身份门禁、批次状态与 SDB 证据；正式工艺阈值和量产放行仍需独立验收
- 离线地图：Playback / Log 检测 `gps_lat` / `gps_lon` 后显示轨迹和事件 marker
- 应用更新：仅运维版使用 TUF 签名更新源；Windows 可自动安装，macOS 校验下载包后由用户手动替换。客户版通过人工完整包交付，不提供应用在线更新。设备固件 OTA 与应用更新分开
- 国际化：跟随系统 / 简体中文 / English，运行时即时切换
- 主题：深色 / 深色高对比 / 浅色

## 完整包下载（v1.9.0）

客户版使用产品本名，全量运维版保留 Ops 标识。完整包从阿里云直接下载，包含所需运行环境，无需安装 Python。

| 平台 | 客户版 | 全量运维版 |
|---|---|---|
| Windows x86_64（10.0.19041 起） | [下载](https://60.205.157.141/downloads/v1.9.0/satellite_terminal_manager-windows-x86_64-v1.9.0.7z?build=a867cfed9da78186db33d67b4a389279e2d8dfa7) | [下载](https://60.205.157.141/downloads/v1.9.0/satellite_terminal_manager-ops-windows-x86_64-v1.9.0.7z?build=a867cfed9da78186db33d67b4a389279e2d8dfa7) |
| macOS 14+ Apple Silicon | [下载](https://60.205.157.141/downloads/v1.9.0/satellite_terminal_manager-macos-arm64-v1.9.0.zip?build=a867cfed9da78186db33d67b4a389279e2d8dfa7) | [下载](https://60.205.157.141/downloads/v1.9.0/satellite_terminal_manager-ops-macos-arm64-v1.9.0.zip?build=a867cfed9da78186db33d67b4a389279e2d8dfa7) |

[SHA-256 校验和](https://60.205.157.141/downloads/v1.9.0/SHA256SUMS.txt) · [逐包验证记录](doc/M53_release_validation.json)。首次从旧版迁移请退出旧程序，再解压到独立目录或替换 Mac 应用。当前 Windows 包未做系统代码签名，Mac 使用 ad-hoc 签名，尚未完成 Apple 公证。

## 安装与运行

发行包分为客户版和运维全量版。客户安装目录为 `SatelliteTerminalManager`，macOS 包为 `SatelliteTerminalManager.app`；完整包归档名为 `satellite_terminal_manager-<windows|macos>-<arch>-v<version>.<7z|zip>`。运维全量版目录/app 名带 `Ops`，归档名带 `-ops-`；两版 Windows 主程序均为 `SatelliteTerminalManager.exe`。Windows 发行构建由 GitHub Actions 完成，macOS 发行构建在本机完成；完整包通过 Aliyun `https://60.205.157.141` 分发。内部 Python 包、user 构建身份、bundle identifier 和配置目录沿用既有名称。

发行状态、计划、验收与实际发布记录见 [`M53_manager_naming_release_plan.md`](doc/M53_manager_naming_release_plan.md)、[`M53_manager_naming_release_acceptance.md`](doc/M53_manager_naming_release_acceptance.md) 和 [`M53_manager_naming_release_dev_log.md`](doc/M53_manager_naming_release_dev_log.md)。

```bash
# 建议先建虚拟环境
python3 -m venv .venv
source .venv/bin/activate
pip3 install -r satellite_debug_tool/requirements.txt

# 必须使用 -m 方式运行
python3 -m satellite_debug_tool.main

# 启动即进入自动化测试工作区
python3 -m satellite_debug_tool.main --production

# 或先安装 editable 包
pip3 install -e satellite_debug_tool
python3 -m satellite_debug_tool.main
```

临时指定本次运行语言（不会改用户配置）：

```bash
SATELLITE_DEBUG_LOCALE=en_US python3 -m satellite_debug_tool.main
SATELLITE_DEBUG_LOCALE=zh_CN python3 -m satellite_debug_tool.main
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
├── core/           # 协议、会话和领域逻辑
│   ├── protocol/   # DEBUG v2 包络与 Debug/Product/Orbit 领域解码
│   ├── session/    # DeviceSessionCore、SessionRegistry 与控制器
│   ├── product/    # Product Service 注册策略、模型与权威 Store
│   ├── production/ # 批次、设备身份、recipe、夹具与证据状态
│   ├── playback/   # 后台 SDB 索引与磁盘型窗口查询
│   ├── comm/       # QThread 串口/UDP 工作线程
│   └── data/       # 实时遥测、状态、事件和 GNSS/Orbit Store
├── i18n/           # TranslationManager + TS/QM 翻译资源
├── ui/             # 三工作区、懒加载呈现与页面生命周期
└── io/             # SDB 录制、流式导入与唯一 schema
```

默认进入客户工作台。`Ctrl+Shift+E` 首次确认后在本次会话解锁工程工作区；`Ctrl+Shift+P`
解锁试产工作区。工作区切换不重连设备，也不建立第二份 endpoint 状态源。

M22 已完成 AFD01C 的软件合同与自动化验收。AFD01C 真机连续遥测、RF/平台、Windows DPI、正式
recipe、签名测试包和 3D 外观仍是独立验收项；软件测试通过不代表这些外部证据已经成立。

## 关键文档

- `AGENTS.md` — 当前工程约定与架构的唯一权威入口
- `doc/DEBUG设备协议接口规范_v2.md` — DEBUG v2 协议权威规范
- `doc/M22_AFD01C_upper_pc_adaptation_plan.md` — AFD01C 当前适配范围与根因设计
- `doc/M22_AFD01C_upper_pc_adaptation_acceptance.md` — AFD01C 软件证据与未完成真机边界
- `doc/upper_pc_function_definition_vnext.md` — M7–M16 历史功能定义与路线基线
- `doc/user_manual.md` / `doc/user_manual_en.md` — M16 工程工作区历史操作手册；不覆盖 M18–M22 新工作区
- `doc/i18n_terms.md` — 中英术语表
- `doc/acceptance_log.md` — 验收日志
- `doc/RELEASING.md` — 发版流程
- `BUILDING.md` — 打包指南
