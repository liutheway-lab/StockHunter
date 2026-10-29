import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# =====================================================
# 1. 設定區（想調整策略，只改這一區）
# =====================================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

LOOKBACK = 30             # 在最近 30 個交易日內找前波高點 B
TRANCHE_ENTRY = 0.30      # 進場點投入 30%
TRANCHE_BREAKOUT = 0.40   # 突破點投入 40%
MAX_AVG_RATIO = 0.94      # 平均成本不高於 B 的 94%
PULLBACK_MAX = 0.20       # 回檔超過 20% 視為轉弱，不通知
VOL_RATIO = 0.60          # 今日量需 <= 高點日成交量的 60%
STOP_PCT = 0.10           # 停損：平均成本下方 10%
RRR = 2.5                 # 風險報酬比 2.5 倍
MIN_AVG_TURNOVER = 5e7    # 近 20 日平均成交金額 >= 5000 萬，過濾冷門股
BATCH = 100               # 每批下載幾檔

# 已進場的持股：{"股票代號": 突破點B}，突破時會通知你加碼。沒有就留空
# 例如：HOLDINGS = {"2330.TW": 1050.0}
HOLDINGS = {}

# 抓不到全市場清單時的備用清單
FALLBACK = ["2330.TW", "2317.TW", "2454.TW", "2382.TW", "3231.TW", "2603.TW"]

# 由上面參數推算：進場點最高可到 B 的幾倍（預設 0.86）
MAX_ENTRY_RATIO = round(
    (MAX_AVG_RATIO * (TRANCHE_ENTRY + TRANCHE_BREAKOUT) - TRANCHE_BREAKOUT) / TRANCHE_ENTRY, 4
)


# =====================================================
# 2. 取得全市場股票清單（上市 + 上櫃）
# =====================================================
def get_universe():
    items = {}
    for mode, suffix in ((2, ".TW"), (4, ".TWO")):
        url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "ms950"
        df = pd.read_html(io.StringIO(r.text))[0]
        df.columns = df.iloc[0]
        df = df.iloc[1:]
        df = df[df["CFICode"] == "ESVUFR"]  # 只留一般股票
        for s in df["有價證券代號及名稱"]:
            parts = str(s).split("\u3000")
            if len(parts) >= 2 and parts[0].isdigit() and len(parts[0]) == 4:
                items[parts[0] + suffix] = parts[1].strip()
    return items


# =====================================================
# 3. 下載資料
# =====================================================
def download_batch(tickers):
    for _ in range(3):
        try:
            data = yf.download(
                tickers, period="8mo", interval="1d", group_by="ticker",
                auto_adjust=False, threads=True, progress=False,
            )
            if data is not None and not data.empty:
                return data
        except Exception as e:
            print(f"下載失敗，重試中: {e}")
        time.sleep(5)
    return None


def get_frame(data, ticker):
    if isinstance(data.columns, pd.MultiIndex):
        if ticker not in data.columns.get_level_values(0):
            return None
        df = data[ticker]
    else:
        df = data
    df = df.dropna(subset=["Close"])
    return df if not df.empty else None


# =====================================================
# 4. 策略判斷
# =====================================================
def analyze(df):
    if len(df) < 70:
        return None

    # 流動性過濾
    turnover = (df["Close"] * df["Volume"]).rolling(20).mean().iloc[-1]
    if not turnover >= MIN_AVG_TURNOVER:
        return None

    # 前波高點 B，且必須是真的創高（高於更早之前的最高點）
    win = df.iloc[-LOOKBACK:]
    B = float(win["High"].max())
    B_idx = win["High"].idxmax()
    prior_high = float(df["High"].iloc[:-LOOKBACK].max())
    if B <= prior_high:
        return None

    close = float(df["Close"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])
    ratio = close / B

    # 回檔深度：14% ~ 20%
    if not (1 - PULLBACK_MAX <= ratio <= MAX_ENTRY_RATIO):
        return None

    # 量縮
    vol_B = float(df.loc[B_idx, "Volume"])
    mav20 = float(df["Volume"].rolling(20).mean().iloc[-1])
    if not (vol <= VOL_RATIO * vol_B and vol <= mav20):
        return None

    # 大趨勢仍向上：EMA20 在 EMA60 之上，且收盤仍站在 EMA60 之上
    ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
    ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
    if not (ema20 > ema60 and close > ema60):
        return None

    # 交易計畫
    entry = close
    avg = (TRANCHE_ENTRY * entry + TRANCHE_BREAKOUT * B) / (TRANCHE_ENTRY + TRANCHE_BREAKOUT)
    return {
        "close": close,
        "B": B,
        "pullback": (1 - ratio) * 100,
        "avg": avg,
        "avg_ratio": avg / B * 100,
        "stop_first": entry * (1 - STOP_PCT),
        "stop_avg": avg * (1 - STOP_PCT),
        "target": avg * (1 + STOP_PCT * RRR),
    }


