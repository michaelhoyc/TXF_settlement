#!/usr/bin/env python3
"""台指期結算區間 - 標記近 N 個月結算日的開收盤價格帶（日K棒圖）"""
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(".env")

import sys
import calendar
import logging
import time
import requests
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import shioaji as sj
from datetime import datetime, date, timedelta

# ─── 設定 ────────────────────────────────────────────────────────────────────
LOOKBACK_MONTHS = int(sys.argv[1]) if len(sys.argv) > 1 else 8

TG_TOKEN   = os.getenv("TG_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
API_KEY    = os.getenv("API_KEY", "")
SECRET_KEY = os.getenv("SECRET_KEY", "")

TEMP_DIR = os.path.join(os.path.dirname(__file__), "temp")
LOG_DIR  = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"settlement_{datetime.now().strftime('%Y%m%d')}.log"),
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger(__name__)

# 每個結算區間的顏色（由舊到新，舊的較淡，最新的最深）
_BAND_COLORS = [
    "#1565C0",  # 深藍（最新）
    "#1976D2",
    "#1E88E5",
    "#2196F3",
    "#42A5F5",
    "#64B5F6",
    "#90CAF9",
    "#BBDEFB",  # 淺藍（最舊）
]


# ─── 結算日計算 ────────────────────────────────────────────────────────────────
def get_settlement_day(year: int, month: int) -> date:
    """台指期結算日：每月第三個星期三"""
    cal = calendar.monthcalendar(year, month)
    wednesdays = [w[calendar.WEDNESDAY] for w in cal if w[calendar.WEDNESDAY] != 0]
    return date(year, month, wednesdays[2])


def get_recent_settlement_days(n: int) -> list[date]:
    """取得最近 n 個結算日（不超過今日），由舊到新排序"""
    today = date.today()
    results: list[date] = []
    year, month = today.year, today.month

    while len(results) < n:
        sd = get_settlement_day(year, month)
        if sd <= today:
            results.append(sd)
        month -= 1
        if month == 0:
            year -= 1
            month = 12

    return sorted(results)


