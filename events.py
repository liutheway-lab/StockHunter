"""events.py — 近 5 個交易日重大事件日曆（晨報與 App 共用）。全部自動，不用手動維護。

資料來源（每次執行時自己抓，抓不到就用備用）：
- BLS（非農、CPI、PPI、JOLTS）：bls.gov 官方行事曆（ICS）
- BEA（PCE、GDP）：apps.bea.gov 官方發布日期（JSON）
- Fed（FOMC 利率決議、會議紀要）：federalreserve.gov 的 FOMC 行事曆頁面；紀要＝決議後 21 天（Fed 慣例）
- 台股休市日：政府辦公日曆（ruyut/TaiwanCalendar，資料來自行政院人事行政總處）
- 美股休市／提早收盤：用規則計算（感恩節、聖誕節、耶穌受難日…），不需要網路
- 台灣央行理監事會：央行沒有公開資料，用規則推估（3、6、9、12 月第 3 個星期四），標示「暫定」
- ISM、台指期結算、月營收截止：用規則計算
所有時間都是「台灣時間」。每個來源抓不到時，會用下面的備用表；備用表涵蓋到 FALLBACK_UNTIL。
"""
import html as _html
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

WEEKDAY = "一二三四五六日"
MARK = {3: "‼️", 2: "❗️", 1: ""}      # 重要度 → 標記：3＝非常重要、2＝重要、1＝一般
TPE = ZoneInfo("Asia/Taipei")
NY = ZoneInfo("America/New_York")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

STATUS = {"BLS": None, "BEA": None, "FOMC": None, "台股休市日": None}      # True＝抓到官方資料、False＝抓不到改用備用


# ───────────────────────── 備用表（官方資料抓不到時才用）─────────────────────────
FALLBACK_UNTIL = date(2026, 12, 31)            # 備用表涵蓋到這天；官方資料抓不到而且超過這天，才會提醒「事件可能不完整」

# FOMC 決議日（美東；取自 federalreserve.gov）
FOMC_FALLBACK = [
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17", "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09", "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]
BLS_FALLBACK = [
    ("2026-11-06", "21:30", "非農就業（10月）", 3),
    ("2026-11-10", "21:30", "CPI（10月）", 3),
    ("2026-11-13", "21:30", "PPI（10月）", 2),
    ("2026-12-01", "23:00", "JOLTS（10月）", 1),
    ("2026-12-04", "21:30", "非農就業（11月）", 3),
    ("2026-12-10", "21:30", "CPI（11月）", 3),
    ("2026-12-15", "21:30", "PPI（11月）", 2),
]
BEA_FALLBACK = [                                # 取自 BEA 官方 JSON（台灣時間）
    ("2026-11-25", "21:30", "PCE", 2), ("2026-11-25", "21:30", "GDP", 2),
    ("2026-12-23", "21:30", "PCE", 2), ("2026-12-23", "21:30", "GDP", 2),
]
TW_FALLBACK = {"2026-12-25": "行憲紀念日", "2027-01-01": "開國紀念日"}      # 台股休市的平日（政府辦公日曆抓不到時用）


# ───────────────────────── 抓網頁 ─────────────────────────
_CACHE = {}


def _get(url, timeout=10, ttl=6 * 3600):
    """抓網頁文字；同一次執行內 ttl 秒不重抓。失敗回傳 None（失敗也記住 10 分鐘，避免每次都等逾時）"""
    now = time.time()
    hit = _CACHE.get(url)
    if hit and now - hit[0] < (ttl if hit[1] is not None else 600):
        return hit[1]
    try:
        import requests
        r = requests.get(url, headers=UA, timeout=timeout)
        r.raise_for_status()
        txt = r.text
    except Exception as e:
        print(f"抓取失敗 {url}：{repr(e)[:100]}")
        txt = None
    _CACHE[url] = (now, txt)
    return txt


def today_tw():
    return datetime.now(TPE).date()


def _d(s):
    return date.fromisoformat(s)


def _et_to_tw(d, hm=(14, 0)):
    """美東某日某時 → 台灣（日期, 時間）。自動處理夏令／冬令時間"""
    t = datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=NY).astimezone(TPE)
    return t.date(), f"{t:%H:%M}"


# ───────────────────────── 美股休市（規則計算）─────────────────────────
def _nth_weekday(y, m, wd, n):
    d = date(y, m, 1)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y, m, wd):
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    while d.weekday() != wd:
        d -= timedelta(days=1)
    return d


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mo, da = divmod(h + l - 7 * m + 114, 31)
    return date(y, mo, da + 1)


def _observed(d, new_year=False):
    """假日遇到週末的補假：週六提前到週五、週日順延到週一（元旦遇週六不補假）"""
    if d.weekday() == 5:
        return None if new_year else d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=None)
