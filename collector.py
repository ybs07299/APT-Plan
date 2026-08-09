#!/usr/bin/env python3
"""
국토교통부 아파트 매매 실거래가(상세) 수집기 v2.

v1 대비 변경점:
  - 기본 수집기간 240개월(20년)
  - data/series.json 추가: 월별 중위가격 시계열 + 두 단지 GAP
  - 거래 없는 달은 직전값 이월(forward-fill)하고 filled 플래그로 표시

환경변수:
  MOLIT_SERVICE_KEY : 공공데이터포털 '디코딩' 인증키
"""

import json
import os
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlencode, quote_plus
import urllib.request
import xml.etree.ElementTree as ET

BASE_URL = ("http://apis.data.go.kr/1613000/RTMSDataSvcAptTradeDev"
            "/getRTMSDataSvcAptTradeDev")

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"


# ---------------------------------------------------------------- utilities

def month_range(months_back: int):
    today = date.today()
    out, y, m = [], today.year, today.month
    for _ in range(months_back):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return sorted(out)


def to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(v):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def pick(d, *names):
    for n in names:
        if n in d and str(d[n]).strip():
            return str(d[n]).strip()
    return None


# ---------------------------------------------------------------- api call

def fetch_month(service_key, lawd_cd, deal_ymd, num_rows=1000, retries=3):
    params = {
        "serviceKey": service_key,
        "LAWD_CD": lawd_cd,
        "DEAL_YMD": deal_ymd,
        "pageNo": "1",
        "numOfRows": str(num_rows),
    }
    url = f"{BASE_URL}?{urlencode(params, quote_via=quote_plus)}"

    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                raw = resp.read()
            root = ET.fromstring(raw)
            code = root.findtext(".//resultCode")
            msg = root.findtext(".//resultMsg") or ""
            if code not in (None, "00", "000"):
                raise RuntimeError(f"API error {code}: {msg}")
            return [{c.tag: (c.text or "").strip() for c in item}
                    for item in root.iter("item")]
        except Exception as e:                       # noqa: BLE001
            last_err = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{lawd_cd}/{deal_ymd} 수집 실패: {last_err}")


# ---------------------------------------------------------------- normalize

def normalize(raw, lawd_cd):
    amount = to_int(pick(raw, "dealAmount", "거래금액"))
    area = to_float(pick(raw, "excluUseAr", "전용면적"))
    y = to_int(pick(raw, "dealYear", "년"))
    m = to_int(pick(raw, "dealMonth", "월"))
    d = to_int(pick(raw, "dealDay", "일"))
    if not all([amount, area, y, m, d]):
        return None

    cancel_flag = pick(raw, "cdealType", "해제여부")
    cancel_day = pick(raw, "cdealDay", "해제사유발생일")
    canceled = bool(cancel_flag and cancel_flag.upper() == "O") or bool(cancel_day)

    return {
        "date": f"{y}-{m:02d}-{d:02d}",
        "ym": f"{y}-{m:02d}",
        "apt": pick(raw, "aptNm", "아파트") or "",
        "dong": pick(raw, "umdNm", "법정동") or "",
        "area": area,
        "floor": to_int(pick(raw, "floor", "층")),
        "price_manwon": amount,
        "canceled": canceled,
        "lawd_cd": lawd_cd,
    }


def matches(rec, target):
    if rec["canceled"]:
        return False
    apt = rec["apt"].replace(" ", "")
    if not any(k.replace(" ", "") in apt for k in target["apt_keywords"]):
        return False
    for k in target.get("exclude_keywords", []):
        if k.replace(" ", "") in apt:
            return False
    if not (target["area_min"] <= rec["area"] <= target["area_max"]):
        return False

    # 저층 제외: 층 정보가 없는 건도 제외 (정상가 판단 불가)
    floor_min = target.get("floor_min")
    if floor_min is not None:
        if rec["floor"] is None or rec["floor"] < floor_min:
            return False
    return True


def deal_key(r):
    return (r["date"], r["apt"], r["area"], r["floor"], r["price_manwon"])


# ---------------------------------------------------------------- collect

