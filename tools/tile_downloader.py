"""OSM tile 离线下载工具（M8-S1）。

供用户在**有网络**的环境一次性下载某区域的 tile 到本地，之后
satellite_debug_tool 应用本体完全离线运行。

用法::

    python -m tools.tile_downloader \\
        --bbox 118.3,31.2,119.2,32.6 \\
        --zoom 12-16 \\
        --label nanjing

下载到 ``~/.satellite_debug_tool/tiles/{label}/{z}/{x}/{y}.png``，下次启动
satellite_debug_tool 时 MapWidget 自动检测该目录。

OSM Fair Use：默认限速 2 req/s + 加 User-Agent，必要时 ``--rate`` 调整。
建议夜间/带宽空闲时跑；南京 zoom 12-16 约 8500 tile / 130 MB / 70 分钟。
"""

from __future__ import annotations

import argparse
import math
import ssl
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional


def make_ssl_context(insecure: bool = False) -> Optional[ssl.SSLContext]:
    """构造 HTTPS SSL 上下文。

    macOS 上 python.org 安装的 Python 不走系统证书库，默认 urllib 验证会报
    CERTIFICATE_VERIFY_FAILED。优先用 certifi 的 CA bundle；insecure=True 时
    跳过验证（OSM 公开瓦片，低风险兜底）。
    """
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        # certifi 不可用 → 用默认（可能仍失败，提示用户加 --insecure）
        return ssl.create_default_context()


__all__ = [
    "TileCoord",
    "lat_lon_to_tile",
    "iter_tiles",
    "parse_zoom_spec",
    "download_tiles",
    "DEFAULT_CACHE_DIR",
    "DEFAULT_TILE_URL_TEMPLATE",
    "USER_AGENT",
]


DEFAULT_CACHE_DIR = Path.home() / ".satellite_debug_tool" / "tiles"
DEFAULT_TILE_URL_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "satellite_debug_tool/0.2"


@dataclass(frozen=True)
class TileCoord:
    z: int
    x: int
    y: int

    def to_path(self, root: Path) -> Path:
        return root / str(self.z) / str(self.x) / f"{self.y}.png"


