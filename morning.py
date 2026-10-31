"""morning.py — 晨間國際行情速報（台灣時間 08:30）
想增減項目，只改下面的 SYMBOLS 清單。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from notify import send_telegram

# 顯示名稱 -> Yahoo 代號
SYMBOLS = {
    "小Nasdaq期": "NQ=F",
    "小S&P期": "ES=F",
    "小道瓊期": "YM=F",
    "日經225": "^N225",
    "韓國KOSPI": "^KS11",
    "恐慌指數VIX": "^VIX",
}


def get_quote(symbol):
    """回傳 (最新價, 前一日收盤, 最新資料日期)；抓不到會丟出例外"""
    tk = yf.Ticker(symbol)
    hist = tk.history(period="5d", interval="1d")
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        raise ValueError("資料不足")
    curr = float(hist["Close"].iloc[-1])
    last_date = hist.index[-1]

    prev = None
    try:
        fi = getattr(tk, "fast_info", None)
        if fi is not None and getattr(fi, "previous_close", None):
            prev = float(fi.previous_close)
    except Exception:
        pass
    if not prev or prev <= 0:
        prev = float(hist["Close"].iloc[-2])
    return curr, prev, last_date


def format_line(label, curr, prev, last_date):
    diff = curr - prev
    pct = diff / prev * 100
    # 台股習慣：紅漲、綠跌
    if diff > 0:
        icon, sign = "🔴", "+"
    elif diff < 0:
        icon, sign = "🟢", ""
    else:
        icon, sign = "⚪", ""
    if curr >= 1000:
        price, chg = f"{curr:,.1f}", f"{sign}{diff:,.1f}"
    else:
        price, chg = f"{curr:,.2f}", f"{sign}{diff:,.2f}"
    warn = " ⚠️數值偏大，請核對" if abs(pct) > 40 else ""
    return f"{icon} *{label}*：`{price}` ｜ `{chg}`（`{pct:+.2f}%`）· {last_date:%m/%d}{warn}"


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    lines, failed = [], 0
    for label, sym in SYMBOLS.items():
        try:
            curr, prev, d = get_quote(sym)
            lines.append(format_line(label, curr, prev, d))
        except Exception as e:
            failed += 1
            lines.append(f"⚠️ *{label}*：抓不到（{e}）")

    report = (
        f"☀️ *【台股晨間作戰速報 · 08:30】*\n"
        f"📅 `{now:%Y-%m-%d %H:%M}`\n"
        f"────────────────────\n"
        f"🌍 *國際盤態與期指：*\n" + "\n".join(lines) +
        f"\n────────────────────\n"
        f"💡 每行最後是資料日期。08:45 台指期開盤、09:00 現貨開盤。"
    )
    send_telegram([report])
    if failed == len(SYMBOLS):
        raise SystemExit("全部項目都抓不到，請檢查網路或 Yahoo 是否限流")


if __name__ == "__main__":
    run()
