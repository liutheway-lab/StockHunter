import argparse
import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# =====================================================
# 1. 核心參數與環境變數
# =====================================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")
SHEET_CSV_URL = os.getenv("HOLDINGS_SHEET_URL", "")

# 流動性門檻
MIN_VOL_SHARES = 1000     # 5 日均量 >= 1000 張
MIN_AVG_TURNOVER = 5e7    # 20 日均成交金額 >= 5000 萬台幣

# 策略一：創高拉回金字塔參數 (30% / 40% / 30%)
TRANCHE_1 = 0.30
TRANCHE_2 = 0.40
TRANCHE_3 = 0.30
EXT_RATIO = 1.08          # 第三筆加碼點位：前高 * 1.08 (+8%)

LOOKBACK = 30
MAX_AVG_RATIO = 0.94      # 前兩筆累計均價不高於前高 94% (等價於回檔至少 14%)
PULLBACK_MAX = 0.20       # 最大允許回檔 20%
VOL_RATIO = 0.60          # 量縮至突破日量的 60% 以下
STOP_PCT = 0.10           # 第一筆初始停損 10%
STOP_ADD_PCT = 0.05       # 第二筆加碼防守容忍度：跌破前高 5% 砍單
RRR = 2.5                 # 目標風報比 2.5 倍

MAX_ENTRY_RATIO = round(
    (MAX_AVG_RATIO * (TRANCHE_1 + TRANCHE_2) - TRANCHE_2) / TRANCHE_1, 4
)
MIN_ENTRY_RATIO = 1.0 - PULLBACK_MAX

# 策略二：低檔起漲爆量突破參數
BOTTOM_PLATFORM = 30      # 盤整頸線天數
BOTTOM_MAX_RISE = 0.18    # 離近 60 日最低點不超過 18%
BOTTOM_VOL_MULT = 2.0     # 今日量能 >= 20 日均量 2.0 倍
BOTTOM_STOP_PCT = 0.07    # 頸線下緣停損 7%

BATCH = 60
FALLBACK = ["2330.TW", "2317.TW", "2454.TW", "2382.TW", "3231.TW", "2603.TW"]


# =====================================================
# 2. 輔助工具函式
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


def send_telegram(blocks):
    if not TG_TOKEN or not TG_CHAT:
        print("\n[模擬輸出 Telegram 訊息]")
        for b in blocks:
            print(b)
        return

    target_chats = [cid.strip() for cid in TG_CHAT.split(",") if cid.strip()]
    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 4 > 3500 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        chunks.append(cur)

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for chat_id in target_chats:
        for c in chunks:
            try:
                res = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": c, "parse_mode": "Markdown", "disable_web_page_preview": True},
                    timeout=15
                )
                if not res.ok:
                    requests.post(
                        url,
                        json={"chat_id": chat_id, "text": c.replace("*", "").replace("`", "")},
                        timeout=15
                    )
            except Exception as e:
                print(f"Telegram 發送異常 ({chat_id}): {e}")


def get_remote_holdings():
    if not SHEET_CSV_URL:
        return {}
    try:
        df = pd.read_csv(SHEET_CSV_URL)
        df.columns = [str(c).strip() for c in df.columns]
        holdings = {}
        for _, row in df.iterrows():
            ticker = str(row.get("ticker", "")).strip()
            if not ticker or pd.isna(row.get("entry")):
                continue
            holdings[ticker] = {
                "entry": float(row["entry"]),
                "B": float(row["B"]),
                "stage": int(row.get("stage", 1))
            }
        return holdings
    except Exception as e:
        print(f"讀取 Google Sheets 失敗: {e}")
        return {}


