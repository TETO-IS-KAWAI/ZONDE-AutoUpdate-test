"""
기상청 API 허브 - Skew-T Log-P 단열선도 자동 생성
GitHub Actions에서 매일 실행 → docs/ 에 PNG + JSON 저장 → README 갱신
"""

# ── 표준 라이브러리 ──────────────────────────────────────────────────────────
import codecs
import json
import os
import re
import sys
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

# ── 서드파티 ─────────────────────────────────────────────────────────────────
import matplotlib
matplotlib.use("Agg")                        # 헤드리스(GUI 없는) 환경
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests

import metpy.calc as mpcalc
from metpy.plots import SkewT
from metpy.units import units

# ═══════════════════════════════════════════════════════════════════════════════
# 설정
# ═══════════════════════════════════════════════════════════════════════════════

API_URL    = os.environ.get("KMA_API_URL", "")   # GitHub Secret에서 주입

OUTPUT_DIR = Path("docs")
OUTPUT_DIR.mkdir(exist_ok=True)

RAW_PATH  = OUTPUT_DIR / "original.txt"
PLOT_PATH = OUTPUT_DIR / "skewt_latest.png"
META_PATH = OUTPUT_DIR / "meta.json"

KST       = timezone(timedelta(hours=9))
NOW       = datetime.now(KST)
DATE_STR  = NOW.strftime("%Y년 %m월 %d일 %H:%M KST")
FILE_DATE = NOW.strftime("%Y%m%d")

# 다크 모드 PNG 색상 팔레트
_C = dict(
    bg      = "#0d1117",
    surface = "#161b22",
    border  = "#30363d",
    text    = "#e6edf3",
    subtext = "#8b949e",
    temp    = "#ff6b6b",
    dew     = "#4fc3f7",
    parcel  = "#ffd54f",
    dry_adi = "#4caf50",
    moist   = "#26a69a",
    mix     = "#ff8f00",
    lcl_c   = "#ffeb3b",
    lfc_c   = "#69f0ae",
    el_c    = "#40c4ff",
)


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 데이터 수집
# ═══════════════════════════════════════════════════════════════════════════════

def download_raw(url: str, path: Path) -> None:
    """API URL에서 원시 파일 다운로드"""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    path.write_bytes(resp.content)
    print(f"[✓] 다운로드: {path} ({len(resp.content):,} bytes)")


def recode(path: Path, src: str = "euc-kr", dst: str = "utf-8") -> None:
    """EUC-KR → UTF-8 재인코딩"""
    text = codecs.open(path, "r", src).read()
    codecs.open(path, "w", dst).write(text)
    print(f"[✓] 인코딩 변환: {src} → {dst}")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 데이터 파싱
# ═══════════════════════════════════════════════════════════════════════════════

COLS = ["YYMMDDHHMI", "STN", "PA", "GH", "TA", "TD", "WD", "WS", "FLAG"]

def load_data(path: Path) -> pd.DataFrame:
    """
    원시 텍스트 → 정제된 DataFrame
      - 결측값(-999) 처리
      - PA/TA/TD 결측 행 제거
      - 기압 내림차순(지표→고층) 정렬
    """
    df = pd.read_csv(
        path,
        sep=r"\s+",
        comment="#",
        header=None,
        names=COLS,
        na_values=-999.0,
    )
    df["datetime"] = pd.to_datetime(df["YYMMDDHHMI"], format="%Y%m%d%H%M")
    df = (
        df.dropna(subset=["PA", "TA", "TD"])
          .sort_values("PA", ascending=False)
          .reset_index(drop=True)
    )
    print(f"[✓] 데이터 로드: {len(df)}개 레벨  "
          f"({df['PA'].max():.0f} ~ {df['PA'].min():.0f} hPa)")
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 기상 파라미터 계산
# ═══════════════════════════════════════════════════════════════════════════════

def _safe(fn, *args, **kw):
    """
    MetPy 계산 중 오류 또는 nan 결과 시 None 반환
    LFC/EL 등은 조건이 불충족되면 (nan hPa, nan °C) 튜플을 반환하므로 함께 처리
    """
    try:
        result = fn(*args, **kw)
        # 튜플 반환 (p, T) 형태인 경우 nan 체크
        if isinstance(result, tuple) and len(result) == 2:
            if np.isnan(result[0].magnitude) or np.isnan(result[1].magnitude):
                print(f"    [!] {fn.__name__}: 조건 불충족 (nan) → None 처리")
                return None
        return result
    except Exception as e:
        print(f"    [!] {fn.__name__} 계산 실패: {e}")
        return None


