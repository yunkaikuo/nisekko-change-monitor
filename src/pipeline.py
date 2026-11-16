"""Step 1: 二世谷植生變化偵測 (Sentinel-2 -> NDVI/NBR 差異 -> GeoJSON)"""
import argparse
import json
import math
from pathlib import Path

import geopandas as gpd
import numpy as np
import planetary_computer as pc
import xarray as xr
from odc.stac import configure_rio, load
from pystac_client import Client
from rasterio import features
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import shape

# ---- 設定 ---------------------------------------------------------------
BBOX = [140.39, 42.64, 141.01, 43.09]   # 比羅夫周邊約 50x50 km (lon/lat)
CRS = "EPSG:32654"                       # UTM 54N，單位為公尺
RES = 20                                 # 解析度(公尺)；Step 1 先用 20 m 求快
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
SEASON = ("07-01", "09-30")              # 無雪季
MAX_CLOUD = 40
MIN_AREA_M2 = 5000                       # 0.5 ha
KEEP_CLASSES = [10, 20]   # WorldCover: 10=樹木 20=灌木（排除 40 農田、30 草地）
OUT = Path("web/data")


def composite(catalog, year):
    """某年無雪季中位數合成 -> NDVI, NBR"""
    items = list(catalog.search(
        collections=["sentinel-2-l2a"], bbox=BBOX,
        datetime=f"{year}-{SEASON[0]}/{year}-{SEASON[1]}",
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}},
    ).items())
    print(f"[{year}] 找到 {len(items)} 景")
    if not items:
        raise SystemExit(f"{year} 沒有符合條件的影像")

    ds = load(
        items, bands=["B04", "B08", "B12", "SCL"], bbox=BBOX,
        crs=CRS, resolution=RES, groupby="solar_day",
        chunks={"x": 1024, "y": 1024},
        resampling={"SCL": "nearest", "*": "bilinear"},
    )
    ok = ds["SCL"].isin([4, 5])  # 4=植生 5=裸地；其餘(雲/雪/水/陰影)排除
    # 2022-01-25 之後的處理基準 04.00 起，DN 多了 +1000 偏移
    offset = xr.where(ds.time >= np.datetime64("2022-01-25"), 1000, 0)

    def refl(b):
        a = ds[b].astype("float32")
        a = a.where(a > 0)
        return ((a - offset) / 10000).clip(0, 1).where(ok)

    red, nir, swir = refl("B04"), refl("B08"), refl("B12")
    out = xr.Dataset({
        "ndvi": (nir - red) / (nir + red),
        "nbr": (nir - swir) / (nir + swir),
    }).chunk({"time": -1}).median("time", skipna=True).compute()
    return out, len(items)

def landcover(catalog):
    """ESA WorldCover 2021，對齊到同一個網格"""
    items = list(catalog.search(
        collections=["esa-worldcover"], bbox=BBOX,
        datetime="2021-01-01/2021-12-31").items())
    print(f"WorldCover 找到 {len(items)} 個圖磚")
    ds = load(items, bands=["map"], bbox=BBOX, crs=CRS,
              resolution=RES, resampling="mode")
    return ds["map"].max("time").compute().values

def detect(before, after):
    """植生損失：前期是植生、後期明顯下降"""
    ndvi_b, ndvi_a = before["ndvi"].values, after["ndvi"].values
    dndvi = ndvi_a - ndvi_b
    dnbr = before["nbr"].values - after["nbr"].values
    mask = (ndvi_b > 0.5) & (ndvi_a < 0.35) & (dndvi < -0.3) & (dnbr > 0.2)
    mask &= np.isin(lc, KEEP_CLASSES)

    x, y = after.x.values, after.y.values
    assert y[0] > y[-1], "y 軸應為由北到南"
    transform = from_origin(x[0] - RES / 2, y[0] + RES / 2, RES, RES)

    lab, n = ndimage.label(mask)
    ids = np.arange(1, n + 1)
    sizes = ndimage.sum(mask, lab, ids)
    min_px = math.ceil(MIN_AREA_M2 / (RES * RES))
    keep = ids[sizes >= min_px]
    lab = np.where(np.isin(lab, keep), lab, 0).astype("int32")
    print(f"候選斑塊 {n}，面積 >= 0.5 ha 保留 {len(keep)}")
    if len(keep) == 0:
        return gpd.GeoDataFrame(
            columns=["id", "area_ha", "ndvi_before", "ndvi_after", "geometry"],
            geometry="geometry", crs=CRS).to_crs(4326)

    mb = dict(zip(keep, ndimage.mean(ndvi_b, lab, keep)))
    ma = dict(zip(keep, ndimage.mean(ndvi_a, lab, keep)))
    sz = dict(zip(ids, sizes))
    rows = []
    for geom, val in features.shapes(lab, mask=lab > 0, transform=transform):
        v = int(val)
        rows.append({
            "id": v,
            "area_ha": round(sz[v] * RES * RES / 10000, 2),
            "ndvi_before": round(float(mb[v]), 2),
            "ndvi_after": round(float(ma[v]), 2),
            "geometry": shape(geom).simplify(RES / 2),
        })
    return gpd.GeoDataFrame(rows, crs=CRS).to_crs(4326)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=int, default=2022)
    ap.add_argument("--current", type=int, default=2026)
    a = ap.parse_args()

    configure_rio(cloud_defaults=True)
    catalog = Client.open(STAC_URL, modifier=pc.sign_inplace)
    before, nb = composite(catalog, a.base)
    after, na = composite(catalog, a.current)
    lc = landcover(catalog)   
    gdf = detect(before, after, lc)

    OUT.mkdir(parents=True, exist_ok=True)
    gdf.to_file(OUT / "changes.geojson", driver="GeoJSON")
    summary = {
        "base_year": a.base, "current_year": a.current,
        "scenes_base": nb, "scenes_current": na,
        "patches": int(len(gdf)),
        "total_area_ha": round(float(gdf["area_ha"].sum()), 1) if len(gdf) else 0,
        "bbox": BBOX,
    }
    (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(summary)


if __name__ == "__main__":
    main()