# ─── Telegram ────────────────────────────────────────────────────────────────
def send_tg_photo(file_path: str, caption: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        log.warning("Telegram 設定不完整，跳過發送")
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto"
    with open(file_path, "rb") as f:
        try:
            res = requests.post(
                url,
                data={"chat_id": TG_CHAT_ID, "caption": caption, "parse_mode": "Markdown"},
                files={"photo": f},
                timeout=30,
            ).json()
            if res.get("ok"):
                log.info("Telegram 發送成功")
            else:
                log.error(f"Telegram 發送失敗: {res.get('description')}")
        except Exception as e:
            log.error(f"Telegram 連線錯誤: {e}")


# ─── 繪圖 ─────────────────────────────────────────────────────────────────────
_STYLE = mpf.make_mpf_style(
    base_mpf_style="yahoo",
    marketcolors=mpf.make_marketcolors(
        up="red", down="green", edge="inherit", wick="inherit", volume="inherit"
    ),
    y_on_right=True,
)


def draw_chart(df_daily: pd.DataFrame, settlement_days: list[date]) -> str:
    """
    繪製日K棒 + 結算區間帶（每個結算日的開收盤價格帶，向右延伸至圖表邊緣）
    """
    n = len(df_daily)
    df_idx = df_daily.reset_index()

    # 找出每個結算日在 DataFrame 中對應的 bar index
    # 計算動態偏移量（整體 High/Low 範圍的 0.5%）
    y_range = float(df_daily["High"].max() - df_daily["Low"].min())
    label_offset = y_range * 0.24

    settlement_bars: list[tuple[int, float, float, float, date]] = []
    for sd in settlement_days:
        sd_ts = pd.Timestamp(sd)
        # 允許 ±2 個交易日的偏差（應付例假日）
        for delta in range(0, 3):
            mask = df_idx["ts"].dt.normalize() == sd_ts + pd.Timedelta(days=delta)
            if mask.any():
                bar_idx = int(df_idx[mask].index[0])
                row = df_idx.loc[bar_idx]
                top  = max(float(row["Open"]), float(row["Close"]))
                bot  = min(float(row["Open"]), float(row["Close"]))
                high = float(row["High"])
                settlement_bars.append((bar_idx, top, bot, high, sd))
                break

    fig, axes = mpf.plot(
        df_daily,
        type="candle",
        style=_STYLE,
        volume=True,
        title=f"TXF Settlement Zone  (Last {LOOKBACK_MONTHS} months)",
        figsize=(18, 9),
        returnfig=True,
        warn_too_much_data=n + 1,
        datetime_format="%m/%d",
        xrotation=45,
    )
    ax = axes[0]

    # 顏色清單：最新結算日使用深色，最舊用淺色
    colors = list(reversed(_BAND_COLORS))  # index 0 = 最舊
    n_colors = len(colors)

    for i, (bar_idx, top, bot, high, sd) in enumerate(settlement_bars):
        color = colors[min(i, n_colors - 1)]

        # 從結算日延伸到圖表右端的半透明帶
        ax.fill_between(
            [bar_idx - 0.5, n - 0.5],
            [bot, bot],
            [top, top],
            color=color,
            alpha=0.20,
            linewidth=0,
            zorder=2,
        )

        # 結算日上下緣各畫一條實線（強調區間邊界）
        for y_val in (top, bot):
            ax.plot(
                [bar_idx - 0.5, n - 0.5],
                [y_val, y_val],
                color=color,
                linewidth=0.8,
                alpha=0.7,
                zorder=3,
            )

        # 結算日垂直虛線
        ax.axvline(x=bar_idx, color=color, linestyle="--", alpha=0.6,
                   linewidth=1.2, zorder=4)

        # 標籤（顯示在 K 棒最高點上方，避免覆蓋影線）
        ax.text(
            bar_idx,
            high + label_offset,
            f"{sd.month}/{sd.day}\nSettle",
            color="navy",
            fontsize=7.5,
            fontweight="bold",
            va="bottom",
            ha="center",
            zorder=6,
            bbox=dict(
                boxstyle="round,pad=0.25",
                facecolor="lightyellow",
                edgecolor=color,
                alpha=0.92,
                linewidth=0.8,
            ),
        )

        # 在 K 棒右側顯示區間數值（最新結算日才顯示，避免擁擠）
        if i == len(settlement_bars) - 1:
            ax.text(
                n - 0.4,
                (top + bot) / 2,
                f" {top:.0f}\n ───\n {bot:.0f}",
                color=color,
                fontsize=7,
                va="center",
                ha="left",
                zorder=6,
            )

    tmp_path = os.path.join(TEMP_DIR, f"settlement_{int(time.time() * 1000)}.png")
    fig.savefig(tmp_path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    log.info(f"圖表儲存：{tmp_path}")
    return tmp_path


# ─── Shioaji 資料抓取 ─────────────────────────────────────────────────────────
def fetch_daily_kbars(api: sj.Shioaji, start_str: str, end_str: str) -> pd.DataFrame:
    """
    抓取 TXF 近月 1 分 K，resample 成日K（僅日盤 08:45~13:45）
    """
    contract = api.Contracts.Futures.TXF.TXFR1
    log.info(f"抓取 1K K 棒 {start_str} ~ {end_str}...")
    kbars = api.kbars(contract, start=start_str, end=end_str)

    if not kbars or not kbars.ts:
        raise RuntimeError("無 K 棒資料，請確認日期範圍是否含有交易日")

    df = pd.DataFrame({**kbars})
    df["ts"] = pd.to_datetime(df["ts"])
    df.set_index("ts", inplace=True)
    df.rename(columns={"open": "Open", "high": "High", "low": "Low",
                        "close": "Close", "volume": "Volume"}, inplace=True)

    # 只取日盤時段，避免夜盤的不同日期歸屬問題
    df_day = df.between_time("08:45", "13:45")

    df_daily = (
        df_day.resample("1D")
        .agg({"Open": "first", "High": "max", "Low": "min",
              "Close": "last", "Volume": "sum"})
        .dropna()
    )
    df_daily = df_daily[df_daily["Volume"] > 0]
    log.info(f"日K 共 {len(df_daily)} 根")
    return df_daily


# ─── 主程式 ─────────────────────────────────────────────────────────────────
def main() -> None:
    assert API_KEY and SECRET_KEY, "請在 .env 設定 API_KEY / SECRET_KEY"

    settlement_days = get_recent_settlement_days(LOOKBACK_MONTHS)
    log.info(f"結算日（{len(settlement_days)} 個）：{[str(d) for d in settlement_days]}")

    # 查詢範圍：最早結算日前 10 天 ~ 今天
    start_str = (settlement_days[0] - timedelta(days=10)).strftime("%Y-%m-%d")
    end_str   = date.today().strftime("%Y-%m-%d")

    log.info("登入 Shioaji (simulation)...")
    api = sj.Shioaji(simulation=True)
    api.login(api_key=API_KEY, secret_key=SECRET_KEY)

    try:
        df_daily = fetch_daily_kbars(api, start_str, end_str)

        if len(df_daily) < 5:
            log.error("日K 資料不足（< 5 根），請確認連線與日期範圍")
            return

        log.info("繪製圖表...")
        path = draw_chart(df_daily, settlement_days)

        latest = settlement_days[-1]
        sd_lines = "\n".join(
            [f"  · `{sd.strftime('%Y/%m/%d')}`  (週三)" for sd in reversed(settlement_days)]
        )
        msg = (
            f"📅 *台指期 結算區間圖*\n"
            f"最近結算日：`{latest.strftime('%Y/%m/%d')}`\n"
            f"查詢期間：`{start_str}` ~ `{end_str}`\n\n"
            f"*歷史結算日列表：*\n{sd_lines}"
        )

        log.info("發送 Telegram...")
        send_tg_photo(path, msg)

        if os.path.exists(path):
            os.remove(path)

    finally:
        api.logout()
        log.info("完成")


if __name__ == "__main__":
    main()
