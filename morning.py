"""morning.py — 晨間國際行情速報（台灣時間 08:30）
想增減項目，只改下面的 SYMBOLS 清單。
顏色規則：🟢 = 對股市有利、🔴 = 對股市不利。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from notify import send_telegram

VERSION = "morning v2"

# 顯示名稱 -> Yahoo 代號
SYMBOLS = {
    "小Nasdaq期": "NQ=F",
    "小S&P期": "ES=F",
    "小道瓊期": "YM=F",
    "日經225": "^N225",
    "韓國KOSPI": "^KS11",
    "恐慌指數VIX": "^VIX",
}

# 「上漲代表壞事」的項目：上漲顯示 🔴、下跌顯示 🟢
INVERSE_SYMBOLS = {"^VIX"}


def get_quote(symbol):
    """回傳 (最新價, 前收, 最新資料日期)。
    最新價與前收都取自同一份日線資料（最後一筆與倒數第二筆），所以點數與百分比必定一致。"""
    hist = yf.Ticker(symbol).history(period="10d", interval="1d")
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        raise ValueError("資料不足")
    curr = float(hist["Close"].iloc[-1])
    prev = float(hist["Close"].iloc[-2])
    if prev <= 0:
        raise ValueError("前收異常")
    return curr, prev, hist.index[-1]


def format_line(label, symbol, curr, prev, last_date, today):
    diff = curr - prev
    pct = diff / prev * 100
    if round(diff, 4) == 0:
        icon = "⚪"
    else:
        good = diff > 0
        if symbol in INVERSE_SYMBOLS:
            good = not good
        icon = "🟢" if good else "🔴"

    if curr >= 1000:
        price, chg, base = f"{curr:,.1f}", f"{diff:+,.1f}", f"{prev:,.1f}"
    else:
        price, chg, base = f"{curr:,.2f}", f"{diff:+,.2f}", f"{prev:,.2f}"
    warn = " ⚠️數值偏大，請核對" if abs(pct) > 40 else ""
    if (today - last_date.date()).days > 4:
        warn += " ⚠️資料較舊（可能休市）"
    return (f"{icon} *{label}*：`{price}` ｜ `{chg}`（`{pct:+.2f}%`）\n"
            f"　　前收 {base} · 資料日 {last_date:%m/%d}{warn}")


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    lines, failed = [], 0
    for label, sym in SYMBOLS.items():
        try:
            curr, prev, d = get_quote(sym)
            lines.append(format_line(label, sym, curr, prev, d, now.date()))
        except Exception as e:
            failed += 1
            lines.append(f"⚠️ *{label}*：抓不到（{e}）")

    weekend = ""
    if now.weekday() >= 5:
        weekend = "📌 *今天是週末*：以下是最後一個交易日的資料（每行有資料日），不是即時行情。\n"

    report = (
        f"☀️ *【台股晨間作戰速報 · 08:30】*\n"
        f"📅 `{now:%Y-%m-%d %H:%M}`\n"
        f"{weekend}"
        f"────────────────────\n"
        f"🌍 *國際盤態與期指：*\n" + "\n".join(lines) +
        f"\n────────────────────\n"
        f"🟢 上漲＝好事　🔴 下跌＝壞事\n"
        f"（VIX 相反：VIX 上漲代表恐慌升高，顯示 🔴）\n"
        f"💡 08:45 台指期開盤、09:00 現貨開盤。　`{VERSION}`"
    )
    send_telegram([report])
    if failed == len(SYMBOLS):
        raise SystemExit("全部項目都抓不到，請檢查網路或 Yahoo 是否限流")


if __name__ == "__main__":
    run()
