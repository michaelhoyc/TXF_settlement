"""結算日、假日順延、指定月份與 VWAP 的離線測試。不需要 Shioaji 憑證。"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from txf_settlement import (  # noqa: E402
    BAND_COLORS,
    LABEL_OFFSET_RATIO,
    ConfigError,
    DailySession,
    Settlement,
    SettlementPlan,
    active_contract_month,
    band_color,
    build_caption,
    build_front_month_frame,
    build_plan,
    date_chunks,
    escape_markdown,
    fit_caption,
    load_bundled_calendar,
    match_price_bands,
    minute_bars_to_sessions,
    parse_cli,
    parse_finmind_fsp,
    parse_taifex_daily_csv,
    parse_taifex_fsp_html,
    require_shioaji_keys,
    roll_to_trading_day,
    settlement_for_month,
    settlement_label,
    third_wednesday,
    trading_days_covering,
)


CSV_FIXTURE = """交易日期,契約,到期月份(週別),開盤價,最高價,最低價,收盤價,漲跌價,漲跌%,成交量,結算價,未沖銷契約數,最後最佳買價,最後最佳賣價,歷史最高價,歷史最低價,是否因訊息面暫停交易,交易時段,價差對單式委託成交量
2026/10/21,TX,202610  ,47240,47288,46596,46648,307,0.66%,23455,0,8936,46648,46649,50265,39852,,一般,,
2026/10/21,TX,202610  ,46490,47098,46136,46975,634,1.37%,30400,-,-,46950,46978,50265,39852,,盤後,,
2026/10/21,TX,202611  ,47388,47450,46872,46992,482,1.04%,40515,47013,114962,46991,46997,50440,44435,,一般,,
2026/10/21,TX,202610W1,1,2,3,4,0,0,10,1,1,1,1,1,1,,一般,,
2026/10/21,TX,202610/202611,1,2,3,4,0,0,99,1,1,1,1,1,1,,一般,,
2026/10/22,TX,202611  ,47164,47337,46217,46272,0,0,55226,46234,1,1,1,1,1,,一般,,
"""

FSP_HTML = """
<table>
<tr class="12bk">
  <td align=middle>2026/10/28</td>
  <td>202610W4</td>
  <td align=right>46961</td>
</tr>
<tr bgcolor="#FFFFFF" class="12bk">
  <td align=middle>2026/10/21</td>
  <td width="10%" align=middle>202610  </td>
  <td align=right>46650</td>
</tr>
<tr class="12bk">
  <td>2026/02/23</td>
  <td>202602</td>
  <td>33839</td>
