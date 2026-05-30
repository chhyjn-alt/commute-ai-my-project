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
from geopy.geocoders import Nominatim

# ==========================================
# 1. 페이지 및 환경 설정
# ==========================================
st.set_page_config(page_title="행복한 퇴근 이후", page_icon="🌆", layout="wide")

if 'num_people' not in st.session_state:
    st.session_state.num_people = 3

# 카카오 REST API 키
KAKAO_API_KEY = "df68bf65618592b6d685caec6521432f"

# ==========================================
# 2. 공통 함수 (데이터 캐싱 적용)
# ==========================================
@st.cache_data
def get_lat_lon(address):
    geolocator = Nominatim(user_agent="happy_after_work_app_v2")
    try:
        location = geolocator.geocode(address)
        return (location.latitude, location.longitude) if location else (None, None)
    except:
        return None, None

@st.cache_data
def get_realtime_weather_and_temp():
    try:
        res = requests.get("https://api.open-meteo.com/v1/forecast?latitude=36.8065&longitude=127.1522&current_weather=true&timezone=auto", timeout=5).json()
        code = res['current_weather']['weathercode']
        temp = res['current_weather']['temperature']
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
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"origin": f"{origin_lon},{origin_lat}", "destination": f"{dest_lon},{dest_lat}", "priority": "RECOMMEND"}
    try:
        res = requests.get(url, headers=headers, params=params).json()
        if res['routes'][0]['result_code'] == 0:
            summary = res['routes'][0]['summary']
            dist = summary['distance'] / 1000
            dur = summary['duration'] / 60
            path = []
            for section in res['routes'][0]['sections']:
                for road in section['roads']:
                    for i in range(0, len(road['vertexes']), 2):
                        path.append([road['vertexes'][i+1], road['vertexes'][i]])
            return round(dist, 1), round(dur, 1), path
    except:
        pass
    return None, None, []

def get_real_road_path(waypoints):
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
    url = f"http://router.project-osrm.org/route/v1/driving/{coord_str}?overview=full&geometries=polyline"
    try:
        res = requests.get(url, timeout=5).json()
        return polyline.decode(res['routes'][0]['geometry'])
    except:
        return waypoints 

@st.cache_data
def get_kakao_restaurants(lat, lon, radius_m):
    url = "https://dapi.kakao.com/v2/local/search/category.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"category_group_code": "FD6", "x": str(lon), "y": str(lat), "radius": int(radius_m), "sort": "popularity"}
    try:
        res = requests.get(url, headers=headers, params=params).json()
        return res.get('documents', [])[:5]
    except:
        return []

# ==========================================
# 3. 사이드바 메뉴 제어
# ==========================================
with st.sidebar:
    st.header("시스템 메뉴")
    선택메뉴 = st.selectbox("실행할 프로그램 선택", [
        "1. 퇴근시간 최적화 AI", 
        "2. 회식장소 최적위치 산출기",
        "3. 귀가 알림 ETA 산출기"
    ])
    st.markdown("---")

