"""futures_sweep.py — 台指期「流動性掃蕩」提醒（只私訊管理者，不發群組，不下單）

什麼是掃蕩：價格刺破一個關鍵高（低）點，把停損單掃掉，然後 5 分K 又收回去。
  掃上方：高點被刺破，5 分K 收在高點下方 → 留意做空
  掃下方：低點被刺破，5 分K 收在低點上方 → 留意做多
關鍵價位（大價位）：上個日盤高低、上個夜盤高低、今天開盤 30 分鐘高低、本盤（目前這個盤）到目前的高低（要形成 30 分鐘以上）
盤中波段（小價位）：左右各 2 根 5 分K 都比它低（高）的點；只看最近 3 小時、離現價 150 點內，同方向同時只提醒一個
提醒裡會標「大價位／小價位」，並附這根 5 分K 的成交量是近 20 根平均的幾倍（只是參考，不拿來過濾）
兩段提醒：
  第 1 段：收回的那根 5 分K 收盤時，提醒「掃蕩出現」
  第 2 段：之後連續 2 根 5 分K 都沒有再創新高（低），而且收盤都在價位的收回那一側 → 提醒「確認」
           中途又創新高（低）或收盤又站回去 → 提醒「失敗」，不用再盯
不提醒：收盤前最後 5 分鐘的訊號、刺破不到 3 點、機器人重啟後不完整的那一根 5 分K
5 分K 是機器人自己每 5 秒抓期交所報價組出來的（用盤中最高最低價補上兩次抓價之間的針）。
數字都在 config.py 的 FUT_SWEEP_ 開頭（沒設定就用預設值）。
"""
import csv
import io
import json
import os
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config as cfg
import futures_guard as G

TZ = ZoneInfo("Asia/Taipei")
LEVEL_FILE = "/tmp/futures_levels.json"
ZIP_URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{d}.zip"
HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData"}
NAMES = {"day_hi": "上個日盤高", "day_lo": "上個日盤低", "night_hi": "上個夜盤高", "night_lo": "上個夜盤低",
         "or_hi": "今日開盤30分高", "or_lo": "今日開盤30分低", "sess_hi": "本盤高", "sess_lo": "本盤低"}
STORED = ("day_hi", "day_lo", "night_hi", "night_lo", "or_hi", "or_lo")      # 會存檔的價位（本盤高低隨時在變，不存）
SESS_NAME = {"0": "日盤", "1": "夜盤"}


def _cfg(name, default):
    return getattr(cfg, name, default)


def cells(s):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def f0(x):
    return f"{x:,.0f}"


