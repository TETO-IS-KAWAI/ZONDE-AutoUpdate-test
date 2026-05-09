"""
기상청 API 허브 - Skew-T Log-P 단열선도 자동 생성 스크립트
매일 GitHub Actions에서 실행되어 그래프를 생성하고 저장합니다.
"""

import os
import sys
import requests
import codecs
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # GUI 없는 환경에서 실행
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# --- 한글 폰트 설정 시작 ---
# 시스템에 설치된 나눔바른고딕 폰트 경로 설정
font_path = '/usr/share/fonts/truetype/nanum/NanumBarunGothic.ttf'

# 만약 로컬 환경(Windows 등)에서도 실행한다면 경로를 체크하거나 예외처리를 합니다.
if os.path.exists(font_path):
    font_prop = fm.FontProperties(fname=font_path)
    plt.rcParams['font.family'] = font_prop.get_name()
else:
    # 윈도우 환경인 경우 (예시)
    plt.rcParams['font.family'] = 'Malgun Gothic'

# 마이너스 기호 깨짐 방지
plt.rcParams['axes.unicode_minus'] = False
# --- 한글 폰트 설정 끝 ---

import matplotlib.patches as mpatches
from pathlib import Path
from datetime import datetime, timezone, timedelta
import json
import warnings
warnings.filterwarnings('ignore')

import metpy.calc as mpcalc
from metpy.plots import SkewT
from metpy.units import units

# ── 환경변수에서 API URL 읽기 (GitHub Secrets에서 주입) ──────────────────────
API_URL = os.environ.get("KMA_API_URL", "")

# 출력 디렉터리
OUTPUT_DIR = Path("docs")
OUTPUT_DIR.mkdir(exist_ok=True)

KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
DATE_STR = NOW.strftime("%Y년 %m월 %d일 %H:%M KST")
FILE_DATE = NOW.strftime("%Y%m%d")

ORIGINAL_PATH = OUTPUT_DIR / "original.txt"
PLOT_PATH     = OUTPUT_DIR / "skewt_latest.png"
META_PATH     = OUTPUT_DIR / "meta.json"


# ─────────────────────────────────────────────────────────────────────────────
def download_file(file_url: str, save_path: Path):
    """API에서 파일 다운로드"""
    resp = requests.get(file_url, timeout=30)
    resp.raise_for_status()
    save_path.write_bytes(resp.content)
    print(f"[✓] 다운로드 완료: {save_path}")


def convert_encoding(file_path: Path, from_enc="euc-kr", to_enc="utf-8"):
    """EUC-KR → UTF-8 변환"""
    with codecs.open(file_path, "r", from_enc) as f:
        content = f.read()
    with codecs.open(file_path, "w", to_enc) as f:
        f.write(content)
    print(f"[✓] 인코딩 변환 완료: {from_enc} → {to_enc}")


def load_sounding_data(path: Path) -> pd.DataFrame:
    """관측 데이터 로드 및 정리"""
    df = pd.read_csv(
        path,
        sep=r'\s+',
        comment="#",
        header=None,
        names=["YYMMDDHHMI", "STN", "PA", "GH", "TA", "TD", "WD", "WS", "FLAG"],
        na_values=-999.0
    )
    df["datetime"] = pd.to_datetime(df["YYMMDDHHMI"], format="%Y%m%d%H%M")
    df_clean = df.dropna(subset=["PA", "TA", "TD"])
    df_clean = df_clean.sort_values("PA", ascending=False).reset_index(drop=True)
    print(f"[✓] 데이터 로드 완료: {len(df_clean)}개 레벨")
    return df_clean


