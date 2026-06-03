import math
import random
import requests
import pandas as pd
import streamlit as st
import folium
import time
import polyline
from datetime import datetime, timedelta
from streamlit_folium import st_folium
import streamlit.components.v1 as components

# ==========================================
# 1. 페이지 설정 및 세션 상태 초기화 (CSS 충돌 코드 완전 삭제됨)
# ==========================================
st.set_page_config(page_title="행복한 퇴근 이후", page_icon="🌆", layout="centered")

# 리셋 방지용 필수 세션 키 생성
initial_session_keys = {
    'num_people': 3,
    'favorite_contact': "",
    'commute_data': None,
    'dinner_data': None,
    'notify_data': None,
    'm1_start_results': [],
    'm1_end_results': [],
    'm3_start_results': [],
    'm3_end_results': []
}

for key, default_value in initial_session_keys.items():
    if key not in st.session_state:
        st.session_state[key] = default_value

str_kakao_rest_key = "df68bf65618592b6d685caec6521432f"

# ==========================================
# 2. 핵심 네트워크 API 정의
# ==========================================
def search_kakao_address(query):
    if not query or not query.strip():
        return []
    url = "https://dapi.kakao.com/v2/local/search/address.json"
    headers = {"Authorization": f"KakaoAK {str_kakao_rest_key}"}
    params = {"query": query, "size": 10}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=3).json()
        return res.get('documents', [])
    except:
        return []

@st.cache_data
def get_realtime_weather_and_temp():
    try:
        res = requests.get("https://api.open-meteo.com/v1/forecast?latitude=36.8065&longitude=127.1522&current_weather=true&timezone=auto", timeout=3).json()
        current = res.get('current_weather', {})
        code = current.get('weathercode', 0)
        temp = current.get('temperature', 20.0)
        if code in [0, 1, 2, 3]: desc = "맑음/구름"
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]: desc = "비 (강수)"
        elif code in [71, 73, 75, 85, 86]: desc = "눈 (결빙)"
        else: desc = "기타"
        return desc, temp
    except:
        return "수집 실패", 20.0

@st.cache_data
def get_kakao_navi_baseline(origin_lat, origin_lon, dest_lat, dest_lon):
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {str_kakao_rest_key}"}
    params = {"origin": f"{origin_lon},{origin_lat}", "destination": f"{dest_lon},{dest_lat}", "priority": "RECOMMEND"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=3).json()
        routes = res.get('routes', [])
        if routes and routes[0].get('result_code') == 0:
            summary = routes[0]['summary']
            dist = summary['distance'] / 1000
            dur = summary['duration'] / 60
            path = []
            for section in routes[0].get('sections', []):
                for road in section.get('roads', []):
                    for i in range(0, len(road.get('vertexes', [])), 2):
                        path.append([road['vertexes'][i+1], road['vertexes'][i]])
            return round(dist, 1), round(dur, 1), path
    except:
        pass
    return None, None, []

def get_real_road_path(waypoints):
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
    url = f"http://router.project-osrm.org/route/v1/driving/{coord_str}?overview=full&geometries=polyline"
    try:
        res = requests.get(url, timeout=3).json()
        routes = res.get('routes', [])
        if routes:
            return polyline.decode(routes[0]['geometry'])
    except:
        pass
    return waypoints 

def get_kakao_restaurants(lat, lon, radius_m):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {str_kakao_rest_key}"}
    params = {"query": "맛집", "category_group_code": "FD6", "x": str(lon), "y": str(lat), "radius": int(radius_m), "sort": "accuracy"}
    try:
        res = requests.get(url, headers=headers, params=params, timeout=3).json()
        return res.get('documents', [])[:5]
    except:
        return []

# ==========================================
# 3. 사이드바 내비게이션
# ==========================================
with st.sidebar:
    st.markdown("### ⚙️ 시스템 메뉴")
    선택메뉴 = st.selectbox("프로그램 선택", ["1. 퇴근시간 최적화 AI", "2. 회식장소 최적위치 산출기", "3. 출발 알리미"], key="main_menu_select")

