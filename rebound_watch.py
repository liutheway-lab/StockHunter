"""rebound_watch.py — 盤中「瞬間殺低後反彈」偵測（跑在 Render 機器人上，交易時間內每 90 秒掃一次全市場）

重點不是「今天跌多少」，而是「短時間內瞬間殺下去，然後又快速拉回來」。
做法：每檔股票記住今天盤中的成交價快照（每 90 秒一輪），條件：
  - 殺低要快：5 分鐘內從高點跌下來至少 DIP_MIN（4%），而且殺低段要出量
  - 殺低的低點之後，現價反彈 ≥ REBOUND_MIN（4%），整個盤中都算
  - 一直往上漲的股票沒有殺低，不會進來
排序：反彈幅度大的先推；每次最多推 TOP_N 檔；同一檔冷卻 COOLDOWN_SEC 秒才會再推。
資料來源：證交所 MIS 盤中即時報價（公開、免費、不用金鑰）；股票清單來自 data.get_directory()。
推播只私訊管理者（TG_OWNER_ID）。
"""
import datetime as dt
import os
import time
from collections import deque

import requests

import config as cfg
import data as D

TPE = dt.timezone(dt.timedelta(hours=8))
DIP_SEC = 300             # 殺低要快：高點到低點在 5 分鐘內
WINDOW_SEC = 6 * 3600     # 整個盤中都看：殺低後不管過多久，只要出現符合的樣態就提醒
DIP_MIN = 0.04            # 殺低：從視窗內前面的高點跌下來至少 4%
REBOUND_MIN = 0.04        # 殺低低點之後反彈 ≥ 4%
MIN_POINTS = 3            # 視窗內至少要有 3 個成交價（不然不算）
LIQ_LOTS = cfg.MIN_LOTS   # 今天到目前累計成交量至少 1000 張（太冷門的不推）
VOL_MULT = 2.0            # 殺低段的量：每分鐘量 ≥ 今天到目前平均每分鐘量的 2 倍（下殺要出量）
COOLDOWN_SEC = 1800       # 同一檔 30 分鐘內不重複推
TOP_N = 10                # 每次最多推幾檔
BATCH = 40
OPEN_HM, CLOSE_HM = (9, 0), (13, 30)

_hist = {}                # 代號 → deque[(時間戳, 成交價, 累計張數, 昨收)]（名稱另存 _names，省記憶體）
_names = {}               # 代號 → 名稱（每檔只存一次）
_last_push = {}           # 代號 → 上次推播時間戳
_day = {"d": None}


def _num(v):
    try:
        f = float(str(v).split("_")[0])
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _cum_vol(v):
    """MIS 的 v 是今天累計成交量（張）；讀不到就回傳 None"""
    try:
        f = float(str(v).split("_")[0])
        return f if f >= 0 else None
    except (TypeError, ValueError):
        return None


def record(q, ts):
    """把一筆 MIS 資料的成交價與累計量記進歷史；沒有成交價（-、盤前）就略過"""
    z = _num(q.get("z"))
    code = str(q.get("c", "")).strip()
    if not (z and code):
        return
    if code not in _names:
        _names[code] = str(q.get("n", ""))
    h = _hist.setdefault(code, deque(maxlen=1000))
    h.append((ts, z, _cum_vol(q.get("v")), _num(q.get("y"))))


def check(code, ts):
    """殺低要快（高點到低點 ≤ DIP_SEC）、殺低段要出量，之後現價反彈 ≥ REBOUND_MIN（最多看 WINDOW_SEC 內）。
    昨收當作開盤前的高點，所以「開盤直接跌停再拉回」也抓得到。
    符合回傳 (代號, 名稱, 殺低幅度, 反彈幅度, 現價, 今日累計張數)，否則 None"""
    h = _hist.get(code)
    if not h:
        return None
    full = list(h)
    y = next((row[3] for row in full if row[3]), None)
    if y:                                            # 昨收當作開盤時的高點（成交量 0）
        full = [(_open_ts(ts), y, 0.0, None)] + full
    pts = [(t, p, v) for t, p, v, _ in full if ts - t <= WINDOW_SEC]
    if len(pts) < MIN_POINTS or pts[-1][2] is None:
        return None
    ts_, prices, vols = [t for t, *_ in pts], [p for _, p, _ in pts], [v for *_, v in pts]
    cur = prices[-1]
    if vols[-1] < LIQ_LOTS:                          # 太冷門，不推
        return None
    # 今天到目前的平均每分鐘量（張/分）
    mins_today = max((ts - _open_ts(ts)) / 60, 1.0)
    base_rate = vols[-1] / mins_today
    best = None
    for j in range(1, len(prices) - 1):              # j = 殺低後的低點，必須在現價之前
        cands = [k for k in range(j) if ts_[j] - ts_[k] <= DIP_SEC]
        if not cands:
            continue
        hi = max(cands, key=lambda k: prices[k])     # 殺低前的高點
        dip = prices[j] / prices[hi] - 1
        reb = cur / prices[j] - 1
        if dip > -DIP_MIN or reb < REBOUND_MIN:
            continue
        dip_vol = vols[j] - vols[hi]                 # 高點到低點這段成交量
        dip_min = max((ts_[j] - ts_[hi]) / 60, 1.0)
        if dip_vol / dip_min < VOL_MULT * base_rate:  # 殺低沒出量，不算
            continue
        if best is None or reb > best[3]:
            best = (code, _names.get(code, ""), dip, reb, cur, vols[-1])
    return best


