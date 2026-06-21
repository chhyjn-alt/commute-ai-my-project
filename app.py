"""
=========================================================
회식 장소 최적 위치 산출 프로그램 (v3)
- 참석자 주소 기반 이동 반경 교집합 탐색
- 교집합 내 실제 상권(식당/카페/주점) 좌표로 마커 위치 보정(Snap)

[원본(v2) 대비 주요 개선 사항]
1. 지오코딩 캐싱 + RateLimiter 적용
   → 같은 주소 재계산 시 즉시 응답, Nominatim 사용 정책(1req/sec) 준수로 차단 방지
2. Overpass API 다중 미러 서버 + 재시도 로직
   → 메인 서버 혼잡/장애 시에도 다른 서버로 자동 전환하여 상권 데이터 수집
3. 격자 탐색을 NumPy 벡터 연산(meshgrid)으로 전환
   → 이중 for문 제거, 반경/해상도가 커져도 안정적인 속도 유지
4. "교집합 2명 이상일 때만 POI 보정" 제약 제거
   → 원본은 1명만 겹쳐도 보정이 전혀 안 됐음. 이제 겹침이 1명이라도 상권 보정 적용
5. 동률 후보가 다수인데 주변 상권 정보가 전혀 없는 경우,
   임의의 첫 번째 점이 아니라 동률 후보들의 기하학적 중심으로 대체 → 결과 안정성 향상
6. 반경 크기에 따라 격자 해상도를 자동 조절
   → 넓은 범위는 과도하게 세밀한 격자로 인한 연산 지연 방지, 좁은 범위는 더 세밀하게
7. 입력 검증(최소 2명) 및 단계별 진행 메시지(①~④) 추가로 디버깅/시연 가독성 향상
=========================================================
"""

import time
import requests
import numpy as np
import folium
import ipywidgets as widgets
from IPython.display import display, clear_output
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

# ---------------------------------------------------------
# 1. 지오코딩: 캐싱 + Rate Limiter
# ---------------------------------------------------------
_geolocator = Nominatim(user_agent="colab_ai_class_poi_v3")
_geocode_limited = RateLimiter(_geolocator.geocode, min_delay_seconds=1.1)
_geocode_cache = {}


def get_lat_lon(address: str):
    """주소 텍스트를 위도, 경도 좌표로 변환 (캐싱 적용)"""
    address = address.strip()
    if not address:
        return None
    if address in _geocode_cache:
        return _geocode_cache[address]

    queries = [address]
    if "대한민국" not in address:
        queries.append(f"대한민국 {address}")

    for q in queries:
        try:
            loc = _geocode_limited(q, timeout=10)
            if loc:
                coord = (loc.latitude, loc.longitude)
                _geocode_cache[address] = coord
                return coord
        except Exception:
            continue

    _geocode_cache[address] = None
    return None


# ---------------------------------------------------------
# 2. 상권 POI 수집: 다중 미러 서버 + 재시도
# ---------------------------------------------------------
_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


