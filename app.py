"""
=========================================================
행복한 퇴근 이후 - 통합 앱 v5 (Streamlit / GitHub 배포 버전)
  탭1) 퇴근시간 최적화 AI
  탭2) 회식장소 최적위치 산출기
  탭3) 출발 알리미

[v5 개선 사항]  * v4 기능은 모두 유지
1) 회식 결과 문서 공유 (PDF / HTML)
  - 검색 결과를 PDF 파일로 저장해 카카오톡·메일 등으로 첨부 공유
  - PDF에 산출 조건, 추천 중심 도로명 주소, 참석자별 소요시간,
    선택한 음식 종류 TOP5, 카카오맵 위치 링크 포함
  - 한글 폰트 자동 탐색 (나눔고딕/맑은 고딕). 폰트가 없으면
    안내 문구를 표시하고, HTML 저장은 항상 가능
  - 중심 좌표 → 도로명 주소 자동 변환 (카카오 좌표→주소 API)
2) 장거리 모임 대응 (거점 도시 기반 2단계 선정)
  - 참석자 최대 직선 이격 60km 이상이면 자동으로 장거리 모드 전환
    (자동 / 일반 고정 / 장거리 고정 선택 가능)
  - 1단계: 전국 주요 KTX/SRT역(교통 거점) 후보를 직선거리로 압축한 뒤
    카카오내비 실시간 소요시간으로 최적 거점 도시 선정
  - 2단계: 거점역 주변 음식점 밀집도(상권)를 분석해 중심을 미세 조정
  - 장거리는 KTX/SRT 이용 권장 안내, 표시 시간은 자동차 실시간 기준
    (대중교통 간이 추정·회식시각 혼잡도 보정은 장거리에 미적용)
  - 같은 도시·인접 도시 모임은 기존 격자 탐색 로직을 그대로 사용

[배포 참고]
  - requirements.txt에 fpdf2 추가
  - Streamlit Cloud에서 한글 PDF를 쓰려면 저장소 루트에 packages.txt를
    만들고 fonts-nanum 한 줄 추가 (또는 NanumGothic.ttf 파일을 저장소에 포함)
=========================================================
"""

import html
import json
import math
import os
import random
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
import pandas as pd
import folium
import streamlit as st
from streamlit_folium import st_folium

try:
    import polyline
    _HAS_POLYLINE = True
except Exception:
    _HAS_POLYLINE = False

try:
    from fpdf import FPDF
    _HAS_FPDF = True
except Exception:
    FPDF = None
    _HAS_FPDF = False


# =========================================================
# 0. 페이지 설정 / 상태 초기화 / 공통 유틸
# =========================================================
st.set_page_config(page_title="행복한 퇴근 이후", page_icon="🌆", layout="centered")

_DEFAULT_STATE = {
    "num_people": 3,
    "favorite_contact": "",
    "commute_data": None,
    "dinner_data": None,
    "notify_data": None,
    "error_log": [],
}
for k, v in _DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v


def now_kst():
    return datetime.utcnow() + timedelta(hours=9)


def log_error(context, err):
    """오류를 세션 기록에 저장 (사이드바에서 확인)"""
    msg = f"{now_kst().strftime('%H:%M:%S')} [{context}] {type(err).__name__}: {err}"
    st.session_state.error_log.append(msg)
    st.session_state.error_log = st.session_state.error_log[-20:]


def load_kakao_key():
    """Secrets 우선 조회, 없으면 내장 키 사용 (키 재발급 후 Secrets 이전 권장)"""
    try:
        return st.secrets["KAKAO_REST_KEY"]
    except Exception:
        return "df68bf65618592b6d685caec6521432f"


KAKAO_KEY = load_kakao_key()
_TIMEOUT = 8

_OSRM_MIRRORS = [
    "https://router.project-osrm.org",
    "https://routing.openstreetmap.de/routed-car",
]


# =========================================================
# 1. 주소 즐겨찾기 (파일 저장)
# =========================================================
_FAV_FILE = "favorites.json"


def load_favorites():
    try:
        with open(_FAV_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def save_favorites(favs):
    try:
        with open(_FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favs, f, ensure_ascii=False)
    except Exception as e:
        log_error("즐겨찾기 저장", e)


if "fav_addresses" not in st.session_state:
    st.session_state.fav_addresses = load_favorites()


# =========================================================
# 2. 외부 API 함수
# =========================================================
def search_kakao_address(query):
    """카카오 주소 검색 (좌표 변환)"""
    if not query or not query.strip():
        return []
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"query": query, "size": 10}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        return res.get("documents", [])
    except Exception as e:
        log_error("주소 검색", e)
        st.warning(f"주소 검색 통신에 실패했습니다: {query}")
        return []


@st.cache_data(show_spinner=False, ttl=3600)
def coord_to_address(lat, lon):
    """좌표 → 도로명(없으면 지번) 주소 변환. 실패 시 None."""
    url = "https://dapi.kakao.com/v2/local/geo/coord2address.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"x": f"{lon:.6f}", "y": f"{lat:.6f}"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        docs = res.get("documents", [])
        if docs:
            road = docs[0].get("road_address")
            if road and road.get("address_name"):
                return road["address_name"]
            jibun = docs[0].get("address")
            if jibun and jibun.get("address_name"):
                return jibun["address_name"]
    except Exception:
        pass
    return None


@st.cache_data(show_spinner=False, ttl=600)
def get_realtime_weather_and_temp(lat, lon):
    """지정 좌표의 현재 날씨/기온 (Open-Meteo, 무료/키 불필요)"""
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat:.3f}&longitude={lon:.3f}"
               "&current_weather=true&timezone=auto")
        res = requests.get(url, timeout=_TIMEOUT).json()
        cur = res.get("current_weather", {})
        code = cur.get("weathercode", 0)
        temp = cur.get("temperature", 20.0)
        if code in [0, 1, 2, 3]:
            desc = "맑음/구름"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]:
            desc = "비 (강수)"
        elif code in [71, 73, 75, 85, 86]:
            desc = "눈 (결빙)"
        else:
            desc = "기타"
        return desc, temp
    except Exception:
        return "수집 실패", 20.0


def weather_multiplier(desc):
    """날씨에 따른 소요시간 보정 계수"""
    if "비" in desc:
        return 1.15
    if "눈" in desc:
        return 1.30
    return 1.0


def peak_val(h):
    """퇴근 혼잡도 가우시안 모델 (18시 15분 피크)"""
    return math.exp(-((h - 18.25) ** 2) / 0.04) if h <= 18.25 else math.exp(-((h - 18.25) ** 2) / 0.12)


@st.cache_data(show_spinner=False, ttl=300)
def get_kakao_routes(o_lat, o_lon, d_lat, d_lon, alternatives=False):
    """카카오내비 경로 목록 [{dist(km), dur(분), path}]. alternatives=True면 대안경로 포함."""
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"origin": f"{o_lon},{o_lat}", "destination": f"{d_lon},{d_lat}",
              "priority": "RECOMMEND"}
    if alternatives:
        params["alternatives"] = "true"
    out = []
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        for r in res.get("routes", []):
            if r.get("result_code") != 0:
                continue
            s = r["summary"]
            path = []
            for sec in r.get("sections", []):
                for road in sec.get("roads", []):
                    vs = road.get("vertexes", [])
                    for i in range(0, len(vs), 2):
                        path.append([vs[i + 1], vs[i]])
            out.append({"dist": round(s["distance"] / 1000, 1),
                        "dur": round(s["duration"] / 60, 1),
                        "path": path})
    except Exception:
        pass
    return out


def get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon):
    """카카오내비 대표 경로 (거리, 시간, 경로좌표)"""
    routes = get_kakao_routes(o_lat, o_lon, d_lat, d_lon)
    if routes:
        r = routes[0]
        return r["dist"], r["dur"], r["path"]
    return None, None, []


def osrm_request(path_and_query):
    """OSRM 요청을 미러 서버로 재시도"""
    for base in _OSRM_MIRRORS:
        try:
            res = requests.get(f"{base}{path_and_query}", timeout=_TIMEOUT)
            res.raise_for_status()
            return res.json()
        except Exception:
            time.sleep(0.3)
            continue
    return {}


def get_real_road_path(waypoints):
    """OSRM 경로 좌표 (polyline 미설치 시 직선 좌표 반환)"""
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
    data = osrm_request(f"/route/v1/driving/{coord_str}?overview=full&geometries=polyline")
    routes = data.get("routes", [])
    if routes and _HAS_POLYLINE:
        try:
            return polyline.decode(routes[0]["geometry"])
        except Exception:
            return waypoints
    return waypoints


