# 필수 라이브러리 설치 (최초 1회 실행용 자동화)
!pip install polyline folium -q

import math
import random
import requests
import polyline
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import folium
from folium import plugins
from IPython.display import display, HTML
from datetime import datetime, timedelta, timezone
from google.colab import data_table

def get_real_road_path(waypoints):
    """OSRM API를 호출하여 실제 도로망을 따라가는 위경도 경로를 추출합니다."""
    # OSRM은 '경도,위도' 순서를 사용함
    coord_str = ";".join([f"{lon},{lat}" for lat, lon in waypoints])
    url = f"http://router.project-osrm.org/route/v1/driving/{coord_str}?overview=full&geometries=polyline"
    
    try:
        res = requests.get(url, timeout=5).json()
        encoded_poly = res['routes'][0]['geometry']
        # 암호화된 폴리라인을 [위도, 경도] 리스트로 디코딩
        return polyline.decode(encoded_poly)
    except Exception as e:
        print(f"라우팅 API 호출 실패. 직선 경로로 대체합니다. ({e})")
        return waypoints

def get_realtime_weather():
    """Open-Meteo API를 통해 현재 기상 상황을 수집하고 가중치를 반환합니다."""
    WEATHER_LAT, WEATHER_LON = 36.8065, 127.1522 
    url = f"https://api.open-meteo.com/v1/forecast?latitude={WEATHER_LAT}&longitude={WEATHER_LON}&current_weather=true&timezone=auto"
    try:
        res = requests.get(url, timeout=5).json()
        code = res['current_weather']['weathercode']
        if code in [0, 1, 2, 3]: return "맑음/구름", 1.0
        elif code in [51, 53, 55, 61, 63, 65, 80, 81, 82]: return "비 (강수)", 1.30 
        elif code in [71, 73, 75, 85, 86]: return "눈 (결빙)", 1.60 
        else: return "기타", 1.15
    except:
        return "수집 실패(기본값)", 1.0

