"""morning.py — 晨間國際行情速報（台灣時間 08:30）
想增減項目，只改下面的 GROUPS。
顏色規則：🟢 = 利多、🔴 = 利空（一般上漲是利多；VIX 上漲是利空，已自動處理）。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import yfinance as yf

from notify import send_telegram

# 分組顯示：(組名, {顯示名稱: Yahoo 代號})
GROUPS = [
    ("🇺🇸 美股期貨", {"小Nasdaq期": "NQ=F", "小S&P期": "ES=F", "小道瓊期": "YM=F"}),
    ("🌏 亞洲股市", {"日經225": "^N225", "韓國KOSPI": "^KS11"}),
    ("😨 恐慌指數", {"VIX": "^VIX"}),
]

# 「上漲代表壞事」的項目：上漲顯示 🔴、下跌顯示 🟢
INVERSE_SYMBOLS = {"^VIX"}

WEEKDAY = "一二三四五六日"
LINE = "━━━━━━━━━━━━━━"


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


def format_item(label, symbol, curr, prev, d, group_date):
    diff = curr - prev
    pct = diff / prev * 100
    flat = round(diff, 4) == 0
    if flat:
        icon = "⚪"
    else:
        good = diff > 0
        if symbol in INVERSE_SYMBOLS:
            good = not good
        icon = "🟢" if good else "🔴"

    if curr >= 1000:
        price, chg = f"{curr:,.1f}", f"{diff:+,.1f}"
    else:
        price, chg = f"{curr:,.2f}", f"{diff:+,.2f}"
    pct_txt = "0.00%" if flat else f"{pct:+.2f}%"

    note = ""
    if d.date() != group_date:
        note += f"　⚠️資料日 {d:%m/%d}"
    if abs(pct) > 40:
        note += "　⚠️數值偏大，請核對"
    return f"{icon} *{label}*　*{pct_txt}*\n　　{price}　（{chg}）{note}"


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    today = now.date()
    blocks, total, failed = [], 0, 0

    for title, items in GROUPS:
        rows, dates = [], []
        for label, sym in items.items():
            total += 1
            try:
                curr, prev, d = get_quote(sym)
                rows.append((label, sym, curr, prev, d))
                dates.append(d.date())
            except Exception as e:
                failed += 1
                rows.append((label, sym, None, str(e), None))

        gdate = max(dates) if dates else None
        head = f"*{title}*"
        if gdate:
            head += f"　{gdate:%m/%d}"
            if (today - gdate).days > 4:
                head += "　⚠️資料較舊（可能休市）"

        lines = []
        for label, sym, curr, prev, d in rows:
            if curr is None:
                lines.append(f"⚠️ *{label}*　抓不到（{prev}）")
            else:
                lines.append(format_item(label, sym, curr, prev, d, gdate))
        blocks.append(head + "\n" + "\n".join(lines))

    top = [
        "☀️ *台股晨間速報*",
        f"🗓 {now:%m/%d}（{WEEKDAY[now.weekday()]}）{now:%H:%M}",
    ]
    if now.weekday() >= 5:
        top.append("📌 週末｜以下為最後交易日資料，非即時行情")

    report = (
        "\n".join(top) + f"\n{LINE}\n\n"
        + "\n\n".join(blocks)
        + f"\n\n{LINE}\n"
        "🟢 利多　🔴 利空\n"
        "⏰ 08:45 台指期開盤　09:00 現貨開盤"
    )
    send_telegram([report])
    if failed == total:
        raise SystemExit("全部項目都抓不到，請檢查網路或 Yahoo 是否限流")


if __name__ == "__main__":
    run()
