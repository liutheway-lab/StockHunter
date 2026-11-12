"""futures_sweep.py — 台指期「流動性掃蕩」提醒（只私訊管理者，不發群組，不下單）

什麼是掃蕩：價格刺破一個關鍵高（低）點，把停損單掃掉，然後 5 分K 又收回去。
  掃上方：高點被刺破，5 分K 收在高點下方 → 留意做空
  掃下方：低點被刺破，5 分K 收在低點上方 → 留意做多
關鍵價位（大價位）：上個交易日高低、今日全天高低（含昨夜盤）、今天開盤 30 分鐘高低、本盤（目前這個盤）到目前的高低（要形成 30 分鐘以上）
  一個交易日＝夜盤＋日盤：昨天 15:00 開的夜盤，和今天的日盤是同一天；昨天日盤和前天夜盤是同一天
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
import threading
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
NAMES = {"prev_hi": "上個交易日高", "prev_lo": "上個交易日低", "td_hi": "今日全天高", "td_lo": "今日全天低",
         "or_hi": "今日開盤30分高", "or_lo": "今日開盤30分低", "sess_hi": "本盤高", "sess_lo": "本盤低"}
STORED = ("prev_hi", "prev_lo", "td_hi", "td_lo", "or_hi", "or_lo")      # 會存檔的價位（本盤高低隨時在變，不存）
ALIAS = {"day_hi": "prev_hi", "day_lo": "prev_lo", "night_hi": "td_hi", "night_lo": "td_lo"}   # 舊的手動設定名稱（SWEEP_LEVELS）
KEEP_DAYS = 6                                                               # 存檔保留最近幾個交易日的夜盤／日盤紀錄
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
LOG_FILE = "/tmp/sweep_log.json"
LOG_NAME = "sweep_log.json"


def us_zone(ts, t=None, sess=None):
    """這個時間點屬於哪個時段：'日盤'、'美股開盤前後'（夜盤，美股 9:30 開盤前 30 分到後 60 分，夏令冬令自動換算）、'夜盤其他'"""
    if ts is None:                                           # 舊紀錄沒存時間：用冬令時間 22:30 當開盤
        if sess == "0":
            return "日盤"
        return "美股開盤前後" if t is not None and 2200 <= t < 2330 else "夜盤其他"
    if G.session_of(ts) == "0":
        return "日盤"
    try:
        ny = ZoneInfo("America/New_York")
        d0 = ts.astimezone(ny).date()
        for k in (-1, 0, 1):
            d = d0 + timedelta(days=k)
            op = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ny).astimezone(TZ)
            if -_cfg("FUT_US_BEFORE", 30) <= (ts - op).total_seconds() / 60 < _cfg("FUT_US_AFTER", 60):
                return "美股開盤前後"
    except Exception:
        pass
    return "夜盤其他"


class Journal:
    """掃蕩紀錄：每次提醒自動記一筆。「確認」那根的收盤價當進場價，之後 FUT_LOG_BARS 根 5分K 內，
    記錄價格在碰到停損之前，先走到停損距離（R）的幾倍。遠停損＝掃蕩極點、近停損＝確認那幾根的最低（高）點，兩種各記一份。
    存在 /tmp 與 GitHub（sweep_log.json），重啟後還在"""

    def __init__(self, load=True):
        self.rows = []
        self.meta = {}                                        # {"weekly": 最近一次週報的日期, "milestone": 已推過的確認單筆數}
        self._lock = threading.Lock()
        if load:
            self.load()

    def load(self):
        data = None
        try:
            data = G.gh_read(LOG_NAME)
        except Exception as e:
            print("掃蕩紀錄讀取失敗：", repr(e)[:100], flush=True)
        if data is None:
            try:
                with open(LOG_FILE, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = []
        self.meta = {}
        if isinstance(data, dict):
            self.meta = data.get("meta") or {}
            data = data.get("rows")
        self.rows = data if isinstance(data, list) else []
        for r in self.rows:                                   # 重啟前還在等確認／追蹤的，記憶體已經沒了：標成中斷
            if r.get("status") in ("wait", "track"):
                was = r.get("status")
                r["status"], r["result"] = "done", ("中斷" if was == "wait" else "完成")

    def _save(self):
        rows = {"rows": self.rows[-2000:], "meta": dict(self.meta)}
        try:
            with open(LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
        except Exception:
            pass
        if not getattr(G, "GH_TOKEN", ""):
            return

        def go():
            with self._lock:
                try:
                    G.gh_write(LOG_NAME, rows, "更新掃蕩紀錄")
                except Exception as e:
                    print("掃蕩紀錄存檔失敗：", repr(e)[:100], flush=True)
        threading.Thread(target=go, daemon=True).start()

    def opened(self, s, bar, ratio):
        s["jid"] = len(self.rows)
        self.rows.append({"day": bar["day"], "sess": bar["sess"], "t": bar["t"], "ts": datetime.now(TZ).isoformat(timespec="minutes"), "name": Engine._name_line(s),
                          "tier": s["tier"], "dir": s["dir"], "level": s["level"], "ext": s["ext"],
                          "trend": s.get("trend", "未定"), "ratio": round(ratio, 1) if ratio else None,
                          "status": "wait", "result": None})
        self._save()

    def _row(self, s):
        i = s.get("jid")
        return self.rows[i] if i is not None and 0 <= i < len(self.rows) else None

    def failed(self, s):
        r = self._row(s)
        if r:
            r["status"], r["result"] = "done", "失敗"
            self._save()

    def confirmed(self, s, bar):
        r = self._row(s)
        if r:
            near = s.get("near")                               # 近停損：確認那幾根 5分K 的最低點（做空＝最高點）
            r.update(status="track", entry=bar["c"], stop=s["ext"], stop2=near if near is not None else s["ext"],
                     bars=0, rf=0.0, rn=0.0, sf=False, sn=False, result=None)
            self._save()

    def on_bar(self, bar):
        """確認後逐根追蹤：遠停損（掃蕩／影線的極點）與近停損（確認那幾根的最低／最高點）各自記錄「碰到停損之前，
        價格先走到停損距離（R）的幾倍」。同一根 K 棒碰到停損，那根的有利走勢不算（保守）。兩個停損都碰到、或追蹤滿 FUT_LOG_BARS 根就結案"""
        nbars = _cfg("FUT_LOG_BARS", 24)
        changed = False
        for r in self.rows:
            if r.get("status") != "track":
                continue
            changed = True
            if (r["day"], r["sess"]) != (bar["day"], bar["sess"]):
                r["status"], r["result"] = "done", "完成"
                continue
            short = r["dir"] == "short"
            fav = (r["entry"] - bar["l"]) if short else (bar["h"] - r["entry"])
            adv = (bar["h"] - r["entry"]) if short else (r["entry"] - bar["l"])
            r["bars"] += 1
            for stop, rk, sk in ((r["stop"], "rf", "sf"), (r.get("stop2", r["stop"]), "rn", "sn")):
                if r.get(sk):
                    continue
                d = max(abs(stop - r["entry"]), 1)
                if adv >= d:
                    r[sk] = True
                else:
                    r[rk] = max(r.get(rk, 0.0), fav / d)
            if (r.get("sf") and r.get("sn")) or r["bars"] >= nbars:
                r["status"], r["result"] = "done", "完成"
        if changed:
            self._save()

    def digest(self, now):
        """該自動統整給你看的時候，回傳 (標題, 卡片行)；不用就回 None。
        每週六 09:00 之後第一次（有資料才推）；確認單每累積滿 FUT_LOG_MILESTONE 筆（預設 30）再推一次"""
        if not _cfg("FUT_LOG_DIGEST", True) or not self.rows:
            return None
        conf = sum(1 for r in self.rows if r.get("entry") is not None)
        step = _cfg("FUT_LOG_MILESTONE", 30)
        title = None
        if now.weekday() == 5 and now.hour >= 9 and self.meta.get("weekly") != now.date().isoformat():
            self.meta["weekly"] = now.date().isoformat()
            self.meta["milestone"] = conf // step * step
            title = "📊 掃蕩週報"
        elif step and conf // step * step > self.meta.get("milestone", 0):
            self.meta["milestone"] = conf // step * step
            title = f"📊 確認單累積 {conf} 筆"
        if title is None:
            return None
        self._save()
        return title, ["（自動統整）"] + self.lines()[1:]

    def lines(self):
        rows = self.rows
        if not rows:
            return ["📊 掃蕩紀錄", "還沒有資料", "之後每次掃蕩提醒", "都會自動記錄"]
        pat = [r for r in rows if r.get("tier") in ("滯漲", "轉折", "破")]
        rows = [r for r in rows if r.get("tier") not in ("滯漲", "轉折", "破")]
        if not rows and not pat:
            return ["📊 掃蕩紀錄", "還沒有資料", "之後每次掃蕩提醒", "都會自動記錄"]
        conf = [r for r in rows if r.get("entry") is not None]
        fail = [r for r in rows if r.get("result") == "失敗"]
        nb = _cfg("FUT_LOG_BARS", 24)
        L = ["📊 掃蕩紀錄", f"提醒 {len(rows)} 次", f"　確認 {len(conf)}｜失敗 {len(fail)}", "",
             f"確認後 {nb * 5 // 60} 小時內", "價格在碰到停損之前", "先走到停損距離（R）", "的幾倍，佔幾 %", "最划算＝期望值最高的", "目標倍數（單位 R）", "",
             "遠＝停損放掃蕩極點", "近＝停損放確認那幾根", "　　最低（高）點", ""]

        def block(title, rs, ev=False):
            done = [r for r in rs if r.get("status") == "done" and "rf" in r]
            out = [title]
            if not done:
                return out + ["　還沒有資料"]
            n = len(done)
            out.append(f"　{n} 筆")
            for nm, sk, rk in (("遠", "stop", "rf"), ("近", "stop2", "rn")):
                if nm == "近" and all(r.get("stop2", r["stop"]) == r["stop"] for r in done):
                    continue                                   # 型態的停損只有一個，不用重複
                ds = [abs(r.get(sk, r["stop"]) - r["entry"]) for r in done]
                ps = [sum(1 for r in done if r.get(rk, 0) >= k) / n for k in (1, 2, 3, 4)]
                out.append(f"　{nm} 停損距離 {sum(ds) / len(ds):.0f} 點")
                out.append(" " + " ".join(f"{k}R{p:.0%}".replace("%", "") for k, p in zip((1, 2, 3, 4), ps)))
                if ev:                                         # 期望值（單位 R）：先走到 k 倍的機率 p，賺 k、賠 1
                    best = max(range(4), key=lambda i: ps[i] * (i + 1) - (1 - ps[i]))
                    e = ps[best] * (best + 1) - (1 - ps[best])
                    out.append(f"　　最划算 {best + 1}R（{e:+.2f}）")
            return out
        L += block("【全部】", conf, ev=True) + [""]
        for tr in ("順勢", "逆勢"):
            L += block(f"【{tr}】", [r for r in conf if r.get("trend") == tr]) + [""]
        L += block("【其他（盤整／未定）】", [r for r in conf if r.get("trend") not in ("順勢", "逆勢")]) + [""]
        def zone(r):
            try:
                ts = datetime.fromisoformat(r["ts"]) if r.get("ts") else None
            except Exception:
                ts = None
            return us_zone(ts, r.get("t"), r.get("sess"))
        for z in ("日盤", "美股開盤前後", "夜盤其他"):
            L += block({"日盤": "【日盤】", "美股開盤前後": "【夜盤・美股開盤前後】", "夜盤其他": "【夜盤・其他】"}[z], [r for r in conf if zone(r) == z]) + [""]
        for tier in ("大", "小", "影線"):
            L += block("【影線】" if tier == "影線" else f"【{tier}價位】", [r for r in conf if r.get("tier") == tier]) + [""]
        for tier, title, dr in (("滯漲", "【放量滯漲／不跌】", None), ("轉折", "【結構轉換】", None),
                                ("破", "【創新低追空】", "short"), ("破", "【創新高追多】", "long")):
            rs = [r for r in pat if r.get("tier") == tier and (dr is None or r.get("dir") == dr)]
            if rs:                                              # 有型態提醒才顯示這一塊
                L += block(title, rs, ev=True)[:1] + [f"　提醒 {len(rs)} 次"] + block(title, rs, ev=True)[1:] + [""]
        L.append("樣本少時別太相信")
        return L


class Engine:
    def __init__(self, levels=None):
        self.levels = {k: v for k, v in (levels or {}).items() if k in STORED and v}
        self.pivot = (levels or {}).get("pivot") or None   # 基準價＝上個交易日 (高+低+收)/3；收盤在它上面算偏多，下面算偏空
        # 每個交易日一筆：夜盤 n_hi/n_lo、日盤 d_hi/d_lo/d_c（收盤）。夜盤算進「隔天」的交易日（昨夜盤 + 今日日盤 = 今日）
        self.days = {k: dict(v) for k, v in ((levels or {}).get("days") or {}).items() if isinstance(v, dict)}
        self.prev = None                                   # 上個交易日 {"day","hi","lo","c"}
        self.manual = {}                                   # 手動指定的價位（SWEEP_LEVELS），refresh 時不被蓋掉
        if self.days:
            self.refresh(G.trade_day(datetime.now(TZ)))
        self.journal = None                                # 掃蕩紀錄（Journal），沒有就不記
        self.events = []                                   # 新形成的轉折高低點：[("low"／"high", 價位, 時間)]，給持倉的「停損上移」提醒用
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
        self.shi, self.slo = [], []       # 這個盤所有的轉折高、轉折低（結構轉換用，不會被掃掉就刪）
        self.seed_key = None              # 機器人重啟時，用期交所給的「本盤到目前的高低」補上重啟前的本盤高低
        self.seed_hi = None
        self.seed_lo = None

    def refresh(self, cur_day):
        """由每個交易日的紀錄算出：上個交易日（前夜盤＋日盤）高低收、今日全天（已結束的夜盤段）高低、基準價。
        cur_day＝目前的交易日（YYYY-MM-DD）。日盤收盤後這一天就算完整，變成「上個交易日」"""
        for k in ("prev_hi", "prev_lo", "td_hi", "td_lo"):
            self.levels.pop(k, None)
        self.prev = None
        done = sorted(d for d, g in self.days.items() if g.get("d_hi") is not None and d <= cur_day)
        if done:
            g = self.days[done[-1]]
            his = [v for v in (g.get("n_hi"), g["d_hi"]) if v is not None]
            los = [v for v in (g.get("n_lo"), g["d_lo"]) if v is not None]
            hi, lo, c = max(his), min(los), g.get("d_c")
            self.prev = {"day": done[-1], "hi": hi, "lo": lo, "c": c}
            self.levels["prev_hi"], self.levels["prev_lo"] = hi, lo
            if c is not None:
                self.pivot = (hi + lo + c) / 3
        g = self.days.get(cur_day)
        if g and g.get("d_hi") is None and g.get("n_hi") is not None and g.get("n_lo") is not None:
            self.levels["td_hi"], self.levels["td_lo"] = g["n_hi"], g["n_lo"]
        self.levels.update(self.manual)

    # 盤結束：把這個盤的高低記進它所屬的交易日（夜盤 → 隔天那個交易日，日盤 → 當天）
    def close_session(self, sess, hi, lo, day=None):
        if hi is None or lo is None:
            return False
        day = day or (self.skey[0] if self.skey else G.trade_day(datetime.now(TZ)))
        g = self.days.setdefault(day, {})
        if sess == "0":
            g["d_hi"], g["d_lo"] = hi, lo
            if self.last_c and self.last_c[0] == "0":
                g["d_c"] = self.last_c[1]
            self.levels.pop("or_hi", None)
            self.levels.pop("or_lo", None)
        else:
            g["n_hi"], g["n_lo"] = hi, lo
        for k in sorted(self.days)[:-KEEP_DAYS]:
            del self.days[k]
        self.refresh(day)
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
    def _tier_line(s):
        if s["tier"] in ("滯漲", "轉折", "破"):
            return {"滯漲": "級別 放量滯漲", "轉折": "級別 結構轉換", "破": "級別 破全日高低"}[s["tier"]]
        return "級別 影線" if s["tier"] == "影線" else f"級別 {s['tier']}價位"

    @staticmethod
    def _name_line(s):
        if s["tier"] == "滯漲":
            return f"{'高檔放量滯漲' if s['dir'] == 'short' else '低檔放量不跌'} {f0(s['level'])}"
        if s["tier"] == "轉折":
            return f"{'高點降低' if s['dir'] == 'short' else '低點墊高'} {f0(s['level'])}"
        if s["tier"] == "破":
            return f"{'創新低追空' if s['dir'] == 'short' else '創新高追多'} {f0(s['level'])}"
        if s["tier"] == "影線":
            return f"{'長上影線高點' if s['dir'] == 'short' else '長下影線低點'} {f0(s['level'])}"
        return f"{NAMES[s['name']]} {f0(s['level'])}" if s["tier"] == "大" else f"盤中波段{'高' if s['dir'] == 'short' else '低'} {f0(s['level'])}"

    def _trend_tag(self, s, up, bar):
        """今日偏多／偏空與順勢逆勢的標籤（同時記到 s["trend"] 給紀錄用）"""
        if self.pivot:                                        # 收盤在前一個日盤的基準價上面＝偏多，下面＝偏空
            trend = bar["c"] - self.pivot
            with_trend = (trend < 0) if up else (trend > 0)
            s["trend"] = "順勢" if with_trend else "逆勢"
            return f"{'偏多' if trend > 0 else '偏空'}（{'順勢' if with_trend else '逆勢'}）"
        trend = bar["c"] - self.sess_open if self.sess_open else 0
        if self.nbars <= 1:
            s["trend"] = "未定"
            return "剛開盤 走勢未定"
        if abs(trend) < 30:
            s["trend"] = "盤整"
            return "今日走勢 盤整"
        with_trend = (trend < 0) if up else (trend > 0)
        s["trend"] = "順勢" if with_trend else "逆勢"
        return f"今日{'偏多' if trend > 0 else '偏空'}（{'順勢' if with_trend else '逆勢'}）"

    def _wick_text(self, s, bar, ratio=None):
        up = s["dir"] == "short"
        L = ["🔻 長上影線，留意做空" if up else "🔺 長下影線，留意做多", "級別 影線", f"影線 {s['pierce']:.0f} 點",
             f"{'高' if up else '低'}點 {f0(s['level'])}", f"收盤 {f0(bar['c'])}"]
        if ratio is not None:
            L.append(f"這根成交量 {ratio:.1f} 倍均量")
        L += [self._trend_tag(s, up, bar), f"停損參考 {f0(s['ext'])} {'上' if up else '下'}", f"接著看 {_cfg('FUT_SWEEP_BARS', 2)} 根 5分K"]
        if self.journal:
            self.journal.opened(s, bar, ratio)
        return L

    def _sweep_text(self, s, bar, ratio=None):
        up = s["dir"] == "short"
        tag = self._trend_tag(s, up, bar)
        sign = "高" if up else "低"
        L = ["🔻 掃上方，留意做空" if up else "🔺 掃下方，留意做多", self._tier_line(s), self._name_line(s)]
        if s["tier"] == "小":
            L.append(f"{s['t'] // 100:02d}:{s['t'] % 100:02d} 形成")
        L += [f"刺破 {s['pierce']:.0f} 點 {sign} {f0(s['ext'])}"]
        if s.get("held"):
            L.append(f"先在價位{'上' if up else '下'}方 {s['held']} 根")
        L.append(f"5分K 收回 {f0(bar['c'])}")
        if ratio is not None:
            L.append(f"這根成交量 {ratio:.1f} 倍均量")
        L += [tag, f"停損參考 {f0(s['ext'])} {'上' if up else '下'}", f"接著看 {_cfg('FUT_SWEEP_BARS', 2)} 根 5分K"]
        if self.journal:
            self.journal.opened(s, bar, ratio)
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
            self.shi.append({"idx": i, "level": b["h"], "t": b["t"]})
            self.events.append(("high", b["h"], b["t"]))
        if b["l"] < min(x["l"] for x in left) and b["l"] <= min(x["l"] for x in right):
            self.swings.append({"dir": "long", "level": b["l"], "idx": i, "t": b["t"], "name": f"sw_lo_{b['t']}"})
            self.slo.append({"idx": i, "level": b["l"], "t": b["t"]})
            self.events.append(("low", b["l"], b["t"]))

    def on_bar(self, bar):
        """一根完成的 5 分K → 回傳要提醒的文字清單（每則是 list of 行）"""
        out = []
        if bar.get("partial"):
            return out
        skey = (bar["day"], bar["sess"])
        self.last_c = (bar["sess"], bar["c"])
        if self.journal:
            self.journal.on_bar(bar)
        if skey != self.skey:
            self.skey, self.sess_open, self.pending, self.or_bars, self.nbars = skey, None, [], [], 0
            self.watch_live, self.trail = [], []
            self.hist, self.swings, self.vols, self.exc = [], [], [], {}
            self.shi, self.slo = [], []
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
            s["near"] = max(s.get("near", bar["h"]), bar["h"]) if up else min(s.get("near", bar["l"]), bar["l"])
            broke = (bar["h"] > s["ext"]) if up else (bar["l"] < s["ext"])
            back = (bar["c"] >= s["level"]) if up else (bar["c"] <= s["level"])
            who = f"（{'做空' if up else '做多'}）"
            if broke or back:
                why = ("又創高 " if up else "又創低 ") + f0(bar["h"] if up else bar["l"]) if broke else \
                      ("收盤又站上 " if up else "收盤又跌破 ") + f0(s["level"])
                out.append([f"❌ 掃蕩失敗{who}", self._tier_line(s), self._name_line(s), why, "這個訊號不用看了"])
                if self.journal:
                    self.journal.failed(s)
            elif s["n"] >= need:
                out.append([f"✅ 掃蕩確認{who}", self._tier_line(s), self._name_line(s),
                            f"連 {need} 根 沒再創{'高' if up else '低'}", f"現價 {f0(bar['c'])}",
                            f"停損參考 {f0(s['ext'])} {'上' if up else '下'}"])
                if self.journal:
                    self.journal.confirmed(s, bar)
                self.trail.append({"dir": s["dir"], "ext": s["ext"], "n": 0})     # 確認後還會再盯一陣子，有更高的點就跟著移
            else:
                keep.append(s)
        self.pending = keep
        # 1b. 已經提醒過的「留意」：又創新高（低）就作廢，不能再說高點降低
        keep_w = []
        for w in self.watch_live:
            w["age"] += 1
            hit = (bar["h"] > w["a"]) if w["up"] else (bar["l"] < w["a"])
            if hit:
                out.append(["👀 留意作廢", f"又{'創高' if w['up'] else '創低'} {f0(bar['h'] if w['up'] else bar['l'])}",
                            f"{'高點' if w['up'] else '低點'} {f0(w['a'])} 已被突破", "這個形態不成立"])
            elif w["age"] < 12:
                keep_w.append(w)
        self.watch_live = keep_w
        # 1c. 掃蕩確認後又出現更高（低）的點：停損參考跟著移，提醒一次
        keep_t = []
        for t in self.trail:
            t["n"] += 1
            up_t = t["dir"] == "short"
            hit = (bar["h"] > t["ext"]) if up_t else (bar["l"] < t["ext"])
            if hit:
                newe = bar["h"] if up_t else bar["l"]
                out.append([f"⚠️ 掃蕩確認後又{'創高' if up_t else '創低'}（{'做空' if up_t else '做多'}）",
                            f"新{'高' if up_t else '低'} {f0(newe)}", f"停損參考 改放 {f0(newe)} {'上' if up_t else '下'}",
                            f"原來 {f0(t['ext'])}"])
                t["ext"] = newe
            if t["n"] < 6:
                keep_t.append(t)
        self.trail = keep_t
        # 2. 盤中波段高低：先更新，再看這根有沒有把它們掃掉
        self.hist.append(bar)
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
        if _cfg("FUT_WICK_ON", True) and not last_bar:                                # 長影線：不需要任何價位，只看這根 K 棒本身
            rng = bar["h"] - bar["l"]
            prior = self.hist[-21:-1]
            avg = (sum(b["h"] - b["l"] for b in prior) / len(prior)) if len(prior) >= 6 else 0
            need_pts = max(_cfg("FUT_WICK_MIN", 60), _cfg("FUT_WICK_MULT", 2.5) * avg)      # 至少 60 點，而且要是平常一根 K 棒的 2.5 倍以上
            fresh = {p["dir"] for p in self.pending if p["n"] == 0}                        # 這根已經因為掃蕩提醒過的方向，不重複
            for d, wick in (("long", min(bar["o"], bar["c"]) - bar["l"]), ("short", bar["h"] - max(bar["o"], bar["c"]))):
                if rng <= 0 or wick < need_pts or wick < _cfg("FUT_WICK_PCT", 0.6) * rng:
                    continue
                if d in fresh or any(p["tier"] == "影線" and p["dir"] == d for p in self.pending):
                    continue
                lvl = bar["l"] if d == "long" else bar["h"]
                w = {"dir": d, "name": f"wick_{bar['t']}", "level": lvl, "ext": lvl, "pierce": wick, "n": 0, "tier": "影線", "t": bar["t"]}
                self.pending.append(w)
                out.append(self._wick_text(w, bar, ratio))
        if _cfg("FUT_PAT_ON", True) and not last_bar:
            out += self._patterns(bar, skey, ratio)
        if bar.get("v"):
            self.vols.append(bar["v"])
        return out

    def _pat_card(self, s, bar, lines):
        """型態提醒：貼上走勢標籤與停損，並直接記進掃蕩紀錄（進場價＝這根收盤，停損＝停損參考）"""
        up = s["dir"] == "short"
        tag = self._trend_tag(s, up, bar)
        L = lines + [tag, f"停損參考 {f0(s['ext'])} {'上' if up else '下'}"]
        if self.journal:
            self.journal.opened(s, bar, None)
            self.journal.confirmed(s, bar)
        return L

    def _patterns(self, bar, skey, ratio):
        """高檔放量滯漲／低檔放量不跌、結構轉換（高點降低＋收盤跌破頸線／低點墊高＋收盤站上頸線）。都只是提醒，不下單"""
        out = []
        hi, lo = max(b["h"] for b in self.hist), min(b["l"] for b in self.hist)
        if self.seed_key == skey:                                   # 重啟後補上重啟前的本盤高低
            if self.seed_hi is not None:
                hi = max(hi, self.seed_hi)
            if self.seed_lo is not None:
                lo = min(lo, self.seed_lo)
        cool = _cfg("FUT_PAT_COOL", 6)
        last = self.__dict__.setdefault("_pat_last", {})
        ready = lambda key: self.nbars - last.get(key, -999) >= cool
        rng = bar["h"] - bar["l"]
        # ① 放量滯漲／不跌：量大，但實體小、收在區間的一側，而且在本盤高（低）附近
        if _cfg("FUT_STALL_ON", True) and rng > 0 and ratio is not None and ratio >= _cfg("FUT_STALL_VOL", 2.0) \
                and abs(bar["c"] - bar["o"]) <= _cfg("FUT_STALL_BODY", 0.4) * rng:
            near, mid = _cfg("FUT_STALL_NEAR", 40), bar["l"] + rng / 2
            for d in ("short", "long"):
                up = d == "short"
                if not ((bar["c"] <= mid and bar["h"] >= hi - near) if up else (bar["c"] >= mid and bar["l"] <= lo + near)):
                    continue
                if not ready(("stall", d)):
                    continue
                last[("stall", d)] = self.nbars
                lv = bar["h"] if up else bar["l"]
                s = {"dir": d, "name": f"stall_{bar['t']}", "level": lv, "ext": lv, "pierce": 0, "n": 0, "tier": "滯漲", "t": bar["t"]}
                out.append(self._pat_card(s, bar, ["🔻 高檔放量滯漲" if up else "🔺 低檔放量不跌", "留意轉空（觀察）" if up else "留意轉多（觀察）",
                                                   f"這根量 {ratio:.1f} 倍均量", f"{'高' if up else '低'}點 {f0(lv)}",
                                                   f"收盤 {f0(bar['c'])}（收在{'下' if up else '上'}半）"]))
        # ② 結構轉換：最近兩個轉折高（低）一個比一個低（高），收盤跌破（站上）它們之間的頸線
        if _cfg("FUT_STRUCT_ON", True):
            step, near, age = _cfg("FUT_STRUCT_LH", 15), _cfg("FUT_STRUCT_NEAR", 50), _cfg("FUT_STRUCT_AGE", 24)
            done = self.__dict__.setdefault("_struct_done", set())
            cur = len(self.hist) - 1
            for d, pts_ in (("short", self.shi), ("long", self.slo)):
                if len(pts_) < 2:
                    continue
                up = d == "short"
                a, b = pts_[-2], pts_[-1]
                key = (skey, d, a["idx"], b["idx"])
                if key in done or cur <= b["idx"] or cur - b["idx"] > age:
                    continue
                if up and not (b["level"] <= a["level"] - step and a["level"] >= hi - near):
                    continue
                if (not up) and not (b["level"] >= a["level"] + step and a["level"] <= lo + near):
                    continue
                seg = self.hist[a["idx"]:b["idx"] + 1]
                neck = min(x["l"] for x in seg) if up else max(x["h"] for x in seg)
                if not ((bar["c"] < neck) if up else (bar["c"] > neck)):
                    # 還沒跌破（站上）頸線：高點降低已經成立，先提醒「留意」，不用等頸線
                    wkey = ("watch",) + key
                    if _cfg("FUT_STRUCT_WATCH", True) and wkey not in done and ready(("watch", d)):
                        done.add(wkey)
                        last[("watch", d)] = self.nbars
                        self.watch_live.append({"up": up, "a": a["level"], "age": 0})
                        out.append(["👀 高點降低（留意轉空）" if up else "👀 低點墊高（留意轉多）", "級別 結構轉換（未確認）",
                                    f"{'高點降低' if up else '低點墊高'} {f0(a['level'])}→{f0(b['level'])}",
                                    f"頸線 {f0(neck)}",
                                    f"收盤{'跌破' if up else '站上'}才確認",
                                    f"{'站回' if up else '跌破'} {f0(a['level'])} {'上' if up else '下'}就失效",
                                    f"現價 {f0(bar['c'])}"])
                    continue
                done.add(key)
                if not ready(("struct", d)):
                    continue
                last[("struct", d)] = self.nbars
                s = {"dir": d, "name": f"struct_{bar['t']}", "level": b["level"], "ext": b["level"], "pierce": 0, "n": 0, "tier": "轉折", "t": bar["t"]}
                out.append(self._pat_card(s, bar, ["🔻 結構轉換（轉空）" if up else "🔺 結構轉換（轉多）", "級別 結構轉換",
                                                   f"{'高點降低' if up else '低點墊高'} {f0(a['level'])}→{f0(b['level'])}",
                                                   f"收盤{'跌破' if up else '站上'} {f0(neck)}", f"收盤 {f0(bar['c'])}"] ) + ["建議 1 口小試"])
        # ③ 創新低追空／創新高追多：收盤跌破（站上）「全日（含夜盤）」的低（高）點。只記進掃蕩紀錄，不推播
        if _cfg("FUT_BREAK_ON", True) and self.journal:
            self._break_log(bar, skey, ready, last)
        return out

    def _break_log(self, bar, skey, ready, last):
        """全日低點＝夜盤已結束的那段（td_lo）＋本盤到上一根為止（含重啟補上的）。低點要形成至少 FUT_BREAK_AGE 根（預設 6＝30 分鐘）才算「站穩的低點」，
        不然只是一路下殺中的連續創低。收盤跌破它 FUT_BREAK_MIN 點以上就記一筆：進場價＝這根收盤；
        停損遠＝最近 FUT_BREAK_FAR 根（預設 12）的最高點，近＝最近 FUT_BREAK_NEAR 根（預設 3）的最高點"""
        prev, cur_i = self.hist[:-1], len(self.hist) - 1
        age, mn = _cfg("FUT_BREAK_AGE", 6), _cfg("FUT_BREAK_MIN", 3)
        done = self.__dict__.setdefault("_brk_done", set())
        for d in ("short", "long"):
            dn = d == "short"                                   # short＝跌破低點追空；long＝站上高點追多
            cands = []                                          # (價位, 是不是已經站穩)
            pick = (lambda b: b["l"]) if dn else (lambda b: b["h"])
            better = min if dn else max
            if prev:
                i = better(range(len(prev)), key=lambda k: (pick(prev[k]), k if dn else -k))
                cands.append((pick(prev[i]), cur_i - i >= age))
            td = self.levels.get("td_lo" if dn else "td_hi")
            if td is not None:
                cands.append((td, True))
            if self.seed_key == skey:
                sd = self.seed_lo if dn else self.seed_hi
                if sd is not None:
                    cands.append((sd, True))
            if not cands:
                continue
            lvl = better(c[0] for c in cands)
            if not any(c[1] for c in cands if c[0] == lvl):     # 全日低（高）是剛剛才創的：連續下殺，不算站穩後被跌破
                continue
            if not ((bar["c"] <= lvl - mn) if dn else (bar["c"] >= lvl + mn)):
                continue
            key = (skey, d, lvl)
            if key in done or not ready(("brk", d)):
                continue
            done.add(key)
            last[("brk", d)] = self.nbars
            far_n, near_n = _cfg("FUT_BREAK_FAR", 12), _cfg("FUT_BREAK_NEAR", 3)
            seg = self.hist[-far_n:]
            ext = max(b["h"] for b in seg) if dn else min(b["l"] for b in seg)
            nseg = self.hist[-near_n:]
            near = max(b["h"] for b in nseg) if dn else min(b["l"] for b in nseg)
            s = {"dir": d, "name": f"brk_{bar['t']}", "level": lvl, "ext": ext, "near": near, "pierce": abs(bar["c"] - lvl),
                 "n": 0, "tier": "破", "t": bar["t"]}
            self._trend_tag(s, dn, bar)
            self.journal.opened(s, bar, None)
            self.journal.confirmed(s, bar)


# ───────────── 報價 ─────────────
def quote_full(now=None, mt=None):
    """近月台指期：{last, high, low, vol}（高低、累計成交量都是這個盤到目前為止）；沒開盤回 None。mt 可指定盤別"""
    now = now or datetime.now(TZ)
    mt = mt or G.session_of(now)
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
def quote_official(now=None):
    """日盤收盤後（13:45～13:55）：再用日盤代碼抓一次，取期交所的收盤價；抓不到回 None"""
    now = now or datetime.now(TZ)
    q = quote_full(now, mt="0")
    return q["last"] if q and q.get("last") is not None else None


def load_levels(now=None):
    now = now or datetime.now(TZ)
    try:
        with open(LEVEL_FILE, encoding="utf-8") as f:
            j = json.load(f)
        if now - datetime.fromisoformat(j["asof"]) < timedelta(days=4):
            return {k: v for k, v in j["levels"].items() if k in STORED or k in ("pivot", "days")}
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
    """機器人剛啟動、沒有存檔時：從期交所最近幾天的逐筆檔，算出最近幾個完整的日盤／夜盤高低，
    回傳 {"days": {交易日: {n_hi, n_lo（夜盤）, d_hi, d_lo, d_c（日盤，含收盤）}}}。夜盤算進隔天的交易日。
    一行一行讀、不把整個檔案放進記憶體（Render 只有 512MB）"""
    seg = {}                                      # {(盤別, 起算日, 到期月): [高, 低]}
    lastt, lastp = {}, {}
    tail = {}                                     # {(起算日, 到期月): 夜盤跨午夜之後最晚的成交時間}，要到 04:30 以後才算夜盤資料齊了
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
                    tail[(k[1], k[2])] = max(tail.get((k[1], k[2]), 0), t)
                else:
                    continue
                v = seg.get(k)
                if v is None:
                    seg[k] = [px, px]
                else:
                    v[0], v[1] = max(v[0], px), min(v[1], px)
    best = {}                                     # {(盤別, 起算日): (到期月, 高, 低)}，只收已經結束、資料齊的盤
    for (sk, day, exp), (hi, lo) in seg.items():
        end = datetime(day.year, day.month, day.day, 13, 45, tzinfo=TZ) if sk == "0" else datetime(day.year, day.month, day.day, 5, 0, tzinfo=TZ) + timedelta(days=1)
        if end > now:
            continue
        if sk == "1" and tail.get((day, exp), 0) < 43000:           # 夜盤後半段的檔案還沒出來：不是完整的夜盤，不用
            continue
        if (sk, day) not in best or exp < best[(sk, day)][0]:      # 同一天有兩個到期月：用最近月
            best[(sk, day)] = (exp, hi, lo)
    days = {}
    for (sk, day), (exp, hi, lo) in best.items():
        if sk == "0":                                              # 日盤 → 當天
            rec = days.setdefault(day.isoformat(), {})
            rec["d_hi"], rec["d_lo"] = hi, lo
            if ("0", day, exp) in lastp:
                rec["d_c"] = lastp[("0", day, exp)]                # 日盤最後一筆成交價＝收盤價
        else:                                                      # 夜盤 → 隔天的交易日
            rec = days.setdefault(G.trade_day(datetime(day.year, day.month, day.day, 15, 0, tzinfo=TZ)), {})
            rec["n_hi"], rec["n_lo"] = hi, lo
    return {"days": days} if days else {}


# ───────────── 背景執行 ─────────────
def parse_manual(txt):
    """'day_hi=44250,day_lo=42521' → {'day_hi': 44250.0, 'day_lo': 42521.0}；格式不對的略過"""
    out = {}
    for part in (txt or "").replace("，", ",").split(","):
        k, _, v = part.partition("=")
        k = k.strip()
        k = ALIAS.get(k, k)
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

    def __init__(self, send, quote=quote_full, seed=seed_levels, official=quote_official):
        self.send, self.quote, self.seed, self.official = send, quote, seed, official
        self._off_day = None                                    # 日盤剛收盤、還在等官方收盤價的交易日
        self.bb = BarBuilder()
        self.eng = Engine(load_levels())
        self.eng.pivot = self.eng.pivot or None
        if _cfg("FUT_LOG_ON", True):
            self.eng.journal = Journal()
        self.seeded = False
        self.hello = False
        self.errs = 0
        self.last_q = None
        self.last_at = None

    def _save(self, now):
        save_levels({**self.eng.levels, "days": self.eng.days, **({"pivot": self.eng.pivot} if self.eng.pivot else {})}, now)

    def level_lines(self, now=None):
        """目前所有價位，給「期貨→行情」和開機卡片用。照時間分組、組與組之間空一行"""
        now = now or datetime.now(TZ)
        sess = G.session_of(now)
        lv = self.eng.levels
        G_ = []                                                  # 每一組是一個 list
        if self.last_q and self.last_q.get("last") is not None and sess is not None:
            G_.append([f"現價 {f0(self.last_q['last'])}"])
        if sess is not None and self.bb.hi is not None and self.bb.lo is not None:
            hi, lo, title = self.bb.hi, self.bb.lo, "今夜盤"
            if sess == "0":                                       # 日盤：今日全天＝昨夜盤＋今日日盤到目前
                title = "今日日盤"
                if "td_hi" in lv and "td_lo" in lv:
                    hi, lo, title = max(hi, lv["td_hi"]), min(lo, lv["td_lo"]), "今日全天"
            G_.append([title, f"高 {f0(hi)}", f"低 {f0(lo)}"])
        if "or_hi" in lv and "or_lo" in lv:
            G_.append(["開盤30分", f"高 {f0(lv['or_hi'])}", f"低 {f0(lv['or_lo'])}"])
        pv = self.eng.prev
        if pv:                                                    # 上個交易日＝前夜盤＋日盤，含收盤
            g = ["上個交易日", f"高 {f0(pv['hi'])}", f"低 {f0(pv['lo'])}"]
            if pv.get("c") is not None:
                g.append(f"收 {f0(pv['c'])}")
            G_.append(g)
        if self.eng.pivot:
            G_.append([f"多空分界 {f0(self.eng.pivot)}", "（高低收 平均）"])
        if sess is None:
            G_.insert(0, ["目前沒開盤"])
        L = []
        for g in G_:
            if L:
                L.append("")
            L += g
        return L

    def ensure_levels(self, now):
        if self.seeded:
            return
        self.seeded = True
        manual = parse_manual(os.getenv("SWEEP_LEVELS", ""))
        if manual:                                  # 手動指定的價位（例：期交所檔案還沒出來時用）
            self.eng.manual = manual
            self.eng.levels.update(manual)
            print("掃蕩關鍵價位（手動）：", manual, flush=True)
        if "prev_hi" not in self.eng.levels or not self.eng.pivot:
            try:
                got = self.seed(now) or {}
                for day, rec in (got.get("days") or {}).items():     # 已經記錄的欄位不蓋掉，只補沒有的
                    cur = self.eng.days.setdefault(day, {})
                    for k, v in rec.items():
                        cur.setdefault(k, v)
                if got.get("days"):
                    self.eng.refresh(G.trade_day(now))
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
            self._official_close(now)
            fl = self.bb.flush()
            if fl:
                bars, sess, hi, lo = fl
                for b in bars:
                    for lines in self.eng.on_bar(b):
                        self.send(lines)
                        n += 1
                self.eng.events = []
                if self.eng.close_session(sess, hi, lo):
                    self._save(now)
                    if sess == "0":
                        self._off_day = G.trade_day(now)            # 等官方收盤價
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
        try:
            n += self._handle_events() + self._near_check(now, q) + self._hl_check(now, q)
        except Exception as e:
            print("持倉提醒失敗：", repr(e)[:100], flush=True)
        return n

    def _official_close(self, now):
        """日盤收盤後 13:45～13:55 每 30 秒問一次期交所官方收盤價；拿到就取代最後成交價，記成上個交易日的收盤"""
        day = self._off_day
        if not day or not _cfg("FUT_CLOSE_OFFICIAL", True):
            return
        hm = (now.hour, now.minute)
        if hm > (13, 55):
            self._off_day = None
            return
        if hm < (13, 45):
            return
        try:
            px = self.official(now)
        except Exception as e:
            print("官方收盤價抓取失敗：", repr(e)[:100], flush=True)
            return
        if px is None:
            return
        self._off_day = None
        g = self.eng.days.setdefault(day, {})
        last = g.get("d_c")
        g["d_c"] = px
        self.eng.refresh(day)
        self._save(now)
        if last is not None and round(last) != round(px):
            self.send(["🧾 收盤價校正", f"最後成交 {f0(last)}", f"官方收盤 {f0(px)}",
                       f"上個交易日收盤已改成 {f0(px)}", f"多空分界 {f0(self.eng.pivot)}"])
        print("收盤價：最後成交", last, "官方", px, flush=True)

    @staticmethod
    @staticmethod
    def _positions():
        try:
            return G.get_state().get("positions") or []
        except Exception as e:
            print("讀取期貨部位失敗：", repr(e)[:80], flush=True)
            return []

    def _handle_events(self):
        """新的轉折低點（做多）／轉折高點（做空）出現：如果比目前停損更有利，提醒你要不要把停損上移（下移），由你決定"""
        evs, self.eng.events = self.eng.events, []
        if not evs or not _cfg("FUT_SWING_ALERT_ON", True):
            return 0
        pos = self._positions()
        if not pos:
            return 0
        price = (self.last_q or {}).get("last")
        n = 0
        for kind, level, t in evs:
            for p in pos:
                stop = p.get("stop")
                if kind == "low" and p["side"] == "多":
                    ok = (stop is None or level > stop) and (price is None or level < price)
                elif kind == "high" and p["side"] == "空":
                    ok = (stop is None or level < stop) and (price is None or level > price)
                else:
                    continue
                if not ok:
                    continue
                up = kind == "low"
                key = (p["kind"], p["side"], round(p["avg"]))
                last = self.__dict__.setdefault("_swing_last", {}).get(key)
                if stop is None:
                    if last is not None:
                        continue                                  # 沒設停損：已經提醒過一次，不再每個轉折都跳
                else:
                    ref = stop if last is None else (max(stop, last) if up else min(stop, last))
                    if (level - ref if up else ref - level) < _cfg("FUT_SWING_MIN_STEP", 30):
                        continue                                  # 比上次提醒（或目前停損）改善不到 N 點：不吵你
                self._swing_last[key] = level
                self.send([f"📍 新轉折{'低' if up else '高'}點", f"{p['side']} {p['kind']} {p['qty']}口", f"{t // 100:02d}:{t % 100:02d} 形成",
                           f"轉折{'低' if up else '高'}點 {f0(level)}", f"目前停損 {f0(stop) if stop is not None else '未設'}",
                           f"可考慮把停損{'上' if up else '下'}移", "按「期貨→停利/停損」改"])
                n += 1
        return n

    def _near_check(self, now, q):
        """有部位時：價格接近前高（做多）／前低（做空）FUT_NEAR_LEVEL 點內，提醒你考慮減碼或停利；每個價位只提醒一次"""
        if not _cfg("FUT_NEAR_ALERT_ON", True) or not q or q.get("last") is None:
            return 0
        pos = self._positions()
        price, near = q["last"], _cfg("FUT_NEAR_LEVEL", 30)
        sess, day = G.session_of(now), G.trade_day(now)
        cands = {"多": [(NAMES[k], v) for k, v in self.eng.levels.items() if k.endswith("_hi")],
                 "空": [(NAMES[k], v) for k, v in self.eng.levels.items() if k.endswith("_lo")]}
        hi_, lo_ = q.get("high"), q.get("low")                       # 本盤高低用期交所報價的（機器人重啟後，自己組的 5 分K 只有重啟之後的，會漏掉更高的高點）
        if (hi_ is None or lo_ is None) and self.eng.hist:
            hi_, lo_ = max(b["h"] for b in self.eng.hist), min(b["l"] for b in self.eng.hist)
        if hi_ is not None:
            cands["多"].append(("本盤前高", hi_))
        if lo_ is not None:
            cands["空"].append(("本盤前低", lo_))
        edge = _cfg("FUT_NEAR_MIN", 3)                               # 離本盤高低不到 3 點＝就在高低點上，那是「創高／創低」，不算接近
        sent = self.__dict__.setdefault("_near_sent", set())
        cool = self.__dict__.setdefault("_near_ts", {})
        n = 0
        if not pos:                                                  # 空手：提醒價格靠近前高前低，讓你留意建倉機會
            if not _cfg("FUT_FLAT_NEAR_ON", True):
                return 0
            ts = now.timestamp()
            if ts - getattr(self, "_flat_ts", 0) < _cfg("FUT_FLAT_COOL", 600):   # 冷卻：避免趨勢盤連發
                return 0
            pv = getattr(self.eng, "pivot", None)
            for side in ("多", "空"):
                for name, level in cands[side]:
                    hi = side == "多"
                    gap = (level - price) if hi else (price - level)
                    key = ("flat", day, sess, name) if name.startswith("本盤") else ("flat", day, sess, name, level)
                    if (edge if name.startswith("本盤") else 0) <= gap <= near and key not in sent:
                        sent.add(key)
                        self._flat_ts = ts
                        L = [f"📍 空手 接近{'前高' if hi else '前低'}", f"{name} {f0(level)}", f"現價 {f0(price)}", f"還差 {gap:.0f} 點"]
                        if pv:
                            L.append(f"多空分界 {f0(pv)}（{'偏多' if price > pv else '偏空'}）")
                        L.append("留意突破或被壓回" if hi else "留意跌破或撐住")
                        self.send(L)
                        return 1
            return 0
        for p in pos:
            for name, level in cands.get(p["side"], []):
                gap = (level - price) if p["side"] == "多" else (price - level)
                live = name.startswith("本盤")                      # 本盤高低會一直被刷新：不要每換一個價位就再提醒
                key = (day, sess, p["side"], p["kind"], name) if live else (day, sess, p["side"], p["kind"], name, level)
                ck = (p["side"], p["kind"], name)
                if live and now.timestamp() - cool.get(ck, 0) < _cfg("FUT_NEAR_COOL", 1800):
                    continue
                if (edge if live else 0) <= gap <= near and key not in sent:
                    sent.add(key)
                    cool[ck] = now.timestamp()
                    hi = p["side"] == "多"
                    self.send([f"📍 接近{'前高' if hi else '前低'}", f"{p['side']} {p['kind']} {p['qty']}口", f"{name} {f0(level)}",
                               f"現價 {f0(price)}", f"還差 {gap:.0f} 點", "可考慮減碼或停利"])
                    n += 1
        return n

    def _hl_check(self, now, q):
        """創本盤新高／新低：不管有沒有部位都提醒（空手也是在等進場）。
        開盤前 FUT_HL_WARM 分鐘只記錄；之後第一次突破馬上提醒，接著要再創高（低）FUT_HL_STEP 點以上才再提醒，
        並註明這次突破了哪些關鍵價位（上個交易日高、今日全天高、開盤30分高…）"""
        if not _cfg("FUT_HL_ON", True) or not q or q.get("high") is None or q.get("low") is None:
            return 0
        sess = G.session_of(now)
        if sess is None:
            return 0
        key = (G.trade_day(now), sess)
        hl = self.__dict__.setdefault("_hl", {})
        if key not in hl:
            hl.clear()
            start = now.replace(hour=8, minute=45, second=0, microsecond=0) if sess == "0" else now.replace(hour=15, minute=0, second=0, microsecond=0)
            if sess == "1" and now.hour < 15:
                start -= timedelta(days=1)
            hl[key] = {"start": start, "hi": q["high"], "lo": q["low"], "ready": False, "ah": None, "al": None}
        h = hl[key]
        if not h["ready"]:                                      # 暖機：只記錄，不提醒
            h["hi"], h["lo"] = q["high"], q["low"]
            if (now - h["start"]).total_seconds() >= _cfg("FUT_HL_WARM", 30) * 60:
                h["ready"] = True
            return 0
        step, n = _cfg("FUT_HL_STEP", 30), 0
        price = q.get("last")
        for up in (True, False):
            cur = q["high"] if up else q["low"]
            ref = h["ah" if up else "al"]
            if ref is None:
                ref = h["hi" if up else "lo"]
                trig = cur > ref if up else cur < ref
            else:
                trig = (cur - ref >= step) if up else (ref - cur >= step)
            if not trig:
                continue
            names = [NAMES[k] + " " + f0(v) for k, v in self.eng.levels.items()
                     if k in (("prev_hi", "td_hi", "or_hi") if up else ("prev_lo", "td_lo", "or_lo")) and ((ref < v <= cur) if up else (cur <= v < ref))]
            L = [("🔺 創本盤新高" if up else "🔻 創本盤新低") if sess == "0" else ("🔺 創夜盤新高" if up else "🔻 創夜盤新低"),
                 f"{'新高' if up else '新低'} {f0(cur)}", f"{'上一個高' if up else '上一個低'} {f0(ref)}"]
            if price is not None:
                L.append(f"現價 {f0(price)}")
            for nm in names:
                L.append(f"突破 {nm}" if up else f"跌破 {nm}")
            self.send(L)
            h["ah" if up else "al"] = cur
            n += 1
        return n

    def _digest_check(self, now=None):
        """每分鐘最多看一次：週報／累積筆數到了就私訊統整"""
        j = self.eng.journal
        if j is None:
            return
        now = now or datetime.now(TZ)
        ts = now.timestamp()
        if ts - getattr(self, "_dg_ts", 0) < 60:
            return
        self._dg_ts = ts
        try:
            d = j.digest(now)
            if d:
                self.send([d[0]] + d[1])
        except Exception as e:
            print("掃蕩統整失敗：", repr(e)[:100], flush=True)

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
            self._digest_check()
            time.sleep(_cfg("FUT_SWEEP_POLL", 5) if r != -1 else 30)


RUNNER = None


def journal_lines():
    """「期貨→掃蕩紀錄」：機器人在跑就用它記憶體裡的，不然讀存檔"""
    j = RUNNER.eng.journal if (RUNNER is not None and RUNNER.eng.journal) else Journal()
    return j.lines()


def status_lines():
    """「期貨→行情」：現價、今盤高低、各個關鍵價位"""
    if RUNNER is None:
        return ["掃蕩提醒還沒啟動"]
    now = datetime.now(TZ)
    return [f"📍 台指期行情 {now:%H:%M}"] + (RUNNER.level_lines(now) or ["還沒有價位資料"])


def loop(send):
    """send(lines)：把一組短行做成字卡私訊管理者"""
    global RUNNER
    RUNNER = Runner(send)
    RUNNER.run()