def compute_params(df):
    """기상 파라미터 계산"""
    p   = df["PA"].values * units.hPa
    t   = df["TA"].values * units.degC
    td  = df["TD"].values * units.degC

    prof = mpcalc.parcel_profile(p, t[0], td[0]).to("degC")
    cape, cin = mpcalc.cape_cin(p, t, td, prof)

    try:
        lcl_p, lcl_t = mpcalc.lcl(p[0], t[0], td[0])
    except Exception:
        lcl_p, lcl_t = None, None

    try:
        lfc_p, lfc_t = mpcalc.lfc(p, t, td, which="bottom")
    except Exception:
        lfc_p, lfc_t = None, None

    try:
        el_p, el_t = mpcalc.el(p, t, td)
    except Exception:
        el_p, el_t = None, None

    return dict(p=p, t=t, td=td, prof=prof,
                cape=cape, cin=cin,
                lcl_p=lcl_p, lcl_t=lcl_t,
                lfc_p=lfc_p, lfc_t=lfc_t,
                el_p=el_p,   el_t=el_t)


def draw_skewt(params: dict, date_str: str, save_path_base: Path):
    """
    Skew-T Log-P 단열선도 생성 및 저장 (다크/라이트 통합)
    save_path_base: 파일명 접두사 (예: Path("docs/skewt"))
    """
    p, t, td, prof = params["p"], params["t"], params["td"], params["prof"]
    cape = params["cape"].magnitude
    cin  = params["cin"].magnitude

    # 테마 리스트 (수정 요청사항 3번 반영)
    themes = ['dark', 'light']

    for theme in themes:
        # [중요] 이전 루프의 스타일이나 잔상을 완전히 제거
        plt.close('all')
        
        if theme == 'dark':
            plt.style.use('dark_background')
            bg_color = "#0d1117"     # 배경색
            grid_color = "#333333"   # 격자색
            text_color = "white"     # 텍스트색
            box_color = "#1e2a38"    # 텍스트 박스색
        else:
            plt.style.use('default') # 기본 테마(화이트)로 초기화
            bg_color = "white"
            grid_color = "#dddddd"
            text_color = "black"
            box_color = "#f0f0f0"

        fig = plt.figure(figsize=(9, 11), facecolor=bg_color)
        skew = SkewT(fig, rotation=45)
        ax = skew.ax
        ax.set_facecolor(bg_color)

        # 5번: 시각화 이미지 색상 및 두께 개선
        # 온도 / 이슬점 / 기층 상승 곡선
        skew.plot(p, t, "tomato", linewidth=2.2, label="기온 (°C)")
        skew.plot(p, td, "#4fc3f7", linewidth=2.2, linestyle="dashed", label="이슬점 (°C)")
        skew.plot(p, prof, "#ffd54f", linewidth=1.6, linestyle="dashed", label="기층 상승 곡선")

        # 단열선 및 배경 격자
        skew.plot_dry_adiabats(colors="#3a6e3a", linewidth=0.8, linestyle="--", alpha=0.4)
        skew.plot_moist_adiabats(colors="#1a6e4e", linewidth=0.8, linestyle="-.", alpha=0.4)
        skew.plot_mixing_lines(colors="#6e3a1a", linewidth=0.7, linestyle=":", alpha=0.4)

        for pres in [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]:
            ax.axhline(pres, lw=0.6, color=grid_color, zorder=0)

        # CAPE / CIN 음영
        skew.shade_cape(p, t, prof, facecolor="tomato", alpha=0.3)
        skew.shade_cin(p, t, prof, facecolor="steelblue", alpha=0.3)

        # 2번: 마커 및 Legend 수정 (zorder 상향으로 가려짐 방지)
        marker_kw = dict(zorder=10, s=80, transform=ax.get_xaxis_transform())
        if params.get("lcl_p") is not None:
            ax.scatter(params["lcl_t"].magnitude, params["lcl_p"].magnitude, 
                       color="yellow", marker="^", label="LCL", **marker_kw)
        if params.get("lfc_p") is not None:
            ax.scatter(params["lfc_t"].magnitude, params["lfc_p"].magnitude, 
                       color="lime", marker="s", label="LFC", **marker_kw)
        if params.get("el_p") is not None:
            ax.scatter(params["el_t"].magnitude, params["el_p"].magnitude, 
                       color="cyan", marker="D", label="EL", **marker_kw)

        # 6번: 텍스트 및 레이블 수정 (한글 폰트 명시적 적용 권장)
        ax.set_ylim(1050, 100)
        ax.set_xlim(-40, 45)
        ax.set_xlabel("기온 (°C)", color=text_color, fontsize=11)
        ax.set_ylabel("기압 (hPa)", color=text_color, fontsize=11)
        ax.tick_params(colors=text_color)

        ax.set_title(f"Skew-T Log-P 단열선도 ({theme.upper()})\n{date_str}", 
                     color=text_color, fontsize=14, fontweight="bold", pad=15)

        # 하단 정보 텍스트 박스
        info_text = f"CAPE : {cape:6.1f} J/kg\nCIN  : {cin:6.1f} J/kg"
        ax.text(0.03, 0.03, info_text, transform=ax.transAxes, fontsize=10, 
                color=text_color, family='monospace', verticalalignment="bottom",
                bbox=dict(facecolor=box_color, edgecolor=grid_color, alpha=0.8, pad=6))

        # 범례
        ax.legend(loc="upper right", fontsize=9, facecolor=box_color, 
                  edgecolor=grid_color, labelcolor=text_color)

        # 3번: 다크/화이트 구분 저장
        final_save_path = f"{save_path_base}_{theme}.png"
        plt.tight_layout()
        # [중요] facecolor를 지정해야 화이트/다크 배경이 파일에 제대로 기록됨
        plt.savefig(final_save_path, dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor())
        print(f"[✓] {theme} 그래프 저장 완료: {final_save_path}")

    return cape, cin


