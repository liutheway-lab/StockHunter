# StockHunter 台股戰情室

每天自動掃描台股、判斷大盤位階，並透過 Telegram 通知。
全部跑在 GitHub Actions、Render 與 Streamlit 上，不需要自己的電腦開機。

---

## 功能總覽

| 功能 | 說明 | 時間（台灣） |
|---|---|---|
| 盤前晨報 | 國際股市、期貨、VIX、大盤位階 | 每個交易日 08:20 |
| 盤中監控 | 追蹤清單與持股的出場、支撐、壓力提醒 | 08:50 ～ 13:30 |
| 大盤成交金額量測 | 13:25 起量測證交所大盤成交金額，找出最早可確認的時間 | 13:25 ～ 15:30 |
| 夜間掃描 | 三套選股策略＋恐慌承接名單＋大盤線圖 | 每個交易日 14:35 起 |
| 網頁 App | 今日推薦、個股診斷、資金試算 | 隨時 |
| Telegram 機器人 | 追蹤清單、查價、即時提醒 | 隨時（Render） |

---

## 大盤位階（五層）

用加權指數的 MA5、MA10、MA20、MA60 判斷目前在哪一層：

- **第 1 層**：強多頭
- **第 2 層**：多頭
- **第 3 層**：盤整／轉折
- **第 4 層**：空頭
- **第 5 層**：強空頭

每一層啟用的策略不同。第 4、5 層要先通過「止跌確認」才會列出買進名單。
過熱與恐慌會另外標註，不影響層級。

---

## 選股策略

| 策略 | 重點 |
|---|---|
| 低位階起漲 | 低檔盤整後帶量突破頸線 |
| 量縮抗跌 | 3 日均量縮到 20 日均量一半以下，回檔小於大盤 |
| 強勢續攻 | 創高拉回、或噴出後整理，再度站上盤整高點 |
| 恐慌承接（觀察） | 先殺後拉、長下影、帶量、振幅夠大；只供追蹤，沒有追價點 |

分批進場比例可以在網頁 App 調整。

---

## 檔案說明

**掃描與通知**

| 檔案 | 用途 |
|---|---|
| `config.py` | 所有可調參數（門檻、比例、時間），想改策略數字只改這個檔 |
| `night.py` | 夜間掃描主程式 |
| `morning.py` | 盤前晨報 |
| `intraday.py` | 盤中監控與持股／追蹤清單提醒 |
| `strategies.py` | 四套策略的判斷邏輯 |
| `data.py` | 資料抓取（Yahoo、證交所、富果） |
| `notify.py` | Telegram 傳送 |
| `chart_png.py` | 大盤與個股線圖 |
| `guide.py` | 策略說明書（每週一傳送） |
| `events.py` | 重大事件日曆（非農、CPI、PCE、GDP 等，自動從 BLS／BEA 官方抓） |

**網頁與機器人**

| 檔案 | 用途 |
|---|---|
| `app.py` | Streamlit 網頁 App |
| `bot_server.py` | Render 上的 Telegram 機器人與備援排程 |

**量測與測試（一次性或除錯用）**

| 檔案 | 用途 |
|---|---|
| `volume_probe.py` | 大盤成交金額量測（每 2 分鐘抓一次，抓到就停） |
| `volume_profile.py` | 盤中各時段成交量比例量測（一次性） |
| `futures_test.py` | 台指期貨報價測試（TAIFEX MIS） |
| `fugle_test.py` | 富果報價測試 |
| `backtest.py` | 回測 |
| `ping.py` | 連線測試（Yahoo、TWSE、Telegram） |

**GitHub Actions 排程**（`.github/workflows/`）

`morning.yml`、`intraday.yml`、`night.yml`、`volume_probe.yml`、`volume_profile.yml`、`commands.yml`、`guide.yml`、`ping.yml`、`backtest.yml`、`futures_test.yml`、`fugle_test.yml`

**部署設定**

| 檔案 | 用途 |
|---|---|
| `render.yaml` | Render 服務設定（Telegram 機器人） |
| `requirements.txt` | Python 套件清單 |

---

## 需要的 Secrets

在 GitHub 的 **Settings → Secrets and variables → Actions** 設定：

- `TG_BOT_TOKEN`：Telegram 機器人 token
- `TG_OWNER_ID`：管理者的 Telegram 數字 ID
- `TG_CHAT_ID`：群組通知的 chat id
- `FUGLE_API_KEY`：富果行情 API 金鑰

Render 上另外需要 `GH_TOKEN`（備援排程用）與 `FUGLE_API_KEY`。

> 金鑰只放在 Secrets 裡，不要寫進程式碼或 README。

---

## 開發紀錄

| 日期 | 內容 |
|---|---|
| 10/29 | 建立 Telegram Bot |
| 10/30 | 連線、晨報、夜間掃描測試 |
| 10/31 | 啟用大盤位階判斷模型；四套選股策略部署；網頁 App 建置 |
| 11/01 | 調整選股策略；啟用 Render 備援排程 |
| 11/02 | Telegram 機器人即時提醒上線；啟用 UptimeRobot 讓 Render 不休眠 |
| 11/03 | 選股策略重新定義為三套；報價系統改為富果 |
| 11/04 | 修正大盤位階判斷模型；Telegram 機器人新增追蹤清單（取代持股清單）；期貨報價測試 |

---

## 已知限制

- GitHub 排程有時會延遲或沒有執行，所以 Render 上有備援排程，沒有跑就補跑一次。
- 證交所與 Yahoo 偶爾連不上時，程式會改用備用來源或顯示警告，結果可能不完整。
- 大盤成交金額在 13:25 之後才開始量測，正式的通知時間還在確認中。
- 策略門檻是依照目前的觀察設定，尚未經過完整回測驗證。

---

## 免責聲明

本專案只做資料整理與條件篩選，**不構成投資建議**。
任何買賣決策請自行判斷，並自行承擔風險。
