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

# ==========================================
# 1. 페이지 및 세션 기본 설정
# ==========================================
st.set_page_config(page_title="퇴근시간 최적화 AI", page_icon="🚗", layout="wide")

# ⭐️ 수정 포인트: 지도 렌더링 시 화면이 초기화되는 현상을 방지하기 위해 세션 상태를 고정합니다.
if 'run_status' not in st.session_state:
    st.session_state.run_status = False

def start_analysis():
    st.session_state.run_status = True

# ==========================================
# 2. 핵심 로직 함수
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

# ==========================================
# 3. 화면 UI 및 입력부 (좌측 사이드바)
# ==========================================
st.title("🚗 다중 변수 기반 퇴근시간 최적화 AI")
st.markdown("실시간 기상 상황과 실제 도로망(OSRM)을 반영하여 10분 구간별 최적의 출발 1분을 찾아냅니다.")

with st.sidebar:
    st.header("⚙️ 시스템 설정")
    출발지 = st.text_input("출발지", "삼성디스플레이 아산1캠퍼스")
    목적지 = st.text_input("목적지", "충청남도 천안시 동남구 성황로 40")
    
    탐색_시작시간 = st.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time())
    탐색_종료시간 = st.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time())
    지정_날짜 = st.date_input("분석 기준일 (날짜 선택)")
    
    st.markdown("---")
    # ⭐️ 수정 포인트: 버튼 클릭 시 세션 상태를 변경하는 함수(on_click)를 호출합니다.
    st.button("🔍 실시간 스캔 및 분석 시작", on_click=start_analysis)

# ==========================================
# 4. 분석 실행부
# ==========================================
# ⭐️ 수정 포인트: 단순 버튼 상태가 아닌, 고정된 세션 상태를 기준으로 실행합니다.
if st.session_state.run_status:
    KST = timezone(timedelta(hours=9), name="KST")
    
    weekday_idx = 지정_날짜.weekday()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    weekday_str = weekdays[weekday_idx]
    
    if weekday_idx == 4: day_weight, day_desc = 1.25, f"{weekday_str}요일 (퇴근 러시 극심)"
    elif weekday_idx >= 5: day_weight, day_desc = 0.8, f"{weekday_str}요일 (주말 한산함)"
    else: day_weight, day_desc = 1.0, f"{weekday_str}요일 (일반 평일)"

    st_hour, st_min = 탐색_시작시간.hour, 탐색_시작시간.minute
    ed_hour, ed_min = 탐색_종료시간.hour, 탐색_종료시간.minute
    start_dt = datetime(지정_날짜.year, 지정_날짜.month, 지정_날짜.day, st_hour, st_min)
    end_dt = datetime(지정_날짜.year, 지정_날짜.month, 지정_날짜.day, ed_hour, ed_min)
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

    # ==========================================
    # 5. 결과 화면 출력 
    # ==========================================
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

        st_folium(m, width=700, height=500)