def compute(df: pd.DataFrame) -> dict:
    """
    단열선도에 필요한 모든 파라미터 계산
    반환 키:
        p, t, td, gh : pint Quantity 배열
        prof         : 기층 상승 곡선
        cape, cin    : pint Quantity (J/kg)
        lcl, lfc, el : (p_qty, t_qty) 튜플 or None
        pwat         : 가강수량 pint Quantity or None
    """
    p  = df["PA"].values * units.hPa
    t  = df["TA"].values * units.degC
    td = df["TD"].values * units.degC
    gh = df["GH"].values  # 지오퍼텐셜 고도(m), 단위 없이 보관

    prof = mpcalc.parcel_profile(p, t[0], td[0]).to("degC")
    cape_q, cin_q = mpcalc.cape_cin(p, t, td, prof)

    # LCL: 공식 계산, 거의 항상 성공
    lcl = _safe(mpcalc.lcl, p[0], t[0], td[0])

    # LFC: 조건부 불안정이 없으면 None
    lfc = _safe(mpcalc.lfc, p, t, td, which="bottom")

    # EL: LFC가 없으면 대개 None
    el  = _safe(mpcalc.el, p, t, td)

    # 가강수량
    pwat = _safe(mpcalc.precipitable_water, p, td)

    return dict(
        p=p, t=t, td=td, gh=gh, prof=prof,
        cape=cape_q, cin=cin_q,
        lcl=lcl, lfc=lfc, el=el,
        pwat=pwat,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# 4. 그래프 생성
# ═══════════════════════════════════════════════════════════════════════════════

def draw_skewt(params: dict, date_str: str, save_path: Path) -> None:
    """Skew-T Log-P 단열선도 생성 및 PNG 저장"""
    p, t, td, prof = params["p"], params["t"], params["td"], params["prof"]
    lcl, lfc, el   = params["lcl"], params["lfc"], params["el"]
    cape = params["cape"].magnitude
    cin  = params["cin"].magnitude

    fig  = plt.figure(figsize=(9, 11), facecolor=_C["bg"])
    skew = SkewT(fig, rotation=45)
    ax   = skew.ax

    # ── 배경 ──────────────────────────────────────────────────────────────────
    ax.set_facecolor(_C["bg"])
    for sp in ax.spines.values():
        sp.set_edgecolor(_C["border"])

    for pres in [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]:
        ax.axhline(pres, lw=0.5, color=_C["border"], zorder=0)

    # ── 단열선 / 혼합비 선 ────────────────────────────────────────────────────
    skew.plot_dry_adiabats(
        colors=_C["dry_adi"], linewidth=0.6, linestyle="--", alpha=0.45)
    skew.plot_moist_adiabats(
        colors=_C["moist"],   linewidth=0.6, linestyle="-.", alpha=0.45)
    skew.plot_mixing_lines(
        colors=_C["mix"],     linewidth=0.5, linestyle=":",  alpha=0.55,
        pressure=np.arange(100, 1051, 10) * units.hPa)

    # ── CAPE / CIN 음영 ───────────────────────────────────────────────────────
    skew.shade_cape(p, t, prof, facecolor=_C["temp"],  alpha=0.20)
    skew.shade_cin (p, t, prof, facecolor=_C["dew"],   alpha=0.20)

    # ── 메인 곡선 ─────────────────────────────────────────────────────────────
    skew.plot(p, t,    _C["temp"],   linewidth=1.8, label="기온",        zorder=4)
    skew.plot(p, td,   _C["dew"],    linewidth=1.8, linestyle="dashed",
              label="이슬점",  zorder=4)
    skew.plot(p, prof, _C["parcel"], linewidth=1.3, linestyle="dashed",
              label="기층 상승 곡선", zorder=4)

    # ── 관측 레벨 점 ──────────────────────────────────────────────────────────
    # skew.plot() 은 내부적으로 SkewT 좌표 변환을 처리하므로 scatter 대신 사용
    skew.plot(p, t,  _C["temp"], linestyle="none",
              marker="o", markersize=3.5, alpha=0.75, zorder=5)
    skew.plot(p, td, _C["dew"],  linestyle="none",
              marker="o", markersize=3.5, alpha=0.75, zorder=5)

    # ── 특수 레벨 마커 (LCL / LFC / EL) ─────────────────────────────────────
    def _mark(qty_pair, color, marker, label):
        """(p_qty, t_qty) 튜플 → SkewT 위에 마커 표시"""
        if qty_pair is None:
            return
        pv = qty_pair[0].to("hPa")
        tv = qty_pair[1].to("degC")
        # nan 체크 (MetPy가 nan Quantity를 반환하는 경우 대비)
        if np.isnan(pv.magnitude) or np.isnan(tv.magnitude):
            return
        skew.plot(pv, tv, linestyle="none",
                  marker=marker, markersize=10, color=color,
                  markeredgecolor="white", markeredgewidth=1.0,
                  zorder=7, label=label)

    _mark(lcl, _C["lcl_c"], "^", "LCL")
    _mark(lfc, _C["lfc_c"], "s", "LFC")
    _mark(el,  _C["el_c"],  "D", "EL")

    # ── 축 설정 ───────────────────────────────────────────────────────────────
    ax.set_ylim(1050, 100)
    ax.set_xlim(-40, 45)
    ax.set_xlabel("기온 (°C)",   color=_C["subtext"], fontsize=10)
    ax.set_ylabel("기압 (hPa)", color=_C["subtext"], fontsize=10)
    ax.tick_params(colors=_C["subtext"])

    # ── 제목 ──────────────────────────────────────────────────────────────────
    ax.set_title(
        f"Skew-T Log-P 단열선도\n{date_str}",
        color=_C["text"], fontsize=12, fontweight="bold", pad=10,
    )

    # ── 정보 박스 ─────────────────────────────────────────────────────────────
    def _fmt(pair):
        if pair is None:
            return "없음"
        pv = pair[0].to("hPa").magnitude
        tv = pair[1].to("degC").magnitude
        return f"{pv:.0f} hPa / {tv:.1f} °C"

    lines = [
        f"CAPE : {cape:>8.1f} J/kg",
        f"CIN  : {cin:>8.1f} J/kg",
        f"LCL  : {_fmt(lcl)}",
        f"LFC  : {_fmt(lfc)}",
        f"EL   : {_fmt(el)}",
    ]
    if params["pwat"] is not None:
        lines.append(f"PWAT : {params['pwat'].to('mm').magnitude:>5.1f} mm")

    ax.text(
        0.02, 0.02, "\n".join(lines),
        transform=ax.transAxes, fontsize=8.5,
        color=_C["text"], verticalalignment="bottom",
        fontfamily="monospace",
        bbox=dict(facecolor=_C["surface"], edgecolor=_C["border"],
                  alpha=0.90, pad=6),
    )

    # ── 범례 ──────────────────────────────────────────────────────────────────
    ax.legend(
        loc="upper right", fontsize=8,
        facecolor=_C["surface"], edgecolor=_C["border"],
        labelcolor=_C["text"],
    )

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[✓] 그래프 저장: {save_path}")


# ═══════════════════════════════════════════════════════════════════════════════
# 5. 메타데이터 JSON 저장
# ═══════════════════════════════════════════════════════════════════════════════

def save_meta(df: pd.DataFrame, params: dict) -> None:
    """
    웹페이지(index.html)가 읽는 meta.json 생성
      - 스칼라 파라미터 (CAPE, CIN, LCL, LFC, EL, PWAT)
      - 전체 관측 레벨 테이블 (levels)
    """
    def _pv(pair, i):
        """(p_qty, t_qty) 튜플에서 float 추출, None → None"""
        if pair is None:
            return None
        v = pair[i]
        return round(float((v.to("hPa") if i == 0 else v.to("degC")).magnitude), 1)

    table = (
        df[["PA", "GH", "TA", "TD", "WD", "WS"]]
        .replace({float("nan"): None})
        .round(1)
        .to_dict(orient="records")
    )

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
        "levels"    : table,
    }
    META_PATH.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"[✓] 메타데이터 저장: {META_PATH}  ({len(table)}개 레벨)")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. README 갱신