def get_commercial_pois(lat_min, lat_max, lon_min, lon_max):
    """지정 범위 내 상업 시설(식당/카페/주점/펍) 좌표 수집"""
    query = f"""
    [out:json][timeout:25];
    (
      node["amenity"="restaurant"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["amenity"="cafe"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["amenity"="bar"]({lat_min},{lon_min},{lat_max},{lon_max});
      node["amenity"="pub"]({lat_min},{lon_min},{lat_max},{lon_max});
    );
    out center;
    """
    for url in _OVERPASS_MIRRORS:
        try:
            resp = requests.get(url, params={"data": query}, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            pois = [[el["lat"], el["lon"]] for el in data.get("elements", []) if "lat" in el]
            return np.array(pois) if pois else np.array([])
        except Exception:
            time.sleep(0.4)
            continue
    return np.array([])


# ---------------------------------------------------------
# 3. 반경 크기에 따른 격자 해상도 자동 조절
# ---------------------------------------------------------
def adaptive_grid_size(radius_km: float) -> int:
    if radius_km <= 2:
        return 150
    elif radius_km <= 5:
        return 110
    else:
        return 80


# ---------------------------------------------------------
# 4. 벡터화된 교집합 격자 탐색 (핵심 성능 개선)
# ---------------------------------------------------------
def find_overlap_grid(valid_coords, radius_km):
    lats, lons = zip(*valid_coords)
    margin = (radius_km / 111.0) + 0.01
    lat_min, lat_max = min(lats) - margin, max(lats) + margin
    lon_min, lon_max = min(lons) - margin, max(lons) + margin

    grid_size = adaptive_grid_size(radius_km)
    grid_lats = np.linspace(lat_min, lat_max, grid_size)
    grid_lons = np.linspace(lon_min, lon_max, grid_size)
    glat_mesh, glon_mesh = np.meshgrid(grid_lats, grid_lons, indexing="ij")

    overlap_count = np.zeros_like(glat_mesh, dtype=int)
    for lat, lon in valid_coords:
        dist = np.sqrt(
            ((glat_mesh - lat) * 111.0) ** 2
            + ((glon_mesh - lon) * 111.0 * np.cos(np.radians(lat))) ** 2
        )
        overlap_count += (dist <= radius_km)

    max_overlap = int(overlap_count.max())
    mask = overlap_count == max_overlap
    overlap_points = list(zip(glat_mesh[mask].tolist(), glon_mesh[mask].tolist()))

    dlat = grid_lats[1] - grid_lats[0]
    dlon = grid_lons[1] - grid_lons[0]
    bounds = (lat_min, lat_max, lon_min, lon_max)
    return overlap_points, max_overlap, dlat, dlon, bounds


# ---------------------------------------------------------
# 5. 후보 지점 중 상권 밀집도가 가장 높은 곳으로 마커 보정
# ---------------------------------------------------------
def snap_to_best_commercial_spot(overlap_points, pois, snap_radius_km=0.3):
    best_poi_count = -1
    best_coord = None

    for p_lat, p_lon in overlap_points:
        if pois.size > 0:
            dist = np.sqrt(
                ((pois[:, 0] - p_lat) * 111.0) ** 2
                + ((pois[:, 1] - p_lon) * 111.0 * np.cos(np.radians(p_lat))) ** 2
            )
            nearby = pois[dist <= snap_radius_km]
            poi_count = len(nearby)
        else:
            nearby = np.array([])
            poi_count = 0

        if poi_count > best_poi_count:
            best_poi_count = poi_count
            if poi_count > 0:
                best_coord = (float(np.mean(nearby[:, 0])), float(np.mean(nearby[:, 1])))
            else:
                best_coord = (p_lat, p_lon)

    # 모든 후보의 주변 상권이 0개라면, 임의의 한 점이 아니라
    # 동률 후보들의 기하학적 중심을 사용해 결과를 더 안정적으로 만든다.
    if best_poi_count <= 0 and len(overlap_points) > 1:
        lats = [p[0] for p in overlap_points]
        lons = [p[1] for p in overlap_points]
        best_coord = (float(np.mean(lats)), float(np.mean(lons)))
        best_poi_count = 0

    return best_coord, best_poi_count


# ===========================================================
# 6. UI 위젯 구성
# ===========================================================
title = widgets.HTML(
    "<h2>🍻 회식 장소 최적 위치 산출 프로그램 (v3)</h2>"
    "<p>참석자 주소와 반경을 입력하고 <b>[정밀 최적 위치 산출]</b> 버튼을 누르세요.</p>"
)

address_inputs = [
    widgets.Text(value='충남 천안시 성황로 40', description='참석자 1:', layout={'width': '400px'}),
    widgets.Text(value='충남 아산시 삼성로 180', description='참석자 2:', layout={'width': '400px'}),
    widgets.Text(value='충남 천안시 성성6로 21', description='참석자 3:', layout={'width': '400px'}),
]
inputs_box = widgets.VBox(address_inputs)

add_btn = widgets.Button(description="➕ 인원 추가", button_style='info')
radius_slider = widgets.FloatSlider(value=3.0, min=0.5, max=10.0, step=0.5, description='반경(km):')
calc_btn = widgets.Button(description="🔍 정밀 최적 위치 산출", button_style='primary')

map_output_area = widgets.Output()


def add_address(b):
    new_idx = len(address_inputs) + 1
    new_input = widgets.Text(placeholder='예: 천안시 불당동', description=f'참석자 {new_idx}:', layout={'width': '400px'})
    address_inputs.append(new_input)
    inputs_box.children = tuple(address_inputs)


add_btn.on_click(add_address)


# ===========================================================
# 7. 메인 연산 함수
# ===========================================================
def calculate_optimal_location(b):
    with map_output_area:
        clear_output(wait=True)

        addresses = [w.value.strip() for w in address_inputs if w.value.strip()]
        if len(addresses) < 2:
            print("⚠️ 최소 2명 이상의 주소를 입력해야 교집합을 계산할 수 있습니다.")
            return

        print(f"① 주소 {len(addresses)}건 좌표 변환 중...")
        valid_coords = []
        for addr in addresses:
            coord = get_lat_lon(addr)
            if coord:
                valid_coords.append(coord)
            else:
                print(f"   - 좌표 변환 실패(오류 주소): {addr}")

        if len(valid_coords) < 2:
            print("⚠️ 유효한 좌표가 2개 미만이라 교집합을 계산할 수 없습니다.")
            return

        radius_km = radius_slider.value
        print(f"② 반경 {radius_km}km 기준 이동 범위 교집합 탐색 중...")
        overlap_points, max_overlap, dlat, dlon, bounds = find_overlap_grid(valid_coords, radius_km)
        lat_min, lat_max, lon_min, lon_max = bounds

        print("③ 상권(식당/카페/주점) 데이터 수집 중...")
        pois = get_commercial_pois(lat_min, lat_max, lon_min, lon_max)
        print(f"   - 수집된 상권 수: {len(pois)}개")

        print("④ 최적 위치 보정 및 지도 렌더링 중...")
        final_coord, poi_count = snap_to_best_commercial_spot(overlap_points, pois)

        lats = [c[0] for c in valid_coords]
        lons = [c[1] for c in valid_coords]
        center_lat, center_lon = float(np.mean(lats)), float(np.mean(lons))

        m = folium.Map(location=[center_lat, center_lon], zoom_start=12, width=800, height=520)

        for lat, lon in valid_coords:
            folium.CircleMarker(
                location=[lat, lon], radius=5, color='black', fill=True, fill_color='black'
            ).add_to(m)
            folium.Circle(
                location=[lat, lon], radius=radius_km * 1000,
                color='#3186cc', fill=True, fill_color='#3186cc', fill_opacity=0.15, weight=1
            ).add_to(m)

        # 교집합 영역 렌더링 (브라우저 부담 방지를 위해 최대 600개로 샘플링하여 표시,
        # 단 최적 위치 계산에는 전체 후보를 사용함)
        render_points = overlap_points
        if len(render_points) > 600:
            step = len(render_points) // 600
            render_points = render_points[::step]

        for p_lat, p_lon in render_points:
            folium.Rectangle(
                bounds=[(p_lat - dlat / 2, p_lon - dlon / 2), (p_lat + dlat / 2, p_lon + dlon / 2)],
                color='red', fill=True, fill_color='red', fill_opacity=0.3, weight=0
            ).add_to(m)

        if final_coord:
            popup = f"최적 위치 ({max_overlap}명 겹침"
            popup += f" / 반경 300m 내 상권 {poi_count}개)" if poi_count > 0 else " / 주변 상권 정보 없음)"
            folium.Marker(
                location=list(final_coord), popup=popup,
                icon=folium.Icon(color='red', icon='star')
            ).add_to(m)

        legend_html = """
        <div style="position: fixed; bottom: 30px; left: 30px; z-index:9999;
                    background-color: white; padding: 10px; border-radius: 6px;
                    border: 1px solid #ccc; font-size: 13px;">
        <b>범례</b><br>
        ⚫ 참석자 위치 &nbsp;|&nbsp; 🔵 이동 가능 반경 &nbsp;|&nbsp; 🔴 최대 교집합 영역 &nbsp;|&nbsp; ⭐ 추천 장소
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))
        m.fit_bounds([[lat_min, lon_min], [lat_max, lon_max]])

        print("✅ 연산 완료. 아래 지도를 확인하세요.")
        display(m)


calc_btn.on_click(calculate_optimal_location)

# ===========================================================
# 8. 화면 출력
# ===========================================================
ui_container = widgets.VBox([
    title,
    inputs_box,
    widgets.HBox([add_btn, radius_slider]),
    calc_btn,
])

display(ui_container)
display(map_output_area)
1 1