# ==========================================
# 4. 기능 1: 퇴근시간 최적화 AI
# ==========================================
if 선택메뉴 == "1. 퇴근시간 최적화 AI":
    st.title("🚗 카카오 실시간 연동형 퇴근시간 최적화 AI")
    
    with st.sidebar:
        st.subheader("분석 설정")
        출발지 = st.text_input("출발지", "충청남도 아산시 탕정면 삼성로 1")
        목적지 = st.text_input("목적지", "충청남도 천안시 동남구 성황로 40")
        탐색_시작 = st.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time())
        탐색_종료 = st.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time())
        실행버튼 = st.button("실시간 동기화 및 예측 실행", type="primary")

    if 실행버튼:
        with st.spinner("교통량 파싱 및 1분 단위 스캔 중..."):
            origin_lat, origin_lon = get_lat_lon(출발지)
            dest_lat, dest_lon = get_lat_lon(목적지)
            
            if not origin_lat or not dest_lat:
                st.error("좌표 변환 실패. 주소를 확인하십시오.")
            else:
                dist_base, dur_base, path_kakao = get_kakao_navi_baseline(origin_lat, origin_lon, dest_lat, dest_lon)
                weather_desc, temperature = get_realtime_weather_and_temp()
                
                if dur_base:
                    now = datetime.now()
                    current_hour_val = now.hour + now.minute / 60.0
                    current_peak = math.exp(-((current_hour_val - 18.25) ** 2) / 0.04) if current_hour_val <= 18.25 else math.exp(-((current_hour_val - 18.25) ** 2) / 0.12)
                    
                    base_time_A = dur_base / (1.0 + current_peak * 1.2)
                    base_time_B = base_time_A * 1.15
                    
                    today = datetime.today()
                    start_dt = datetime(today.year, today.month, today.day, 탐색_시작.hour, 탐색_시작.minute)
                    end_dt = datetime(today.year, today.month, today.day, 탐색_종료.hour, 탐색_종료.minute)
                    
                    options = []
                    current = start_dt
                    random.seed(int(start_dt.timestamp()))
                    
                    while current <= end_dt:
                        hour_val = current.hour + current.minute / 60.0
                        peak = math.exp(-((hour_val - 18.25) ** 2) / 0.04) if hour_val <= 18.25 else math.exp(-((hour_val - 18.25) ** 2) / 0.12)
                        noise = random.uniform(-1.0, 1.5)
                        
                        dur_A = base_time_A * (1.0 + peak * 1.2) + noise
                        dur_B = base_time_B * (1.0 + peak * 0.3) + (noise * 0.5)
                        
                        diff_min = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
                        penalty = (diff_min ** 1.2) * 0.05
                        
                        options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current, "route_name": "경로A (카카오/번영로)", "distance_km": round(dist_base, 1), "duration_min": round(dur_A, 1), "score": dur_A + penalty})
                        options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current, "route_name": "경로B (우회도로)", "distance_km": round(dist_base * 1.15, 1), "duration_min": round(dur_B, 1), "score": dur_B + penalty})
                        current += timedelta(minutes=1)
                    
                    df_all = pd.DataFrame(options)
                    df_all['10분구간'] = df_all['dt_obj'].apply(lambda x: f"{x.replace(minute=(x.minute // 10) * 10).strftime('%H:%M')}~{(x.replace(minute=(x.minute // 10) * 10) + timedelta(minutes=9)).strftime('%H:%M')}")
                    summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()
                    
                    display_df = summary[["10분구간", "departure_time", "route_name", "duration_min", "distance_km"]].copy()
                    display_df.columns = ["10분구간", "최적 출발시간", "최고의 경로", "소요시간 (분)", "거리 (km)"]
                    display_df["날씨"] = weather_desc
                    display_df["온도"] = f"{temperature} °C"

                    col1, col2 = st.columns([1, 1])
                    with col1:
                        st.subheader("⏰ 10분 구간별 최적 산출 결과")
                        st.dataframe(display_df, use_container_width=True, hide_index=True)
                        selected_window = st.selectbox("🗺️ 지도에 표시할 10분 구간 선택", display_df["10분구간"].tolist())
                        target_row = display_df[display_df["10분구간"] == selected_window].iloc[0]
                        target_route = target_row["최고의 경로"]

                    with col2:
                        st.subheader(f"🗺️ 시각화: {target_route}")
                        m = folium.Map(location=[(origin_lat+dest_lat)/2, (origin_lon+dest_lon)/2], zoom_start=12, tiles='CartoDB Positron')
                        folium.Marker([origin_lat, origin_lon], popup="출발지", icon=folium.Icon(color='blue')).add_to(m)
                        folium.Marker([dest_lat, dest_lon], popup="목적지", icon=folium.Icon(color='red')).add_to(m)
                        
                        if "경로A" in target_route:
                            folium.PolyLine(locations=path_kakao, color='#dc3545', weight=6, tooltip="경로A (카카오 최적)").add_to(m)
                        else:
                            waypoint_B = [36.8200, 127.1100]
                            path_osrm = get_real_road_path([[origin_lat, origin_lon], waypoint_B, [dest_lat, dest_lon]])
                            folium.PolyLine(locations=path_osrm, color='#28a745', weight=6, tooltip="경로B (우회도로)").add_to(m)
                        st_folium(m, width=700, height=450)

