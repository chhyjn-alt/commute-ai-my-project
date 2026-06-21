"""
=========================================================
행복한 퇴근 이후 - 통합 앱 (Streamlit / GitHub 배포 최적화 버전)
  탭1) 퇴근시간 최적화 AI
  탭2) 회식장소 최적위치 산출기
  탭3) 출발 알리미 (문자 앱 열기 링크)

[원본 대비 주요 개선]
1. 보안: 하드코딩된 카카오 키를 st.secrets로 분리 (GitHub 노출 방지)
2. 무한 로딩 해결:
   - 외부 API 타임아웃을 3초 -> 8초로 상향 + OSRM 미러/재시도
   - st.tabs 사용으로 탭 전환 시 불필요한 전체 재연산 방지
   - st_folium 결과값 추적 차단(returned_objects=[])으로 리렌더 루프 차단
3. 로직 버그 수정: 회식 후보 3x3 격자 간격 계산 정상화
4. 예외 처리: bare except 제거, 사용자에게 원인 메시지 표시
5. 패키지 의존성 정리 (requirements.txt 동봉)

[배포 방법]
1) requirements.txt 를 저장소에 추가
2) .streamlit/secrets.toml 에 카카오 키 저장 (아래 형식)
       KAKAO_REST_KEY = "발급받은_본인_키"
   * Streamlit Cloud 는 Settings > Secrets 메뉴에 같은 내용 입력
3) app.py 를 메인 파일로 지정하여 배포
=========================================================
"""

import math
import random
import time
import urllib.parse
from datetime import datetime, timedelta

import requests
import pandas as pd
import folium
import streamlit as st
from streamlit_folium import st_folium

# polyline 은 경로 디코딩에만 사용. 미설치 환경에서도 앱이 죽지 않도록 안전 처리.
try:
    import polyline
    _HAS_POLYLINE = True
except Exception:
    _HAS_POLYLINE = False


# =========================================================
# 0. 페이지 설정 / 상태 초기화 / 키 로딩
# =========================================================
st.set_page_config(page_title="행복한 퇴근 이후", page_icon="🌆", layout="centered")

_DEFAULT_STATE = {
    "num_people": 3,
    "favorite_contact": "",
    "commute_data": None,
    "dinner_data": None,
    "notify_data": None,
    "m1_start_results": [],
    "m1_end_results": [],
    "m3_start_results": [],
    "m3_end_results": [],
}
for k, v in _DEFAULT_STATE.items():
    if k not in st.session_state:
        st.session_state[k] = v


def get_kakao_key():
    """카카오 REST 키를 secrets 에서 로딩. 없으면 안내 후 중단."""
    try:
        return st.secrets["KAKAO_REST_KEY"]
    except Exception:
        st.error(
            "카카오 REST 키가 설정되지 않았습니다.\n\n"
            "`.streamlit/secrets.toml` 파일(또는 Streamlit Cloud의 Secrets)에 "
            "아래 한 줄을 추가해 주세요:\n\n"
            '`KAKAO_REST_KEY = \"발급받은_본인_키\"`'
        )
        st.stop()


KAKAO_KEY = get_kakao_key()
_TIMEOUT = 8  # 외부 API 공통 타임아웃 (원본 3초 -> 8초)

_OSRM_MIRRORS = [
    "https://router.project-osrm.org",
    "https://routing.openstreetmap.de/routed-car",
]


# =========================================================
# 1. 외부 API 함수
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
    except Exception:
        st.warning(f"주소 검색 통신에 실패했습니다: {query}")
        return []


@st.cache_data(show_spinner=False, ttl=600)
def get_realtime_weather_and_temp():
    """천안 지역 현재 날씨/기온 (Open-Meteo, 무료/키 불필요)"""
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=36.8065&longitude=127.1522"
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


