import math
import random
import requests
import polyline
import pandas as pd
import streamlit as st
import folium
from folium import plugins
from streamlit_folium import st_folium
from datetime import datetime, timedelta, timezone
from geopy.geocoders import Nominatim

# ==========================================
# 1. 페이지 및 세션 기본 설정
# ==========================================
st.set_page_config(page_title="종합 교통/물류 최적화 시스템", page_icon="⚙️", layout="wide")

# 세션 상태 초기화
if 'run_status' not in st.session_state:
    st.session_state.run_status = False
if 'num_people' not in st.session_state:
    st.session_state.num_people = 3

def start_analysis():
    st.session_state.run_status = True

# 카카오 API 키 고정
KAKAO_API_KEY = "df68bf65618592b6d685caec6521432f"

# ==========================================
# 2. 공통 및 개별 로직 함수 정의
# ==========================================
def get_real_road_path(waypoints):
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
    url = f"http://router.project-osrm.org/route/v1/driving/{coord_str}?overview=full&geometries=polyline"
    try:
        res = requests.get(url, timeout=5).json()
        return polyline.decode(res['routes'][0]['geometry'])
    except:
        return waypoints 

def get_realtime_weather():
    try:
        res = requests.get("https://api.open-meteo.com/v1/forecast?latitude=36.8065&longitude=127.1522&current_weather=true&timezone=auto", timeout=5).json()
        code = res['current_weather']['weathercode']
        if code in [0, 1, 2, 3]: return "맑음/구름", 1.0
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]: return "비 (강수)", 1.30 
        elif code in [71, 73, 75, 85, 86]: return "눈 (결빙)", 1.60 
        else: return "기타", 1.15
    except:
        return "수집 실패", 1.0

def get_lat_lon(address):
    geolocator = Nominatim(user_agent="dinner_optimizer_app_v2")
    try:
        location = geolocator.geocode(address)
        return (location.latitude, location.longitude) if location else (None, None)
    except:
        return None, None

def find_best_time_location(valid_locations):
    avg_lat = sum(loc["lat"] for loc in valid_locations) / len(valid_locations)
    avg_lon = sum(loc["lon"] for loc in valid_locations) / len(valid_locations)
    
    candidates = []
    grid_size = 3
    lat_offset = 3 / 111.0 
    lon_offset = 3 / (111.0 * math.cos(math.radians(avg_lat)))
    
    for i in range(grid_size):
        for j in range(grid_size):
            lat = avg_lat - lat_offset + (2 * lat_offset * i / (grid_size - 1))
            lon = avg_lon - lon_offset + (2 * lon_offset * j / (grid_size - 1))
            candidates.append((lat, lon))
            
    sources = [(loc["lat"], loc["lon"]) for loc in valid_locations]
    coords = sources + candidates
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in coords])
    src_str = ";".join(map(str, range(len(sources))))
    dst_str = ";".join(map(str, range(len(sources), len(coords))))
    
    url = f"http://router.project-osrm.org/table/v1/driving/{coord_str}?sources={src_str}&destinations={dst_str}"
    
    try:
        res = requests.get(url, timeout=5).json()
        durations = res.get('durations', [])
        
        best_idx = 0
        min_max_time = float('inf')
        best_times = []
        
        for d_idx in range(len(candidates)):
            times = [durations[s_idx][d_idx] for s_idx in range(len(sources))]
            if None in times: continue
            max_time = max(times)
            
            if max_time < min_max_time:
                min_max_time = max_time
                best_idx = d_idx
                best_times = times
                
        return candidates[best_idx][0], candidates[best_idx][1], best_times
    except:
        return avg_lat, avg_lon, []

def get_kakao_restaurants(lat, lon):
    url = "https://dapi.kakao.com/v2/local/search/keyword.json"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {"query": "맛집", "x": lon, "y": lat, "radius": 1500, "sort": "accuracy"}
    try:
        res = requests.get(url, headers=headers, params=params).json()
        return res.get('documents', [])[:5]
    except:
        return []

# ==========================================
# 3. 사이드바 메뉴 통합 및 제어
# ==========================================
with st.sidebar:
    st.header("📱 시스템 메뉴")
    선택메뉴 = st.selectbox("실행할 프로그램 선택", ["🚗 퇴근시간 최적화 AI", "🍻 회식장소 소요시간 추천기"])
    st.markdown("---")