def check_tdcc_increasing(stock_id: str):
    clean_id = stock_id.replace(".TWO", "").replace(".TW", "")
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={clean_id}"
    try:
        r = requests.get(url, timeout=6, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            tables = pd.read_html(io.StringIO(r.text))
            for t in tables:
                if len(t.columns) >= 7 and len(t) >= 4:
                    col_idx = 7 if len(t.columns) > 7 else -2
                    w0 = float(str(t.iloc[1, col_idx]).replace("%", ""))
                    w1 = float(str(t.iloc[2, col_idx]).replace("%", ""))
                    w2 = float(str(t.iloc[3, col_idx]).replace("%", ""))

                    if w0 > w1 > w2:
                        diff = round(w0 - w2, 2)
                        return True, w0, diff
    except Exception:
        pass
    return False, None, None


def get_tv_link(ticker: str):
    if ticker.endswith(".TWO"):
        return ticker.replace(".TWO", ""), f"https://tw.tradingview.com/chart/?symbol=TPEX%3A{ticker.replace('.TWO', '')}"
    return ticker.replace(".TW", ""), f"https://tw.tradingview.com/chart/?symbol=TWSE%3A{ticker.replace('.TW', '')}"


# =====================================================
# 3. 策略分析模組
# =====================================================
def analyze_pullback(df, ticker, info, is_large_inc, latest_ratio, diff):
    if not is_large_inc:
        return None

    close = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])

    avg_vol_5 = df["Volume"].rolling(5).mean().iloc[-1]
    if avg_vol_5 < (MIN_VOL_SHARES * 1000):
        return None
    turnover = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]
    if pd.isna(turnover) or turnover < MIN_AVG_TURNOVER:
        return None

    search_window = df.iloc[-LOOKBACK:-2]
    if search_window.empty:
        return None
    B = float(search_window["High"].max())
    B_idx = search_window["High"].idxmax()

    prior_window = df.iloc[:-LOOKBACK]
    if not prior_window.empty and B < float(prior_window["High"].max()) * 0.98:
        return None

    ratio = close / B
    if not (MIN_ENTRY_RATIO <= ratio <= MAX_ENTRY_RATIO):
        return None

    vol_B = float(df.loc[B_idx, "Volume"])
    mav20 = float(df["Volume"].rolling(20).mean().iloc[-1])
    if vol_B <= 0 or mav20 <= 0 or not (vol <= VOL_RATIO * vol_B and vol <= mav20):
        return None

    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
    if not (ema20 > ema60 and close >= ema60 * 0.98):
        return None

    entry_price = to_tw_tick(close)
    b_price = to_tw_tick(B)
    b2_price = to_tw_tick(B * EXT_RATIO)

    avg_1 = entry_price
    avg_2 = to_tw_tick((TRANCHE_1 * entry_price + TRANCHE_2 * b_price) / (TRANCHE_1 + TRANCHE_2))
    avg_3 = to_tw_tick((TRANCHE_1 * entry_price + TRANCHE_2 * b_price + TRANCHE_3 * b2_price) / 1.0)

    stop_second = to_tw_tick(b_price * (1 - STOP_ADD_PCT))
    loss_second = (stop_second - b_price) * TRANCHE_2
    breakeven_entry = to_tw_tick((entry_price * TRANCHE_1 - loss_second) / TRANCHE_1)

    return {
        "ticker": ticker,
        "name": info["name"],
        "industry": info["industry"],
        "close": entry_price,
        "B": b_price,
        "B2": b2_price,
        "pullback": round((1 - entry_price / b_price) * 100, 1),
        "avg_1": avg_1,
        "avg_2": avg_2,
        "avg_3": avg_3,
        "stop_first": to_tw_tick(entry_price * (1 - STOP_PCT)),
        "stop_second": stop_second,
        "breakeven_entry": breakeven_entry,
        "target": to_tw_tick(avg_3 * (1 + STOP_PCT * RRR)),
        "large_status": f"連3週增持 (+{diff}%) ｜ 現持股 {latest_ratio:.1f}%",
    }