def _open_ts(ts):
    """ts 當天 09:00（台北）的時間戳"""
    d = dt.datetime.fromtimestamp(ts, TPE)
    return d.replace(hour=OPEN_HM[0], minute=OPEN_HM[1], second=0, microsecond=0).timestamp()


def scan(universe_rows, sess=None, ts=None):
    """掃全市場一次，回傳符合條件的清單（已依反彈幅度排序，還沒套用冷卻）"""
    ts = ts or time.time()
    if sess is None:
        sess = requests.Session()
        try:
            sess.get("https://mis.twse.com.tw/stock/index.jsp", headers=D._MIS_HEAD, timeout=5)
        except Exception:
            pass
    exs = [("tse_" if r["suffix"] == ".TW" else "otc_") + f"{r['code']}.tw" for r in universe_rows]
    codes = []
    for k in range(0, len(exs), BATCH):
        try:
            for q in fetch_batch(sess, exs[k:k + BATCH]):
                record(q, ts)
                codes.append(str(q.get("c", "")).strip())
        except Exception as e:
            print("盤中反彈：批次失敗", repr(e)[:80])
        time.sleep(0.2)
    hits = [h for h in (check(c, ts) for c in set(codes)) if h]
    return sorted(hits, key=lambda h: -h[3])                   # 反彈幅度大的先


def fetch_batch(sess, exs):
    r = sess.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                 params={"ex_ch": "|".join(exs), "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                 headers=D._MIS_HEAD, timeout=10)
    return (r.json() or {}).get("msgArray") or []


def new_hits(hits, ts=None):
    """冷卻期內推過的不再推；最多 TOP_N 檔；推了的記下時間"""
    ts = ts or time.time()
    out = [h for h in hits if ts - _last_push.get(h[0], -1e18) >= COOLDOWN_SEC][:TOP_N]
    for h in out:
        _last_push[h[0]] = ts
    return out


def message(hs):
    now = dt.datetime.now(TPE)
    lines = [
        "⚡ 盤中殺低後反彈",
        f"🕐 {now:%H:%M}　｜　共 {len(hs)} 檔",
        "━━━━━━━━━━━━━━",
    ]
    for code, name, drop, reb, z, lots in hs:
        lines += [
            f"🟢 {code} {name}",
            f"　📉 殺低 {drop * 100:.1f}%　📈 反彈 +{reb * 100:.1f}%",
            f"　💰 現價 {z:g}　📊 今日量 {lots:,.0f} 張",
            "",
        ]
    lines += [
        "━━━━━━━━━━━━━━",
        "⚠️ 條件符合，不代表可以買",
    ]
    return "\n".join(lines)


def in_session(now=None):
    now = now or dt.datetime.now(TPE)
    if now.weekday() >= 5:
        return False
    return OPEN_HM <= (now.hour, now.minute) <= CLOSE_HM


def _reset_if_new_day():
    today = dt.datetime.now(TPE).date().isoformat()
    if _day["d"] != today:
        _day["d"] = today
        _hist.clear()
        _last_push.clear()


def send_owner(text):
    owner = os.getenv("TG_OWNER_ID", "").strip()
    tok = os.getenv("TG_BOT_TOKEN", "").strip()
    if not (owner and tok):
        print("盤中反彈（沒有設定 TG_OWNER_ID，只印出）：", text)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": owner, "text": text[:3900]}, timeout=20)
    except Exception as e:
        print("盤中反彈推播失敗：", repr(e)[:80])


def loop(interval=90, get_dir=None):
    """背景執行緒：交易時間內每 interval 秒掃全市場一次，有新的符合就私訊。
    get_dir：回傳已經載入好的股票清單（bot_server 傳入 intraday 的清單，不再另外下載一份，省記憶體）；
    還沒載入好就回傳 None，這一輪先不掃。"""
    universe = None
    while True:
        try:
            if in_session():
                _reset_if_new_day()
                if universe is None:
                    d = get_dir() if get_dir else D.get_directory()
                    if d is not None:
                        universe = d.to_dict("records")
                        del d
                if universe is not None:
                    hs = new_hits(scan(universe))
                    if hs:
                        send_owner(message(hs))
        except Exception as e:
            print("盤中反彈錯誤：", repr(e)[:120])
        time.sleep(interval)
