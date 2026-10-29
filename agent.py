import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# =====================================================
# 1. 核心參數設定
# =====================================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# 流動性與籌碼濾網門檻
MIN_VOL_SHARES = 1000     # 5 日均量 >= 1000 張
MIN_AVG_TURNOVER = 5e7    # 20 日均成交額 >= 5000 萬台幣
MIN_LARGE_RATIO = 45.0    # 400 張以上大戶持股比率 >= 45%

# 三筆加碼權重與幾何點位
TRANCHE_1 = 0.30          # 第一筆：拉回量縮接刀 (30%)
TRANCHE_2 = 0.40          # 第二筆：突破前高點 B 加碼 (40%)
TRANCHE_3 = 0.30          # 第三筆：主升段延伸突破加碼 (30%)
EXT_RATIO = 1.08          # 第三筆加碼點位：1.08 * B (+8%)

LOOKBACK = 30
MAX_AVG_RATIO = 0.94      # 前兩筆均價不高於 B 的 94% (回檔至少 14%)
PULLBACK_MAX = 0.20       # 最大回檔 20%
VOL_RATIO = 0.60          # 量縮比率 <= 60%
STOP_PCT = 0.10           # 停損幅度 10%
RRR = 2.5                 # 風報比 2.5 倍

# 第一筆進場上限：E <= 0.86B
MAX_ENTRY_RATIO = round(
    (MAX_AVG_RATIO * (TRANCHE_1 + TRANCHE_2) - TRANCHE_2) / TRANCHE_1, 4
)
MIN_ENTRY_RATIO = 1.0 - PULLBACK_MAX

BATCH = 60
HOLDINGS = {}
FALLBACK = ["2330.TW", "2317.TW", "2454.TW", "2382.TW", "3231.TW", "2603.TW"]


# =====================================================
# 2. 國際盤勢與美股期貨獲取模組 (晨間 08:00)
# =====================================================
def get_global_market():
    """
    抓取日經、韓綜、美股三大期指 (NQ, ES, YM) 最新報價與漲跌幅
    """
    symbols = {
        "日經225": "^N225",
        "韓國KOSPI": "^KS11",
        "小Nasdaq期": "NQ=F",
        "小S&P期": "ES=F",
        "小道瓊期": "YM=F",
    }
    market_status = []
    try:
        data = yf.download(
            list(symbols.values()), period="5d", interval="1d",
            progress=False, group_by="ticker"
        )
        for label, sym in symbols.items():
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    sub = data[sym].dropna(subset=["Close"])
                else:
                    sub = data.dropna(subset=["Close"])

                if len(sub) >= 2:
                    curr = float(sub["Close"].iloc[-1])
                    prev = float(sub["Close"].iloc[-2])
                    chg_pct = (curr - prev) / prev * 100
                    icon = "🔴" if chg_pct < 0 else "🟢"
                    market_status.append(f"{icon} {label}: `{curr:,.1f}` ({chg_pct:+.2f}%)")
            except Exception:
                continue
    except Exception as e:
        market_status.append(f"⚠️ 國際盤勢獲取異常: {e}")

    return market_status


# =====================================================
# 3. 台灣證券交易所法定跳動檔位 (Tick Size)
# =====================================================
def to_tw_tick(price: float) -> float:
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
# 4. 爬取全市場清單與題材
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
            df = df[df["CFICode"] == "ESVUFR"]
            for _, row in df.iterrows():
                s = str(row["有價證券代號及名稱"])
                industry = str(row.get("產業別", "熱門標的")).strip()
                parts = s.split("\u3000")
                if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4:
                    code = parts[0] + suffix
                    items[code] = {
                        "name": parts[1].strip(),
                        "industry": industry if industry and industry != "nan" else "科技題材"
                    }
        except Exception as e:
            print(f"清單抓取失敗: {e}")
    return items


# =====================================================
# 5. 爬取神秘金字塔 (400 張大戶持股比率)
# =====================================================
def get_tdcc_large_ratio(stock_id: str):
    clean_id = stock_id.replace(".TW", "").replace(".TWO", "")
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={clean_id}"
    try:
        r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            tables = pd.read_html(io.StringIO(r.text))
            for t in tables:
                if len(t.columns) >= 7:
                    val = t.iloc[1, 7] if len(t.columns) > 7 else t.iloc[1, -2]
                    return float(str(val).replace("%", ""))
    except Exception:
        pass
    return None