def collect_target(service_key, target, months, refresh_months):
    out_path = DATA_DIR / f"deals_{target['id']}.json"
    existing, cached = {}, set()

    if out_path.exists():
        prev = json.loads(out_path.read_text(encoding="utf-8"))
        for r in prev.get("deals", []):
            existing[deal_key(r)] = r
        cached = set(prev.get("collected_months", []))

    recent = set(months[-refresh_months:])
    todo = [m for m in months if m in recent or m not in cached]
    print(f"[{target['id']}] 조회 {len(todo)}개월 (캐시 {len(cached)})")

    samples = []
    for i, ym in enumerate(todo, 1):
        for raw in fetch_month(service_key, target["lawd_cd"], ym):
            rec = normalize(raw, target["lawd_cd"])
            if rec is None:
                continue
            if matches(rec, target):
                existing[deal_key(rec)] = rec
            elif len(samples) < 8 and not rec["canceled"]:
                samples.append(f"{rec['apt']}({rec['area']})")
        if i % 24 == 0:
            print(f"   ... {i}/{len(todo)}")
        time.sleep(0.12)

    deals = sorted(existing.values(), key=lambda r: (r["date"], r["floor"] or 0))
    payload = {
        "id": target["id"],
        "label": target["label"],
        "lawd_cd": target["lawd_cd"],
        "area_range": [target["area_min"], target["area_max"]],
        "updated_at": date.today().isoformat(),
        "collected_months": sorted(cached | set(todo)),
        "count": len(deals),
        "latest": deals[-1] if deals else None,
        "deals": deals,
    }
    DATA_DIR.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                        encoding="utf-8")

    print(f"[{target['id']}] 매칭 {len(deals)}건")
    if not deals and samples:
        print(f"   !! 0건. 해당 지역 실제 단지명 예시: {samples}")
    return payload


# ---------------------------------------------------------------- series

def monthly_median(deals):
    buckets = {}
    for r in deals:
        buckets.setdefault(r["ym"], []).append(r["price_manwon"])
    return {k: int(statistics.median(v)) for k, v in buckets.items()}


def build_series(payloads, months):
    """월별 중위가 시계열 + GAP. 거래 없는 달은 직전값 이월."""
    labels = [f"{m[:4]}-{m[4:]}" for m in months]
    med = {p["id"]: monthly_median(p["deals"]) for p in payloads}

    out = {"months": labels, "series": {}}
    for pid, table in med.items():
        vals, filled, last = [], [], None
        for lb in labels:
            if lb in table:
                last = table[lb]
                vals.append(last)
                filled.append(False)
            else:
                vals.append(last)          # None = 첫 거래 이전
                filled.append(last is not None)
        out["series"][pid] = {"price": vals, "filled": filled}

    cur = out["series"].get("current", {}).get("price", [None] * len(labels))
    tgt = out["series"].get("target", {}).get("price", [None] * len(labels))
    gap, ratio = [], []
    for a, b in zip(cur, tgt):
        if a and b:
            gap.append(b - a)
            ratio.append(round(b / a, 4))
        else:
            gap.append(None)
            ratio.append(None)
    out["gap"], out["ratio"] = gap, ratio

    valid = [(i, g) for i, g in enumerate(gap) if g is not None]
    if valid:
        gs = sorted(g for _, g in valid)
        now = valid[-1][1]
        out["gap_stats"] = {
            "now": now,
            "min": gs[0],
            "max": gs[-1],
            "median": gs[len(gs) // 2],
            "percentile": round(sum(1 for g in gs if g <= now) / len(gs) * 100, 1),
            "first_month": labels[valid[0][0]],
            "n": len(gs),
        }
    out["updated_at"] = date.today().isoformat()
    return out


# ---------------------------------------------------------------- main

def main():
    key = os.environ.get("MOLIT_SERVICE_KEY", "").strip()
    if not key:
        sys.exit("MOLIT_SERVICE_KEY 환경변수가 없습니다. (포털의 '디코딩' 인증키)")

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    months = month_range(cfg.get("months_back", 240))
    refresh = cfg.get("recent_refresh_months", 3)

    payloads = [collect_target(key, t, months, refresh) for t in cfg["targets"]]

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "series.json").write_text(
        json.dumps(build_series(payloads, months), ensure_ascii=False),
        encoding="utf-8")
    (DATA_DIR / "summary.json").write_text(
        json.dumps({
            "updated_at": date.today().isoformat(),
            "targets": [{"id": p["id"], "label": p["label"],
                         "count": p["count"], "latest": p["latest"]}
                        for p in payloads]
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    print("완료")


if __name__ == "__main__":
    main()