# ───────────── 5 分K 組合 ─────────────
class BarBuilder:
    """把一次一次抓到的報價組成 5 分K。feed() 回傳這次完成的 5 分K 清單（含成交量 v，算不出來是 None）"""

    def __init__(self):
        self.cur = None
        self.skey = None                  # (交易日, 盤別)
        self.hi = None                    # 這個盤到目前的最高、最低（期交所給的）
        self.lo = None
        self.cum = None                   # 這個盤到目前的累計成交量（期交所給的）

    @staticmethod
    def _fin(b):
        c0, c1 = b.pop("cum0", None), b.pop("cum1", None)
        b["v"] = (c1 - c0) if (c0 is not None and c1 is not None and c1 >= c0) else None
        return b

    def feed(self, now, q):
        sess = G.session_of(now)
        if sess is None or q.get("last") is None:
            return []
        p = q["last"]
        day = G.trade_day(now)
        hhmm = now.hour * 100 + now.minute // 5 * 5
        key = (day, sess, hhmm)
        skey = (day, sess)
        out = []
        if skey != self.skey:                                  # 換盤：最高最低、累計量重新開始
            if self.cur:
                out.append(self._fin(self.cur))
                self.cur = None
            self.skey, self.hi, self.lo, self.cum = skey, None, None, None
        if self.cur is None or self.cur["key"] != key:
            first = self.cur is None
            if self.cur:
                out.append(self._fin(self.cur))
            partial = first and (now.minute % 5 != 0 or now.second > 20)
            base = self.cum if self.cum is not None else (0 if (first and not partial and hhmm in (845, 1500)) else None)
            self.cur = {"key": key, "day": day, "sess": sess, "t": hhmm, "o": p, "h": p, "l": p, "c": p,
                        "partial": partial, "cum0": base, "cum1": None}
        b = self.cur
        b["h"], b["l"], b["c"] = max(b["h"], p), min(b["l"], p), p
        qh, ql = q.get("high"), q.get("low")
        if qh is not None and self.hi is not None and qh > self.hi:     # 兩次抓價之間創的新高，補進這根的高點
            b["h"] = max(b["h"], qh)
        if ql is not None and self.lo is not None and ql < self.lo:
            b["l"] = min(b["l"], ql)
        for v in (qh, p):
            if v is not None:
                self.hi = v if self.hi is None else max(self.hi, v)
        for v in (ql, p):
            if v is not None:
                self.lo = v if self.lo is None else min(self.lo, v)
        if q.get("vol") is not None:
            b["cum1"] = self.cum = q["vol"]
        return out

    def flush(self):
        """盤結束：回傳 (最後一根, 盤別, 這個盤最高, 最低)；沒東西回 None"""
        if self.skey is None:
            return None
        res = ([self._fin(self.cur)] if self.cur else [], self.skey[1], self.hi, self.lo)
        self.cur, self.skey, self.hi, self.lo, self.cum = None, None, None, None, None
        return res


