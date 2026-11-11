"""futures_sweep.py — 台指期「流動性掃蕩」提醒（只私訊管理者，不發群組，不下單）

什麼是掃蕩：價格刺破一個關鍵高（低）點，把停損單掃掉，然後 5 分K 又收回去。
  掃上方：高點被刺破，5 分K 收在高點下方 → 留意做空
  掃下方：低點被刺破，5 分K 收在低點上方 → 留意做多
關鍵價位（大價位）：上個日盤高低、上個夜盤高低、今天開盤 30 分鐘高低、本盤（目前這個盤）到目前的高低（要形成 30 分鐘以上）
盤中波段（小價位）：左右各 2 根 5 分K 都比它低（高）的點；只看最近 3 小時、離現價 150 點內，同方向同時只提醒一個
價格先站上（跌破）價位再回來也算（最多停留 3 根 5 分K），停損參考用這段期間最極端的點
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
        self.pivot = (levels or {}).get("pivot") or None   # 基準價＝前一個日盤 (高+低+收)/3；收盤在它上面算偏多，下面算偏空
        self.last_c = None                                 # (盤別, 最近一根收盤)，日盤結束時算基準價用
        self.pending = []
        self.done = set()
        self.skey = None
        self.sess_open = None
        self.nbars = 0
        self.or_bars = []
        self.hist = []                    # 這個盤已完成的 5 分K
        self.swings = []                  # 盤中波段高低（小價位）
        self.vols = []                    # 這個盤每根 5 分K 的成交量
        self.exc = {}                     # 價位被「收盤站上（跌破）」之後的停留狀態：{價位: {ext 最極端的點, n 停留幾根}}
        self.seed_key = None              # 機器人重啟時，用期交所給的「本盤到目前的高低」補上重啟前的本盤高低
        self.seed_hi = None
        self.seed_lo = None

    # 盤結束：把這個盤的高低存成「上個日盤／夜盤」
    def close_session(self, sess, hi, lo):
        if hi is None or lo is None:
            return False
        pre = "day" if sess == "0" else "night"
        self.levels[pre + "_hi"], self.levels[pre + "_lo"] = hi, lo
        if sess == "0" and self.last_c and self.last_c[0] == "0":
            self.pivot = (hi + lo + self.last_c[1]) / 3
        if sess == "0":
            self.levels.pop("or_hi", None)
            self.levels.pop("or_lo", None)
        self.pending, self.swings = [], []
        return True

    def seed_session(self, day, sess, hi, lo):
        """重啟後記憶體是空的：把期交所給的「本盤到目前的最高、最低」當成早就形成的本盤高低（重啟前創的，一定超過 30 分鐘）"""
        self.seed_key, self.seed_hi, self.seed_lo = (day, sess), hi, lo

    def retire_passed(self, day, sess, hi, lo):
        """機器人剛啟動（或重啟）時：這個盤已經超過（跌破）的價位，不知道當時有沒有提醒過，一律當成用掉了，避免重複提醒"""
        mn = _cfg("FUT_SWEEP_MIN", 3)
        gone = []
        for name, level in self.levels.items():
            up = name.endswith("_hi")
            if (up and hi is not None and hi >= level + mn) or ((not up) and lo is not None and lo <= level - mn):
                self.done.add((day, sess, name))
                gone.append(name)
        return gone

    @staticmethod
    def _name_line(s):
        return f"{NAMES[s['name']]} {f0(s['level'])}" if s["tier"] == "大" else f"盤中波段{'高' if s['dir'] == 'short' else '低'} {f0(s['level'])}"

    def _sweep_text(self, s, bar, ratio=None):
        up = s["dir"] == "short"
        if self.pivot:                                        # 收盤在前一個日盤的基準價上面＝偏多，下面＝偏空
            trend = bar["c"] - self.pivot
            with_trend = (trend < 0) if up else (trend > 0)
            tag = f"{'偏多' if trend > 0 else '偏空'}（{'順勢' if with_trend else '逆勢'}）"
        else:
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
        L += [f"刺破 {s['pierce']:.0f} 點 {sign} {f0(s['ext'])}"]
        if s.get("held"):
            L.append(f"先在價位{'上' if up else '下'}方 {s['held']} 根")
        L.append(f"5分K 收回 {f0(bar['c'])}")
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
        self.last_c = (bar["sess"], bar["c"])
        if skey != self.skey:
            self.skey, self.sess_open, self.pending, self.or_bars, self.nbars = skey, None, [], [], 0
            self.hist, self.swings, self.vols, self.exc = [], [], [], {}
            if self.seed_key != skey:
                self.seed_hi = self.seed_lo = None
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
            who = f"（{'做空' if up else '做多'}）"
            if broke or back:
                why = ("又創高 " if up else "又創低 ") + f0(bar["h"] if up else bar["l"]) if broke else \
                      ("收盤又站上 " if up else "收盤又跌破 ") + f0(s["level"])
                out.append([f"❌ 掃蕩失敗{who}", f"級別 {s['tier']}價位", self._name_line(s), why, "這個訊號不用看了"])
            elif s["n"] >= need:
                out.append([f"✅ 掃蕩確認{who}", f"級別 {s['tier']}價位", self._name_line(s),
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
            if self.seed_key == skey:
                if self.seed_hi is not None:
                    lv["sess_hi"] = max(lv.get("sess_hi", self.seed_hi), self.seed_hi)
                if self.seed_lo is not None:
                    lv["sess_lo"] = min(lv.get("sess_lo", self.seed_lo), self.seed_lo)
            for name, level in lv.items():
                dk = (bar["day"], bar["sess"], name if not name.startswith("sess_") else f"{name}@{level}")
                if name in busy or dk in self.done:
                    continue
                up = name.endswith("_hi")
                xk = f"{name}@{level}"
                ex = self.exc.get(xk)
                hold = _cfg("FUT_SWEEP_HOLD", 3)
                beyond = (bar["c"] >= level) if up else (bar["c"] <= level)         # 這根收在價位的「外側」（站上高點／跌破低點）
                reach = (bar["h"] >= level + mn) if up else (bar["l"] <= level - mn)
                if beyond:                                                          # 還沒收回：記下停留幾根、最極端的點（停損要放這個點外面）
                    if ex is None and reach:
                        ex = self.exc[xk] = {"ext": level, "n": 0}
                    if ex is not None and not ex.get("dead"):
                        ex["ext"] = max(ex["ext"], bar["h"]) if up else min(ex["ext"], bar["l"])
                        ex["n"] += 1
                        if ex["n"] > hold:                                          # 站太久了：不算掃蕩，等它回來也不提醒
                            ex["dead"] = True
                    continue
                self.exc.pop(xk, None)                                              # 這根收回來了
                if ex is not None and ex.get("dead"):
                    self.done.add(dk)                                               # 已經站上（跌破）很久才回來：這個價位在這個盤已經被用掉了，不再提醒
                    continue
                far = max(bar["h"], ex["ext"]) if (up and ex) else bar["h"] if up else (min(bar["l"], ex["ext"]) if ex else bar["l"])
                if (up and far < level + mn) or ((not up) and far > level - mn):
                    continue
                s = {"dir": "short" if up else "long", "name": name, "level": level, "ext": far,
                     "pierce": (far - level) if up else (level - far), "n": 0, "tier": "大", "held": ex["n"] if ex else 0}
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
            return {k: v for k, v in j["levels"].items() if k in STORED or k == "pivot"}
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
    lastt, lastp = {}, {}
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
                    if t >= lastt.get(k, 0):
                        lastt[k], lastp[k] = t, px          # 日盤最後一筆成交價＝收盤價
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
            if s == "0" and (s, day, ends[day][0]) in lastp:
                best["close"] = lastp[(s, day, ends[day][0])]
    out = {}
    if "0" in best:
        out["day_hi"], out["day_lo"] = best["0"][1], best["0"][2]
        if "close" in best:
            out["pivot"] = (best["0"][1] + best["0"][2] + best["close"]) / 3
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
        self.eng.pivot = self.eng.pivot or None
        self.seeded = False
        self.hello = False
        self.errs = 0
        self.last_q = None
        self.last_at = None

    def _save(self, now):
        save_levels({**self.eng.levels, **({"pivot": self.eng.pivot} if self.eng.pivot else {})}, now)

    def level_lines(self, now=None):
        """目前所有價位（含這個盤到目前為止的高低），給「期貨→行情」和開機卡片用"""
        now = now or datetime.now(TZ)
        sess = G.session_of(now)
        L = []
        if self.last_q and self.last_q.get("last") is not None and sess is not None:
            L.append(f"現價 {f0(self.last_q['last'])}")
        if sess is not None and self.bb.hi is not None and self.bb.lo is not None:
            nm = "今日盤" if sess == "0" else "今夜盤"
            L += [f"{nm}高 {f0(self.bb.hi)}", f"{nm}低 {f0(self.bb.lo)}"]
        for k in ("day_hi", "day_lo", "night_hi", "night_lo", "or_hi", "or_lo"):
            if k in self.eng.levels:
                L.append(f"{NAMES[k]} {f0(self.eng.levels[k])}")
        if self.eng.pivot:
            L.append(f"基準價 {f0(self.eng.pivot)}")
        if sess is None:
            L.insert(0, "目前沒開盤")
        return L

    def ensure_levels(self, now):
        if self.seeded:
            return
        self.seeded = True
        manual = parse_manual(os.getenv("SWEEP_LEVELS", ""))
        if manual:                                  # 手動指定的價位（例：今天日盤高低，期交所檔案還沒出來時用）
            self.eng.levels.update(manual)
            print("掃蕩關鍵價位（手動）：", manual, flush=True)
        if not all(k in self.eng.levels for k in ("day_hi", "night_hi")) or not self.eng.pivot:
            try:
                got = self.seed(now)
                if got.get("pivot") and not self.eng.pivot:
                    self.eng.pivot = got["pivot"]
                for k, v in got.items():
                    if k != "pivot":
                        self.eng.levels.setdefault(k, v)
                self._save(now)
                print("掃蕩關鍵價位：", {NAMES[k]: v for k, v in self.eng.levels.items()}, "基準價", self.eng.pivot, flush=True)
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
                    self._save(now)
            return -1 if n == 0 else n
        q = self.quote(now)
        self.last_q, self.last_at = q, now
        if not self.hello and q.get("last") is not None:        # 開機後第一次抓到報價：私訊你一張「運作中」卡片
            self.hello = True
            self.last_q = q
            L = ["✅ 掃蕩提醒運作中", "盤中高低 " + ("有" if q.get("high") is not None and q.get("low") is not None else "沒有（針會漏）")]
            self.bb.hi, self.bb.lo = (self.bb.hi if self.bb.hi is not None else q.get("high")), (self.bb.lo if self.bb.lo is not None else q.get("low"))
            gone = self.eng.retire_passed(G.trade_day(now), G.session_of(now), self.bb.hi, self.bb.lo)
            if gone:
                print("重啟後視為已用掉的價位：", [NAMES[k] for k in gone], flush=True)
            sess_now = G.session_of(now)
            start = now.replace(hour=8 if sess_now == "0" else 15, minute=45 if sess_now == "0" else 0, second=0, microsecond=0)
            if sess_now == "1" and now.hour < 15:
                start -= timedelta(days=1)
            if sess_now is not None and (now - start) >= timedelta(minutes=30) and self.bb.hi is not None and self.bb.lo is not None:
                self.eng.seed_session(G.trade_day(now), sess_now, self.bb.hi, self.bb.lo)
            L += self.level_lines(now) or ["關鍵價位 還沒有"]
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


RUNNER = None


def status_lines():
    """「期貨→行情」：現價、今盤高低、各個關鍵價位"""
    if RUNNER is None:
        return ["掃蕩提醒還沒啟動"]
    now = datetime.now(TZ)
    return [f"📍 台指期價位 {now:%H:%M}"] + (RUNNER.level_lines(now) or ["還沒有價位資料"])


def loop(send):
    """send(lines)：把一組短行做成字卡私訊管理者"""
    global RUNNER
    RUNNER = Runner(send)
    RUNNER.run()
