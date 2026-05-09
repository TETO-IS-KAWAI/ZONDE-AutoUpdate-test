"""
기상청 API 허브 - Skew-T Log-P 단열선도 자동 생성
GitHub Actions에서 매일 실행 → docs/ 에 PNG(다크/라이트) + JSON 저장 → README 갱신

변경 이력:
  v2.1 - 한글 폰트 수정 (FontProperties 직접 경로 방식)
       - 다크/라이트 테마 PNG 각각 생성
       - 범례 항목 수정 및 추가 (풍향 바브, 건조/습윤 단열선, 혼합비선)
       - 시각화 색상·두께 개선
       - 보안: API URL 마스킹, 환경변수 검증 강화
       - 라이브러리 버전 고정 (requirements.txt)
       - SRH(폭풍상대소용돌이도) 파라미터 추가
"""

# ── 표준 라이브러리 ──────────────────────────────────────────────────────────
import codecs
import json
import os
import re
import shutil
import sys
import urllib.parse
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

# ── 서드파티 ─────────────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.lines import Line2D
import numpy as np
import pandas as pd
import requests

import metpy.calc as mpcalc
from metpy.plots import SkewT
from metpy.units import units

# ═══════════════════════════════════════════════════════════════════════════════
# 0. 보안 설정
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_api_url(url: str) -> str:
    """
    KMA_API_URL 환경변수 검증.
      - 빈 문자열 거부
      - http/https 스킴만 허용
      - 허용된 도메인(기상청 API 허브)만 허용
      - 로그에 API 키 노출 방지
    """
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        print("[✗] KMA_API_URL 파싱 실패 — 올바른 URL 형식이 아닙니다.")
        sys.exit(1)

    if parsed.scheme not in ("http", "https"):
        print(f"[✗] 허용되지 않는 URL 스킴: {parsed.scheme}")
        sys.exit(1)

    allowed_hosts = (
        "apihub.kma.go.kr",
        "data.kma.go.kr",
        "apis.data.go.kr",
    )
    host = parsed.hostname or ""
    if not any(host == h or host.endswith("." + h) for h in allowed_hosts):
        print(f"[✗] 허용되지 않는 호스트: {host}")
        print(f"    허용 도메인: {', '.join(allowed_hosts)}")
        sys.exit(1)

    return url


def _mask_url(url: str) -> str:
    """로그 출력 시 쿼리스트링(API 키 포함) 마스킹"""
    try:
        parsed = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse(parsed._replace(query="***"))
    except Exception:
        return "***"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 설정
# ═══════════════════════════════════════════════════════════════════════════════

_RAW_API_URL = os.environ.get("KMA_API_URL", "")
API_URL      = _validate_api_url(_RAW_API_URL)

OUTPUT_DIR = Path("docs")
OUTPUT_DIR.mkdir(exist_ok=True)

RAW_PATH        = OUTPUT_DIR / "original.txt"
PLOT_DARK_PATH  = OUTPUT_DIR / "skewt_dark.png"
PLOT_LIGHT_PATH = OUTPUT_DIR / "skewt_light.png"
PLOT_PATH       = OUTPUT_DIR / "skewt_latest.png"   # 하위 호환 (다크 복사본)
META_PATH       = OUTPUT_DIR / "meta.json"

KST       = timezone(timedelta(hours=9))
NOW       = datetime.now(KST)
DATE_STR  = NOW.strftime("%Y년 %m월 %d일 %H:%M KST")
FILE_DATE = NOW.strftime("%Y%m%d")

# ── 한글 폰트 설정 ────────────────────────────────────────────────────────────
# matplotlib font cache 갱신 문제를 우회: 폰트 파일 경로를 직접 지정
_FONT_CANDIDATES = [
    # Linux (Ubuntu/Debian) — GitHub Actions 환경
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    # macOS
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/NanumGothic.ttf",
    # Windows
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/gulim.ttc",
]
_FONT_PATH = next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)

if _FONT_PATH:
    _KO_FP = fm.FontProperties(fname=_FONT_PATH)
    print(f"[✓] 한글 폰트: {_FONT_PATH}")
