"""
=========================================================
행복한 퇴근 이후 - 통합 앱 v4 (Streamlit / GitHub 배포 버전)
  탭1) 퇴근시간 최적화 AI
  탭2) 회식장소 최적위치 산출기
  탭3) 출발 알리미

[v4 개선 사항]
그룹1 빠른 개선
  - 탭1 시간대별 소요시간 라인 차트
  - 날씨를 출발지/중심지 좌표 기준으로 동적 조회
  - 참석자 이름 입력 및 지도 라벨 반영
  - 연산 진행 단계 실시간 표시
  - 각 탭 결과 초기화 버튼
그룹2 정확도 개선
  - 경로B를 카카오내비 실제 대안경로로 교체 (미제공 시 추정 표기)
  - 비/눈 날씨 보정 계수 적용
  - 회식 중심 2단계 탐색 (광역 후 정밀 재탐색)
  - 참석자 분포에 따른 후보 간격 자동 조절
  - 회식 예정 시각 혼잡도 보정 (추정 계수, 표시용)
그룹3 편의 기능
  - 탭1 최적 출발시간을 탭3 문자에 자동 연동
  - 탭2 결과 공유 문구 자동 생성
  - 탭3 메시지 발송 전 직접 편집
  - 주소 즐겨찾기 (파일 저장, 클라우드 재시작 시 초기화될 수 있음)
  - 이동 수단 선택 (자동차 / 대중교통 간이 추정)
그룹4 보안/유지보수
  - 카카오 키를 Secrets 우선 조회, 없으면 내장 키 사용
  - 오류 발생 이력을 사이드바에서 확인 가능
  * 파일 분리는 배포 편의를 위해 보류 (단일 파일 유지)
=========================================================
"""

import json
import math
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

                avg_lat = sum(l["lat"] for l in locs) / len(locs)
                avg_lon = sum(l["lon"] for l in locs) / len(locs)

                # 참석자 분포에 따라 후보 간격 자동 조절
                spread = max_spread_km(sources)
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
                    }
                    step.success("✅ 완료. 아래 결과를 확인하세요.")
            except Exception as e:
                log_error("회식 산출", e)
                step.empty()
                st.error(f"연산 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.dinner_data:
        d = st.session_state.dinner_data
        st.markdown("#### 🗺️ 위치 분석 결과")

        def disp_time(mins):
            """저장된 자동차 실시간 소요시간을 표시용 값으로 변환 (회식시각/날씨/이동수단 보정)"""
            if mins is None:
                return None
            t = mins * d.get("time_factor", 1.0) * d.get("w_mult", 1.0)
            if d.get("transit"):
                t = t * 1.6 + 12.0
            return t

        view_cat = st.selectbox("🍽️ 음식 종류 선택 (검색 후 자유롭게 전환)",
                                list(CATEGORY_OPTIONS.keys()), key="m2_view_category")
        sel_rests = d.get("rests_by_cat", {}).get(view_cat, [])

        # 선택 종류가 기본 반경 내에 없어 중심이 재조정된 경우, 해당 기준으로 표시
        adj = d.get("adjusted_by_cat", {}).get(view_cat)
        if adj:
            eff_lat, eff_lon = adj["center"]
            eff_times = adj.get("times", [])
            eff_paths = adj.get("paths", [])
            st.info(f"기본 반경 내에 {view_cat} 매장이 없어, 가장 가까운 {view_cat} 밀집 지역으로 중심을 재조정했습니다. 지도에는 기존 중심(빨간 별)과 재조정 중심(주황 별)이 함께 표시되며, 시간과 경로는 재조정 위치 기준입니다.")
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

            for r in sel_rests:
                folium.Marker([float(r["y"]), float(r["x"])], popup=r["place_name"],
                              icon=folium.Icon(color="green", icon="cutlery")).add_to(m)

            all_lats = [l["lat"] for l in d["locs"]] + [eff_lat]
            all_lons = [l["lon"] for l in d["locs"]] + [eff_lon]
            if adj:
                all_lats.append(d["b_lat"])
                all_lons.append(d["b_lon"])
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

            # 공유 문구 자동 생성
            st.markdown("#### 📣 공유 문구")
            lines = [f"[회식 안내] {view_cat} 회식 장소 추천 결과입니다."]
            if sel_rests:
                top = sel_rests[0]
                t_addr = top.get("road_address_name", "").strip() or top.get("address_name", "")
                lines.append(f"추천 매장: {top['place_name']} ({t_addr})")
            for idx, loc in enumerate(d["locs"]):
                t = disp_time(eff_times[idx] if idx < len(eff_times) else None)
                if t is not None:
                    lines.append(f"{loc['name']}: 약 {int(round(t))}분")
            lines.append(f"기준: {d.get('dinner_label', '')} 출발 예상 ({'대중교통 간이 추정' if d.get('transit') else '자동차'})")
            share_msg = "\n".join(lines)
            st.code(share_msg, language="text")
            st.caption("오른쪽 위 복사 아이콘으로 복사해 단체방에 공유하세요.")
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