# ==========================================
# 4. 모듈 1: 퇴근시간 최적화 AI
# ==========================================
if 선택메뉴 == "1. 퇴근시간 최적화 AI":
    st.markdown("### 🚗 퇴근시간 최적화 AI")
    
    with st.sidebar:
        st.markdown("#### 주소 검색 및 선택")
        m1_start_q = st.text_input("출발지 검색어 입력", "탕정 삼성로", key="m1_start_q")
        if st.button("출발지 주소 검색", key="m1_start_btn", use_container_width=True):
            st.session_state.m1_start_results = search_kakao_address(m1_start_q)
        
        m1_start_options = [doc['address_name'] for doc in st.session_state.m1_start_results]
        if m1_start_options:
            selected_m1_start = st.selectbox("정확한 출발지 주소 선택", m1_start_options, key="m1_start_select")
        else:
            st.caption("검색 결과가 없습니다.")
            selected_m1_start = None

        st.markdown("---")
        m1_end_q = st.text_input("목적지 검색어 입력", "천안 성황로", key="m1_end_q")
        if st.button("목적지 주소 검색", key="m1_end_btn", use_container_width=True):
            st.session_state.m1_end_results = search_kakao_address(m1_end_q)
            
        m1_end_options = [doc['address_name'] for doc in st.session_state.m1_end_results]
        if m1_end_options:
            selected_m1_end = st.selectbox("정확한 목적지 주소 선택", m1_end_options, key="m1_end_select")
        else:
            st.caption("검색 결과가 없습니다.")
            selected_m1_end = None

        st.markdown("---")
        탐색_시작 = st.time_input("시작시간", datetime.strptime("17:30", "%H:%M").time(), key="m1_time_start")
        탐색_종료 = st.time_input("종료시간", datetime.strptime("19:00", "%H:%M").time(), key="m1_time_end")
        
        if st.button("🔍 고밀도 스캔 실행", type="primary", use_container_width=True, key="m1_run_btn"):
            if not selected_m1_start or not selected_m1_end:
                st.error("출발지와 목적지 주소를 검색 후 선택해 주십시오.")
            else:
                with st.spinner("트래픽 분석 중..."):
                    try:
                        s_idx = m1_start_options.index(selected_m1_start)
                        e_idx = m1_end_options.index(selected_m1_end)
                        o_lat, o_lon = float(st.session_state.m1_start_results[s_idx]['y']), float(st.session_state.m1_start_results[s_idx]['x'])
                        d_lat, d_lon = float(st.session_state.m1_end_results[e_idx]['y']), float(st.session_state.m1_end_results[e_idx]['x'])
                        
                        dist_b, dur_b, path_k = get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon)
                        w_desc, temp_val = get_realtime_weather_and_temp()
                        
                        if dur_b:
                            now = datetime.now()
                            c_hour = now.hour + now.minute / 60.0
                            c_peak = math.exp(-((c_hour - 18.25) ** 2) / 0.04) if c_hour <= 18.25 else math.exp(-((c_hour - 18.25) ** 2) / 0.12)
                            base_A = dur_b / (1.0 + c_peak * 1.2)
                            base_B = base_A * 1.15
                            
                            today = datetime.today()
                            start_dt = datetime(today.year, today.month, today.day, 탐색_시작.hour, 탐색_시작.minute)
                            end_dt = datetime(today.year, today.month, today.day, 탐색_종료.hour, 탐색_종료.minute)
                            
                            options = []
                            current = start_dt
                            random.seed(int(start_dt.timestamp()))
                            
                            while current <= end_dt:
                                h_val = current.hour + current.minute / 60.0
                                peak = math.exp(-((h_val - 18.25) ** 2) / 0.04) if h_val <= 18.25 else math.exp(-((h_val - 18.25) ** 2) / 0.12)
                                noise = random.uniform(-1.0, 1.5)
                                dur_A, dur_B = base_A * (1.0 + peak * 1.2) + noise, base_B * (1.0 + peak * 0.3) + (noise * 0.5)
                                diff_m = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
                                penalty = (diff_m ** 1.2) * 0.05
                                
                                options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current, "route_name": "경로A (카카오 최적)", "distance_km": round(dist_b, 1), "duration_min": round(dur_A, 1), "score": dur_A + penalty})
                                options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current, "route_name": "경로B (우회도로)", "distance_km": round(dist_b * 1.15, 1), "duration_min": round(dur_B, 1), "score": dur_B + penalty})
                                current += timedelta(minutes=1)
                            
                            df_all = pd.DataFrame(options)
                            df_all['10분구간'] = df_all['dt_obj'].apply(lambda x: f"{x.replace(minute=(x.minute // 10) * 10).strftime('%H:%M')}~{(x.replace(minute=(x.minute // 10) * 10) + timedelta(minutes=9)).strftime('%H:%M')}")
                            summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()
                            
                            display_df = summary[["10분구간", "departure_time", "route_name", "duration_min", "distance_km"]].copy()
                            display_df.columns = ["10분구간", "최적 출발시간", "최고의 경로", "소요시간 (분)", "거리 (km)"]
                            display_df["날씨"] = w_desc
                            display_df["온도"] = f"{temp_val} °C"
                            
                            st.session_state.commute_data = {"df": display_df, "o_lat": o_lat, "o_lon": o_lon, "d_lat": d_lat, "d_lon": d_lon, "path_k": path_k}
                        else:
                            st.error("카카오 맵 경로 서버 통신에 실패했습니다.")
                    except Exception:
                        st.error("데이터 파싱 도중 예외가 발생했습니다.")

    if st.session_state.commute_data:
        c_res = st.session_state.commute_data
        st.markdown("#### ⏰ 최적 연산 결과")
        st.dataframe(c_res["df"], use_container_width=True, hide_index=True)
        selected_window = st.selectbox("🗺️ 지도 연동 구간 선택", c_res["df"]["10분구간"].tolist(), key="m1_map_select")
        
        try:
            target_route = c_res["df"][c_res["df"]["10분구간"] == selected_window].iloc[0]["최고의 경로"]
            st.markdown(f"#### 🗺️ 최적 루트: {target_route}")
            
            m = folium.Map(location=[(c_res["o_lat"]+c_res["d_lat"])/2, (c_res["o_lon"]+c_res["d_lon"])/2], zoom_start=12, tiles='CartoDB positron')
            folium.Marker([c_res["o_lat"], c_res["o_lon"]], popup="출발지").add_to(m)
            folium.Marker([c_res["d_lat"], c_res["d_lon"]], popup="목적지").add_to(m)
            
            if "경로A" in target_route: 
                folium.PolyLine(locations=c_res["path_k"], color='#dc3545', weight=6).add_to(m)
            else:
                path_o = get_real_road_path([[c_res["o_lat"], c_res["o_lon"]], [36.8200, 127.1100], [c_res["d_lat"], c_res["d_lon"]]])
                folium.PolyLine(locations=path_o, color='#28a745', weight=6).add_to(m)
            st_folium(m, use_container_width=True, height=350, key="map_commute_fixed")
        except:
            st.caption("지도를 동기화하는 중입니다.")