def nyse_closed(y):
    """美股（NYSE）當年的休市日 {日期: 中文名稱}"""
    raw = [
        (_observed(date(y, 1, 1), True), "元旦"),
        (_nth_weekday(y, 1, 0, 3), "馬丁路德·金恩日"),
        (_nth_weekday(y, 2, 0, 3), "總統日"),
        (_easter(y) - timedelta(days=2), "耶穌受難日"),
        (_last_weekday(y, 5, 0), "陣亡將士紀念日"),
        (_observed(date(y, 6, 19)), "六月節"),
        (_observed(date(y, 7, 4)), "獨立紀念日"),
        (_nth_weekday(y, 9, 0, 1), "勞動節"),
        (_nth_weekday(y, 11, 3, 4), "感恩節"),
        (_observed(date(y, 12, 25)), "聖誕節"),
    ]
    return {d: n for d, n in raw if d}


@lru_cache(maxsize=None)
def nyse_early(y):
    """美股提早收盤日（感恩節隔天；平安夜遇平日）"""
    out = {_nth_weekday(y, 11, 3, 4) + timedelta(days=1)}
    eve = date(y, 12, 24)
    if eve.weekday() < 5 and eve not in nyse_closed(y):
        out.add(eve)
    return out


def _is_us_business(d):
    return d.weekday() < 5 and d not in nyse_closed(d.year)


def _nth_business(year, month, n):
    """美國每月第 n 個營業日"""
    d, k = date(year, month, 1), 0
    while True:
        if _is_us_business(d):
            k += 1
            if k == n:
                return d
        d += timedelta(days=1)


# ───────────────────────── 台股休市（政府辦公日曆）─────────────────────────
_TW_MAP = {}


def _tw_year(y):
    """政府辦公日曆：當年的平日休假 {ISO日期: 名稱}；抓不到回傳 None"""
    for url in (f"https://cdn.jsdelivr.net/gh/ruyut/TaiwanCalendar/data/{y}.json",
                f"https://raw.githubusercontent.com/ruyut/TaiwanCalendar/master/data/{y}.json"):
        txt = _get(url)
        if not txt:
            continue
        try:
            data = json.loads(txt)
            recs = data if isinstance(data, list) else [h for m in data.get("months", []) for h in m.get("holidays", [])]
            out = {}
            for r in recs:
                ds = str(r.get("date", "")).replace("-", "")
                if r.get("isHoliday") and len(ds) == 8:
                    d = date(int(ds[:4]), int(ds[4:6]), int(ds[6:8]))
                    if d.weekday() < 5:                                  # 週末本來就不開市
                        out[d.isoformat()] = r.get("description") or r.get("name") or "休市"
            if len(out) >= 5:                                           # 太少 → 格式不對，當作抓不到
                return out
        except Exception as e:
            print("台灣假日資料解析失敗：", repr(e)[:100])
    return None


def tw_holidays(y):
    """{ISO日期: 名稱}：台股休市的平日。抓得到用官方，抓不到用備用表"""
    hit = _TW_MAP.get(y)
    if hit and time.time() - hit[0] < 6 * 3600:
        return hit[1]
    got = _tw_year(y)
    if y == today_tw().year:
        STATUS["台股休市日"] = got is not None
    val = got if got is not None else {k: v for k, v in TW_FALLBACK.items() if k.startswith(str(y))}
    _TW_MAP[y] = (time.time(), val)
    return val


def _is_tw_trading(d):
    return d.weekday() < 5 and d.isoformat() not in tw_holidays(d.year)


# ───────────────────────── FOMC（Fed 官方行事曆頁面）─────────────────────────
FED_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
_MRX = "|".join(_MONTHS)


def _parse_fomc(raw):
    """從 Fed 行事曆頁面讀出決議日（會議第二天）。讀不出來或看起來不對回傳 None"""
    text = _html.unescape(re.sub(r"<[^>]+>", "\n", raw))
    parts = re.split(r"(\d{4})\s+FOMC\s+Meetings", text)
    out = []
    for i in range(1, len(parts), 2):
        year, body = int(parts[i]), parts[i + 1]
        days = set()
        for mo, d1, d2 in re.findall(rf"({_MRX})\s*(\d{{1,2}})(?!\d)(?:\s*[-–]\s*(\d{{1,2}})(?!\d))?\*?(?!\s*,\s*\d{{4}})", body):
            try:
                d = date(year, _MONTHS.index(mo) + 1, int(d2 or d1))
            except ValueError:
                continue
            if d.weekday() in (1, 2):                                   # 決議日都在星期二或三
                days.add(d)
        if len(days) >= 6:                                              # 一年至少 6 場（通常 8 場），太少就不採用
            out += sorted(days)
    return [d.isoformat() for d in out] or None


