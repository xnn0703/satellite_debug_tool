# M13 Profile 语义层开发记录

## 2026-06-30

### 输入

用户要求继续推进；上一阶段已完成 `upper_pc_function_definition_vnext.md`，并把 M13 定义为下一阶段：Profile 语义层。

### 调研证据

- Playback 地图检测当前在 profile 中查 `gps_lat` / `gps_lon` 名称。
- Log 地图检测当前在日志列名中查 `gps_lat` / `gps_lon`。
- Live Dashboard 模式按钮当前只在 `state_id == 0` 时下发 `SET_TRACE_MODE`。
- `ProfileCache` 当前 schema_version 为 1，只保存 channels/states/events/meta。

### 本次产出

- 新增 `doc/M13_profile_semantics_plan.md`。
- 新增 `doc/M13_profile_semantics_acceptance.md`。
- 新增本开发记录。

### 当前决策

- 用户已确认继续开发，并允许下位机同步修改。
- 本轮按 M13-A 上位机本地语义层 + M13-B 下位机 `PROFILE_SEMANTICS` 扩展帧推进。
- 显式语义优先，旧名称约定作为 fallback。
- Dashboard 控制绑定首批只允许 `SET_TRACE_MODE` 白名单，避免误发未知控制命令。
- 当前下位机 debug core 对 `SET_TRACE_MODE` 返回 `NOT_SUPPORTED`，因此新语义帧暂不声明 trace mode control binding；旧 profile 仍保留 `state_id == 0` fallback。

### 验证

- `git diff --check`
  - 结果：通过。
- `rg -n "M13_profile_semantics|schema_version|semantic|state_id == 0|gps_lat|gps_lon|SET_TRACE_MODE" doc/M13_profile_semantics_*.md doc/upper_pc_function_definition_vnext.md`
  - 结果：能检索到 M13 计划、验收、开发记录和 vNext 文档索引。

### 后续实现清单

- 上位机：新增语义模型、schema v2、语义帧解码和 UI 接入。
- 下位机：新增 role/capability 注册 API、语义帧序列化、afd01/ufd45 profile 声明。
- 验证：上位机 pytest、diff check、下位机可行构建或最小静态验证。

### 已实现

- 上位机新增 `core/profile/semantics.py`，定义 channel/state role、control binding、capability 和名称 fallback。
- `DeviceProfile` / `ProfileCache` 升级到 schema v2；`profile_from_dict()` 兼容 schema v1。
- 协议层新增 `CmdType.PROFILE_SEMANTICS = 0x0C` 和 `SubCmd.REQUEST_PROFILE_SEMANTICS = 0x13`，并实现解码。
- `Handshake.feed()` 在收到语义帧时写入 `ProfileStore.apply_profile_semantics()`，但 ready 仍只依赖 META + 三张 DEFINE。
- Playback/Log 地图检测改为 role resolver；Log 虚拟 profile 会按列名生成 semantics。
- Live 3D 自动绑定改为 role 优先、名称 fallback。
- Dashboard 补齐 critical ENUM 构建：有 control binding 时显示可点击 `ModeButtonGroup`，否则显示只读 `EnumStatusChip`。
- LiveView 模式切换改为查 `control_binding`，只对白名单 `SET_TRACE_MODE(enum_value)` 下发。
- 下位机 debug core 新增 semantic role/capability 注册 API 和 `PROFILE_SEMANTICS` 序列化/广播。
- afd01/ufd45 profile 已声明主要 channel/state roles 和 capabilities；因 debug core 尚未实现 `SET_TRACE_MODE`，未声明 trace mode control binding。
- 协议规范 `doc/DEBUG设备协议接口规范_v2.md` 已补充 0x0C/0x13 和语义帧格式。

### 已验证

- `PYTHONPATH=. pytest satellite_debug_tool/tests/test_codec_v2.py satellite_debug_tool/tests/test_profile_semantics.py satellite_debug_tool/tests/test_playback_view.py satellite_debug_tool/tests/test_log_view_semantics.py satellite_debug_tool/tests/test_dashboard_control_binding.py -q`
  - 结果：48 passed。
- `cd /Users/xmac/Documents/project_manager/1-softHz/3-project_dev/3-satlite_comm_terminal/code && ./build.sh afd01 app debug`
  - 结果：构建成功；仅保留既有 warning。
- `cd /Users/xmac/Documents/project_manager/1-softHz/3-project_dev/3-satlite_comm_terminal/code && ./build.sh ufd45 app debug`
  - 结果：构建成功；输出 warning 较多但无新增编译错误。

### 待验证

- 暂无。

### 最终验证

- `PYTHONPATH=. pytest satellite_debug_tool/tests`
  - 结果：497 passed, 4 warnings。
- `git diff --check`（上位机仓库）
  - 结果：通过。
- `git diff --check`（下位机仓库）
  - 结果：通过。
