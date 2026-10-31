"""morning.py — 晨間國際行情速報（台灣時間 08:30）
想增減項目，只改下面的 GROUPS。
顏色規則：🟢 = 利多、🔴 = 利空（一般上漲是利多；VIX 上漲是利空，已自動處理）。
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
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

# 期交所官方開放資料（期貨每日行情，含「盤後」= 夜盤）
TAIFEX_URL = "https://openapi.taifex.com.tw/v1/DailyMarketReportFut"

# ── 今日結論的判斷規則（想調鬆緊就改這裡）──
US_SYMS = {"NQ=F", "ES=F", "YM=F"}   # 美股期貨（取平均）
ASIA_SYMS = {"^N225", "^KS11"}       # 亞洲股市（取平均）
FLAT = 0.3          # 漲跌幅絕對值小於這個 % 視為持平，不加不減分
VIX_WARN = 25       # VIX 高於這個值：扣 1 分
VIX_PANIC = 30      # VIX 高於這個值：直接判「不適合」
TX_CRASH = -1.5     # 夜盤跌幅(%)低於這個值：直接判「不適合」
ENTER_AT = 3        # 分數 >= 這個值：適合進場
AVOID_AT = -2       # 分數 <= 這個值：不適合進場

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


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def get_tx_night():
    """台指期（TX）近月夜盤。回傳 (收盤價, 參考價, 日期)。
    參考價 = 同一天日盤的結算價；找不到就用官方的漲跌欄位回推。"""
    r = requests.get(TAIFEX_URL, timeout=30,
                     headers={"accept": "application/json", "User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    rows = [x for x in r.json() if str(x.get("Contract", "")).strip() == "TX"]

    def month(x):
        return str(x.get("ContractMonth(Week)", "")).strip()

    def single(x):  # 只要單一月份（6 位數字），排除週選與跨月價差
        m = month(x)
        return m.isdigit() and len(m) == 6

    night = [x for x in rows if single(x) and "盤後" in str(x.get("TradingSession", ""))
             and _num(x.get("Last"))]
    if not night:
        raise ValueError("查無夜盤資料（期交所可能尚未更新）")
    near = min(night, key=month)
    last = _num(near["Last"])

    prev = None
    for x in rows:
        if single(x) and month(x) == month(near) and "一般" in str(x.get("TradingSession", "")):
            prev = _num(x.get("SettlementPrice"))
            break
    if not prev:
        chg = _num(near.get("Change"))
        if chg is None:
            raise ValueError("缺少參考價")
        prev = last - chg
    d = datetime.strptime(str(near.get("Date")).strip()[:8], "%Y%m%d")
    return last, prev, d


def judge(pcts, levels):
    """把夜盤、美股期貨、亞股、VIX 綜合成一個結論。
    計分：夜盤 ±2、美股期貨平均 ±2、亞股平均 ±1、VIX 變動 ±1（VIX 反向），
    另外 VIX 偏高會扣分或直接否決。回傳 (圖示, 結論, 依據清單, 分數, 滿分)"""
    score = max_score = 0
    parts, veto = [], None

    def vote(p, w):
        return w if p >= FLAT else (-w if p <= -FLAT else 0)

    tx = pcts.get("TX")
    if tx is not None:
        score += vote(tx, 2)
        max_score += 2
        parts.append(f"夜盤 {tx:+.2f}%")
        if tx <= TX_CRASH:
            veto = "夜盤跌幅過大"

    us = [pcts[k] for k in US_SYMS if k in pcts]
    if us:
        avg = sum(us) / len(us)
        score += vote(avg, 2)
        max_score += 2
        parts.append(f"美股期貨 {avg:+.2f}%")

    asia = [pcts[k] for k in ASIA_SYMS if k in pcts]
    if asia:
        avg = sum(asia) / len(asia)
        score += vote(avg, 1)
        max_score += 1
        parts.append(f"亞股 {avg:+.2f}%")

    if "^VIX" in pcts:
        score += vote(-pcts["^VIX"], 1)
        max_score += 1
        lv = levels.get("^VIX")
        parts.append(f"VIX {lv:.1f}" if lv else "VIX")
        if lv and lv >= VIX_PANIC:
            veto = "VIX 進入恐慌區"
        elif lv and lv >= VIX_WARN:
            score -= 1

    if max_score < 3:
        return "⚪", "資料不足，無法判斷", parts, score, max_score
    if veto or score <= AVOID_AT:
        return "🔴", "不適合進場（偏空）" + (f"：{veto}" if veto else ""), parts, score, max_score
    if score >= ENTER_AT:
        return "🟢", "適合進場（偏多）", parts, score, max_score
    return "🟡", "中性：輕倉或觀望", parts, score, max_score


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
    pcts, levels = {}, {}

    # 最前面：台指期夜盤
    total += 1
    try:
        curr, prev, d = get_tx_night()
        pcts["TX"] = (curr - prev) / prev * 100
        head = f"*🌙 台指期夜盤*　{d:%m/%d} 盤"
        blocks.append(head + "\n" + format_item("台指期近月", "TX", curr, prev, d, d.date()))
    except Exception as e:
        failed += 1
        blocks.append(f"*🌙 台指期夜盤*\n⚠️ 抓不到（{e}）")

    for title, items in GROUPS:
        rows, dates = [], []
        for label, sym in items.items():
            total += 1
            try:
                curr, prev, d = get_quote(sym)
                rows.append((label, sym, curr, prev, d))
                pcts[sym] = (curr - prev) / prev * 100
                levels[sym] = curr
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

    icon, text, parts, score, max_score = judge(pcts, levels)
    concl = f"🎯 *今日結論*　{icon} *{text}*\n　　" + "、".join(parts)
    score = max(-max_score, min(max_score, score))
    concl += f"\n　　綜合分數 {score:+d}／{max_score}"
    if now.weekday() >= 5:
        concl += "（週末資料，僅供參考）"

    report = (
        "\n".join(top) + f"\n{LINE}\n\n"
        + concl + f"\n\n{LINE}\n\n"
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
