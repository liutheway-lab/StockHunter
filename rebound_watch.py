"""rebound_watch.py — 盤中「快速殺低後反彈」偵測（跑在 Render 機器人上，交易時間內每 90 秒一次）

條件（用證交所 MIS 的即時資料）：
  - 日內低點 l 比昨收 y 低 ≥ DROP_MIN（預設 3%）
  - 現價 z 比日內低點 l 高 ≥ REBOUND_MIN（預設 1.5%）
排序：跌幅大的先推；每次最多推 TOP_N 檔；同一檔股票每天只推一次。
推播只私訊管理者（TG_OWNER_ID）。
"""
import datetime as dt
import os
import time

import requests

import data as D

TPE = dt.timezone(dt.timedelta(hours=8))
DROP_MIN = 0.03
REBOUND_MIN = 0.015
TOP_N = 10
BATCH = 40
OPEN_HM, CLOSE_HM = (9, 0), (13, 30)
_pushed = {"day": None, "codes": set()}


def _num(v):
    try:
        f = float(str(v).split("_")[0])
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def evaluate(q):
    """單一檔的 MIS 資料 → (代號, 名稱, 跌幅, 反彈幅度, 現價)；不符合回傳 None"""
    y, l, z = _num(q.get("y")), _num(q.get("l")), _num(q.get("z"))
    if not (y and l and z):
        return None
    drop = l / y - 1
    reb = z / l - 1
    if drop <= -DROP_MIN and reb >= REBOUND_MIN:
        return (str(q.get("c", "")), str(q.get("n", "")), drop, reb, z)
    return None


def fetch_batch(sess, exs):
    r = sess.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                 params={"ex_ch": "|".join(exs), "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                 headers=D._MIS_HEAD, timeout=10)
    return (r.json() or {}).get("msgArray") or []


def scan(universe_rows, sess=None):
    """universe_rows：[{code, suffix}]。回傳符合條件的清單（已依跌幅排序，還沒去掉推過的）"""
    if sess is None:
        sess = requests.Session()
        try:
            sess.get("https://mis.twse.com.tw/stock/index.jsp", headers=D._MIS_HEAD, timeout=5)
        except Exception:
            pass
    exs = [("tse_" if r["suffix"] == ".TW" else "otc_") + f"{r['code']}.tw" for r in universe_rows]
    hits = []
    for k in range(0, len(exs), BATCH):
        try:
            for q in fetch_batch(sess, exs[k:k + BATCH]):
                h = evaluate(q)
                if h:
                    hits.append(h)
        except Exception as e:
            print("盤中反彈：批次失敗", repr(e)[:80])
        time.sleep(0.2)
    return sorted(hits, key=lambda h: h[2])          # 跌幅大（數字小）的先


def new_hits(hits):
    """今天還沒推過的，最多 TOP_N 檔；並記下來"""
    today = dt.datetime.now(TPE).date().isoformat()
    if _pushed["day"] != today:
        _pushed.update(day=today, codes=set())
    out = [h for h in hits if h[0] not in _pushed["codes"]][:TOP_N]
    _pushed["codes"].update(h[0] for h in out)
    return out


def message(hs):
    lines = [f"⚡ 盤中快速殺低後反彈（{dt.datetime.now(TPE):%H:%M}）"]
    for code, name, drop, reb, z in hs:
        lines.append(f"{code} {name}　現價 {z:g}｜低點跌 {drop * 100:.1f}%｜反彈 +{reb * 100:.1f}%")
    lines.append("（只是條件符合，不代表可以買）")
    return "\n".join(lines)


def in_session(now=None):
    now = now or dt.datetime.now(TPE)
    if now.weekday() >= 5:
        return False
    return OPEN_HM <= (now.hour, now.minute) <= CLOSE_HM


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


def loop(interval=90):
    """背景執行緒：交易時間內每 interval 秒掃一次，有新的符合就私訊"""
    universe = None
    while True:
        try:
            if in_session():
                if universe is None:
                    universe = D.get_directory().to_dict("records")
                hs = new_hits(scan(universe))
                if hs:
                    send_owner(message(hs))
        except Exception as e:
            print("盤中反彈錯誤：", repr(e)[:120])
        time.sleep(interval)