# ==========================================
# 5. 모듈 2: 회식장소 최적위치 산출기
# ==========================================
elif 선택메뉴 == "2. 회식장소 최적위치 산출기":
    st.markdown("### 🍻 회식장소 추천기")
    with st.sidebar:
        st.markdown("#### 참석 인원 제어")
        b1, b2 = st.columns(2)
        if b1.button("➕ 추가", key="m2_add_p"): st.session_state.num_people += 1
        if b2.button("➖ 감소", key="m2_sub_p") and st.session_state.num_people > 2: st.session_state.num_people -= 1
        st.markdown("---")
        search_radius = st.slider("🎯 탐색 반경 (m)", min_value=100, max_value=2000, value=500, step=100, key="m2_radius_slider")

    st.markdown("#### 👥 참석자 주소 검색")
    addresses = []
    
    for i in range(st.session_state.num_people):
        st.markdown(f"**참석자 {i+1} 설정**")
        p_query = st.text_input(f"참석자 {i+1} 도로명/건물명 검색", key=f"m2_p_query_{i}")
        
        if st.button(f"참석자 {i+1} 주소 조회", key=f"m2_p_btn_{i}"):
            st.session_state[f"m2_p_res_{i}"] = search_kakao_address(p_query)
            
        p_res = st.session_state.get(f"m2_p_res_{i}", [])
        p_options = [doc['address_name'] for doc in p_res]
        
        if p_options:
            selected_p_addr = st.selectbox(f"참석자 {i+1} 최종 주소 확정", p_options, key=f"m2_p_select_{i}")
            idx = p_options.index(selected_p_addr)
            addresses.append({"name": f"참석자 {i+1}", "doc": p_res[idx]})
        else:
            st.caption("주소를 검색한 뒤 선택해 주십시오.")
        st.markdown("---")
        
    if st.button("🔍 최적 위치 및 맛집 산출", type="primary", use_container_width=True, key="m2_run_btn"):
        if len(addresses) < st.session_state.num_people:
            st.error("모든 참석자의 주소를 검색 후 선택 완료해야 계산이 가능합니다.")
        else:
            with st.spinner("중심점 연산 중..."):
                valid_locations = []
                for p in addresses:
                    try:
                        lat = float(p["doc"]['y'])
                        lon = float(p["doc"]['x'])
                        valid_locations.append({"name": p["name"], "lat": lat, "lon": lon})
                    except:
                        pass
                
                if valid_locations:
                    avg_lat = sum(l["lat"] for l in valid_locations) / len(valid_locations)
                    avg_lon = sum(l["lon"] for l in valid_locations) / len(valid_locations)
                    
                    candidates = []
                    lat_off = 3 / 111.0 
                    lon_off = 3 / (111.0 * math.cos(math.radians(avg_lat)))
                    for i in range(3):
                        for j in range(3):
                            candidates.append((avg_lat - lat_off + (2 * lat_off * i / 2), avg_lon - lon_off + (2 * lon_off * j / 2)))
                    
                    sources = [(l["lat"], l["lon"]) for l in valid_locations]
                    coords = sources + candidates
                    coord_str = ";".join([f"{lon},{lat}" for lat, lon in coords])
                    src_str = ";".join(map(str, range(len(sources))))
                    dst_str = ";".join(map(str, range(len(sources), len(coords))))
                    
                    try:
                        res = requests.get(f"http://router.project-osrm.org/table/v1/driving/{coord_str}?sources={src_str}&destinations={dst_str}", timeout=3).json()
                        durations = res.get('durations', [])
                    except: durations = []
                    
                    best_idx, min_max = 0, float('inf')
                    best_times = []
                    if durations:
                        for d_idx in range(len(candidates)):
                            times = [durations[s_idx][d_idx] for s_idx in range(len(sources))]
                            if None in times: continue
                            if max(times) < min_max:
                                min_max = max(times)
                                best_idx = d_idx
                                best_times = times
                    
                    b_lat, b_lon = candidates[best_idx]
                    rests = get_kakao_restaurants(b_lat, b_lon, search_radius)
                    
                    st.session_state.dinner_data = {
                        "valid_locations": valid_locations, "b_lat": b_lat, "b_lon": b_lon,
                        "best_times": best_times, "rests": rests, "radius": search_radius
                    }

    if st.session_state.dinner_data:
        d_res = st.session_state.dinner_data
        st.markdown("#### 🗺️ 위치 분석 결과")
        
        if d_res.get("best_times"):
            for idx, loc in enumerate(d_res["valid_locations"]):
                st.markdown(f"⏱️ **{loc['name']} 소요시간**: 약 {int(d_res['best_times'][idx]//60)}분")
        
        try:
            m = folium.Map(location=[d_res["b_lat"], d_res["b_lon"]], zoom_start=14, tiles='CartoDB positron')
            for l in d_res["valid_locations"]:
                folium.Marker([l["lat"], l["lon"]], popup=l["name"]).add_to(m)
                folium.PolyLine([[l["lat"], l["lon"]], [d_res["b_lat"], d_res["b_lon"]]], color="gray", weight=2, dash_array='5, 5').add_to(m)
            
            folium.Circle(
                location=[d_res["b_lat"], d_res["b_lon"]], radius=int(d_res["radius"]), color='#0052cc',
                fill=True, fill_color='#0052cc', fill_opacity=0.3, weight=2
            ).add_to(m)
            
            folium.Marker([d_res["b_lat"], d_res["b_lon"]], popup="최적 중심점").add_to(m)
            for r in d_res["rests"]:
                folium.Marker([float(r['y']), float(r['x'])], popup=r['place_name']).add_to(m)
            st_folium(m, use_container_width=True, height=350, key="map_dinner_fixed")
            
            st.markdown(f"#### 🍽️ 반경 {d_res['radius']}m 내 맛집")
            if d_res["rests"]:
                rest_list = []
                for idx, r in enumerate(d_res["rests"]):
                    addr = r.get('road_address_name', '').strip()
                    if not addr: addr = r.get('address_name', '주소 누락')
                    rest_list.append({
                        "순위": f"{idx+1}위", "이름": r['place_name'],
                        "종류": r.get('category_name', '').split('>')[-1].strip(),
                        "주소": addr, "링크": r['place_url']
                    })
                st.dataframe(pd.DataFrame(rest_list), column_config={"링크": st.column_config.LinkColumn("🔗 지도")}, hide_index=True, use_container_width=True)
            else:
                st.info(f"지정한 반경({d_res['radius']}m) 내에 검색된 맛집이 없습니다.")
        except:
            st.caption("결과 화면을 동기화 중입니다.")