@st.cache_data(show_spinner=False, ttl=300)
def get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon):
    """카카오 내비 기준 경로(거리/시간/경로좌표). 캐시 가능한 기본형 자료만 반환."""
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"origin": f"{o_lon},{o_lat}", "destination": f"{d_lon},{d_lat}", "priority": "RECOMMEND"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        routes = res.get("routes", [])
        if routes and routes[0].get("result_code") == 0:
            summary = routes[0]["summary"]
            dist = summary["distance"] / 1000
            dur = summary["duration"] / 60
            path = []
            for section in routes[0].get("sections", []):
                for road in section.get("roads", []):
                    vs = road.get("vertexes", [])
                    for i in range(0, len(vs), 2):
                        path.append([vs[i + 1], vs[i]])
            return round(dist, 1), round(dur, 1), path
    except Exception:
        pass
    return None, None, []


def osrm_request(path_and_query):
    """OSRM 요청을 미러 서버로 재시도."""
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
    """OSRM 우회 경로 좌표 (polyline 미설치 시 직선 좌표 반환)"""
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
    data = osrm_request(f"/route/v1/driving/{coord_str}?overview=full&geometries=polyline")
    routes = data.get("routes", [])
    if routes and _HAS_POLYLINE:
        try:
            return polyline.decode(routes[0]["geometry"])
        except Exception:
            return waypoints
    return waypoints


def get_kakao_restaurants(lat, lon, radius_m):
    """카카오 키워드 검색으로 주변 맛집 상위 5곳"""
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_KEY}"}
    params = {"query": "맛집", "category_group_code": "FD6",
              "x": str(lon), "y": str(lat), "radius": int(radius_m), "sort": "accuracy"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=_TIMEOUT).json()
        return res.get("documents", [])[:5]
    except Exception:
        return []


def now_kst():
    return datetime.utcnow() + timedelta(hours=9)


# =========================================================
# 2. 상단 탭 구성
# =========================================================
st.title("🌆 행복한 퇴근 이후")
tab1, tab2, tab3 = st.tabs(["🚗 퇴근시간 최적화", "🍻 회식장소 추천", "💬 출발 알리미"])


