"""futures_guard.py — 台指期風控提醒（只私訊管理者，不發群組）

1. Telegram 按鈕「期貨」：開倉、平倉、部位、停損、風控設定（部位由你輸入，機器人不會下單）
2. 背景監控（Render，bot_server.py 啟動）：有部位時每 20 秒抓期交所報價，算已實現＋浮動損益
   - 單日虧損達上限 70% 提醒一次、達上限每 5 分鐘一直提醒
   - 部位超過上限（大台等值口數）提醒
   - 離停損價 30 點內提醒、碰到停損價每 5 分鐘一直提醒
   - 收盤前（日盤 13:30、夜盤 04:50）還有部位就提醒
3. 部位存檔：記憶體＋GitHub 的 futures_state.json（Render 環境變數 FUT_GH_TOKEN，沒有就用 GH_TOKEN；
   要有 Contents 讀寫權限）。存不進 GitHub 時，結果卡片與「部位」卡片會寫「只存在記憶體」，因為機器人重啟後部位會不見
所有數字都在 config.py 的 FUT_ 開頭。
"""
import base64
import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

import config as cfg

TZ = ZoneInfo("Asia/Taipei")
STATE_NAME = "futures_state.json"
GH_REPO = os.getenv("GH_REPO", "liutheway-lab/StockHunter")
GH_BRANCH = os.getenv("GH_BRANCH", "main")
GH_TOKEN = (os.getenv("FUT_GH_TOKEN") or os.getenv("GH_TOKEN") or "").strip()      # FUT_GH_TOKEN：專門存期貨部位用的 token（不影響原本的 GH_TOKEN）
LOCAL_FILE = "/tmp/futures_state.json"
MIS_URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
ENTRY = "期貨"
ABORT = {"追蹤", "移除", "調價", "查價", "持股", "說明", "清畫面", "管理者", "更多"}      # 做到一半按了別的功能：中止這一串
KINDS = ["大台", "小台", "微台"]
SIDES = ["多", "空"]

_lock = threading.RLock()
_STATE = {"data": None, "loaded_at": 0.0, "warned": False, "dirty": False}
FG_S = {}            # {chat id: 目前問到哪一步}


# ───────────── 純計算（好測試）─────────────
def empty_state(now=None):
    now = now or datetime.now(TZ)
    return {"day": trade_day(now), "realized": 0.0, "positions": []}


def trade_day(now):
    """期貨交易日：夜盤 15:00 起算下一個交易日；週末往後推到週一"""
    d = now.date()
    if now.hour >= 15:
        d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def session_of(now):
    """目前是哪個盤：'0'＝日盤 08:45～13:45、'1'＝夜盤 15:00～隔天 05:00、None＝沒開盤"""
    wd, hm = now.weekday(), (now.hour, now.minute)
    if wd < 5 and (8, 45) <= hm < (13, 45):
        return "0"
    if wd < 5 and hm >= (15, 0):
        return "1"
    if 1 <= wd <= 5 and hm < (5, 0):
        return "1"
    return None


def roll_day(st, now):
    """新的交易日：已實現損益歸零，部位照舊（留倉）"""
    d = trade_day(now)
    if st.get("day") != d:
        st["day"], st["realized"] = d, 0.0
    return st


def eq_lots(p):
    return p["qty"] * cfg.FUT_EQ.get(p["kind"], 1.0)


def exposure(st):
    return sum(eq_lots(p) for p in st["positions"])


def pts(p, price):
    return (price - p["avg"]) * (1 if p["side"] == "多" else -1)


def money(p, price):
    return pts(p, price) * cfg.FUT_POINT.get(p["kind"], 200) * p["qty"]


def unrealized(st, price):
    return sum(money(p, price) for p in st["positions"])


def find_pos(st, kind, side):
    for p in st["positions"]:
        if p["kind"] == kind and p["side"] == side:
            return p
    return None


def open_pos(st, kind, side, qty, price, stop):
    """開倉或加碼（同一種合約、同方向合併成一筆，均價加權）"""
    p = find_pos(st, kind, side)
    if p:
        p["avg"] = (p["avg"] * p["qty"] + price * qty) / (p["qty"] + qty)
        p["qty"] += qty
        if stop is not None:
            p["stop"] = stop
    else:
        st["positions"].append({"kind": kind, "side": side, "qty": qty, "avg": price, "stop": stop})
    return find_pos(st, kind, side)