else:
    _KO_FP = fm.FontProperties()
    print("[!] 한글 폰트를 찾지 못했습니다 — 기본 폰트 사용 (한글 깨짐 가능)")


def kfont(size: float = 10, bold: bool = False) -> fm.FontProperties:
    """크기·굵기를 지정한 한글 FontProperties 반환"""
    fp = fm.FontProperties(fname=_KO_FP.get_file(), size=size)
    if bold:
        fp.set_weight("bold")
    return fp


# ── 테마별 색상 팔레트 ────────────────────────────────────────────────────────
_DARK = dict(
    bg       = "#0d1117",
    surface  = "#161b22",
    border   = "#30363d",
    text     = "#e6edf3",
    subtext  = "#8b949e",
    # 데이터 곡선
    temp     = "#ff6b6b",
    dew      = "#4fc3f7",
    parcel   = "#ffd54f",
    # 단열선·혼합비선
    dry_adi  = "#4caf50",
    moist    = "#26a69a",
    mix      = "#ff8f00",
    # 특수 레벨
    lcl      = "#ffeb3b",
    lfc      = "#69f0ae",
    el       = "#40c4ff",
    # CAPE/CIN 음영
    cape_sh  = "#ff6b6b",
    cin_sh   = "#4fc3f7",
    # 풍향 바브
    barb     = "#c8d1d9",
)

_LIGHT = dict(
    bg       = "#ffffff",
    surface  = "#f6f8fa",
    border   = "#d0d7de",
    text     = "#1f2328",
    subtext  = "#57606a",
    temp     = "#c0392b",
    dew      = "#1565c0",
    parcel   = "#f57f17",
    dry_adi  = "#2e7d32",
    moist    = "#00695c",
    mix      = "#e65100",
    lcl      = "#f9a825",
    lfc      = "#1b5e20",
    el       = "#0277bd",
    cape_sh  = "#c0392b",
    cin_sh   = "#1565c0",
    barb     = "#424242",
)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 데이터 수집
# ═══════════════════════════════════════════════════════════════════════════════