# =====================================================
# 6. 下載與資料處理
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
            print(f"批次下載重試: {e}")
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
# 7. 策略邏輯分析核心
# =====================================================
def analyze(df, ticker, info):
    if len(df) < 70:
        return None

    close = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])

    # 1. 流動性過濾
    avg_vol_5 = df["Volume"].rolling(5).mean().iloc[-1]
    if avg_vol_5 < (MIN_VOL_SHARES * 1000):
        return None

    turnover = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]
    if pd.isna(turnover) or turnover < MIN_AVG_TURNOVER:
        return None

    # 2. 定位前波高點 B (避開最近 2 天，確保已進入拉回整理)
    search_window = df.iloc[-LOOKBACK:-2]
    if search_window.empty:
        return None
    B = float(search_window["High"].max())
    B_idx = search_window["High"].idxmax()

    prior_window = df.iloc[:-LOOKBACK]
    if not prior_window.empty and B < float(prior_window["High"].max()) * 0.98:
        return None

    ratio = close / B
    # 3. 回檔區間檢核：0.80B <= Close <= 0.86B (拉回 14% ~ 20%)
    if not (MIN_ENTRY_RATIO <= ratio <= MAX_ENTRY_RATIO):
        return None

    # 4. 量縮檢核：成交量 <= 突破日量的 60% 且 <= 20 日均量
    vol_B = float(df.loc[B_idx, "Volume"])
    mav20 = float(df["Volume"].rolling(20).mean().iloc[-1])
    if vol_B <= 0 or mav20 <= 0 or not (vol <= VOL_RATIO * vol_B and vol <= mav20):
        return None

    # 5. 均線結構：EMA20 > EMA60 且收盤站上 EMA60
    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
    if not (ema20 > ema60 and close >= ema60 * 0.98):
        return None

    # 6. 神秘金字塔籌碼面檢核
    large_ratio = get_tdcc_large_ratio(ticker)
    if large_ratio is not None and large_ratio < MIN_LARGE_RATIO:
        return None

    # 7. 三筆加碼法定檔位換算
    entry_price = to_tw_tick(close)
    b_price = to_tw_tick(B)
    b2_price = to_tw_tick(B * EXT_RATIO)

    avg_3 = to_tw_tick(
        (TRANCHE_1 * entry_price + TRANCHE_2 * b_price + TRANCHE_3 * b2_price) / 1.0
    )

    return {
        "ticker": ticker,
        "name": info["name"],
        "industry": info["industry"],
        "close": entry_price,
        "B": b_price,
        "B2": b2_price,
        "pullback": round((1 - entry_price / b_price) * 100, 1),
        "avg_3": avg_3,
        "stop_first": to_tw_tick(entry_price * (1 - STOP_PCT)),
        "target": to_tw_tick(avg_3 * (1 + STOP_PCT * RRR)),
        "large_ratio": f"{large_ratio:.1f}%" if large_ratio else "主力控盤",
    }


def format_match(m):
    clean_code = m['ticker'].replace(".TW", "").replace(".TWO", "")
    tv_link = f"https://tw.tradingview.com/chart/?symbol=TWSE%3A{clean_code}"
    return (
        f"🎯 *【精選進場標的】{clean_code} {m['name']}* （{m['industry']}）\n"
        f"👑 *400張大戶持股*：`{m['large_ratio']}`\n"
        f"▫️ 第一筆進場 (30%)：`{m['close']}`（回測 -{m['pullback']}%）\n"
        f"▫️ 第二筆加碼 (40%)：`{m['B']}`（突破前高）\n"
        f"▫️ 第三筆加碼 (30%)：`{m['B2']}`（突破延伸 +8%）\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💎 *三筆打滿預估總均價*：`{m['avg_3']}`\n"
        f"🛑 現階段停損：`{m['stop_first']}`（單筆 -10%）\n"
        f"🎯 最終目標價：`{m['target']}`（風報比 {RRR}:1）\n"
        f"📈 [點此查看 TradingView 圖表]({tv_link})"
    )


# =====================================================
# 8. 主流程與推播
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

    # 1. 抓取早晨國際盤態
    global_status = get_global_market()

    try:
        universe = get_universe()
        if len(universe) < 500:
            raise ValueError(f"清單異常 ({len(universe)} 檔)")
    except Exception as e:
        universe = {t: {"name": t, "industry": "科技"} for t in FALLBACK}

    tickers = list(universe.keys())
    matches, ok, failed = [], 0, 0
    total = len(tickers)

    print(f"[{now.strftime('%H:%M:%S')}] 啟動晨間全方位掃描 (共 {total} 檔)...")

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
                res = analyze(df, t, universe[t])
                if res:
                    matches.append(res)
            except Exception:
                failed += 1

        print(f"進度: {min(i + BATCH, total)}/{total} | 成功: {ok} | 篩出: {len(matches)}")
        time.sleep(1)

    # 晨間日報抬頭（整合國際盤態）
    head_lines = [
        f"☀️ *【台股晨間作戰日報 · 08:00】*",
        f"📅 掃描時間：`{now.strftime('%Y-%m-%d %H:%M')}`",
        f"\n🌍 *【即時晨間國際盤態】*",
    ] + global_status + [
        f"\n🔍 掃描母體：`{ok}` 檔 ｜ 🎯 觸發標的：`{len(matches)}` 檔",
        f"⚙️ 規則：回測 14%~20% ｜ 量縮 < 60% ｜ 400張大戶 > 45%",
    ]

    if not matches:
        head_lines.append("\n今日無同時符合回測量縮與籌碼鎖定標的，建議空手觀望 ✅")

    blocks = ["\n".join(head_lines)]
    blocks += [format_match(m) for m in matches]
    send_telegram(blocks)
    print("晨間推播完成。")


if __name__ == "__main__":
    run()