def fomc_decisions():
    txt = _get(FED_URL, ttl=12 * 3600)
    got = _parse_fomc(txt) if txt else None
    STATUS["FOMC"] = got is not None
    return got or FOMC_FALLBACK


def fomc_events():
    """FOMC 利率決議（美東 14:00）與會議紀要（決議後 21 天，同一時間）"""
    out = []
    for s in fomc_decisions():
        d = _d(s)
        td, tt = _et_to_tw(d)
        out.append((td, tt, "FOMC 利率決議＋記者會", 3))
        md, mt = _et_to_tw(d + timedelta(days=21))
        out.append((md, mt, "FOMC 會議紀要", 2))
    return out


# ───────────────────────── BLS 官方行事曆（ICS）─────────────────────────
BLS_ICS = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_NAMES = (                                  # (ICS 裡的關鍵字, 顯示名稱, 重要度)
    ("Employment Situation", "非農就業", 3),
    ("Consumer Price Index", "CPI", 3),
    ("Producer Price Index", "PPI", 2),
    ("Job Openings", "JOLTS", 1),
)


def _parse_ics(text):
    """極簡 ICS 解析：回傳每個 VEVENT 的 {屬性名: (完整鍵含參數, 值)}"""
    lines = []
    for raw in text.replace("\r", "").split("\n"):
        if raw[:1] in (" ", "\t") and lines:              # 折行接續
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    out, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT":
            cur = {}
        elif ln == "END:VEVENT":
            if cur is not None:
                out.append(cur)
            cur = None
        elif cur is not None and ":" in ln:
            key, val = ln.split(":", 1)
            cur[key.split(";")[0].upper()] = (key, val)
    return out


def _ics_start(key, val):
    """DTSTART → 台灣（日期, 時間）；全天事件時間回傳 ""；看不懂回傳 None"""
    try:
        if len(val) == 8:                                       # 只有日期
            return _d(f"{val[:4]}-{val[4:6]}-{val[6:8]}"), ""
        y, mo, da, hh, mi = int(val[:4]), int(val[4:6]), int(val[6:8]), int(val[9:11]), int(val[11:13])
        if val.endswith("Z"):
            t = datetime(y, mo, da, hh, mi, tzinfo=timezone.utc)
        elif "TZID=" in key:
            t = datetime(y, mo, da, hh, mi, tzinfo=ZoneInfo(key.split("TZID=")[1]))
        else:
            t = datetime(y, mo, da, hh, mi, tzinfo=TPE)
        t = t.astimezone(TPE)
        return t.date(), f"{t:%H:%M}"
    except Exception:
        return None


def bls_events():
    """BLS 官方行事曆轉成 (日期, 時間, 名稱, 重要度)；抓不到回傳 None（改用備用表）"""
    txt = _get(BLS_ICS)
    items = []
    try:
        for ev in _parse_ics(txt or ""):
            s = ev.get("SUMMARY", ("", ""))[1].replace(" ", "").lower()      # 忽略空格（折行時空格可能被吃掉）
            hit = [(lab, imp) for kw, lab, imp in BLS_NAMES if kw.replace(" ", "").lower() in s]
            st = _ics_start(*ev["DTSTART"]) if "DTSTART" in ev else None
            if hit and st:
                items.append((st[0], st[1], hit[0][0], hit[0][1]))
    except Exception as e:
        print("BLS 行事曆解析失敗：", repr(e)[:100])
    STATUS["BLS"] = bool(items)
    return items or None


# ───────────────────────── BEA 官方發布日期（JSON）─────────────────────────
BEA_JSON = "https://apps.bea.gov/API/signup/release_dates.json"
BEA_NAMES = (("Personal Income and Outlays", "PCE", 2), ("Gross Domestic Product", "GDP", 2))


def bea_events():
    """BEA 官方發布日期轉成 (日期, 時間, 名稱, 重要度)；抓不到回傳 None（改用備用表）"""
    txt = _get(BEA_JSON)
    items = []
    try:
        data = json.loads(txt) if txt else {}
        for key, label, imp in BEA_NAMES:
            for ts in (data.get(key) or {}).get("release_dates", []):
                t = datetime.fromisoformat(ts)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                t = t.astimezone(TPE)
                items.append((t.date(), f"{t:%H:%M}", label, imp))
    except Exception as e:
        print("BEA 發布日期解析失敗：", repr(e)[:100])
    STATUS["BEA"] = bool(items)
    return items or None


