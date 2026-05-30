import math
import random
import requests
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

# 발급받은 카카오 API 키 고정
KAKAO_API_KEY = "df68bf65618592b6d685caec6521432f"

# ==========================================
# 2. 핵심 로직 함수
# ==========================================
def get_lat_lon(address):
    """주소를 위도, 경도로 변환합니다."""
    geolocator = Nominatim(user_agent="traffic_predictor_v3")
    try:
        location = geolocator.geocode(address)
        return (location.latitude, location.longitude) if location else (None, None)
    except:
        return None, None

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

def predict_future_intervals(baseline_duration, start_time, end_time):
    """실시간 수집된 소요시간을 기준으로 시간대별 정체 가중치를 적용하여 미래 소요시간을 산출합니다."""
    now = datetime.now()
    current_hour_val = now.hour + now.minute / 60.0
    
    # 현재 시점의 정체 지수 계산 (18:15분 피크 가우시안 모델)
    if current_hour_val <= 18.25:
        current_peak = math.exp(-((current_hour_val - 18.25) ** 2) / 0.04)
    else:
        current_peak = math.exp(-((current_hour_val - 18.25) ** 2) / 0.12)
        
    # 기본 주행 시간 역산 (정체가 전혀 없을 때의 예측 소요 시간)
    base_drive_time = baseline_duration / (1.0 + current_peak * 1.2)
    
    options = []
    current = start_time
    
    while current <= end_time:
        hour_val = current.hour + current.minute / 60.0
        
        # 타겟 시간의 정체 지수 계산
        if hour_val <= 18.25:
            target_peak = math.exp(-((hour_val - 18.25) ** 2) / 0.04)
        else:
            target_peak = math.exp(-((hour_val - 18.25) ** 2) / 0.12)
            
        # 미래 특정 시점의 최종 예측 소요 시간 도출
        predicted_duration = base_drive_time * (1.0 + target_peak * 1.2)
        
        options.append({
            "출발 시간": current.strftime("%H:%M"),
            "예상 소요 시간 (분)": round(predicted_duration, 1)
        })
        current += timedelta(minutes=10) # 10분 단위 구간 산출
        
    return pd.DataFrame(options)

# ==========================================
# 3. 화면 UI 및 컨트롤러
# ==========================================
st.title("🚗 카카오 실시간 연동형 퇴근시간 최적화 AI")
st.markdown("카카오 모빌리티의 현재 실시간 소요 시간을 기준점 삼아, 퇴근 시간대(17:30~19:00)의 10분 단위 정체 추이를 예측 및 산출합니다.")

with st.sidebar:
    st.header("⚙️ 분석 설정")
    출발지 = st.text_input("출발지 (도로명 주소)", "충청남도 아산시 탕정면 삼성로 1")
    목적지 = st.text_input("목적지 (도로명 주소)", "충청남도 천안시 동남구 성황로 40")
    
    st.markdown("---")
    탐색_시작 = st.time_input("탐색 시작시간", datetime.strptime("17:30", "%H:%M").time())
    탐색_종료 = st.time_input("탐색 종료시간", datetime.strptime("19:00", "%H:%M").time())
    
    st.markdown("---")
    st.button("🔍 실시간 동기화 및 미래 예측 실행", on_click=start_analysis, type="primary", use_container_width=True)

# ==========================================
# 4. 데이터 처리 및 시각화 출력
# ==========================================
if st.session_state.run_status:
    with st.spinner("카카오 실시간 교통량 파싱 및 시간대별 최적화 알고리즘 구동 중..."):
        
        # 1. 주소 -> 좌표 변환
        origin_lat, origin_lon = get_lat_lon(출발지)
        dest_lat, dest_lon = get_lat_lon(목적지)
        
        if not origin_lat or not dest_lat:
            st.error("주소 해석에 실패했습니다. 정확한 도로명 주소 형식인지 확인하십시오.")
        else:
            # 2. 현재 기준 카카오 실시간 데이터 확보
            distance, current_duration, path_coords = get_kakao_navi_baseline(origin_lat, origin_lon, dest_lat, dest_lon)
            
            if current_duration:
                st.success(f"📡 카카오 실시간 교통 정보 동기화 완료 (현재 소요 시간: {int(current_duration)}분 / 주행 거리: {distance}km)")
                
                # 3. 10분 단위 구간 예측 데이터 프레임 생성
                today = datetime.today()
                start_dt = datetime(today.year, today.month, today.day, 탐색_시작.hour, 탐색_시작.minute)
                end_dt = datetime(today.year, today.month, today.day, 탐색_종료.hour, 탐색_종료.minute)
                
                df_intervals = predict_future_intervals(current_duration, start_dt, end_dt)
                
                # 4. 화면 분할 레이아웃 출력
                col1, col2 = st.columns([1, 1])
                
                with col1:
                    st.subheader("⏰ 10분 구간별 최적 출발시간 예측 표")
                    st.dataframe(df_intervals, use_container_width=True, hide_index=True)
                    
                    st.subheader("📈 퇴근 시간대별 소요 시간 변동 추이")
                    chart_data = df_intervals.set_index("출발 시간")
                    st.line_chart(chart_data)
                    
                with col2:
                    st.subheader("🗺️ 카카오 내비 제공 실시간 추천 경로")
                    m = folium.Map(location=[(origin_lat+dest_lat)/2, (origin_lon+dest_lon)/2], zoom_start=12, tiles='CartoDB Positron')
                    
                    folium.Marker([origin_lat, origin_lon], popup="출발지", icon=folium.Icon(color='blue', icon='play')).add_to(m)
                    folium.Marker([dest_lat, dest_lon], popup="목적지", icon=folium.Icon(color='red', icon='stop')).add_to(m)
                    folium.PolyLine(locations=path_coords, color='#ffc107', weight=6, tooltip="실시간 경로").add_to(m)
                    
                    st_folium(m, width=700, height=520, key="map_hybrid")
            else:
                st.error("카카오 내비 서버와의 통신 리턴값이 비어있습니다. API 한도 또는 네트워크 상태를 확인하십시오.")