def get_kakao_duration_min(o_lat, o_lon, d_lat, d_lon):
    """카카오내비 실시간 교통 기준 소요시간(분). 실패 시 None. (스레드 안전)"""
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"origin": f"{o_lon},{o_lat}", "destination": f"{d_lon},{d_lat}", "priority": "TIME"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        routes = res.get("routes", [])
        if routes and routes[0].get("result_code") == 0:
            return routes[0]["summary"]["duration"] / 60.0
    except Exception:
        pass
    return None


def build_realtime_matrix(sources, candidates):
    """참석자 x 후보지 실시간 소요시간 행렬(분) 병렬 계산"""
    matrix = [[None] * len(candidates) for _ in sources]
    with ThreadPoolExecutor(max_workers=5) as ex:
        futures = {}
        for s_idx, (s_lat, s_lon) in enumerate(sources):
            for d_idx, (d_lat, d_lon) in enumerate(candidates):
                fut = ex.submit(get_kakao_duration_min, s_lat, s_lon, d_lat, d_lon)
                futures[fut] = (s_idx, d_idx)
        for fut, (s_idx, d_idx) in futures.items():
            try:
                matrix[s_idx][d_idx] = fut.result()
            except Exception:
                matrix[s_idx][d_idx] = None
    return matrix


def get_route_path(o_lat, o_lon, d_lat, d_lon):
    """실제 도로 이동 경로 좌표 (카카오 우선, 실패 시 OSRM, 최후 직선)"""
    _, _, path = get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon)
    if path:
        return path
    return get_real_road_path([[o_lat, o_lon], [d_lat, d_lon]])


def approx_km(a_lat, a_lon, b_lat, b_lon):
    """두 좌표 간 근사 거리(km)"""
    return math.sqrt(((a_lat - b_lat) * 111.0) ** 2
                     + ((a_lon - b_lon) * 111.0 * math.cos(math.radians(a_lat))) ** 2)


def max_spread_km(points):
    """참석자 간 최대 이격 거리(km)"""
    best = 0.0
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            best = max(best, approx_km(points[i][0], points[i][1], points[j][0], points[j][1]))
    return best


def make_grid(center_lat, center_lon, off_km):
    """중심 좌표 주변 3x3 후보 격자 생성"""
    lat_off = off_km / 111.0
    lon_off = off_km / (111.0 * math.cos(math.radians(center_lat)))
    return [(center_lat + lat_off * i, center_lon + lon_off * j)
            for i in (-1, 0, 1) for j in (-1, 0, 1)]


# 음식 종류별 검색 설정 (표시명: (검색어, 카카오 카테고리 코드))
CATEGORY_OPTIONS = {
    "전체 (맛집)": ("맛집", "FD6"),
    "한식": ("한식", "FD6"),
    "중식": ("중식", "FD6"),
    "일식": ("일식", "FD6"),
    "양식": ("양식", "FD6"),
    "고기/구이": ("고기구이", "FD6"),
    "치킨": ("치킨", "FD6"),
    "회/해산물": ("횟집", "FD6"),
    "술집/호프": ("술집", "FD6"),
    "카페/디저트": ("카페", "CE7"),
}


def get_kakao_restaurants(lat, lon, radius_m, query="맛집", cat_code="FD6", sort="accuracy"):
    """카카오 키워드 검색으로 주변 식당 상위 5곳 (스레드 안전)"""
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"query": query, "category_group_code": cat_code,
              "x": str(lon), "y": str(lat), "radius": int(radius_m), "sort": sort}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        return res.get("documents", [])[:5]
    except Exception:
        return []


def get_kakao_place_count(lat, lon, radius_m, query="맛집", cat_code="FD6"):
    """반경 내 음식점 총 매장 수 (상권 밀집도 지표, 스레드 안전)"""
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"query": query, "category_group_code": cat_code,
              "x": str(lon), "y": str(lat), "radius": int(radius_m), "size": 1}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        return int(res.get("meta", {}).get("total_count", 0))
    except Exception:
        return 0


def fetch_all_category_restaurants(lat, lon, radius_m):
    """모든 음식 종류의 TOP 5를 병렬 수집.
    기본 반경 내에 없으면 광역(최대 10km)에서 거리순으로 재탐색하고,
    찾은 매장들의 무게중심 좌표를 재조정 중심으로 함께 반환한다."""
    results, adjusted = {}, {}
    wide_r = min(max(int(radius_m) * 5, 3000), 10000)

    def fetch_one(label, q, c):
        docs = get_kakao_restaurants(lat, lon, radius_m, q, c)
        if docs:
            return label, docs, None
        docs = get_kakao_restaurants(lat, lon, wide_r, q, c, sort="distance")
        if docs:
            c_lat = sum(float(r["y"]) for r in docs) / len(docs)
            c_lon = sum(float(r["x"]) for r in docs) / len(docs)
            return label, docs, (c_lat, c_lon)
        return label, [], None

    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(fetch_one, label, q, c) for label, (q, c) in CATEGORY_OPTIONS.items()]
        for fut in futs:
            try:
                label, docs, adj_center = fut.result()
                results[label] = docs
                if adj_center:
                    adjusted[label] = {"center": adj_center}
            except Exception:
                pass
    return results, adjusted


# =========================================================
# 2.5 장거리 모임: 전국 교통 거점(KTX/SRT역) 기반 선정
# =========================================================
_LONG_DIST_KM = 60  # 참석자 최대 직선 이격이 이 값 이상이면 장거리 모드 자동 전환

# 전국 주요 교통 거점 (KTX/SRT 정차역 중심, 좌표는 역사 기준 근사값)
MAJOR_HUBS = [
    {"city": "서울", "name": "서울역", "lat": 37.5547, "lon": 126.9706},
    {"city": "서울", "name": "용산역", "lat": 37.5298, "lon": 126.9648},
    {"city": "서울", "name": "수서역(SRT)", "lat": 37.4871, "lon": 127.1013},
    {"city": "서울", "name": "청량리역", "lat": 37.5801, "lon": 127.0459},
    {"city": "광명", "name": "광명역", "lat": 37.4162, "lon": 126.8848},
    {"city": "수원", "name": "수원역", "lat": 37.2659, "lon": 127.0010},
    {"city": "평택", "name": "평택지제역", "lat": 37.0187, "lon": 127.0703},
    {"city": "천안아산", "name": "천안아산역", "lat": 36.7943, "lon": 127.1045},
    {"city": "청주", "name": "오송역", "lat": 36.6203, "lon": 127.3269},
    {"city": "대전", "name": "대전역", "lat": 36.3316, "lon": 127.4344},
    {"city": "김천구미", "name": "김천구미역", "lat": 36.1135, "lon": 128.0533},
    {"city": "대구", "name": "동대구역", "lat": 35.8797, "lon": 128.6286},
    {"city": "경주", "name": "경주역(KTX)", "lat": 35.7981, "lon": 129.1391},
    {"city": "울산", "name": "울산역", "lat": 35.5514, "lon": 129.1386},
    {"city": "부산", "name": "부산역", "lat": 35.1151, "lon": 129.0405},
    {"city": "창원", "name": "창원중앙역", "lat": 35.2323, "lon": 128.6721},
    {"city": "광주", "name": "광주송정역", "lat": 35.1372, "lon": 126.7934},
    {"city": "전주", "name": "전주역", "lat": 35.8404, "lon": 127.1614},
    {"city": "익산", "name": "익산역", "lat": 35.9394, "lon": 126.9460},
    {"city": "강릉", "name": "강릉역", "lat": 37.7638, "lon": 128.8996},
    {"city": "포항", "name": "포항역", "lat": 36.0710, "lon": 129.3415},
    {"city": "목포", "name": "목포역", "lat": 34.7936, "lon": 126.3888},
    {"city": "여수", "name": "여수엑스포역", "lat": 34.7527, "lon": 127.7469},
]


def thin_path(path, max_pts=1200):
    """장거리 경로 좌표를 지도 표시용으로 간격 축소 (형상 유지)"""
    if not path or len(path) <= max_pts:
        return path
    step = len(path) / float(max_pts)
    out = [path[int(i * step)] for i in range(max_pts)]
    out.append(path[-1])
    return out


