# M13 Profile 语义层验收标准

## 验收范围

本验收覆盖 M13-A + M13-B：上位机本地 profile 语义层、JSON schema v2、UI 消费点迁移、DEBUG v2 前向兼容语义帧，以及 afd01/ufd45 profile 语义声明。

## 功能验收

| 编号 | 项目 | 验收条件 | 状态 |
|------|------|----------|------|
| S-01 | schema v1 兼容 | 旧 profile JSON / 旧 SDB header 仍可导入，语义由名称 fallback 推断 | 已实现 |
| S-02 | schema v2 round-trip | 带 `semantics` 的 profile 导入再导出，roles/control/capabilities 不丢失 | 已实现 |
| S-03 | channel role 查询 | `find_channel_by_role(hw, "gps_lat")` 可返回显式 role 通道 | 已实现 |
| S-04 | state role 查询 | `find_state_by_role(hw, "trace_mode")` 可返回显式 role 状态 | 已实现 |
| S-05 | control binding | trace mode 不在 `state_id == 0` 时，Dashboard 点击仍下发 `SET_TRACE_MODE(enum_value)` | 已实现 |
| S-06 | legacy fallback | 无 semantics 时，旧 `state_id == 0` trace mode 行为不变 | 已实现 |
| S-07 | Playback 地图 | 通道名不是 `gps_lat/gps_lon` 但带 role 时，地图按钮可启用 | 已实现 |
| S-08 | Log 地图 | 日志列名能生成或推断 GPS role，地图行为不退化 | 已实现 |
| S-09 | 3D 绑定 | roll/pitch/yaw/antenna_az/antenna_el role 优先于名称匹配 | 已实现 |
| S-10 | capability 查询 | `has_capability()` 对显式 true/false/缺省值返回正确 | 已实现 |
| S-11 | 语义帧解码 | 上位机能解码 `PROFILE_SEMANTICS(0x0C)` 并写入 ProfileStore | 已实现 |
| S-12 | 下位机语义上报 | debug core 能注册 role/capability 并在 DEFINE 广播中发送语义帧 | 已实现 |
| S-13 | 不误报可控 | 下位机未实现 `SET_TRACE_MODE` 时，不通过语义帧声明 trace mode control binding | 已实现 |

## 回归验收

| 编号 | 项目 | 验收条件 | 状态 |
|------|------|----------|------|
| R-01 | 全量测试 | `PYTHONPATH=. pytest satellite_debug_tool/tests` 全绿 | 已验证：497 passed |
| R-02 | 文档检查 | `git diff --check` 通过 | 已验证：上位机/下位机仓库均通过 |
| R-03 | 旧数据回放 | 现有不带 semantics 的 SDB v2 测试继续通过 | 已验证：全量测试通过 |
| R-04 | UI 不误隐藏 | capability 字段缺失时，不隐藏现有参数/OTA/控制入口 | 已实现 |
| R-05 | 旧固件兼容 | 缺失语义帧时，旧名称 / `state_id == 0` fallback 行为不退化 | 已实现 |
| R-06 | 下位机构建 | `cd code && ./build.sh afd01 app debug` 或可行的最小构建验证通过 | 已验证：afd01/ufd45 app debug 均通过 |

## 不验收项

- 不验收真实设备联调。
- 不验收 UI 语义编辑器。
- 不验收 CSV 导入导出。

## 通过标准

所有 S/R 项完成并记录证据后，M13-A 才算完成。若用户决定同步做协议 v2.x，则另开 M13-B 验收文档。
