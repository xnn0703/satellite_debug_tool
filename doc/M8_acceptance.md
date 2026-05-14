# M8 验收标准

> 关联 [M8_plan.md](M8_plan.md)
> 命名延续 M7 风格（T-/M-/P-/L-/I-/D- 系列），编号在本文档独立递增

## T. tile_downloader

- [ ] **T1 单测：bbox→tile 坐标转换正确**：对 (lon=118.78, lat=32.04) zoom 14 → tile (x,y) 计算正确（与在线 OSM tile inspector 一致）
- [ ] **T2 断点续传**：第二次跑同样命令时，已存在的 tile 文件被跳过（log 显示 skipped 数）
- [ ] **T3 真实下载南京一小段**：`--bbox 118.78,32.04,118.82,32.08 --zoom 14-15 --label nanjing_test`，能跑完无异常，下到 ~/.satellite_debug_tool/tiles/nanjing_test/
- [ ] **T4 限速生效**：默认 `--rate 2` 时，相邻请求间隔 ≥ 0.5s（OSM 友好）
- [ ] **T5 User-Agent 正确**：抓包看到 `User-Agent: satellite_debug_tool/0.2`（或当前版本号）

## M. MapWidget

- [ ] **M1 创建不崩**：`MapWidget()` 实例化 + 加 root layout，QWebEngineView 加载 map.html 不报错
- [ ] **M2 set_track 灌轨迹**：传 1000 点 (lats, lons)，JS 端绘制 polyline + 起点终点 marker
- [ ] **M3 add_event marker**：传 (lat, lon, name, level)，地图出现彩色 marker（不同 level 不同颜色），点击 popup 显示 name
- [ ] **M4 clear**：clear() 后地图空，再 set_track 重新画
- [ ] **M5 离线 fallback**：没 tiles 目录时显示"请先下载 tile"提示文字而不是空白

## P. PlaybackView 集成

- [ ] **P1 GPS 检测启用按钮**：profile 含 gps_lat + gps_lon channel → 工具栏"地图"按钮可点；否则 disabled
- [ ] **P2 浮窗打开**：点"地图"按钮 → QDockWidget 浮出（默认不停靠主窗），标题"地图"
- [ ] **P3 灌入 .sdb 后轨迹显示**：加载含 GPS channel 的 .sdb → 地图自动 set_track + fit_bounds
- [ ] **P4 范围选择联动**：TimeRangeControl 选"最近 5 分钟" → 地图全轨迹仍可见但选段被高亮（粗、亮色 polyline 叠在底色上）
- [ ] **P5 事件 marker**：录制中触发的所有 event 在地图上画 marker（按 level 着色）
- [ ] **P6 起点 / 终点 marker**：第一条 GPS 点绿色 ▶；最后一条红色 ■

## L. LogView 集成

- [ ] **L1 列名检测**：log 解析后 columns 含 gps_lat + gps_lon → 启用"地图"按钮，浮窗 set_track
- [ ] **L2 无 GPS 列时禁用**：log 不含这些列时按钮 disabled（不崩）

## I. 主题 / 联动

- [ ] **I1 主题切换**：Dark / Dark-HC / Light 切换后地图样式跟随（CSS 控件颜色）
- [ ] **I2 关闭 / 重开浮窗状态保留**：地图浮窗关闭后再开，原轨迹与 marker 仍在
- [ ] **I3 同时多个 Tab 浮窗独立**：PlaybackView 和 LogView 各自的 MapWidget 互不影响
- [ ] **I4 TimeRangeControl 选段 ↔ 地图高亮 ↔ chart X 视窗** 三者同步

## D. 文档 / 测试

- [ ] **D1 CLAUDE.md 更新**：架构图加 map_widget / tile_downloader / Leaflet bundle
- [ ] **D2 M8_dev_log.md**：每阶段实施细节 / commit hash / 偏离 plan
- [ ] **D3 pytest 全过**：现有 219 passed + M8 新增（tile_downloader + map_widget smoke）
- [ ] **D4 合并到 master**：分支 review 后 merge

## H. 回归

- [ ] **H1 不影响 M7 三 Tab 基本流程**：Live 连接 / Playback 加载 / Log 解析 仍 OK
- [ ] **H2 LogView 不含 GPS 时表现与 M7 一致**：曲线 / TimeRangeControl 正常
