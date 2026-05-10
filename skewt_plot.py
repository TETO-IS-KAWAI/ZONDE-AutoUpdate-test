"""
기상청 API 허브 - Skew-T Log-P 단열선도 자동 생성
GitHub Actions에서 매일 실행 → docs/ 에 PNG(다크/라이트) + JSON 저장 → README 갱신

변경 이력:
  v3.0 - 코드 전면 정리 (draw/save_meta/main 연결 버그 수정)
       - 다크/라이트 PNG 파일명 수정 (skewt_dark.png / skewt_light.png)
       - 마커 좌표 변환 수정 (skew.plot 방식 통일)
       - 팔레트 딕셔너리 실제 적용
       - 한글 폰트 경로 탐지 유지
       - save_meta / update_readme 분리
       - 보안: URL 도메인 화이트리스트, 로그 마스킹
"""

import codecs, json, os, re, shutil, sys, urllib.parse, warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path

warnings.filterwarnings("ignore")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
import pandas as pd
import requests

import metpy.calc as mpcalc
from metpy.plots import SkewT
from metpy.units import units


# ═══════════════════════════════════════════════════════════════════════════════
# 0. 보안 유틸
# ═══════════════════════════════════════════════════════════════════════════════

def _validate_api_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        print("[✗] KMA_API_URL 파싱 실패"); sys.exit(1)

    if parsed.scheme != "https":
        print(f"[✗] 허용되지 않는 URL 스킴: {parsed.scheme!r}"); sys.exit(1)

    _ALLOWED = ("apihub.kma.go.kr", "data.kma.go.kr", "apis.data.go.kr")
    host = parsed.hostname or ""
    if not any(host == h or host.endswith("." + h) for h in _ALLOWED):
        print(f"[✗] 허용되지 않는 호스트: {host!r}"); sys.exit(1)
    return url


def _mask_url(url: str) -> str:
    try:
        p = urllib.parse.urlparse(url)
        return urllib.parse.urlunparse(p._replace(query="***"))
    except Exception:
        return "***"


# ═══════════════════════════════════════════════════════════════════════════════
# 1. 설정
# ═══════════════════════════════════════════════════════════════════════════════

API_URL = _validate_api_url(os.environ.get("KMA_API_URL", "").strip())

OUTPUT_DIR      = Path("docs")
OUTPUT_DIR.mkdir(exist_ok=True)

RAW_PATH        = OUTPUT_DIR / "original.txt"
PLOT_DARK_PATH  = OUTPUT_DIR / "skewt_dark.png"
PLOT_LIGHT_PATH = OUTPUT_DIR / "skewt_light.png"
PLOT_LATEST     = OUTPUT_DIR / "skewt_latest.png"
META_PATH       = OUTPUT_DIR / "meta.json"

KST       = timezone(timedelta(hours=9))
NOW       = datetime.now(KST)
DATE_STR  = NOW.strftime("%Y년 %m월 %d일 %H:%M KST")
FILE_DATE = NOW.strftime("%Y%m%d")

# ── 한글 폰트 ─────────────────────────────────────────────────────────────────
# Noto Sans CJK: SIL OFL 1.1 (상업적 사용 포함 무료)
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Medium.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/NanumGothic.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/gulim.ttc",
]
_FONT_PATH = next((p for p in _FONT_CANDIDATES if Path(p).exists()), None)

if _FONT_PATH:
    # addfont() 로 직접 등록 → font cache 갱신 없이 즉시 인식 (Actions 환경 안정)
    fm.fontManager.addfont(_FONT_PATH)
    _KO_FP = fm.FontProperties(fname=_FONT_PATH)
    _font_name = _KO_FP.get_name()
    matplotlib.rcParams["font.family"] = _font_name
    matplotlib.rcParams["axes.unicode_minus"] = False
    print(f"[✓] 한글 폰트: {_FONT_PATH}  ({_font_name})")
else:
    _KO_FP = None
    matplotlib.rcParams["axes.unicode_minus"] = False
    print("[!] 한글 폰트 없음 — 기본 폰트 사용 (한글 깨짐 가능)")


def kfont(size: float = 10, bold: bool = False) -> fm.FontProperties:
    """한글 FontProperties 반환. 폰트 없으면 기본 폰트."""
    if _KO_FP is None:
        fp = fm.FontProperties(size=size)
    else:
        fp = fm.FontProperties(fname=_KO_FP.get_file(), size=size)
    if bold:
        fp.set_weight("bold")
    return fp


