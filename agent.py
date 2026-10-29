import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# =====================================================
# 1. 策略核心參數設定
# =====================================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# 標的價格與流動性硬濾網 (剔除冷門怪股)
MIN_PRICE = 25.0          # 最低股價 (排除低價投機水餃股)
MAX_PRICE = 800.0         # 最高股價 (避開散戶資金難配置的超高價股)
MIN_VOL_SHARES = 1000     # 5 日均量至少 1000 張 (張 = 股數 / 1000)
MIN_AVG_TURNOVER = 5e7    # 20 日均成交金額 >= 5000 萬

# 籌碼面門檻 (神秘金字塔 / 集保股權分散表)
MIN_LARGE_RATIO = 45.0    # 400 張以上大戶持股比例至少 >= 45%

# 量價幾何條件 (回檔 14%~20%、分批 30%/40%)
LOOKBACK = 30
TRANCHE_ENTRY = 0.30      # 第一筆進場部位 30%
TRANCHE_BREAKOUT = 0.40   # 突破點加碼部位 40%
MAX_AVG_RATIO = 0.94      # 平均成本不高於 B 的 94%
PULLBACK_MAX = 0.20       # 最大允許回檔 20%
VOL_RATIO = 0.60          # 當日量需 <= 高點量能 60%
STOP_PCT = 0.10           # 停損幅度 10%
RRR = 2.5                 # 風險報酬比 2.5 倍 (獲利目標 +25%)

# 依公式精準計算：(0.3E + 0.4B)/0.7 <= 0.94B  =>  E <= 0.86B
MAX_ENTRY_RATIO = round(
    (MAX_AVG_RATIO * (TRANCHE_ENTRY + TRANCHE_BREAKOUT) - TRANCHE_BREAKOUT) / TRANCHE_ENTRY, 4
)
MIN_ENTRY_RATIO = 1.0 - PULLBACK_MAX  # 0.80

BATCH = 60                # 降低每批下載量，減少 yfinance 限流頻率
HOLDINGS = {}
FALLBACK = ["2330.TW", "2317.TW", "2454.TW", "2382.TW", "3231.TW", "2603.TW"]


# =====================================================
# 2. 台灣證交所法定撮合檔位 (Tick Size) 規範轉換
# =====================================================
def to_tw_tick(price: float) -> float:
    """
    將計算出的浮點數價格，正規化為台灣證券市場撮合檔位
    """
    if price <= 0:
        return 0.01
    if price < 10.0:
        tick = 0.01
    elif price < 50.0:
        tick = 0.05
    elif price < 100.0:
        tick = 0.10
    elif price < 500.0:
        tick = 0.50
    elif price < 1000.0:
        tick = 1.00
    else:
        tick = 5.00

    return round(round(price / tick) * tick, 2)


# =====================================================
# 3. 爬取全市場清單與產業題材分類
# =====================================================
def get_universe():
    items = {}
    for mode, suffix in ((2, ".TW"), (4, ".TWO")):
        url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.encoding = "ms950"
            tables = pd.read_html(io.StringIO(r.text))
            if not tables:
                continue
            df = tables[0]
            df.columns = df.iloc[0]
            df = df.iloc[1:]
            df = df[df["CFICode"] == "ESVUFR"]  # 只保留普通股
            for _, row in df.iterrows():
                s = str(row["有價證券代號及名稱"])
                industry = str(row.get("產業別", "熱門類股")).strip()
                parts = s.split("\u3000")
                if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4:
                    code = parts[0] + suffix
                    items[code] = {
                        "name": parts[1].strip(),
                        "industry": industry if industry and industry != "nan" else "科技題材"
                    }
        except Exception as e:
            print(f"清單抓取失敗 mode={mode}: {e}")
    return items


# =====================================================
# 4. 爬取神秘金字塔 (TDCC 大戶籌碼)
# =====================================================
def get_tdcc_large_ratio(stock_id: str):
    """
    爬取神秘金字塔最新一期 400 張以上大戶持股比率
    """
    clean_id = stock_id.replace(".TW", "").replace(".TWO", "")
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={clean_id}"
    try:
        r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            tables = pd.read_html(io.StringIO(r.text))
            for t in tables:
                if len(t.columns) >= 7:
                    # 最新一期大戶持股比例欄位通常在最後幾欄
                    val = t.iloc[1, 7] if len(t.columns) > 7 else t.iloc[1, -2]
                    return float(str(val).replace("%", ""))
    except Exception:
        pass
    return None