def close_pos(st, p, qty, price):
    """平倉 qty 口，回傳這次實現的損益（元）"""
    qty = min(qty, p["qty"])
    gain = pts(p, price) * cfg.FUT_POINT.get(p["kind"], 200) * qty
    st["realized"] += gain
    p["qty"] -= qty
    if p["qty"] <= 0:
        st["positions"].remove(p)
    return gain


def evaluate(st, price, now):
    """回傳要提醒的清單 [(key, 文字, 重複秒數或 None)]；None＝同一個交易日只提醒一次"""
    out = []
    day = st.get("day", "")
    total = st["realized"] + unrealized(st, price)
    lim = cfg.FUT_DAILY_LOSS
    head = f"單日損益 {total:+,.0f}\n上限 -{lim:,.0f}"
    if total <= -lim:
        out.append(("limit", f"🚨 單日虧損已達上限\n{head}\n請先處理部位", cfg.FUT_REPEAT_SEC))
    elif total <= -lim * cfg.FUT_WARN_PCT:
        out.append((f"warn-{day}", f"⚠️ 單日虧損已達上限 {cfg.FUT_WARN_PCT:.0%}\n{head}", None))
    ex = exposure(st)
    if ex > cfg.FUT_MAX_EQ + 1e-9:
        out.append((f"size-{day}-{ex:g}", f"⚠️ 部位超過上限\n目前 {ex:g} 口大台等值\n上限 {cfg.FUT_MAX_EQ:g} 口", 1800))
    for p in st["positions"]:
        if p.get("stop") is None:
            continue
        gap = (price - p["stop"]) if p["side"] == "多" else (p["stop"] - price)
        name = f"{p['side']} {p['kind']} {p['qty']}口"
        if gap <= 0:
            out.append((f"stophit-{p['kind']}-{p['side']}", f"🛑 已碰到停損價\n{name}\n停損 {p['stop']:,.0f}｜現價 {price:,.0f}", cfg.FUT_REPEAT_SEC))
        elif gap <= cfg.FUT_STOP_NEAR:
            out.append((f"stopnear-{day}-{p['kind']}-{p['side']}", f"⚠️ 接近停損價\n{name}\n停損 {p['stop']:,.0f}｜現價 {price:,.0f}\n只剩 {gap:.0f} 點", 900))
    hm = (now.hour, now.minute)
    if st["positions"]:
        if (13, 30) <= hm < (13, 45):
            out.append((f"close-day-{day}", "⏰ 日盤 13:45 收盤\n你還有期貨部位\n要留倉嗎？", None))
        if (4, 50) <= hm < (5, 0):
            out.append((f"close-night-{day}", "⏰ 夜盤 05:00 收盤\n你還有期貨部位\n要留倉嗎？", None))
    return out


def due(sent, key, repeat, now_ts):
    """第一次一定提醒；repeat 秒數到了才再提醒；repeat 是 None 就只提醒一次"""
    last = sent.get(key)
    if last is None or (repeat and now_ts - last >= repeat):
        sent[key] = now_ts
        return True
    return False


# ───────────── 報價（期交所，免費）─────────────
def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


MONTH_CODE = "ABCDEFGHIJKL"          # 1 月＝A … 12 月＝L


def expiry_of(sid, now):
    """TXFK6-F → 2026/11 合約的到期日（每月第 3 個星期三）；看不懂回 None"""
    try:
        month = MONTH_CODE.index(sid[3]) + 1
        year = now.year // 10 * 10 + int(sid[4])
        if year < now.year - 1:
            year += 10
    except Exception:
        return None
    first = datetime(year, month, 1, tzinfo=TZ)
    wed = [first + timedelta(days=i) for i in range(21) if (first + timedelta(days=i)).weekday() == 2]
    return wed[2].date()


