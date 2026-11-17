"""Step 2: 偵測 + manifest(可重現) + metrics(可監控)"""
import argparse
import hashlib
import json
import math
import os
import platform
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import planetary_computer as pc
import xarray as xr
from odc.stac import configure_rio, load
from pystac_client import Client
from rasterio import features
from rasterio.transform import from_origin
from scipy import ndimage
from shapely.geometry import shape

from config import (BBOX, CRS, KEEP_CLASSES, MIN_AREA_M2, PARAMS,
                    PIPELINE_VERSION, RES, STAC_URL, THRESH, fingerprint,
                    search_items)

OUT = Path("web/data")
LABELS = Path("validation/labels.csv")


def composite(catalog, year):
    items = search_items(catalog, year)
    print(f"[{year}] 找到 {len(items)} 景")
    if not items:
        raise SystemExit(f"{year} 沒有符合條件的影像")
    ds = load(items, bands=["B04", "B08", "B12", "SCL"], bbox=BBOX, crs=CRS,
              resolution=RES, groupby="solar_day", chunks={"x": 1024, "y": 1024},
              resampling={"SCL": "nearest", "*": "bilinear"})
    ok = ds["SCL"].isin([4, 5])
    offset = xr.where(ds.time >= np.datetime64("2022-01-25"), 1000, 0)

    def refl(b):
        a = ds[b].astype("float32")
        a = a.where(a > 0)
        return ((a - offset) / 10000).clip(0, 1).where(ok)

    red, nir, swir = refl("B04"), refl("B08"), refl("B12")
    out = xr.Dataset({"ndvi": (nir - red) / (nir + red),
                      "nbr": (nir - swir) / (nir + swir)}
                     ).chunk({"time": -1}).median("time", skipna=True).compute()
    clouds = [i.properties.get("eo:cloud_cover") for i in items]
    clouds = [c for c in clouds if c is not None]
    stats = {
        "scenes": len(items),
        "dates": int(ds.sizes["time"]),
        "item_ids": [i.id for i in items],
        "cloud_mean": round(float(np.mean(clouds)), 1) if clouds else None,
        "valid_ratio": round(float(np.isfinite(out["ndvi"].values).mean()), 3),
    }
    return out, stats


def landcover(catalog):
    items = list(catalog.search(collections=["esa-worldcover"], bbox=BBOX,
                                datetime="2021-01-01/2021-12-31").items())
    print(f"WorldCover 找到 {len(items)} 個圖磚")
    ds = load(items, bands=["map"], bbox=BBOX, crs=CRS, resolution=RES,
              resampling="mode")
    return ds["map"].max("time").compute().values, [i.id for i in items]


def detect(before, after, lc):
    ndvi_b, ndvi_a = before["ndvi"].values, after["ndvi"].values
    dndvi = ndvi_a - ndvi_b
    dnbr = before["nbr"].values - after["nbr"].values
    mask = ((ndvi_b > THRESH["ndvi_before_min"]) & (ndvi_a < THRESH["ndvi_after_max"])
            & (dndvi < THRESH["dndvi_max"]) & (dnbr > THRESH["dnbr_min"]))
    mask &= np.isin(lc, KEEP_CLASSES)

    x, y = after.x.values, after.y.values
    assert y[0] > y[-1]
    transform = from_origin(x[0] - RES / 2, y[0] + RES / 2, RES, RES)

    lab, n = ndimage.label(mask)
    ids = np.arange(1, n + 1)
    sizes = ndimage.sum(mask, lab, ids)
    keep = ids[sizes >= math.ceil(MIN_AREA_M2 / (RES * RES))]
    lab = np.where(np.isin(lab, keep), lab, 0).astype("int32")
    print(f"候選斑塊 {n}，面積 >= 0.5 ha 保留 {len(keep)}")
    cols = ["id", "area_ha", "ndvi_before", "ndvi_after", "lon", "lat", "geometry"]
    if len(keep) == 0:
        return gpd.GeoDataFrame(columns=cols, geometry="geometry", crs=4326)

    mb = dict(zip(keep, ndimage.mean(ndvi_b, lab, keep)))
    ma = dict(zip(keep, ndimage.mean(ndvi_a, lab, keep)))
    sz = dict(zip(ids, sizes))
    rows = []
    for geom, val in features.shapes(lab, mask=lab > 0, transform=transform):
        v = int(val)
        rows.append({"id": v, "area_ha": round(sz[v] * RES * RES / 10000, 2),
                     "ndvi_before": round(float(mb[v]), 2),
                     "ndvi_after": round(float(ma[v]), 2),
                     "geometry": shape(geom).simplify(RES / 2)})
    gdf = gpd.GeoDataFrame(rows, crs=CRS).to_crs(4326)
    rp = gdf.geometry.representative_point()
    gdf["lon"], gdf["lat"] = rp.x.round(5), rp.y.round(5)
    return gdf