def download_raw(url: str, path: Path) -> None:
    """API URL에서 원시 파일 다운로드"""
    print(f"[→] 다운로드: {_mask_url(url)}")
    session = requests.Session()
    session.max_redirects = 5

    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        print("[✗] 다운로드 타임아웃 (30초 초과)")
        sys.exit(1)
    except requests.exceptions.SSLError as e:
        print(f"[✗] SSL 오류: {e}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"[✗] HTTP 오류: {e}")
        sys.exit(1)

    content_type = resp.headers.get("Content-Type", "")
    if "html" in content_type.lower():
        print("[✗] HTML 응답 반환됨 — API URL 또는 키를 확인하세요.")
        sys.exit(1)

    path.write_bytes(resp.content)
    print(f"[✓] 다운로드 완료: {len(resp.content):,} bytes")


def recode(path: Path, src: str = "euc-kr", dst: str = "utf-8") -> None:
    """EUC-KR → UTF-8 재인코딩 (깨진 바이트는 replace 처리)"""
    with codecs.open(path, "r", src, errors="replace") as f:
        text = f.read()
    with codecs.open(path, "w", dst) as f:
        f.write(text)
    print(f"[✓] 인코딩 변환: {src} → {dst}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 데이터 파싱
# ═══════════════════════════════════════════════════════════════════════════════

COLS = ["YYMMDDHHMI", "STN", "PA", "GH", "TA", "TD", "WD", "WS", "FLAG"]


def load_data(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path,
        sep=r"\s+",
        comment="#",
        header=None,
        names=COLS,
        na_values=[-999.0, -9999.0],
    )
    df["datetime"] = pd.to_datetime(
        df["YYMMDDHHMI"], format="%Y%m%d%H%M", errors="coerce")
    df = (
        df.dropna(subset=["PA", "TA", "TD"])
          .sort_values("PA", ascending=False)
          .reset_index(drop=True)
    )
    print(f"[✓] 데이터 로드: {len(df)}개 레벨  "
          f"({df['PA'].max():.0f} ~ {df['PA'].min():.0f} hPa)")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 기상 파라미터 계산
# ═══════════════════════════════════════════════════════════════════════════════

def _safe(fn, *args, **kw):
    """MetPy 계산 실패 또는 nan 결과 시 None 반환"""
    try:
        result = fn(*args, **kw)
        if isinstance(result, tuple) and len(result) == 2:
            if np.isnan(result[0].magnitude) or np.isnan(result[1].magnitude):
                print(f"    [!] {fn.__name__}: 조건 불충족 (nan) → None")
                return None
        return result
    except Exception as e:
        print(f"    [!] {fn.__name__} 실패: {e}")
        return None


def compute(df: pd.DataFrame) -> dict:
    p  = df["PA"].values * units.hPa
    t  = df["TA"].values * units.degC
    td = df["TD"].values * units.degC
    gh = df["GH"].values
    wd = df["WD"].values
    ws = df["WS"].values

    prof          = mpcalc.parcel_profile(p, t[0], td[0]).to("degC")
    cape_q, cin_q = mpcalc.cape_cin(p, t, td, prof)

    lcl  = _safe(mpcalc.lcl, p[0], t[0], td[0])
    lfc  = _safe(mpcalc.lfc, p, t, td, which="bottom")
    el   = _safe(mpcalc.el, p, t, td)
    pwat = _safe(mpcalc.precipitable_water, p, td)

    # 0–6 km 벌크 윈드시어
    shr06 = None
    srh   = None
    mask = ~np.isnan(wd) & ~np.isnan(ws)
    if mask.sum() >= 2:
        try:
            u, v = mpcalc.wind_components(
                ws[mask] * units("m/s"),
                wd[mask] * units.degrees,
            )
            shr06 = _safe(mpcalc.bulk_shear, p[mask], u, v,
                          depth=6000 * units.meter)
            srh_result = _safe(mpcalc.storm_relative_helicity,
                               p[mask], u, v, depth=3000 * units.meter)
            if srh_result is not None:
                # (positive_srh, negative_srh, total_srh)
                srh = float(srh_result[2].magnitude) if isinstance(srh_result, tuple) \
                      else float(srh_result.magnitude)
        except Exception as e:
            print(f"    [!] 윈드 파라미터 계산 실패: {e}")

    return dict(
        p=p, t=t, td=td, gh=gh, wd=wd, ws=ws,
        prof=prof, cape=cape_q, cin=cin_q,
        lcl=lcl, lfc=lfc, el=el,
        pwat=pwat, shr06=shr06, srh=srh,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 그래프 생성
# ═══════════════════════════════════════════════════════════════════════════════

def draw_skewt(params: dict, date_str: str, save_path_base: Path):
    """
    Skew-T Log-P 단열선도 생성 및 저장 (다크/라이트 통합)
    save_path_base: 파일명 접두사 (예: Path("docs/skewt"))
    """
    p, t, td, prof = params["p"], params["t"], params["td"], params["prof"]
    gh, wd, ws     = params["gh"], params["wd"], params["ws"]
    lcl, lfc, el   = params["lcl"], params["lfc"], params["el"]
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


    meta = {
        "generated" : DATE_STR,
        "file_date" : FILE_DATE,
        "cape"      : round(float(params["cape"].magnitude), 1),
        "cin"       : round(float(params["cin"].magnitude),  1),
        "lcl_p"     : _pv(params["lcl"], 0),
        "lcl_t"     : _pv(params["lcl"], 1),
        "lfc_p"     : _pv(params["lfc"], 0),
        "lfc_t"     : _pv(params["lfc"], 1),
        "el_p"      : _pv(params["el"],  0),
        "el_t"      : _pv(params["el"],  1),
        "pwat"      : (round(float(params["pwat"].to("mm").magnitude), 1)
                       if params["pwat"] is not None else None),
        "shr06"     : _shr_mag(params["shr06"]),
        "srh"       : (round(params["srh"], 1)
                       if params["srh"] is not None else None),
        "levels"    : table,
    }
    META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[✓] 메타데이터 저장: {META_PATH}  ({len(table)}개 레벨)")


# ═══════════════════════════════════════════════════════════════════════════════
# 7. README 갱신
# ═══════════════════════════════════════════════════════════════════════════════

def update_readme(params: dict) -> None:
    cape = params["cape"].magnitude
    cin  = params["cin"].magnitude

    def badge(label, value, color):
        v = f"{value:.0f}%20J%2Fkg" if value is not None else "N%2FA"
        return f"![{label}](https://img.shields.io/badge/{label}-{v}-{color})"

    badges = badge("CAPE", cape, "orange") + "  " + badge("CIN", cin, "blue")

    block = (
        "<!-- SKEWT_AUTO_START -->\n"
        f"### 최신 단열선도 — {DATE_STR}\n\n"
        f"{badges}\n\n"
        "| 다크 테마 | 라이트 테마 |\n"
        "|-----------|-------------|\n"
        "| ![Skew-T Dark](docs/skewt_dark.png) |"
        " ![Skew-T Light](docs/skewt_light.png) |\n"
        "<!-- SKEWT_AUTO_END -->"
    )

    readme = Path("README.md")
    if not readme.exists():
        readme.write_text(
            "# ZONDE API Analyze\n\n"
            "기상청 API 허브를 이용한 Skew-T Log-P 단열선도 자동 생성\n\n" + block,
            encoding="utf-8",
        )
    else:
        content = readme.read_text(encoding="utf-8")
        pattern = r"<!-- SKEWT_AUTO_START -->.*?<!-- SKEWT_AUTO_END -->"
        if re.search(pattern, content, re.DOTALL):
            content = re.sub(pattern, block, content, flags=re.DOTALL)
        else:
            content += "\n\n" + block
        readme.write_text(content, encoding="utf-8")

    print("[✓] README.md 갱신 완료")

# skewt_plot.py 내 적절한 위치에 추가
def save_export_files(df, date_str):
    # CSV 저장
    df.to_csv(f"docs/sounding_{date_str}.csv", index=False, encoding='utf-8-sig')
    # XLSX 저장 (requirements.txt에 openpyxl 추가 필요)
    df.to_excel(f"docs/sounding_{date_str}.xlsx", index=False)
    # TXT 저장
    with open(f"docs/sounding_{date_str}.txt", "w", encoding='utf-8') as f:
        f.write(df.to_string(index=False))
# ═══════════════════════════════════════════════════════════════════════════════
# 진입점
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if not API_URL:
        print("[✗] 환경변수 KMA_API_URL 이 설정되지 않았습니다.")
        print("    GitHub: Settings → Secrets → KMA_API_URL 을 등록하세요.")
        sys.exit(1)

    sep = "=" * 56
    print(f"\n{sep}\n  Skew-T Log-P 단열선도 자동 생성\n  {DATE_STR}\n{sep}\n")

    download_raw(API_URL, RAW_PATH)
    recode(RAW_PATH)
    df     = load_data(RAW_PATH)
    params = compute(df)

    print(f"\n  CAPE = {params['cape'].magnitude:.1f} J/kg")
    print(f"  CIN  = {params['cin'].magnitude:.1f} J/kg")
    for key, label in [("lcl", "LCL"), ("lfc", "LFC"), ("el", "EL")]:
        v = params[key]
        if v:
            print(f"  {label}  = {v[0].to('hPa').magnitude:.0f} hPa"
                  f" / {v[1].to('degC').magnitude:.1f} °C")
        else:
            print(f"  {label}  = 없음")
    if params["pwat"] is not None:
        print(f"  PWAT = {params['pwat'].to('mm').magnitude:.1f} mm")
    if params["srh"] is not None:
        print(f"  SRH  = {params['srh']:.1f} m²/s²")

    print()
    draw_skewt(params, DATE_STR, PLOT_DARK_PATH,  _DARK)
    draw_skewt(params, DATE_STR, PLOT_LIGHT_PATH, _LIGHT)

    # 하위 호환: 기존 skewt_latest.png = 다크 버전
    shutil.copy2(PLOT_DARK_PATH, PLOT_PATH)
    print(f"[✓] 하위 호환 복사: {PLOT_PATH}")

    save_meta(df, params)
    update_readme(params)

    print(f"\n{sep}\n  완료!\n{sep}\n")


if __name__ == "__main__":
    main()