def pick_near(qs, now):
    """近月合約：還沒到期、到期日最近的那一個（到期日 13:30 結算後算過期）；判斷不出來就用成交量最大的"""
    cands = [q for q in qs if re.fullmatch(r"TXF[A-L]\d-[FM]", str(q.get("SymbolID", ""))) and _num(q.get("CLastPrice"))]
    if not cands:
        return None
    best = None
    for q in cands:
        ex = expiry_of(str(q["SymbolID"]), now)
        if ex is None or ex < now.date() or (ex == now.date() and (now.hour, now.minute) >= (13, 30)):
            continue
        if best is None or ex < best[0]:
            best = (ex, q)
    return best[1] if best else max(cands, key=lambda x: _num(x.get("CTotalVolume")) or 0)


def quote(now=None):
    """近月台指期最新價。日盤、夜盤各用自己的行情；沒開盤回 None"""
    now = now or datetime.now(TZ)
    mt = session_of(now)
    if mt is None:
        return None
    body = {"MarketType": mt, "SymbolType": "F", "KindID": "1", "CID": "TXF",
            "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}
    r = requests.post(MIS_URL, json=body, timeout=15,
                      headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json;charset=UTF-8",
                               "Origin": "https://mis.taifex.com.tw", "Referer": "https://mis.taifex.com.tw/futures/"})
    r.raise_for_status()
    j = r.json()
    if str(j.get("RtCode")) != "0":
        raise ValueError(f"期交所回應異常：{j.get('RtMsg')}")
    q = pick_near((j.get("RtData") or {}).get("QuoteList") or [], now)
    if q is None:
        raise ValueError("查無報價")
    return _num(q.get("CLastPrice"))


# ───────────── 存檔（記憶體＋GitHub）─────────────
def _gh(method, **kw):
    return requests.request(method, f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_NAME}", timeout=20,
                            headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"}, **kw)


def _gh_load():
    """回傳 (狀態或 None, sha)。檔案還不存在回 ({}, None)；連不上拋例外"""
    r = _gh("GET", params={"ref": GH_BRANCH})
    if r.status_code == 404:
        return {}, None
    r.raise_for_status()
    j = r.json()
    return json.loads(base64.b64decode(j["content"]).decode("utf-8")), j.get("sha")


