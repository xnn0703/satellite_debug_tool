# M8 — 离线地图回放（GPS 轨迹 + 事件标记）

> 状态：草案，开发完毕后合并到 master
> 关联：[M8_acceptance.md](M8_acceptance.md)
> 工作分支：复用 `claude/v0.2-tabs-perf-log`（M7 完工后直接续上）

## 1. 背景与目标

用户开始在数据中加 GPS 位置（`gps_lat` / `gps_lon` / 可选 `gps_alt`），需要：
- 回放 `.sdb` 时，根据 GPS 数据在**离线**地图上画轨迹
- 事件 (EventReport) 在地图上加 marker
- WindTerm log 解析后也支持地图（log 里有 lat/lon 列时）
- 地图作为浮窗，不占现有曲线区域，可拖动 / 显示 / 隐藏

**关键约束**：应用本体**完全不联网**。

## 2. 整体方案

```
首次（用户在有网环境）：
  python -m tools.tile_downloader --bbox 118.3,31.2,119.2,32.6 --zoom 12-16 --label nanjing
  → 下载到 ~/.satellite_debug_tool/tiles/nanjing/{z}/{x}/{y}.png

之后（车载/外场）：
  app 启动 → 检测 ~/.satellite_debug_tool/tiles/* → 注入到 Leaflet
  → 完全离线运行，无网络请求
```

**数据流**：
```
Playback / Log Tab
  DataStore 检测 gps_lat + gps_lon channel 存在
  → 启用工具栏"地图"按钮
  → 点击 → 浮窗 MapWidget 显示
  → set_track(lats, lons) 画轨迹
  → 每个 event → add_event(lat_at_event_ts, lon_at_event_ts, name, level)
  → TimeRangeControl.range_changed → MapWidget.set_track_highlight(ts_start, ts_end)
```

**栈**：
- `PySide6.QtWebEngineWidgets.QWebEngineView` 嵌入 HTML
- Leaflet 1.9.x（开源 MIT；JS + CSS 文件 bundle 到 `satellite_debug_tool/ui/assets/leaflet/`）
- `QtWebChannel` Python ↔ JS 双向通信
- tile 用 `file://` URL 直读本地缓存目录

## 3. 详细设计

### 3.1 tile_downloader CLI（`tools/tile_downloader.py`）

```bash
python -m tools.tile_downloader \
    --bbox 118.3,31.2,119.2,32.6 \
    --zoom 12-16 \
    --label nanjing \
    [--source osm] [--cache ~/.satellite_debug_tool/tiles] [--rate 2]
```

- `--bbox lon_min,lat_min,lon_max,lat_max`
- `--zoom 12-16` 或 `--zoom 12,13,14,15,16` 支持范围 / 列表
- `--label` 子目录名（多区域共存：nanjing / shanghai / ...）
- `--rate` 限速（OSM fair use 默认 2 req/s）
- `--source osm` 默认 `https://tile.openstreetmap.org/{z}/{x}/{y}.png`
  - 后续可加 `--source mapbox --token xxx` 等（M8 不实现）
- User-Agent 加 `satellite_debug_tool/0.2`（OSM 要求）
- 断点续传：已存在 tile 跳过

**容量估算**（南京 bbox ≈ 1° × 1.4°，zoom 12-16）：

| zoom | tile 数 | 累计 (MB) |
|------|---------|----------|
| 12   | ~25     | 0.4       |
| 13   | ~100    | 1.5       |
| 14   | ~400    | 6         |
| 15   | ~1600   | 25        |
| 16   | ~6400   | 100       |
| **合计** | ~8500 | **~130 MB** |

下载耗时（2 req/s 限速）≈ 71 分钟。可接受（一次性）。

### 3.2 MapWidget（`ui/map_widget.py`）

```python
class MapWidget(QWidget):
    """嵌入 Leaflet 的浮窗组件。"""

    def __init__(self, tiles_dir: Path | None = None, parent=None):
        ...
        # QWebEngineView 加载 ui/assets/map.html
        # 检测 tiles_dir 子目录（按 label）→ 注入 JS Layer 列表
        # 没 tiles → 显示居中提示

    # 公共 API
    def set_track(self, timestamps_ms: np.ndarray, lats: np.ndarray, lons: np.ndarray) -> None
    def set_track_highlight(self, start_ms: float, end_ms: float) -> None
    def add_event(self, timestamp_ms: float, name: str, level: int, lat: float | None = None, lon: float | None = None) -> None
    def clear(self) -> None
    def fit_track_bounds(self) -> None
    def set_theme(self, theme: str, scale: str = "small") -> None
```

`assets/map.html`：单文件，内嵌 leaflet.css + leaflet.js（约 150 KB），定义：
- `setTrack(lats, lons)` → 主 polyline + 起点 marker（绿色 ▶）+ 终点 marker（红色 ■）
- `setTrackHighlight(startIdx, endIdx)` → 高亮 polyline（粗、亮色）叠在底色 polyline 上
- `addEvent(lat, lon, name, level)` → 按 level 着色 marker + popup
- `clearAll()`
- `fitBounds()`

Python ↔ JS：用 `QWebChannel` + `QObject.runJavaScript("api.setTrack(...)")` 异步调用。

### 3.3 事件位置内插

EventReport 自带 `timestamp_ms`，但事件不携带 lat/lon。需要根据时间戳从 DataStore 的 gps_lat / gps_lon 通道**线性内插**：

