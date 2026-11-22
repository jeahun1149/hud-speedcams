"""
단속카메라 단속 방향 추정 (OpenStreetMap 도로 지도로) — build_cams.py 가 씀
  카메라마다 CAM_ROAD_M 안의 가장 가까운 도로 선분을 찾아서:
   - 일방통행 도로 (중앙분리대 있는 큰길·고속도로는 지도에서 상·하행이 따로 그려진 일방통행) → 그 도로 방향
   - 양방향 도로 → 우리나라는 우측통행이라 카메라는 단속하는 차로의 오른쪽에 섬
                  → 카메라가 도로 중심선의 오른쪽이면 선분 방향, 왼쪽이면 반대 방향
   - 애매하면 모름 (0): 도로가 멀거나, 중심선에 너무 가깝거나, 양방향 단속(도로노선방향 3)
  결과 = 레코드의 남는 1바이트: 0 모름, 1~180 = 방향/2 + 1 (방향: 북 0°, 동 90°, 2° 단위)
  지도: Geofabrik south-korea-latest.osm.pbf, pip install osmium (지도 처리할 때만 필요)
"""
import math

ROAD = {"motorway", "trunk", "primary", "secondary", "tertiary", "unclassified", "residential", "road",
        "living_street", "motorway_link", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
CELL = 0.002            # 격자 칸 (도, 약 200 m)
CAM_ROAD_M = 30.0       # 카메라와 도로 사이 최대 거리
SIDE_MIN_M = 2.0        # 양방향 도로에서 중심선과 이보다 가까우면 어느 쪽인지 모름


def _cell(lat, lon):
    return int(math.floor(lat / CELL)), int(math.floor(lon / CELL))


def _xy(lat0, lon0, lat, lon):
    """(lat0,lon0) 기준 동쪽 x, 북쪽 y (m)"""
    return (lon - lon0) * 111320.0 * math.cos(math.radians(lat0)), (lat - lat0) * 110540.0


def encode(bearing_deg):
    return int(round((bearing_deg % 360) / 2.0)) % 180 + 1


def direction_for(cam_lat, cam_lon, segs):
    """segs: [(lat1, lon1, lat2, lon2, oneway)] (oneway 1 = 그 방향, -1 = 반대, 0 = 양방향)
    → (방향 도 또는 None, 이유)"""
    best = None
    for la1, lo1, la2, lo2, ow in segs:
        ax, ay = _xy(cam_lat, cam_lon, la1, lo1)
        bx, by = _xy(cam_lat, cam_lon, la2, lo2)
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        if L2 < 0.01:
            continue
        t = max(0.0, min(1.0, (-ax * dx - ay * dy) / L2))   # 카메라(원점)에서 선분에 내린 수선의 발
        d = math.hypot(ax + t * dx, ay + t * dy)
        if best is None or d < best[0]:
            side = (dx * (-ay) - dy * (-ax)) / math.sqrt(L2)   # 중심선에서 떨어진 거리 (+ = 진행 방향 왼쪽)
            best = (d, math.degrees(math.atan2(dx, dy)) % 360, ow, side)
    if best is None or best[0] > CAM_ROAD_M:
        return None, "no_road"
    _, brg, ow, side = best
    if ow == 1:
        return brg, "oneway"
    if ow == -1:
        return (brg + 180) % 360, "oneway"
    if abs(side) < SIDE_MIN_M:
        return None, "center"
    return (brg if side < 0 else brg + 180) % 360, "side"   # 오른쪽(side<0) = 선분 방향을 단속


def _oneway(tags, hw):
    ow_tag = tags.get("oneway")
    if ow_tag in ("yes", "true", "1"):
        return 1
    if ow_tag == "-1":
        return -1
    if ow_tag == "no":
        return 0
    if hw in ("motorway", "motorway_link") or tags.get("junction") in ("roundabout", "circular"):
        return 1                                             # 고속도로·회전교차로는 표시가 없어도 일방통행
    return 0


def _add_segments(need, segs, pts, ow):
    """도로 선분 중 카메라 근처 칸(need)에 걸치는 것만 칸별로 모음"""
    for (la1, lo1), (la2, lo2) in zip(pts, pts[1:]):
        c1, c2 = _cell(la1, lo1), _cell(la2, lo2)
        r0, r1 = sorted((c1[0], c2[0]))
        k0, k1 = sorted((c1[1], c2[1]))
        if (r1 - r0 + 1) * (k1 - k0 + 1) > 400:              # 아주 긴 선분 (거의 없음)
            continue
        seg = (la1, lo1, la2, lo2, ow)
        for r in range(r0, r1 + 1):
            for k in range(k0, k1 + 1):
                if (r, k) in need:
                    segs.setdefault((r, k), []).append(seg)


def _read_roads(pbf_path, need):
    import osmium                                            # (지도 처리할 때만 필요)

    class Roads(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.segs = {}

        def way(self, w):
            hw = w.tags.get("highway")
            if hw not in ROAD:
                return
            try:
                pts = [(n.location.lat, n.location.lon) for n in w.nodes]
            except osmium.InvalidLocationError:
                return
            _add_segments(need, self.segs, pts, _oneway(w.tags, hw))

    h = Roads()
    h.apply_file(pbf_path, locations=True, idx="flex_mem")
    return h.segs


def directions(pbf_path, cams, segs=None):
    """cams: [(lat, lon, 도로노선방향)] → ([방향 바이트], 통계 dict)
    segs 를 주면 지도 대신 그것을 씀 (시험용: {칸: [선분]})"""
    if segs is None:
        need = set()
        for lat, lon, _ in cams:
            r, k = _cell(lat, lon)
            for dr in (-1, 0, 1):
                for dk in (-1, 0, 1):
                    need.add((r + dr, k + dk))
        segs = _read_roads(pbf_path, need)
    out, stats = [], {}
    for lat, lon, drc in cams:
        if drc == 3:                                         # 양방향 단속 → 방향 걸러내지 않음
            out.append(0)
            stats["both_dir"] = stats.get("both_dir", 0) + 1
            continue
        r, k = _cell(lat, lon)
        near = [s for dr in (-1, 0, 1) for dk in (-1, 0, 1) for s in segs.get((r + dr, k + dk), ())]
        brg, why = direction_for(lat, lon, near)
        out.append(0 if brg is None else encode(brg))
        stats[why] = stats.get(why, 0) + 1
    return out, stats