# ───────────── 掃蕩判斷（純計算）─────────────
class Engine:
    def __init__(self, levels=None):
        self.levels = {k: v for k, v in (levels or {}).items() if k in STORED and v}
        self.pending = []
        self.done = set()
        self.skey = None
        self.sess_open = None
        self.nbars = 0
        self.or_bars = []
        self.hist = []                    # 這個盤已完成的 5 分K
        self.swings = []                  # 盤中波段高低（小價位）
        self.vols = []                    # 這個盤每根 5 分K 的成交量

    # 盤結束：把這個盤的高低存成「上個日盤／夜盤」
    def close_session(self, sess, hi, lo):
        if hi is None or lo is None:
            return False
        pre = "day" if sess == "0" else "night"
        self.levels[pre + "_hi"], self.levels[pre + "_lo"] = hi, lo
        if sess == "0":
            self.levels.pop("or_hi", None)
            self.levels.pop("or_lo", None)
        self.pending, self.swings = [], []
        return True

    @staticmethod
    def _name_line(s):
        return f"{NAMES[s['name']]} {f0(s['level'])}" if s["tier"] == "大" else f"盤中波段{'高' if s['dir'] == 'short' else '低'} {f0(s['level'])}"

    def _sweep_text(self, s, bar, ratio=None):
        up = s["dir"] == "short"
        trend = bar["c"] - self.sess_open if self.sess_open else 0
        if self.nbars <= 1:
            tag = "剛開盤 走勢未定"
        elif abs(trend) < 30:
            tag = "今日走勢 盤整"
        else:
            with_trend = (trend < 0) if up else (trend > 0)
            tag = f"今日{'偏多' if trend > 0 else '偏空'}（{'順勢' if with_trend else '逆勢'}）"
        sign = "高" if up else "低"
        L = ["🔻 掃上方，留意做空" if up else "🔺 掃下方，留意做多", f"級別 {s['tier']}價位", self._name_line(s)]
        if s["tier"] == "小":
            L.append(f"{s['t'] // 100:02d}:{s['t'] % 100:02d} 形成")
        L += [f"刺破 {s['pierce']:.0f} 點 {sign} {f0(s['ext'])}", f"5分K 收回 {f0(bar['c'])}"]
        if ratio is not None:
            L.append(f"這根成交量 {ratio:.1f} 倍均量")
        L += [tag, f"停損參考 {f0(s['ext'])} {'上' if up else '下'}", f"接著看 {_cfg('FUT_SWEEP_BARS', 2)} 根 5分K"]
        return L

    def _update_swings(self):
        """每多一根 5 分K：往回看第 N 根，如果它比左右各 N 根都高（低），就成為一個波段高（低）"""
        n = _cfg("FUT_SWEEP_SWING_N", 2)
        h = self.hist
        i = len(h) - 1 - n
        if i < n:
            return
        b, left, right = h[i], h[i - n:i], h[i + 1:i + 1 + n]
        if b["h"] > max(x["h"] for x in left) and b["h"] >= max(x["h"] for x in right):
            self.swings.append({"dir": "short", "level": b["h"], "idx": i, "t": b["t"], "name": f"sw_hi_{b['t']}"})
        if b["l"] < min(x["l"] for x in left) and b["l"] <= min(x["l"] for x in right):
            self.swings.append({"dir": "long", "level": b["l"], "idx": i, "t": b["t"], "name": f"sw_lo_{b['t']}"})

    def on_bar(self, bar):
        """一根完成的 5 分K → 回傳要提醒的文字清單（每則是 list of 行）"""
        out = []
        if bar.get("partial"):
            return out
        skey = (bar["day"], bar["sess"])
        if skey != self.skey:
            self.skey, self.sess_open, self.pending, self.or_bars, self.nbars = skey, None, [], [], 0
            self.hist, self.swings, self.vols = [], [], []
            if bar["sess"] == "0":
                self.levels.pop("or_hi", None)
                self.levels.pop("or_lo", None)
        if self.sess_open is None:
            self.sess_open = bar["o"]
        self.nbars += 1
        need = _cfg("FUT_SWEEP_BARS", 2)
        mn = _cfg("FUT_SWEEP_MIN", 3)
        # 這根的成交量是平常的幾倍（只放在提醒裡給你參考，不拿來過濾）
        prior = [v for v in self.vols[-20:] if v]
        ratio = (bar["v"] / (sum(prior) / len(prior))) if (bar.get("v") and len(prior) >= 6) else None
        # 開盤 30 分鐘高低（日盤 08:45～09:15）
        if bar["sess"] == "0":
            if bar["t"] < 915:
                self.or_bars.append(bar)
            elif "or_hi" not in self.levels and len(self.or_bars) >= 6:
                self.levels["or_hi"] = max(b["h"] for b in self.or_bars)
                self.levels["or_lo"] = min(b["l"] for b in self.or_bars)
        # 1. 先處理已經在等確認的掃蕩
        keep = []
        for s in self.pending:
            up = s["dir"] == "short"
            s["n"] += 1
            broke = (bar["h"] > s["ext"]) if up else (bar["l"] < s["ext"])
            back = (bar["c"] >= s["level"]) if up else (bar["c"] <= s["level"])
            who = f"（{'空' if up else '多'}·{s['tier']}）"
            if broke or back:
                why = ("又創高 " if up else "又創低 ") + f0(bar["h"] if up else bar["l"]) if broke else \
                      ("收盤又站上 " if up else "收盤又跌破 ") + f0(s["level"])
                out.append([f"❌ 掃蕩失敗{who}", self._name_line(s), why, "這個訊號不用看了"])
            elif s["n"] >= need:
                out.append([f"✅ 掃蕩確認{who}", self._name_line(s),
                            f"連 {need} 根 沒再創{'高' if up else '低'}", f"現價 {f0(bar['c'])}",
                            f"停損參考 {f0(s['ext'])} {'上' if up else '下'}"])
            else:
                keep.append(s)
        self.pending = keep
        # 2. 盤中波段高低：先更新，再看這根有沒有把它們掃掉
        self.hist.append(bar)
        if _cfg("FUT_SWEEP_SWING_ON", True):
            self._update_swings()
        # 收盤前最後 5 分鐘不提醒（但價位還是照常更新）
        last_bar = (bar["sess"] == "0" and bar["t"] >= 1340) or (bar["sess"] == "1" and 455 <= bar["t"] < 500)
        fired = set()
        if not last_bar:
            busy = {s["name"] for s in self.pending}
            lv = dict(self.levels)
            prev = self.hist[:-1]                       # 本盤高低（到上一根為止）：形成至少 30 分鐘才算，不然剛創高就被自己掃，沒意義
            if prev:
                hi_i = max(range(len(prev)), key=lambda i: (prev[i]["h"], -i))
                lo_i = min(range(len(prev)), key=lambda i: (prev[i]["l"], i))
                cur_i = len(self.hist) - 1
                if cur_i - hi_i >= _cfg("FUT_SWEEP_SESS_AGE", 6):
                    lv["sess_hi"] = prev[hi_i]["h"]
                if cur_i - lo_i >= _cfg("FUT_SWEEP_SESS_AGE", 6):
                    lv["sess_lo"] = prev[lo_i]["l"]
            for name, level in lv.items():
                dk = (bar["day"], bar["sess"], name if not name.startswith("sess_") else f"{name}@{level}")
                if name in busy or dk in self.done:
                    continue
                up = name.endswith("_hi")
                if up and bar["h"] >= level + mn and bar["c"] < level:
                    s = {"dir": "short", "name": name, "level": level, "ext": bar["h"], "pierce": bar["h"] - level, "n": 0, "tier": "大"}
                elif (not up) and bar["l"] <= level - mn and bar["c"] > level:
                    s = {"dir": "long", "name": name, "level": level, "ext": bar["l"], "pierce": level - bar["l"], "n": 0, "tier": "大"}
                else:
                    continue
                self.done.add(dk)
                self.pending.append(s)
                fired.add(s["dir"])
                out.append(self._sweep_text(s, bar, ratio))
        if _cfg("FUT_SWEEP_SWING_ON", True):
            idx, age, dist = len(self.hist) - 1, _cfg("FUT_SWEEP_SWING_AGE", 36), _cfg("FUT_SWEEP_SWING_DIST", 150)
            alive, cands = [], {"short": [], "long": []}
            for w in self.swings:
                if idx - w["idx"] > age:
                    continue                                              # 太久以前的不算
                up = w["dir"] == "short"
                if (bar["h"] > w["level"]) if up else (bar["l"] < w["level"]):      # 被刺破了：不管有沒有收回，這個價位用掉了
                    ok = (bar["h"] >= w["level"] + mn and bar["c"] < w["level"]) if up else (bar["l"] <= w["level"] - mn and bar["c"] > w["level"])
                    if ok and abs(bar["c"] - w["level"]) <= dist:
                        cands[w["dir"]].append(w)
                    continue
                alive.append(w)
            self.swings = alive
            if not last_bar:
                for d, ws in cands.items():
                    if not ws or d in fired or any(p["tier"] == "小" and p["dir"] == d for p in self.pending):
                        continue
                    w = max(ws, key=lambda x: x["level"]) if d == "short" else min(ws, key=lambda x: x["level"])
                    up = d == "short"
                    s = {"dir": d, "name": w["name"], "level": w["level"], "t": w["t"], "n": 0, "tier": "小",
                         "ext": bar["h"] if up else bar["l"], "pierce": (bar["h"] - w["level"]) if up else (w["level"] - bar["l"])}
                    self.pending.append(s)
                    out.append(self._sweep_text(s, bar, ratio))
        if bar.get("v"):
            self.vols.append(bar["v"])
        return out


