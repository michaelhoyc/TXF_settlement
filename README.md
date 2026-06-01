# TXF Settlement Zone | 台指期結算區間

台指期每月結算日價格區間標記工具，以 Shioaji API 抓取日K棒資料，自動計算每月第三個星期三（結算日）的開收盤價格帶，並繪製帶有延伸區間的日K棒圖表推播至 Telegram。

---

## 功能特色

- 自動計算近 N 個月的台指期結算日（每月第三個星期三）
- 以日盤（08:45–13:45）1 分 K resample 成日K棒
- 結算日開收盤價格帶向右延伸至圖表邊緣，作為支撐/壓力參考區間
- 顏色深淺漸層：最新結算日深藍 → 最舊淺藍
- 標籤顯示在 K 棒最高點上方，不覆蓋 K 線
- 最新結算日右側顯示區間上下緣數值
- 圖表自動推播至 Telegram

---

## 檔案結構

```
txf_settlement/
├── settlement.py          # 主程式
├── .env                   # API 金鑰設定（不含入版本控制）
├── temp/                  # 暫存圖片（執行後自動清除）
└── logs/                  # 每日執行日誌
```

---

## 安裝需求

- Python 3.9+
- 套件：`shioaji[speed]`、`pandas`、`mplfinance`、`matplotlib`、`requests`、`python-dotenv`

```bash
pip install "shioaji[speed]" pandas mplfinance matplotlib requests python-dotenv
```

---

## 環境設定

建立 `.env` 檔案（或 symlink 至現有設定）：

```env
API_KEY=your_sinopac_api_key
SECRET_KEY=your_sinopac_secret_key
TG_TOKEN=your_telegram_bot_token
TG_CHAT_ID=your_telegram_chat_id
```

> 登入使用 `simulation=True`，不需要正式交易權限。

---

## 執行方式

```bash
# 預設：顯示最近 8 個結算日
python settlement.py

# 自訂顯示 N 個結算日
python settlement.py 5
```

---

## 圖表範例

| 項目 | 說明 |
|------|------|
| K 棒 | 台指期近月（TXFR1）日盤日K棒 |
| 結算區間 | 結算日開收盤實體帶，向右延伸 |
| 標籤 | `M/D Settle`，位於 K 棒最高點上方 |
| 顏色 | 深藍（最新）→ 淺藍（最舊） |

---

## Telegram 推播格式

```
📅 台指期 結算區間圖
最近結算日：YYYY/MM/DD
查詢期間：YYYY-MM-DD ~ YYYY-MM-DD

歷史結算日列表：
  · YYYY/MM/DD  (週三)
  ...
```

---

## 結算日規則

台指期結算日 = **每月第三個星期三**

若當日為例假日，程式自動往後搜尋最近的交易日（容錯 ±2 個交易日）。

---

## 相依專案

本專案與 [TXF_monitor](https://github.com/giantcash/TXF_monitor) 共用 `.env` 設定與 Python 虛擬環境。