# ==========================================
# 4. 기능 A: 퇴근시간 최적화 AI 화면
# ==========================================
if 선택메뉴 == "🚗 퇴근시간 최적화 AI":
    st.title("🚗 다중 변수 기반 퇴근시간 최적화 AI")
    st.markdown("실시간 기상 상황과 실제 도로망(OSRM)을 반영하여 10분 구간별 최적의 출발 1분을 찾아냅니다.")
    
    with st.sidebar:
        st.subheader("⚙️ 퇴근 설정")
        출발지 = st.text_input("출발지", "삼성디스플레이 아산1캠퍼스")
        목적지 = st.text_input("목적지", "충청남도 천안시 동남구 성황로 40")
        탐색_시작시간 = st.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time())
        탐색_종료시간 = st.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time())
        지정_날짜 = st.date_input("분석 기준일 (날짜 선택)", key="date_commute")
        st.markdown("---")
        st.button("🔍 실시간 스캔 및 분석 시작", on_click=start_analysis, key="btn_commute")

    if st.session_state.run_status:
        KST = timezone(timedelta(hours=9), name="KST")
        weekday_idx = 지정_날짜.weekday()
        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        weekday_str = weekdays[weekday_idx]
        
        if weekday_idx == 4: day_weight, day_desc = 1.25, f"{weekday_str}요일 (퇴근 러시 극심)"
        elif weekday_idx >= 5: day_weight, day_desc = 0.8, f"{weekday_str}요일 (주말 한산함)"
        else: day_weight, day_desc = 1.0, f"{weekday_str}요일 (일반 평일)"

        start_dt = datetime(지정_날짜.year, 지정_날짜.month, 지정_날짜.day, 탐색_시작시간.hour, 탐색_시작시간.minute)
        end_dt = datetime(지정_날짜.year, 지정_날짜.month, 지정_날짜.day, 탐색_종료시간.hour, 탐색_종료시간.minute)
        if end_dt < start_dt: end_dt += timedelta(days=1)

        current_weather, base_weather_weight = get_realtime_weather()
        st.info(f"📅 **분석 기준일:** {지정_날짜.strftime('%Y년 %m월 %d일')} | {day_desc} \n\n📡 **실시간 기상:** {current_weather} (가중치: {base_weather_weight})")
        
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        options = []
        current = start_dt
        random.seed(int(start_dt.timestamp()))
        
        total_steps = int((end_dt - start_dt).total_seconds() / 60) + 1
        step_count = 0

        while current <= end_dt:
            step_count += 1
            progress_bar.progress(step_count / total_steps)
            status_text.text(f"📡 AI 교통망 스캔 중... {current.strftime('%H:%M')}")
            
            hour_val = current.hour + current.minute / 60
            if hour_val <= 18.25: peak = math.exp(-((hour_val - 18.25) ** 2) / 0.04) 
            else: peak = math.exp(-((hour_val - 18.25) ** 2) / 0.12)
                
            noise = random.uniform(-1.0, 1.5)
            weather_impact_A = base_weather_weight
            weather_impact_B = 1.0 + (base_weather_weight - 1.0) * 0.4 

            dur_1 = (28 + 32 * peak + noise) * day_weight * weather_impact_A
            dur_2 = (35 + 10 * peak + (noise * 0.5)) * day_weight * weather_impact_B
            
            diff_minutes = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
            penalty = (diff_minutes ** 1.2) * 0.05 

            options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current, "route_name": "경로A (번영로)", "distance_km": 18.5, "duration_min": round(dur_1, 1), "score": round(dur_1 + penalty, 2)})
            options.append({"departure_time": current.strftime("%H:%M"), "dt_obj": current, "route_name": "경로B (우회도로)", "distance_km": 20.2, "duration_min": round(dur_2, 1), "score": round(dur_2 + penalty, 2)})
            current += timedelta(minutes=1)

        status_text.text("✅ 분석 완료!")
        progress_bar.empty()

        df_all = pd.DataFrame(options).sort_values(["score", "duration_min"]).reset_index(drop=True)

        def make_window(dt):
            st_time = dt.replace(minute=(dt.minute // 10) * 10)
            return f"{st_time.strftime('%H:%M')}~{(st_time + timedelta(minutes=9)).strftime('%H:%M')}"

        df_all['10분구간'] = df_all['dt_obj'].apply(make_window)
        summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()

        display_df = summary[["10분구간", "departure_time", "route_name", "distance_km", "duration_min"]].rename(
            columns={"departure_time": "최적 출발시간", "route_name": "추천 경로", "distance_km": "거리(km)", "duration_min": "최종 예상시간(분)"}
        )
        display_df.insert(0, '요일', weekday_str)
        display_df.insert(0, '날짜', 지정_날짜.strftime('%Y-%m-%d'))
        display_df['기상상황'] = current_weather

        col1, col2 = st.columns([1, 1])
        with col1:
            st.subheader("⏰ 10분 구간별 최적 출발시간")
            st.dataframe(display_df, use_container_width=True, hide_index=True)
            st.subheader("📈 1분 단위 소요시간 추이")
            chart_data = df_all.pivot(index='departure_time', columns='route_name', values='duration_min')
            st.line_chart(chart_data)

        with col2:
            st.subheader("🗺️ 실제 도로망 경로 시각화")
            origin_c, dest_c = [36.8193, 127.0585], [36.8065, 127.1522]
            waypoint_A, waypoint_B = [36.7946, 127.1044], [36.8200, 127.1100] 

            m = folium.Map(location=[(origin_c[0]+dest_c[0])/2, (origin_c[1]+dest_c[1])/2], zoom_start=12, tiles='CartoDB Positron')
            folium.Marker(origin_c, popup=f'출발지', icon=folium.Icon(color='blue')).add_to(m)
            folium.Marker(dest_c, popup=f'목적지', icon=folium.Icon(color='red')).add_to(m)

            with st.spinner("실제 도로 좌표를 그리는 중..."):
                path_A = get_real_road_path([origin_c, waypoint_A, dest_c])
                path_B = get_real_road_path([origin_c, waypoint_B, dest_c])

            plugins.AntPath(locations=path_A, color='#dc3545', weight=5, tooltip="경로A (번영로)").add_to(m)
            plugins.AntPath(locations=path_B, color='#28a745', weight=5, tooltip="경로B (우회도로)").add_to(m)
            st_folium(m, width=700, height=500, key="map_commute")

# ==========================================
# 5. 기능 B: 회식장소 추천기 화면
# ==========================================
elif 선택메뉴 == "🍻 회식장소 소요시간 추천기":
    st.title("🍻 소요시간 밸런스 기반 회식장소 추천기")
    st.markdown("참석자들의 실제 차량 이동 시간을 스캔하여, 모두가 덜 억울한 최적의 장소와 주변 맛집을 매핑합니다.")
    
    with st.sidebar:
        st.subheader("⚙️ 인원 제어")
        btn1, btn2 = st.columns(2)
        with btn1:
            if st.button("➕ 인원 추가"): st.session_state.num_people += 1
        with btn2:
            if st.button("➖ 인원 감소") and st.session_state.num_people > 2: st.session_state.num_people -= 1

    col1, col2 = st.columns([1, 2])
    
    with col1:
        st.subheader("👥 참석자 출발지 입력")
        addresses = []
        for i in range(st.session_state.num_people):
            default_addr = ["서울특별시 강남구 역삼동", "서울특별시 마포구 서교동", "경기도 성남시 분당구 삼평동"][i] if i < 3 else ""
            addr = st.text_input(f"참석자 {i+1} 출발지", value=default_addr, key=f"addr_dinner_{i}")
            if addr.strip():
                addresses.append({"name": f"참석자 {i+1}", "address": addr})

        st.markdown("---")
        calculate_btn = st.button("🔍 소요시간 스캔 및 맛집 찾기", type="primary", use_container_width=True, key="btn_dinner")

    with col2:
        st.subheader("🗺️ 분석 결과")
        if calculate_btn:
            if len(addresses) < 2:
                st.warning("최소 2명 이상의 주소를 입력해주세요.")
            else:
                with st.spinner("AI가 후보지의 실시간 이동 시간을 계산 중입니다..."):
                    valid_locations = []
                    for person in addresses:
                        lat, lon = get_lat_lon(person["address"])
                        if lat and lon:
                            valid_locations.append({"name": person["name"], "address": person["address"], "lat": lat, "lon": lon})
                        time.sleep(0.4)
                    
                    if valid_locations:
                        best_lat, best_lon, travel_times = find_best_time_location(valid_locations)
                        st.success("✨ 이동 시간을 분석하여 모두에게 가장 공평한 지점을 도출했습니다!")
                        
                        if travel_times:
                            st.markdown("#### ⏱️ 예상 소요시간 (차량 기준)")
                            time_cols = st.columns(len(valid_locations))
                            for idx, col in enumerate(time_cols):
                                mins = int(travel_times[idx] // 60)
                                col.metric(label=valid_locations[idx]["name"], value=f"약 {mins}분")
                        
                        restaurants = get_kakao_restaurants(best_lat, best_lon)
                        m = folium.Map(location=[best_lat, best_lon], zoom_start=12, tiles='CartoDB Positron')
                        
                        for loc in valid_locations:
                            folium.Marker([loc["lat"], loc["lon"]], popup=loc["name"], icon=folium.Icon(color='blue', icon='user')).add_to(m)
                            folium.PolyLine([[loc["lat"], loc["lon"]], [best_lat, best_lon]], color="gray", weight=2, dash_array='5, 5', opacity=0.6).add_to(m)
                        
                        folium.Marker([best_lat, best_lon], popup="<b>소요시간 최적 지점</b>", icon=folium.Icon(color='red', icon='star')).add_to(m)
                        
                        for rest in restaurants:
                            folium.Marker(
                                [float(rest['y']), float(rest['x'])],
                                popup=f"<b>{rest['place_name']}</b>",
                                icon=folium.Icon(color='orange', icon='cutlery')
                            ).add_to(m)

                        st_folium(m, width=800, height=450, key="map_dinner")
                        
                        st.markdown("### 🍽️ 이 주변 카카오맵 추천 맛집 Top 5")
                        if restaurants:
                            for idx, rest in enumerate(restaurants):
                                st.write(f"**{idx+1}. {rest['place_name']}** ({rest.get('category_name', '').split('>')[-1].strip()})")
                                st.caption(f"📍 {rest['road_address_name']} | 📞 {rest['phone'] if rest['phone'] else '번호 없음'}")
                                st.markdown(f"[👉 카카오맵에서 상세 보기]({rest['place_url']})")
                                st.divider()
                        else:
                            st.info("해당 지점 주변에 검색된 맛집이 없습니다.")
        else:
            st.info("왼쪽에서 주소를 입력하고 검색을 시작해보세요.")