# ── 테마 팔레트 ───────────────────────────────────────────────────────────────
_DARK: dict = dict(
    bg        = "#0d1117",
    surface   = "#161b22",
    border    = "#30363d",
    text      = "#e6edf3",
    subtext   = "#8b949e",
    grid      = "#21262d",
    temp      = "#ff6b6b",
    dew       = "#4fc3f7",
    parcel    = "#ffd54f",
    dry_adi   = "#66bb6a",
    moist_adi = "#26a69a",
    mix_line  = "#ffa726",
    cape_fill = "#ff6b6b",
    cin_fill  = "#4fc3f7",
    lcl_c     = "#ffe082",
    lfc_c     = "#a5d6a7",
    el_c      = "#80deea",
    box_bg    = "#161b22",
)

_LIGHT: dict = dict(
    bg        = "#ffffff",
    surface   = "#f6f8fa",
    border    = "#d0d7de",
    text      = "#1f2328",
    subtext   = "#57606a",
    grid      = "#e4e8ec",
    temp      = "#c0392b",
    dew       = "#1565c0",
    parcel    = "#f57f17",
    dry_adi   = "#2e7d32",
    moist_adi = "#00695c",
    mix_line  = "#e65100",
    cape_fill = "#ef9a9a",
    cin_fill  = "#90caf9",
    lcl_c     = "#f9a825",
    lfc_c     = "#2e7d32",
    el_c      = "#0277bd",
    box_bg    = "#f0f2f5",
)


# ═══════════════════════════════════════════════════════════════════════════════
# 2. 데이터 수집
# ═══════════════════════════════════════════════════════════════════════════════

