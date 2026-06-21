"""
=========================================================
회식 장소 최적 위치 산출 앱 (Streamlit 버전)
- GitHub -> Streamlit Cloud 배포용
- 참석자 주소 기반 이동 반경 교집합 탐색
- 교집합 내 실제 상권(식당/카페/주점) 좌표로 마커 위치 보정(Snap)

[실행 방법]
1) requirements.txt 에 아래 패키지 명시
   streamlit
   folium
   streamlit-folium
   geopy
   numpy
   requests
2) Streamlit Cloud 에서 app.py 를 메인 파일로 지정하여 배포
=========================================================
"""

import time
import requests
import numpy as np
import folium
import streamlit as st
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim
from geopy.extra.rate_limiter import RateLimiter

# ---------------------------------------------------------
# 페이지 설정
# ---------------------------------------------------------
st.set_page_config(page_title="회식 장소 최적 위치 산출", page_icon="🍻", layout="wide")


# ---------------------------------------------------------
# 1. 지오코딩: 캐싱 + Rate Limiter
#    @st.cache_resource 로 객체를 한 번만 생성
# ---------------------------------------------------------
@st.cache_resource
def get_geocoder():
    geolocator = Nominatim(user_agent="streamlit_meetup_finder_v1")
    return RateLimiter(geolocator.geocode, min_delay_seconds=1.1)


@st.cache_data(show_spinner=False)
def get_lat_lon(address: str):
    """주소 텍스트를 위도, 경도 좌표로 변환 (결과 캐싱)"""
    address = address.strip()
    if not address:
        return None

    geocode = get_geocoder()
    queries = [address]
    if "대한민국" not in address:
        queries.append(f"대한민국 {address}")

    for q in queries:
        try:
            loc = geocode(q, timeout=10)
            if loc:
                return (loc.latitude, loc.longitude)
        except Exception:
            continue
    return None


# ---------------------------------------------------------
# 2. 상권 POI 수집: 다중 미러 서버 + 재시도
# ---------------------------------------------------------
_OVERPASS_MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
]


@st.cache_data(show_spinner=False)
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
    return 80


# ---------------------------------------------------------
# 4. 벡터화된 교집합 격자 탐색
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

    # 모든 후보의 주변 상권이 0개면 동률 후보들의 기하학적 중심을 사용
    if best_poi_count <= 0 and len(overlap_points) > 1:
        lats = [p[0] for p in overlap_points]
        lons = [p[1] for p in overlap_points]
        best_coord = (float(np.mean(lats)), float(np.mean(lons)))
        best_poi_count = 0

    return best_coord, best_poi_count


# ===========================================================
# 6. 화면 구성 (사이드바: 입력 / 메인: 지도)
# ===========================================================
st.title("🍻 회식 장소 최적 위치 산출")
st.caption("참석자 주소와 이동 반경을 입력하면, 모두의 이동 범위가 겹치는 상권 중심을 추천합니다.")

# 참석자 입력 개수를 세션 상태로 관리
if "num_people" not in st.session_state:
    st.session_state.num_people = 3

with st.sidebar:
    st.header("입력")

    col_a, col_b = st.columns(2)
    if col_a.button("➕ 인원 추가"):
        st.session_state.num_people += 1
    if col_b.button("➖ 인원 삭제") and st.session_state.num_people > 2:
        st.session_state.num_people -= 1

    default_addresses = [
        "충남 천안시 성황로 40",
        "충남 아산시 삼성로 180",
        "충남 천안시 성성6로 21",
    ]

    addresses = []
    for i in range(st.session_state.num_people):
        default = default_addresses[i] if i < len(default_addresses) else ""
        addr = st.text_input(f"참석자 {i + 1}", value=default, key=f"addr_{i}")
        addresses.append(addr)

    radius_km = st.slider("이동 반경 (km)", min_value=0.5, max_value=10.0, value=3.0, step=0.5)
    run = st.button("🔍 정밀 최적 위치 산출", type="primary", use_container_width=True)


# ===========================================================
# 7. 메인 연산 및 지도 렌더링
# ===========================================================
def build_map(valid_coords, radius_km):
    overlap_points, max_overlap, dlat, dlon, bounds = find_overlap_grid(valid_coords, radius_km)
    lat_min, lat_max, lon_min, lon_max = bounds

    pois = get_commercial_pois(lat_min, lat_max, lon_min, lon_max)
    final_coord, poi_count = snap_to_best_commercial_spot(overlap_points, pois)

    lats = [c[0] for c in valid_coords]
    lons = [c[1] for c in valid_coords]
    center = [float(np.mean(lats)), float(np.mean(lons))]

    m = folium.Map(location=center, zoom_start=12)

    for lat, lon in valid_coords:
        folium.CircleMarker(
            location=[lat, lon], radius=5, color="black", fill=True, fill_color="black"
        ).add_to(m)
        folium.Circle(
            location=[lat, lon], radius=radius_km * 1000,
            color="#3186cc", fill=True, fill_color="#3186cc", fill_opacity=0.15, weight=1
        ).add_to(m)

    # 교집합 영역 렌더링 (브라우저 부담 방지를 위해 최대 600개로 샘플링)
    render_points = overlap_points
    if len(render_points) > 600:
        step = len(render_points) // 600
        render_points = render_points[::step]

    for p_lat, p_lon in render_points:
        folium.Rectangle(
            bounds=[(p_lat - dlat / 2, p_lon - dlon / 2), (p_lat + dlat / 2, p_lon + dlon / 2)],
            color="red", fill=True, fill_color="red", fill_opacity=0.3, weight=0
        ).add_to(m)

    if final_coord:
        popup = f"최적 위치 ({max_overlap}명 겹침"
        popup += f" / 반경 300m 내 상권 {poi_count}개)" if poi_count > 0 else " / 주변 상권 정보 없음)"
        folium.Marker(
            location=list(final_coord), popup=popup,
            icon=folium.Icon(color="red", icon="star")
        ).add_to(m)

    m.fit_bounds([[lat_min, lon_min], [lat_max, lon_max]])
    return m, max_overlap, final_coord, poi_count, len(pois)


if run:
    filled = [a.strip() for a in addresses if a.strip()]
    if len(filled) < 2:
        st.warning("⚠️ 최소 2명 이상의 주소를 입력해야 교집합을 계산할 수 있습니다.")
    else:
        with st.spinner("주소 변환 → 교집합 탐색 → 상권 보정 중..."):
            valid_coords = []
            failed = []
            for addr in filled:
                coord = get_lat_lon(addr)
                if coord:
                    valid_coords.append(coord)
                else:
                    failed.append(addr)

            if failed:
                st.warning("좌표 변환 실패한 주소: " + ", ".join(failed))

            if len(valid_coords) < 2:
                st.error("유효한 좌표가 2개 미만이라 계산할 수 없습니다. 주소를 확인해 주세요.")
            else:
                m, max_overlap, final_coord, poi_count, poi_total = build_map(valid_coords, radius_km)

                c1, c2, c3 = st.columns(3)
                c1.metric("최대 겹침 인원", f"{max_overlap}명")
                c2.metric("탐색된 상권 수", f"{poi_total}개")
                c3.metric("추천지 반경 300m 상권", f"{poi_count}개")

                st_folium(m, width=900, height=560, returned_objects=[])

                if final_coord:
                    st.success(f"⭐ 추천 좌표: {final_coord[0]:.5f}, {final_coord[1]:.5f}")
else:
    st.info("좌측 사이드바에서 주소와 반경을 입력하고 [정밀 최적 위치 산출]을 눌러주세요.")
