#!/usr/bin/env python3
"""台指期結算區間：指定月份日 K、官方最後結算價，並推播到 Telegram。

日 K 優先使用期交所每日行情的指定契約月份，避免 TXFR1 在結算日把新舊月份混在一起。
官方最後結算價來一期交所「指數期貨最後結算價」。Shioaji 只在期交所缺日、
且該月份仍未到期時，用單月合約補資料。
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd
import requests
from dotenv import load_dotenv

from txf_settlement import (
    LABEL_OFFSET_RATIO,
    BAND_COLORS,
    ConfigError,
    DataError,
    PriceBand,
    SettlementPlan,
    active_contract_month,
    build_caption,
    build_front_month_frame,
    build_plan,
    calendar_window,
    date_chunks,
    expected_trading_days,
    kbars_frame,
    load_bundled_calendar,
    match_price_bands,
    minute_bars_to_sessions,
    missing_trading_days,
    parse_cli,
    parse_finmind_fsp,
    parse_finmind_trading_dates,
    parse_taifex_daily_csv,
    parse_taifex_fsp_html,
    require_shioaji_keys,
    settlement_label,
    band_color,
    trading_days_covering,
)

ROOT = Path(__file__).resolve().parent
TEMP_DIR = ROOT / "temp"
LOG_DIR = ROOT / "logs"

TAIFEX_DAILY_URL = "https://www.taifex.com.tw/cht/3/futDataDown"
TAIFEX_FSP_URL = "https://www.taifex.com.tw/cht/5/futIndxFSP"
FINMIND_URL = "https://api.finmindtrade.com/api/v4/data"
USER_AGENT = "txf-settlement/1.1"
HTTP_TIMEOUT = 30
HTTP_RETRIES = 3
KBAR_CHUNK_DAYS = 30
DAILY_CHUNK_DAYS = 30

log = logging.getLogger("txf_settlement")

_STYLE = mpf.make_mpf_style(
    base_mpf_style="yahoo",
    marketcolors=mpf.make_marketcolors(
        up="red", down="green", edge="inherit", wick="inherit", volume="inherit"
    ),
    y_on_right=True,
)


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(
                LOG_DIR / f"settlement_{date.today().strftime('%Y%m%d')}.log",
                encoding="utf-8",
            ),
        ],
        force=True,
    )


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Referer": "https://www.taifex.com.tw/cht/3/futDailyMarketView",
        }
    )
    return session


def request_with_retry(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
    timeout = kwargs.pop("timeout", HTTP_TIMEOUT)
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            response = session.request(method, url, timeout=timeout, **kwargs)
            response.raise_for_status()
            return response
        except requests.RequestException as exc:
            last_error = exc
            log.warning("請求失敗 %s (%s/%s): %s", url.split("?", 1)[0], attempt, HTTP_RETRIES, exc.__class__.__name__)
            if attempt < HTTP_RETRIES:
                time.sleep(1.5 * attempt)
    raise DataError(f"請求在 {HTTP_RETRIES} 次嘗試後仍失敗：{url.split('?', 1)[0]}") from last_error


def decode_taifex_csv(content: bytes) -> str:
    if content.lstrip().startswith(b"<"):
        raise DataError("期交所每日行情回傳網頁而不是 CSV")
    for encoding in ("cp950", "big5", "utf-8-sig", "utf-8"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DataError("期交所每日行情的編碼無法解析")


def fetch_taifex_sessions(session: requests.Session, start: date, end: date):
    collected = []
    failed: list[str] = []
    for chunk_start, chunk_end in date_chunks(start, end, DAILY_CHUNK_DAYS):
        payload = {
            "down_type": "1",
            "commodity_id": "TX",
            "commodity_id2": "",
            "queryStartDate": chunk_start.strftime("%Y/%m/%d"),
            "queryEndDate": chunk_end.strftime("%Y/%m/%d"),
        }
        try:
            response = request_with_retry(session, "POST", TAIFEX_DAILY_URL, data=payload, timeout=HTTP_TIMEOUT)
            text = decode_taifex_csv(response.content)
        except DataError as exc:
            failed.append(f"{chunk_start.isoformat()}～{chunk_end.isoformat()}（{exc}）")
            log.warning("期交所每日行情 %s～%s 失敗：%s", chunk_start, chunk_end, exc)
            continue
        rows = parse_taifex_daily_csv(text)
        log.info("期交所 %s～%s 解析到 %s 筆 TX 月契約日／夜盤", chunk_start, chunk_end, len(rows))
        collected.extend(rows)
    if failed and not collected:
        raise DataError("期交所每日行情全部失敗：" + "；".join(failed))
    if failed:
        log.warning("期交所有 %s 個區間沒抓到，稍後嘗試用 Shioaji 指定月份補齊", len(failed))
    return collected


def fetch_taifex_fsp(session: requests.Session, start: date, end: date) -> dict[date, float]:
    payload = {
        "commodityIds": "1",
        "start_year": str(start.year),
        "start_month": f"{start.month:02d}",
        "end_year": str(end.year),
        "end_month": f"{end.month:02d}",
    }
    response = request_with_retry(session, "POST", TAIFEX_FSP_URL, data=payload, timeout=HTTP_TIMEOUT)
    response.encoding = response.apparent_encoding or "utf-8"
    prices = parse_taifex_fsp_html(response.text)
    if not prices:
        raise DataError("期交所最後結算價頁面沒有解析到 TX 月契約")
    return prices


def fetch_finmind_fsp(session: requests.Session, start: date, end: date, token: str) -> dict[date, float]:
    response = request_with_retry(
        session,
        "GET",
        FINMIND_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "dataset": "TaiwanFuturesFinalSettlementPrice",
            "data_id": "TX",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        timeout=HTTP_TIMEOUT,
    )
    payload = response.json()
    if payload.get("status") not in (200, "200"):
        raise DataError(str(payload.get("msg") or "FinMind 最後結算價失敗"))
    return parse_finmind_fsp(payload)


def fetch_finmind_trading_days(
    session: requests.Session,
    start: date,
    end: date,
    token: str,
) -> set[date]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    response = request_with_retry(
        session,
        "GET",
        FINMIND_URL,
        headers=headers,
        params={
            "dataset": "TaiwanStockTradingDate",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        },
        timeout=HTTP_TIMEOUT,
    )
    payload = response.json()
    if payload.get("status") not in (200, "200"):
        raise DataError(str(payload.get("msg") or "FinMind 交易日曆失敗"))
    days = parse_finmind_trading_dates(payload)
    if not days:
        raise DataError("FinMind 交易日曆是空的")
    return days


def load_final_prices(
    session: requests.Session,
    plan: SettlementPlan,
    token: str,
) -> tuple[dict[date, float], list[str]]:
    start = min(item.effective for item in plan.displayed)
    end = max(item.effective for item in plan.displayed)
    warnings: list[str] = []
    prices: dict[date, float] = {}
    try:
        prices = fetch_taifex_fsp(session, start, end)
        log.info("期交所最後結算價取得 %s 筆月契約", len(prices))
    except DataError as exc:
        warnings.append(f"期交所最後結算價取得失敗：{exc}。不會用開收盤代替")

    missing = [item for item in plan.displayed if item.effective not in prices]
    if missing and token:
        try:
            extra = fetch_finmind_fsp(session, start, end, token)
            filled = 0
            for item in missing:
                if item.effective in extra:
                    prices[item.effective] = extra[item.effective]
                    filled += 1
            log.info("FinMind 最後結算價補上 %s 筆", filled)
        except DataError as exc:
            warnings.append(f"FinMind 最後結算價取得失敗：{exc}")
    elif missing:
        warnings.append(
            f"{len(missing)} 個結算日沒有官方最後結算價。"
            "可在 .env 設定 FINMIND_TOKEN（FinMind Backer 以上）作為備援。"
            "程式不會用日盤開收盤或每日結算價冒充"
        )
    return prices, warnings


def _call_with_retry(action, what: str, retries: int = 3):
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return action()
        except Exception as exc:
            last_error = exc
            log.warning("%s 失敗 (%s/%s): %s", what, attempt, retries, exc.__class__.__name__)
            if attempt < retries:
                time.sleep(1.5 * attempt)
    raise DataError(f"{what} 在 {retries} 次嘗試後仍失敗") from last_error


def login_shioaji(api, api_key: str, secret_key: str) -> None:
    def attempt() -> None:
        try:
            api.login(api_key=api_key, secret_key=secret_key, contracts_timeout=30000)
        except TypeError:
            api.login(api_key=api_key, secret_key=secret_key)

    _call_with_retry(attempt, "Shioaji 登入")


def fetch_kbars(api, contract, start: date, end: date):
    def attempt():
        try:
            return api.kbars(contract, start=start.isoformat(), end=end.isoformat(), timeout=30000)
        except TypeError:
            return api.kbars(contract, start=start.isoformat(), end=end.isoformat())

    return _call_with_retry(attempt, f"Shioaji K 棒 {start.isoformat()}～{end.isoformat()}")


def iter_txf_contracts(api) -> list:
    futures = getattr(getattr(api, "Contracts", None), "Futures", None)
    if futures is None:
        return []
    group = getattr(futures, "TXF", None)
    if group is None:
        try:
            group = futures["TXF"]
        except Exception:
            return []
    if isinstance(group, dict):
        return list(group.values())
    try:
        return list(group)
    except TypeError:
        return [value for name, value in vars(group).items() if not name.startswith("_")]


def find_month_contract(api, contract_month: str):
    """只接受指定月份代碼。TXFR1／TXFR2 會在結算日換 target，不能拿來補日 K。"""
    for contract in iter_txf_contracts(api):
        code = str(getattr(contract, "code", ""))
        if code.endswith("R1") or code.endswith("R2"):
            continue
        delivery = str(getattr(contract, "delivery_month", "")).strip()
        if delivery == contract_month:
            return contract
    return None


def fetch_shioaji_sessions(missing: list[date], plan: SettlementPlan, trading_days: set[date]):
    api_key = os.getenv("API_KEY", "")
    secret_key = os.getenv("SECRET_KEY", "")
    require_shioaji_keys(api_key, secret_key)
    try:
        import shioaji as sj
    except ImportError as exc:
        raise DataError("沒有安裝 shioaji，無法補齊缺日") from exc

    by_month: dict[str, list[date]] = {}
    for day in missing:
        month = active_contract_month(day, plan.cycles)
        if month:
            by_month.setdefault(month, []).append(day)

    api = sj.Shioaji(simulation=True)
    collected = []
    try:
        log.info("登入 Shioaji (simulation)，只用指定月份合約補缺日")
        login_shioaji(api, api_key, secret_key)
        for month, days in by_month.items():
            contract = find_month_contract(api, month)
            if contract is None:
                log.warning(
                    "Shioaji 查無未到期的 TXF %s（到期月份通常無法再以單月代碼查詢，且不改用 TXFR1），略過 %s 日",
                    month,
                    len(days),
                )
                continue
            code = getattr(contract, "code", month)
            frames = []
            for chunk_start, chunk_end in date_chunks(min(days), max(days), KBAR_CHUNK_DAYS):
                log.info("抓取 %s (%s) 1 分 K %s～%s", code, month, chunk_start, chunk_end)
                kbars = fetch_kbars(api, contract, chunk_start, chunk_end)
                if not kbars or not getattr(kbars, "ts", None):
                    log.warning("Shioaji %s %s～%s 沒有 K 棒", code, chunk_start, chunk_end)
                    continue
                frames.append(kbars_frame(pd.DataFrame({**kbars})))
            if not frames:
                continue
            minute = pd.concat(frames).sort_index()
            minute = minute[~minute.index.duplicated(keep="last")]
            sessions = minute_bars_to_sessions(minute, month, trading_days)
            log.info("Shioaji %s 收成 %s 筆日／夜盤", code, len(sessions))
            collected.extend(sessions)
    finally:
        try:
            api.logout()
        except Exception:
            log.warning("Shioaji 登出失敗")
    return collected


def draw_chart(
    frame: pd.DataFrame,
    bands: list[PriceBand],
    lookback: int,
    include_night: bool,
    out_dir: Path | None = None,
) -> str:
    out_dir = Path(out_dir) if out_dir is not None else TEMP_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    candles = frame[["Open", "High", "Low", "Close", "Volume"]].copy()
    count = len(candles)
    y_range = float(candles["High"].max() - candles["Low"].min())
    label_offset = y_range * LABEL_OFFSET_RATIO if y_range else 1.0

    fig, axes = mpf.plot(
        candles,
        type="candle",
        style=_STYLE,
        volume=True,
        title=f"TXF Settlement Zone  (Last {lookback} months)",
        figsize=(18, 9),
        returnfig=True,
        warn_too_much_data=count + 1,
        datetime_format="%m/%d",
        xrotation=45,
    )
    ax = axes[0]
    vwap = frame["vwap"] if "vwap" in frame.columns else pd.Series(index=candles.index, dtype=float)
    if vwap.notna().any():
        night = " +night" if include_night else ""
        ax.plot(
            range(count),
            vwap.to_numpy(),
            color="#fbc02d",
            lw=1.3,
            alpha=0.9,
            label=f"ContractAvg VWAP{night}",
            zorder=5,
        )
        ax.legend(loc="upper left", fontsize=8)
    if vwap.isna().any():
        ax.text(
            0.01,
            0.02,
            "ContractAvg N/A before first anchored contract",
            transform=ax.transAxes,
            color="gray",
            fontsize=8,
            va="bottom",
        )

    positions = {pd.Timestamp(ts).date(): index for index, ts in enumerate(candles.index)}
    fsp_values = []
    for index, band in enumerate(bands):
        bar_idx = positions.get(band.bar_date)
        if bar_idx is None:
            log.warning("結算日 %s 無法對到圖上的座標，略過", band.bar_date.isoformat())
            continue
        color = band_color(index, len(bands), BAND_COLORS)
        ax.fill_between(
            [bar_idx - 0.5, count - 0.5],
            [band.bot, band.bot],
            [band.top, band.top],
            color=color,
            alpha=0.20,
            linewidth=0,
            zorder=2,
        )
        for level in (band.top, band.bot):
            ax.plot(
                [bar_idx - 0.5, count - 0.5],
                [level, level],
                color=color,
                linewidth=0.8,
                alpha=0.7,
                zorder=3,
            )
        if band.fsp is not None:
            fsp_values.append(band.fsp)
            ax.plot(
                [bar_idx - 0.5, count - 0.5],
                [band.fsp, band.fsp],
                color=color,
                linewidth=1.8,
                linestyle="-",
                zorder=4,
            )
        ax.axvline(x=bar_idx, color=color, linestyle="--", alpha=0.6, linewidth=1.2, zorder=4)
        ax.text(
            bar_idx,
            band.high + label_offset,
            settlement_label(band.settlement, band.fsp),
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
        if index == len(bands) - 1:
            if band.fsp is None:
                side = f" FSP N/A\n {band.top:.0f}\n ---\n {band.bot:.0f}"
            else:
                side = f" FSP {band.fsp:.0f}\n {band.top:.0f}\n ---\n {band.bot:.0f}"
            ax.text(
                count - 0.4,
                (band.top + band.bot) / 2,
                side,
                color=color,
                fontsize=7,
                va="center",
                ha="left",
                zorder=6,
            )

    if fsp_values:
        ymin, ymax = ax.get_ylim()
        pad = label_offset or 1.0
        ax.set_ylim(min(ymin, min(fsp_values) - pad), max(ymax, max(fsp_values) + pad))

    path = out_dir / f"settlement_{int(time.time() * 1000)}.png"
    fig.savefig(path, dpi=120, bbox_inches="tight")
    plt.close(fig)
    log.info("圖表儲存：%s", path)
    return str(path)


def _post_photo(path: str, caption: str, parse_mode: str | None) -> tuple[bool, str]:
    token = os.getenv("TG_TOKEN", "")
    chat_id = os.getenv("TG_CHAT_ID", "")
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id, "caption": caption}
    if parse_mode:
        data["parse_mode"] = parse_mode
    try:
        with open(path, "rb") as handle:
            response = requests.post(url, data=data, files={"photo": handle}, timeout=HTTP_TIMEOUT)
        body = response.json()
    except requests.RequestException as exc:
        log.error("Telegram 連線錯誤: %s", exc.__class__.__name__)
        return False, exc.__class__.__name__
    except ValueError:
        log.error("Telegram 回應不是 JSON")
        return False, "invalid json"
    if body.get("ok"):
        return True, ""
    description = str(body.get("description") or "")
    log.error("Telegram 發送失敗: %s", description)
    return False, description


def send_tg_photo(path: str, caption: str) -> None:
    token = os.getenv("TG_TOKEN", "")
    chat_id = os.getenv("TG_CHAT_ID", "")
    if not token or not chat_id:
        log.warning("Telegram 設定不完整，跳過發送")
        return
    ok, description = _post_photo(path, caption, "Markdown")
    if ok:
        log.info("Telegram 發送成功")
        return
    lowered = description.lower()
    if "parse" in lowered or "markdown" in lowered or "can't" in lowered:
        log.warning("Markdown 發送失敗，改以純文字再試一次")
        plain = caption.replace("\\", "")
        ok, _ = _post_photo(path, plain, None)
        if ok:
            log.info("Telegram 純文字發送成功")


def run(options, today: date | None = None) -> int:
    today = today or date.today()
    window_start, window_end = calendar_window(options.lookback, today)
    session = make_session()
    token = os.getenv("FINMIND_TOKEN", "")
    live_days = None
    try:
        live_days = fetch_finmind_trading_days(session, window_start, window_end, token)
        log.info("交易日曆來源：FinMind，%s 個交易日", len(live_days))
    except DataError as exc:
        log.warning("FinMind 交易日曆取得失敗，改用內建日曆：%s", exc)

    bundled, bundled_start, bundled_end = load_bundled_calendar()
    trading_days, calendar_warnings = trading_days_covering(
        window_start,
        window_end,
        live_days,
        bundled,
        bundled_start,
        bundled_end,
    )
    for warning in calendar_warnings:
        log.warning("%s", warning)

    plan = build_plan(options.lookback, today, trading_days)
    if plan.anchor is None:
        log.warning("找不到前一次結算錨點，第一段 ContractAvg 不會畫")
    else:
        log.info(
            "ContractAvg 錨點：%s 契約 %s，行情自 %s 起",
            plan.anchor.effective.isoformat(),
            plan.anchor.contract_month,
            plan.data_start.isoformat(),
        )
    for item in plan.displayed:
        if item.rolled:
            log.info(
                "結算日 %s 不是交易日，順延至 %s（契約 %s）",
                item.nominal.isoformat(),
                item.effective.isoformat(),
                item.contract_month,
            )
        else:
            log.info("結算日 %s（契約 %s）", item.effective.isoformat(), item.contract_month)

    try:
        sessions = fetch_taifex_sessions(session, plan.data_start, today)
    except DataError as exc:
        log.warning("%s", exc)
        sessions = []

    frame, frame_warnings = build_front_month_frame(
        sessions, plan, options.include_night, trading_days
    )
    expected = expected_trading_days(plan, today, trading_days)
    missing = missing_trading_days(frame, expected)
    if missing:
        sample = ", ".join(day.isoformat() for day in missing[:8])
        suffix = "" if len(missing) <= 8 else f" 等共 {len(missing)} 日"
        log.warning("日K對不到這些交易日：%s%s。嘗試用 Shioaji 指定月份補齊，不使用 TXFR1", sample, suffix)
        try:
            extra = fetch_shioaji_sessions(missing, plan, trading_days)
            if extra:
                sessions = list(sessions) + list(extra)
                frame, frame_warnings = build_front_month_frame(
                    sessions, plan, options.include_night, trading_days
                )
                missing = missing_trading_days(frame, expected)
        except ConfigError as exc:
            log.warning("%s", exc)
        except DataError as exc:
            log.warning("Shioaji 補齊失敗：%s", exc)
        if missing:
            sample = ", ".join(day.isoformat() for day in missing[:8])
            suffix = "" if len(missing) <= 8 else f" 等共 {len(missing)} 日"
            frame_warnings.append(f"仍有交易日沒有日K：{sample}{suffix}")

    for warning in frame_warnings:
        log.warning("%s", warning)

    if len(frame) < 5:
        log.error("日K不足 5 根（目前 %s 根）。期交所與 Shioaji 都沒有足夠的指定月份資料", len(frame))
        return 1

    final_prices, price_warnings = load_final_prices(session, plan, token)
    bands, band_warnings = match_price_bands(frame, plan.displayed, final_prices)
    for warning in price_warnings + band_warnings:
        log.warning("%s", warning)

    log.info("日K共 %s 根，價格帶 %s 條", len(frame), len(bands))
    path = draw_chart(frame, bands, options.lookback, options.include_night)
    caption = build_caption(
        bands,
        plan.data_start,
        today,
        options.include_night,
        calendar_warnings + frame_warnings + price_warnings + band_warnings,
    )
    log.info("發送 Telegram...")
    try:
        send_tg_photo(path, caption)
    finally:
        if os.path.exists(path):
            os.remove(path)
    log.info("完成")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    os.chdir(ROOT)
    load_dotenv(ROOT / ".env")
    try:
        options = parse_cli(list(sys.argv if argv is None else argv))
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    try:
        return run(options)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except DataError as exc:
        log.error("%s", exc)
        return 1
    except Exception:
        log.exception("未預期的錯誤")
        return 1


if __name__ == "__main__":
    sys.exit(main())