def analyze_bottom(df, ticker, info, is_large_inc, latest_ratio, diff):
    if not is_large_inc:
        return None

    close = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])

    avg_vol_5 = df["Volume"].rolling(5).mean().iloc[-1]
    if avg_vol_5 < (MIN_VOL_SHARES * 1000):
        return None
    turnover = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]
    if pd.isna(turnover) or turnover < MIN_AVG_TURNOVER:
        return None

    low_60 = float(df["Low"].iloc[-60:].min())
    if low_60 <= 0 or (close - low_60) / low_60 > BOTTOM_MAX_RISE:
        return None

    platform_window = df.iloc[-(BOTTOM_PLATFORM + 1):-1]
    if platform_window.empty:
        return None
    neckline = float(platform_window["High"].max())

    if close <= neckline:
        return None

    mav20_vol = float(df["Volume"].rolling(20).mean().iloc[-1])
    if mav20_vol <= 0 or (vol / mav20_vol) < BOTTOM_VOL_MULT:
        return None

    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema20_prev = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-2])
    ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
    if not (close >= ema60 and ema20 > ema20_prev):
        return None

    entry_price = to_tw_tick(close)
    neckline_tick = to_tw_tick(neckline)
    vol_mult = round(vol / mav20_vol, 1)

    stop_loss = to_tw_tick(min(neckline_tick * 0.98, entry_price * (1 - BOTTOM_STOP_PCT)))
    target_price = to_tw_tick(entry_price * 1.20)

    return {
        "ticker": ticker,
        "name": info["name"],
        "industry": info["industry"],
        "close": entry_price,
        "neckline": neckline_tick,
        "vol_mult": vol_mult,
        "stop_loss": stop_loss,
        "target": target_price,
        "large_status": f"連3週增持 (+{diff}%) ｜ 現持股 {latest_ratio:.1f}%",
    }


# =====================================================
# 4. 執行模組 (晨間速報 · 修正基準點數與漲跌幅)
# =====================================================
def run_morning():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    symbols = {
        "小Nasdaq期": "NQ=F",
        "小S&P期": "ES=F",
        "小道瓊期": "YM=F",
        "日經225": "^N225",
        "韓國KOSPI": "^KS11",
        "恐慌指數VIX": "^VIX",
    }

    market_lines = []

    for label, sym in symbols.items():
        try:
            tk = yf.Ticker(sym)
            hist = tk.history(period="5d", interval="1d")

            if hist.empty or len(hist) < 2:
                continue

            curr = float(hist["Close"].iloc[-1])

            # 優先讀取官方標準前日收盤價 (Previous Close)
            prev = None
            try:
                fast_info = getattr(tk, "fast_info", None)
                if fast_info and hasattr(fast_info, "previous_close") and fast_info.previous_close:
                    prev = float(fast_info.previous_close)
            except Exception:
                pass

            if not prev or prev <= 0:
                prev = float(hist["Close"].iloc[-2])

            diff_pts = curr - prev
            pct_chg = (diff_pts / prev) * 100

            # 台股慣例：紅漲 🔴，綠跌 🟢
            if diff_pts > 0:
                icon = "🔴"
                sign = "+"
            elif diff_pts < 0:
                icon = "🟢"
                sign = ""
            else:
                icon = "⚪"
                sign = " "

            # 依指數大小切換小數點位數
            if curr >= 1000:
                curr_str = f"{curr:,.1f}"
                diff_str = f"{sign}{diff_pts:,.1f} 點"
            else:
                curr_str = f"{curr:,.2f}"
                diff_str = f"{sign}{diff_pts:,.2f}"

            market_lines.append(
                f"{icon} *{label}*：`{curr_str}` ｜ `{diff_str}`（`{pct_chg:+.2f}%`）"
            )
        except Exception as e:
            market_lines.append(f"⚠️ *{label}*：獲取異常 ({e})")

    report = (
        f"☀️ *【台股晨間作戰速報 · 08:30】*\n"
        f"📅 執行時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
        f"────────────────────\n"
        f"🌍 *即時國際盤態與期指走勢：*\n" +
        "\n".join(market_lines) +
        f"\n────────────────────\n"
        f"💡 08:45 台指期開盤、09:00 現貨開盤，請搭配昨夜作戰清單進場。"
    )

    send_telegram([report])
    print("晨間國際行情推播完成。")


