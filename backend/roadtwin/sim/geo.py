"""Fast SUMO (x, y) -> (lon, lat) conversion.

sumolib can do this, but only after parsing an entire .net.xml -- several
seconds on a 2500-edge network, paid again in every worker process. We only
need the projection header, so we parse just that and convert in bulk.
"""

from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET


class GeoProjector:
    def __init__(self, net_path: str | Path):
        self.offset_x = 0.0
        self.offset_y = 0.0
        self._transformer = None

        # <location> is the first child of <net>, so we can stop parsing early.
        for _event, elem in ET.iterparse(str(net_path), events=("start",)):
            if elem.tag == "location":
                offset = elem.get("netOffset", "0,0").split(",")
                self.offset_x = float(offset[0])
                self.offset_y = float(offset[1])
                proj = elem.get("projParameter", "")
                if proj and proj not in ("!", "-"):
                    try:
                        from pyproj import CRS, Transformer

                        self._transformer = Transformer.from_crs(
                            CRS.from_proj4(proj), CRS.from_epsg(4326), always_xy=True
                        )
                    except Exception:  # noqa: BLE001 - fall back to identity
                        self._transformer = None
                break
            if elem.tag == "net":
                continue
            break

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        px, py = x - self.offset_x, y - self.offset_y
        if self._transformer is None:
            return px, py
        lon, lat = self._transformer.transform(px, py)
        return lon, lat

    def many(self, coords: list[tuple[float, float]]) -> list[tuple[float, float]]:
        """Bulk convert. pyproj vectorises internally, so this is much faster."""
        if not coords:
            return []
        xs = [c[0] - self.offset_x for c in coords]
        ys = [c[1] - self.offset_y for c in coords]
        if self._transformer is None:
            return list(zip(xs, ys))
        lons, lats = self._transformer.transform(xs, ys)
        return list(zip(lons, lats))
