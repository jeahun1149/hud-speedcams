#!/usr/bin/env python3
"""
HUD 과속 단속카메라 DB 생성기
  공공데이터포털 '전국무인교통단속카메라표준데이터' → dist/cams.bin + dist/version.json
  - GitHub Actions(.github/workflows/update-cams.yml)가 매일 실행, 내용이 바뀐 경우에만 Releases(cams-latest)에 올림
  - PC에서 확인할 때는 build_cams_pc.py 로 실행 (API 키를 거기에 적음, 저장소에는 안 올라감)
  - 환경변수 DATA_GO_KR_KEY = 공공데이터포털 '일반 인증키(Decoding)'  (requests 가 알아서 인코딩함)
  - 옵션: python build_cams.py --dump  → 첫 페이지 원본 응답을 dist/sample.json 으로 저장 (필드 이름 확인용)
  - 단속 방향: OpenStreetMap 도로 지도(Geofabrik, 약 250 MB, dist/ 에 받아 둠)로 추정 (cam_dirs.py, pip install osmium)

cams.bin (리틀 엔디언)
  헤더 12바이트: "CAM1" + uint32 레코드 수 + uint32 레코드 크기(12)
  레코드 12바이트: int32 위도x1e6, int32 경도x1e6, uint8 제한속도(km/h), uint8 단속구분, uint8 도로노선방향,
                   uint8 단속 방향 (0 모름, 1~180 = 방향/2 + 1, 북 0° 시계방향 — 예전 파일은 0 = 모름)
  위도 순 정렬, 중복 제거
version.json: version, count, size, sha256
"""
import datetime
import hashlib
import json
import os
import struct
import sys
import time

import requests

API_URL = "http://api.data.go.kr/openapi/tn_pubr_public_unmanned_traffic_camera_api"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
ROWS = 1000
MIN_RECORDS = 10000          # 이보다 적으면 API 이상으로 보고 실패 처리 (기존 DB 유지)
OSM_URL = "https://download.geofabrik.de/asia/south-korea-latest.osm.pbf"   # 단속 방향 추정용 도로 지도

# API 필드 이름은 아직 실제 응답으로 확인 전 → 후보를 여러 개 두고 처음 맞는 것을 씀
#  (첫 실행 때 '사용한 필드' 출력과 --dump 결과로 확인 후 정리)
FIELD_LAT = ("latitude", "lat", "la")
FIELD_LON = ("longitude", "lon", "lo")
FIELD_SPEED = ("lmttVe", "lmttSpd", "limitSpeed")
FIELD_KIND = ("regltSe", "regltSeCd")
FIELD_DIR = ("roadRouteDrc", "roadRouteDrcCd")


def fetch_page(key, page, tries=4):
    # 공공데이터포털은 가끔 응답이 아주 느리거나 끊김 → 기다렸다가 다시 시도 (해외 서버인 GitHub Actions 에서 특히)
    for n in range(tries):
        try:
            r = requests.get(API_URL, params={"serviceKey": key, "pageNo": page,
                                              "numOfRows": ROWS, "type": "json"}, timeout=(15, 90))
            if r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}")
            break
        except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
            if n == tries - 1:
                raise SystemExit(f"{page}쪽 받기 실패 ({tries}번 시도): {e}")
            wait = 10 * (n + 1)
            print(f"  {page}쪽 실패 ({e.__class__.__name__}) → {wait}초 뒤 다시 시도")
            time.sleep(wait)
    r.raise_for_status()
    try:
        data = r.json()
    except ValueError:
        # 키 오류 등은 type=json 이어도 XML 로 옴
        raise SystemExit(f"API 응답이 JSON이 아님 (키/신청 상태 확인):\n{r.text[:500]}")
    # 응답이 {"response": {"header", "body"}} 로 감싸져 오기도, {"header", "body"} 로 바로 오기도 함
    root = data.get("response", data) if isinstance(data, dict) else None
    if not isinstance(root, dict) or "body" not in root:
        raise SystemExit(f"예상과 다른 API 응답 (키 등록 대기/오류일 수 있음):\n{r.text[:800]}")
    header = root.get("header", {})
    if str(header.get("resultCode", "00")) not in ("00", "0"):
        raise SystemExit(f"API 오류: {header}")
    return root


