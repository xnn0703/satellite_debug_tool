# M8 开发日志 — 离线 GPS 地图

> 关联 [M8_plan.md](M8_plan.md) · [M8_acceptance.md](M8_acceptance.md)
> 分支: `claude/v0.2-tabs-perf-log`（同 M7 续用）
> 起止: 2026-05-15 同日完工

## Commit 一览

```
a357cca feat(ui/M8-S3-S7): PlaybackView / LogView 地图集成 + 联动 + 事件
387228d feat(ui/M8-S2): MapWidget — Leaflet + QWebEngine 离线地图组件
3a9c42e feat(tools/M8-S1): OSM tile 离线下载 CLI + plan/acceptance 文档
```

测试演进: 219 (M7 完工) → 235 (M8-S1 +16) → 244 (M8-S2 +9) → 247 (M8-S3-S7 +3) = **247 passed, 0 failed**

---

## S1 — tile_downloader CLI

- 算法：标准 Slippy Map 公式（Web Mercator）`(lon, lat, zoom) → (x, y)`
- 限速：默认 2 req/s 遵守 OSM Fair Use
- User-Agent: `satellite_debug_tool/0.2`（OSM 要求标识请求方）
- 断点续传：已存在且非空 .png 跳过；中断后再跑只下缺的
- 用 `urllib.request` 而非 `requests`，零新依赖

南京下载估算（zoom 12-16）：≈8500 tile / 130 MB / 70 分钟。

**测试**: 16 条覆盖 WGS84 → tile（南京实测对照 OSM 在线 inspector）、bbox 枚举、zoom 解析、断点续传、User-Agent 校验。

## S2 — MapWidget

栈：`QWebEngineView` + `Leaflet 1.9.4`（bundle 144KB JS + 14KB CSS + 5 个 marker/layer PNG）。

**关键设计**：
- 单向 Python → JS：`runJavaScript` 调 `window.setTrack(...)` 等。**不引 QWebChannel**（数据流单向，无需 JS → Python 回调）
- 异步加载缓冲：HTML 未加载完时所有 `set_track/add_event/clear` 调用进 `_pending_js` 队列，`loadFinished` 后回放，避免 race condition
- 多区域支持：`~/.satellite_debug_tool/tiles/<region>/` 自动按字母序选第一个非空目录，`set_region("shanghai")` 显式切换
- 离线 fallback：没 tile 时显示 placeholder 文字指引用户运行 `tile_downloader`，应用本身不崩
- `LocalContentCanAccessFileUrls=True`：允许 file:// 协议加载本地 leaflet.js + tile

**JS 端 (map.html)**：
- `preferCanvas=true`：大量 polyline / marker 时 Canvas 比 SVG 快
- `setTrack(points)`：主 polyline (蓝) + 起点 `circleMarker` (绿 ▶) + 终点 (红 ■) + 自动 `fitBounds`
- `setTrackHighlight(startMs, endMs)`：按时间戳过滤点集，在底色轨迹上叠 `weight:5 opacity:0.95` 的橙色高亮线
- `addEvent(lat, lon, name, level)`：按 level 着色 circleMarker + popup
- `setTheme(theme)`：通过 body className 切换深浅样式

**测试**: 9 条覆盖 region 自动检测 / 显式指定 / set_region 找不到、JS 缓冲队列、空 track、主题 API。

## S3-S7 — Playback / Log 集成（一次合并）

PlaybackView 和 LogView 模式一致，但事件路径不同：
- Playback 有 EventReport（来自 .sdb），事件 marker 通过 `np.interp` 在 GPS 时间序列上算坐标
- Log 没有事件概念，只画轨迹 + 起点终点 + range 高亮

**关键 bug**（在写测试时发现）：

`ProfileStore.import_dict` 不会自动 set `current_hw_type`，只有 `apply_meta`（实时握手路径）才会 set。原 `_detect_gps_channels` 用 `current_hw_type()` 永远拿到 None，PlaybackView 加载完 .sdb 后地图按钮不会启用。

**修法**：`_detect_gps_channels(hw_type)` 接受显式 hw 参数，`_load_file` 传入本地变量 `hw`（已经是 `import_dict` 的返回值）。增加 fallback 链：current_hw_type → 第一个已知 profile（防御性）。

**起点 / 终点 marker**：完全在 JS 端实现（map.html `setTrack` 自动加），Python 侧零代码。

**range 高亮换算**：TimeRangeControl 的 sec 是相对 chart 时间原点；地图用绝对 ms（设备时间戳）。换算 `ms = first_ts_ms + sec * 1000`（Playback 路径 first_ts_ms 来自加载时记录的最小帧时间戳；Log 路径占位时间戳从 0 开始）。

**事件 marker 实时性**：PlaybackView 同时把事件接到 chart 和 map 两个 hook（`_on_event_added_for_chart` + `_on_event_added_for_map`）。地图浮窗未开时直接 short-circuit return，开了再实时上图；历史事件通过 `_refresh_map_events()` 灌入（`EventLog.all()`）。

**测试**: PlaybackView 新增 3 条 GPS 检测：无 profile / 含 GPS / 不含 GPS。

## 偏离 plan 之处

| plan 描述 | 实际做法 | 原因 |
|----------|---------|------|
| S5/S6/S7 各独立 commit | 合并到 a357cca 单 commit | 三者在同一 `_load_file` 流程内紧耦合，分开 commit 反而要回退中间状态；测试覆盖度不变 |
| MapWidget 用 `LinearRegionItem` 同步 X 视窗 | 用 polyline overlay 实现 highlight | LinearRegionItem 是 pyqtgraph 概念，地图是 Leaflet 栈 |
| plan §3.6 深色 tile | 只切 body className 调控件色，tile 本身仍用 OSM 默认 | 深色 tile 需要额外缓存一份；M8 不做（plan §6 也明确不做） |

## 已知限制（用户验收时关注）

- 用户首次跑 `tile_downloader` 前，地图浮窗显示 placeholder 文字提示，不是空白
- OSM 服务限速 2 req/s，南京 zoom 12-16 全套约 70 分钟（一次性）
- QWebEngineView 首次实例化有 1-2 秒启动开销（懒加载策略已减轻）
- `gps_lat / gps_lon` 是约定 channel 名（精确匹配），下位机协议需要按此命名；不做模糊匹配

## 用户后续验证项

详见 [M8_acceptance.md](M8_acceptance.md) 自评章节。需用户验证的关键项：
- T3：真实下载南京 tile（首次需要 70min，可在闲时跑）
- P3/P5/P6：含 GPS 的真实 .sdb 文件加载 + 事件 marker 显示
- L1：含 GPS 的真实 log 加载
- I1：三种主题切换地图样式
- H2：不含 GPS 的旧文件兼容性
