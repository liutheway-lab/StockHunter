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
# 1. 核心參數設定
# =====================================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# 流動性與籌碼濾網門檻
MIN_VOL_SHARES = 1000     # 5 日均量 >= 1000 張 (張 = 股數 / 1000)
MIN_AVG_TURNOVER = 5e7    # 20 日均成交金額 >= 5000 萬台幣
MIN_LARGE_RATIO = 45.0    # 400 張以上大戶持股比率 >= 45%

# 三筆加碼權重與幾何點位 (30% + 40% + 30% = 100%)
TRANCHE_1 = 0.30          # 第一筆：左側回檔量縮接刀 (30%)
TRANCHE_2 = 0.40          # 第二筆：突破前高點 B 確認加碼 (40%)
TRANCHE_3 = 0.30          # 第三筆：主升段延伸突破加碼 (30%)
EXT_RATIO = 1.08          # 第三筆加碼點位：1.08 * B (+8%)

LOOKBACK = 30             # 尋找波段高點 B 的觀察天數
MAX_AVG_RATIO = 0.94      # 前兩筆平均成本不高於 B 的 94% (等價於回檔至少 14%)
PULLBACK_MAX = 0.20       # 最大允許回檔 20%
VOL_RATIO = 0.60          # 今日成交量需 <= 高點量能的 60%
STOP_PCT = 0.10           # 停損幅度 10%
RRR = 2.5                 # 風險報酬比 2.5 倍 (獲利目標 +25%)

# 第一筆進場容許上限：E <= 0.86B
MAX_ENTRY_RATIO = round(
    (MAX_AVG_RATIO * (TRANCHE_1 + TRANCHE_2) - TRANCHE_2) / TRANCHE_1, 4
)
MIN_ENTRY_RATIO = 1.0 - PULLBACK_MAX  # 0.80

BATCH = 60                # 批次下載數量
HOLDINGS = {}
FALLBACK = ["2330.TW", "2317.TW", "2454.TW", "2382.TW", "3231.TW", "2603.TW"]