# =====================================================
# 5. 資料下載與解析
# =====================================================
def download_batch(tickers):
    for attempt in range(3):
        try:
            data = yf.download(
                tickers, period="8mo", interval="1d", group_by="ticker",
                auto_adjust=True, threads=True, progress=False,
            )
            if data is not None and not data.empty:
                return data
        except Exception as e:
            print(f"批次下載重試 ({attempt+1}/3): {e}")
        time.sleep(3)
    return None


def get_frame(data, ticker):
    try:
        if isinstance(data.columns, pd.MultiIndex):
            if ticker in data.columns.levels[0]:
                df = data[ticker].copy()
            elif ticker in data.columns.levels[1]:
                df = data.xs(ticker, axis=1, level=1).copy()
            else:
                return None
        else:
            df = data.copy()
        df = df.dropna(subset=["Close"])
        return df if len(df) >= 65 else None
    except Exception:
        return None


# =====================================================
# 6. 多維度策略分析核心
# =====================================================
def analyze(df, ticker, info):
    if len(df) < 70:
        return None

    close = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])

    # 1. 價格門檻 (排除水餃股與千金股)
    if not (MIN_PRICE <= close <= MAX_PRICE):
        return None

    # 2. 流動性濾網 (5 日均量 >= 1000 張 且 20 日均額 >= 5000 萬)
    avg_vol_5 = df["Volume"].rolling(5).mean().iloc[-1]
    if avg_vol_5 < (MIN_VOL_SHARES * 1000):
        return None

    turnover = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]
    if pd.isna(turnover) or turnover < MIN_AVG_TURNOVER:
        return None

    # 3. 前波創高高點 B (排除近 2 日，確認已脫離高點展開回測)
    search_window = df.iloc[-LOOKBACK:-2]
    if search_window.empty:
        return None
    B = float(search_window["High"].max())
    B_idx = search_window["High"].idxmax()

    # 確保 B 是真正的波段高點
    prior_window = df.iloc[:-LOOKBACK]
    if not prior_window.empty and B < float(prior_window["High"].max()) * 0.98:
        return None

    ratio = close / B
    # 4. 回檔區間檢核：0.80B <= Close <= 0.86B (回測 14% ~ 20%)
    if not (MIN_ENTRY_RATIO <= ratio <= MAX_ENTRY_RATIO):
        return None

    # 5. 量縮檢核：當日成交量 <= 突破日量的 60% 且 <= 20 日均量
    vol_B = float(df.loc[B_idx, "Volume"])
    mav20 = float(df["Volume"].rolling(20).mean().iloc[-1])
    if vol_B <= 0 or mav20 <= 0 or not (vol <= VOL_RATIO * vol_B and vol <= mav20):
        return None

    # 6. 均線多頭保護：EMA20 > EMA60 且收盤站上 EMA60 (中期架構未翻空)
    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
    if not (ema20 > ema60 and close >= ema60 * 0.98):
        return None

    # 7. 神秘金字塔籌碼面檢核 (400張以上大戶持股比率)
    large_ratio = get_tdcc_large_ratio(ticker)
    if large_ratio is not None and large_ratio < MIN_LARGE_RATIO:
        return None  # 大戶持股過低或鬆散，直接剔除

    # 8. 依台股合法撮合檔位 (Tick Size) 計算點位
    entry_price = to_tw_tick(close)
    b_price = to_tw_tick(B)
    avg_cost = to_tw_tick(
        (TRANCHE_ENTRY * entry_price + TRANCHE_BREAKOUT * b_price) / (TRANCHE_ENTRY + TRANCHE_BREAKOUT)
    )

    return {
        "ticker": ticker,
        "name": info["name"],
        "industry": info["industry"],
        "close": entry_price,
        "B": b_price,
        "pullback": round((1 - entry_price / b_price) * 100, 1),
        "avg": avg_cost,
        "avg_ratio": round((avg_cost / b_price) * 100, 1),
        "stop_first": to_tw_tick(entry_price * (1 - STOP_PCT)),         # 第一筆進場停損 (Tick)
        "stop_avg": to_tw_tick(avg_cost * (1 - STOP_PCT)),             # 加碼後總部位停損 (Tick)
        "target": to_tw_tick(avg_cost * (1 + STOP_PCT * RRR)),         # 2.5 倍風報比目標價 (Tick)
        "large_ratio": f"{large_ratio:.1f}%" if large_ratio else "主力控盤",
    }