def save_meta(cape, cin, date_str, file_date):
    """메타데이터 JSON 저장 (웹페이지에서 활용)"""
    meta = {
        "generated": date_str,
        "file_date": file_date,
        "cape": round(float(cape), 1),
        "cin":  round(float(cin),  1),
    }
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[✓] 메타데이터 저장 완료: {META_PATH}")


def update_readme(cape, cin, date_str):
    """README.md의 그래프 이미지 갱신"""
    readme = Path("README.md")
    badge_cape = f"![CAPE](https://img.shields.io/badge/CAPE-{cape:.0f}%20J%2Fkg-orange)"
    badge_cin  = f"![CIN](https://img.shields.io/badge/CIN-{cin:.0f}%20J%2Fkg-blue)"
    block = (
        "<!-- SKEWT_AUTO_START -->\n"
        f"### 최신 단열선도 ({date_str})\n\n"
        f"{badge_cape}  {badge_cin}\n\n"
        "![Skew-T Log-P](docs/skewt_latest.png)\n"
        "<!-- SKEWT_AUTO_END -->"
    )

    if not readme.exists():
        readme.write_text(
            "# ZONDE API Analyze\n\n기상청 API 허브를 이용한 Skew-T Log-P 단열선도 자동 생성\n\n" + block,
            encoding="utf-8"
        )
    else:
        content = readme.read_text(encoding="utf-8")
        import re
        pattern = r"<!-- SKEWT_AUTO_START -->.*?<!-- SKEWT_AUTO_END -->"
        if re.search(pattern, content, flags=re.DOTALL):
            content = re.sub(pattern, block, content, flags=re.DOTALL)
        else:
            content += "\n\n" + block
        readme.write_text(content, encoding="utf-8")
    print("[✓] README.md 갱신 완료")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    if not API_URL:
        print("[✗] 환경변수 KMA_API_URL 이 설정되지 않았습니다.")
        sys.exit(1)

    print(f"\n{'='*50}")
    print(f"  Skew-T Log-P 단열선도 자동 생성")
    print(f"  {DATE_STR}")
    print(f"{'='*50}\n")

    # 1. API 데이터 수집
    download_file(API_URL, ORIGINAL_PATH)
    convert_encoding(ORIGINAL_PATH)

    # 2. 데이터 처리
    df = load_sounding_data(ORIGINAL_PATH)
    params = compute_params(df)

    # 3. 그래프 생성
    cape, cin = draw_skewt(params, DATE_STR, PLOT_PATH)

    # 4. 메타데이터 저장
    save_meta(cape, cin, DATE_STR, FILE_DATE)

    # 5. README 갱신
    update_readme(cape, cin, DATE_STR)

    print(f"\n[완료] CAPE={cape:.1f} J/kg  CIN={cin:.1f} J/kg\n")


if __name__ == "__main__":
    main()
