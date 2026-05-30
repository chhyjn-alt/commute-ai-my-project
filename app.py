import streamlit as st
import pandas as pd
import folium
import requests
import time
from datetime import datetime
from streamlit_folium import st_folium
from geopy.geocoders import Nominatim

# ==========================================
# 1. 페이지 및 환경 설정
# ==========================================
st.set_page_config(page_title="종합 교통/물류 최적화 시스템", page_icon="⚙️", layout="wide")

if 'run_status' not in st.session_state:
    st.session_state.run_status = False

def start_analysis():
    st.session_state.run_status = True

# 발급받은 카카오 API 키
KAKAO_API_KEY = "df68bf65618592b6d685caec6521432f"

# ==========================================
# 2. 핵심 로직 함수
# ==========================================
def get_lat_lon(address):
    """주소를 위도, 경도로 변환합니다."""
    geolocator = Nominatim(user_agent="traffic_optimizer")
    try:
        location = geolocator.geocode(address)
        return (location.latitude, location.longitude) if location else (None, None)
    except:
        return None, None

def get_kakao_navi_route(origin_lat, origin_lon, dest_lat, dest_lon):
    """카카오 모빌리티 길찾기 API (실시간 교통 반영)"""
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    
    # origin과 destination은 "경도,위도" 순서로 입력해야 함
    params = {
        "origin": f"{origin_lon},{origin_lat}",
        "destination": f"{dest_lon},{dest_lat}",
        "priority": "RECOMMEND" # 최적경로 (실시간 교통 반영)
    }
    
    try:
        res = requests.get(url, headers=headers, params=params).json()
        if res['routes'][0]['result_code'] == 0:
            summary = res['routes'][0]['summary']
            distance = summary['distance'] / 1000 # km 단위 변환
            duration = summary['duration'] / 60   # 분 단위 변환
            
            # 경로 좌표 추출 (지도에 그리기 위함)
            path = []
            for section in res['routes'][0]['sections']:
                for road in section['roads']:
                    for i in range(0, len(road['vertexes']), 2):
                        path.append([road['vertexes'][i+1], road['vertexes'][i]]) # [위도, 경도]
            return round(distance, 1), round(duration, 1), path
    except:
        pass
    return None, None, []

# ==========================================
# 3. 사이드바 제어 및 화면 렌더링
# ==========================================
with st.sidebar:
    st.header("📱 시스템 메뉴")
    선택메뉴 = st.selectbox("실행할 프로그램 선택", ["🚗 카카오 실시간 내비 연동기", "🍻 회식장소 추천기 (생략)"])
    st.markdown("---")

if 선택메뉴 == "🚗 카카오 실시간 내비 연동기":
    st.title("🚗 카카오 내비 API 실시간 소요시간 분석기")
    st.markdown("실제 카카오 모빌리티 서버에 접속하여 **'현재 시각 기준'** 진짜 소요시간과 최적 경로를 추출합니다.")
    
    with st.sidebar:
        st.subheader("⚙️ 주소 설정")
        출발지 = st.text_input("출발지 (도로명 주소 권장)", "충청남도 아산시 탕정면 삼성로 1") # 삼성디스플레이 아산1캠퍼스 도로명
        목적지 = st.text_input("목적지", "충청남도 천안시 동남구 성황로 40")
        
        st.markdown("---")
        st.button("🔍 실시간 카카오 내비 스캔", on_click=start_analysis, type="primary")

    if st.session_state.run_status:
        with st.spinner("카카오 모빌리티 서버에서 실시간 교통상황을 가져오고 있습니다..."):
            
            # 1. 주소를 좌표로 변환
            origin_lat, origin_lon = get_lat_lon(출발지)
            dest_lat, dest_lon = get_lat_lon(목적지)
            
            if not origin_lat or not dest_lat:
                st.error("입력하신 주소의 좌표를 찾을 수 없습니다. 정확한 도로명 주소를 입력해주세요.")
            else:
                # 2. 카카오 내비 API 호출 (실시간 소요시간 및 경로 추출)
                distance, duration, path_coords = get_kakao_navi_route(origin_lat, origin_lon, dest_lat, dest_lon)
                
                if distance:
                    now = datetime.now().strftime("%H시 %M분")
                    st.success("✅ 카카오 실시간 교통상황 연동 성공!")
                    
                    # 3. 대시보드 출력
                    col1, col2, col3 = st.columns(3)
                    col1.metric("기준 시간", now)
                    col2.metric("실시간 예상 소요 시간", f"{int(duration)}분")
                    col3.metric("최적 주행 거리", f"{distance} km")
                    
                    # 4. 지도에 실제 경로 그리기
                    st.subheader("🗺️ 카카오 내비 안내 경로 (실시간 교통 반영)")
                    m = folium.Map(location=[(origin_lat+dest_lat)/2, (origin_lon+dest_lon)/2], zoom_start=12, tiles='CartoDB Positron')
                    
                    folium.Marker([origin_lat, origin_lon], popup="출발지", icon=folium.Icon(color='blue', icon='play')).add_to(m)
                    folium.Marker([dest_lat, dest_lon], popup="목적지", icon=folium.Icon(color='red', icon='stop')).add_to(m)
                    
                    # 카카오가 알려준 디테일한 꺾임(Vertex) 좌표대로 선을 그림
                    folium.PolyLine(locations=path_coords, color='#dc3545', weight=6, tooltip="카카오 최적 경로").add_to(m)
                    
                    st_folium(m, width=1000, height=500)
                    
                    st.caption("※ 본 데이터는 Kakao Mobility API를 통해 실시간으로 수집된 실제 주행 데이터입니다.")
                else:
                    st.error("경로를 탐색할 수 없습니다. 두 지점이 너무 멀거나 차로 갈 수 없는 곳입니다.")
