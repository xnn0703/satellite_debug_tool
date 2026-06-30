# M13-GNSS 验收标准

## 功能验收

| 编号 | 项目 | 验收条件 | 状态 |
|------|------|----------|------|
| G-01 | 上位机 role | `gps_num_sv` / `gps_speed` / `gps_cog` / `gps_vel_n` / `gps_vel_e` / `gps_vel_d` / `gps_cog_std` 有常量、导出和名称 fallback | 已实现 |
| G-02 | AFD01 通道定义 | AFD01 profile 注册 26..31 GNSS 运动通道，并声明对应 role | 已实现 |
| G-03 | AFD01 数据上报 | AFD01 周期 debug 喂数时上报 GPS_FIX、位置、速度、COG、N/E/D、cAcc | 已实现 |
| G-04 | UFD45 通道定义 | UFD45 profile 注册 GPS 位置、星数、速度、COG 通道，并声明对应 role | 已实现 |
| G-05 | UFD45 数据上报 | UFD45 周期 debug 喂数时上报 GPS_FIX、位置、速度、COG | 已实现 |
| G-06 | 回放保留 | 新通道通过普通 DATA_REPORT 进入 DataStore/SDB，能被曲线和回放使用 | 已实现：复用既有 channel 数据链路 |

## 回归验收

| 编号 | 项目 | 验收条件 | 状态 |
|------|------|----------|------|
| R-01 | 上位机测试 | GNSS role 测试通过，相关 profile 语义测试不回退 | 已验证：498 passed, 4 warnings |
| R-02 | 下位机构建 | AFD01 / UFD45 app debug 构建通过 | 已验证：afd01/ufd45 app debug 均通过 |
| R-03 | 差异检查 | 上位机和下位机 `git diff --check` 均通过 | 已验证 |

## 不验收项

- 不验收真实设备联调。
- 不新增地图航向箭头。
- 不改变 Dashboard critical KPI 策略。