# ==========================================
# 5. 기능 2: 회식장소 최적위치 산출기
# ==========================================
elif 선택메뉴 == "2. 회식장소 최적위치 산출기":
    st.title("🍻 소요시간 밸런스 기반 회식장소 추천기")
    
    with st.sidebar:
        st.subheader("⚙️ 인원 제어")
        b1, b2 = st.columns(2)
        if b1.button("➕ 인원 추가"): st.session_state.num_people += 1
        if b2.button("➖ 인원 감소") and st.session_state.num_people > 2: st.session_state.num_people -= 1
        st.markdown("---")
        search_radius = st.slider("🎯 맛집 탐색 반경 설정 (m)", min_value=100, max_value=2000, value=500, step=100)

    col1, col2 = st.columns([1, 2])
    with col1:
        st.subheader("👥 참석자 출발지 입력")
        addresses = []
        for i in range(st.session_state.num_people):
            default_addr = ["서울특별시 강남구 역삼동", "서울특별시 마포구 서교동", "경기도 성남시 분당구 삼평동"][i] if i < 3 else ""
            addr = st.text_input(f"참석자 {i+1} 출발지", value=default_addr, key=f"addr_{i}")
            if addr.strip(): addresses.append({"name": f"참석자 {i+1}", "address": addr})
        실행버튼 = st.button("🔍 소요시간 스캔 및 맛집 찾기", type="primary")

    with col2:
        st.subheader("🗺️ 분석 결과")
        if 실행버튼:
            if len(addresses) < 2:
                st.warning("최소 2명의 주소가 필요합니다.")
            else:
                with st.spinner("AI가 이동 시간을 분석 중입니다..."):
                    valid_locations = []
                    for p in addresses:
                        lat, lon = get_lat_lon(p["address"])
                        if lat and lon: valid_locations.append({"name": p["name"], "lat": lat, "lon": lon})
                    
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
                            res = requests.get(f"http://router.project-osrm.org/table/v1/driving/{coord_str}?sources={src_str}&destinations={dst_str}").json()
                            durations = res.get('durations', [])
                        except:
                            durations = []
                        
                        best_idx, min_max_time, best_times = 0, float('inf'), []
                        if durations:
                            for d_idx in range(len(candidates)):
                                times = [durations[s_idx][d_idx] for s_idx in range(len(sources))]
                                if None in times: continue
                                if max(times) < min_max_time:
                                    min_max_time, best_idx, best_times = max(times), d_idx, times
                                
                        b_lat, b_lon = candidates[best_idx]
                        
                        if best_times:
                            st.success("✨ 공평한 지점 분석 완료")
                            t_cols = st.columns(len(valid_locations))
                            for idx, col in enumerate(t_cols):
                                col.metric(valid_locations[idx]["name"], f"약 {int(best_times[idx]//60)}분")
                        
                        rests = get_kakao_restaurants(b_lat, b_lon, search_radius)
                        
                        m = folium.Map(location=[b_lat, b_lon], zoom_start=14, tiles='CartoDB Positron')
                        for l in valid_locations:
                            folium.Marker([l["lat"], l["lon"]], popup=l["name"]).add_to(m)
                            folium.PolyLine([[l["lat"], l["lon"]], [b_lat, b_lon]], color="gray", weight=2, dash_array='5, 5').add_to(m)
                        
                        folium.Circle(
                            location=[b_lat, b_lon], radius=int(search_radius), color='#0052cc',
                            fill=True, fill_color='#0052cc', fill_opacity=0.3, weight=2
                        ).add_to(m)
                        
                        folium.Marker([b_lat, b_lon], popup="최적 중심점", icon=folium.Icon(color='red', icon='star')).add_to(m)
                        for r in rests:
                            folium.Marker([float(r['y']), float(r['x'])], popup=r['place_name'], icon=folium.Icon(color='orange', icon='cutlery')).add_to(m)
                        st_folium(m, width=700, height=450)
                        
                        st.markdown(f"### 🍽️ AI 추천 반경 {search_radius}m 내 인기 맛집 Top 5")
                        if rests:
                            rest_data = []
                            for i, r in enumerate(rests):
                                addr = r.get('road_address_name', '').strip()
                                if not addr: addr = r.get('address_name', '주소 정보 없음')
                                rest_data.append({
                                    "순위": f"{i+1}위", "식당 이름": r['place_name'],
                                    "종류": r.get('category_name', '').split('>')[-1].strip(),
                                    "상세 주소": addr, "메뉴 및 가격": r['place_url']
                                })
                            st.dataframe(pd.DataFrame(rest_data), column_config={"메뉴 및 가격": st.column_config.LinkColumn("🔗 카카오맵 확인")}, hide_index=True, use_container_width=True)
                        else:
                            st.info("검색된 맛집이 없습니다. 반경을 조정하십시오.")

# ==========================================
# 6. 기능 3: 귀가 알림 ETA 산출기
# ==========================================
elif 선택메뉴 == "3. 귀가 알림 ETA 산출기":
    st.title("💬 귀가 알림 실시간 ETA 산출기")
    st.markdown("목적지까지의 실시간 소요시간(카카오 내비 데이터)을 반영하여 도착 예정 시간(ETA)을 산출하고, 메신저 공유용 텍스트 템플릿을 생성합니다.")
    
    col1, col2 = st.columns([1, 1])
    with col1:
        출발지 = st.text_input("출발지", "충청남도 아산시 탕정면 삼성로 1")
        목적지 = st.text_input("도착지", "충청남도 천안시 동남구 성황로 40")
        산출버튼 = st.button("도착 예정 시간(ETA) 산출", type="primary")
        
    with col2:
        if 산출버튼:
            origin_lat, origin_lon = get_lat_lon(출발지)
            dest_lat, dest_lon = get_lat_lon(목적지)
            if origin_lat and dest_lat:
                dist, dur, _ = get_kakao_navi_baseline(origin_lat, origin_lon, dest_lat, dest_lon)
                if dur:
                    eta = datetime.now() + timedelta(minutes=dur)
                    st.success("실시간 교통 데이터 기반 예측이 완료되었습니다.")
                    
                    message_template = f"[귀가 알림]\n지금 출발합니다.\n🚗 도착 예정 시간: {eta.strftime('%H시 %M분')}\n(현재 교통상황 기준 약 {int(dur)}분 소요 예상)"
                    st.text_area("생성된 메시지 (복사하여 카카오톡 등에 사용하십시오)", value=message_template, height=150)
                else:
                    st.error("내비게이션 경로 탐색에 실패했습니다.")