# ---------------------------------------------------------
# 탭 1: 퇴근시간 최적화 AI
# ---------------------------------------------------------
with tab1:
    st.subheader("퇴근시간 최적화 AI")
    st.caption("출발지·목적지와 탐색 시간대를 입력하면, 혼잡도 시뮬레이션으로 10분 구간별 최적 출발시간을 추천합니다.")

    col1, col2 = st.columns(2)
    with col1:
        m1_start_q = st.text_input("출발지 검색어", "탕정 삼성로", key="m1_start_q")
        if st.button("출발지 주소 검색", key="m1_start_btn", use_container_width=True):
            st.session_state.m1_start_results = search_kakao_address(m1_start_q)
        m1_start_options = [d["address_name"] for d in st.session_state.m1_start_results]
        selected_m1_start = st.selectbox("출발지 선택", m1_start_options, key="m1_start_select") if m1_start_options else None
    with col2:
        m1_end_q = st.text_input("목적지 검색어", "천안 성황로", key="m1_end_q")
        if st.button("목적지 주소 검색", key="m1_end_btn", use_container_width=True):
            st.session_state.m1_end_results = search_kakao_address(m1_end_q)
        m1_end_options = [d["address_name"] for d in st.session_state.m1_end_results]
        selected_m1_end = st.selectbox("목적지 선택", m1_end_options, key="m1_end_select") if m1_end_options else None

    c1, c2 = st.columns(2)
    탐색_시작 = c1.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time(), key="m1_time_start")
    탐색_종료 = c2.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time(), key="m1_time_end")

    if st.button("🔍 최적 출발시간 스캔", type="primary", use_container_width=True, key="m1_run_btn"):
        if not selected_m1_start or not selected_m1_end:
            st.error("출발지와 목적지를 검색 후 선택해 주세요.")
        else:
            with st.spinner("교통 혼잡도 시뮬레이션 중..."):
                try:
                    s_idx = m1_start_options.index(selected_m1_start)
                    e_idx = m1_end_options.index(selected_m1_end)
                    o_lat = float(st.session_state.m1_start_results[s_idx]["y"])
                    o_lon = float(st.session_state.m1_start_results[s_idx]["x"])
                    d_lat = float(st.session_state.m1_end_results[e_idx]["y"])
                    d_lon = float(st.session_state.m1_end_results[e_idx]["x"])

                    dist_b, dur_b, path_k = get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon)
                    w_desc, temp_val = get_realtime_weather_and_temp()

                    if dur_b:
                        today = now_kst()
                        c_hour = today.hour + today.minute / 60.0
                        c_peak = (math.exp(-((c_hour - 18.25) ** 2) / 0.04)
                                  if c_hour <= 18.25 else math.exp(-((c_hour - 18.25) ** 2) / 0.12))
                        base_A = dur_b / (1.0 + c_peak * 1.2)
                        base_B = base_A * 1.15

                        start_dt = datetime(today.year, today.month, today.day, 탐색_시작.hour, 탐색_시작.minute)
                        end_dt = datetime(today.year, today.month, today.day, 탐색_종료.hour, 탐색_종료.minute)

                        options = []
                        current = start_dt
                        random.seed(int(start_dt.timestamp()))
                        while current <= end_dt:
                            h = current.hour + current.minute / 60.0
                            peak = (math.exp(-((h - 18.25) ** 2) / 0.04)
                                    if h <= 18.25 else math.exp(-((h - 18.25) ** 2) / 0.12))
                            noise = random.uniform(-1.0, 1.5)
                            dur_A = base_A * (1.0 + peak * 1.2) + noise
                            dur_B = base_B * (1.0 + peak * 0.3) + (noise * 0.5)
                            diff_m = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
                            penalty = (diff_m ** 1.2) * 0.05
                            options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current,
                                            "route_name": "경로A (카카오 최적)", "distance_km": round(dist_b, 1),
                                            "duration_min": round(dur_A, 1), "score": dur_A + penalty})
                            options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current,
                                            "route_name": "경로B (우회도로)", "distance_km": round(dist_b * 1.15, 1),
                                            "duration_min": round(dur_B, 1), "score": dur_B + penalty})
                            current += timedelta(minutes=1)

                        df_all = pd.DataFrame(options)
                        df_all["10분구간"] = df_all["dt_obj"].apply(
                            lambda x: f"{x.replace(minute=(x.minute // 10) * 10).strftime('%H:%M')}~"
                                      f"{(x.replace(minute=(x.minute // 10) * 10) + timedelta(minutes=9)).strftime('%H:%M')}")
                        summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()

                        disp = summary[["10분구간", "departure_time", "route_name", "duration_min", "distance_km"]].copy()
                        disp.columns = ["10분구간", "최적 출발시간", "최고의 경로", "소요시간(분)", "거리(km)"]
                        disp["날씨"] = w_desc
                        disp["온도"] = f"{temp_val} °C"

                        st.session_state.commute_data = {
                            "df": disp, "o_lat": o_lat, "o_lon": o_lon,
                            "d_lat": d_lat, "d_lon": d_lon, "path_k": path_k
                        }
                    else:
                        st.error("카카오 길찾기 서버 통신에 실패했습니다. 잠시 후 다시 시도해 주세요.")
                except Exception as e:
                    st.error(f"데이터 처리 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.commute_data:
        c = st.session_state.commute_data
        st.markdown("#### ⏰ 최적 연산 결과")
        st.dataframe(c["df"], use_container_width=True, hide_index=True)
        sel = st.selectbox("🗺️ 지도에 표시할 구간 선택", c["df"]["10분구간"].tolist(), key="m1_map_select")
        try:
            target = c["df"][c["df"]["10분구간"] == sel].iloc[0]["최고의 경로"]
            st.markdown(f"**선택 경로: {target}**")
            m = folium.Map(location=[(c["o_lat"] + c["d_lat"]) / 2, (c["o_lon"] + c["d_lon"]) / 2],
                           zoom_start=12, tiles="CartoDB positron")
            folium.Marker([c["o_lat"], c["o_lon"]], popup="출발지", icon=folium.Icon(color="blue")).add_to(m)
            folium.Marker([c["d_lat"], c["d_lon"]], popup="목적지", icon=folium.Icon(color="red")).add_to(m)
            if "경로A" in target and c["path_k"]:
                folium.PolyLine(locations=c["path_k"], color="#dc3545", weight=6).add_to(m)
            else:
                mid = [(c["o_lat"] + c["d_lat"]) / 2 + 0.01, (c["o_lon"] + c["d_lon"]) / 2 - 0.01]
                path_o = get_real_road_path([[c["o_lat"], c["o_lon"]], mid, [c["d_lat"], c["d_lon"]]])
                folium.PolyLine(locations=path_o, color="#28a745", weight=6).add_to(m)
            st_folium(m, use_container_width=True, height=350, key="map_commute", returned_objects=[])
        except Exception:
            st.caption("지도를 준비하는 중입니다.")


# ---------------------------------------------------------
# 탭 2: 회식장소 최적위치 산출기
# ---------------------------------------------------------
with tab2:
    st.subheader("회식장소 추천기")
    st.caption("참석자 주소를 모두 입력하면, 모두의 이동시간이 가장 균형 잡힌 중심점과 주변 맛집을 추천합니다.")

    bc1, bc2, bc3 = st.columns([1, 1, 2])
    if bc1.button("➕ 인원 추가", key="m2_add"):
        st.session_state.num_people += 1
    if bc2.button("➖ 인원 감소", key="m2_sub") and st.session_state.num_people > 2:
        st.session_state.num_people -= 1
    search_radius = bc3.slider("탐색 반경(m)", 100, 2000, 500, 100, key="m2_radius")

    addresses = []
    for i in range(st.session_state.num_people):
        st.markdown(f"**참석자 {i + 1}**")
        pc1, pc2 = st.columns([3, 1])
        p_query = pc1.text_input(f"도로명/건물명 검색", key=f"m2_q_{i}", label_visibility="collapsed",
                                 placeholder=f"참석자 {i + 1} 주소 검색어")
        if pc2.button("조회", key=f"m2_btn_{i}", use_container_width=True):
            st.session_state[f"m2_res_{i}"] = search_kakao_address(p_query)
        p_res = st.session_state.get(f"m2_res_{i}", [])
        p_options = [d["address_name"] for d in p_res]
        if p_options:
            sel_addr = st.selectbox(f"참석자 {i + 1} 주소 확정", p_options, key=f"m2_sel_{i}")
            addresses.append({"name": f"참석자 {i + 1}", "doc": p_res[p_options.index(sel_addr)]})
        else:
            st.caption("검색어 입력 후 [조회]를 눌러 주소를 선택하세요.")

    if st.button("🔍 최적 위치 및 맛집 산출", type="primary", use_container_width=True, key="m2_run"):
        if len(addresses) < st.session_state.num_people:
            st.error("모든 참석자의 주소를 검색·선택해야 계산할 수 있습니다.")
        else:
            with st.spinner("이동시간 행렬 연산 중..."):
                try:
                    locs = []
                    for p in addresses:
                        locs.append({"name": p["name"], "lat": float(p["doc"]["y"]), "lon": float(p["doc"]["x"])})

                    avg_lat = sum(l["lat"] for l in locs) / len(locs)
                    avg_lon = sum(l["lon"] for l in locs) / len(locs)

                    # 후보 3x3 격자 (버그 수정: 간격이 -off, 0, +off 로 균등 분포되도록)
                    candidates = []
                    lat_off = 3 / 111.0
                    lon_off = 3 / (111.0 * math.cos(math.radians(avg_lat)))
                    for i in (-1, 0, 1):
                        for j in (-1, 0, 1):
                            candidates.append((avg_lat + lat_off * i, avg_lon + lon_off * j))

                    sources = [(l["lat"], l["lon"]) for l in locs]
                    coords = sources + candidates
                    coord_str = ";".join([f"{lon},{lat}" for lat, lon in coords])
                    src_str = ";".join(map(str, range(len(sources))))
                    dst_str = ";".join(map(str, range(len(sources), len(coords))))

                    data = osrm_request(f"/table/v1/driving/{coord_str}?sources={src_str}&destinations={dst_str}")
                    durations = data.get("durations", [])

                    best_idx, min_max, best_times = 0, float("inf"), []
                    if durations:
                        for d_idx in range(len(candidates)):
                            times = [durations[s][d_idx] for s in range(len(sources))]
                            if None in times:
                                continue
                            if max(times) < min_max:
                                min_max, best_idx, best_times = max(times), d_idx, times

                    b_lat, b_lon = candidates[best_idx]
                    rests = get_kakao_restaurants(b_lat, b_lon, search_radius)

                    st.session_state.dinner_data = {
                        "locs": locs, "b_lat": b_lat, "b_lon": b_lon,
                        "best_times": best_times, "rests": rests, "radius": search_radius
                    }
                except Exception as e:
                    st.error(f"연산 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.dinner_data:
        d = st.session_state.dinner_data
        st.markdown("#### 🗺️ 위치 분석 결과")
        if d.get("best_times"):
            cols = st.columns(len(d["locs"]))
            for idx, loc in enumerate(d["locs"]):
                cols[idx].metric(loc["name"], f"{int(d['best_times'][idx] // 60)}분")
        try:
            m = folium.Map(location=[d["b_lat"], d["b_lon"]], zoom_start=14, tiles="CartoDB positron")
            for l in d["locs"]:
                folium.Marker([l["lat"], l["lon"]], popup=l["name"], icon=folium.Icon(color="blue")).add_to(m)
                folium.PolyLine([[l["lat"], l["lon"]], [d["b_lat"], d["b_lon"]]],
                                color="gray", weight=2, dash_array="5, 5").add_to(m)
            folium.Circle(location=[d["b_lat"], d["b_lon"]], radius=int(d["radius"]),
                          color="#0052cc", fill=True, fill_color="#0052cc", fill_opacity=0.3, weight=2).add_to(m)
            folium.Marker([d["b_lat"], d["b_lon"]], popup="최적 중심점",
                          icon=folium.Icon(color="red", icon="star")).add_to(m)
            for r in d["rests"]:
                folium.Marker([float(r["y"]), float(r["x"])], popup=r["place_name"],
                              icon=folium.Icon(color="green", icon="cutlery")).add_to(m)
            st_folium(m, use_container_width=True, height=350, key="map_dinner", returned_objects=[])

            st.markdown(f"#### 🍽️ 반경 {d['radius']}m 내 맛집")
            if d["rests"]:
                rest_list = []
                for idx, r in enumerate(d["rests"]):
                    addr = r.get("road_address_name", "").strip() or r.get("address_name", "주소 누락")
                    rest_list.append({"순위": f"{idx + 1}위", "이름": r["place_name"],
                                      "종류": r.get("category_name", "").split(">")[-1].strip(),
                                      "주소": addr, "링크": r["place_url"]})
                st.dataframe(pd.DataFrame(rest_list),
                             column_config={"링크": st.column_config.LinkColumn("🔗 지도")},
                             hide_index=True, use_container_width=True)
            else:
                st.info(f"반경 {d['radius']}m 내에 검색된 맛집이 없습니다. 반경을 넓혀보세요.")
        except Exception:
            st.caption("결과 화면을 준비하는 중입니다.")


# ---------------------------------------------------------
# 탭 3: 출발 알리미
# ---------------------------------------------------------
with tab3:
    st.subheader("출발 알리미")
    st.caption("도착 예정 시간을 계산해 문자 메시지를 만들고, 휴대폰 문자 앱을 바로 여는 링크를 생성합니다.")

    col1, col2 = st.columns(2)
    with col1:
        m3_start_q = st.text_input("출발지 검색어", "탕정 삼성로", key="m3_start_q")
        if st.button("출발지 검색", key="m3_start_btn", use_container_width=True):
            st.session_state.m3_start_results = search_kakao_address(m3_start_q)
        m3_start_options = [d["address_name"] for d in st.session_state.m3_start_results]
        selected_m3_start = st.selectbox("출발 주소 선택", m3_start_options, key="m3_start_select") if m3_start_options else None
    with col2:
        m3_end_q = st.text_input("목적지 검색어", "천안 성황로", key="m3_end_q")
        if st.button("목적지 검색", key="m3_end_btn", use_container_width=True):
            st.session_state.m3_end_results = search_kakao_address(m3_end_q)
        m3_end_options = [d["address_name"] for d in st.session_state.m3_end_results]
        selected_m3_end = st.selectbox("목적 주소 선택", m3_end_options, key="m3_end_select") if m3_end_options else None

    contact_in = st.text_input("수신자 (이름 + 번호)", value=st.session_state.favorite_contact,
                               placeholder="예: 배우자 01012345678", key="m3_contact")
    if st.button("⭐ 즐겨찾기 등록", use_container_width=True, key="m3_fav"):
        st.session_state.favorite_contact = contact_in
        st.success("즐겨찾기로 등록되었습니다.")

    if st.button("✅ 도착 예정시간 계산", type="primary", use_container_width=True, key="m3_prepare"):
        if not selected_m3_start or not selected_m3_end:
            st.error("출발지와 목적지를 검색 후 선택해 주세요.")
        elif not contact_in.strip():
            st.error("수신자 연락처를 입력해 주세요.")
        else:
            with st.spinner("교통 정보 분석 중..."):
                try:
                    s_idx = m3_start_options.index(selected_m3_start)
                    e_idx = m3_end_options.index(selected_m3_end)
                    o_lat = float(st.session_state.m3_start_results[s_idx]["y"])
                    o_lon = float(st.session_state.m3_start_results[s_idx]["x"])
                    d_lat = float(st.session_state.m3_end_results[e_idx]["y"])
                    d_lon = float(st.session_state.m3_end_results[e_idx]["x"])

                    _, dur, _ = get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon)
                    if dur:
                        eta = (now_kst() + timedelta(minutes=dur)).strftime("%H시 %M분")
                        target_name = contact_in.split(" ")[0] if " " in contact_in else contact_in
                        msg = (f"[{target_name}님 출발 알림]\n지금 퇴근 후 출발합니다.\n\n"
                               f"📍 출발: {selected_m3_start}\n🚩 도착: {selected_m3_end}\n\n"
                               f"🚗 도착 예정: {eta}\n(실시간 교통 기준 약 {int(dur)}분 소요 예상)")
                        phone = "".join(filter(str.isdigit, contact_in))
                        st.session_state.notify_data = {"ready": True, "msg": msg, "phone": phone}
                    else:
                        st.error("교통 정보를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
                except Exception as e:
                    st.error(f"연산 중 오류가 발생했습니다: {type(e).__name__}")

    if st.session_state.notify_data and st.session_state.notify_data.get("ready"):
        n = st.session_state.notify_data
        st.markdown("#### 📋 발송 내용 미리보기")
        st.code(n["msg"], language="text")
        sms_url = f"sms:{n['phone']}?body={urllib.parse.quote(n['msg'])}"
        st.link_button("💬 문자 앱으로 열기", sms_url, type="primary", use_container_width=True)
        st.caption("버튼을 누르면 휴대폰 기본 문자 앱이 열리며, 내용이 자동 입력됩니다. (전송 버튼은 직접 눌러야 발송됩니다)")
