"""morning.py — 晨間國際行情速報（台灣時間 08:30）
想增減項目，只改下面的 GROUPS（名稱請用 4 個中文字，表格才會對齊）。
漲跌符號：▲ 上漲、▼ 下跌、─ 持平（只看價格方向，不用紅綠燈）。
"""
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

from notify import send_telegram

# 分組顯示：(組名, {顯示名稱(4字): Yahoo 代號})
GROUPS = [
    ("🇺🇸 美股期貨", {"那斯達克": "NQ=F", "標普五百": "ES=F", "道瓊工業": "YM=F"}),
    ("🌏 亞洲股市", {"日經指數": "^N225", "韓國綜合": "^KS11"}),   # 日經改用指數：日本休市日（例如 11/03 文化之日）會自動比上一個交易日
    ("😨 恐慌指數", {"恐慌指數": "^VIX"}),
]

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
FW = "　"  # 全形空白：用來把中文名稱補成一樣長，表格才會對齊


def pad(name, n=4):
    return name + FW * max(0, n - len(name))


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


# 期貨的「前一盤收盤」要抓幾點的價格：{代號: (時區, 時, 分)}。要跟券商看盤軟體的「漲跌」基準一致。
# 美股期貨：用美東 16:00（美股現貨收盤、結算價的時間）。
# 日經不用期貨：CME 的日經期貨在日本休市日（例如 11/03 文化之日）仍有零星成交，拿來當前收基準會讓漲跌對不起來。
FUT_BASE = {
    "NQ=F": ("America/New_York", 16, 0),
    "ES=F": ("America/New_York", 16, 0),
    "YM=F": ("America/New_York", 16, 0),
}


def get_quote_fut(symbol):
    """期貨：最新價 vs 前一盤收盤。用 5 分 K：最新一筆 = 最新價；
    最近一次「基準時間」（見 FUT_BASE）之前的最後一筆 = 前一盤收盤。回傳 (最新價, 前一盤收盤, 前一盤日期)"""
    tzname, hh, mm = FUT_BASE.get(symbol, ("America/New_York", 16, 0))
    tz = ZoneInfo(tzname)
    h = yf.Ticker(symbol).history(period="5d", interval="5m").dropna(subset=["Close"])
    if len(h) < 20:
        raise ValueError("5 分 K 資料不足")
    idx = h.index.tz_convert(tz) if h.index.tz is not None else h.index.tz_localize("UTC").tz_convert(tz)
    last_t = idx[-1]
    day = last_t.date()                                 # 用「牆上日期」算基準時間，換日光節約時間那週才不會差一小時
    if (last_t.hour, last_t.minute) < (hh, mm):
        day -= timedelta(days=1)
    while day.weekday() >= 5:                           # 週六、週日沒有交易盤，往前找到週五
        day -= timedelta(days=1)
    cut = pd.Timestamp(datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz))
    before = [k for k in range(len(idx)) if idx[k] < cut]
    if not before:
        raise ValueError("找不到前一盤收盤")
    k = before[-1]
    if (cut - idx[k]) > timedelta(hours=3):             # 基準時間前最後一筆離太遠 = 資料有缺，不可信
        raise ValueError("前一盤收盤資料有缺")
    curr, prev = float(h["Close"].iloc[-1]), float(h["Close"].iloc[k])
    print(symbol, "5分K：最新", idx[-1].strftime("%m/%d %H:%M"), round(curr, 2),
          "｜前一盤收盤", idx[k].strftime("%m/%d %H:%M"), round(prev, 2))
    if prev <= 0:
        raise ValueError("前收異常")
    return curr, prev, idx[k]


# ── 亞洲現貨指數（日經、韓國綜合）：Yahoo 的日線偶爾會漏掉某一天（例如 11/03），
#    直接拿「日線最後兩筆」會把更早的收盤當成前收，漲跌就整個錯掉。
#    做法：日線 + 5 分 K 一起看，每天取最後一筆當收盤；再檢查「前一個交易日」有沒有在資料裡。
IDX_TZ = {"^N225": "Asia/Tokyo", "^KS11": "Asia/Seoul"}
IDX_HOLIDAYS = {          # 該市場休市的平日（只要寫會影響「缺資料」判斷的日子；寫漏了頂多多一個警示）
    "^N225": {"2026-11-03", "2026-11-23", "2026-12-31", "2027-01-01", "2027-01-04", "2027-01-11", "2027-02-11", "2027-02-23"},
    "^KS11": {"2026-12-25", "2026-12-31", "2027-01-01"},
}
SUSPECT = {}              # {代號: 說明}：前收可能缺資料，不列入分數
NAVER_URL = "https://m.stock.naver.com/api/index/KOSPI/basic"      # 韓國 Naver 證券：KOSPI 現價與前日比