```python
def gps_at_time(data_store, ts_ms: float) -> tuple[float, float] | None:
    lat_buf = data_store.get_channel("ch_<gps_lat_id>")
    lon_buf = data_store.get_channel("ch_<gps_lon_id>")
    if lat_buf is None or lon_buf is None:
        return None
    times = lat_buf.get_times()
    lats = lat_buf.get_values()
    lons = lon_buf.get_values()
    if times.size == 0:
        return None
    # numpy.interp 在时间轴外插值时返回端点（不外推），符合直觉
    return float(np.interp(ts_ms, times, lats)), float(np.interp(ts_ms, times, lons))
```

### 3.4 浮窗集成（PlaybackView / LogView）

`QDockWidget` 作为浮窗承载 MapWidget：
- PlaybackView / LogView 内部新建 `_map_dock = QDockWidget("地图")`
- `_map_dock.setFloating(True)` 默认浮出
- 顶部工具栏新增"地图"按钮 → `_map_dock.setVisible(not self._map_dock.isVisible())`
- 只在 DataStore 有 gps_lat + gps_lon channel 时启用按钮

### 3.5 检测 GPS channel 的策略

约定 channel 名（精确匹配）：
- `gps_lat` / `gps_lon` / 可选 `gps_alt`

LogView 路径：解析后 columns 列表里查这两个名字（精确匹配，case-sensitive）。
PlaybackView 路径：profile 的 channel 列表里查 name 字段。

不找的话隐藏地图按钮。**没有模糊匹配**避免误识别。

### 3.6 浮窗 + 主题

- MapWidget 接 `set_theme(theme, scale)`
- 浅色主题：用 OSM 默认 tile（亮）
- 深色主题：用 OSM 默认 tile（同上，但叠半透明深色蒙版）—— M8 不做深色 tile（需要额外缓存一份）
- HTML 里用 CSS 调 leaflet 控件颜色匹配主题

## 4. 文件改动清单

| 文件 | 改动 |
|------|------|
| **新** `tools/tile_downloader.py` | OSM tile 离线下载 CLI |
| **新** `satellite_debug_tool/ui/assets/leaflet/leaflet.js` | bundle Leaflet 1.9.x |
| **新** `satellite_debug_tool/ui/assets/leaflet/leaflet.css` | Leaflet 样式 |
| **新** `satellite_debug_tool/ui/assets/map.html` | 单文件 HTML + JS bridge |
| **新** `satellite_debug_tool/ui/map_widget.py` | MapWidget(QWidget) 封装 |
| `satellite_debug_tool/ui/playback_view.py` | 加 "地图" 按钮 + QDockWidget + GPS 检测 + 接 range_changed |
| `satellite_debug_tool/ui/log_view.py` | 同 PlaybackView |
| `satellite_debug_tool/ui/__init__.py` | 导出 MapWidget |
| `satellite_debug_tool/core/data/data_store.py` | （可选）加 `gps_at_time(ts)` helper |
| **新** `satellite_debug_tool/tests/test_map_widget.py` | API smoke + JS bridge |
| **新** `satellite_debug_tool/tests/test_tile_downloader.py` | 单测 URL 拼接 / 跳过已下载 |

## 5. 实施阶段

| 阶段 | 内容 | 验收锚点 |
|------|------|---------|
| **S1 tile_downloader** | CLI 工具 + bbox→tile 转换 + OSM 下载 + 单测 | T1, T2 |
| **S2 MapWidget** | HTML + JS bridge + Python 包装 + smoke | M1-M3 |
| **S3 Playback 集成** | QDockWidget 浮窗 + GPS 检测 + 启用按钮 + 灌入 track | P1, P2 |
| **S4 Log 集成** | LogView 同 S3，复用 MapWidget | L1 |
| **S5 联动 + 事件** | TimeRangeControl 高亮 + 事件 marker + 起点终点 | I1-I4 |
| **S6 文档收尾** | CLAUDE.md / M8_dev_log.md / acceptance 自评 + 合并到 master | D1-D4 |

## 6. 不做的事

- 不做实时 LiveView 地图（用户明确不要）
- 不做深色 tile（用 OSM 默认 + CSS 蒙版即可）
- 不做 mapbox / 高德 / 百度（OSM 够用，未来扩展点 `--source`）
- 不做地图路径规划 / 距离测量等高级功能
- 不做卫星图 / 地形图
- 不做 mbtiles 直读（用 file:// 直接读 tile 目录最简单）

## 7. 风险与权衡

| 风险 | 缓解 |
|------|------|
| OSM fair use 限制（≤2 req/s）下载南京要 70 分钟 | 用户在闲时跑一次即可；CLI 显示进度条 + ETA |
| QWebEngineView 启动慢（首次几秒） | 浮窗按需创建（点"地图"时才创建），不影响打开 Tab |
| Leaflet bundle 进项目 ~150KB | 一次性，可接受 |
| WebChannel 双向通信延迟 | 单次调用 < 10ms；灌入 10 万点用 Polyline 一次性传 OK |
| 用户首次没下 tile，启动 app 看到的是空地图 | HTML 显示提示文字 "未检测到 tile 目录，请先运行 tile_downloader" |
| QWebEngineView 在某些 Linux/嵌入环境不可用 | 启动时 try import；不可用则禁用"地图"按钮并提示 |