def select_best_hub(sources, fair_mode, top_n=4):
    """장거리 1단계: 거점 도시(역) 선정.
    직선거리로 후보 top_n 압축 → 카카오내비 실시간 소요시간으로 최종 선정.
    실시간 실패 시 OSRM 표준 경로, 그마저 실패하면 직선거리 1순위.
    반환: (hub dict, 참석자별 시간 리스트 또는 None, 실시간 성공 여부)"""
    scored = []
    for hub in MAJOR_HUBS:
        ds = [approx_km(s[0], s[1], hub["lat"], hub["lon"]) for s in sources]
        score = max(ds) if fair_mode else sum(ds)
        scored.append((score, hub))
    scored.sort(key=lambda x: x[0])
    cand_hubs = [h for _, h in scored[:top_n]]
    cand_pts = [(h["lat"], h["lon"]) for h in cand_hubs]

    def pick(mat):
        b_idx, b_score, b_times = None, float("inf"), []
        for d_idx in range(len(cand_pts)):
            ts = [mat[s][d_idx] for s in range(len(sources))]
            if any(t is None for t in ts):
                continue
            sc = max(ts) if fair_mode else sum(ts)
            if sc < b_score:
                b_idx, b_score, b_times = d_idx, sc, ts
        return b_idx, b_times

    mat = build_realtime_matrix(sources, cand_pts)
    idx, times = pick(mat)
    if idx is not None:
        return cand_hubs[idx], times, True

    # 실시간 실패 → OSRM 표준 경로 대체
    coords = sources + cand_pts
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in coords])
    src_str = ";".join(map(str, range(len(sources))))
    dst_str = ";".join(map(str, range(len(sources), len(coords))))
    data = osrm_request(f"/table/v1/driving/{coord_str}?sources={src_str}&destinations={dst_str}")
    durations = data.get("durations", [])
    if durations:
        osrm_mat = [[(durations[s][d] / 60.0 if durations[s][d] is not None else None)
                     for d in range(len(cand_pts))] for s in range(len(sources))]
        idx, times = pick(osrm_mat)
        if idx is not None:
            return cand_hubs[idx], times, False

    # 전부 실패 → 직선거리 1순위 거점, 시간 미상
    return cand_hubs[0], None, False


def refine_center_by_density(hub_lat, hub_lon, count_radius_m=500, step_km=0.4):
    """장거리 2단계: 거점역 주변 3x3 지점의 음식점 수(상권 밀집도)를 비교해
    가장 밀집한 지점으로 중심을 미세 조정. (동률이면 역 위치 유지)
    반환: (중심 lat, 중심 lon, 선정 지점 매장 수, 역 기준 매장 수)"""
    lat_off = step_km / 111.0
    lon_off = step_km / (111.0 * math.cos(math.radians(hub_lat)))
    cands = [(hub_lat + lat_off * i, hub_lon + lon_off * j)
             for i in (-1, 0, 1) for j in (-1, 0, 1)]
    counts = [0] * len(cands)
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(get_kakao_place_count, la, lo, count_radius_m): idx
                for idx, (la, lo) in enumerate(cands)}
        for fut, idx in futs.items():
            try:
                counts[idx] = fut.result()
            except Exception:
                counts[idx] = 0
    hub_idx = 4  # (0, 0) 위치 = 역 자체
    best_idx = hub_idx
    for idx, c in enumerate(counts):
        if c > counts[best_idx]:
            best_idx = idx
    return cands[best_idx][0], cands[best_idx][1], counts[best_idx], counts[hub_idx]


# =========================================================
# 2.6 공유 문서 생성 (PDF / HTML)
# =========================================================
_FONT_CANDIDATES = [
    "NanumGothic.ttf",                                        # 저장소 루트에 직접 포함한 경우
    "fonts/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",        # packages.txt: fonts-nanum
    "/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "C:/Windows/Fonts/malgun.ttf",                            # 로컬 윈도우 테스트용
]


def find_korean_font():
    """PDF에 임베드할 한글 TTF 폰트 경로 탐색. 없으면 None."""
    for p in _FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def build_share_pdf(meta, att_rows, rest_rows, font_path):
    """회식 추천 결과 PDF 바이트 생성 (fpdf2 + 한글 TTF 필요).
    meta: 산출 조건/중심 주소/링크 dict, att_rows: [(이름, 시간문구)],
    rest_rows: [{rank, name, cat, addr, url}]"""
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.add_font("KR", "", font_path)
    epw = pdf.epw

    def section(txt):
        pdf.ln(2)
        pdf.set_font("KR", size=12)
        pdf.set_text_color(20, 40, 90)
        pdf.cell(0, 8, txt)
        pdf.ln(9)
        pdf.set_text_color(30, 30, 30)

    def body(txt, size=10, h=6):
        pdf.set_font("KR", size=size)
        pdf.set_text_color(30, 30, 30)
        pdf.multi_cell(epw, h, txt)

    # 제목
    pdf.set_font("KR", size=16)
    pdf.set_text_color(20, 40, 90)
    pdf.cell(0, 10, "회식 장소 추천 결과")
    pdf.ln(11)
    pdf.set_font("KR", size=9)
    pdf.set_text_color(120, 120, 120)
    pdf.cell(0, 5, f"생성 {meta['generated']} · 행복한 퇴근 이후 v5")
    pdf.ln(9)

    section("1. 산출 조건")
    body(f"산출 모드: {meta['mode']}")
    body(f"산출 기준: {meta['criterion']} / 이동 수단: {meta['transport']} / 회식 예정 {meta['dinner_time']}")
    body(f"날씨 반영: {meta['weather']}")

    section("2. 추천 중심 위치")
    body(f"주소: {meta['center_addr']}")
    pdf.set_font("KR", size=10)
    pdf.set_text_color(0, 80, 200)
    pdf.cell(0, 6, "> 카카오맵에서 위치 열기", link=meta["kakao_link"])
    pdf.ln(8)
    pdf.set_text_color(30, 30, 30)

    section("3. 참석자별 예상 이동시간")
    pdf.set_font("KR", size=10)
    name_w, t_w = epw * 0.6, epw * 0.4
    pdf.set_fill_color(235, 240, 250)
    pdf.cell(name_w, 7, "참석자", border=1, fill=True)
    pdf.cell(t_w, 7, "예상 소요시간", border=1, fill=True)
    pdf.ln(7)
    for name, t in att_rows:
        pdf.cell(name_w, 7, str(name)[:24], border=1)
        pdf.cell(t_w, 7, str(t), border=1)
        pdf.ln(7)

    section(f"4. 추천 맛집 TOP {len(rest_rows)} - {meta['category']}")
    if rest_rows:
        for r in rest_rows:
            pdf.set_font("KR", size=11)
            pdf.set_text_color(30, 30, 30)
            pdf.multi_cell(epw, 6, f"{r['rank']}위  {r['name']}  ({r['cat']})")
            pdf.set_font("KR", size=9)
            pdf.set_text_color(90, 90, 90)
            pdf.multi_cell(epw, 5, f"주소: {r['addr']}")
            if r.get("url"):
                pdf.set_text_color(0, 80, 200)
                pdf.cell(0, 5, "> 카카오맵 상세 보기", link=r["url"])
                pdf.ln(6)
            pdf.ln(1)
    else:
        body("해당 종류의 매장을 찾지 못했습니다.")

    if meta.get("notice"):
        pdf.ln(2)
        pdf.set_font("KR", size=9)
        pdf.set_text_color(160, 80, 0)
        pdf.multi_cell(epw, 5, f"※ {meta['notice']}")

    pdf.ln(3)
    pdf.set_font("KR", size=8)
    pdf.set_text_color(150, 150, 150)
    pdf.multi_cell(epw, 4, "소요시간은 산출 시점의 실시간 교통(카카오내비) 기준 추정치이며 실제와 다를 수 있습니다.")
    return bytes(pdf.output())


def build_share_html(meta, att_rows, rest_rows):
    """회식 추천 결과 HTML 바이트 생성 (폰트 임베드 불필요, 항상 동작)"""
    esc = html.escape
    att_html = "".join(f"<tr><td>{esc(str(n))}</td><td>{esc(str(t))}</td></tr>"
                       for n, t in att_rows)
    if rest_rows:
        items = []
        for r in rest_rows:
            link = (f"<a href='{esc(r['url'])}' target='_blank'>카카오맵 상세 보기</a>"
                    if r.get("url") else "")
            items.append(
                f"<li><div class='rname'>{r['rank']}위 · {esc(r['name'])} "
                f"<span class='rcat'>{esc(r['cat'])}</span></div>"
                f"<div class='raddr'>{esc(r['addr'])}</div>{link}</li>")
        rest_html = "".join(items)
    else:
        rest_html = "<li>해당 종류의 매장을 찾지 못했습니다.</li>"
    notice_html = (f"<p class='notice'>※ {esc(meta['notice'])}</p>"
                   if meta.get("notice") else "")
    page = f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>회식 장소 추천 결과</title>