def fetch_all(key, dump=False):
    items, page = [], 1
    while True:
        data = fetch_page(key, page)
        if dump and page == 1:
            os.makedirs(OUT_DIR, exist_ok=True)
            with open(os.path.join(OUT_DIR, "sample.json"), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        body = data["body"]
        batch = body.get("items") or []
        if isinstance(batch, dict):
            batch = batch.get("item", [])
        if isinstance(batch, dict):          # 한 건만 오면 dict
            batch = [batch]
        items += batch
        total = int(body.get("totalCount") or 0)
        print(f"  {page}쪽: 누적 {len(items)} / {total}")
        if not batch or len(items) >= total:
            return items
        page += 1


def pick(item, names):
    for n in names:
        if n in item and item[n] not in (None, ""):
            return n
    return None


def to_int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def parse(items):
    if items:
        used = {k: pick(items[0], v) for k, v in
                (("위도", FIELD_LAT), ("경도", FIELD_LON), ("제한속도", FIELD_SPEED),
                 ("단속구분", FIELD_KIND), ("방향", FIELD_DIR))}
        print("사용한 필드:", used)
        print("첫 항목 필드 목록:", sorted(items[0].keys()))
    recs, skipped = set(), 0
    for it in items:
        klat, klon = pick(it, FIELD_LAT), pick(it, FIELD_LON)
        try:
            lat, lon = float(it[klat]), float(it[klon])
        except (KeyError, TypeError, ValueError):
            skipped += 1
            continue
        if not (33 < lat < 39 and 124 < lon < 132):     # 대한민국 범위 밖 = 잘못된 좌표
            skipped += 1
            continue
        ks, kk, kd = pick(it, FIELD_SPEED), pick(it, FIELD_KIND), pick(it, FIELD_DIR)
        recs.add((round(lat * 1e6), round(lon * 1e6),
                  max(0, min(to_int(it.get(ks)), 255)),
                  to_int(it.get(kk)) & 0xFF,
                  to_int(it.get(kd)) & 0xFF))
    print(f"좌표 없음/범위 밖 제외: {skipped}건")
    return sorted(recs)


def osm_pbf():
    """도로 지도 파일 (없거나 하루 지났으면 Geofabrik 에서 새로 받음, 약 250 MB) → 경로"""
    path = os.environ.get("CAMS_OSM_PBF") or os.path.join(OUT_DIR, "south-korea-latest.osm.pbf")
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600:
        print(f"도로 지도: {path} (받아 둔 것 사용)")
        return path
    print(f"도로 지도 받는 중: {OSM_URL}")
    tmp = path + ".part"
    with requests.get(OSM_URL, stream=True, timeout=(15, 120)) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    os.replace(tmp, path)
    print(f"도로 지도 받음: {os.path.getsize(path) // (1 << 20)} MB")
    return path


def add_directions(recs):
    """레코드에 단속 방향 바이트 추가 (cam_dirs.py: OpenStreetMap 도로로 추정, 0 = 모름)"""
    try:
        import cam_dirs
    except ImportError:
        raise SystemExit("osmium 이 없음 → pip install osmium  (단속 방향 추정에 필요)")
    t0 = time.time()
    dirs, stats = cam_dirs.directions(osm_pbf(), [(r[0] / 1e6, r[1] / 1e6, r[4]) for r in recs])
    known = sum(1 for d in dirs if d)
    print(f"단속 방향: {known}/{len(recs)}건 추정 ({known * 100 // max(1, len(recs))}%), {stats}, {time.time() - t0:.0f}초")
    if known < len(recs) // 4:                               # 지도 처리가 이상함 → 기존 DB 유지
        raise SystemExit(f"단속 방향 추정이 너무 적음: {known}건")
    return [r + (d,) for r, d in zip(recs, dirs)]


def build(recs):
    rec = struct.Struct("<iiBBBB")                           # 마지막 1바이트 = 단속 방향 (0 모름, 1~180 = 방향/2 + 1)
    return b"CAM1" + struct.pack("<II", len(recs), rec.size) + b"".join(rec.pack(*r) for r in recs)


def set_output(changed):
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")


def main():
    sys.stdout.reconfigure(line_buffering=True)          # GitHub Actions 로그에 진행 상황이 바로 보이게
    key = os.environ.get("DATA_GO_KR_KEY")
    if not key:
        raise SystemExit("환경변수 DATA_GO_KR_KEY 가 없음 (PC에서는 build_cams_pc.py 사용)")
    os.makedirs(OUT_DIR, exist_ok=True)
    try:
        import osmium  # noqa: F401  (단속 방향 추정에 필요 — 오래 걸리는 받기 전에 먼저 확인)
    except ImportError:
        raise SystemExit("osmium 이 없음 → pip install osmium  (단속 방향 추정에 필요)")
    recs = parse(fetch_all(key, dump="--dump" in sys.argv))
    if len(recs) < MIN_RECORDS:
        raise SystemExit(f"레코드 수 이상: {len(recs)} (기존 DB 유지)")
    recs = add_directions(recs)
    data = build(recs)
    sha = hashlib.sha256(data).hexdigest()
    ver_path = os.path.join(OUT_DIR, "version.json")
    old = {}
    if os.path.exists(ver_path):
        with open(ver_path, encoding="utf-8") as f:
            old = json.load(f)
    if old.get("sha256") == sha:
        print(f"변경 없음 ({len(recs)}건)")
        set_output(False)
        return
    with open(os.path.join(OUT_DIR, "cams.bin"), "wb") as f:
        f.write(data)
    with open(ver_path, "w", encoding="utf-8") as f:
        json.dump({"version": datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M"),
                   "count": len(recs), "size": len(data), "sha256": sha}, f, indent=2)
    print(f"갱신 완료: {len(recs)}건, {len(data)} bytes")
    set_output(True)


if __name__ == "__main__":
    main()