def format_match(m):
    return (
        f"📌 {m['ticker'][:-3] if m['ticker'].endswith('.TW') else m['ticker'][:-4]} {m['name']}\n"
        f"前波高點 B：{m['B']:.2f}\n"
        f"現價(進場)：{m['close']:.2f}（回檔 -{m['pullback']:.1f}%）\n"
        f"突破點：{m['B']:.2f}\n"
        f"預估平均成本：{m['avg']:.2f}（B 的 {m['avg_ratio']:.1f}%）\n"
        f"停損：{m['stop_first']:.2f}（只買第一筆）／{m['stop_avg']:.2f}（兩筆都買）\n"
        f"目標價：{m['target']:.2f}（風報比 {RRR}:1）"
    )


# =====================================================
# 5. Telegram 發送
# =====================================================
def send_telegram(blocks):
    text_all = "\n\n".join(blocks)
    if not TG_TOKEN or not TG_CHAT:
        print(text_all)
        print("（尚未設定 Telegram 金鑰，只在畫面印出，沒有推播）")
        return

    # 分段發送，避免超過 Telegram 單則 4096 字上限
    chunks, cur = [], ""
    for b in blocks:
        if len(cur) + len(b) + 2 > 3800 and cur:
            chunks.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        chunks.append(cur)

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    for c in chunks:
        r = requests.post(url, json={"chat_id": TG_CHAT, "text": c}, timeout=15)
        if not r.ok:
            print(f"Telegram 發送失敗: {r.status_code} {r.text}")


# =====================================================
# 6. 主流程
# =====================================================
def check_holdings():
    alerts = []
    if not HOLDINGS:
        return alerts
    data = download_batch(list(HOLDINGS))
    if data is None:
        return alerts
    for t, B in HOLDINGS.items():
        df = get_frame(data, t)
        if df is not None and float(df["Close"].iloc[-1]) > B:
            alerts.append(
                f"🔔 持股 {t} 已突破前高 {B}，現價 {float(df['Close'].iloc[-1]):.2f}\n"
                f"依計畫可投入 {int(TRANCHE_BREAKOUT * 100)}% 加碼"
            )
    return alerts


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    notes = []

    try:
        universe = get_universe()
        if len(universe) < 500:
            raise ValueError(f"清單數量異常，只有 {len(universe)} 檔")
    except Exception as e:
        universe = {t: t for t in FALLBACK}
        notes.append(f"⚠️ 全市場清單抓取失敗，今天只掃備用清單 {len(universe)} 檔（{e}）")

    tickers = list(universe)
    matches, ok, failed, last_date = [], 0, 0, None

    for i in range(0, len(tickers), BATCH):
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
                res = analyze(df)
                if res:
                    res["ticker"] = t
                    res["name"] = universe[t]
                    matches.append(res)
            except Exception as e:
                failed += 1
                print(f"處理 {t} 出錯: {e}")
        time.sleep(1)

    if failed > len(tickers) * 0.3:
        notes.append(f"⚠️ 有 {failed} 檔抓不到資料，今天結果可能不完整")

    # 組訊息：摘要一定會發（心跳通知）
    date_str = str(last_date) if last_date else "無資料"
    head = (
        f"📊 台股掃描完成 {now.strftime('%Y-%m-%d %H:%M')}\n"
        f"資料日期：{date_str}\n"
        f"成功掃描 {ok} 檔 ｜ 符合條件 {len(matches)} 檔"
    )
    if not matches:
        head += "\n今天沒有進場機會，機器人運作正常 ✅"

    blocks = [head] + notes
    blocks += [format_match(m) for m in matches]
    blocks += check_holdings()
    send_telegram(blocks)


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        send_telegram([f"⚠️ 今日掃描失敗：{e}"])
        raise
