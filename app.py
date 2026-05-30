import math
import random
import requests
import pandas as pd
import streamlit as st
import folium
import time
from datetime import datetime, timedelta
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

# ==========================================
# 1. 페이지 및 환경 설정
# ==========================================
st.set_page_config(page_title="종합 교통 최적화 시스템", page_icon="⚙️", layout="wide")

if 'run_status' not in st.session_state:
    st.session_state.run_status = False

def start_analysis():
    st.session_state.run_status = True

# 카카오 REST API 키 고정
KAKAO_API_KEY = "df68bf65618592b6d685caec6521432f"

# ==========================================
# 2. 핵심 로직 함수
# ==========================================
def get_lat_lon(address):
    """주소를 위도, 경도로 변환합니다."""
    geolocator = Nominatim(user_agent="traffic_predictor_v4")
    try:
        location = geolocator.geocode(address)
        return (location.latitude, location.longitude) if location else (None, None)
    except:
        return None, None

def get_realtime_weather_and_temp():
    """Open-Meteo API를 통해 현재 기상 상황과 온도를 수집합니다."""
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

def get_kakao_navi_baseline(origin_lat, origin_lon, dest_lat, dest_lon):
    """카카오 API를 통해 '현재 시각' 기준의 실시간 소요 시간과 거리를 가져옵니다."""
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    params = {
        "origin": f"{origin_lon},{origin_lat}",
        "destination": f"{dest_lon},{dest_lat}",
        "priority": "RECOMMEND"
    }
    try:
        res = requests.get(url, headers=headers, params=params).json()
        if res['routes'][0]['result_code'] == 0:
            summary = res['routes'][0]['summary']
            distance = summary['distance'] / 1000 # km
            duration = summary['duration'] / 60   # 분
            
            path = []
            for section in res['routes'][0]['sections']:
                for road in section['roads']:
                    for i in range(0, len(road['vertexes']), 2):
                        path.append([road['vertexes'][i+1], road['vertexes'][i]])
            return round(distance, 1), round(duration, 1), path
    except:
        pass
    return None, None, []

# ==========================================
# 3. 화면 UI 및 컨트롤러
# ==========================================
st.title("🚗 카카오 실시간 연동형 퇴근시간 최적화 AI")
st.markdown("카카오 모빌리티 실시간 데이터를 앵커 삼아 1분 단위로 교통 흐름을 시뮬레이션한 후, 매 10분 구간 내 최적의 결과를 산출합니다.")

with st.sidebar:
    st.header("⚙️ 분석 설정")
    출발지 = st.text_input("출발지 (도로명 주소)", "충청남도 아산시 탕정면 삼성로 1")
    목적지 = st.text_input("목적지 (도로명 주소)", "충청남도 천안시 동남구 성황로 40")
    
    st.markdown("---")
    탐색_시작 = st.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time())
    탐색_종료 = st.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time())
    
    st.markdown("---")
    st.button("🔍 1분 단위 정밀 스캔 및 미래 예측 실행", on_click=start_analysis, type="primary", use_container_width=True)