# ───────────── 報價 ─────────────
def quote_full(now=None):
    """近月台指期：{last, high, low, vol}（高低、累計成交量都是這個盤到目前為止）；沒開盤回 None"""
    now = now or datetime.now(TZ)
    mt = G.session_of(now)
    if mt is None:
        return None
    body = {"MarketType": mt, "SymbolType": "F", "KindID": "1", "CID": "TXF",
            "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}
    r = requests.post(G.MIS_URL, json=body, timeout=15,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json;charset=UTF-8",
                               "Origin": "https://mis.taifex.com.tw", "Referer": "https://mis.taifex.com.tw/futures/"})
    r.raise_for_status()
    j = r.json()
    if str(j.get("RtCode")) != "0":
        raise ValueError(f"期交所回應異常：{j.get('RtMsg')}")
    q = G.pick_near((j.get("RtData") or {}).get("QuoteList") or [], now)
    if q is None:
        raise ValueError("查無報價")
    return {"last": G._num(q.get("CLastPrice")), "high": G._num(q.get("CHighPrice")), "low": G._num(q.get("CLowPrice")),
            "vol": G._num(q.get("CTotalVolume"))}


# ───────────── 關鍵價位：存檔與開機時補算 ─────────────
def load_levels(now=None):
    now = now or datetime.now(TZ)
    try:
        with open(LEVEL_FILE, encoding="utf-8") as f:
            j = json.load(f)
        if now - datetime.fromisoformat(j["asof"]) < timedelta(days=4):
            return {k: v for k, v in j["levels"].items() if k in STORED}
    except Exception:
        pass
    return {}