# ==========================================
# 6. 모듈 3: 출발 알리미
# ==========================================
elif 선택메뉴 == "3. 출발 알리미":
    st.markdown("### 💬 출발 알리미")
    
    st.markdown("#### 경로 및 수신 설정")
    
    m3_start_q = st.text_input("출발지 검색어 입력", "탕정 삼성로", key="m3_start_q")
    if st.button("출발지 검색", key="m3_start_btn", use_container_width=True):
        st.session_state.m3_start_results = search_kakao_address(m3_start_q)
        
    m3_start_options = [doc['address_name'] for doc in st.session_state.m3_start_results]
    if m3_start_options:
        selected_m3_start = st.selectbox("출발 주소 선택", m3_start_options, key="m3_start_select")
    else:
        selected_m3_start = None

    st.markdown("---")
    m3_end_q = st.text_input("목적지 검색어 입력", "천안 성황로", key="m3_end_q")
    if st.button("목적지 검색", key="m3_end_btn", use_container_width=True):
        st.session_state.m3_end_results = search_kakao_address(m3_end_q)
        
    m3_end_options = [doc['address_name'] for doc in st.session_state.m3_end_results]
    if m3_end_options:
        selected_m3_end = st.selectbox("목적 주소 선택", m3_end_options, key="m3_end_select")
    else:
        selected_m3_end = None

    st.markdown("---")
    contact_in = st.text_input("수신자 이름", value=st.session_state.favorite_contact, placeholder="예: 배우자", key="m3_contact_input")
    if st.button("⭐️ 즐겨찾기 등록", use_container_width=True, key="m3_fav_btn"):
        st.session_state.favorite_contact = contact_in
        st.success("즐겨찾기로 등록되었습니다.")
        
    if st.button("⏱️ 실시간 ETA 메시지 생성", type="primary", use_container_width=True, key="m3_run_btn"):
        if not selected_m3_start or not selected_m3_end:
            st.error("출발지와 목적지 주소를 검색 후 선택 완료해 주십시오.")
        else:
            try:
                s_idx = m3_start_options.index(selected_m3_start)
                e_idx = m3_end_options.index(selected_m3_end)
                o_lat, o_lon = float(st.session_state.m3_start_results[s_idx]['y']), float(st.session_state.m3_start_results[s_idx]['x'])
                d_lat, d_lon = float(st.session_state.m3_end_results[e_idx]['y']), float(st.session_state.m3_end_results[e_idx]['x'])
                
                _, dur, _ = get_kakao_navi_baseline(o_lat, o_lon, d_lat, d_lon)
                if dur:
                    eta_time = datetime.now() + timedelta(minutes=dur)
                    st.session_state.notify_data = {"eta": eta_time.strftime('%H시 %M분'), "dur": int(dur), "target": contact_in}
                else: 
                    st.error("교통정보를 획득하지 못했습니다.")
            except:
                st.error("연산 처리 중 오류가 발생했습니다.")

    st.markdown("---")
    if st.session_state.notify_data:
        n_res = st.session_state.notify_data
        
        final_msg = f"[{n_res['target']}님 출발 알림]\\n지금 퇴근 후 출발합니다.\\n🚗 도착 예정 시간: {n_res['eta']}\\n(실시간 교통망 기준 약 {n_res['dur']}분 소요 예상)"