def _naver_kospi():
    """備援：Naver 的 KOSPI 即時報價（現價、前日比一次給）。回傳 (現價, 前收, 時間)；數字對不上就丟錯誤"""
    r = requests.get(NAVER_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    curr, diff, ratio = _num(j.get("closePrice")), _num(j.get("compareToPreviousClosePrice")), _num(j.get("fluctuationsRatio"))
    if curr is None or diff is None:
        raise ValueError("Naver 欄位缺少")
    cd = str((j.get("compareToPreviousPrice") or {}).get("code", ""))
    if cd in ("1", "2"):
        down = False
    elif cd in ("4", "5"):
        down = True
    else:
        down = bool(ratio is not None and ratio < 0) or diff < 0
    d = -abs(diff) if down else abs(diff)
    prev = curr - d
    if prev <= 0 or (ratio is not None and abs(abs(d) / prev * 100 - abs(ratio)) > 0.1):
        raise ValueError("Naver 數字對不上")
    t = j.get("localTradedAt")
    ts = pd.Timestamp(t) if t else pd.Timestamp.now(tz="Asia/Seoul")
    when = ts.tz_localize(None) if ts.tzinfo is None else ts.tz_convert("Asia/Seoul").tz_localize(None)
    print("Naver KOSPI：", curr, "前收", round(prev, 2), "時間", when)
    return curr, prev, when


def get_quote_index(symbol):
    """日經／韓國綜合：回傳 (最新價, 前收, 最新資料日期)。"""
    tz = IDX_TZ[symbol]
    d = yf.Ticker(symbol).history(period="10d", interval="1d").dropna(subset=["Close"])
    closes = {t.date(): float(c) for t, c in zip(d.index, d["Close"])}           # 日線的日期已經是當地日期
    fives = {}
    try:
        m = yf.Ticker(symbol).history(period="5d", interval="5m").dropna(subset=["Close"])
        if len(m):
            mi = m.index.tz_convert(tz) if m.index.tz is not None else m.index.tz_localize("UTC").tz_convert(tz)
            for t, c in zip(mi, m["Close"]):                                   # 時間由早到晚，同一天最後一筆就是當天收盤（盤中＝最新價）
                fives[t.date()] = float(c)
    except Exception as e:
        print(symbol, "5 分 K 抓不到：", repr(e))
    print(symbol, "日線：", [(f"{k:%m/%d}", round(v, 2)) for k, v in list(closes.items())[-4:]],
          "｜5分K每天最後一筆：", [(f"{k:%m/%d}", round(v, 2)) for k, v in list(fives.items())[-4:]])
    if not closes and not fives:
        raise ValueError("沒有資料")
    dlast, mlast = max(closes) if closes else None, max(fives) if fives else None
    if mlast is not None and (dlast is None or mlast > dlast):
        cur_date, curr = mlast, fives[mlast]
    else:
        cur_date, curr = dlast, closes[dlast]
    merged = dict(fives)
    merged.update(closes)                                                      # 同一天日線優先；日線漏掉的那天用 5 分 K 補
    olds = [k for k in merged if k < cur_date]
    if not olds:
        raise ValueError("找不到前一個交易日")
    prev_date = max(olds)
    prev = merged[prev_date]
    exp = cur_date - timedelta(days=1)                                         # 理論上的前一個交易日：往前跳過週末與休市日
    while exp.weekday() >= 5 or f"{exp:%Y-%m-%d}" in IDX_HOLIDAYS.get(symbol, ()):
        exp -= timedelta(days=1)
    SUSPECT.pop(symbol, None)
    if prev_date < exp:
        SUSPECT[symbol] = f"缺 {exp:%m/%d} 收盤"
        print(symbol, "⚠️ 前一個交易日", exp, "不在資料裡，最近一筆是", prev_date)
        if symbol == "^KS11":
            try:
                c2, p2, w2 = _naver_kospi()
                SUSPECT.pop(symbol, None)
                return c2, p2, pd.Timestamp(w2)
            except Exception as e:
                print("Naver 備援失敗：", repr(e))
    print(symbol, f"→ 最新 {cur_date:%m/%d} {curr:,.2f}｜前收 {prev_date:%m/%d} {prev:,.2f}")
    return curr, prev, pd.Timestamp(cur_date)


def get_quote(symbol):
    """回傳 (最新價, 前收, 最新資料日期)。
    期貨用 get_quote_fut（對前一盤收盤，基準時間見 FUT_BASE）；失敗或其他商品，用日線最後兩筆（點數與百分比必定一致）。"""
    if symbol in IDX_TZ:
        try:
            return get_quote_index(symbol)
        except Exception as e:
            print(f"{symbol} 專用抓法失敗，改用日線：", repr(e))
    if symbol.endswith("=F"):
        try:
            return get_quote_fut(symbol)
        except Exception as e:
            print(f"{symbol} 5 分 K 方式失敗，改用日線：", repr(e))
    hist = yf.Ticker(symbol).history(period="10d", interval="1d")
    hist = hist.dropna(subset=["Close"])
    if len(hist) < 2:
        raise ValueError("資料不足")
    print(symbol, "最近日線：", [(f"{i:%m/%d}", round(float(c), 2)) for i, c in zip(hist.index[-3:], hist["Close"].iloc[-3:])])
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
    計分：夜盤 ±1、美股期貨平均 ±2、亞股平均 ±1、VIX 變動 ±1（VIX 反向），
    另外 VIX 偏高會扣分或直接否決。
    回傳 (圖示, 結論, 明細[(名稱, 數值, 分數)], 總分, 滿分, 否決原因)"""
    score = max_score = 0
    rows, veto = [], None

    def vote(p, w):
        return w if p >= FLAT else (-w if p <= -FLAT else 0)

    tx = pcts.get("TX")
    if tx is not None:
        v = vote(tx, 1)
        score += v
        max_score += 1
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
    """一個項目 = 表格一行：箭頭、名稱、漲跌幅、價格、漲跌點"""
    diff = curr - prev
    pct = diff / prev * 100
    flat = round(diff, 4) == 0
    icon = "─" if flat else "▲" if diff > 0 else "▼"

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
    # GitHub 的排程有時會延遲好幾個小時才跑：過了 10:30 才由「排程」叫起來的，早報已經過期，不要再送
    # （手動執行不受影響；備援補跑是在 09:30 前，也不受影響）
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule" and (now.hour, now.minute) >= (10, 30):
        print(f"排程延遲到 {now:%H:%M}，早報已過期，這次不送")
        return
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
        notes = []
        if gdate:                                       # 有項目停在比較早的日期：現貨指數當作休市，期貨當作沒有新資料
            for label, sym, curr, prev, d in rows:
                lag = timedelta(days=1 if sym.endswith("=F") else 0)      # 期貨日線的日期是交易所（美國）時間，差一天算正常
                if curr is None or d.date() >= gdate - lag:
                    continue
                pcts.pop(sym, None)                     # 舊資料不拿來算今天的結論
                why = "無新資料" if sym.endswith("=F") else "休市"
                notes.append(f"※ {label}{why}（停在 {d:%m/%d}）")
        for label, sym, curr, prev, d in rows:
            if sym in SUSPECT and curr is not None:
                pcts.pop(sym, None)                     # 前收可能缺資料，數字不可信，不拿來算今天的結論
                notes.append(f"※ {label}前收資料{SUSPECT[sym]}，不列入分數")
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
                lines.append(fmt_row(label, sym, curr, prev, " ⚠️" if sym in SUSPECT else ""))
        blocks.append(head + "\n" + code(lines) + ("\n" + "\n".join(notes) if notes else ""))

    top = [
        "☀️ *台股晨間速報*",
        f"{now:%m/%d}（{WEEKDAY[now.weekday()]}）{now:%H:%M}",
    ]
    if now.weekday() >= 5:
        top.append("📌 週末｜以下為最後交易日資料\n　　非即時行情")

    icon, text, drows, score, max_score, veto = judge(pcts, levels)
    tbl = [f"{pad(n)} {p:+d}" for n, v, p in drows]                 # 只寫分數；漲跌幅、VIX 數字下面各區塊都有
    score = max(-max_score, min(max_score, score))
    tbl.append(f"{pad('綜合分數')} {score:+d}/{max_score}")
    concl = f"🎯 *今日結論*\n{icon} *{text}*\n" + code(tbl)
    if veto:
        concl += f"\n⚠️ 否決條件：{veto}"
    if now.weekday() >= 5:
        concl += "\n（週末資料，僅供參考）"

    try:                                                # 近 5 個交易日重大事件（events.py）；失敗不影響早報
        import events
        cal = events.telegram_block(today)
    except Exception as e:
        print("事件日曆失敗：", repr(e))
        cal = ""

    report = (
        "\n".join(top) + f"\n{LINE}\n"
        + concl + f"\n{LINE}\n"
        + "\n".join(blocks)
        + (f"\n{LINE}\n" + cal if cal else "")
        + f"\n{LINE}\n"
        "⏰ 08:45 台指期開盤　09:00 現貨開盤"
    )
    send_telegram([report])
    if failed == total:
        raise SystemExit("全部項目都抓不到，請檢查網路或 Yahoo 是否限流")


if __name__ == "__main__":
    run()