def save_levels(levels, now=None):
    now = now or datetime.now(TZ)
    try:
        with open(LEVEL_FILE, "w", encoding="utf-8") as f:
            json.dump({"asof": now.isoformat(), "levels": levels}, f)
    except Exception:
        pass


def fetch_zip(d):
    for _ in range(2):
        try:
            r = requests.get(ZIP_URL.format(d=d.strftime("%Y_%m_%d")), headers=HEAD, timeout=90)
            return r.content if (r.status_code == 200 and r.content.startswith(b"PK")) else None
        except Exception as e:
            print("期交所每日檔下載失敗：", repr(e)[:80], flush=True)
    return None


def seed_levels(now, fetch=fetch_zip, files=3):
    """機器人剛啟動、沒有存檔時：從期交所最近幾天的逐筆檔，算出最近一個完整的日盤／夜盤高低。
    一行一行讀、不把整個檔案放進記憶體（Render 只有 512MB）"""
    seg = {}                                      # {(盤別, 起算日, 到期月): [高, 低]}
    got, d = 0, now.date()
    for _ in range(8):
        if got >= files:
            break
        raw = fetch(d)
        d -= timedelta(days=1)
        if raw is None:
            continue
        got += 1
        z = zipfile.ZipFile(io.BytesIO(raw))
        with z.open(z.namelist()[0]) as fh:
            rd = csv.reader(io.TextIOWrapper(fh, encoding="cp950", errors="replace"))
            next(rd, None)
            for row in rd:
                try:
                    if len(row) < 6 or row[1].strip() != "TX" or "/" in row[2]:
                        continue
                    t, px, day = int(row[3].strip()), float(row[4].strip()), datetime.strptime(row[0].strip(), "%Y%m%d").date()
                except ValueError:
                    continue
                if 84500 <= t <= 134500:
                    k = ("0", day, row[2].strip())
                elif t >= 150000:
                    k = ("1", day, row[2].strip())
                elif t < 50000:
                    k = ("1", day - timedelta(days=1), row[2].strip())
                else:
                    continue
                v = seg.get(k)
                if v is None:
                    seg[k] = [px, px]
                else:
                    v[0], v[1] = max(v[0], px), min(v[1], px)
    best = {}                                     # {盤別: (起算日, 高, 低)}，只收已經結束的盤
    for s in ("0", "1"):
        ends = {}
        for (sk, day, exp), (hi, lo) in seg.items():
            if sk != s:
                continue
            end = datetime(day.year, day.month, day.day, 13, 45, tzinfo=TZ) if s == "0" else datetime(day.year, day.month, day.day, 5, 0, tzinfo=TZ) + timedelta(days=1)
            if end <= now:
                if day not in ends or exp < ends[day][0]:           # 同一天有兩個到期月：用最近月
                    ends[day] = (exp, hi, lo)
        if ends:
            day = max(ends)
            best[s] = (day, ends[day][1], ends[day][2])
    out = {}
    if "0" in best:
        out["day_hi"], out["day_lo"] = best["0"][1], best["0"][2]
    if "1" in best:
        out["night_hi"], out["night_lo"] = best["1"][1], best["1"][2]
    return out


