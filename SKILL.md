---
name: txf-settlement
description: "發送台指期結算區間日K棒圖到 Telegram，標記近 N 個月每月結算日的開收盤價格帶。觸發時機：用戶要求台指期結算區間、結算日圖表、結算價位圖、傳結算圖、查看結算帶。"
---

# 台指期結算區間技能

## 專案位置

`/home/michael/workspace/txf_settlement/`

| 檔案 | 說明 |
|------|------|
| `settlement.py` | 主程式（計算結算日、抓K棒、繪圖、推播） |
| `.env` | symlink → `txf_monitor/.env`（共用 API/TG 設定） |
| `temp/` | 暫存圖片（執行後自動清除） |
| `logs/settlement_YYYYMMDD.log` | 每日日誌 |

---

## 預設行為（無額外參數時）

呼叫 `/txf-settlement` 但用戶未指定參數時，**直接執行以下流程**：

1. 執行 `settlement.py`（預設顯示最近 8 個結算日）
2. 完成後回報執行結果（成功/失敗）

---

## 操作一覽

| 用戶說 | 執行動作 |
|--------|----------|
| （無參數）/ 傳結算圖 / 結算區間 | `settlement.py`（8 個月） |
| 傳最近 N 個月 / 顯示 N 個 | `settlement.py N` |
| 查看日誌 | 顯示最新日誌 |

---

## 執行指令

```bash
# 預設：最近 8 個結算日
/home/michael/workspace/txf_monitor/venv/bin/python \
  /home/michael/workspace/txf_settlement/settlement.py

# 自訂顯示 N 個結算日
/home/michael/workspace/txf_monitor/venv/bin/python \
  /home/michael/workspace/txf_settlement/settlement.py [N]
```

---

## 圖表說明

- **日K棒**：TXF 近月合約（TXFR1），**日盤 08:45–13:45** resample（夜盤不含）
- **結算區間帶**：每個結算日的開收盤價格帶（實體），向右延伸至圖表右端
- **顏色**：最新深藍 → 最舊淺藍（8 個漸層）
- **標籤**：每個結算日顯示「M/D Settle」，位置在 K 棒最高點（含上影線）上方，偏移量為整體 High/Low 範圍的 24%
- **垂直虛線**：標記結算日位置
- **最新結算日**：右側顯示區間上下緣數值

### Telegram 訊息格式

```
📅 台指期 結算區間圖
最近結算日：YYYY/MM/DD
查詢期間：YYYY-MM-DD ~ YYYY-MM-DD

歷史結算日列表：
  · YYYY/MM/DD  (週三)   ← 最新
  · YYYY/MM/DD  (週三)
  ...
```

---

## 結算日計算規則

台指期結算日 = **每月第三個星期三**

| 月份 | 結算日範例 |
|------|-----------|
| 例假日順延 | 若當日為假日，程式自動往後搜尋 ±2 個交易日 |

---

## 環境設定（.env）

共用 `/home/michael/workspace/txf_monitor/.env`：

| 變數 | 說明 |
|------|------|
| `API_KEY` | 永豐證券 API Key |
| `SECRET_KEY` | 永豐證券 Secret Key |
| `TG_TOKEN` | Telegram Bot Token |
| `TG_CHAT_ID` | Telegram Chat ID |

> 登入使用 `simulation=True`（模擬環境），不需要正式交易權限。

---

## Log 解讀

- `結算日（N 個）：[...]` → 計算出的結算日清單
- `抓取 1K K 棒 ... ~ ...` → 開始向 Shioaji 請求資料
- `日K 共 N 根` → resample 完成
- `圖表儲存：...` → 圖片已產生
- `Telegram 發送成功` → 推播成功
