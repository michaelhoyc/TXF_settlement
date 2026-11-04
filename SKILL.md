---
name: txf-settlement
description: "發送台指期結算區間日K棒圖到 Telegram。標出近 N 個月結算日的官方最後結算價，以及到期月份日盤開收盤區間。觸發時機：用戶要求台指期結算區間、結算日圖表、結算價位圖、傳結算圖、查看結算帶。"
---

# 台指期結算區間技能

## 專案位置

以這個儲存庫的根目錄為工作目錄，不依賴某一台電腦上的絕對路徑。

| 檔案 | 說明 |
|------|------|
| `settlement.py` | 命令列：抓資料、繪圖、推播 |
| `txf_settlement.py` | 結算日、假日順延、指定月份銜接、VWAP |
| `.env` | 專案根目錄的環境變數（不進版控） |
| `temp/` | 暫存圖片（送出後刪除） |
| `logs/settlement_YYYYMMDD.log` | 每日日誌 |
| `data/twse_trading_days.json` | FinMind 連不上時的交易日曆 |

Python 使用目前環境裡已安裝依賴的解譯器。依賴列在 `requirements.txt`。

---

## 預設行為（無額外參數時）

呼叫這個技能但用戶未指定參數時，直接執行：

1. 在專案根目錄執行 `python settlement.py`（最近 8 個結算日，黃線只計日盤）
2. 完成後回報成功或失敗，並摘要日誌裡的警告（缺官方結算價、缺日 K、假日順延）

---

## 操作一覽

| 用戶說 | 執行動作 |
|--------|----------|
| （無參數）/ 傳結算圖 / 結算區間 | `python settlement.py` |
| 傳最近 N 個月 / 顯示 N 個 | `python settlement.py N` |
| 黃線要含夜盤 | `python settlement.py N --include-night` |
| 查看日誌 | 顯示 `logs/` 裡最新的 `settlement_*.log` |

`N` 不是正整數時，程式會結束並留下錯誤訊息，不要把它當成成功。

---

## 執行指令

```bash
# 在專案根目錄
python settlement.py

# 自訂 N 個結算日
python settlement.py 5

# 黃線納入夜盤成交量
python settlement.py 8 --include-night
```

---

## 圖表說明

- **日 K**：每個交易日的近月，取期交所該契約月份的日盤。結算日仍用到期月份，不用 `TXFR1`
- **色帶 OC**：結算日日盤的開盤與收盤，向右延伸。這不是最後結算價
- **FSP 線**：期交所官方最後結算價。取不到就標 `FSP N/A`，不用開收盤代替
- **顏色**：最新固定最深，最舊最淺
- **標籤**：`M/D`、`FSP 價格`、`OC`，放在最高價上方，距離約為整段高低價的 24%
- **黃線**：契約期間日典型價的成交量加權均價。預設不含夜盤。沒有前一次結算錨點的那一段不畫
- **垂直虛線**：結算日位置

### Telegram 訊息

會寫明官方最後結算價、日盤開收，以及是否順延。內容過長會截斷。金鑰或憑證錯誤只會出現在日誌，不會出現在訊息裡。

---

## 結算日

每月第三個星期三。若不是交易日，順延到下一個交易日。交易日優先用 FinMind，失敗則用 `data/twse_trading_days.json`。

---

## 環境變數（`.env`）

| 變數 | 說明 |
|------|------|
| `API_KEY` | 永豐 Shioaji API Key。只有補缺日時需要 |
| `SECRET_KEY` | 永豐 Shioaji Secret Key |
| `TG_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | Telegram Chat ID |
| `FINMIND_TOKEN` | 選用。Backer 以上才能補官方最後結算價 |

登入 Shioaji 時使用 `simulation=True`。

---

## Log 解讀

- `結算日 ... 不是交易日，順延至 ...` → 假日順延
- `官方最後結算價` / `沒有官方最後結算價` → FSP 是否取到
- `日K對不到這些交易日` → 行情缺日，沒有靜默略過
- `Shioaji 查無未到期的 TXF` → 該月份已到期，不能用連續近月硬補
- `圖表儲存`、`Telegram 發送成功` → 圖與推播
