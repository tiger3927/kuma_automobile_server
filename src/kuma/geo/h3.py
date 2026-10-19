"""地图缓存用的粗网格归一化。

依赖清单里没有 h3 库（§6 只允许 Shapely），v1 用等经纬网格近似做缓存 key 归一化：
`{lat_bin}_{lon_bin}`，边长约 0.01°（≈1.1km）。将来引入 h3 只换这一个文件。
"""

from __future__ import annotations

GRID = 0.01


def cell(lon: float, lat: float, *, grid: float = GRID) -> str:
    return f"{int(lat / grid)}:{int(lon / grid)}"


def od_key(a: tuple[float, float], b: tuple[float, float], *, mode: str = "driving",
           bucket: str = "flat") -> str:
    """OD 缓存 key：起终点各归一化到网格 + 交通方式 + 时段桶。"""
    return f"{cell(a[0], a[1])}|{cell(b[0], b[1])}|{mode}|{bucket}"