def _gh_save(st):
    sha = None
    try:
        r = _gh("GET", params={"ref": GH_BRANCH})
        if r.status_code == 200:
            sha = r.json().get("sha")
    except Exception:
        pass
    body = {"message": "更新期貨部位", "branch": GH_BRANCH,
            "content": base64.b64encode(json.dumps(st, ensure_ascii=False, indent=1).encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    r = _gh("PUT", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"GitHub 存檔失敗 {r.status_code} {r.text[:100]}")


def get_state(max_age=120, send=None):
    """取得目前部位。記憶體優先；超過 max_age 秒會再從 GitHub 讀一次（連不上就沿用記憶體）"""
    with _lock:
        now = time.time()
        if _STATE["data"] is None or (GH_TOKEN and not _STATE["dirty"] and now - _STATE["loaded_at"] > max_age):
            data = None
            if GH_TOKEN:
                try:
                    data, _ = _gh_load()
                except Exception as e:
                    print("期貨部位讀取失敗：", repr(e)[:120], flush=True)
            if data is None and _STATE["data"] is None:
                try:
                    with open(LOCAL_FILE, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:
                    data = None
            if data is not None:
                _STATE["data"] = data or empty_state()
            elif _STATE["data"] is None:
                _STATE["data"] = empty_state()
            _STATE["loaded_at"] = now
        st = _STATE["data"]
        for k, v in empty_state().items():
            st.setdefault(k, v)
        roll_day(st, datetime.now(TZ))
        return st


def save_state(st, send=None):
    """存起來。回傳 True＝GitHub 也存好了；False＝只存在記憶體（會私訊提醒一次）"""
    with _lock:
        _STATE["data"] = st
        _STATE["loaded_at"] = time.time()
        try:
            with open(LOCAL_FILE, "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False)
        except Exception:
            pass
        ok = False
        if GH_TOKEN:
            try:
                _gh_save(st)
                ok = True
                _STATE["warned"] = False
            except Exception as e:
                print("期貨部位存檔失敗：", repr(e)[:160], flush=True)
        _STATE["dirty"] = bool(GH_TOKEN) and not ok          # GitHub 沒存成功：先不要用它的舊資料蓋掉記憶體
        return ok                                            # 沒存到 GitHub 時，結果卡片與「部位」卡片會直接寫「只存在記憶體」，不再另外傳訊息吵你


# ───────────── 畫面 ─────────────
def fmt(x):
    return f"{x:,.2f}".rstrip("0").rstrip(".")


def label(p):
    return f"{p['side']} {p['kind']} {p['qty']}口 @{fmt(p['avg'])}"


def persisted():
    """部位有沒有存到 GitHub（機器人重啟後還在）"""
    return bool(GH_TOKEN) and not _STATE["dirty"]


def status_lines(st, price=None, now=None):
    now = now or datetime.now(TZ)
    L = [f"📈 期貨部位　{now:%H:%M}"]
    if not st["positions"]:
        L.append("目前沒有部位")
    for p in st["positions"]:
        L.append(label(p))
        if price is not None:
            L.append(f"　現價 {price:,.0f}｜{pts(p, price):+,.0f} 點")
            L.append(f"　損益 {money(p, price):+,.0f}")
        if p.get("stop") is not None:
            gap = (price - p["stop"]) if (price is not None and p["side"] == "多") else ((p["stop"] - price) if price is not None else None)
            L.append(f"　停損 {fmt(p['stop'])}" + (f"（差 {gap:,.0f} 點）" if gap is not None else ""))
            L += stop_note(p)
        else:
            L.append("　停損 未設")
    total = st["realized"] + (unrealized(st, price) if price is not None else 0)
    L.append("")
    L.append(f"今日已實現 {st['realized']:+,.0f}")
    if price is not None:
        L.append(f"今日合計　 {total:+,.0f}")
        L.append(f"虧損上限　 -{cfg.FUT_DAILY_LOSS:,.0f}（用掉 {max(0, -total) / cfg.FUT_DAILY_LOSS:.0%}）")
    else:
        L.append(f"虧損上限　 -{cfg.FUT_DAILY_LOSS:,.0f}")
        L.append("（目前沒有即時報價）")
    L.append(f"部位　　　 {exposure(st):g}／{cfg.FUT_MAX_EQ:g} 口大台等值")
    if not persisted():
        L += ["", "⚠️ 部位只存在記憶體", "機器人重啟後會不見"]
    return L


def stop_note(p):
    """停損價相對成本的說明：在成本價另一側＝鎖定獲利；否則是最多虧多少"""
    if p.get("stop") is None:
        return []
    d = pts(p, p["stop"])
    mny = d * cfg.FUT_POINT.get(p["kind"], 200) * p["qty"]
    return [f"　鎖定獲利 {d:,.0f} 點（{mny:+,.0f} 元）"] if d > 0 else ([f"　最多虧 {-d:,.0f} 點（{mny:+,.0f} 元）"] if d < 0 else ["　停損在成本價"])


def limits_lines():
    return ["🛡 風控設定", f"單日虧損上限 {cfg.FUT_DAILY_LOSS:,.0f} 元", f"最大部位 {cfg.FUT_MAX_EQ:g} 口大台等值",
            "（小台 0.25 口、微台 0.05 口）", f"虧損 {cfg.FUT_WARN_PCT:.0%} 先提醒", f"離停損 {cfg.FUT_STOP_NEAR} 點內提醒",
            "要改請改 config.py 的 FUT_"]


# ───────────── Telegram 按鈕流程 ─────────────
def reset(cid, ctx=None):
    FG_S.pop(cid, None)
    if ctx:
        ctx["SESSIONS"].pop(cid, None)


def _price(t):
    try:
        v = float(t.replace(",", ""))
    except ValueError:
        return None
    return v if 1000 <= v <= 200000 else None


def handle(cid, text, mid0, ctx):
    """回傳 True＝這則訊息已經處理（呼叫端不用再處理）。ctx 由 intraday.py 傳入：send、kb、pre、delete、SESSIONS、TRACK"""
    t = (text or "").strip()
    if t != ENTRY and cid not in FG_S:
        return False
    send, kb, pre, delete = ctx["send"], ctx["kb"], ctx["pre"], ctx["delete"]
    S, TRACK = ctx["SESSIONS"], ctx["TRACK"]

    def ask(msg, rows):
        send(cid, msg, kb(rows + [["取消"]]))

    def finish(lines=None, notice=None):
        FG_S.pop(cid, None)
        S.pop(cid, None)
        delete(cid, TRACK.pop(cid, []))
        if lines:
            send(cid, pre(lines), None, True)
        if notice:
            send(cid, notice)

    if t == ENTRY:
        FG_S[cid] = {"flow": "menu"}
        S[cid] = {"flow": "fut"}
        TRACK[cid] = [mid0]
        ask("📈 期貨風控\n要做什麼？", [["開倉", "平倉"], ["部位", "停損"], ["價位", "風控設定"]])
        return True

    TRACK.setdefault(cid, []).append(mid0)
    if t == "取消":
        finish(notice="已取消")
        return True
    if t in ABORT:
        finish()
        return False                                   # 讓原本的功能照常處理這個按鈕

    f = FG_S[cid]
    st = get_state(send=ctx.get("alert"))
    flow, step = f["flow"], f.get("step")

    def current_price():
        try:
            return quote()
        except Exception as e:
            print("期貨報價失敗：", repr(e)[:100], flush=True)
            return None

    def done_save(lines, extra=None):
        ok = save_state(st, ctx.get("alert"))
        if extra:
            lines = lines + extra
        if not ok:
            lines = lines + ["", "⚠️ 只存在記憶體"]
        finish(lines)

    if flow == "menu":
        if t == "開倉":
            FG_S[cid] = {"flow": "open", "step": "side"}
            ask("做多還是做空？", [SIDES])
        elif t in ("平倉", "停損"):
            if not st["positions"]:
                finish(notice="目前沒有期貨部位")
            else:
                FG_S[cid] = {"flow": "close" if t == "平倉" else "stop", "step": "pick"}
                ask("哪一筆？", [[label(p)] for p in st["positions"]])
        elif t == "部位":
            finish(status_lines(st, current_price()))
        elif t == "價位":                                    # 現價、今盤高低、各個關鍵價位（掃蕩提醒在盯的）
            try:
                import futures_sweep as SW
                finish(SW.status_lines())
            except Exception as e:
                finish(["價位暫時查不到", type(e).__name__])
        elif t == "風控設定":
            finish(limits_lines())
        else:
            ask("請按下面的按鈕", [["開倉", "平倉"], ["部位", "停損"], ["價位", "風控設定"]])
        return True

    if flow == "open":
        if step == "side":
            if t not in SIDES:
                ask("請按「多」或「空」", [SIDES])
            else:
                f.update(side=t, step="kind")
                ask("哪一種合約？\n（大台一點 200 元）", [KINDS])
        elif step == "kind":
            if t not in KINDS:
                ask("請按下面的按鈕", [KINDS])
            else:
                f.update(kind=t, step="qty")
                ask("幾口？\n請輸入數字，例：1", [])
        elif step == "qty":
            if not re.fullmatch(r"\d{1,3}", t) or int(t) < 1:
                ask("請輸入口數（整數）\n例：1", [])
            else:
                f.update(qty=int(t), step="price")
                ask("成交價多少？\n例：44000", [])
        elif step == "price":
            v = _price(t)
            if v is None:
                ask("價格看起來不對\n請輸入台指期的點數\n例：44000", [])
            else:
                f.update(price=v, step="stop")
                ask("停損價多少？\n不想設就按「不設」", [["不設"]])
        elif step == "stop":
            if t == "不設":
                stop = None
            else:
                stop = _price(t)
                if stop is None:
                    ask("價格看起來不對\n請輸入停損價，或按「不設」", [["不設"]])
                    return True
                ref = current_price() or f["price"]               # 有現價就跟現價比（停損可以設在成本價另一側鎖利，只要不會馬上被打到）
                if (f["side"] == "多" and stop >= ref) or (f["side"] == "空" and stop <= ref):
                    ask(f"做{f['side']}的停損價\n要在現價{'下' if f['side'] == '多' else '上'}方\n現價 {ref:,.0f}\n請重新輸入", [["不設"]])
                    return True
            p = open_pos(st, f["kind"], f["side"], f["qty"], f["price"], stop)
            extra = []
            if exposure(st) > cfg.FUT_MAX_EQ + 1e-9:
                extra = ["", f"⚠️ 部位 {exposure(st):g} 口，超過上限 {cfg.FUT_MAX_EQ:g}"]
            done_save(["✅ 已記錄開倉", label(p), "　停損 " + (fmt(p["stop"]) if p.get("stop") is not None else "未設")] + stop_note(p), extra)
        return True

    if flow in ("close", "stop"):
        if step == "pick":
            p = next((x for x in st["positions"] if label(x) == t), None)
            if p is None:
                ask("請按下面的按鈕", [[label(x)] for x in st["positions"]])
            else:
                f.update(pick=[p["kind"], p["side"]], step="qty" if flow == "close" else "stop")
                if flow == "close":
                    ask(f"平幾口？（目前 {p['qty']} 口）", [["全部"]])
                else:
                    px = current_price()
                    tip = [f"現價 {px:,.0f}"] if px else []
                    ask("新的停損價？\n" + "\n".join(tip + [f"成本 {fmt(p['avg'])}", "可以設在成本價另一側", "（鎖定獲利）", "不想設就按「不設」"]), [["不設"]])
            return True
        p = find_pos(st, *f["pick"])
        if p is None:
            finish(notice="這筆部位已經不在了")
            return True
        if flow == "stop":
            if t == "不設":
                p["stop"] = None
            else:
                v = _price(t)
                if v is None:
                    ask("價格看起來不對\n請輸入停損價，或按「不設」", [["不設"]])
                    return True
                px = current_price()
                if px and ((p["side"] == "多" and v >= px) or (p["side"] == "空" and v <= px)):
                    ask(f"做{p['side']}的停損價\n要在現價{'下' if p['side'] == '多' else '上'}方\n現價 {px:,.0f}\n不然會馬上被打到\n請重新輸入", [["不設"]])
                    return True
                p["stop"] = v
            done_save(["✅ 已更新停損", label(p), "　停損 " + (fmt(p["stop"]) if p.get("stop") is not None else "未設")] + stop_note(p))
            return True
        if step == "qty":                                   # 平倉：幾口
            if t == "全部":
                q = p["qty"]
            elif re.fullmatch(r"\d{1,3}", t) and 1 <= int(t) <= p["qty"]:
                q = int(t)
            else:
                ask(f"請輸入 1～{p['qty']} 的口數\n或按「全部」", [["全部"]])
                return True
            f.update(qty=q, step="price")
            ask("平倉價多少？\n例：44200", [])
        elif step == "price":
            v = _price(t)
            if v is None:
                ask("價格看起來不對\n請輸入台指期的點數\n例：44200", [])
                return True
            name = label(p)
            gain = close_pos(st, p, f["qty"], v)
            done_save(["✅ 已記錄平倉", f"{name}", f"平 {f['qty']} 口 @{fmt(v)}", f"這筆損益 {gain:+,.0f}", f"今日已實現 {st['realized']:+,.0f}"])
        return True

    finish()
    return True


# ───────────── 背景監控（bot_server.py 啟動）─────────────
def check_once(send, sent, now=None):
    """做一次檢查：有部位且開盤才抓報價。回傳這一次送出的提醒數（測試用）"""
    now = now or datetime.now(TZ)
    st = get_state(send=send)
    roll_day(st, now)
    if not st["positions"] or session_of(now) is None:
        return 0
    price = quote(now)
    n = 0
    for key, text, repeat in evaluate(st, price, now):
        if due(sent, key, repeat, time.time()):
            send(text)
            n += 1
    return n


def loop(send):
    """send(text)：私訊管理者"""
    sent, errs = {}, 0
    while True:
        try:
            check_once(send, sent)
            errs = 0
        except Exception as e:
            errs += 1
            print("期貨監控錯誤：", repr(e)[:160], flush=True)
            if errs == 10:                                   # 連續 10 次抓不到（約 3 分鐘）：告訴你，監控可能失效
                try:
                    send("⚠️ 期貨報價連續抓不到\n風控提醒可能失效\n請自己盯一下部位")
                except Exception:
                    pass
        time.sleep(cfg.FUT_POLL_SEC)
