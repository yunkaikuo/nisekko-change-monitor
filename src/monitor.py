"""監控：把本次 metrics 與門檻、上一次結果比較；有異常寫 alerts.md（由 workflow 開 Issue）。"""
import json
from pathlib import Path

M = Path("web/data/metrics.json")


def load(p, default):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return default


cur = load(M, None)
if cur is None:
    raise SystemExit("找不到 metrics.json")
prev = load("prev/metrics.json", None)
hist = load("prev/history.json", [])
if not isinstance(hist, list):
    hist = []

alerts = []
vr = min(v for v in cur["valid_ratio"].values() if v is not None)
if vr < 0.7:
    alerts.append(f"有效像素比例偏低（{vr}），雲／雪遮罩後可用資料不足")
if min(cur["scenes"].values()) < 5:
    alerts.append(f"可用影像過少（{cur['scenes']}），中位數合成不穩定")
if prev and prev.get("years") == cur["years"] and prev.get("patches"):
    ratio = cur["patches"] / prev["patches"]
    if ratio > 2 or ratio < 0.5:
        alerts.append(f"斑塊數與上次相比變動過大：{prev['patches']} -> {cur['patches']}")
v = cur.get("validation")
if v and v.get("precision") is not None and v["labeled"] >= 10 and v["precision"] < 0.7:
    alerts.append(f"抽樣 precision 低於 0.7（{v['precision']}，n={v['labeled']}）")

cur["alerts"] = alerts
M.write_text(json.dumps(cur, ensure_ascii=False, indent=2))

hist.append({k: cur.get(k) for k in
             ["run_at", "years", "scenes", "patches", "total_area_ha", "validation",
              "pipeline_version"]} | {"alerts": len(alerts)})
Path("web/data/history.json").write_text(
    json.dumps(hist[-60:], ensure_ascii=False, indent=2))

if alerts:
    Path("alerts.md").write_text(
        "## 管線監控警示\n\n" + "\n".join(f"- {x}" for x in alerts) +
        f"\n\n- 執行時間：{cur['run_at']}\n- 指紋：`{cur['fingerprint']}`\n")
    print("ALERTS:", alerts)
else:
    print("無警示")