def precision(gdf):
    """用 validation/labels.csv（lon,lat,label；1=真實變化 0=誤報）計算 precision"""
    if not LABELS.exists() or len(gdf) == 0:
        return None
    df = pd.read_csv(LABELS)
    if len(df) == 0:
        return None
    pts = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat),
                           crs=4326).to_crs(CRS)
    polys = gdf.to_crs(CRS)[["id", "geometry"]]
    j = gpd.sjoin_nearest(pts, polys, how="left", max_distance=30)
    j = j[~j.index.duplicated()]
    m = j[j["id"].notna()]
    return {"labeled": int(len(df)), "matched": int(len(m)),
            "precision": round(float(m["label"].mean()), 3) if len(m) else None}


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def main():
    t0 = time.time()
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", type=int, default=2022)
    ap.add_argument("--current", type=int, default=2026)
    a = ap.parse_args()

    configure_rio(cloud_defaults=True)
    catalog = Client.open(STAC_URL, modifier=pc.sign_inplace)
    before, sb = composite(catalog, a.base)
    after, sa = composite(catalog, a.current)
    lc, lc_ids = landcover(catalog)
    gdf = detect(before, after, lc)
    prec = precision(gdf)

    OUT.mkdir(parents=True, exist_ok=True)
    gdf.to_file(OUT / "changes.geojson", driver="GeoJSON")
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fp = fingerprint({a.base: sb["item_ids"], a.current: sa["item_ids"]})

    manifest = {
        "run_id": os.getenv("GITHUB_RUN_ID", "local"), "run_at": now,
        "git_sha": os.getenv("GITHUB_SHA", "local"),
        "pipeline_version": PIPELINE_VERSION, "fingerprint": fp,
        "years": {"base": a.base, "current": a.current}, "params": PARAMS,
        "inputs": {"stac": STAC_URL,
                   "sentinel2_items": {str(a.base): sb["item_ids"],
                                       str(a.current): sa["item_ids"]},
                   "worldcover_items": lc_ids},
        "outputs": {"changes.geojson": sha256(OUT / "changes.geojson")},
        "env": {"python": platform.python_version(),
                "packages": {p: version(p) for p in
                             ["numpy", "xarray", "odc-stac", "geopandas", "rasterio"]}},
    }
    metrics = {
        "run_at": now, "pipeline_version": PIPELINE_VERSION, "fingerprint": fp,
        "years": {"base": a.base, "current": a.current},
        "scenes": {"base": sb["scenes"], "current": sa["scenes"]},
        "dates": {"base": sb["dates"], "current": sa["dates"]},
        "cloud_mean": {"base": sb["cloud_mean"], "current": sa["cloud_mean"]},
        "valid_ratio": {"base": sb["valid_ratio"], "current": sa["valid_ratio"]},
        "patches": int(len(gdf)),
        "total_area_ha": round(float(gdf["area_ha"].sum()), 1) if len(gdf) else 0,
        "validation": prec, "duration_s": round(time.time() - t0),
        "alerts": [],
    }
    summary = {"base_year": a.base, "current_year": a.current, "run_at": now,
               "patches": metrics["patches"], "total_area_ha": metrics["total_area_ha"]}
    for name, obj in [("manifest", manifest), ("metrics", metrics), ("summary", summary)]:
        (OUT / f"{name}.json").write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