def download_raw(url: str, path: Path) -> None:
    print(f"[→] 다운로드: {_mask_url(url)}")
    session = requests.Session()
    session.max_redirects = 5
    try:
        resp = session.get(url, timeout=30, allow_redirects=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        print("[✗] 타임아웃"); sys.exit(1)
    except requests.exceptions.SSLError as e:
        print(f"[✗] SSL 오류: {e}"); sys.exit(1)
    except requests.exceptions.HTTPError as e:
        print(f"[✗] HTTP 오류: {e}"); sys.exit(1)

    if "html" in resp.headers.get("Content-Type", "").lower():
        print("[✗] HTML 응답 — API URL/키 확인 필요"); sys.exit(1)

    path.write_bytes(resp.content)
    print(f"[✓] 다운로드 완료: {len(resp.content):,} bytes")


def recode(path: Path, src: str = "euc-kr", dst: str = "utf-8") -> None:
    with codecs.open(path, "r", src, errors="replace") as f:
        text = f.read()
    with codecs.open(path, "w", dst) as f:
        f.write(text)
    print(f"[✓] 인코딩 변환: {src} → {dst}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. 데이터 파싱
# ═══════════════════════════════════════════════════════════════════════════════

_COLS = ["YYMMDDHHMI", "STN", "PA", "GH", "TA", "TD", "WD", "WS", "FLAG"]


def load_data(path: Path) -> pd.DataFrame:
    df = pd.read_csv(
        path, sep=r"\s+", comment="#", header=None,
        names=_COLS, na_values=[-999.0, -9999.0],
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
    try:
        result = fn(*args, **kw)
        if isinstance(result, tuple) and len(result) == 2:
            if np.isnan(result[0].magnitude) or np.isnan(result[1].magnitude):
                print(f"    [!] {fn.__name__}: nan → None")
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
    el   = _safe(mpcalc.el,  p, t, td)
    pwat = _safe(mpcalc.precipitable_water, p, td)

    shr06 = None
    srh   = None
    mask  = ~np.isnan(wd) & ~np.isnan(ws)
    if mask.sum() >= 2:
        try:
            u, v = mpcalc.wind_components(
                ws[mask] * units("m/s"),
                wd[mask] * units.degrees,
            )
            shr_res = _safe(mpcalc.bulk_shear, p[mask], u, v,
                            depth=6000 * units.meter)
            if shr_res is not None:
                u_s, v_s = shr_res
                shr06 = float(np.sqrt(u_s.magnitude**2 + v_s.magnitude**2))
            srh_res = _safe(mpcalc.storm_relative_helicity,
                            p[mask], u, v, depth=3000 * units.meter)
            if srh_res is not None:
                srh = float(srh_res[2].magnitude)
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

def _draw_one(params: dict, date_str: str, C: dict, save_path: Path) -> None:
    """단일 테마 Skew-T Log-P PNG 생성"""
    p, t, td, prof = params["p"], params["t"], params["td"], params["prof"]
    lcl, lfc, el   = params["lcl"], params["lfc"], params["el"]
    cape = params["cape"].magnitude
    cin  = params["cin"].magnitude

    plt.close("all")

    fig  = plt.figure(figsize=(9, 11), facecolor=C["bg"])
    skew = SkewT(fig, rotation=45)
    ax   = skew.ax
    ax.set_facecolor(C["bg"])
    for sp in ax.spines.values():
        sp.set_edgecolor(C["border"])

    # 등압선
    for pres in [1000, 925, 850, 700, 600, 500, 400, 300, 250, 200, 150, 100]:
        ax.axhline(pres, lw=0.6, color=C["grid"], zorder=0)

    # 단열선 / 혼합비 선
    skew.plot_dry_adiabats(
        colors=C["dry_adi"],   linewidth=0.8, linestyle="--", alpha=0.45)
    skew.plot_moist_adiabats(
        colors=C["moist_adi"], linewidth=0.8, linestyle="-.", alpha=0.45)
    skew.plot_mixing_lines(
        colors=C["mix_line"],  linewidth=0.65, linestyle=":", alpha=0.50,
        pressure=np.arange(100, 1051, 10) * units.hPa)

    # CAPE / CIN 음영
    skew.shade_cape(p, t, prof, facecolor=C["cape_fill"], alpha=0.25)
    skew.shade_cin (p, t, prof, facecolor=C["cin_fill"],  alpha=0.25)

    # 메인 곡선
    skew.plot(p, t,    C["temp"],   lw=2.2, label="Temp (T)",       zorder=4)
    skew.plot(p, td,   C["dew"],    lw=2.2, linestyle="dashed",
              label="Dew (Td)", zorder=4)
    skew.plot(p, prof, C["parcel"], lw=1.6, linestyle=(0, (4, 3)),
              label="Parcel", zorder=4)

    # 관측 레벨 점 (skew.plot 으로 좌표 변환 자동 처리)
    skew.plot(p, t,  C["temp"], linestyle="none",
              marker="o", markersize=4.5, alpha=0.80, zorder=5)
    skew.plot(p, td, C["dew"],  linestyle="none",
              marker="o", markersize=4.5, alpha=0.80, zorder=5)

    # 특수 레벨 마커
    def _mark(pair, color, marker, label):
        if pair is None:
            return
        pv, tv = pair[0].to("hPa"), pair[1].to("degC")
        if np.isnan(pv.magnitude) or np.isnan(tv.magnitude):
            return
        skew.plot(pv, tv, linestyle="none",
                  marker=marker, markersize=12, color=color,
                  markeredgecolor=C["text"], markeredgewidth=1.1,
                  zorder=7, label=label)

    _mark(lcl, C["lcl_c"], "^", "LCL")
    _mark(lfc, C["lfc_c"], "s", "LFC")
    _mark(el,  C["el_c"],  "D", "EL")

    # 축
    ax.set_ylim(1050, 100)
    ax.set_xlim(-40, 45)
    ax.set_xlabel("Temp (°C)",   color=C["subtext"], fontsize=11,
                  fontproperties=kfont(11))
    ax.set_ylabel("Pressure (hPa)", color=C["subtext"], fontsize=11,
                  fontproperties=kfont(11))
    ax.tick_params(colors=C["subtext"])

    # 제목
    ax.set_title(
        f"Skew-T Log-P Diagram\n{date_str}",
        color=C["text"], fontsize=13, fontweight="bold", pad=12,
        fontproperties=kfont(13, bold=True),
    )

    # 정보 박스
    def _fmt(pair):
        if pair is None:
            return "N/A"
        return f"{pair[0].to('hPa').magnitude:.0f} hPa  /  {pair[1].to('degC').magnitude:.1f} \u00b0C"

    lines = [
        f"CAPE  :  {cape:>8.1f}  J/kg",
        f"CIN   :  {cin:>8.1f}  J/kg",
        f"LCL   :  {_fmt(lcl)}",
        f"LFC   :  {_fmt(lfc)}",
        f"EL    :  {_fmt(el)}",
    ]
    if params["pwat"] is not None:
        lines.append(f"PWAT  :  {params['pwat'].to('mm').magnitude:>5.1f}  mm")
    if params["shr06"] is not None:
        lines.append(f"SHR06 :  {params['shr06']:>5.1f}  m/s")
    if params["srh"] is not None:
        lines.append(f"SRH   :  {params['srh']:>6.1f}  m\u00b2/s\u00b2")

    ax.text(
        0.015, 0.015, "\n".join(lines),
        transform=ax.transAxes, fontsize=8.5,
        color=C["text"], va="bottom", fontfamily="monospace",
        bbox=dict(facecolor=C["box_bg"], edgecolor=C["border"],
                  alpha=0.92, pad=6, boxstyle="round,pad=0.5"),
    )

    # 범례 (중복 제거)
    handles, labels = ax.get_legend_handles_labels()
    seen, h_out, l_out = set(), [], []
    for h, lb in zip(handles, labels):
        if lb.startswith("_") or lb in seen:
            continue
        seen.add(lb); h_out.append(h); l_out.append(lb)
    ax.legend(h_out, l_out, loc="upper right", fontsize=8.5,
              facecolor=C["box_bg"], edgecolor=C["border"],
              labelcolor=C["text"], framealpha=0.92,
              prop=kfont(8.5))

    plt.tight_layout()
    plt.savefig(save_path, dpi=150, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"[✓] 저장: {save_path}")


def draw_both(params: dict, date_str: str) -> None:
    _draw_one(params, date_str, _DARK,  PLOT_DARK_PATH)
    _draw_one(params, date_str, _LIGHT, PLOT_LIGHT_PATH)
    shutil.copy2(PLOT_DARK_PATH, PLOT_LATEST)
    print(f"[✓] 하위 호환 복사: {PLOT_LATEST}")


# ═══════════════════════════════════════════════════════════════════════════════
# 6. 메타데이터 JSON
# ═══════════════════════════════════════════════════════════════════════════════

def save_meta(df: pd.DataFrame, params: dict) -> None:
    def _pv(pair, i):
        if pair is None:
            return None
        mag = (pair[i].to("hPa") if i == 0 else pair[i].to("degC")).magnitude
        return None if np.isnan(mag) else round(float(mag), 1)

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
        "lcl_p"     : _pv(params["lcl"], 0), "lcl_t": _pv(params["lcl"], 1),
        "lfc_p"     : _pv(params["lfc"], 0), "lfc_t": _pv(params["lfc"], 1),
        "el_p"      : _pv(params["el"],  0), "el_t":  _pv(params["el"],  1),
        "pwat"      : (round(float(params["pwat"].to("mm").magnitude), 1)
                       if params["pwat"] is not None else None),
        "shr06"     : (round(params["shr06"], 1)
                       if params["shr06"] is not None else None),
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
    block  = (
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
            "# ZONDE API Analyze\n\n기상청 API 허브를 이용한 "
            "Skew-T Log-P 단열선도 자동 생성\n\n" + block, encoding="utf-8")
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

    sep = "=" * 56
    print(f"\n{sep}\n  Skew-T Log-P 단열선도 자동 생성\n  {DATE_STR}\n{sep}\n")

    download_raw(API_URL, RAW_PATH)
    recode(RAW_PATH)
    df     = load_data(RAW_PATH)
    params = compute(df)

    print(f"\n  CAPE  = {params['cape'].magnitude:.1f} J/kg")
    print(f"  CIN   = {params['cin'].magnitude:.1f} J/kg")
    for key, label in [("lcl","LCL"), ("lfc","LFC"), ("el","EL")]:
        v = params[key]
        if v:
            print(f"  {label}   = {v[0].to('hPa').magnitude:.0f} hPa"
                  f" / {v[1].to('degC').magnitude:.1f} °C")
        else:
            print(f"  {label}   = N/A")
    if params["pwat"] is not None:
        print(f"  PWAT  = {params['pwat'].to('mm').magnitude:.1f} mm")
    if params["shr06"] is not None:
        print(f"  SHR06 = {params['shr06']:.1f} m/s")
    if params["srh"] is not None:
        print(f"  SRH   = {params['srh']:.1f} m²/s²")
    print()

    draw_both(params, DATE_STR)
    save_meta(df, params)
    update_readme(params)

    print(f"\n{sep}\n  완료!\n{sep}\n")


if __name__ == "__main__":
    main()