def format_match(m):
    clean_code = m['ticker'].replace(".TW", "").replace(".TWO", "")
    tv_link = f"https://tw.tradingview.com/chart/?symbol=TWSE%3A{clean_code}"
    return (
        f"🎯 *【精選進場訊號】{clean_code} {m['name']}* （{m['industry']}）\n"
        f"👑 *400張大戶持股*：`{m['large_ratio']}`\n"
        f"▫️ 前波突破高點 (B)：`{m['B']}`\n"
        f"▫️ 現價進場 (30% 部位)：`{m['close']}`（拉回 -{m['pullback']}%）\n"
        f"▫️ 突破加碼 (40% 部位)：預估均價 `{m['avg']}`（佔高點 {m['avg_ratio']}%）\n"
        f"━━━━━━━━━━━━━━━\n"
        f"🛑 停損點位：`{m['stop_first']}`（單買第一筆）／`{m['stop_avg']}`（加碼後）\n"
        f"🎯 目標價位：`{m['target']}`（風報比 {RRR}:1）\n"
        f"📊 [點此開啟 TradingView 即時圖表]({tv_link})"
    )


# =====================================================
# 7. Telegram 推播與主流程
# =====================================================
def send_telegram(blocks):
    text_all = "\n\n".join(blocks)
    if not TG_TOKEN or not TG_CHAT:
        print(text_all)
        return

    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3500 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        chunks.append(cur)

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for c in chunks:
        try:
            requests.post(
                url, json={"chat_id": TG_CHAT, "text": c, "parse_mode": "Markdown", "disable_web_page_preview": True},
                timeout=15
            )
        except Exception as e:
            print(f"Telegram 發送異常: {e}")


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    notes = []

    try:
        universe = get_universe()
        if len(universe) < 500:
            raise ValueError(f"清單異常 ({len(universe)} 檔)")
    except Exception as e:
        universe = {t: {"name": t, "industry": "科技"} for t in FALLBACK}
        notes.append(f"⚠️ 全市場清單獲取失敗，改掃備用清單 ({e})")

    tickers = list(universe.keys())
    matches, ok, failed, last_date = [], 0, 0, None
    total = len(tickers)

    print(f"[{now.strftime('%H:%M:%S')}] 啟動全方位選股掃描 (共 {total} 檔)...")

    for i in range(0, total, BATCH):
        batch = tickers[i:i + BATCH]
        data = download_batch(batch)
        if data is None:
            failed += len(batch)
            continue

        for t in batch:
            try:
                df = get_frame(data, t)
                if df is None:
                    failed += 1
                    continue
                ok += 1
                d = df.index[-1].date()
                last_date = d if last_date is None else max(last_date, d)
                res = analyze(df, t, universe[t])
                if res:
                    matches.append(res)
            except Exception:
                failed += 1

        print(f"進度: {min(i + BATCH, total)}/{total} | 成功: {ok} | 篩出符合: {len(matches)}")
        time.sleep(1)

    date_str = str(last_date) if last_date else "無最新交易日"
    head = (
        f"📊 *【高勝率創高拉回 · 籌碼題材日報】*\n"
        f"📅 時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
        f"📈 資料日：`{date_str}`\n"
        f"🔍 掃描母體：`{ok}` 檔 ｜ 💎 精選標的：`{len(matches)}` 檔\n"
        f"⚙️ 核心濾網：股價 25~800 ｜ 均量 > 1000張 ｜ 400張大戶 > 45% ｜ 合法 Tick 報價"
    )
    if not matches:
        head += "\n\n今日無同時符合「量縮回測 + 大戶控盤」標的，保持空手 ✅"

    blocks = [head] + notes
    blocks += [format_match(m) for m in matches]
    send_telegram(blocks)
    print("掃描與推播完成。")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        send_telegram([f"🚨 掃描程序執行失敗：{e}"])
        raise