# =====================================================
# 5. 執行模組 (尾盤哨兵 · 13:18)
# =====================================================
def run_monitor():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    holdings = get_remote_holdings()
    if not holdings:
        print(f"[{now.strftime('%H:%M:%S')}] Google 試算表目前無任何持股需監控。")
        return

    tickers = list(holdings.keys())
    try:
        data = yf.download(tickers, period="5d", interval="1d", group_by="ticker", auto_adjust=True, progress=False)
    except Exception as e:
        print(f"獲取持股報價異常: {e}")
        return

    alerts, safe_lines = [], []
    for t, info in holdings.items():
        try:
            if isinstance(data.columns, pd.MultiIndex):
                sub = data[t].dropna(subset=["Close"]) if t in data.columns.levels[0] else data.xs(t, axis=1, level=1).dropna(subset=["Close"])
            else:
                sub = data.dropna(subset=["Close"])
            if sub.empty:
                continue

            curr = to_tw_tick(float(sub["Close"].iloc[-1]))
            E, B, stage = info["entry"], info["B"], info["stage"]

            stop_second = to_tw_tick(B * (1 - STOP_ADD_PCT))
            loss_second = (stop_second - B) * TRANCHE_2
            breakeven = to_tw_tick((E * TRANCHE_1 - loss_second) / TRANCHE_1)

            if stage == 1:
                stop_first = to_tw_tick(E * (1 - STOP_PCT))
                if curr >= B:
                    alerts.append(f"🚀 *【加碼訊號】{t}* 現價 `{curr}` 突破前高 `{B}`！請動用 40% 資金加碼第 2 筆，並將試算表 stage 改為 2。")
                elif curr <= stop_first:
                    alerts.append(f"🚨 *【停損警報】{t}* 現價 `{curr}` 跌破停損點 `{stop_first}` (-10%)！請全數平倉第 1 筆。")
                else:
                    safe_lines.append(f"• *{t}* (階段1): 現價 `{curr}` ｜ 停損守 `{stop_first}` ｜ 前高突破看 `{B}`")

            elif stage == 2:
                ext_price = to_tw_tick(B * EXT_RATIO)
                if curr <= stop_second:
                    alerts.append(
                        f"🚨 *【第 2 筆加碼停損】{t}* 現價 `{curr}` 跌破專屬防守價 `{stop_second}` (-5%)！\n"
                        f"👉 建議平倉砍掉加碼的 40% 部位。第 1 筆保本底線守 `{breakeven}`。請將 stage 改回 1。"
                    )
                elif curr >= ext_price:
                    alerts.append(f"🌟 *【第 3 筆打滿】{t}* 現價 `{curr}` 突破延伸點 `{ext_price}` (+8%)！請動用剩餘 30% 打滿部位，防守移至前高 `{B}`。")
                else:
                    safe_lines.append(f"• *{t}* (階段2): 現價 `{curr}` ｜ 假突破防守守 `{stop_second}`")
        except Exception:
            continue

    if alerts:
        send_telegram([f"⚡ *【台股尾盤防守警報 · 13:18】*\n📅 時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n────────────────────"] + alerts)
    elif safe_lines:
        send_telegram([f"🛡️ *【台股尾盤持股安全回報 · 13:18】*\n📅 時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n────────────────────\n" + "\n".join(safe_lines)])