</tr>
</table>
"""


def _session(day, month, session, open_, high, low, close, volume):
    return DailySession(day, month, session, open_, high, low, close, volume)


def _plan_oct():
    anchor = Settlement(date(2026, 9, 16), date(2026, 9, 16), "202609")
    october = Settlement(date(2026, 10, 21), date(2026, 10, 21), "202610")
    november = Settlement(date(2026, 11, 18), date(2026, 11, 18), "202611")
    return SettlementPlan(
        displayed=[october],
        anchor=anchor,
        cycles=[anchor, october, november],
        data_start=date(2026, 10, 20),
    )


def test_third_wednesday_known_dates():
    assert third_wednesday(2024, 1) == date(2024, 1, 17)
    assert third_wednesday(2024, 2) == date(2024, 2, 21)
    assert third_wednesday(2024, 11) == date(2024, 11, 20)
    assert third_wednesday(2026, 2) == date(2026, 2, 18)
    assert third_wednesday(2026, 10) == date(2026, 10, 21)
    assert third_wednesday(2026, 11) == date(2026, 11, 18)


def test_label_offset_matches_implementation():
    assert LABEL_OFFSET_RATIO == 0.24


def test_bundled_calendar_rolls_lunar_new_year_2026():
    days, start, end = load_bundled_calendar()
    assert start <= date(2026, 2, 1) <= end
    assert date(2026, 2, 18) not in days
    assert date(2026, 2, 23) in days
    assert roll_to_trading_day(date(2026, 2, 18), days) == date(2026, 2, 23)
    settlement = settlement_for_month(2026, 2, days)
    assert settlement.nominal == date(2026, 2, 18)
    assert settlement.effective == date(2026, 2, 23)
    assert settlement.rolled
    assert settlement.contract_month == "202602"


def test_recent_settlement_skips_future_rollover_and_keeps_anchor():
    days, _, _ = load_bundled_calendar()
    holiday = build_plan(1, date(2026, 2, 18), days)
    assert holiday.displayed[0].effective == date(2026, 1, 21)
    assert all(item.effective <= date(2026, 2, 18) for item in holiday.displayed)

    after = build_plan(1, date(2026, 2, 24), days)
    shown = after.displayed[0]
    assert shown.effective == date(2026, 2, 23)
    assert shown.nominal == date(2026, 2, 18)
    assert after.anchor is not None
    assert after.anchor.effective < shown.effective
    assert after.data_start > after.anchor.effective
    assert after.data_start in days


def test_weekday_fallback_warns_outside_bundle():
    days, start, end = load_bundled_calendar()
    covered, warnings = trading_days_covering(
        date(2027, 1, 1),
        date(2027, 1, 10),
        live_days=None,
        bundled=days,
        bundled_start=start,
        bundled_end=end,
    )
    assert warnings
    assert date(2027, 1, 1) in covered  # 週五
    assert date(2027, 1, 3) not in covered  # 週日
    assert "週一至週五" in warnings[0]


def test_parse_cli_rejects_bad_n_without_assert():
    assert parse_cli(["settlement.py"]).lookback == 8
    assert parse_cli(["settlement.py"]).include_night is False
    assert parse_cli(["settlement.py", "5"]).lookback == 5
    both = parse_cli(["settlement.py", "--include-night", "6"])
    assert both.lookback == 6 and both.include_night
    assert parse_cli(["settlement.py", "7", "--include-night"]).include_night
    for bad in ("0", "abc", "1.5", "121", "-3"):
        with pytest.raises(ConfigError):
            parse_cli(["settlement.py", bad])
    with pytest.raises(ConfigError):
        require_shioaji_keys("", "secret")
    with pytest.raises(ConfigError):
        require_shioaji_keys("key", "")


def test_parse_cli_via_main_returns_exit_code(monkeypatch, tmp_path):
    import os
    import settlement

    cwd = os.getcwd()
    monkeypatch.setattr(settlement, "LOG_DIR", tmp_path)
    monkeypatch.setattr(settlement, "TEMP_DIR", tmp_path)
    monkeypatch.setattr(settlement, "ROOT", tmp_path)
    try:
        assert settlement.main(["settlement.py", "0"]) == 2
        assert settlement.main(["settlement.py", "nope"]) == 2
    finally:
        os.chdir(cwd)


def test_daily_csv_keeps_specific_month_and_ignores_weekly_spread():
    rows = parse_taifex_daily_csv(CSV_FIXTURE)
    keys = {(row.trade_date, row.contract_month, row.session) for row in rows}
    assert (date(2026, 10, 21), "202610", "day") in keys
    assert (date(2026, 10, 21), "202610", "night") in keys
    assert (date(2026, 10, 21), "202611", "day") in keys
    assert not any("W" in row.contract_month or "/" in row.contract_month for row in rows)
    expiry = next(
        row for row in rows
        if row.trade_date == date(2026, 10, 21) and row.contract_month == "202610" and row.session == "day"
    )
    assert expiry.close == 46648
    assert expiry.open == 47240
    assert expiry.volume == 23455


def test_fsp_html_skips_weekly_contracts():
    prices = parse_taifex_fsp_html(FSP_HTML)
    assert prices[date(2026, 10, 21)] == 46650
    assert prices[date(2026, 2, 23)] == 33839
    assert date(2026, 10, 28) not in prices


def test_fsp_html_tolerates_missing_row_close():
    broken = """
    <TR class="12bk"><TD>2026/10/28</TD><TD>202610W4</TD><TD>46961</TD>
    <TR class="12bk"><TD>2026/10/21</TD><TD>202610  </TD><TD>46650</TD></TR>
    """
    assert parse_taifex_fsp_html(broken) == {date(2026, 10, 21): 46650.0}


def test_plan_survives_calendar_that_ends_this_year():
    days, _, _ = load_bundled_calendar()
    plan = build_plan(8, date(2026, 11, 4), days)
    assert len(plan.displayed) == 8
    assert plan.displayed[-1].effective == date(2026, 10, 21)
    assert plan.displayed[-1].contract_month == "202610"
    assert plan.cycles[-1].effective > date(2026, 11, 4)


def test_live_calendar_tail_falls_back_to_weekdays():
    live = {date(2026, 12, 30), date(2026, 12, 31)}
    covered, warnings = trading_days_covering(date(2026, 12, 30), date(2027, 1, 5), live)
    assert date(2027, 1, 4) in covered
    assert date(2027, 1, 3) not in covered
    assert warnings and "週一至週五" in warnings[0]


def test_finmind_fsp_keeps_monthly_tx_only():
    payload = {
        "data": [
            {"date": "2026-10-21", "contract_month": "202610", "futures_id": "TX", "settlement_price": 46650},
            {"date": "2026-10-28", "contract_month": "202610W4", "futures_id": "TX", "settlement_price": 46961},
            {"date": "2026-10-21", "contract_month": "202610", "futures_id": "TE", "settlement_price": 1},
        ]
    }
    assert parse_finmind_fsp(payload) == {date(2026, 10, 21): 46650.0}


def test_front_month_on_settlement_day_is_expiring_contract():
    sessions = parse_taifex_daily_csv(CSV_FIXTURE)
    sessions.append(_session(date(2026, 10, 20), "202610", "day", 10, 14, 6, 10, 100))
    plan = _plan_oct()
    frame, _warnings = build_front_month_frame(sessions, plan, include_night=False, trading_days=set())
    october = date(2026, 10, 21)
    november = date(2026, 10, 22)
    assert frame.loc[pd.Timestamp(october), "contract_month"] == "202610"
    assert frame.loc[pd.Timestamp(october), "Close"] == 46648
    assert frame.loc[pd.Timestamp(october), "Open"] == 47240
    assert frame.loc[pd.Timestamp(november), "contract_month"] == "202611"
    assert active_contract_month(october, plan.cycles) == "202610"
    assert active_contract_month(november, plan.cycles) == "202611"


def test_vwap_is_volume_weighted_and_resets_after_settlement():
    plan = _plan_oct()
    sessions = [
        _session(date(2026, 10, 20), "202610", "day", 10, 14, 6, 10, 1),  # typical 10
        _session(date(2026, 10, 21), "202610", "day", 20, 20, 20, 20, 1),  # typical 20
        _session(date(2026, 10, 21), "202611", "day", 100, 100, 100, 100, 50),
        _session(date(2026, 10, 22), "202611", "day", 30, 30, 30, 30, 1),
    ]
    frame, _warnings = build_front_month_frame(sessions, plan, False, set())
    assert frame.loc[pd.Timestamp("2026-10-20"), "vwap"] == pytest.approx(10)
    assert frame.loc[pd.Timestamp("2026-10-21"), "vwap"] == pytest.approx(15)
    assert frame.loc[pd.Timestamp("2026-10-22"), "vwap"] == pytest.approx(30)


def test_include_night_changes_vwap_weights():
    plan = _plan_oct()
    sessions = [
        _session(date(2026, 10, 20), "202610", "day", 10, 10, 10, 10, 1),
        _session(date(2026, 10, 20), "202610", "night", 30, 30, 30, 30, 1),
    ]
    day_only, _ = build_front_month_frame(sessions, plan, False, set())
    with_night, warnings = build_front_month_frame(sessions, plan, True, set())
    assert day_only.loc[pd.Timestamp("2026-10-20"), "vwap"] == pytest.approx(10)
    assert with_night.loc[pd.Timestamp("2026-10-20"), "vwap"] == pytest.approx(20)
    assert with_night.loc[pd.Timestamp("2026-10-20"), "Volume"] == 1
    assert warnings == []


def test_vwap_before_anchor_is_not_drawn():
    october = Settlement(date(2026, 10, 21), date(2026, 10, 21), "202610")
    plan = SettlementPlan(
        displayed=[october],
        anchor=None,
        cycles=[october],
        data_start=date(2026, 10, 20),
    )
    sessions = [
        _session(date(2026, 10, 20), "202610", "day", 10, 10, 10, 10, 1),
        _session(date(2026, 10, 21), "202610", "day", 12, 12, 12, 12, 1),
    ]
    frame, warnings = build_front_month_frame(sessions, plan, False, set())
    assert frame["vwap"].isna().all()
    assert any("不畫" in warning for warning in warnings)


def test_missing_settlement_bar_does_not_slide_forward():
    october = Settlement(date(2026, 10, 21), date(2026, 10, 21), "202610")
    index = pd.to_datetime(["2026-10-20", "2026-10-22"])
    frame = pd.DataFrame(
        {"Open": [1, 2], "High": [2, 3], "Low": [1, 2], "Close": [2, 3]},
        index=index,
    )
    bands, warnings = match_price_bands(frame, [october], {date(2026, 10, 22): 999})
    assert bands == []
    assert len(warnings) == 1
    assert "2026-10-21" in warnings[0]
    assert "略過" in warnings[0]


def test_matched_band_uses_fsp_separately_from_open_close():
    october = Settlement(date(2026, 10, 21), date(2026, 10, 21), "202610")
    frame = pd.DataFrame(
        {"Open": [47240.0], "High": [47288.0], "Low": [46596.0], "Close": [46648.0]},
        index=pd.to_datetime(["2026-10-21"]),
    )
    bands, warnings = match_price_bands(frame, [october], {date(2026, 10, 21): 46650})
    assert warnings == []
    assert bands[0].fsp == 46650
    assert bands[0].open == 47240
    assert bands[0].close == 46648
    label = settlement_label(october, 46650)
    assert "FSP 46650" in label
    assert "OC" in label
    assert "Settle" not in label
    missing, notes = match_price_bands(frame, [october], {})
    assert missing[0].fsp is None
    assert "不會用開收盤冒充" in notes[0]
    assert "FSP N/A" in settlement_label(october, None)


def test_band_color_newest_is_darkest_even_when_n_is_small():
    assert band_color(2, 3) == BAND_COLORS[0]
    assert band_color(0, 3) == BAND_COLORS[2]
    assert band_color(0, 8) == BAND_COLORS[-1]
    assert band_color(7, 8) == BAND_COLORS[0]
    assert band_color(0, 10) == BAND_COLORS[-1]
    assert band_color(1, 10) == BAND_COLORS[-1]


def test_caption_escapes_markdown_and_respects_1024():
    assert escape_markdown("a_b*c`d[e") == r"a\_b\*c\`d\[e"
    october = Settlement(date(2026, 10, 21), date(2026, 10, 21), "202610")
    from txf_settlement import PriceBand

    band = PriceBand(october, october.effective, 47240, 46648, 47288, 46650)
    caption = build_caption(
        [band],
        date(2026, 9, 17),
        date(2026, 10, 21),
        False,
        ["檔名 a_b 需跳脫"],
    )
    assert "Settle" not in caption
    assert "46650" in caption
    assert "47240" in caption
    assert "主力成本" in caption
    assert r"a\_b" in caption
    assert len(caption) <= 1024
    long_caption = fit_caption("x" * 5000)
    assert len(long_caption) <= 1024
    assert "截斷" in long_caption
    multiline = fit_caption(("日" * 80 + "\n") * 40)
    assert len(multiline) <= 1024
    assert multiline.endswith("…（內容過長已截斷，完整清單見日誌）")


def test_night_session_rolls_to_next_trading_day():
    trading = {
        date(2026, 10, 16),  # 週五
        date(2026, 10, 19),  # 週一
        date(2026, 10, 21),
    }
    index = pd.to_datetime(
        [
            "2026-10-16 10:00",
            "2026-10-16 15:00",
            "2026-10-17 02:00",
            "2026-10-21 13:45",
            "2026-10-21 14:30",
        ]
    )
    frame = pd.DataFrame(
        {
            "Open": [1, 2, 3, 4, 9],
            "High": [1, 2, 3, 4, 9],
            "Low": [1, 2, 3, 4, 9],
            "Close": [1, 2, 3, 4, 9],
            "Volume": [10, 20, 30, 40, 50],
        },
        index=index,
    )
    sessions = minute_bars_to_sessions(frame, "202610", trading)
    mapped = {(row.trade_date, row.session): row.volume for row in sessions}
    assert mapped[(date(2026, 10, 16), "day")] == 10
    assert mapped[(date(2026, 10, 19), "night")] == 50  # 週五晚 + 週六凌晨
    assert mapped[(date(2026, 10, 21), "day")] == 40
    assert all(row.session != "day" or row.trade_date != date(2026, 10, 21) or row.open != 9 for row in sessions)


def test_date_chunks_cover_range_without_overlap():
    chunks = list(date_chunks(date(2026, 1, 1), date(2026, 2, 5), 30))
    assert chunks[0] == (date(2026, 1, 1), date(2026, 1, 30))
    assert chunks[1] == (date(2026, 1, 31), date(2026, 2, 5))
    flat = []
    for start, end in chunks:
        cursor = start
        while cursor <= end:
            flat.append(cursor)
            cursor = datetime.fromordinal(cursor.toordinal() + 1).date()
    assert flat[0] == date(2026, 1, 1)
    assert flat[-1] == date(2026, 2, 5)
    assert len(flat) == len(set(flat))


def test_draw_chart_smoke(tmp_path):
    import settlement

    index = pd.bdate_range("2026-09-01", periods=20)
    frame = pd.DataFrame(
        {
            "Open": [100 + i for i in range(20)],
            "High": [110 + i for i in range(20)],
            "Low": [90 + i for i in range(20)],
            "Close": [105 + i for i in range(20)],
            "Volume": [1000 + i for i in range(20)],
            "vwap": [float("nan")] * 5 + [102 + i * 0.2 for i in range(15)],
        },
        index=index,
    )
    october = Settlement(date(2026, 9, 16), date(2026, 9, 16), "202609")
    # 9/16/2026 是週三，bdate_range 從 9/1 起的第 12 個營業日左右。直接用 index 裡的一天。
    bar_day = index[10].date()
    band_settlement = Settlement(bar_day, bar_day, "202609")
    from txf_settlement import PriceBand

    bands = [
        PriceBand(october, index[3].date(), 100, 104, 112, None),
        PriceBand(band_settlement, bar_day, 110, 108, 120, 109),
    ]
    # 第一條的日期必須在 index 裡，否則畫圖會略過。改成 index 內的日期。
    bands[0] = PriceBand(
        Settlement(index[3].date(), index[3].date(), "202609"),
        index[3].date(),
        103,
        101,
        112,
        None,
    )
    path = settlement.draw_chart(frame, bands, lookback=2, include_night=False, out_dir=tmp_path)
    written = Path(path)
    assert written.exists()
    assert written.stat().st_size > 1000
    written.unlink()