def lat_lon_to_tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    """WGS84 经纬度 → Web Mercator tile 坐标（OSM 标准）。

    返回 (x, y)。zoom 越大数字越大（越细）。
    参考: https://wiki.openstreetmap.org/wiki/Slippy_map_tilenames
    """
    if not -85.0511 <= lat <= 85.0511:
        raise ValueError(f"lat out of Web Mercator range: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise ValueError(f"lon out of range: {lon}")
    n = 2 ** zoom
    lat_rad = math.radians(lat)
    x = int((lon + 180.0) / 360.0 * n)
    y = int(
        (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n
    )
    # 边界保护
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def iter_tiles(
    bbox: tuple[float, float, float, float],
    zooms: list[int],
) -> Iterator[TileCoord]:
    """枚举 bbox 在每个 zoom 下覆盖的所有 tile。

    bbox = (lon_min, lat_min, lon_max, lat_max)
    """
    lon_min, lat_min, lon_max, lat_max = bbox
    if lon_min > lon_max or lat_min > lat_max:
        raise ValueError(f"invalid bbox: {bbox}")
    for z in zooms:
        # 注意 y 方向：纬度大 (北) → y 小
        x0, y0 = lat_lon_to_tile(lat_max, lon_min, z)
        x1, y1 = lat_lon_to_tile(lat_min, lon_max, z)
        for x in range(min(x0, x1), max(x0, x1) + 1):
            for y in range(min(y0, y1), max(y0, y1) + 1):
                yield TileCoord(z=z, x=x, y=y)


def parse_zoom_spec(spec: str) -> list[int]:
    """解析 '12-16' 或 '12,13,15'。"""
    spec = spec.strip()
    if "-" in spec and "," not in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


def parse_bbox(spec: str) -> tuple[float, float, float, float]:
    parts = [float(x) for x in spec.split(",")]
    if len(parts) != 4:
        raise ValueError(f"bbox must be 4 comma-separated floats: {spec}")
    lon_min, lat_min, lon_max, lat_max = parts
    return (lon_min, lat_min, lon_max, lat_max)


def download_tile(
    tile: TileCoord,
    url_template: str,
    dest_path: Path,
    timeout: float = 15.0,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> bool:
    """下载单个 tile。返回 True 表示成功（或已存在）。失败抛异常。"""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    url = url_template.format(z=tile.z, x=tile.x, y=tile.y)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_suffix(".png.tmp")
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            data = resp.read()
        tmp_path.write_bytes(data)
        tmp_path.replace(dest_path)
        return True
    except Exception:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise


class _RateGate:
    """跨线程共享的全局限速器（简单令牌间隔 + Lock）。"""

    def __init__(self, rate_per_sec: float):
        import threading
        self._interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next = now + self._interval


def download_tiles(
    bbox: tuple[float, float, float, float],
    zooms: list[int],
    label: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    url_template: str = DEFAULT_TILE_URL_TEMPLATE,
    rate_per_sec: float = 2.0,
    progress_cb=None,
    insecure: bool = False,
    workers: int = 1,
) -> dict:
    """主入口。返回 stats dict。

    workers>1 时并行下载（共享全局限速器：总速率仍受 rate_per_sec 约束，
    但多线程隐藏每请求的网络往返延迟 → 实测可比单线程快数倍）。
    """
    root = cache_dir / label
    root.mkdir(parents=True, exist_ok=True)
    ssl_context = make_ssl_context(insecure)

    tiles = list(iter_tiles(bbox, zooms))
    total = len(tiles)
    gate = _RateGate(rate_per_sec)

    import threading
    state_lock = threading.Lock()
    counters = {"downloaded": 0, "skipped": 0, "failed": 0, "done": 0}
    failed_tiles: list[TileCoord] = []

    def _bump(key: str, tile: Optional[TileCoord] = None) -> None:
        with state_lock:
            counters[key] += 1
            counters["done"] += 1
            if tile is not None:
                failed_tiles.append(tile)
            done = counters["done"]
        if progress_cb:
            progress_cb(done, total, counters["downloaded"],
                        counters["skipped"], counters["failed"])

    def _work(t: TileCoord) -> None:
        dest = t.to_path(root)
        if dest.exists() and dest.stat().st_size > 0:
            _bump("skipped")
            return
        gate.wait()
        try:
            download_tile(t, url_template, dest, ssl_context=ssl_context)
            _bump("downloaded")
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            _bump("failed", tile=t)
            print(f"  ! failed z={t.z} x={t.x} y={t.y}: {exc}", file=sys.stderr)

    if workers <= 1:
        for t in tiles:
            _work(t)
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_work, tiles))

    return {
        "total": total,
        "downloaded": counters["downloaded"],
        "skipped": counters["skipped"],
        "failed": counters["failed"],
        "failed_tiles": failed_tiles,
        "root": root,
    }


def _default_progress(i: int, total: int, dl: int, sk: int, fa: int) -> None:
    # 每 50 个 tile 或最后一个时打印
    if i == total or i % 50 == 0:
        bar = int(40 * i / total)
        sys.stdout.write(
            f"\r[{'#' * bar}{'.' * (40 - bar)}] {i}/{total}  "
            f"dl={dl} skip={sk} fail={fa}"
        )
        sys.stdout.flush()
        if i == total:
            sys.stdout.write("\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="tile_downloader",
        description="离线下载 OSM tile 到本地缓存，供 satellite_debug_tool 离线使用",
    )
    p.add_argument("--bbox", required=True,
                   help="lon_min,lat_min,lon_max,lat_max（南京示例：118.3,31.2,119.2,32.6）")
    p.add_argument("--zoom", required=True,
                   help="zoom 范围：'12-16' 或列表 '12,14,16'")
    p.add_argument("--label", required=True,
                   help="子目录名（多区域共存：nanjing / shanghai / beijing ...）")
    p.add_argument("--cache", default=str(DEFAULT_CACHE_DIR),
                   help=f"缓存根目录（默认 {DEFAULT_CACHE_DIR}）")
    p.add_argument("--source", default=DEFAULT_TILE_URL_TEMPLATE,
                   help="tile URL 模板（含 {z}/{x}/{y} 占位）")
    p.add_argument("--rate", type=float, default=2.0,
                   help="每秒请求数上限（OSM fair use 默认 2；自建/CDN 源可调高）")
    p.add_argument("--workers", type=int, default=1,
                   help="并行下载线程数（默认 1；建议 4-8，配合 --rate 提速）")
    p.add_argument("--insecure", action="store_true",
                   help="跳过 HTTPS 证书验证（macOS python.org 证书问题兜底；OSM 公开瓦片低风险）")
    args = p.parse_args(argv)

    bbox = parse_bbox(args.bbox)
    zooms = parse_zoom_spec(args.zoom)
    print(f"区域: {bbox}, zoom: {zooms}, label: {args.label}")
    print(f"  缓存目录: {Path(args.cache) / args.label}")
    print(f"  限速: {args.rate} req/s, 并行: {args.workers}"
          + ("（不验证证书）" if args.insecure else ""))

    stats = download_tiles(
        bbox=bbox,
        zooms=zooms,
        label=args.label,
        cache_dir=Path(args.cache),
        url_template=args.source,
        rate_per_sec=args.rate,
        progress_cb=_default_progress,
        insecure=args.insecure,
        workers=args.workers,
    )
    print(f"完成: 总 {stats['total']}, 新下 {stats['downloaded']}, "
          f"跳过 {stats['skipped']}, 失败 {stats['failed']}")
    print(f"  路径: {stats['root']}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