# ==========================================
# 4. 데이터 처리 및 시각화 출력
# ==========================================
if st.session_state.run_status:
    with st.spinner("카카오 실시간 교통량 반영 및 1분 단위 고밀도 정밀 연산 중..."):
        
        # 1. 주소 -> 좌표 변환
        origin_lat, origin_lon = get_lat_lon(출발지)
        dest_lat, dest_lon = get_lat_lon(목적지)
        
        if not origin_lat or not dest_lat:
            st.error("주소 해석에 실패했습니다. 정확한 도로명 주소 형식인지 확인하십시오.")
        else:
            # 2. 현재 기준 실시간 데이터 및 기상 데이터 수집
            distance_base, duration_base, path_coords = get_kakao_navi_baseline(origin_lat, origin_lon, dest_lat, dest_lon)
            weather_desc, temperature = get_realtime_weather_and_temp()
            
            if duration_base:
                st.success(f"📡 데이터 동기화 완료 | 실시간 소요시간: {int(duration_base)}분 | 기상: {weather_desc} ({temperature}°C)")
                
                # 3. 1분 단위 고밀도 가우시안 시뮬레이션
                now = datetime.now()
                current_hour_val = now.hour + now.minute / 60.0
                
                if current_hour_val <= 18.25:
                    current_peak = math.exp(-((current_hour_val - 18.25) ** 2) / 0.04)
                else:
                    current_peak = math.exp(-((current_hour_val - 18.25) ** 2) / 0.12)
                
                # 기준 경로 주행 시간 역산
                base_drive_time_A = duration_base / (1.0 + current_peak * 1.2)
                base_drive_time_B = base_drive_time_A * 1.15  # 우회 도로는 약 15% 기본 소요시간 김
                
                today = datetime.today()
                start_dt = datetime(today.year, today.month, today.day, 탐색_시작.hour, 탐색_시작.minute)
                end_dt = datetime(today.year, today.month, today.day, 탐색_종료.hour, 탐색_종료.minute)
                
                options = []
                current = start_dt
                random.seed(int(start_dt.timestamp()))
                
                # 1분 단위 루프 생성
                while current <= end_dt:
                    hour_val = current.hour + current.minute / 60.0
                    
                    # 시간대별 정체 지수 산출
                    if hour_val <= 18.25:
                        peak = math.exp(-((hour_val - 18.25) ** 2) / 0.04)
                    else:
                        peak = math.exp(-((hour_val - 18.25) ** 2) / 0.12)
                    
                    noise = random.uniform(-1.0, 1.5)
                    
                    # 경로A(번영로) 및 경로B(우회도로)의 정체 민감도 차별 적용
                    dur_A = base_drive_time_A * (1.0 + peak * 1.2) + noise
                    dur_B = base_drive_time_B * (1.0 + peak * 0.3) + (noise * 0.5)
                    
                    # 선호 시간 패널티 적용 (18시 10분 최적 타겟팅 가중치)
                    diff_minutes = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
                    penalty = (diff_minutes ** 1.2) * 0.05
                    
                    options.append({
                        "departure_time": current.strftime("%H:%M"),
                        "dt_obj": current,
                        "route_name": "경로A (번영로)",
                        "distance_km": round(distance_base, 1),
                        "duration_min": round(dur_A, 1),
                        "score": round(dur_A + penalty, 2)
                    })
                    options.append({
                        "departure_time": current.strftime("%H:%M"),
                        "dt_obj": current,
                        "route_name": "경로B (우회도로)",
                        "distance_km": round(distance_base * 1.15, 1),
                        "duration_min": round(dur_B, 1),
                        "score": round(dur_B + penalty, 2)
                    })
                    current += timedelta(minutes=1)
                
                df_all = pd.DataFrame(options)
                
                # 4. 10분 구간 그룹라이징 및 최적값 추출 로직
                def make_window(dt):
                    st_time = dt.replace(minute=(dt.minute // 10) * 10)
                    return f"{st_time.strftime('%H:%M')}~{(st_time + timedelta(minutes=9)).strftime('%H:%M')}"
                
                df_all['10분구간'] = df_all['dt_obj'].apply(make_window)
                
                # 10분 구간 내에서 스코어가 가장 낮은(최적인) 1분 단위 행 선택
                summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()
                
                # 데이터 프레임 정리 및 출력 항목 매핑
                display_df = summary[["10분구간", "departure_time", "route_name", "duration_min", "distance_km"]].rename(
                    columns={
                        "departure_time": "최적 출발시간",
                        "route_name": "최고의 경로",
                        "duration_min": "소요시간 (분)",
                        "distance_km": "거리 (km)"
                    }
                )
                display_df["날씨"] = weather_desc
                display_df["온도"] = f"{temperature} °C"
                
                # 5. 화면 분할 레이아웃 시각화
                col1, col2 = st.columns([1, 1])
                
                with col1:
                    st.subheader("⏰ 10분 구간별 최적 출발 분 및 경로 산출")
                    st.dataframe(display_df, use_container_width=True, hide_index=True)
                    
                    st.subheader("📈 1분 단위 전수조사 소요시간 추이 그래프")
                    chart_data = df_all.pivot(index='departure_time', columns='route_name', values='duration_min')
                    st.line_chart(chart_data)
                    
                with col2:
                    st.subheader("🗺️ 카카오 내비 제공 실시간 추천 경로")
                    m = folium.Map(location=[(origin_lat+dest_lat)/2, (origin_lon+dest_lon)/2], zoom_start=12, tiles='CartoDB Positron')
                    
                    folium.Marker([origin_lat, origin_lon], popup="출발지", icon=folium.Icon(color='blue', icon='play')).add_to(m)
                    folium.Marker([dest_lat, dest_lon], popup="목적지", icon=folium.Icon(color='red', icon='stop')).add_to(m)
                    folium.PolyLine(locations=path_coords, color='#ffc107', weight=6, tooltip="실시간 경로").add_to(m)
                    
                    st_folium(m, width=700, height=520, key="map_ 정밀")
            else:
                st.error("카카오 내비 서버 응답을 처리하지 못했습니다. 주소명 혹은 API 상태를 재검증하십시오.")