# ═══════════════════════════════════════════════════════════════════════════════

def update_readme(params: dict) -> None:
    """README.md 의 <!-- SKEWT_AUTO_START/END --> 구간을 최신 정보로 교체"""
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
        "![Skew-T Log-P](docs/skewt_latest.png)\n"
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


# ═══════════════════════════════════════════════════════════════════════════════
# 진입점
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    if not API_URL:
        print("[✗] 환경변수 KMA_API_URL 이 설정되지 않았습니다.")
        print("    GitHub: Settings → Secrets → KMA_API_URL 을 등록하세요.")
        sys.exit(1)

    sep = "=" * 52
    print(f"\n{sep}\n  Skew-T Log-P 단열선도 자동 생성\n  {DATE_STR}\n{sep}\n")

    download_raw(API_URL, RAW_PATH)           # 1. 수집
    recode(RAW_PATH)                          # 2. 인코딩 변환
    df     = load_data(RAW_PATH)              # 3. 파싱
    params = compute(df)                      # 4. 계산

    # 계산 결과 출력
    print(f"    CAPE = {params['cape'].magnitude:.1f} J/kg")
    print(f"    CIN  = {params['cin'].magnitude:.1f} J/kg")
    for key, label in [("lcl", "LCL"), ("lfc", "LFC"), ("el", "EL")]:
        v = params[key]
        if v:
            print(f"    {label}  = {v[0].to('hPa').magnitude:.0f} hPa"
                  f" / {v[1].to('degC').magnitude:.1f} °C")
        else:
            print(f"    {label}  = 없음")
    if params["pwat"]:
        print(f"    PWAT = {params['pwat'].to('mm').magnitude:.1f} mm")

    draw_skewt(params, DATE_STR, PLOT_PATH)   # 5. 그래프
    save_meta(df, params)                     # 6. JSON
    update_readme(params)                     # 7. README

    print(f"\n{sep}\n  완료!\n{sep}\n")


if __name__ == "__main__":
    main()