def run_optimization(출발지, 목적지, 탐색_시작시간, 탐색_종료시간, 날짜_설정, 지정_날짜):
    """최적화 분석 및 시각화를 수행하는 메인 함수입니다."""
    
    data_table.enable_dataframe_formatter()
    plt.rc('font', family='NanumGothic')
    plt.rcParams['axes.unicode_minus'] = False

    KST = timezone(timedelta(hours=9), name="KST")
    now = datetime.now(KST)

    if "오늘" in 날짜_설정:
        target_date = now.date()
    else:
        target_date = datetime.strptime(지정_날짜, "%Y-%m-%d").date()

    weekday_idx = target_date.weekday()
    weekdays = ["월", "화", "수", "목", "금", "토", "일"]
    weekday_str = weekdays[weekday_idx]

    if weekday_idx == 4:
        day_weight = 1.25
        day_desc = f"{weekday_str}요일 (퇴근 러시 극심, 교통량 25% 증가)"
    elif weekday_idx >= 5:
        day_weight = 0.8
        day_desc = f"{weekday_str}요일 (주말 한산함, 교통량 20% 감소)"
    else:
        day_weight = 1.0
        day_desc = f"{weekday_str}요일 (일반 평일 교통량)"

    st_hour, st_min = map(int, 탐색_시작시간.split(':'))
    ed_hour, ed_min = map(int, 탐색_종료시간.split(':'))
    start_dt = datetime(target_date.year, target_date.month, target_date.day, st_hour, st_min)
    end_dt = datetime(target_date.year, target_date.month, target_date.day, ed_hour, ed_min)
    if end_dt < start_dt: end_dt += timedelta(days=1)

    print("="*90)
    print(f"📅 [분석 기준일] {target_date.strftime('%Y년 %m월 %d일')} | {day_desc}")

    current_weather, base_weather_weight = get_realtime_weather()
    print(f"📡 [실시간 기상] 상태: {current_weather} (기본 가중치: {base_weather_weight})")
    print("="*90 + "\n")

    options = []
    current = start_dt
    random.seed(int(start_dt.timestamp()))

    while current <= end_dt:
        hour_val = current.hour + current.minute / 60
        
        if hour_val <= 18.25:
            peak = math.exp(-((hour_val - 18.25) ** 2) / 0.04) 
        else:
            peak = math.exp(-((hour_val - 18.25) ** 2) / 0.12)
            
        noise = random.uniform(-1.0, 1.5)
        
        weather_impact_A = base_weather_weight
        weather_impact_B = 1.0 + (base_weather_weight - 1.0) * 0.4 

        dur_1 = (28 + 32 * peak + noise) * day_weight * weather_impact_A
        dur_2 = (35 + 10 * peak + (noise * 0.5)) * day_weight * weather_impact_B
        
        diff_minutes = abs((current.hour * 60 + current.minute) - (18 * 60 + 10))
        penalty = (diff_minutes ** 1.2) * 0.05 

        options.append({
            "departure_time": current.strftime("%H:%M"), 
            "dt_obj": current, 
            "route_name": "경로A (번영로/최단거리)", 
            "distance_km": 18.5, 
            "duration_min": round(dur_1, 1), 
            "score": round(dur_1 + penalty, 2)
        })
        options.append({
            "departure_time": current.strftime("%H:%M"), 
            "dt_obj": current, 
            "route_name": "경로B (탕정/우회도로)", 
            "distance_km": 20.2, 
            "duration_min": round(dur_2, 1), 
            "score": round(dur_2 + penalty, 2)
        })
        current += timedelta(minutes=1)

    df_all = pd.DataFrame(options).sort_values(["score", "duration_min"]).reset_index(drop=True)

    def make_window(dt):
        st_time = dt.replace(minute=(dt.minute // 10) * 10)
        return f"{st_time.strftime('%H:%M')}~{(st_time + timedelta(minutes=9)).strftime('%H:%M')}"

    df_all['10분구간'] = df_all['dt_obj'].apply(make_window)
    summary = df_all.sort_values(["10분구간", "score"]).groupby("10분구간", as_index=False).first()

    display_df = summary[["10분구간", "departure_time", "route_name", "distance_km", "duration_min"]].rename(
        columns={
            "departure_time": "최적 출발시간", 
            "route_name": "추천 경로", 
            "distance_km": "거리(km)",
            "duration_min": "최종 예상시간(분)"
        }
    )
    display_df.insert(0, '요일', weekday_str)
    display_df.insert(0, '날짜', target_date.strftime('%Y-%m-%d'))
    display_df['기상상황'] = current_weather

    print("="*95)
    print(" ⏰ 고도화 모델 적용 - 10분 구간별 최적 출발시간 산출 결과")
    print("="*95)
    display(HTML(display_df.to_html(index=False, justify='center')))
    display_df.to_csv("최적퇴근시간표_실제도로망.csv", index=False, encoding='utf-8-sig')

    fig, ax = plt.subplots(figsize=(8, 3))
    colors = {'경로A (번영로/최단거리)': '#dc3545', '경로B (탕정/우회도로)': '#28a745'}
    for route in df_all['route_name'].unique():
        sub = df_all[df_all['route_name'] == route].sort_values('dt_obj')
        ax.plot(sub['dt_obj'], sub['duration_min'], marker='o', markersize=2, linewidth=1.5, label=route, color=colors.get(route))

    ax.set_title(f'📈 1분 단위 소요시간 추이 분석', fontweight='bold', fontsize=11)
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60, 10))) 
    plt.xticks(rotation=45)
    plt.legend()
    plt.grid(True, linestyle='--', alpha=0.4)
    plt.tight_layout()
    plt.show()

    print("\n🗺️ 실제 도로망(OSRM 기반) 경로 시각화")
    origin_c, dest_c = [36.8193, 127.0585], [36.8065, 127.1522]
    waypoint_A = [36.7946, 127.1044] # 경로A 경유지
    waypoint_B = [36.8200, 127.1100] # 경로B 경유지

    figure = folium.Figure(width=800, height=400)
    m = folium.Map(location=[(origin_c[0]+dest_c[0])/2, (origin_c[1]+dest_c[1])/2], zoom_start=13, tiles='CartoDB Positron').add_to(figure)
    folium.Marker(origin_c, popup=f'출발지', icon=folium.Icon(color='blue')).add_to(m)
    folium.Marker(dest_c, popup=f'목적지', icon=folium.Icon(color='red')).add_to(m)

    # 실제 도로 좌표 계산
    path_A = get_real_road_path([origin_c, waypoint_A, dest_c])
    path_B = get_real_road_path([origin_c, waypoint_B, dest_c])

    plugins.AntPath(locations=path_A, color=colors.get("경로A (번영로/최단거리)"), weight=5, tooltip="경로A (번영로/최단거리)").add_to(m)
    plugins.AntPath(locations=path_B, color=colors.get("경로B (탕정/우회도로)"), weight=5, tooltip="경로B (탕정/우회도로)").add_to(m)

    display(figure)


# ==========================================
# ⭐️ 사용자 입력칸 (코드 실행을 위한 파라미터)
# ==========================================
#@title 🚗 시스템 입력 변수 설정

출발지 = "\uC0BC\uC131\uB514\uC2A4\uD50C\uB808\uC774 \uC544\uC0B01\uCEA0\uD37C\uC2A4" #@param {type:"string"}
목적지 = "\uCDA9\uCCAD\uB0A8\uB3C4 \uCC9C\uC548\uC2DC \uB3D9\uB0A8\uAD6C \uC131\uD669\uB85C 40" #@param {type:"string"}
탐색_시작시간 = "17:30" #@param {type:"string"}
탐색_종료시간 = "19:00" #@param {type:"string"}

날짜_설정 = "\uC624\uB298 (\uC2E4\uC2DC\uAC04 \uC694\uC77C \uC790\uB3D9 \uBC18\uC601)" #@param ["오늘 (실시간 요일 자동 반영)", "특정 날짜 직접 지정"]
지정_날짜 = "2026-05-25" #@param {type:"date"}

# 모델 실행부
run_optimization(출발지, 목적지, 탐색_시작시간, 탐색_종료시간, 날짜_설정, 지정_날짜)
