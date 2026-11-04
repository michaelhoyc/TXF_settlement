"""台指期結算區間的計算與解析。

這裡不連線、不繪圖。結算日、假日順延、指定月份銜接、VWAP、
期交所 CSV／HTML 解析與 Telegram 文字都放在這個模組，方便離線測試。
"""
from __future__ import annotations

import calendar
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger("txf_settlement")

DEFAULT_LOOKBACK = 8
MIN_LOOKBACK = 1
MAX_LOOKBACK = 120
# 標籤放在最高價上方，距離是整段 High/Low 的 24%。舊註解寫 0.5%，與程式不符。
LABEL_OFFSET_RATIO = 0.24
TELEGRAM_CAPTION_LIMIT = 1024
DAY_SESSION_START = time(8, 45)
DAY_SESSION_END = time(13, 45)
NIGHT_SESSION_START = time(15, 0)
NIGHT_SESSION_END = time(5, 0)

BAND_COLORS = [
    "#1565C0",  # 最新
    "#1976D2",
    "#1E88E5",
    "#2196F3",
    "#42A5F5",
    "#64B5F6",
    "#90CAF9",
    "#BBDEFB",  # 最舊；更早的區間共用這個顏色
]

BUNDLED_CALENDAR = Path(__file__).resolve().parent / "data" / "twse_trading_days.json"