# ───────────────────────── 規則計算的事件 ─────────────────────────
def _rule_events(start, end):
    """ISM（美國營業日第 1／第 3 天 10:00 美東＝台灣 23:00）、台指期結算、月營收截止日"""
    out = []
    y, m = start.year, start.month
    while date(y, m, 1) <= end:
        out.append((_nth_business(y, m, 1), "23:00", "ISM 製造業", 2))
        out.append((_nth_business(y, m, 3), "23:00", "ISM 非製造業", 2))
        out.append((_nth_weekday(y, m, 2, 3), "13:30", "台指期結算日", 2))      # 每月第 3 個星期三
        due = date(y, m, 10)
        while not _is_tw_trading(due):                                         # 10 日遇假日，往前找最近的交易日
            due -= timedelta(days=1)
        out.append((due, "", "台股月營收截止", 2))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def cbc_events(start, end):
    """台灣央行理監事會：央行沒有公開資料，用規則推估（3、6、9、12 月第 3 個星期四），標示「暫定」"""
    return [(_nth_weekday(y, m, 3, 3), "", "台灣央行理監事會（暫定）", 3)
            for y in range(start.year, end.year + 1) for m in (3, 6, 9, 12)]


def closure_events(start, end):
    """台股、美股休市與提早收盤"""
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            tw = tw_holidays(d.year).get(d.isoformat())
            if tw:
                out.append((d, "", f"台股休市（{tw}）", 1))
            us = nyse_closed(d.year).get(d)
            if us:
                out.append((d, "", f"美股休市（{us}）", 1))
            elif d in nyse_early(d.year):
                out.append((d, "", "美股提早收盤", 1))
        d += timedelta(days=1)
    return out


# ───────────────────────── 組合 ─────────────────────────
def all_events(start, end):
    """所有來源的事件（還沒篩選日期）"""
    bea, bls = bea_events(), bls_events()
    items = bea if bea is not None else [(_d(a), b, c, e) for a, b, c, e in BEA_FALLBACK]
    items += bls if bls is not None else [(_d(a), b, c, e) for a, b, c, e in BLS_FALLBACK]
    items += fomc_events()
    items += cbc_events(start, end)
    items += closure_events(start, end)
    items += _rule_events(date(start.year, start.month, 1), end)
    return items


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
    seen, out = set(), []
    for it in all_events(start, end):
        key = (it[0], it[2])
        if start <= it[0] <= end and key not in seen:
            seen.add(key)
            out.append(it)
    return sorted(out, key=lambda x: (x[0], x[1] or "99:99"))


def source_status():
    """各來源有沒有抓到官方資料（先呼叫一次讓狀態更新）"""
    bls_events(), bea_events(), fomc_decisions(), tw_holidays(today_tw().year)
    return "、".join(f"{k}{'✅官方' if v else '⚠️備用'}" for k, v in STATUS.items())


def stale_note(today=None, n=5):
    """事件資料可能不完整時的提醒（沒問題回傳空字串）。要在 event_lines 之後呼叫，來源狀態才是最新的"""
    _, end = trading_window(today or today_tw(), n)
    failed = [k for k, v in STATUS.items() if v is False]
    if failed and end > FALLBACK_UNTIL:
        return f"{'、'.join(failed)} 官方資料抓不到，事件可能不完整"
    return ""


def fmt_item(it):
    """一件事一行：11/06(五) 21:30 ‼️非農就業（10月）"""
    d, t, name, imp = it
    mk = MARK.get(imp, "")
    return f"{d:%m/%d}({WEEKDAY[d.weekday()]}) {t or '全天'} {mk + ' ' if mk else ''}{name}"


def event_lines(today=None, n=5):
    """晨報與 App 共用：每件事一行；沒有事件回傳一行說明"""
    items = upcoming(today or today_tw(), n)
    return [fmt_item(it) for it in items] or ["沒有列入的重大事件"]


def event_rows(today=None, n=5):
    """App 用的結構化資料：日期只在同一天的第一列顯示，其餘列留空（排版比較乾淨）"""
    items = upcoming(today or today_tw(), n)
    rows, last = [], None
    for d, t, name, imp in items:
        day = f"{d:%m/%d}（{WEEKDAY[d.weekday()]}）"
        rows.append({"date": day if d != last else "", "time": t or "全天",
                     "mark": MARK.get(imp, ""), "name": name})
        last = d
    return rows


def telegram_block(today=None, n=5):
    """晨報用的文字（Telegram Markdown）"""
    lines = event_lines(today, n)
    note = stale_note(today or today_tw(), n)
    return f"*🔔 未來 {n} 個交易日重大事件*\n" + "\n".join(lines) + ("\n⚠️ " + note if note else "")


if __name__ == "__main__":
    print(telegram_block())
    print("資料來源：", source_status())
