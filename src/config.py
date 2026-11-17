"""共用設定：輕量，不依賴 odc/geopandas，讓 check 工作能快速執行。"""
import hashlib
import json
from pathlib import Path

BBOX = [140.39, 42.64, 141.01, 43.09]
CRS = "EPSG:32654"
RES = 20
STAC_URL = "https://planetarycomputer.microsoft.com/api/stac/v1"
SEASON = ("07-01", "09-30")
MAX_CLOUD = 40
MIN_AREA_M2 = 5000
KEEP_CLASSES = [10, 20]  # WorldCover: 10=樹木 20=灌木
THRESH = {"ndvi_before_min": 0.5, "ndvi_after_max": 0.35,
          "dndvi_max": -0.3, "dnbr_min": 0.2}
PIPELINE_VERSION = "v1-worldcover-mask"  # 改演算法時手動 +1，會觸發重跑

PARAMS = {"bbox": BBOX, "res": RES, "season": SEASON, "max_cloud": MAX_CLOUD,
          "min_area_m2": MIN_AREA_M2, "keep_classes": KEEP_CLASSES,
          "thresholds": THRESH}

def search_items(catalog, year):
    return list(catalog.search(
        collections=["sentinel-2-l2a"], bbox=BBOX,
        datetime=f"{year}-{SEASON[0]}/{year}-{SEASON[1]}",
        query={"eo:cloud_cover": {"lt": MAX_CLOUD}},
    ).items())


def labels_hash():
    p = Path("validation/labels.csv")
    return hashlib.sha256(p.read_bytes()).hexdigest()[:8] if p.exists() else None


def fingerprint(ids_by_year):
    payload = {"version": PIPELINE_VERSION, "params": PARAMS,
               "labels": labels_hash(),
               "items": {str(k): sorted(v) for k, v in ids_by_year.items()}}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