_TD_RE = re.compile(r"<TD[^>]*>(.*?)</TD>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_MD_RE = re.compile(r"([_*`\[])")


class ConfigError(Exception):
    """參數或金鑰不合法。不用 assert，避免 python -O 把檢查拿掉。"""


class DataError(Exception):
    """交易日曆或行情不足以繼續。"""


@dataclass(frozen=True)
class CliOptions:
    lookback: int
    include_night: bool


@dataclass(frozen=True)
class Settlement:
    nominal: date
    effective: date
    contract_month: str

    @property
    def rolled(self) -> bool:
        return self.effective != self.nominal


@dataclass(frozen=True)
class SettlementPlan:
    """displayed 是畫面上的 N 個結算日（舊到新）。

    anchor 是更早的那一次結算，用來當最舊契約的起始點。
    cycles 含 anchor、displayed，以及下一次尚未到期的結算，讓今天之後的近月有歸屬。
    """

    displayed: list[Settlement]
    anchor: Settlement | None
    cycles: list[Settlement]
    data_start: date


@dataclass(frozen=True)
class DailySession:
    trade_date: date
    contract_month: str
    session: str  # "day" 或 "night"
    open: float
    high: float
    low: float
    close: float
    volume: int

    @property
    def typical(self) -> float:
        return (self.open + self.high + self.low + self.close) / 4.0


@dataclass(frozen=True)
class PriceBand:
    settlement: Settlement
    bar_date: date
    open: float
    close: float
    high: float
    fsp: float | None

    @property
    def top(self) -> float:
        return max(self.open, self.close)

    @property
    def bot(self) -> float:
        return min(self.open, self.close)


def parse_cli(argv: list[str]) -> CliOptions:
    lookback = DEFAULT_LOOKBACK
    include_night = False
    positionals: list[str] = []
    for arg in argv[1:]:
        if arg == "--include-night":
            include_night = True
        elif arg.startswith("-"):
            raise ConfigError(f"不認識的參數：{arg}")
        else:
            positionals.append(arg)
    if len(positionals) > 1:
        raise ConfigError(f"只接受一個 N，收到：{' '.join(positionals)}")
    if positionals:
        raw = positionals[0].strip()
        try:
            lookback = int(raw)
        except ValueError as exc:
            raise ConfigError(f"N 必須是正整數，收到：{raw!r}") from exc
        if lookback < MIN_LOOKBACK or lookback > MAX_LOOKBACK:
            raise ConfigError(
                f"N 必須介於 {MIN_LOOKBACK} 與 {MAX_LOOKBACK}，收到：{lookback}"
            )
    return CliOptions(lookback=lookback, include_night=include_night)


def require_shioaji_keys(api_key: str, secret_key: str) -> None:
    if not api_key or not secret_key:
        raise ConfigError("請在 .env 設定 API_KEY / SECRET_KEY（Shioaji 補齊缺日時需要）")


def third_wednesday(year: int, month: int) -> date:
    if month < 1 or month > 12:
        raise ValueError(f"月份必須介於 1–12，收到：{month}")
    cal = calendar.Calendar(firstweekday=calendar.MONDAY)
    wednesdays = [
        day
        for day in cal.itermonthdays(year, month)
        if day and date(year, month, day).weekday() == calendar.WEDNESDAY
    ]
    if len(wednesdays) < 3:
        raise ValueError(f"{year}-{month:02d} 找不到第三個星期三")
    return date(year, month, wednesdays[2])


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def iter_months(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        year, month = add_months(year, month, 1)


def calendar_window(lookback: int, today: date) -> tuple[date, date]:
    """涵蓋回看月份、前一個錨點，以及下一個未到期契約。"""
    start_year, start_month = add_months(today.year, today.month, -(lookback + 3))
    end_year, end_month = add_months(today.year, today.month, 2)
    start = date(start_year, start_month, 1) - timedelta(days=7)
    end = date(end_year, end_month, 28) + timedelta(days=7)
    return start, end


def load_bundled_calendar(path: Path | None = None) -> tuple[set[date], date, date]:
    path = path or BUNDLED_CALENDAR
    payload = json.loads(path.read_text(encoding="utf-8"))
    days = {date.fromisoformat(item) for item in payload["dates"]}
    if not days:
        raise DataError(f"內建交易日曆是空的：{path}")
    start = date.fromisoformat(payload.get("start") or min(days).isoformat())
    end = date.fromisoformat(payload.get("end") or max(days).isoformat())
    return days, start, end


def weekdays_between(start: date, end: date) -> set[date]:
    found: set[date] = set()
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            found.add(cursor)
        cursor += timedelta(days=1)
    return found


def _weekday_fill_warning(start: date, end: date) -> str:
    return (
        f"交易日曆未涵蓋 {start.isoformat()}～{end.isoformat()}，"
        "這段改以週一至週五視為交易日，國定假日可能沒被排除"
    )


def _fill_outside(
    covered: set[date],
    start: date,
    end: date,
    known_start: date,
    known_end: date,
    today: date | None = None,
) -> list[str]:
    """補週一至週五並回傳警告。

    today 有給時，整段都在 today 之後的缺口只寫 log、不回傳：那段只用來決定
    下一個未到期契約的歸屬，不影響圖上任何一根 K 棒，不必每次都出現在推播裡。
    """
    warnings: list[str] = []
    if start < known_start:
        missing_end = known_start - timedelta(days=1)
        covered |= weekdays_between(start, missing_end)
        warnings.append(_weekday_fill_warning(start, missing_end))
    if end > known_end:
        missing_start = known_end + timedelta(days=1)
        covered |= weekdays_between(missing_start, end)
        message = _weekday_fill_warning(missing_start, end)
        if today is not None and missing_start > today:
            log.info("%s（只影響未來契約歸屬，不列入推播）", message)
        else:
            warnings.append(message)
    return warnings


def trading_days_covering(
    start: date,
    end: date,
    live_days: set[date] | None,
    bundled: set[date] | None = None,
    bundled_start: date | None = None,
    bundled_end: date | None = None,
    today: date | None = None,
) -> tuple[set[date], list[str]]:
    """有線上交易日就用線上的。線上或內建都沒蓋到的日期，才退回週一至週五。"""
    if live_days:
        covered = set(live_days)
        return covered, _fill_outside(covered, start, end, min(covered), max(covered), today)
    if bundled is None or bundled_start is None or bundled_end is None:
        bundled, bundled_start, bundled_end = load_bundled_calendar()
    covered = {day for day in bundled if start <= day <= end}
    warnings = _fill_outside(covered, start, end, bundled_start, bundled_end, today)
    if warnings:
        warnings[0] = (
            "內建交易日曆僅涵蓋 "
            f"{bundled_start.isoformat()}～{bundled_end.isoformat()}。"
            + warnings[0]
        )
    return covered, warnings


def roll_to_trading_day(nominal: date, trading_days: set[date], max_ahead: int = 14) -> date:
    """第三個星期三若不是交易日，順延到下一個交易日。"""
    if nominal in trading_days:
        return nominal
    for offset in range(1, max_ahead + 1):
        candidate = nominal + timedelta(days=offset)
        if candidate in trading_days:
            return candidate
    raise DataError(
        f"結算日 {nominal.isoformat()} 往後 {max_ahead} 天內找不到交易日，請檢查交易日曆"
    )


def next_trading_day(day: date, trading_days: set[date], max_ahead: int = 18) -> date:
    for offset in range(1, max_ahead + 1):
        candidate = day + timedelta(days=offset)
        if candidate in trading_days:
            return candidate
    raise DataError(f"{day.isoformat()} 之後 {max_ahead} 天內找不到交易日")


def settlement_for_month(year: int, month: int, trading_days: set[date]) -> Settlement:
    nominal = third_wednesday(year, month)
    return Settlement(
        nominal=nominal,
        effective=roll_to_trading_day(nominal, trading_days),
        contract_month=f"{year}{month:02d}",
    )


def build_plan(lookback: int, today: date, trading_days: set[date]) -> SettlementPlan:
    start_year, start_month = add_months(today.year, today.month, -(lookback + 3))
    end_year, end_month = add_months(today.year, today.month, 2)
    all_settlements: list[Settlement] = []
    for year, month in iter_months((start_year, start_month), (end_year, end_month)):
        try:
            all_settlements.append(settlement_for_month(year, month, trading_days))
        except DataError:
            nominal = third_wednesday(year, month)
            if nominal <= today:
                raise
            # 日曆往往還沒公布明後年。未來月份改看週一至週五，避免整個程式停住。
            fallback = set(trading_days) | weekdays_between(nominal, nominal + timedelta(days=14))
            item = settlement_for_month(year, month, fallback)
            log.warning(
                "交易日曆沒有涵蓋 %s，契約 %s 暫以週一至週五順延為 %s，國定假日可能沒被排除",
                nominal.isoformat(),
                item.contract_month,
                item.effective.isoformat(),
            )
            all_settlements.append(item)
    done = [item for item in all_settlements if item.effective <= today]
    if len(done) < lookback:
        raise DataError(
            f"在 {today.isoformat()} 之前只找到 {len(done)} 個結算日，不足以顯示 {lookback} 個"
        )
    displayed = done[-lookback:]
    anchor_index = all_settlements.index(displayed[0])
    anchor = all_settlements[anchor_index - 1] if anchor_index else None
    upcoming = [item for item in all_settlements if item.effective > today]
    cycles: list[Settlement] = []
    if anchor is not None:
        cycles.append(anchor)
    cycles.extend(displayed)
    if upcoming:
        cycles.append(upcoming[0])
    if anchor is not None:
        data_start = next_trading_day(anchor.effective, trading_days)
    else:
        data_start = displayed[0].effective - timedelta(days=10)
    return SettlementPlan(
        displayed=displayed,
        anchor=anchor,
        cycles=cycles,
        data_start=data_start,
    )


def active_contract_month(day: date, cycles: list[Settlement]) -> str | None:
    """結算日（含當日）仍屬於到期月份，下一個交易日起才換下個月份。"""
    current = _as_date(day)
    for item in cycles:
        if current <= item.effective:
            return item.contract_month
    return None


def _as_date(value: date | datetime | pd.Timestamp) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    return value


def _parse_number(raw: str) -> float | None:
    text = raw.strip().replace(",", "")
    if text in {"", "-", "--"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_taifex_daily_csv(text: str) -> list[DailySession]:
    """解析期交所每日行情。只留 TX 月契約的一般／盤後，略過週契約與價差。"""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    start = 1 if ("交易日期" in lines[0] or "契約" in lines[0]) else 0
    sessions: list[DailySession] = []
    for line in lines[start:]:
        parts = line.split(",")
        if len(parts) < 18 or parts[1].strip() != "TX":
            continue
        month = parts[2].strip()
        if not (len(month) == 6 and month.isdigit()):
            continue
        session_name = parts[17].strip()
        if session_name == "一般":
            session = "day"
        elif session_name == "盤後":
            session = "night"
        else:
            continue
        try:
            trade_date = datetime.strptime(parts[0].strip(), "%Y/%m/%d").date()
        except ValueError:
            continue
        prices = (
            _parse_number(parts[3]),
            _parse_number(parts[4]),
            _parse_number(parts[5]),
            _parse_number(parts[6]),
        )
        volume = _parse_number(parts[9])
        if any(price is None for price in prices) or volume is None or volume <= 0:
            continue
        open_, high, low, close = prices
        sessions.append(
            DailySession(
                trade_date=trade_date,
                contract_month=month,
                session=session,
                open=float(open_),
                high=float(high),
                low=float(low),
                close=float(close),
                volume=int(volume),
            )
        )
    return sessions


def _cell_text(cell: str) -> str:
    return _TAG_RE.sub("", cell).replace("\xa0", " ").strip()


def parse_taifex_fsp_html(html: str) -> dict[date, float]:
    """解析期交所指數期貨最後結算價表。只留 YYYYMM 月契約，略過週選。

    只查 TX 時，頁面有時會漏掉 </TR>，所以依 <TR> 切開，不假設每一列都有收尾。
    """
    found: dict[date, float] = {}
    for part in re.split(r"<TR\b", html, flags=re.I):
        head, sep, _rest = part.partition(">")
        if not sep or "12bk" not in head:
            continue
        cells = [_cell_text(cell) for cell in _TD_RE.findall(part)]
        if len(cells) < 3:
            continue
        try:
            settled = datetime.strptime(cells[0], "%Y/%m/%d").date()
        except ValueError:
            continue
        month = cells[1].strip()
        if not (len(month) == 6 and month.isdigit()):
            continue
        price = _parse_number(cells[2])
        if price is None:
            continue
        found[settled] = price
    return found


def parse_finmind_fsp(payload: dict) -> dict[date, float]:
    found: dict[date, float] = {}
    for row in payload.get("data") or []:
        futures_id = str(row.get("futures_id", "")).upper()
        if futures_id not in {"TX", "TXF"}:
            continue
        month = str(row.get("contract_month", "")).strip()
        if not (len(month) == 6 and month.isdigit()):
            continue
        try:
            settled = date.fromisoformat(str(row["date"])[:10])
        except (KeyError, ValueError):
            continue
        price = _parse_number(str(row.get("settlement_price", "")))
        if price is None:
            continue
        found[settled] = price
    return found


def parse_finmind_trading_dates(payload: dict) -> set[date]:
    days: set[date] = set()
    for row in payload.get("data") or []:
        try:
            days.add(date.fromisoformat(str(row["date"])[:10]))
        except (KeyError, TypeError, ValueError):
            continue
    return days


def _pick_session(rows: list[DailySession], session: str) -> DailySession | None:
    matched = [row for row in rows if row.session == session]
    if not matched:
        return None
    if len(matched) > 1:
        log.warning(
            "契約 %s 在 %s 的%s盤有 %s 筆，採用成交量最大的一筆",
            matched[0].contract_month,
            matched[0].trade_date.isoformat(),
            "日" if session == "day" else "夜",
            len(matched),
        )
        matched.sort(key=lambda row: row.volume, reverse=True)
    return matched[0]


def _cycle_of(day: date, cycles: list[Settlement]) -> tuple[int, bool] | None:
    """回傳 (週期編號, 是否知道前一次結算)。沒有前一次結算的區段視為不完整。"""
    current = _as_date(day)
    for index, item in enumerate(cycles):
        prev = cycles[index - 1].effective if index else None
        if prev is not None and current <= prev:
            continue
        if current <= item.effective:
            return index, prev is not None
    return None


def _contract_vwap(frame: pd.DataFrame, cycles: list[Settlement]) -> pd.Series:
    vwap = pd.Series(np.nan, index=frame.index, dtype=float)
    keys: list[int | None] = []
    for day in frame.index:
        found = _cycle_of(day, cycles)
        keys.append(found[0] if found is not None and found[1] else None)
    grouped = pd.Series(keys, index=frame.index, dtype="object")
    valid = grouped.notna()
    if not valid.any():
        return vwap
    group_key = grouped[valid].astype(int)
    price_volume = frame.loc[valid, "typical"] * frame.loc[valid, "weight"]
    weight = frame.loc[valid, "weight"]
    cumulative = price_volume.groupby(group_key).cumsum()
    cumulative = cumulative / weight.groupby(group_key).cumsum().replace(0, np.nan)
    vwap.loc[cumulative.index] = cumulative
    return vwap


def _coverage_warnings(
    frame: pd.DataFrame,
    plan: SettlementPlan,
    trading_days: set[date],
) -> list[str]:
    warnings: list[str] = []
    if frame.empty:
        return warnings
    first = _cycle_of(frame.index[0], plan.cycles)
    if first is not None and not first[1]:
        warnings.append("第一個結算日之前缺少前一次結算錨點，這一段 ContractAvg 不畫")
    seen: set[int] = set()
    for day in frame.index:
        found = _cycle_of(day, plan.cycles)
        if found is None or not found[1] or found[0] in seen:
            continue
        seen.add(found[0])
        previous = plan.cycles[found[0] - 1]
        try:
            expected = next_trading_day(previous.effective, trading_days)
        except DataError:
            continue
        observed = _as_date(day)
        if observed > expected:
            month = plan.cycles[found[0]].contract_month
            warnings.append(
                f"契約 {month} 的 VWAP 從 {observed.isoformat()} 起算，"
                f"缺少 {expected.isoformat()} 起的交易日，這段不是完整契約期間"
            )
    return warnings


def build_front_month_frame(
    sessions: list[DailySession],
    plan: SettlementPlan,
    include_night: bool,
    trading_days: set[date],
) -> tuple[pd.DataFrame, list[str]]:
    """用指定月份接成近月日 K。結算日仍用到期月份，隔天才換月。

    K 棒與成交量柱維持日盤。include_night 只影響 VWAP 的典型價與權重。
    """
    grouped: dict[tuple[date, str], list[DailySession]] = {}
    for row in sessions:
        grouped.setdefault((row.trade_date, row.contract_month), []).append(row)

    dates_present = sorted(
        {row.trade_date for row in sessions if row.trade_date >= plan.data_start}
    )
    records = []
    missing_day: list[date] = []
    missing_night: list[date] = []
    for trade_date in dates_present:
        month = active_contract_month(trade_date, plan.cycles)
        if month is None:
            continue
        bucket = grouped.get((trade_date, month), [])
        day_session = _pick_session(bucket, "day")
        if day_session is None:
            missing_day.append(trade_date)
            continue
        night_session = _pick_session(bucket, "night")
        typical = day_session.typical
        weight = float(day_session.volume)
        if include_night:
            if night_session is None or night_session.volume <= 0:
                missing_night.append(trade_date)
            else:
                total = day_session.volume + night_session.volume
                typical = (
                    day_session.typical * day_session.volume
                    + night_session.typical * night_session.volume
                ) / total
                weight = float(total)
        records.append(
            {
                "date": trade_date,
                "Open": day_session.open,
                "High": day_session.high,
                "Low": day_session.low,
                "Close": day_session.close,
                "Volume": day_session.volume,
                "contract_month": month,
                "typical": typical,
                "weight": weight,
            }
        )

    warnings: list[str] = []
    if missing_day:
        sample = ", ".join(day.isoformat() for day in missing_day[:5])
        warnings.append(
            f"有 {len(missing_day)} 個交易日對不到指定月份的日盤（例如 {sample}），這些日子不畫入日K"
        )
    if missing_night:
        sample = missing_night[0].isoformat()
        warnings.append(
            f"有 {len(missing_night)} 個交易日沒有夜盤（例如 {sample}），這些日子的 VWAP 只計日盤"
        )

    columns = [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
        "contract_month",
        "typical",
        "weight",
        "vwap",
    ]
    if not records:
        return pd.DataFrame(columns=columns), warnings

    frame = pd.DataFrame.from_records(records).set_index("date").sort_index()
    frame.index = pd.DatetimeIndex(pd.to_datetime(frame.index))
    frame["vwap"] = _contract_vwap(frame, plan.cycles)
    warnings.extend(_coverage_warnings(frame, plan, trading_days))
    return frame, warnings


def expected_trading_days(plan: SettlementPlan, today: date, trading_days: set[date]) -> list[date]:
    return sorted(day for day in trading_days if plan.data_start <= day <= today)


def missing_trading_days(frame: pd.DataFrame, expected: list[date]) -> list[date]:
    if frame.empty:
        return list(expected)
    have = {_as_date(item) for item in frame.index}
    return [day for day in expected if day not in have]


def match_price_bands(
    frame: pd.DataFrame,
    displayed: list[Settlement],
    final_prices: dict[date, float],
) -> tuple[list[PriceBand], list[str]]:
    """結算日必須剛好對到那根日 K。對不到就警告，不往後猜日曆天。"""
    warnings: list[str] = []
    if frame.empty:
        for item in displayed:
            warnings.append(
                f"結算日 {item.effective.isoformat()}（契約 {item.contract_month}）沒有日K，略過價格帶"
            )
        return [], warnings

    lookup = {_as_date(item): position for position, item in enumerate(frame.index)}
    bands: list[PriceBand] = []
    for item in displayed:
        position = lookup.get(item.effective)
        if position is None:
            warnings.append(
                f"結算日 {item.effective.isoformat()}（契約 {item.contract_month}）"
                "在日K中找不到對應交易日，略過價格帶。請檢查交易日曆或行情是否缺日"
            )
            continue
        row = frame.iloc[position]
        price = final_prices.get(item.effective)
        if price is None:
            warnings.append(
                f"結算日 {item.effective.isoformat()} 沒有官方最後結算價，"
                "圖上只標日盤開收盤（OC），不會用開收盤冒充結算價"
            )
        bands.append(
            PriceBand(
                settlement=item,
                bar_date=item.effective,
                open=float(row["Open"]),
                close=float(row["Close"]),
                high=float(row["High"]),
                fsp=price,
            )
        )
    return bands, warnings


def band_color(index_from_oldest: int, count: int, palette: list[str] | None = None) -> str:
    """最新一根固定用最深色。N 小於色票長度時，不會整組改成淺色。"""
    colors = palette or BAND_COLORS
    if count <= 0:
        raise ValueError("count 必須為正")
    age_from_newest = max(0, (count - 1) - index_from_oldest)
    return colors[min(age_from_newest, len(colors) - 1)]


def settlement_label(item: Settlement, final_price: float | None) -> str:
    price = "N/A" if final_price is None else f"{final_price:.0f}"
    return f"{item.effective.month}/{item.effective.day}\nFSP {price}\nOC"


def escape_markdown(text: str) -> str:
    """Telegram legacy Markdown 裡 _ * ` [ 要跳脫。"""
    return _MD_RE.sub(r"\\\1", text)


def fit_caption(text: str, limit: int = TELEGRAM_CAPTION_LIMIT) -> str:
    if len(text) <= limit:
        return text
    note = "\n…（內容過長已截斷，完整清單見日誌）"
    if len(note) >= limit:
        return text[:limit]
    keep = limit - len(note)
    trimmed = text[:keep]
    newline = trimmed.rfind("\n")
    if newline >= keep // 2:
        trimmed = trimmed[:newline]
    result = trimmed.rstrip() + note
    if len(result) > limit:
        result = text[:keep].rstrip() + note
    return result[:limit]


def build_caption(
    bands: list[PriceBand],
    start: date,
    end: date,
    include_night: bool,
    warnings: list[str],
) -> str:
    lines = ["📅 *台指期 結算區間圖*"]
    if bands:
        latest = bands[-1]
        lines.append(f"最近結算日：`{latest.settlement.effective.isoformat()}`")
        if latest.settlement.rolled:
            lines.append(f"順延自：`{latest.settlement.nominal.isoformat()}`")
        if latest.fsp is None:
            lines.append("官方最後結算價：`未取得`")
        else:
            lines.append(f"官方最後結算價：`{latest.fsp:.0f}`")
        lines.append(f"到期契約日盤開收：`{latest.open:.0f}` / `{latest.close:.0f}`")
    lines.append(f"查詢期間：`{start.isoformat()}` ~ `{end.isoformat()}`")
    night = "含夜盤" if include_night else "僅日盤"
    lines.append(f"黃線：契約期間日典型價的成交量加權均價（VWAP，{night}），不是主力成本")
    lines.append("色帶：到期月份日盤開收盤（OC），不是最後結算價")
    lines.append("FSP 線：期交所官方最後結算價（現貨收盤前 30 分鐘平均）")
    if warnings:
        lines.append("")
        lines.append("*注意*")
        for warning in warnings:
            lines.append("· " + escape_markdown(warning))
    lines.append("")
    lines.append("*歷史結算日*")
    if not bands:
        lines.append("· 沒有對到任何結算日")
    for band in reversed(bands):
        item = band.settlement
        price = "未取得" if band.fsp is None else f"{band.fsp:.0f}"
        rolled = f"，順延自 {item.nominal.isoformat()}" if item.rolled else ""
        lines.append(
            f"· `{item.effective.isoformat()}` 契約 `{item.contract_month}` "
            f"結算價 `{price}` 開 `{band.open:.0f}` 收 `{band.close:.0f}`{rolled}"
        )
    return fit_caption("\n".join(lines))


def minute_bars_to_sessions(
    frame: pd.DataFrame,
    contract_month: str,
    trading_days: set[date],
) -> list[DailySession]:
    """把單一月份的 1 分 K 收成期交所日報的日盤／夜盤。

    日盤 08:45–13:45 屬於當天。15:00 之後屬於下一個交易日的夜盤。
    00:00–05:00 屬於當天；若當天不是交易日（例如週六清晨），則屬於下一個交易日。
    """
    if frame.empty:
        return []
    buckets: dict[tuple[date, str], list[tuple[pd.Timestamp, float, float, float, float, float]]] = {}
    for ts, row in frame.iterrows():
        stamp = pd.Timestamp(ts)
        if stamp.tzinfo is not None:
            stamp = stamp.tz_convert("Asia/Taipei").tz_localize(None)
        assigned = assign_minute(stamp, trading_days)
        if assigned is None:
            continue
        trade_date, session = assigned
        buckets.setdefault((trade_date, session), []).append(
            (
                stamp,
                float(row["Open"]),
                float(row["High"]),
                float(row["Low"]),
                float(row["Close"]),
                float(row["Volume"]),
            )
        )
    sessions: list[DailySession] = []
    for (trade_date, session), bars in buckets.items():
        bars.sort(key=lambda item: item[0])
        volume = int(round(sum(item[5] for item in bars)))
        if volume <= 0:
            continue
        sessions.append(
            DailySession(
                trade_date=trade_date,
                contract_month=contract_month,
                session=session,
                open=bars[0][1],
                high=max(item[2] for item in bars),
                low=min(item[3] for item in bars),
                close=bars[-1][4],
                volume=volume,
            )
        )
    return sessions


def assign_minute(stamp: pd.Timestamp, trading_days: set[date]) -> tuple[date, str] | None:
    clock = stamp.time().replace(microsecond=0)
    day = stamp.date()
    if DAY_SESSION_START <= clock <= DAY_SESSION_END:
        if day not in trading_days:
            return None
        return day, "day"
    if clock >= NIGHT_SESSION_START:
        try:
            return next_trading_day(day, trading_days), "night"
        except DataError:
            return None
    if clock <= NIGHT_SESSION_END:
        if day in trading_days:
            return day, "night"
        try:
            return next_trading_day(day - timedelta(days=1), trading_days), "night"
        except DataError:
            return None
    return None


def date_chunks(start: date, end: date, step_days: int = 30):
    if step_days < 1:
        raise ValueError("step_days 必須為正")
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=step_days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def taifex_daily_chunks(start: date, end: date):
    """期交所每日行情（futDataDown）一次最多查一個日曆月。

    超過就回傳網頁而不是 CSV（2026-11-04 實測：02/24～03/24 可、02/24～03/25 不可）。
    固定 30 天的切法只要跨到二月就會超過，所以改成逐段算「起日＋1 個月－1 天」。
    """
    cursor = start
    while cursor <= end:
        year, month = add_months(cursor.year, cursor.month, 1)
        last_day = calendar.monthrange(year, month)[1]
        limit = date(year, month, min(cursor.day, last_day)) - timedelta(days=1)
        chunk_end = min(end, limit)
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def split_today(sessions: list[DailySession], today: date) -> tuple[list[DailySession], bool]:
    """今天的日盤還沒公布時，把今天的列（只有昨晚夜盤）拿掉。

    期交所把「昨晚夜盤＋今天日盤」記在今天，盤中就會先出現一筆只有夜盤的今天。
    回傳 (過濾後的列, 今天是否已有日盤)。
    """
    has_day = any(row.trade_date == today and row.session == "day" for row in sessions)
    if has_day:
        return list(sessions), True
    return [row for row in sessions if row.trade_date != today], False


def kbars_frame(raw: pd.DataFrame) -> pd.DataFrame:
    """把 Shioaji kbars 轉成 Open/High/Low/Close/Volume，索引為時間。"""
    if raw.empty:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    frame = raw.copy()
    if "ts" in frame.columns:
        frame["ts"] = pd.to_datetime(frame["ts"])
        frame = frame.set_index("ts")
    rename = {
        column: column.capitalize()
        for column in frame.columns
        if column.lower() in {"open", "high", "low", "close", "volume"}
    }
    frame = frame.rename(columns=rename)
    needed = ["Open", "High", "Low", "Close", "Volume"]
    missing = [column for column in needed if column not in frame.columns]
    if missing:
        raise DataError(f"K 棒缺少欄位：{', '.join(missing)}")
    return frame[needed].sort_index()