# =====================================================
# 6. 執行模組 (夜間雙策略掃描 · 22:30)
# =====================================================
def run_night():
    now = datetime.now(ZoneInfo("Asia/Taipei"))

    universe = {}
    for mode, suffix in ((2, ".TW"), (4, ".TWO")):
        url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
        try:
            r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
            r.encoding = "ms950"
            tables = pd.read_html(io.StringIO(r.text))
            if tables:
                df = tables[0]
                df.columns = df.iloc[0]
                df = df.iloc[1:]
                df = df[df["CFICode"] == "ESVUFR"]
                for _, row in df.iterrows():
                    s = str(row["有價證券代號及名稱"])
                    ind = str(row.get("產業別", "熱門標的")).strip()
                    parts = s.split("\u3000")
                    if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4:
                        universe[parts[0] + suffix] = {
                            "name": parts[1].strip(),
                            "industry": ind if ind and ind != "nan" else "科技題材"
                        }
        except Exception:
            pass

    if len(universe) < 500:
        universe = {t: {"name": t, "industry": "科技"} for t in FALLBACK}

    tickers = list(universe.keys())
    pullbacks, bottoms = [], []
    total = len(tickers)
    print(f"[{now.strftime('%H:%M:%S')}] 開始執行 22:30 夜間雙策略掃描 ({total} 檔)...")

    for i in range(0, total, BATCH):
        batch = tickers[i:i + BATCH]
        try:
            data = yf.download(
                batch, period="8mo", interval="1d", group_by="ticker",
                auto_adjust=True, threads=True, progress=False
            )
        except Exception:
            continue

        for t in batch:
            try:
                if isinstance(data.columns, pd.MultiIndex):
                    sub = data[t].dropna(subset=["Close"]) if t in data.columns.levels[0] else data.xs(t, axis=1, level=1).dropna(subset=["Close"])
                else:
                    sub = data.dropna(subset=["Close"])
                if len(sub) < 70:
                    continue

                is_inc, ratio, diff = check_tdcc_increasing(t)

                pb = analyze_pullback(sub, t, universe[t], is_inc, ratio, diff)
                if pb:
                    pullbacks.append(pb)

                bt = analyze_bottom(sub, t, universe[t], is_inc, ratio, diff)
                if bt:
                    bottoms.append(bt)
            except Exception:
                continue
        time.sleep(1)

    head = (
        f"🌙 *【台股夜間雙策略作戰名單 · 22:30】*\n"
        f"📅 結算時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
        f"🎯 創高拉回符合：`{len(pullbacks)}` 檔 ｜ 🌱 低檔起漲符合：`{len(bottoms)}` 檔\n"
        f"⚙️ 核心濾網：400張大戶近3週連續增持 ｜ 法定Tick報價\n"
        f"────────────────────"
    )
    blocks = [head]

    if pullbacks:
        blocks.append("👑 *【第一類：創高量縮回測名單】*")
        for m in pullbacks:
            c, link = get_tv_link(m["ticker"])
            blocks.append(
                f"📌 *【創高拉回】{c} {m['name']}* （{m['industry']}）\n"
                f"👑 *大戶籌碼*：`{m['large_status']}`\n\n"
                f"📍 *三段加碼點位與成本推估*\n"
                f"• 第 1 筆進場 (30%)：`{m['close']}`（回測 -{m['pullback']}% ｜ 均價：`{m['avg_1']}`）\n"
                f"• 第 2 筆加碼 (40%)：`{m['B']}`（突破前高 ｜ 均價：`{m['avg_2']}`）\n"
                f"• 第 3 筆加碼 (30%)：`{m['B2']}`（突破延伸 +8% ｜ 打滿均價：`{m['avg_3']}`）\n\n"
                f"🛡️ *階梯防守與目標（給予 5% 洗盤緩衝）*\n"
                f"• 第 1 筆停損：`{m['stop_first']}`（單筆 -10%）\n"
                f"• 第 2 筆防守：`{m['stop_second']}`（跌破前高 -5% ｜ 僅砍加碼單）\n"
                f"• 第 1 筆保本：`{m['breakeven_entry']}`（第 2 筆砍後，整趟平手底線）\n"
                f"🎯 *波段目標價*：`{m['target']}`（風報比 2.5:1）\n\n"
                f"🔗 [開啟 TradingView 圖表]({link})\n"
                f"────────────────────"
            )

    if bottoms:
        blocks.append("🌱 *【第二類：低檔爆量起漲名單】*")
        for m in bottoms:
            c, link = get_tv_link(m["ticker"])
            blocks.append(
                f"🌱 *【低檔起漲】{c} {m['name']}* （{m['industry']}）\n"
                f"👑 *大戶籌碼*：`{m['large_status']}`\n\n"
                f"🚀 *底部出量突破關鍵點位*\n"
                f"• 今日突破進場價：`{m['close']}`（帶量突破 30 日頸線 `{m['neckline']}`）\n"
                f"• 攻擊量能爆發：`{m['vol_mult']} 倍` 20日均量\n"
                f"• 均線結構：帶量突破站上 60EMA，20EMA 向上翻揚\n\n"
                f"🛡️ *防守與波段目標*\n"
                f"• 頸線防守停損：`{m['stop_loss']}`（跌破平台下緣 -7% 出場）\n"
                f"• 首波波段目標：`{m['target']}`（獲利空間預期 +20%）\n\n"
                f"🔗 [開啟 TradingView 圖表]({link})\n"
                f"────────────────────"
            )

    if not pullbacks and not bottoms:
        blocks.append("\n今日雙策略均無符合標的，明日保持空手觀望 ✅")

    send_telegram(blocks)
    print("夜間選股推播完成。")


# =====================================================
# 7. 主程式進入點
# =====================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Screener Agent")
    parser.add_argument("--mode", choices=["night", "morning", "monitor"], default="night")
    args = parser.parse_args()

    if args.mode == "morning":
        run_morning()
    elif args.mode == "monitor":
        run_monitor()
    else:
        run_night()
