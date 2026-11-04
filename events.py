"""events.py — 近 5 個交易日重大事件日曆（晨報與 App 共用）

資料來源：美國勞工統計局（BLS）、聯準會（Fed）、BEA、中央銀行的官方公布行程。
所有時間都是「台灣時間」。美國 11/01 已經回到冬令時間，美東 8:30 ＝ 台灣 21:30、美東 14:00 ＝ 台灣隔天 03:00。

【怎麼增加或修改事件】只改下面的 STATIC（一行一個事件）：
    ("日期", "台灣時間", "名稱", 重要度)
    重要度：3＝非常重要（標 ‼️）、2＝重要（標 ❗️）、1＝一般（不標）
    時間不確定就寫 ""。
改完把 COVERED_UNTIL 改成「事件資料涵蓋到哪一天」，超過那天 App 與晨報會提醒你更新。
"""
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAY = "一二三四五六日"
MARK = {3: "‼️", 2: "❗️", 1: ""}      # 重要度 → 標記
COVERED_UNTIL = date(2026, 12, 31)          # 事件資料只涵蓋到這天；之後要補 2027 年的行程

# 台股休市的平日（用來算「5 個交易日」）。只列已確認的；之後要補（例如 2027 春節）
TW_HOLIDAYS = {"2026-12-25", "2027-01-01"}
# 美股休市（算 ISM 公布日用）
US_HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01"}

STATIC = [
    # ── 美國（BLS／Fed／BEA 官方行程）──
    ("2026-11-06", "21:30", "非農就業（10月）", 3),
    ("2026-11-10", "21:30", "CPI（10月）", 3),
    ("2026-11-13", "21:30", "PPI（10月）", 2),
    ("2026-11-19", "03:00", "FOMC 會議紀要（10月）", 2),
    ("2026-11-25", "21:30", "PCE（10月）", 2),
    ("2026-12-01", "23:00", "JOLTS（10月）", 1),
    ("2026-12-04", "21:30", "非農就業（11月）", 3),
    ("2026-12-10", "03:00", "FOMC 利率決議＋記者會", 3),
    ("2026-12-10", "21:30", "CPI（11月）", 3),
    ("2026-12-15", "21:30", "PPI（11月）", 2),
    ("2026-12-23", "21:30", "PCE（11月）", 2),
    ("2026-12-31", "03:00", "FOMC 會議紀要（12月）", 2),
    # ── 台灣 ──
    ("2026-12-17", "", "台灣央行理監事會（暫定）", 3),
    # ── 休市 ──
    ("2026-11-26", "", "美股休市（感恩節）", 1),
    ("2026-11-27", "", "美股提早收盤", 1),
    ("2026-12-25", "", "美股休市（聖誕）", 1),
    ("2026-12-25", "", "台股休市（行憲紀念）", 1),
]


def today_tw():
    return datetime.now(ZoneInfo("Asia/Taipei")).date()


def _d(s):
    return date.fromisoformat(s)


def _is_tw_trading(d):
    return d.weekday() < 5 and d.isoformat() not in TW_HOLIDAYS


def _is_us_business(d):
    return d.weekday() < 5 and d.isoformat() not in US_HOLIDAYS


def _nth_business(year, month, n):
    """美國每月第 n 個營業日"""
    d, k = date(year, month, 1), 0
    while True:
        if _is_us_business(d):
            k += 1
            if k == n:
                return d
        d += timedelta(days=1)


def _rule_events(start, end):
    """由規則算出來的固定事件：ISM（美國營業日第 1／第 3 天 10:00 美東＝台灣 23:00）、台指期結算、月營收截止日"""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        out.append((_nth_business(y, m, 1), "23:00", "ISM 製造業", 2))
        out.append((_nth_business(y, m, 3), "23:00", "ISM 非製造業", 2))
        wed = [d for d in (date(y, m, k) for k in range(1, 22)) if d.weekday() == 2]
        out.append((wed[2], "13:30", "台指期結算日", 2))              # 每月第 3 個星期三
        due = date(y, m, 10)
        while not _is_tw_trading(due):                                         # 10 日遇假日，往前找最近的交易日
            due -= timedelta(days=1)
        out.append((due, "", "台股月營收截止", 2))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def trading_window(today=None, n=5):
    """從今天起算 n 個交易日，回傳 (起日, 迄日)。今天不是交易日（週末）就從下一個交易日數起"""
    today = today or today_tw()
    d, k = today, 0
    while True:
        if _is_tw_trading(d):
            k += 1
            if k == n:
                return today, d
        d += timedelta(days=1)


def upcoming(today=None, n=5):
    """回傳 [(日期, 時間, 名稱, 重要度)]，從今天到第 n 個交易日，依日期、時間排序"""
    today = today or today_tw()
    start, end = trading_window(today, n)
    items = [(_d(a), b, c, e) for a, b, c, e in STATIC]
    items += _rule_events(date(start.year, start.month, 1), min(end, COVERED_UNTIL))
    seen, out = set(), []
    for it in items:
        key = (it[0], it[2])
        if start <= it[0] <= end and key not in seen:
            seen.add(key)
            out.append(it)
    return sorted(out, key=lambda x: (x[0], x[1] or "99:99"))


def stale_note(today=None, n=5):
    """事件資料不夠新時的提醒（沒問題回傳空字串）"""
    _, end = trading_window(today or today_tw(), n)
    return f"事件資料只涵蓋到 {COVERED_UNTIL:%m/%d}，請更新 events.py" if end > COVERED_UNTIL else ""


def fmt_item(it):
    """一件事一行：11/06(五) 21:30 ‼️非農就業（10月）"""
    d, t, name, imp = it
    mk = MARK.get(imp, "")
    return f"{d:%m/%d}({WEEKDAY[d.weekday()]}) {t or '全天'} {mk + ' ' if mk else ''}{name}"


def event_lines(today=None, n=5):
    """晨報與 App 共用：每件事一行；沒有事件回傳一行說明"""
    items = upcoming(today or today_tw(), n)
    return [fmt_item(it) for it in items] or ["沒有列入的重大事件"]


def telegram_block(today=None, n=5):
    """晨報用的文字（Telegram Markdown）"""
    note = stale_note(today or today_tw(), n)
    return f"*📅 未來 {n} 個交易日重大事件*\n" + "\n".join(event_lines(today, n)) + ("\n⚠️ " + note if note else "")


if __name__ == "__main__":
    print(telegram_block())