# ───────────── 背景執行 ─────────────
def parse_manual(txt):
    """'day_hi=44250,day_lo=42521' → {'day_hi': 44250.0, 'day_lo': 42521.0}；格式不對的略過"""
    out = {}
    for part in (txt or "").replace("，", ",").split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        try:
            if k in NAMES:
                out[k] = float(v.strip().replace(",", ""))
        except ValueError:
            pass
    return out


def card(lines):
    w = max(max(cells(l) for l in lines) + 4, 18)
    import html
    return "<pre>" + "\n".join(html.escape(l) + " " * (w - cells(l)) for l in lines) + "</pre>"


class Runner:
    """每次呼叫 step() 做一次：抓價、組 5 分K、判斷、通知。send(lines) 由外面決定怎麼傳"""

    def __init__(self, send, quote=quote_full, seed=seed_levels):
        self.send, self.quote, self.seed = send, quote, seed
        self.bb = BarBuilder()
        self.eng = Engine(load_levels())
        self.seeded = False
        self.hello = False
        self.errs = 0

    def ensure_levels(self, now):
        if self.seeded:
            return
        self.seeded = True
        manual = parse_manual(os.getenv("SWEEP_LEVELS", ""))
        if manual:                                  # 手動指定的價位（例：今天日盤高低，期交所檔案還沒出來時用）
            self.eng.levels.update(manual)
            print("掃蕩關鍵價位（手動）：", manual, flush=True)
        if not all(k in self.eng.levels for k in ("day_hi", "night_hi")):
            try:
                got = self.seed(now)
                for k, v in got.items():
                    self.eng.levels.setdefault(k, v)
                save_levels(self.eng.levels, now)
                print("掃蕩關鍵價位：", {NAMES[k]: v for k, v in self.eng.levels.items()}, flush=True)
            except Exception as e:
                print("掃蕩關鍵價位補算失敗：", repr(e)[:120], flush=True)

    def step(self, now=None):
        """回傳這一次送出的提醒數（測試用）；沒開盤時回 -1"""
        now = now or datetime.now(TZ)
        n = 0
        self.ensure_levels(now)
        if G.session_of(now) is None:
            fl = self.bb.flush()
            if fl:
                bars, sess, hi, lo = fl
                for b in bars:
                    for lines in self.eng.on_bar(b):
                        self.send(lines)
                        n += 1
                if self.eng.close_session(sess, hi, lo):
                    save_levels(self.eng.levels, now)
            return -1 if n == 0 else n
        q = self.quote(now)
        if not self.hello and q.get("last") is not None:        # 開機後第一次抓到報價：私訊你一張「運作中」卡片
            self.hello = True
            L = ["✅ 掃蕩提醒運作中", f"台指近月 {f0(q['last'])}",
                 "盤中高低 " + ("有" if q.get("high") is not None and q.get("low") is not None else "沒有（針會漏）")]
            L += [f"{NAMES[k]} {f0(v)}" for k, v in self.eng.levels.items()] or ["關鍵價位 還沒有"]
            self.send(L)
            n += 1
        for b in self.bb.feed(now, q):
            for lines in self.eng.on_bar(b):
                self.send(lines)
                n += 1
        return n

    def run(self):
        while True:
            try:
                r = self.step()
                self.errs = 0
            except Exception as e:
                r = 0
                self.errs += 1
                print("掃蕩監控錯誤：", repr(e)[:160], flush=True)
                if self.errs == 20:                                  # 連續 20 次（約 2 分鐘）抓不到
                    try:
                        self.send(["⚠️ 期貨報價連續抓不到", "掃蕩提醒可能失效"])
                    except Exception:
                        pass
            time.sleep(_cfg("FUT_SWEEP_POLL", 5) if r != -1 else 30)


def loop(send):
    """send(lines)：把一組短行做成字卡私訊管理者"""
    Runner(send).run()