# =====================================================
# 2. 輔助函式：台股法定跳動檔位 (Tick Size) 與強健推播
# =====================================================
def to_tw_tick(price: float) -> float:
    """
    將計算出的任意浮點數價格，轉換為符合台灣證券交易所規範的合法檔位。
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


def send_telegram(blocks):
    """
    強健推播函式：以明確區塊分隔發送，若 Markdown 語法報錯自動轉純文字降級重送。
    """
    if not TG_TOKEN or not TG_CHAT:
        print("\n❌ 錯誤：找不到 TG_BOT_TOKEN 或 TG_CHAT_ID，請檢查 GitHub Secrets！")
        for b in blocks:
            print(b)
        return

    target_chats = [cid.strip() for cid in TG_CHAT.split(",") if cid.strip()]

    # 組合訊息，在每張卡片/股票之間保留清晰間距
    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 4 > 3500 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n\n" if cur else "") + b
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


# =====================================================
# 3. 晨間模組 (08:30 專用)：國際市場行情速報
# =====================================================
def run_morning():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    symbols = {
        "日經225": "^N225",
        "韓國KOSPI": "^KS11",
        "小Nasdaq期": "NQ=F",
        "小S&P期": "ES=F",
        "小道瓊期": "YM=F",
        "恐慌指數VIX": "^VIX",
    }
    market_lines = []
    try:
        data = yf.download(
            list(symbols.values()), period="5d", interval="1d",
            progress=False, group_by="ticker"
        )
        for label, sym in symbols.items():
            try:
                sub = data[sym].dropna(subset=["Close"]) if isinstance(data.columns, pd.MultiIndex) else data.dropna(subset=["Close"])
                if len(sub) >= 2:
                    curr = float(sub["Close"].iloc[-1])
                    prev = float(sub["Close"].iloc[-2])
                    chg_pct = (curr - prev) / prev * 100
                    icon = "🔴" if chg_pct < 0 else "🟢"
                    market_lines.append(f"{icon} *{label}*：`{curr:,.1f}`（{chg_pct:+.2f}%）")
            except Exception:
                continue
    except Exception as e:
        market_lines.append(f"⚠️ 國際盤態獲取異常: {e}")

    report = (
        f"☀️ *【台股晨間作戰速報 · 08:30】*\n"
        f"📅 執行時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🌍 *即時國際盤態與期指走勢：*\n" +
        "\n".join(market_lines) +
        f"\n━━━━━━━━━━━━━━━━━━\n"
        f"💡 08:45 台指期開盤、09:00 現貨開盤，請搭配昨夜作戰清單進場。"
    )
    send_telegram([report])
    print("晨間國際行情推播完成。")


# =====================================================
# 4. 夜間模組 (22:30 專用)：全市場籌碼與幾何掃描
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
                    items[parts[0] + suffix] = {
                        "name": parts[1].strip(),
                        "industry": industry if industry and industry != "nan" else "科技題材"
                    }
        except Exception as e:
            print(f"清單抓取失敗 mode={mode}: {e}")
    return items


def get_tdcc_large_ratio(stock_id: str):
    clean_id = stock_id.replace(".TWO", "").replace(".TW", "")
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


def analyze(df, ticker, info):
    if len(df) < 70:
        return None

    close = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])

    # 1. 均量與均額流動性濾網
    avg_vol_5 = df["Volume"].rolling(5).mean().iloc[-1]
    if avg_vol_5 < (MIN_VOL_SHARES * 1000):
        return None

    turnover = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]
    if pd.isna(turnover) or turnover < MIN_AVG_TURNOVER:
        return None

    # 2. 定位前波高點 B (避開最近 2 天)
    search_window = df.iloc[-LOOKBACK:-2]
    if search_window.empty:
        return None
    B = float(search_window["High"].max())
    B_idx = search_window["High"].idxmax()

    prior_window = df.iloc[:-LOOKBACK]
    if not prior_window.empty and B < float(prior_window["High"].max()) * 0.98:
        return None

    ratio = close / B
    # 3. 回檔區間檢核：0.80B <= Close <= 0.86B (回測 14% ~ 20%)
    if not (MIN_ENTRY_RATIO <= ratio <= MAX_ENTRY_RATIO):
        return None

    # 4. 量縮檢核：成交量 <= 突破日量的 60% 且 <= 20 日均量
    vol_B = float(df.loc[B_idx, "Volume"])
    mav20 = float(df["Volume"].rolling(20).mean().iloc[-1])
    if vol_B <= 0 or mav20 <= 0 or not (vol <= VOL_RATIO * vol_B and vol <= mav20):
        return None

    # 5. 均線保護：EMA20 > EMA60 且收盤站上 EMA60
    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
    if not (ema20 > ema60 and close >= ema60 * 0.98):
        return None

    # 6. 神秘金字塔 400 張大戶檢核
    large_ratio = get_tdcc_large_ratio(ticker)
    if large_ratio is not None and large_ratio < MIN_LARGE_RATIO:
        return None

    # 7. 計算三筆金字塔加碼點位（全數對齊台股法定 Tick）
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
    ticker = m['ticker']
    if ticker.endswith(".TWO"):
        exchange = "TPEX"
        clean_code = ticker.replace(".TWO", "")
    else:
        exchange = "TWSE"
        clean_code = ticker.replace(".TW", "")

    tv_link = f"https://tw.tradingview.com/chart/?symbol={exchange}%3A{clean_code}"

    # 使用加粗框線與層次分明的結構，讓每一檔股票獨立成卡片
    return (
        f"════════════════════\n"
        f"📌 *【精選標的】{clean_code} {m['name']}* （{m['industry']}）\n"
        f"👑 *400張大戶持股*：`{m['large_ratio']}`\n"
        f"────────────────────\n"
        f"📍 *三段金字塔點位規劃*：\n"
        f"  • 第 1 筆進場 (30%)：`{m['close']}`（回測 -{m['pullback']}%）\n"
        f"  • 第 2 筆加碼 (40%)：`{m['B']}`（突破前高）\n"
        f"  • 第 3 筆加碼 (30%)：`{m['B2']}`（突破延伸 +8%）\n"
        f"  💎 *三筆打滿預估總均價*：`{m['avg_3']}`\n"
        f"────────────────────\n"
        f"🛡️ *防守與目標出場*：\n"
        f"  • 現階段初始停損：`{m['stop_first']}`（單筆 -10%）\n"
        f"  • 第 2 筆後防守：成本價保本出場\n"
        f"  • 第 3 筆後防守：`{m['B']}`（前高鎖定獲利）\n"
        f"  🎯 *波段目標價*：`{m['target']}`（風報比 {RRR}:1）\n"
        f"────────────────────\n"
        f"🔗 [點此開啟 TradingView 即時圖表]({tv_link})\n"
        f"════════════════════"
    )


def check_holdings():
    alerts = []
    if not HOLDINGS:
        return alerts

    data = download_batch(list(HOLDINGS.keys()))
    if data is None:
        return alerts

    for t, info in HOLDINGS.items():
        df = get_frame(data, t)
        if df is None:
            continue

        curr_close = float(df["Close"].iloc[-1])
        B = info["B"]
        stage = info.get("stage", 1)

        if stage == 1 and curr_close >= B:
            alerts.append(
                f"════════════════════\n"
                f"🔔 *【持股加碼通知】{t}*\n"
                f"現價 `{curr_close:.2f}` 已突破前高 `{B:.2f}`！\n"
                f"👉 執行指令：動用 40% 資金於 `{to_tw_tick(curr_close)}` 加碼。\n"
                f"🛡️ 防守更新：將第一筆停損價上推至進場成本（鎖定保本）。\n"
                f"════════════════════"
            )
        elif stage == 2 and curr_close >= (B * EXT_RATIO):
            alerts.append(
                f"════════════════════\n"
                f"🚀 *【主升段打滿通知】{t}*\n"
                f"現價 `{curr_close:.2f}` 站上延伸點 `{to_tw_tick(B * EXT_RATIO)}`！\n"
                f"👉 執行指令：動用剩餘 30% 資金打滿 100% 部位。\n"
                f"🛡️ 防守更新：全數部位停損點強制上推至前高 `{to_tw_tick(B)}`。\n"
                f"════════════════════"
            )
    return alerts


def run_night():
    now = datetime.now(ZoneInfo("Asia/Taipei"))

    try:
        universe = get_universe()
        if len(universe) < 500:
            raise ValueError(f"清單異常 ({len(universe)} 檔)")
    except Exception as e:
        universe = {t: {"name": t, "industry": "科技"} for t in FALLBACK}

    tickers = list(universe.keys())
    matches, ok, failed = [], 0, 0
    total = len(tickers)

    print(f"[{now.strftime('%H:%M:%S')}] 啟動 22:30 夜間籌碼與選股掃描 (共 {total} 檔)...")

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

        print(f"進度: {min(i + BATCH, total)}/{total} | 成功: {ok} | 篩出符合: {len(matches)}")
        time.sleep(1)

    head = (
        f"🌙 *【台股夜間作戰名單 · 22:30】*\n"
        f"📅 結算時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
        f"🔍 掃描母體：`{ok}` 檔 ｜ 🎯 觸發標的：`{len(matches)}` 檔\n"
        f"⚙️ 條件：信用籌碼已結算 ｜ 回測 14%~20% ｜ 400張大戶 > 45% ｜ 法定Tick報價"
    )
    if not matches:
        head += "\n\n今日無符合「創高回測量縮＋籌碼鎖定」標的，明日保持空手觀望 ✅"

    # 以獨立元素加入 blocks，避免字串黏接
    blocks = [head]
    for m in matches:
        blocks.append(format_match(m))
    blocks.extend(check_holdings())

    send_telegram(blocks)
    print("夜間選股推播完成。")


# =====================================================
# 5. 主程式入口與參數路由
# =====================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stock Screener Agent")
    parser.add_argument("--mode", choices=["night", "morning"], default="night",
                        help="執行模式：night (22:30 選股) 或 morning (08:30 國際盤)")
    args = parser.parse_args()

    if args.mode == "morning":
        run_morning()
    else:
        run_night()
