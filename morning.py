"""morning.py — 晨間國際行情速報（台灣時間 08:30）
想增減項目，只改下面的 GROUPS（名稱請用 4 個中文字，表格才會對齊）。
顏色規則：🟢 = 利多、🔴 = 利空（一般上漲是利多；VIX 上漲是利空，已自動處理）。
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

from notify import send_telegram

# 分組顯示：(組名, {顯示名稱(4字): Yahoo 代號})
GROUPS = [
    ("🇺🇸 美股期貨", {"那斯達克": "NQ=F", "標普五百": "ES=F", "道瓊工業": "YM=F"}),
    ("🌏 亞洲股市", {"日經指數": "^N225", "韓國綜合": "^KS11"}),
    ("😨 恐慌指數", {"恐慌指數": "^VIX"}),
]

# 「上漲代表壞事」的項目：上漲顯示 🔴、下跌顯示 🟢
INVERSE_SYMBOLS = {"^VIX"}

# 台指期夜盤：期交所即時行情（MarketType=1 代表盤後／夜盤，TXF = 臺股期貨）
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
TX_NAME = "台指夜盤"

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
FW = "\u3000"  # 全形空白：用來把中文名稱補成一樣長，表格才會對齊


def pad(name, n=4):
    return name + FW * max(0, n - len(name))


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


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


def get_tx_night():
    """台指期近月夜盤（期交所即時行情）。回傳 (最新價, 參考價, 最後成交時間文字)。
    參考價 = 期交所給的前一日結算價，漲跌就是跟它比。"""
    r = requests.post(
        MIS_URL,
        json={"CID": "TXF", "SymbolType": "F", "MarketType": "1"},
        headers={"User-Agent": "Mozilla/5.0",
                 "Origin": "https://mis.taifex.com.tw",
                 "Referer": "https://mis.taifex.com.tw/futures/"},
        timeout=30,
    )
    r.raise_for_status()
    j = r.json()
    if str(j.get("RtCode")) != "0":
        raise ValueError(f"期交所回應異常：{j.get('RtMsg')}")
    quotes = (j.get("RtData") or {}).get("QuoteList") or []
    # 只要單一月份合約（例如 TXFK6-M），排除跨月價差
    cands = [q for q in quotes
             if re.fullmatch(r"TXF[A-L]\d-[FM]", str(q.get("SymbolID", ""))) and _num(q.get("CLastPrice"))]
    if not cands:
        raise ValueError("查無夜盤報價")
    q = max(cands, key=lambda x: _num(x.get("CTotalVolume")) or 0)  # 成交量最大 = 近月
    print("TX 夜盤原始資料：", {k: q.get(k) for k in
          ("SymbolID", "DispCName", "CLastPrice", "CRefPrice", "CDiff", "CDiffRate", "CDate", "CTime", "CTotalVolume")})

    last = _num(q.get("CLastPrice"))
    ref = _num(q.get("CRefPrice"))
    if not ref:
        diff = _num(q.get("CDiff"))
        if diff is None:
            raise ValueError("缺少參考價")
        ref = last - diff
    t = str(q.get("CTime", "")).strip()
    when = f"{t[:2]}:{t[2:4]}" if len(t) >= 4 and t[:4].isdigit() else ""
    return last, ref, when


def judge(pcts, levels):
    """把夜盤、美股期貨、亞股、VIX 綜合成一個結論。
    計分：夜盤 ±2、美股期貨平均 ±2、亞股平均 ±1、VIX 變動 ±1（VIX 反向），
    另外 VIX 偏高會扣分或直接否決。
    回傳 (圖示, 結論, 明細[(名稱, 數值, 分數)], 總分, 滿分, 否決原因)"""
    score = max_score = 0
    rows, veto = [], None

    def vote(p, w):
        return w if p >= FLAT else (-w if p <= -FLAT else 0)

    tx = pcts.get("TX")
    if tx is not None:
        v = vote(tx, 2)
        score += v
        max_score += 2
        rows.append((TX_NAME, f"{tx:+.2f}%", v))
        if tx <= TX_CRASH:
            veto = "夜盤跌幅過大"

    us = [pcts[k] for k in US_SYMS if k in pcts]
    if us:
        avg = sum(us) / len(us)
        v = vote(avg, 2)
        score += v
        max_score += 2
        rows.append(("美股期貨", f"{avg:+.2f}%", v))

    asia = [pcts[k] for k in ASIA_SYMS if k in pcts]
    if asia:
        avg = sum(asia) / len(asia)
        v = vote(avg, 1)
        score += v
        max_score += 1
        rows.append(("亞洲股市", f"{avg:+.2f}%", v))

    if "^VIX" in pcts:
        v = vote(-pcts["^VIX"], 1)
        lv = levels.get("^VIX")
        if lv and lv >= VIX_PANIC:
            veto = "VIX 進入恐慌區"
        elif lv and lv >= VIX_WARN:
            v -= 1
        score += v
        max_score += 1
        rows.append(("恐慌指數", f"{lv:.1f}" if lv else "-", v))

    if max_score < 3:
        return "⚪", "資料不足，無法判斷", rows, score, max_score, veto
    if veto or score <= AVOID_AT:
        return "🔴", "不適合進場（偏空）", rows, score, max_score, veto
    if score >= ENTER_AT:
        return "🟢", "適合進場（偏多）", rows, score, max_score, veto
    return "🟡", "中性：輕倉或觀望", rows, score, max_score, veto


def fmt_row(name, symbol, curr, prev, note=""):
    """一個項目 = 表格一行：色燈、名稱、漲跌幅、價格、漲跌點"""
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
        price, chg = f"{curr:,.0f}", f"{diff:+,.0f}"
    else:
        price, chg = f"{curr:,.2f}", f"{diff:+,.2f}"
    pct_txt = "0.00%" if flat else f"{pct:+.2f}%"
    if abs(pct) > 40:
        note += " ⚠️數值偏大"
    return f"{icon} {pad(name)} {pct_txt:>7} {price:>6} {chg:>6}{note}"


def code(lines):
    # 行尾補幾個「看不見的空格」(U+2800)，讓 Telegram 右上角的 </> 複製鈕不會蓋到最後一個字
    return "```\n" + "\n".join(ln + "\u2800" * 3 for ln in lines) + "\n```"



def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    today = now.date()
    blocks, total, failed = [], 0, 0
    pcts, levels = {}, {}

    # 最前面：台指期夜盤
    total += 1
    try:
        curr, prev, when = get_tx_night()
        pcts["TX"] = (curr - prev) / prev * 100
        head = "*🌙 台指期夜盤*" + (f"　最後成交 {when}" if when else "")
        blocks.append(head + "\n" + code([fmt_row(TX_NAME, "TX", curr, prev)]))
    except Exception as e:
        failed += 1
        print("TX 夜盤失敗：", repr(e))
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
                lines.append(f"⚠️ {pad(label)} 抓不到")
            else:
                note = f" ⚠️{d:%m/%d}" if d.date() != gdate else ""
                lines.append(fmt_row(label, sym, curr, prev, note))
        blocks.append(head + "\n" + code(lines))

    top = [
        "☀️ *台股晨間速報*",
        f"{now:%m/%d}（{WEEKDAY[now.weekday()]}）{now:%H:%M}",
    ]
    if now.weekday() >= 5:
        top.append("📌 週末｜以下為最後交易日資料，非即時行情")
    top.append("🟢 利多　🔴 利空")


    icon, text, drows, score, max_score, veto = judge(pcts, levels)
    tbl = [f"{pad(n)} {v:>7} {p:>+3d}" for n, v, p in drows]
    score = max(-max_score, min(max_score, score))
    tbl.append(f"{pad('綜合分數')} {'':>7} {score:+d}/{max_score}")
    concl = f"🎯 *今日結論*\n{icon} *{text}*\n" + code(tbl)
    if veto:
        concl += f"\n⚠️ 否決條件：{veto}"
    if now.weekday() >= 5:
        concl += "\n（週末資料，僅供參考）"

    report = (
        "\n".join(top) + f"\n{LINE}\n"
        + concl + f"\n{LINE}\n"
        + "\n".join(blocks)
        + f"\n{LINE}\n"
        "🟢 利多　🔴 利空\n"
        "⏰ 08:45 台指期開盤　09:00 現貨開盤"
    )
    send_telegram([report])
    if failed == total:
        raise SystemExit("全部項目都抓不到，請檢查網路或 Yahoo 是否限流")


if __name__ == "__main__":
    run()