<style>
 body{{font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif;max-width:640px;
      margin:0 auto;padding:20px;color:#222;line-height:1.55}}
 h1{{font-size:22px;color:#14285a;margin-bottom:4px}}
 .sub{{color:#888;font-size:12px;margin-bottom:18px}}
 h2{{font-size:15px;color:#14285a;border-left:4px solid #14285a;padding-left:8px;
    margin:22px 0 8px}}
 table{{width:100%;border-collapse:collapse;font-size:13px}}
 td,th{{border:1px solid #ccd;padding:6px 8px;text-align:left}}
 th{{background:#eef2fa}}
 ul{{list-style:none;padding:0}}
 li{{border:1px solid #e2e6f0;border-radius:8px;padding:10px 12px;margin-bottom:8px}}
 .rname{{font-weight:bold}} .rcat{{color:#888;font-weight:normal;font-size:12px}}
 .raddr{{color:#666;font-size:12px;margin:2px 0}}
 a{{color:#0050c8;font-size:12px}}
 .meta p{{margin:2px 0;font-size:13px}}
 .notice{{color:#a05000;font-size:12px}}
 .foot{{color:#999;font-size:11px;margin-top:20px}}
</style></head><body>
<h1>회식 장소 추천 결과</h1>
<div class="sub">생성 {esc(meta['generated'])} · 행복한 퇴근 이후 v5</div>
<h2>산출 조건</h2>
<div class="meta">
 <p>산출 모드: {esc(meta['mode'])}</p>
 <p>산출 기준: {esc(meta['criterion'])} / 이동 수단: {esc(meta['transport'])} / 회식 예정 {esc(meta['dinner_time'])}</p>
 <p>날씨 반영: {esc(meta['weather'])}</p>
</div>
<h2>추천 중심 위치</h2>
<div class="meta">
 <p>{esc(meta['center_addr'])}</p>
 <p><a href="{esc(meta['kakao_link'])}" target="_blank">카카오맵에서 위치 열기</a></p>
</div>
<h2>참석자별 예상 이동시간</h2>
<table><tr><th>참석자</th><th>예상 소요시간</th></tr>{att_html}</table>
<h2>추천 맛집 TOP {len(rest_rows)} — {esc(meta['category'])}</h2>
<ul>{rest_html}</ul>
{notice_html}
<div class="foot">소요시간은 산출 시점 실시간 교통(카카오내비) 기준 추정치이며 실제와 다를 수 있습니다.</div>
</body></html>"""
    return page.encode("utf-8")


# =========================================================
# 3. 공용 UI: 주소 선택기 (검색 + 즐겨찾기)
# =========================================================
def address_picker(title, key, default_query=""):
    """검색과 즐겨찾기를 통합한 주소 선택 UI. 선택된 주소 doc(dict) 또는 None 반환."""
    fav_names = [f["label"] for f in st.session_state.fav_addresses]
    c1, c2 = st.columns([2, 1])
    q = c1.text_input(f"{title} 검색어", default_query, key=f"{key}_q")
    fav_sel = c2.selectbox("⭐ 즐겨찾기", ["직접 검색"] + fav_names, key=f"{key}_fav")

    if fav_sel != "직접 검색":
        fav = next((x for x in st.session_state.fav_addresses if x["label"] == fav_sel), None)
        if fav:
            st.caption(f"⭐ 선택됨: {fav['label']}")
            return {"address_name": fav["label"], "x": fav["x"], "y": fav["y"]}

    if st.button(f"{title} 검색", key=f"{key}_btn", use_container_width=True):
        st.session_state[f"{key}_res"] = search_kakao_address(q)

    res = st.session_state.get(f"{key}_res", [])
    opts = [doc["address_name"] for doc in res]
    if opts:
        sel = st.selectbox(f"{title} 선택", opts, key=f"{key}_sel")
        doc = res[opts.index(sel)]
        if st.button("⭐ 즐겨찾기 추가", key=f"{key}_addfav"):
            if not any(f["label"] == doc["address_name"] for f in st.session_state.fav_addresses):
                st.session_state.fav_addresses.append(
                    {"label": doc["address_name"], "x": doc["x"], "y": doc["y"]})
                save_favorites(st.session_state.fav_addresses)
                st.success("즐겨찾기에 추가되었습니다.")
        return doc

    st.caption("검색어 입력 후 검색 버튼을 눌러 주소를 선택하세요.")
    return None


# =========================================================
# 4. 사이드바: 즐겨찾기 관리 / 오류 기록
# =========================================================
with st.sidebar:
    st.markdown("### ⭐ 즐겨찾기 주소")
    if st.session_state.fav_addresses:
        for i, f in enumerate(list(st.session_state.fav_addresses)):
            fc1, fc2 = st.columns([4, 1])
            fc1.caption(f["label"])
            if fc2.button("🗑", key=f"fav_del_{i}"):
                st.session_state.fav_addresses.pop(i)
                save_favorites(st.session_state.fav_addresses)
                st.rerun()
    else:
        st.caption("저장된 주소가 없습니다. 주소 선택 후 ⭐ 버튼으로 추가하세요.")

    st.markdown("### 🧾 오류 기록")
    if st.session_state.error_log:
        with st.expander(f"최근 오류 {len(st.session_state.error_log)}건"):
            for line in reversed(st.session_state.error_log):
                st.caption(line)
    else:
        st.caption("기록된 오류가 없습니다.")


# =========================================================
# 5. 상단 탭 구성
# =========================================================
st.title("🌆 행복한 퇴근 이후")
tab1, tab2, tab3 = st.tabs(["🚗 퇴근시간 최적화", "🍻 회식장소 추천", "💬 출발 알리미"])


# ---------------------------------------------------------
# 탭 1: 퇴근시간 최적화 AI
# ---------------------------------------------------------
with tab1:
    st.subheader("퇴근시간 최적화 AI")
    st.caption("실제 대안경로와 출발지 날씨 보정을 반영해 10분 구간별 최적 출발시간을 추천합니다.")

    col1, col2 = st.columns(2)
    with col1:
        start_doc = address_picker("출발지", "m1s", "탕정 삼성로")
    with col2:
        end_doc = address_picker("목적지", "m1e", "천안 성황로")

    c1, c2 = st.columns(2)
    탐색_시작 = c1.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time(), key="m1_time_start")
    탐색_종료 = c2.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time(), key="m1_time_end")

    rc1, rc2 = st.columns([3, 1])
    run1 = rc1.button("🔍 최적 출발시간 스캔", type="primary", use_container_width=True, key="m1_run_btn")
    if rc2.button("🔄 초기화", use_container_width=True, key="m1_reset"):
        st.session_state.commute_data = None
        st.rerun()

    if run1:
        if not start_doc or not end_doc:
            st.error("출발지와 목적지를 선택해 주세요.")
        else:
            step = st.empty()
            try:
                o_lat, o_lon = float(start_doc["y"]), float(start_doc["x"])
                d_lat, d_lon = float(end_doc["y"]), float(end_doc["x"])

                step.info("① 카카오내비 실제 경로(대안 포함) 조회 중...")
                routes = get_kakao_routes(o_lat, o_lon, d_lat, d_lon, alternatives=True)

                step.info("② 출발지 기준 실시간 날씨 조회 중...")
                w_desc, temp_val = get_realtime_weather_and_temp(round(o_lat, 3), round(o_lon, 3))
                w_mult = weather_multiplier(w_desc)

                if routes:
                    r_a = routes[0]
                    if len(routes) >= 2:
                        r_b = routes[1]
                        b_real = True
                        b_name = "경로B (실제 대안경로)"
                    else:
                        mid = [(o_lat + d_lat) / 2 + 0.01, (o_lon + d_lon) / 2 - 0.01]
                        r_b = {"dist": round(r_a["dist"] * 1.15, 1),
                               "dur": round(r_a["dur"] * 1.15, 1),
                               "path": get_real_road_path([[o_lat, o_lon], mid, [d_lat, d_lon]])}
                        b_real = False
                        b_name = "경로B (우회 추정)"

                    step.info("③ 시간대별 혼잡도 시뮬레이션 중...")
                    today = now_kst()
                    c_hour = today.hour + today.minute / 60.0
                    c_peak = peak_val(c_hour)
                    base_A = r_a["dur"] / (1.0 + c_peak * 1.2)
                    base_B = r_b["dur"] / (1.0 + c_peak * 0.5)

                    start_dt = datetime(today.year, today.month, today.day, 탐색_시작.hour, 탐색_시작.minute)
                    end_dt = datetime(today.year, today.month, today.day, 탐색_종료.hour, 탐색_종료.minute)

                    options = []
                    current = start_dt
                    random.seed(int(start_dt.timestamp()))
                    while current <= end_dt:
                        h = current.hour + current.minute / 60.0
                        pk = peak_val(h)
                        noise = random.uniform(-1.0, 1.5)
                        dur_A = (base_A * (1.0 + pk * 1.2) + noise) * w_mult
                        dur_B = (base_B * (1.0 + pk * 0.5) + noise * 0.5) * w_mult
                        diff_m = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
                        penalty = (diff_m ** 1.2) * 0.05
                        options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current,
                                        "route_name": "경로A (카카오 최적)", "distance_km": r_a["dist"],
                                        "duration_min": round(dur_A, 1), "score": dur_A + penalty})
                        options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current,
                                        "route_name": b_name, "distance_km": r_b["dist"],
                                        "duration_min": round(dur_B, 1), "score": dur_B + penalty})
                        current += timedelta(minutes=1)

                    step.info("④ 결과 정리 및 차트 생성 중...")
                    df_all = pd.DataFrame(options)
                    df_all["10분구간"] = df_all["dt_obj"].apply(
                        lambda x: f"{x.replace(minute=(x.minute // 10) * 10).strftime('%H:%M')}~"
                                  f"{(x.replace(minute=(x.minute // 10) * 10) + timedelta(minutes=9)).strftime('%H:%M')}")
                    summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()

                    chart_df = (df_all.groupby(["10분구간", "route_name"])["duration_min"]
                                .min().unstack())

                    best_row = summary.loc[summary["score"].idxmin()]

                    disp = summary[["10분구간", "departure_time", "route_name", "duration_min", "distance_km"]].copy()
                    disp.columns = ["10분구간", "최적 출발시간", "최고의 경로", "소요시간(분)", "거리(km)"]
                    disp["날씨"] = w_desc
                    disp["온도"] = f"{temp_val} °C"

                    st.session_state.commute_data = {
                        "df": disp, "chart": chart_df,
                        "o_lat": o_lat, "o_lon": o_lon, "d_lat": d_lat, "d_lon": d_lon,
                        "path_a": r_a["path"], "path_b": r_b["path"], "b_real": b_real,
                        "weather": w_desc, "w_mult": w_mult,
                        "best_departure": best_row["departure_time"],
                        "best_dur": int(round(best_row["duration_min"])),
                        "start_addr": start_doc["address_name"],
                        "end_addr": end_doc["address_name"],
                    }
                    step.success("✅ 완료. 아래 결과를 확인하세요.")
                else:
                    step.empty()
                    st.error("카카오 길찾기 서버 통신에 실패했습니다. 잠시 후 다시 시도해 주세요.")
            except Exception as e:
                log_error("퇴근 스캔", e)
                step.empty()
                st.error(f"데이터 처리 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.commute_data:
        c = st.session_state.commute_data
        st.markdown("#### ⏰ 최적 연산 결과")
        st.dataframe(c["df"], use_container_width=True, hide_index=True)
        if c.get("w_mult", 1.0) > 1.0:
            st.caption(f"현재 날씨({c['weather']})를 반영해 소요시간을 {int((c['w_mult'] - 1) * 100)}% 상향 보정했습니다.")

        st.markdown("#### 📈 시간대별 예상 소요시간 추이")
        st.line_chart(c["chart"])

        sel = st.selectbox("🗺️ 지도에 표시할 구간 선택", c["df"]["10분구간"].tolist(), key="m1_map_select")
        try:
            target = c["df"][c["df"]["10분구간"] == sel].iloc[0]["최고의 경로"]
            st.markdown(f"**선택 경로: {target}**")
            m = folium.Map(location=[(c["o_lat"] + c["d_lat"]) / 2, (c["o_lon"] + c["d_lon"]) / 2],
                           zoom_start=12, tiles="CartoDB positron")
            folium.Marker([c["o_lat"], c["o_lon"]], popup="출발지", icon=folium.Icon(color="blue")).add_to(m)
            folium.Marker([c["d_lat"], c["d_lon"]], popup="목적지", icon=folium.Icon(color="red")).add_to(m)
            path = c["path_a"] if "경로A" in target else c["path_b"]
            color = "#dc3545" if "경로A" in target else "#28a745"
            if path:
                folium.PolyLine(locations=path, color=color, weight=6).add_to(m)
            st_folium(m, use_container_width=True, height=350, key="map_commute", returned_objects=[])
            if "경로B" in target and not c.get("b_real"):
                st.caption("이 구간의 경로B는 카카오가 대안경로를 제공하지 않아 추정 경로로 표시됩니다.")
        except Exception as e:
            log_error("퇴근 지도", e)
            st.caption("지도를 준비하는 중입니다.")


# ---------------------------------------------------------
# 탭 2: 회식장소 최적위치 산출기
# ---------------------------------------------------------
with tab2:
    st.subheader("회식장소 추천기")
    st.caption("참석자 주소를 입력하면 실시간 소요시간 2단계 탐색으로 중심 위치를 찾고, 음식 종류별 TOP 5를 한 번에 수집합니다.")

    bc1, bc2, bc3 = st.columns([1, 1, 2])
    if bc1.button("➕ 인원 추가", key="m2_add"):
        st.session_state.num_people += 1
    if bc2.button("➖ 인원 감소", key="m2_sub") and st.session_state.num_people > 2:
        st.session_state.num_people -= 1
    search_radius = bc3.slider("탐색 반경(m)", 100, 2000, 500, 100, key="m2_radius")

    criterion = st.radio(
        "📐 중심 산출 기준",
        ["공평 우선 (가장 오래 걸리는 사람의 시간 최소화)", "효율 우선 (전체 소요시간 합 최소화)"],
        key="m2_criterion",
        horizontal=True,
    )

    mode_choice = st.radio(
        "🧭 위치 산출 모드",
        ["자동", "일반(격자) 고정", "장거리(거점역) 고정"],
        key="m2_mode",
        horizontal=True,
    )
    st.caption(f"자동: 참석자 간 최대 직선 이격이 {_LONG_DIST_KM}km 이상이면 전국 KTX/SRT 거점역 기반 장거리 모드로 전환합니다. "
               "같은 도시·인접 도시 모임은 일반(격자) 탐색으로 계산됩니다.")

    tc1, tc2 = st.columns(2)
    transport = tc1.radio("🚗 이동 수단", ["자동차", "대중교통 (간이 추정)"], key="m2_transport", horizontal=True)
    dinner_time = tc2.time_input("🕖 회식 예정 시각", datetime.strptime("19:00", "%H:%M").time(), key="m2_dinner")

    addresses = []
    for i in range(st.session_state.num_people):
        st.markdown(f"**참석자 {i + 1}**")
        p_name = st.text_input("이름", value=f"참석자 {i + 1}", key=f"m2_name_{i}")
        p_doc = address_picker("주소", f"m2p{i}", "")
        if p_doc:
            addresses.append({"name": p_name.strip() or f"참석자 {i + 1}", "doc": p_doc})
        st.markdown("---")

    rc1, rc2 = st.columns([3, 1])
    run2 = rc1.button("🔍 최적 위치 및 맛집 산출", type="primary", use_container_width=True, key="m2_run")
    if rc2.button("🔄 초기화", use_container_width=True, key="m2_reset"):
        st.session_state.dinner_data = None
        st.rerun()

    if run2:
        if len(addresses) < st.session_state.num_people:
            st.error("모든 참석자의 주소를 검색·선택해야 계산할 수 있습니다.")
        else:
            step = st.empty()
            try:
                locs = [{"name": p["name"], "lat": float(p["doc"]["y"]), "lon": float(p["doc"]["x"])}
                        for p in addresses]
                sources = [(l["lat"], l["lon"]) for l in locs]
                fair_mode = criterion.startswith("공평")
                spread = max_spread_km(sources)

                if mode_choice.startswith("장거리"):
                    long_mode = True
                elif mode_choice.startswith("일반"):
                    long_mode = False
                else:
                    long_mode = spread >= _LONG_DIST_KM

                if long_mode:
                    # ---------- 장거리 모드: 거점 도시(역) 선정 → 상권 분석 ----------
                    step.info(f"① 장거리 모드 (최대 이격 {spread:.0f}km). 전국 거점역 후보 평가 중...")
                    hub, hub_times, realtime = select_best_hub(sources, fair_mode)

                    step.info(f"② 거점 확정: {hub['city']} {hub['name']}. 역 주변 상권 밀집 지점 분석 중...")
                    c_lat, c_lon, best_cnt, hub_cnt = refine_center_by_density(
                        hub["lat"], hub["lon"], count_radius_m=max(int(search_radius), 400))

                    step.info("③ 참석자별 실제 경로·소요시간 수집 중 (자동차 기준)...")
                    best_times, route_paths = [], []
                    for s_idx, (s_lat, s_lon) in enumerate(sources):
                        _, dur, path = get_kakao_navi_baseline(s_lat, s_lon, c_lat, c_lon)
                        if dur is None and hub_times:
                            dur = hub_times[s_idx]
                        best_times.append(dur)
                        if path:
                            route_paths.append(thin_path(path))
                        else:
                            route_paths.append(thin_path(
                                get_real_road_path([[s_lat, s_lon], [c_lat, c_lon]])))

                    step.info("④ 음식 종류별 상권 수집 중 (광역 재탐색 포함)...")
                    rests_by_cat, adjusted_by_cat = fetch_all_category_restaurants(
                        c_lat, c_lon, search_radius)
                    # 장거리에서는 종류별 재조정 지점의 시간/경로 재계산을 생략 (근사 표시)

                    t_now = now_kst()
                    w_desc2, _ = get_realtime_weather_and_temp(round(c_lat, 3), round(c_lon, 3))
                    w_mult2 = weather_multiplier(w_desc2)

                    st.session_state.dinner_data = {
                        "locs": locs, "b_lat": c_lat, "b_lon": c_lon,
                        "paths": route_paths, "best_times": best_times,
                        "rests_by_cat": rests_by_cat, "adjusted_by_cat": adjusted_by_cat,
                        "radius": search_radius, "realtime": realtime,
                        "criterion": "공평 우선" if fair_mode else "효율 우선",
                        "calc_time": t_now.strftime("%H:%M"),
                        "time_factor": 1.0,
                        "dinner_label": dinner_time.strftime("%H:%M"),
                        "weather": w_desc2, "w_mult": w_mult2,
                        "transit": transport.startswith("대중교통"),
                        "spread_km": round(spread, 1), "off_km": 0.0,
                        "mode": "long",
                        "hub_city": hub["city"], "hub_name": hub["name"],
                        "hub_lat": hub["lat"], "hub_lon": hub["lon"],
                        "density_best": best_cnt, "density_hub": hub_cnt,
                    }
                    step.success("✅ 완료 (장거리 모드). 아래 결과를 확인하세요.")
                else:
                    # ---------- 일반 모드: 기존 2단계 격자 탐색 ----------
                    avg_lat = sum(l["lat"] for l in locs) / len(locs)
                    avg_lon = sum(l["lon"] for l in locs) / len(locs)

                    # 참석자 분포에 따라 후보 간격 자동 조절
                    off_km = min(max(spread / 2.0, 1.5), 6.0)

                    def pick_best(mat, cands):
                        b_idx, b_score, b_times = None, float("inf"), []
                        for d_idx in range(len(cands)):
                            ts = [mat[s][d_idx] for s in range(len(sources))]
                            if any(t is None for t in ts):
                                continue
                            score = max(ts) if fair_mode else sum(ts)
                            if score < b_score:
                                b_idx, b_score, b_times = d_idx, score, ts
                        return b_idx, b_times

                    step.info(f"① 1단계 광역 탐색 중 (후보 간격 {off_km:.1f}km, 실시간 소요시간)...")
                    cand1 = make_grid(avg_lat, avg_lon, off_km)
                    mat1 = build_realtime_matrix(sources, cand1)
                    idx1, times1 = pick_best(mat1, cand1)
                    realtime = idx1 is not None

                    if idx1 is None:
                        step.info("① 카카오 실시간 실패, OSRM 표준 경로로 대체 탐색 중...")
                        coords = sources + cand1
                        coord_str = ";".join([f"{lon},{lat}" for lat, lon in coords])
                        src_str = ";".join(map(str, range(len(sources))))
                        dst_str = ";".join(map(str, range(len(sources), len(coords))))
                        data = osrm_request(f"/table/v1/driving/{coord_str}?sources={src_str}&destinations={dst_str}")
                        durations = data.get("durations", [])
                        if durations:
                            osrm_mat = [[(durations[s][d] / 60.0 if durations[s][d] is not None else None)
                                         for d in range(len(cand1))] for s in range(len(sources))]
                            idx1, times1 = pick_best(osrm_mat, cand1)

                    if idx1 is None:
                        step.empty()
                        st.error("경로 소요시간 계산에 실패했습니다. 잠시 후 다시 시도해 주세요.")
                    else:
                        b_lat, b_lon = cand1[idx1]
                        best_times = times1

                        # 2단계 정밀 탐색 (실시간 성공 시에만)
                        if realtime:
                            step.info("② 2단계 정밀 탐색 중 (최적 후보 주변 재탐색)...")
                            cand2 = make_grid(b_lat, b_lon, max(off_km / 3.0, 0.5))
                            mat2 = build_realtime_matrix(sources, cand2)
                            idx2, times2 = pick_best(mat2, cand2)
                            if idx2 is not None:
                                s1 = max(times1) if fair_mode else sum(times1)
                                s2 = max(times2) if fair_mode else sum(times2)
                                if s2 <= s1:
                                    b_lat, b_lon = cand2[idx2]
                                    best_times = times2

                        step.info("③ 음식 종류별 상권 수집 중 (광역 재탐색 포함)...")
                        rests_by_cat, adjusted_by_cat = fetch_all_category_restaurants(b_lat, b_lon, search_radius)

                        # 재조정된 종류는 밀집 중심까지의 시간/경로 추가 계산
                        for a_label, a_info in adjusted_by_cat.items():
                            a_lat, a_lon = a_info["center"]
                            a_times, a_paths = [], []
                            for (s_lat, s_lon) in sources:
                                _, a_dur, a_path = get_kakao_navi_baseline(s_lat, s_lon, a_lat, a_lon)
                                a_times.append(a_dur)
                                a_paths.append(a_path if a_path else None)
                            a_info["times"] = a_times
                            a_info["paths"] = a_paths

                        step.info("④ 참석자별 실제 이동 경로 수집 중...")
                        route_paths = [get_route_path(s_lat, s_lon, b_lat, b_lon)
                                       for (s_lat, s_lon) in sources]

                        # 회식 시각 혼잡도 보정 계수 (표시용 추정치)
                        t_now = now_kst()
                        now_h = t_now.hour + t_now.minute / 60.0
                        din_h = dinner_time.hour + dinner_time.minute / 60.0
                        t_factor = (1.0 + peak_val(din_h) * 1.2) / (1.0 + peak_val(now_h) * 1.2)
                        t_factor = min(max(t_factor, 0.7), 1.6)

                        # 중심 좌표 기준 날씨 보정
                        w_desc2, _ = get_realtime_weather_and_temp(round(b_lat, 3), round(b_lon, 3))
                        w_mult2 = weather_multiplier(w_desc2)

                        st.session_state.dinner_data = {
                            "locs": locs, "b_lat": b_lat, "b_lon": b_lon,
                            "paths": route_paths,
                            "best_times": best_times, "rests_by_cat": rests_by_cat,
                            "adjusted_by_cat": adjusted_by_cat,
                            "radius": search_radius,
                            "realtime": realtime,
                            "criterion": "공평 우선" if fair_mode else "효율 우선",
                            "calc_time": t_now.strftime("%H:%M"),
                            "time_factor": t_factor,
                            "dinner_label": dinner_time.strftime("%H:%M"),
                            "weather": w_desc2, "w_mult": w_mult2,
                            "transit": transport.startswith("대중교통"),
                            "spread_km": round(spread, 1), "off_km": round(off_km, 1),
                            "mode": "normal",
                        }
                        step.success("✅ 완료. 아래 결과를 확인하세요.")
            except Exception as e:
                log_error("회식 산출", e)
                step.empty()
                st.error(f"연산 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.dinner_data:
        d = st.session_state.dinner_data
        st.markdown("#### 🗺️ 위치 분석 결과")

        if d.get("mode") == "long":
            density_note = ""
            if d.get("density_best", 0) > d.get("density_hub", 0):
                density_note = (f" 역 주변 상권 분석 결과, 음식점이 더 밀집한 지점"
                                f"(반경 내 약 {d['density_best']}곳)으로 중심을 조정했습니다.")
            st.info(f"🚄 장거리 모드: 참석자 최대 이격 {d.get('spread_km', '?')}km. "
                    f"교통 거점으로 **{d.get('hub_city', '')} {d.get('hub_name', '')}** 일대를 선정했습니다."
                    + density_note)

        def disp_time(mins):
            """저장된 자동차 실시간 소요시간을 표시용 값으로 변환 (회식시각/날씨/이동수단 보정)"""
            if mins is None:
                return None
            t = mins * d.get("time_factor", 1.0) * d.get("w_mult", 1.0)
            if d.get("transit") and d.get("mode") != "long":
                t = t * 1.6 + 12.0
            return t

        view_cat = st.selectbox("🍽️ 음식 종류 선택 (검색 후 자유롭게 전환)",
                                list(CATEGORY_OPTIONS.keys()), key="m2_view_category")
        sel_rests = d.get("rests_by_cat", {}).get(view_cat, [])

        # 선택 종류가 기본 반경 내에 없어 중심이 재조정된 경우, 해당 기준으로 표시
        adj = d.get("adjusted_by_cat", {}).get(view_cat)
        if adj:
            eff_lat, eff_lon = adj["center"]
            if adj.get("times"):
                eff_times = adj["times"]
                eff_paths = adj.get("paths", [])
                st.info(f"기본 반경 내에 {view_cat} 매장이 없어, 가장 가까운 {view_cat} 밀집 지역으로 중심을 재조정했습니다. 지도에는 기존 중심(빨간 별)과 재조정 중심(주황 별)이 함께 표시되며, 시간과 경로는 재조정 위치 기준입니다.")
            else:
                eff_times = d.get("best_times", [])
                eff_paths = d.get("paths", [])
                st.info(f"기본 반경 내에 {view_cat} 매장이 없어, 가장 가까운 {view_cat} 밀집 지역으로 중심을 재조정했습니다. 장거리 모드에서는 표시되는 시간·경로가 기존 중심 기준 근사치입니다.")
        else:
            eff_lat, eff_lon = d["b_lat"], d["b_lon"]
            eff_times = d.get("best_times", [])
            eff_paths = d.get("paths", [])

        if eff_times:
            cols = st.columns(len(d["locs"]))
            for idx, loc in enumerate(d["locs"]):
                t = disp_time(eff_times[idx] if idx < len(eff_times) else None)
                cols[idx].metric(loc["name"], f"{int(round(t))}분" if t is not None else "계산 실패")
            traffic_label = "카카오내비 실시간 기반" if d.get("realtime") else "표준 경로 기준 (실시간 미반영)"
            if d.get("mode") == "long":
                st.caption(f"{d.get('calc_time', '')} 실시간 계산, 자동차 기준, {traffic_label}, "
                           f"날씨({d.get('weather', '')}) 보정 x{d.get('w_mult', 1.0):.2f}, {d.get('criterion', '')} 산출 · "
                           f"장거리 모임은 KTX/SRT 등 대중교통 이용을 권장합니다 "
                           f"(장거리에는 대중교통 간이 추정과 회식시각 혼잡도 보정을 적용하지 않습니다).")
            else:
                mode_label = "대중교통 간이 추정" if d.get("transit") else "자동차"
                st.caption(f"{d.get('calc_time', '')} 실시간 계산, 회식 시각 {d.get('dinner_label', '')} 혼잡도 보정 x{d.get('time_factor', 1.0):.2f}, 날씨({d.get('weather', '')}) 보정 x{d.get('w_mult', 1.0):.2f}, {mode_label} 기준, {traffic_label}, {d.get('criterion', '')} 산출")

        try:
            m = folium.Map(location=[eff_lat, eff_lon], zoom_start=14, tiles="CartoDB positron")
            route_colors = ["#e74c3c", "#2980b9", "#27ae60", "#8e44ad", "#f39c12", "#16a085", "#d35400", "#2c3e50"]
            paths = eff_paths
            times = eff_times
            for idx, l in enumerate(d["locs"]):
                color = route_colors[idx % len(route_colors)]
                t_disp = disp_time(times[idx] if idx < len(times) else None)
                mins = int(round(t_disp)) if t_disp is not None else None
                label_txt = f"{l['name']} {mins}분" if mins is not None else l["name"]

                folium.Marker([l["lat"], l["lon"]], popup=l["name"], icon=folium.Icon(color="blue")).add_to(m)
                route = paths[idx] if idx < len(paths) and paths[idx] else None
                if route and len(route) > 2:
                    folium.PolyLine(route, color=color, weight=4, opacity=0.8,
                                    tooltip=label_txt).add_to(m)
                    mid = route[len(route) // 2]
                else:
                    folium.PolyLine([[l["lat"], l["lon"]], [eff_lat, eff_lon]],
                                    color="gray", weight=2, dash_array="5, 5",
                                    tooltip=f"{label_txt} (경로 미확보, 직선 표시)").add_to(m)
                    mid = [(l["lat"] + eff_lat) / 2, (l["lon"] + eff_lon) / 2]

                folium.Marker(
                    mid,
                    icon=folium.DivIcon(
                        icon_size=(0, 0),
                        html=(f'<div style="background:{color};color:#fff;padding:2px 8px;'
                              f'border-radius:10px;font-size:11px;font-weight:bold;'
                              f'white-space:nowrap;display:inline-block;'
                              f'box-shadow:0 1px 3px rgba(0,0,0,0.4);">{label_txt}</div>')
                    ),
                ).add_to(m)

            folium.Circle(location=[eff_lat, eff_lon], radius=int(d["radius"]),
                          color="#0052cc", fill=True, fill_color="#0052cc", fill_opacity=0.3, weight=2).add_to(m)
            center_label = f"{view_cat} 재조정 중심" if adj else "최적 중심점"
            folium.Marker([eff_lat, eff_lon], popup=center_label,
                          icon=folium.Icon(color="orange" if adj else "red", icon="star")).add_to(m)

            if adj:
                folium.Marker([d["b_lat"], d["b_lon"]], popup="기존 최적 중심점",
                              icon=folium.Icon(color="red", icon="star")).add_to(m)
                shift_km = approx_km(eff_lat, eff_lon, d["b_lat"], d["b_lon"])
                folium.PolyLine([[d["b_lat"], d["b_lon"]], [eff_lat, eff_lon]],
                                color="#555555", weight=2, dash_array="4, 6",
                                tooltip=f"중심 이동 거리 약 {shift_km:.1f}km").add_to(m)

            if d.get("mode") == "long":
                folium.Marker([d["hub_lat"], d["hub_lon"]],
                              popup=f"교통 거점: {d.get('hub_city', '')} {d.get('hub_name', '')}",
                              icon=folium.Icon(color="purple", icon="train", prefix="fa")).add_to(m)

            for r in sel_rests:
                folium.Marker([float(r["y"]), float(r["x"])], popup=r["place_name"],
                              icon=folium.Icon(color="green", icon="cutlery")).add_to(m)

            all_lats = [l["lat"] for l in d["locs"]] + [eff_lat]
            all_lons = [l["lon"] for l in d["locs"]] + [eff_lon]
            if adj:
                all_lats.append(d["b_lat"])
                all_lons.append(d["b_lon"])
            if d.get("mode") == "long":
                all_lats.append(d["hub_lat"])
                all_lons.append(d["hub_lon"])
            m.fit_bounds([[min(all_lats), min(all_lons)], [max(all_lats), max(all_lons)]])

            st_folium(m, use_container_width=True, height=350, key="map_dinner", returned_objects=[])

            if adj:
                st.markdown(f"#### 🍽️ {view_cat} TOP 5 (광역 재탐색 결과)")
            else:
                st.markdown(f"#### 🍽️ 반경 {d['radius']}m 내 {view_cat} TOP 5")
            if sel_rests:
                rest_list = []
                for idx, r in enumerate(sel_rests):
                    addr = r.get("road_address_name", "").strip() or r.get("address_name", "주소 누락")
                    rest_list.append({"순위": f"{idx + 1}위", "이름": r["place_name"],
                                      "종류": r.get("category_name", "").split(">")[-1].strip(),
                                      "주소": addr, "링크": r["place_url"]})
                st.dataframe(pd.DataFrame(rest_list),
                             column_config={"링크": st.column_config.LinkColumn("🔗 지도")},
                             hide_index=True, use_container_width=True)
            else:
                st.info(f"기본 반경과 광역 재탐색 모두에서 {view_cat} 매장을 찾지 못했습니다. 다른 종류를 선택해 보세요.")

            # 공유용 위치 정보 (문구/문서 공용)
            center_addr = coord_to_address(eff_lat, eff_lon) or f"좌표 {eff_lat:.5f}, {eff_lon:.5f}"
            kakao_link = ("https://map.kakao.com/link/map/"
                          + urllib.parse.quote("회식 추천 위치") + f",{eff_lat:.6f},{eff_lon:.6f}")

            # 공유 문구 자동 생성
            st.markdown("#### 📣 공유 문구")
            lines = [f"[회식 안내] {view_cat} 회식 장소 추천 결과입니다."]
            if d.get("mode") == "long":
                lines.append(f"거점: {d.get('hub_city', '')} {d.get('hub_name', '')} 일대")
            lines.append(f"추천 위치: {center_addr}")
            if sel_rests:
                top = sel_rests[0]
                t_addr = top.get("road_address_name", "").strip() or top.get("address_name", "")
                lines.append(f"추천 매장: {top['place_name']} ({t_addr})")
            for idx, loc in enumerate(d["locs"]):
                t = disp_time(eff_times[idx] if idx < len(eff_times) else None)
                if t is not None:
                    lines.append(f"{loc['name']}: 약 {int(round(t))}분")
            mode_label2 = "자동차" if d.get("mode") == "long" else ("대중교통 간이 추정" if d.get("transit") else "자동차")
            lines.append(f"기준: {d.get('dinner_label', '')} 출발 예상 ({mode_label2})")
            lines.append(f"위치 지도: {kakao_link}")
            share_msg = "\n".join(lines)
            st.code(share_msg, language="text")
            st.caption("오른쪽 위 복사 아이콘으로 복사해 단체방에 공유하세요.")

            # 결과 문서 저장 (PDF / HTML)
            st.markdown("#### 📄 문서로 공유 (PDF / HTML)")
            st.caption("현재 선택한 음식 종류 기준으로 문서를 만듭니다. 저장한 파일을 카카오톡·메일 등에 첨부해 공유하세요.")
            try:
                att_rows = []
                for idx, loc in enumerate(d["locs"]):
                    t = disp_time(eff_times[idx] if idx < len(eff_times) else None)
                    att_rows.append((loc["name"], f"약 {int(round(t))}분" if t is not None else "계산 실패"))
                rest_rows = []
                for r_idx, r in enumerate(sel_rests):
                    addr = r.get("road_address_name", "").strip() or r.get("address_name", "주소 정보 없음")
                    rest_rows.append({"rank": r_idx + 1, "name": r["place_name"],
                                      "cat": r.get("category_name", "").split(">")[-1].strip(),
                                      "addr": addr, "url": r.get("place_url", "")})
                meta = {
                    "generated": now_kst().strftime("%Y-%m-%d %H:%M"),
                    "mode": (f"장거리 (거점: {d.get('hub_city', '')} {d.get('hub_name', '')})"
                             if d.get("mode") == "long" else "일반 (근거리 격자 탐색)"),
                    "criterion": d.get("criterion", ""),
                    "transport": ("대중교통 간이 추정"
                                  if d.get("transit") and d.get("mode") != "long"
                                  else "자동차 (실시간)"),
                    "dinner_time": d.get("dinner_label", ""),
                    "weather": f"{d.get('weather', '')} (보정 x{d.get('w_mult', 1.0):.2f})",
                    "category": view_cat,
                    "center_addr": center_addr,
                    "kakao_link": kakao_link,
                    "notice": ("장거리 모임은 KTX/SRT 등 대중교통 이용을 권장합니다. 표기 시간은 자동차 실시간 기준입니다."
                               if d.get("mode") == "long" else ""),
                }
                fname_cat = (view_cat.replace("/", "·").replace(" ", "")
                             .replace("(", "").replace(")", ""))
                stamp = now_kst().strftime("%m%d_%H%M")

                html_bytes = build_share_html(meta, att_rows, rest_rows)
                dcol1, dcol2 = st.columns(2)
                font_path = find_korean_font()
                if _HAS_FPDF and font_path:
                    try:
                        pdf_bytes = build_share_pdf(meta, att_rows, rest_rows, font_path)
                        dcol1.download_button("📄 PDF 저장", data=pdf_bytes,
                                              file_name=f"회식추천_{fname_cat}_{stamp}.pdf",
                                              mime="application/pdf", use_container_width=True)
                    except Exception as e:
                        log_error("PDF 생성", e)
                        dcol1.warning("PDF 생성에 실패했습니다. HTML 저장을 이용해 주세요.")
                else:
                    missing = "fpdf2 미설치" if not _HAS_FPDF else "한글 폰트 미탑재"
                    dcol1.warning(f"PDF 생성 불가 ({missing}). requirements.txt에 fpdf2, "
                                  "packages.txt에 fonts-nanum을 추가하거나 저장소에 NanumGothic.ttf를 "
                                  "넣어 주세요. 우선 HTML 저장을 이용할 수 있습니다.")
                dcol2.download_button("🌐 HTML 저장", data=html_bytes,
                                      file_name=f"회식추천_{fname_cat}_{stamp}.html",
                                      mime="text/html", use_container_width=True)
            except Exception as e:
                log_error("공유 문서", e)
                st.caption("공유 문서를 준비하는 중 문제가 발생했습니다.")
        except Exception as e:
            log_error("회식 지도", e)
            st.caption("결과 화면을 준비하는 중입니다.")


# ---------------------------------------------------------
# 탭 3: 출발 알리미
# ---------------------------------------------------------
with tab3:
    st.subheader("출발 알리미")
    st.caption("도착 예정 시간을 계산해 문자 메시지를 만들어 줍니다. 발송 전 내용을 직접 수정할 수 있습니다.")

    col1, col2 = st.columns(2)
    with col1:
        m3_start_doc = address_picker("출발지", "m3s", "탕정 삼성로")
    with col2:
        m3_end_doc = address_picker("목적지", "m3e", "천안 성황로")

    contact_in = st.text_input("수신자 (이름 + 번호)", value=st.session_state.favorite_contact,
                               placeholder="예: 배우자 01012345678", key="m3_contact")
    if st.button("⭐ 수신자 즐겨찾기 등록", use_container_width=True, key="m3_fav"):
        st.session_state.favorite_contact = contact_in
        st.success("즐겨찾기로 등록되었습니다.")

    # 탭1 연동: 최적 출발시간 문구 포함 옵션
    use_link = False
    cd = st.session_state.commute_data
    if cd and cd.get("best_departure"):
        use_link = st.checkbox(
            f"🚦 퇴근 최적 출발시간 포함 ({cd['best_departure']} 출발, 약 {cd['best_dur']}분 예상)",
            value=True, key="m3_link")

    rc1, rc2 = st.columns([3, 1])
    run3 = rc1.button("✅ 도착 예정시간 계산", type="primary", use_container_width=True, key="m3_prepare")
    if rc2.button("🔄 초기화", use_container_width=True, key="m3_reset"):
        st.session_state.notify_data = None
        st.session_state.pop("m3_edit", None)
        st.rerun()

    if run3:
        if not m3_start_doc or not m3_end_doc:
            st.error("출발지와 목적지를 선택해 주세요.")
        elif not contact_in.strip():
            st.error("수신자 연락처를 입력해 주세요.")
        else:
            with st.spinner("교통 정보 분석 중..."):
                try:
                    o_lat, o_lon = float(m3_start_doc["y"]), float(m3_start_doc["x"])
                    d_lat, d_lon = float(m3_end_doc["y"]), float(m3_end_doc["x"])

                    _, dur, _ = get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon)
                    if dur:
                        eta = (now_kst() + timedelta(minutes=dur)).strftime("%H시 %M분")
                        target_name = contact_in.split(" ")[0] if " " in contact_in else contact_in
                        msg_lines = [f"[{target_name}님 출발 알림]", "지금 퇴근 후 출발합니다.", ""]
                        msg_lines.append(f"📍 출발: {m3_start_doc['address_name']}")
                        msg_lines.append(f"🚩 도착: {m3_end_doc['address_name']}")
                        msg_lines.append("")
                        msg_lines.append(f"🚗 도착 예정: {eta}")
                        msg_lines.append(f"(실시간 교통 기준 약 {int(dur)}분 소요 예상)")
                        if use_link and cd:
                            msg_lines.append(f"🚦 오늘의 추천 출발시간: {cd['best_departure']} (약 {cd['best_dur']}분)")
                        final_msg = "\n".join(msg_lines)
                        phone = "".join(filter(str.isdigit, contact_in))
                        st.session_state.notify_data = {"ready": True, "phone": phone}
                        st.session_state["m3_edit"] = final_msg
                    else:
                        st.error("교통 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
                except Exception as e:
                    log_error("출발 알리미", e)
                    st.error(f"연산 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.notify_data and st.session_state.notify_data.get("ready"):
        n = st.session_state.notify_data

        st.markdown("#### ✏️ 메시지 편집 (자유롭게 수정하세요)")
        edited = st.text_area("메시지 내용", key="m3_edit", height=200, label_visibility="collapsed")

        st.markdown("#### 📋 복사용 최종 내용")
        st.code(edited, language="text")
        st.caption("⬆️ 위 박스 오른쪽 위 복사 아이콘을 누르면 메시지 전체가 복사됩니다.")

        if n["phone"]:
            st.markdown("**받는 번호** (탭하여 복사)")
            st.code(n["phone"], language="text")

        st.markdown("---")
        st.markdown(
            "##### 📱 보내는 방법\n"
            "1. 위 **메시지 박스의 복사 아이콘**을 눌러 내용을 복사합니다.\n"
            "2. 휴대폰 **문자 앱**을 열고 받는 사람을 지정합니다.\n"
            "3. 입력창을 길게 눌러 **붙여넣기** 후 전송합니다."
        )

        sms_url = f"sms:{n['phone']}?body={urllib.parse.quote(edited)}"
        st.link_button("💬 문자 앱 바로 열기 (지원되는 기기만)", sms_url, use_container_width=True)
        st.caption(
            "※ 갤럭시 등 일부 휴대폰은 보안 정책상 웹에서 문자 앱이 자동으로 열리지 않습니다. "
            "이 경우 위의 복사 → 붙여넣기 방법을 이용해 주세요."
        )
