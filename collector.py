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

SEOUL = {
    "11110": "종로구", "11140": "중구",   "11170": "용산구", "11200": "성동구",
    "11215": "광진구", "11230": "동대문구", "11260": "중랑구", "11290": "성북구",
    "11305": "강북구", "11320": "도봉구", "11350": "노원구", "11380": "은평구",
    "11410": "서대문구", "11440": "마포구", "11470": "양천구", "11500": "강서구",
    "11530": "구로구", "11545": "금천구", "11560": "영등포구", "11590": "동작구",
    "11620": "관악구", "11650": "서초구", "11680": "강남구", "11710": "송파구",
    "11740": "강동구",
}

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

class AuthError(Exception):
    """인증키 자체가 잘못됐거나 아직 승인 반영 전."""


FAILED = []   # (lawd_cd, ym, 사유)


def fetch_month(service_key, lawd_cd, deal_ymd, num_rows=1000, retries=5):
    """성공하면 item 리스트, 실패하면 None(기록 후 계속 진행)."""
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
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Safari/537.36"),
                    "Accept": "*/*",
                },
            )
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read()

            text = raw.decode("utf-8", "ignore")
            for token in ("SERVICE_KEY_IS_NOT_REGISTERED",
                          "SERVICE ACCESS DENIED",
                          "LIMITED_NUMBER_OF_SERVICE_REQUESTS"):
                if token in text:
                    raise AuthError(token)

            root = ET.fromstring(raw)
            code = (root.findtext(".//resultCode") or "").strip()
            msg = (root.findtext(".//resultMsg") or "").strip()
            if code and code not in ("00", "000"):
                if code in ("30", "31", "32", "20", "22"):
                    raise AuthError(f"{code} {msg}")
                raise RuntimeError(f"API {code}: {msg}")

            return [{c.tag: (c.text or "").strip() for c in item}
                    for item in root.iter("item")]

        except AuthError:
            raise
        except Exception as e:                       # noqa: BLE001
            last_err = e
            time.sleep(3.0 * (attempt + 1))

    FAILED.append((lawd_cd, deal_ymd, str(last_err)[:80]))
    return None


def preflight(service_key):
    """본격 수집 전에 키가 실제로 먹는지 1회 확인."""
    print("인증키 확인 중...")
    try:
        rows = fetch_month(service_key, "11500", "202601", num_rows=5, retries=3)
    except AuthError as e:
        print("\n" + "=" * 60)
        print("인증 실패:", e)
        print("확인할 것")
        print(" 1. 포털의 '일반 인증키(Decoding)'를 넣었는지  ← 가장 흔한 원인")
        print("    Encoding 키를 넣으면 이중 인코딩되어 반드시 실패합니다.")
        print(" 2. 활용신청이 '승인' 상태인지 (신청 직후면 10~30분 기다렸다 재시도)")
        print(" 3. 신청한 데이터셋이 '아파트 매매 실거래가 상세 자료'가 맞는지")
        print(" 4. 일일 트래픽(개발계정 10,000건)을 초과하지 않았는지")
        print("=" * 60 + "\n")
        sys.exit(1)
    if rows is None:
        print("경고: 시험 호출이 응답하지 않았습니다. 네트워크 일시 문제일 수 있어 계속 진행합니다.")
    else:
        print(f"인증키 정상 (시험 조회 {len(rows)}건)\n")


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
    fail_streak = 0

    samples = []
    for i, ym in enumerate(todo, 1):
        rows = fetch_month(service_key, target["lawd_cd"], ym)
        if rows is None:
            fail_streak += 1
            if fail_streak >= 20:
                print(f"   !! {fail_streak}개월 연속 실패 — 네트워크가 불안정합니다.")
                print("      지금까지 받은 만큼만 저장하고 이 단지는 중단합니다.")
                print("      다시 실행하면 캐시된 달은 건너뛰고 이어서 받습니다.")
                break
            continue
        fail_streak = 0
        for raw in rows:
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


# ---------------------------------------------------------------- volume

def collect_volume(service_key, cfg):
    """자치구별 월별 아파트 매매 거래건수. 해제(취소) 거래는 제외."""
    vcfg = cfg.get("volume") or {}
    if not vcfg.get("enabled", True):
        return None

    months = month_range(vcfg.get("months_back", 120))
    labels = [f"{m[:4]}-{m[4:]}" for m in months]
    refresh = set(months[-cfg.get("recent_refresh_months", 3):])

    focus = vcfg.get("focus", ["11500", "11440"])          # 강서구, 마포구
    codes = list(SEOUL.keys()) if vcfg.get("all_seoul", True) else list(focus)

    cache_path = DATA_DIR / "volume.json"
    cache = {}
    if cache_path.exists():
        prev = json.loads(cache_path.read_text(encoding="utf-8"))
        cache = prev.get("_raw", {})

    total_calls = 0
    for ci, code in enumerate(codes, 1):
        cache.setdefault(code, {})
        todo = [m for m in months if m in refresh or m not in cache[code]]
        if not todo:
            continue
        print(f"[volume] {SEOUL.get(code, code)} {len(todo)}개월")
        for ym in todo:
            rows = fetch_month(service_key, code, ym)
            if rows is None:
                continue
            n = 0
            for raw in rows:
                rec = normalize(raw, code)
                if rec and not rec["canceled"]:
                    n += 1
            cache[code][ym] = n
            total_calls += 1
            time.sleep(0.12)
        if ci % 5 == 0:
            print(f"   ... {ci}/{len(codes)}개 자치구, 누적 {total_calls}건 호출")

    regions, seoul = {}, [0] * len(months)
    for code in codes:
        counts = [cache.get(code, {}).get(m) for m in months]
        regions[code] = {"name": SEOUL.get(code, code), "counts": counts,
                         "focus": code in focus}
        for i, c in enumerate(counts):
            if c:
                seoul[i] += c

    out = {
        "months": labels,
        "regions": regions,
        "seoul": {"name": "서울 전체", "counts": seoul},
        "focus": focus,
        "all_seoul": vcfg.get("all_seoul", True),
        "updated_at": date.today().isoformat(),
        "_raw": cache,
    }
    DATA_DIR.mkdir(exist_ok=True)
    cache_path.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"[volume] 완료 — {len(codes)}개 자치구 / {len(months)}개월")
    return out


# ---------------------------------------------------------------- main

VERSION = "v3-local-2026.08"


def main():
    print("=" * 52)
    print(f" collector {VERSION}")
    print("=" * 52)
    key = os.environ.get("MOLIT_SERVICE_KEY", "").strip()
    if not key:
        sys.exit("MOLIT_SERVICE_KEY 환경변수가 없습니다. (포털의 '디코딩' 인증키)")

    preflight(key)

    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    months = month_range(cfg.get("months_back", 240))
    refresh = cfg.get("recent_refresh_months", 3)

    payloads = [collect_target(key, t, months, refresh) for t in cfg["targets"]]

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "series.json").write_text(
        json.dumps(build_series(payloads, months), ensure_ascii=False),
        encoding="utf-8")
    collect_volume(key, cfg)

    (DATA_DIR / "summary.json").write_text(
        json.dumps({
            "updated_at": date.today().isoformat(),
            "targets": [{"id": p["id"], "label": p["label"],
                         "count": p["count"], "latest": p["latest"]}
                        for p in payloads]
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    DATA_DIR.mkdir(exist_ok=True)
    if FAILED:
        print(f"\n일부 월 수집 실패 {len(FAILED)}건 (나머지는 정상 저장됨)")
        for f in FAILED[:5]:
            print("  ", f)
    print("\n완료")


if __name__ == "__main__":
    main()
