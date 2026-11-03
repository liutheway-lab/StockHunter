"""intraday.py — 盤中監控：追價提醒 + 持股監控 + Telegram 持股指令

每個交易日 08:50 啟動，09:00～13:30 每 1 分鐘查一次證交所即時報價：
  🎯 追價提醒（傳到群組）：明日追價名單上的股票，盤中漲到追價點（漲 4% 且突破盤整高點）
  🛑／⬆️／🎯 持股提醒（只傳給你）：跌破出場價、站上前波高點可加碼、碰到目標價
同一檔、同一種提醒，一天只推播一次。

持股用 Telegram 私訊機器人輸入。管理者（TG_OWNER_ID）可以開通朋友：
  朋友私訊機器人 → 管理者收到通知 → 回覆「允許 朋友ID 名字」就開通；「移除 ID」取消；「使用者」看名單
  每個人的持股分開存在自己跟機器人的私訊裡，提醒也只傳給本人。
指令：
  買 2330 2 950            買進 2 張、950 元（已有持股就算加碼，自動算均價）
  買 2330 2 950 出場 900 目標 1100   自己指定出場價、目標價
  賣 2330                  全部賣出；賣 2330 1 ＝賣 1 張
  設 2330 出場 920         修改出場價（也可以改 目標、前高）
  持股                     看目前持股與距離出場價多遠
持股存在各自跟機器人的私人對話裡（置頂的那則「📌 持股清單」），repo 是公開的也看不到。

跑法：
  python intraday.py             盤中監控（給 intraday.yml 用）
  python intraday.py --commands  只處理一次 Telegram 指令就結束（給 commands.yml 用，盤後也能回覆）
"""
import copy
import html
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

import config as cfg
import strategies as st
from notify import TG_TOKEN, chat_ids, tg_send

TZ = ZoneInfo("Asia/Taipei")
OWNER = os.getenv("TG_OWNER_ID", "").strip()
API = f"https://api.telegram.org/bot{TG_TOKEN}"
POLL_SEC = 60
OPEN_AT, CLOSE_AT = (9, 0), (13, 31)
REPEAT_SEC = 30 * 60          # 條件一直成立：每 30 分鐘再提醒一次
TAIL_AT = (13, 15)            # 尾盤：13:15 之後再補一次
HOLD_TAG = "📌 持股清單"
STATUS_TAG = "⏱ 盤中監控"
HIDE_URL = "https://t.me/zhezhe_users?d="      # 使用者名單藏在置頂標題 📌 的連結裡（畫面上看不到）
_HIDE_OK = {"v": True}                           # 藏在連結裡讀不回來時改成 False，退回「點一下才顯示」
RESULT_FILE = Path(__file__).parent / "scan_results.json"


# ================= Telegram =================
def tg(method, **params):
    try:
        r = requests.post(f"{API}/{method}", json=params, timeout=30)
        j = r.json()
        if not j.get("ok"):
            print(f"Telegram {method} 失敗：{str(j)[:200]}")
        return j
    except Exception as e:
        print(f"Telegram {method} 異常：{e!r}")
        return {"ok": False}


def to_owner(text):
    if OWNER:
        tg_send(OWNER, [text])
    else:
        print("[沒有設定 TG_OWNER_ID]", text)


def send_alert(cid, text, code, name):
    """持股提醒，下面附一顆「賣出」按鈕，按下去直接進賣出流程（要賣多少再問）"""
    kb = {"inline_keyboard": [[{"text": f"賣出 {code} {name}", "callback_data": f"sell:{code}"}]]}
    j = tg("sendMessage", chat_id=cid, text=text, parse_mode="Markdown", reply_markup=kb)
    if not j.get("ok"):
        j = tg("sendMessage", chat_id=cid, text=text.replace("*", ""), reply_markup=kb)
    return (j.get("result") or {}).get("message_id")


def to_group(text):
    for cid in chat_ids():
        tg_send(cid, [text])


# ================= 持股（存在私人對話的置頂訊息） =================
LINE_RE = re.compile(r"^(\d{4,6})\s+(\S+)｜([\d,.]+) (張|股)｜均價 ([\d.]+)｜出場 ([\d.]+|—)｜目標 ([\d.]+|—)｜前高 ([\d.]+|—)｜加碼 (未|已)$")


def fmt_qty(shares):
    """2000 → 2 張；2500 → 2,500 股"""
    shares = int(round(shares))
    return f"{shares // 1000} 張" if shares % 1000 == 0 and shares > 0 else f"{shares:,} 股"


def parse_qty(tok):
    """「2張」「2 張」「500股」→ 股數；沒寫單位回傳 None（要使用者講清楚）"""
    m = re.fullmatch(r"([\d,]+(?:\.\d+)?)\s*(張|股)", tok.replace(" ", ""))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(round(n * 1000)) if m.group(2) == "張" else int(round(n))


def _num(x):
    return None if x in (None, "—", "") else float(x)


def _fmt(x):
    return "—" if x is None else f"{x:g}"


USERS_TAG = "👥 使用者："
USER_RE = re.compile(r"(\d+)\s+([^,，]+)")


def send_to(cid, text):
    if cid:
        tg_send(str(cid), [text])
    else:
        print("[沒有對象]", text)


HEAD_RE = re.compile(r"^(?:🔴|🟢|⚪|▲|▼|－)\s*(\d{4,6})\s+(\S+)\s+([\d,.]+) (張|股)(・已加碼)?$")
FIELD_RES = (("avg", re.compile(r"均價 ([\d,.]+)")), ("stop", re.compile(r"出場 ([\d,.]+|—)")),
             ("target", re.compile(r"目標 ([\d,.]+|—)")), ("key", re.compile(r"前高 ([\d,.]+|—)")))
_LASTQ = {}
_LAST_Z = {}     # {代號: (日期, 最近一次成交價)}：證交所有時一瞬間沒回成交價，就用這個
_STATUS = {}    # {chat id: 「⏱ 盤中監控…」那一行}，跟著置頂看板一起保存
NAME_JOKES = ["哲哲老師只認得代號喔😊\n麻煩幫我打代號好嗎？\n例：2330",
              "哲哲老師從國外留學回來\n不太認識中文😅\n請打代號，例：2330",
              "哲哲老師在國外念書\n中文都還給老師了🤣\n只看得懂數字，例：2330",
              "哲哲老師出國太久\n只記得代號😆\n請打數字代號，例：2330",
              "抱歉，哲哲老師中文不好🙏\n留學回來只會看代號\n例：2330",
              "名字哲哲老師看不懂啦😂\n他只認得代號\n拜託打代號，例：2330"]
_BOOK = {}      # {chat id: (持股, 看板訊息 id, 使用者名單)} 記在機器人記憶裡；手機上的置頂被取消時，持股才不會跟著不見


def _n(v):
    """87 → 87；1020.5 → 1,020.5（千分位、去掉多餘的 0）"""
    return "—" if v is None else f"{v:,.2f}".rstrip("0").rstrip(".")


def _val(t):
    return None if t in (None, "—", "") else float(t.replace(",", ""))


def _hidden_users(pm):
    """讀置頂訊息標題連結裡藏的使用者名單；沒有就回 None"""
    for e in pm.get("entities") or []:
        url = e.get("url") or ""
        if e.get("type") == "text_link" and url.startswith(HIDE_URL):
            try:
                d = json.loads(urllib.parse.unquote(url[len(HIDE_URL):]))
                return {str(k): str(v) for k, v in d.items()}
            except Exception:
                return None
    return None


def load_holdings(cid):
    """讀某個人私訊裡置頂的持股清單。回傳 (持股 dict, 置頂訊息 id, 使用者名單 dict)；使用者名單只存在管理者那則"""
    if not cid:
        return {}, None, {}
    j = tg("getChat", chat_id=cid)
    pm = (j.get("result") or {}).get("pinned_message") or {}
    text = pm.get("text") or ""
    if not text.startswith(HOLD_TAG):
        return {}, None, {}
    h, users, cur = {}, {}, None
    users.update(_hidden_users(pm) or {})
    _STATUS.pop(cid, None)
    for ln in text.splitlines()[1:]:
        ln = ln.strip()
        if ln.startswith(STATUS_TAG):
            _STATUS[cid] = ln
            continue
        if ln.startswith(USERS_TAG):
            users = {m.group(1): m.group(2).strip() for m in USER_RE.finditer(ln[len(USERS_TAG):])}
            continue
        m = HEAD_RE.match(ln)
        if m:                                        # 卡片式：一檔一張卡
            code, name, qty, unit, added = m.groups()
            shares = float(qty.replace(",", "")) * (1000 if unit == "張" else 1)
            cur = {"name": name, "shares": int(round(shares)), "avg": None, "stop": None,
                   "target": None, "key": None, "added": bool(added)}
            h[code] = cur
            continue
        m = LINE_RE.match(ln)
        if m:                                        # 舊格式（一檔一行）：照樣讀得懂，下次存檔會自動換成新排版
            code, name, qty, unit, avg, stop, target, key, added = m.groups()
            shares = float(qty.replace(",", "")) * (1000 if unit == "張" else 1)
            h[code] = {"name": name, "shares": int(round(shares)), "avg": float(avg), "stop": _num(stop),
                       "target": _num(target), "key": _num(key), "added": added == "已"}
            cur = None
            continue
        if cur is not None:
            for k, rx in FIELD_RES:
                mm = rx.search(ln)
                if mm:
                    cur[k] = _val(mm.group(1))
    h = {c: x for c, x in h.items() if x["avg"] is not None}
    return h, pm.get("message_id"), users


def fix_names(h):
    """買進時股票清單還沒載好，名稱暫時是代號；清單好了就補上真正的名稱"""
    if _DIR is None:
        return
    for code, x in h.items():
        if x.get("name") == code:
            nm = stock_name_suffix(code)[0]
            if nm != code:
                x["name"] = nm


def board_text(h, q=None, users=None, title=HOLD_TAG, status=None):
    """持股看板（置頂那則也是它）：一檔一張卡，紅漲綠跌；也是機器人讀取持股的資料來源"""
    q = q or {}
    lines = [f"{title}　{datetime.now(TZ).strftime('%m/%d %H:%M')}"]
    if not h:
        lines += ["", "（目前沒有持股）"]
    for code, x in sorted(h.items()):
        p = (q.get(code) or {}).get("last")
        lamp, now_l, avg_l, stop_l = "－", "成交價 —", f"均價 {_n(x['avg'])}", f"出場 {_n(x['stop'])}"
        if p:
            pct = (p / x["avg"] - 1) * 100
            money = (p - x["avg"]) * x["shares"]
            lamp = "▲" if pct > 0 else "▼" if pct < 0 else "－"
            sg = lambda v: "+" if v >= 0 else "−"
            now_l = f"成交價 {_n(p)}　{sg(pct)}{abs(pct):,.1f}%"
            avg_l += f"　損益 {sg(money)}{abs(money):,.0f} 元"
            if x["stop"]:
                d = (x["stop"] / p - 1) * 100
                stop_l += f"　距出場 {sg(d)}{abs(d):.1f}%"
        lines += ["", f"{lamp} {code} {x['name']}　{fmt_qty(x['shares'])}{'・已加碼' if x['added'] else ''}",
                  now_l, avg_l, stop_l]
        if x["target"] is not None or x["key"] is not None:
            lines.append(f"目標 {_n(x['target'])}　前高 {_n(x['key'])}")
    if status:
        lines += ["", status]
    if users:
        lines += ["", USERS_TAG + "，".join(f"{uid} {name}" for uid, name in users.items())]
    return "\n".join(lines)


def board_html(text):
    """送出用的格式：使用者名單那行做成「點一下才顯示」（spoiler），平常是一片灰點，不佔版面。
    Telegram 回傳給機器人的仍是純文字，所以讀取不受影響"""
    out = []
    for ln in text.split("\n"):
        ln = html.escape(ln, quote=False)
        out.append(f"<tg-spoiler>{ln}</tg-spoiler>" if ln.startswith(USERS_TAG) else ln)
    return "\n".join(out)


def quotes_quick(h, wait=10):
    """查成交價，最多等 wait 秒（查不到就顯示「—」，不要讓機器人卡住）"""
    if not h:
        return {}
    box = {}

    def run():
        try:
            box.update(quotes_for(h))
        except Exception as e:
            print("查報價失敗：", repr(e))
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(wait)
    return dict(box)


def board_payload(text, users, hidden):
    """送出用的文字。hidden：使用者名單藏在標題 📌 的連結裡（畫面上完全看不到）；
    否則把名單那行做成「點一下才顯示」"""
    if not (hidden and users):
        return board_html(text)
    lines = [ln for ln in text.split("\n") if not ln.startswith(USERS_TAG)]
    while lines and lines[-1] == "":
        lines.pop()
    url = HIDE_URL + urllib.parse.quote(json.dumps(users, ensure_ascii=False, separators=(",", ":")), safe="")
    head = html.escape(lines[0][1:], quote=False) if lines[0].startswith("📌") else html.escape(lines[0], quote=False)
    out = [f'<a href="{html.escape(url, quote=True)}">📌</a>{head}'] + [html.escape(ln, quote=False) for ln in lines[1:]]
    return "\n".join(out)


def _post_board(method, cid, mid, text, users, hidden):
    kw = {"chat_id": cid, "text": board_payload(text, users, hidden), "parse_mode": "HTML",
          "link_preview_options": {"is_disabled": True}}
    if mid:
        kw["message_id"] = mid
    j = tg(method, **kw)
    if hidden and users and not (j.get("ok") or "not modified" in str(j)):
        return _post_board(method, cid, mid, text, users, False)           # 藏名單的寫法被拒絕：退回「點一下才顯示」
    return j


def save_holdings(cid, h, msg_id, users=None, q=None, rotate=False):
    """存持股並更新置頂看板。rotate=True：每次都送一則新的並重新置頂、刪掉舊的
    （看板在你手機上被刪或取消置頂時，新的一則會讓置頂列重新出現）"""
    q = quotes_quick(h) if q is None else q
    apply_quote_info(h, q)
    text = board_text(h, q, users, status=_STATUS.get(cid))
    hidden = bool(users) and _HIDE_OK["v"]
    mid = None
    if msg_id and not rotate:
        j = _post_board("editMessageText", cid, msg_id, text, users, hidden)
        if j.get("ok") or "not modified" in str(j):
            mid = msg_id
    if mid is None:
        j = _post_board("sendMessage", cid, None, text, users, hidden)
        mid = (j.get("result") or {}).get("message_id")
        if not mid:
            send_to(cid, "⚠️ 持股看板傳送失敗\n" + str(j.get("description", ""))[:80])
            return msg_id
        pj = tg("pinChatMessage", chat_id=cid, message_id=mid, disable_notification=True)
        if not pj.get("ok"):
            send_to(cid, "⚠️ 置頂失敗\n" + str(pj.get("description", ""))[:80])
        else:
            delete_msgs(cid, [mid + 1])              # 「哲哲老師 pinned …」那則系統訊息（刪不掉也沒關係）
        if msg_id and msg_id != mid:
            delete_msgs(cid, [msg_id])
    if hidden:                                       # 寫完讀回來確認名單真的存得進去，不然朋友會被當成沒開通
        pm = ((tg("getChat", chat_id=cid).get("result") or {}).get("pinned_message")) or {}
        if _hidden_users(pm) != {str(k): str(v) for k, v in users.items()}:
            print("使用者名單藏在連結裡讀不回來，改用「點一下才顯示」")
            _HIDE_OK["v"] = False
            _post_board("editMessageText", cid, mid, text, users, False)
    _BOOK[cid] = (copy.deepcopy(h), mid, copy.deepcopy(users))
    return mid


def set_monitor_status(text):
    """把「盤中監控已啟動／已停止」寫在每個人置頂看板的持股下面（只改文字、不會通知）"""
    for cid in allowed_users():
        try:
            h, mid, users = load_holdings(cid)
            if not h or not mid:
                continue
            _STATUS[cid] = text
            save_holdings(cid, h, mid, users)
        except Exception as e:
            print(f"更新監控狀態失敗（{cid}）：", repr(e))


def ensure_pinned(cid, me, users=None):
    """置頂的持股看板不見了（被刪、被取消置頂）就用記憶中的持股重新送一則並置頂"""
    j = tg("getChat", chat_id=cid)
    if not j.get("ok"):
        return
    pm = (j.get("result") or {}).get("pinned_message") or {}
    if pm.get("message_id") != me[1] or not (pm.get("text") or "").startswith(HOLD_TAG):
        me[1] = save_holdings(cid, me[0], me[1], users, rotate=True)      # 重送一則並置頂，舊的刪掉


def allowed_users():
    """可以使用持股追蹤的人：{chat id: 名字}。管理者（TG_OWNER_ID）＋管理者允許的朋友"""
    if not OWNER:
        return {}
    _, _, users = load_holdings(OWNER)
    return {OWNER: "管理者", **users}


# ================= 股票資料 =================
_DIR = None
DIR_BACKGROUND = False       # bot_server.py 會設成 True：處理訊息時不現抓清單（證交所慢的話會卡住所有人），改由背景載入
_dir_thread = None


def start_dir_loader():
    """背景載入股票清單，失敗每分鐘重試；只有一邊（上市或上櫃）抓到時先用，之後再補"""
    global _dir_thread
    if _dir_thread is not None and _dir_thread.is_alive():
        return

    def run():
        global _DIR
        while True:
            try:
                from lookup import load_directory
                d = load_directory(timeout=15)
                _DIR = d
                if not d.attrs.get("errors"):
                    return
                print("股票清單只抓到一部分，稍後重試：", d.attrs["errors"], flush=True)
            except Exception as e:
                print("股票清單抓取失敗，一分鐘後重試：", repr(e), flush=True)
            time.sleep(60)

    _dir_thread = threading.Thread(target=run, daemon=True)
    _dir_thread.start()


def get_dir():
    """股票清單。背景模式：還沒載好就回傳 None（不等）；一般模式：現抓"""
    global _DIR
    if _DIR is not None:
        return _DIR
    if DIR_BACKGROUND:
        start_dir_loader()
        return None
    try:
        from lookup import load_directory
        _DIR = load_directory()
    except Exception as e:
        print("股票清單抓取失敗：", repr(e))
    return _DIR


_NAME_CACHE = {}


def stock_name_suffix(code):
    """(名稱, .TW/.TWO)。先查官方清單；清單還沒載好時，改用即時報價帶出名稱（最多等 5 秒）"""
    d = get_dir()
    try:
        if d is not None:
            row = d[d["code"] == code]
            if len(row):
                return str(row.iloc[0]["name"]), str(row.iloc[0]["suffix"])
    except Exception as e:
        print("股票清單查詢失敗：", repr(e))
    if code in _NAME_CACHE:
        return _NAME_CACHE[code]
    try:
        q = quotes_quick({code: {"suffix": None}}, 5).get(code) or {}
        if q.get("name"):
            _NAME_CACHE[code] = (str(q["name"]), q.get("suffix") or ".TW")
            return _NAME_CACHE[code]
    except Exception as e:
        print("用報價查名稱失敗：", repr(e))
    return code, ".TW"


def find_stock(text):
    """輸入代號或中文名稱 → 候選清單（list of dict，最多 6 檔）。清單還沒載好時回傳 None"""
    t = text.strip()
    m = re.match(r"^(\d{4,6})\b", t)
    if m:
        return [{"code": m.group(1)}]
    d = get_dir()
    if d is None:
        return None
    try:
        from lookup import resolve
        return resolve(t, d, limit=6)
    except Exception as e:
        print("股票清單查詢失敗：", repr(e))
        return []


def volume_key(raw, price, days=65):
    """前高＝近 65 個交易日裡，成交量最大、而且最高價高過買價的那一天的最高價（找不到就回 None）"""
    try:
        d = raw.tail(days)
        d = d[d["High"] > price]
        if d.empty:
            return None
        return st.tick(float(d.loc[d["Volume"].idxmax(), "High"]))
    except Exception as e:
        print("算前高失敗：", repr(e))
        return None


def auto_key(code, suffix, price):
    """改價位用：抓歷史資料算前高（失敗回 None）"""
    try:
        import yfinance as yf
        from data import _clean
        for sfx in dict.fromkeys([suffix or ".TW", ".TW", ".TWO"]):
            raw = _clean(yf.Ticker(code + sfx).history(period="1y", auto_adjust=False))
            if raw is not None and len(raw) >= 20:
                return volume_key(raw, price)
        return None
    except Exception as e:
        print(f"{code} 抓前高失敗：{e!r}")
        return None


def levels_for(code, suffix, price):
    """買進時自動算出場價、目標價、前波高點：
    有創高拉回／噴後整理的盤整型態 → 照攻擊日規則；沒有 → 出場價＝買價下 10%，目標、前高留空"""
    raw = None
    try:
        import yfinance as yf
        from data import _clean
        raw = _clean(yf.Ticker(code + suffix).history(period="1y", auto_adjust=False))
        if raw is not None and len(raw) >= 80:
            df = st.add_indicators(raw)
            i = len(df) - 1
            for fn in (st.check_pullback, st.check_flag):
                r = fn(df, i)
                if r and "cons_hi" in r and r.get("pre_ok"):     # 盤整條件真的成立才套用（還沒攻擊也行）
                    stop, _ = st.attack_stop(r, price)
                    if stop >= price or r["target"] <= price:      # 輸入的成交價跟現在的盤整區對不上（價位差太多），不能套用
                        break
                    return {"stop": stop, "target": r["target"], "key": st.tick(r["key"]),
                            "why": "依盤整型態",
                            "note": f"符合「{cfg.STRATEGY_NAMES[r['name']]}」盤整型態\n出場、目標、前高\n依攻擊日規則自動算出\n要改請按「價位調整」"}
    except Exception as e:
        print(f"{code} 計算價位失敗：{e!r}")
    key = volume_key(raw, price) if raw is not None else None
    kline = (f"前高 {key:g}\n（近 65 日量最大那天的最高價）" if key
             else "前高留空\n（近 65 日沒有更高的價位）")
    return {"stop": st.tick(price * (1 - cfg.ATTACK_MAX_LOSS), "ceil"), "target": None, "key": key,
            "why": f"買價下 {cfg.ATTACK_MAX_LOSS * 100:.0f}%",
            "note": "目前不是盤整型態\n（創高拉回、噴出後整理）\n出場價先用買價下 10%\n目標價留空\n" + kline + "\n要改請按「價位調整」"}


# ================= 指令 =================
# 輸入框按「/」會跳出的選單（Telegram 規定指令只能用英文，中文直接打也可以）
MENU = []                      # 不再用輸入框旁的「Menu」選單，改用下方四個按鈕
ALIAS = {"start": "說明", "help": "說明", "指令": "說明", "holdings": "持股", "buy": "買", "sell": "賣", "set": "設", "quote": "查價", "price": "查價"}
KEYBOARD = {"keyboard": [[{"text": "買進"}, {"text": "賣出"}], [{"text": "持股"}, {"text": "價位調整"}], [{"text": "查價"}, {"text": "說明"}]],
            "resize_keyboard": True, "is_persistent": True}
KEYBOARD_OWNER = {"keyboard": KEYBOARD["keyboard"] + [[{"text": "管理者"}]],       # 只有管理者看得到「管理者」按鈕
                  "resize_keyboard": True, "is_persistent": True}
FORMAT = {
    "買": "買進格式：買 代號 數量 價格\n例：買 2330 2張 950\n　　買 2330 500股 950\n可以再加：出場 900 目標 1100\n也可以按下方「買進」按鈕",
    "賣": "賣出格式：賣 代號（全部賣出）\n或 賣 2330 1張、賣 2330 500股",
    "設": "修改格式：設 代號 出場 價格\n例：設 2330 出場 920\n也可以改：目標 1100、前高 1000",
}
HELP = ("🤖 *持股追蹤*\n"
        "按下方按鈕就行：\n"
        "買進　→ 輸入代號、數量、成交價\n"
        "賣出　→ 選股票、數量\n"
        "持股　→ 看目前持股與即時損益\n"
        "價位調整　→ 選股票、調整價位\n"
        "查價　→ 輸入代號看最新成交價\n"
        "\n"
        "數量可以打「2張」或「500股」。\n"
        "價位碰到時，機器人會私訊你。")


def _opts(words):
    """把「出場 900 目標 1100 前高 1000」轉成 dict"""
    out = {}
    names = {"出場": "stop", "目標": "target", "前高": "key"}
    for k in range(len(words) - 1):
        if words[k] in names:
            try:
                out[names[words[k]]] = float(words[k + 1])
            except ValueError:
                pass
    return out


def handle(text, h):
    """處理一則持股指令（每個人自己的持股），回傳 (回覆文字, 持股是否有變)"""
    m_ = re.match(r"^設\s*(\d{4,6})\s*(出場|目標|前高)\s*價?格?\s*[:：]?\s*([\d.,]+)\s*$", text.strip())
    if m_:                                           # 「設6620出場價格123」這種寫法也看得懂
        text = f"設 {m_.group(1)} {m_.group(2)} {m_.group(3).replace(',', '')}"
    w = text.replace("/", " ").replace("，", " ").split()
    if not w:
        return None, False
    cmd = w[0].split("@")[0].lower()                 # /buy@機器人名稱 → buy
    cmd = ALIAS.get(cmd, cmd)
    if cmd == "說明":
        return HELP, False
    if cmd in FORMAT and len(w) < (2 if cmd == "賣" else 4):
        return FORMAT[cmd], False                    # 只打了指令、沒打完 → 告訴你格式
    if cmd == "查價":
        codes = list(dict.fromkeys(re.findall(r"\d{4,6}", " ".join(w[1:]))))[:10]
        if not codes:
            return "查價格式：查價 2330\n一次查多檔：查價 2330 2317\n也可以按下方「查價」按鈕", False
        return quote_report(codes), False
    if cmd == "持股":
        q = quotes_quick(h, 10)
        apply_quote_info(h, q)
        _LASTQ["q"] = q
        return holdings_report(h, q), False
    if cmd == "買" and len(w) >= 4:
        code = w[1]
        qty = parse_qty(w[2])
        try:
            price = float(w[3])
        except ValueError:
            price = None
        if qty is None or not price or qty <= 0:
            return "數量要寫單位\n例：買 2330 2張 950\n或：買 2330 500股 950", False
        opt = _opts(w[4:])
        if code in h:                                   # 加碼：算均價；買在前高之上 → 出場價移到前高下 5%
            x = h[code]
            x["avg"] = round((x["avg"] * x["shares"] + price * qty) / (x["shares"] + qty), 2)
            x["shares"] += qty
            msg = f"✅ 已加碼 {code} {x['name']}\n加 {fmt_qty(qty)} @{_fmt(price)}\n共 {fmt_qty(x['shares'])}\n均價 {_fmt(x['avg'])}"
            if x["key"] and price >= x["key"] and not x["added"]:
                x["added"] = True
                new_stop = st.tick(x["key"] * (1 - cfg.ATTACK_STOP2_PCT))
                if x["stop"] is None or new_stop > x["stop"]:
                    x["stop"] = new_stop
                msg += f"\n\n突破前高後加碼\n出場價移到前高下 {cfg.ATTACK_STOP2_PCT * 100:.0f}%\n出場 {_fmt(x['stop'])}"
            x.update(opt)
        else:
            name, sfx = stock_name_suffix(code)
            if name == code:                            # 股票清單沒載好：改用報價附帶的名稱和上市／上櫃
                qi = quotes_quick({code: {"suffix": None}}, 6).get(code) or {}
                name, sfx = qi.get("name") or code, qi.get("suffix") or sfx
            lv = levels_for(code, sfx, price)
            x = {"name": name, "shares": qty, "avg": price, "stop": lv["stop"], "target": lv["target"],
                 "key": lv["key"], "added": False, "suffix": sfx}
            h[code] = x
            why = "自己設定" if "stop" in opt else lv["why"]
            x.update(opt)
            price_l = (f"目標 {_fmt(x['target'])}　前高 {_fmt(x['key'])}" if x["target"] is not None or x["key"] is not None
                       else "目標、前高　尚未設定")
            msg = (f"✅ 已買進 {code} {name}\n{fmt_qty(qty)} @{_fmt(price)}\n\n"
                   f"價位\n出場 {_fmt(x['stop'])}（{why}）\n{price_l}\n\n策略判斷\n{lv['note']}")
        return msg, True
    if cmd == "賣" and len(w) == 2 and w[1] == "全部持股":          # 一次把所有持股都賣掉
        if not h:
            return "目前沒有持股", False
        names = "\n".join(f"{c} {x['name']}" for c, x in sorted(h.items()))
        h.clear()
        return f"✅ 已全部賣出\n{names}\n\n持股已清空", True
    if cmd == "賣" and len(w) >= 2:
        code = w[1]
        if code not in h:
            return f"持股裡沒有 {code}", False
        n = h[code]["shares"]
        if len(w) >= 3 and w[2] != "全部":
            n = parse_qty(w[2])
            if n is None:
                return "數量要寫單位\n例：賣 2330 1張\n或：賣 2330 500股\n全部賣出：賣 2330", False
        name = h[code]["name"]
        h[code]["shares"] -= n
        if h[code]["shares"] <= 0:
            del h[code]
            return f"✅ 已全部賣出 {code} {name}\n已從持股移除", True
        return f"✅ 已賣出 {code} {name}\n賣 {fmt_qty(n)}\n剩 {fmt_qty(h[code]['shares'])}", True
    if cmd == "設" and len(w) >= 4:
        code = w[1]
        if code not in h:
            return f"持股裡沒有 {code}", False
        opt = _opts(w[2:])
        if not opt:
            return "格式：設 2330 出場 920\n也可以改：目標、前高", False
        h[code].update(opt)
        x = h[code]
        msg = f"✅ 已更新 {code} {x['name']}\n出場 {_fmt(x['stop'])}\n目標 {_fmt(x['target'])}\n前高 {_fmt(x['key'])}"
        try:                                                   # 出場價已經高於現價 → 當場提醒，不用乾等
            pq = (quotes_for({code: x}).get(code) or {}).get("last")
            if pq and x["stop"] and pq <= x["stop"]:
                msg += f"\n\n⚠️ 現價 {pq:g} 已低於出場價\n盤中監控下一輪（約 1 分鐘內）\n會送出跌破提醒"
        except Exception:
            pass
        return msg, True
    return "看不懂這個指令\n請按下方的按鈕操作", False


_OFFSET_FILE = Path("/tmp/tg_offset")


ADMIN_HELP = ("\n\n👑 *管理者說明*\n"
              "朋友私訊機器人後\n"
              "你會收到通知\n"
              "回覆「允許 ID 名字」就開通\n"
              "允許 12345 小明　開通\n"
              "移除 12345　取消\n"
              "使用者　看目前有誰可以用")


def admin(text, users):
    """管理者指令。回傳 (回覆, 名單是否有變)；不是管理者指令回傳 (None, False)"""
    w = text.replace("/", " ").split()
    if not w:
        return None, False
    if w[0] == "允許" and len(w) >= 2 and w[1].isdigit():
        users[w[1]] = " ".join(w[2:]) or "朋友"
        send_kb(w[1], "✅ 管理者已開通你\n請按下方按鈕開始\n\n" + HELP)
        return f"✅ 已允許 {w[1]} {users[w[1]]}", True
    if w[0] == "移除" and len(w) >= 2:
        if users.pop(w[1], None) is None:
            return f"名單裡沒有 {w[1]}", False
        return f"✅ 已移除 {w[1]}", True
    if w[0] == "使用者":
        if not users:
            return "目前只有你自己在用。\n朋友私訊機器人後\n你會收到通知\n回覆「允許 他的ID」開通", False
        return "👥 可以使用持股追蹤的人：\n" + "\n".join(f"{u} {n}" for u, n in users.items()), False
    return None, False


SESSIONS = {}          # 按鈕一步一步輸入時，記住每個人目前問到哪一步


TRACK = {}             # 按鈕流程進行中，記住這一次操作的訊息編號（按「取消」時整串刪掉）
LAST_HELP = {}         # 上一次的「說明」訊息編號（再按說明時，把舊的刪掉）
PENDING = {}           # 等管理者開通的人：{ID: 名字}（機器人重啟後會清空，對方再傳一次訊息就會重新出現）
LAST_VIEW = {}         # 上一次的「持股」訊息編號（再按持股時，把舊的刪掉）


LAST_BOT = {}          # 機器人最後傳的一則訊息編號
MENU_WAIT = {}         # 管理者選單正在等你選（還沒做完，先不清）
CHAT = {}              # 每個人私訊視窗裡，機器人傳的和使用者傳的訊息編號（下一次操作時整批清掉，只留釘選和最新的）


def delete_msgs(cid, ids):
    """刪訊息（已經刪過的、太舊的會失敗，不用理它，所以不印錯誤）"""
    for mid in dict.fromkeys(ids):
        if mid:
            try:
                requests.post(f"{API}/deleteMessage", json={"chat_id": cid, "message_id": mid}, timeout=10)
            except Exception:
                pass


def send_kb(cid, text, kb=None):
    """傳訊息並附上鍵盤按鈕（預設是 買進／賣出／持股／說明）。回傳訊息編號"""
    j = tg("sendMessage", chat_id=cid, text=text.replace("*", ""), reply_markup=kb or (KEYBOARD_OWNER if cid == OWNER else KEYBOARD))
    mid = (j.get("result") or {}).get("message_id")
    if mid:
        CHAT.setdefault(cid, []).append(mid)
        LAST_BOT[cid] = mid
        if cid in SESSIONS:
            TRACK.setdefault(cid, []).append(mid)
    return mid


def _kb(rows):
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True, "one_time_keyboard": True}


def guided(cid, text, h):
    """按鈕流程：買進 → 代號 → 數量 → 價格；賣出 → 選股票 → 數量。
    回傳要執行的一行指令（例如「買 2330 2000股 950」），還在問問題時回傳 None"""
    t = text.strip()
    if t == "取消":
        SESSIONS.pop(cid, None)
        delete_msgs(cid, TRACK.pop(cid, []))          # 這一次操作的問答全部清掉
        return "持股"                                  # 取消後回到持股畫面
    if t == "買進":
        SESSIONS[cid] = {"flow": "buy", "step": "code"}
        send_kb(cid, "要買哪一檔？\n請輸入股票代號\n例：2330", _kb([["取消"]]))
        return None
    if t == "賣出":
        if not h:
            send_kb(cid, "目前沒有持股")
            return None
        SESSIONS[cid] = {"flow": "sell", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["全部持股一次賣出"], ["取消"]]
        send_kb(cid, "要賣哪一檔？\n或一次全部賣掉", _kb(rows))
        return None
    if t == "查價":
        SESSIONS[cid] = {"flow": "quote", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]
        send_kb(cid, "要查哪幾檔？\n輸入代號，可以一次多檔\n例：2330 2317" + ("\n或按下面的持股" if h else ""), _kb(rows))
        return None
    if t == "價位調整":
        if not h:
            send_kb(cid, "目前沒有持股")
            return None
        SESSIONS[cid] = {"flow": "edit", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]
        send_kb(cid, "要改哪一檔？", _kb(rows))
        return None
    s_ = SESSIONS[cid]
    if s_["flow"] == "quote":
        codes = list(dict.fromkeys(re.findall(r"\d{4,6}", t)))
        if not codes:
            send_kb(cid, random.choice(NAME_JOKES), _kb([["取消"]]))
            return None
        SESSIONS.pop(cid, None)
        return "查價 " + " ".join(codes[:10])
    if s_["flow"] == "sell" and s_["step"] == "code" and t == "全部持股一次賣出":
        s_["step"] = "confirm_all"
        names = "\n".join(f"{c} {x['name']} {fmt_qty(x['shares'])}" for c, x in sorted(h.items()))
        send_kb(cid, f"確定要把全部 {len(h)} 檔都賣掉嗎？\n{names}", _kb([["確定全部賣出"], ["取消"]]))
        return None
    if s_["flow"] == "sell" and s_["step"] == "confirm_all":
        if t == "確定全部賣出":
            SESSIONS.pop(cid, None)
            return "賣 全部持股"
        send_kb(cid, "請按「確定全部賣出」\n或打「取消」", _kb([["確定全部賣出"], ["取消"]]))
        return None
    if s_["flow"] == "edit":
        if s_["step"] == "code":
            cm = re.search(r"\d{4,6}", t)
            if not cm or cm.group(0) not in h:
                send_kb(cid, "請按下面的股票，或打「取消」", _kb([[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]))
                return None
            s_["code"], s_["step"] = cm.group(0), "field"
            x = h[s_["code"]]
            send_kb(cid, f"{s_['code']} {x['name']}\n出場價 {_fmt(x['stop'])}\n目標價 {_fmt(x['target'])}\n前高 {_fmt(x['key'])}\n要改哪一個？",
                    _kb([["出場價", "目標價", "前高"], ["取消"]]))
            return None
        if s_["step"] == "field":
            if t not in ("出場價", "目標價", "前高"):
                send_kb(cid, "請按下面的按鈕", _kb([["出場價", "目標價", "前高"], ["取消"]]))
                return None
            s_["field"], s_["step"] = t[:2], "price"
            if t == "前高":
                send_kb(cid, "新的前高是多少？\n輸入數字，例：123\n或按「自動抓」\n（近 65 日量最大那天的最高價）",
                        _kb([["自動抓"], ["取消"]]))
            else:
                send_kb(cid, f"新的{t}是多少？\n輸入數字，例：123", _kb([["取消"]]))
            return None
        if s_["field"] == "前高" and t == "自動抓":
            x = h[s_["code"]]
            v = auto_key(s_["code"], x.get("suffix"), x["avg"])
            if v is None:
                send_kb(cid, "抓不到合適的前高\n（近 65 日沒有比均價更高的價）\n請自己輸入數字", _kb([["取消"]]))
                return None
            SESSIONS.pop(cid, None)
            return f"設 {s_['code']} 前高 {v:g}"
        try:
            price = float(t.replace(",", ""))
        except ValueError:
            send_kb(cid, "請輸入數字，例如 123", _kb([["取消"]]))
            return None
        SESSIONS.pop(cid, None)
        return f"設 {s_['code']} {s_['field']} {price:g}"
    code_m = re.search(r"\d{4,6}", t)
    if s_["step"] == "code":
        if not code_m:
            if s_["flow"] == "buy":
                send_kb(cid, random.choice(NAME_JOKES), _kb([["取消"]]))
            else:
                send_kb(cid, "請輸入股票代號\n（4～6 位數字）\n例：2330", _kb([["取消"]]))
            return None
        s_["code"] = code_m.group(0)
        if s_["flow"] == "sell":
            if s_["code"] not in h:
                send_kb(cid, f"持股裡沒有 {s_['code']}", _kb([["取消"]]))
                return None
            s_["step"] = "qty"
            x = h[s_["code"]]
            send_kb(cid, f"{s_['code']} {x['name']} 目前 {fmt_qty(x['shares'])}\n要賣多少？\n按「全部賣出」\n或輸入張數、股數\n例：1張、500股",
                    _kb([["全部賣出"], ["取消"]]))
            return None
        name, _ = stock_name_suffix(s_["code"])
        s_["step"] = "qty"
        send_kb(cid, f"{s_['code']} {name}\n買了多少？\n請輸入張數或股數\n例：2張、500股", _kb([["取消"]]))
        return None
    if s_["step"] == "qty" or s_["step"] == "unit":
        if s_["flow"] == "sell" and t in ("全部", "全部賣出"):
            SESSIONS.pop(cid, None)
            return f"賣 {s_['code']}"
        if s_["step"] == "unit":                       # 剛才輸入了數字，現在按「張」或「股」
            if t in ("張", "股"):
                q = parse_qty(f"{s_['num']}{t}")
            else:
                send_kb(cid, f"{s_['num']} 是幾張還是幾股？\n請按下面的按鈕", _kb([["張", "股"], ["取消"]]))
                return None
        else:
            q = parse_qty(t)
            if q is None and re.fullmatch(r"[\d,]+(?:\.\d+)?", t):    # 只打了數字 → 問單位
                s_["num"], s_["step"] = t.replace(",", ""), "unit"
                send_kb(cid, f"{t} 是幾張還是幾股？", _kb([["張", "股"], ["取消"]]))
                return None
        if q is None:
            send_kb(cid, "請輸入張數或股數\n例：2張、500股", _kb([["取消"]]))
            return None
        if q <= 0:
            send_kb(cid, "數量要大於 0", _kb([["取消"]]))
            return None
        if s_["flow"] == "sell":
            SESSIONS.pop(cid, None)
            return f"賣 {s_['code']} {q}股"
        s_["qty"], s_["step"] = q, "price"
        send_kb(cid, f"成交價是多少？例：950", _kb([["取消"]]))
        return None
    if s_["step"] == "price":
        try:
            price = float(t.replace(",", ""))
        except ValueError:
            send_kb(cid, "請輸入數字，例如 950", _kb([["取消"]]))
            return None
        SESSIONS.pop(cid, None)
        return f"買 {s_['code']} {s_['qty']}股 {price:g}"
    SESSIONS.pop(cid, None)
    return None


def _handle_message(m, state, asked):
    """處理一則訊息（GitHub 輪詢和 bot_server.py 的即時模式共用）"""
    def get(cid):
        if cid not in state:
            loaded = list(load_holdings(cid))
            if loaded[1] is None and cid in _BOOK:           # 置頂被取消了：用記憶中的持股，之後會重新置頂
                h0, mid0, u0 = _BOOK[cid]
                loaded = [copy.deepcopy(h0), mid0, copy.deepcopy(u0)]
            fix_names(loaded[0])
            state[cid] = loaded
        return state[cid]

    chat = m.get("chat") or {}
    text = (m.get("text") or "").strip()
    if chat.get("type") in ("group", "supergroup") and text.split("@")[0] in ("/id", "/start"):
        send_to(str(chat["id"]), f"這個群組的 ID 是 `{chat['id']}`\n"
                                 "請到 GitHub 的 Settings\n"
                                 "→ Secrets → Actions\n"
                                 "把 TG\\_CHAT\\_ID\n"
                                 "改成這串數字（含負號）")
        return
    if chat.get("type") != "private" or not text:
        return
    cid = str(chat["id"])
    if not OWNER:
        send_to(cid, f"你的 Telegram ID 是 `{cid}`\n"
                     "請到 GitHub 的 Settings\n"
                     "→ Secrets → Actions\n"
                     "新增 TG\\_OWNER\\_ID\n"
                     "值填這串數字")
        return
    owner = get(OWNER)                               # 管理者的置頂訊息裡存著朋友名單
    users = owner[2]
    if cid != OWNER and cid not in users:            # 還沒開通的人：通知管理者
        frm = m.get("from") or {}
        name = (frm.get("first_name") or "") + (f"（@{frm['username']}）" if frm.get("username") else "")
        send_to(cid, f"👋 這是持股追蹤機器人\n需要管理者開通才能用\n"
                     f"已送出你的申請（ID {cid}）\n開通後會通知你，請稍等\n"
                     "不用重複傳訊息\n\n"
                     "追價提醒、夜間選股\n會發在群組，不用開通就能看")
        PENDING[cid] = (frm.get("first_name") or "朋友").strip()
        if cid not in asked:
            asked.add(cid)
            send_kb(OWNER, f"👤 {name or '有人'} 想使用持股追蹤\n請按「管理者」→「待開通名單」開通")
        return
    me = get(cid)
    if cid == OWNER:
        mid0 = m.get("message_id")

        def tidy(*extra):                             # 把這一次管理者操作的中間訊息全部清掉
            delete_msgs(cid, TRACK.pop(cid, []) + [mid0] + list(extra))

        if text == "管理者":                          # 管理者功能：全部用按鈕操作
            MENU_WAIT[cid] = True
            TRACK[cid] = [mid0, send_kb(cid, "👑 管理者功能", _kb([["待開通名單", "移除使用者"], ["使用者名單"], ["取消"]]))]
            return
        if text in ("待開通名單", "移除使用者"):
            items = ({c: n for c, n in PENDING.items() if c not in users} if text == "待開通名單" else dict(users))
            verb = "允許" if text == "待開通名單" else "移除"
            MENU_WAIT[cid] = bool(items)
            if not items:
                tidy()
                send_kb(cid, "目前沒有人在等開通" if verb == "允許" else "目前沒有朋友可以移除")
            else:
                TRACK.setdefault(cid, []).extend([mid0, send_kb(cid, "按名字就" + ("開通" if verb == "允許" else "移除"),
                                                                _kb([[f"{verb} {c} {n}"] for c, n in items.items()] + [["取消"]]))])
            return
        if text == "使用者名單":
            tidy()
            send_kb(cid, admin("使用者", users)[0])
            return
        reply, changed = admin(text, users)
        if reply:
            tidy()
            if changed:
                owner[1] = save_holdings(OWNER, owner[0], owner[1], users)
                PENDING.pop(text.split()[1], None)
            send_kb(OWNER, reply)
            return
    me = get(cid)
    if cid in SESSIONS and text in ("持股", "說明"):            # 做到一半按了別的功能：先中止目前這一串
        SESSIONS.pop(cid, None)
    if text in ("買進", "賣出", "價位調整", "查價", "取消") or cid in SESSIONS:   # 按鈕一步一步輸入
        if text == "取消" and cid not in SESSIONS:
            TRACK.setdefault(cid, []).append(m.get("message_id"))   # 管理者選單的取消：連同選單一起清掉
        if text in ("買進", "賣出", "價位調整", "查價"):
            TRACK[cid] = []                            # 開始新的一次操作
        if cid in SESSIONS or text in ("買進", "賣出", "價位調整", "查價"):
            TRACK.setdefault(cid, []).append(m.get("message_id"))
        line = guided(cid, text, me[0])
        if line is None:
            return
        delete_msgs(cid, TRACK.pop(cid, []))            # 做完了：中間的問答清掉，只留最後的結果
        text = line
    _LASTQ.pop("q", None)
    reply, changed = handle(text, me[0])
    if changed:
        me[1] = save_holdings(cid, me[0], me[1], owner[2] if cid == OWNER else None, rotate=True)
    elif text == "持股" and me[0] and "q" in _LASTQ:                 # 按「持股」順便把置頂看板的現價更新
        me[1] = save_holdings(cid, me[0], me[1], owner[2] if cid == OWNER else None, _LASTQ["q"])
    if reply == HELP:
        delete_msgs(cid, LAST_HELP.pop(cid, []))      # 舊的說明刪掉，聊天室只留最新一則
        mid = send_kb(cid, HELP)
        LAST_HELP[cid] = [m.get("message_id"), mid]
    elif reply and text == "持股":
        delete_msgs(cid, LAST_VIEW.pop(cid, []))      # 舊的持股畫面刪掉，只留最新一則
        LAST_VIEW[cid] = [m.get("message_id"), send_kb(cid, reply)]
    elif reply:
        send_kb(cid, reply)


def handle_message(m, state, asked):
    """處理一則訊息。整串操作做完、有結果了，才把上面不需要的訊息清掉（釘選的持股清單和最新結果留著）"""
    chat = m.get("chat") or {}
    cid = str(chat.get("id", ""))
    if (m.get("text") or "").strip() == "改價位":            # 舊按鈕名稱照樣看得懂
        m = {**m, "text": "價位調整"}
    private = chat.get("type") == "private" and bool(m.get("text"))
    if private:
        CHAT.setdefault(cid, []).append(m.get("message_id"))
    before = LAST_BOT.get(cid)
    MENU_WAIT.pop(cid, None)
    _handle_message(m, state, asked)
    if not private or cid not in state or cid in SESSIONS or MENU_WAIT.get(cid):
        return                                        # 還沒開通、或這串操作還沒做完：先不清
    ensure_pinned(cid, state[cid], (state.get(OWNER) or [0, 0, None])[2] if cid == OWNER else None)
    new = LAST_BOT.get(cid)
    if not new or new == before:
        return
    keep = {state[cid][1], (state.get(OWNER) or [None, None])[1], new}      # 釘選的持股清單不能刪
    delete_msgs(cid, [i for i in CHAT.get(cid, []) if i not in keep])
    CHAT[cid] = [new]


def handle_callback(cq, state, asked):
    """按了提醒訊息下面的按鈕：目前只有「賣出」，直接跳到「要賣多少」那一步"""
    tg("answerCallbackQuery", callback_query_id=cq.get("id"))
    m = cq.get("message") or {}
    chat = m.get("chat") or {}
    data = cq.get("data") or ""
    if chat.get("type") != "private" or not data.startswith("sell:"):
        return
    cid, code = str(chat["id"]), data[5:]
    h, mid, _ = load_holdings(cid)
    if not h and cid in _BOOK:
        h = _BOOK[cid][0]
    if code not in h:
        send_kb(cid, f"持股裡已經沒有 {code}\n可能已經賣掉了")
        return
    delete_msgs(cid, TRACK.pop(cid, []))               # 前一個沒做完的操作清掉
    SESSIONS[cid] = {"flow": "sell", "step": "qty", "code": code}
    TRACK[cid] = []
    x = h[code]
    send_kb(cid, f"{code} {x['name']} 目前 {fmt_qty(x['shares'])}\n要賣多少？\n按「全部賣出」\n或輸入張數、股數\n例：1張、500股",
            _kb([["全部賣出"], ["取消"]]))


def setup_menu():
    """清掉輸入框旁的「Menu」指令選單（用下方按鈕就夠了）"""
    for scope in ({"type": "all_private_chats"}, {"type": "default"}):
        tg("deleteMyCommands", scope=scope)
    tg("setChatMenuButton", menu_button={"type": "default"})


def process_commands():
    """（GitHub 輪詢模式）讀取新訊息並處理。如果已經架了 bot_server.py（webhook），就交給它，這裡不處理"""
    info = tg("getWebhookInfo").get("result") or {}
    if info.get("url"):
        return
    if not _OFFSET_FILE.exists():                    # 每次啟動設定一次「/」選單（只在私訊顯示）
        setup_menu()
    offset = int(_OFFSET_FILE.read_text()) if _OFFSET_FILE.exists() else None
    j = tg("getUpdates", offset=offset, timeout=0, allowed_updates=["message", "callback_query"])
    ups = j.get("result") or []
    if not ups:
        return
    state, asked = {}, set()
    for u in ups:
        offset = u["update_id"] + 1
        try:
            if u.get("callback_query"):
                handle_callback(u["callback_query"], state, asked)
            else:
                handle_message(u.get("message") or {}, state, asked)
        except Exception as e:
            print("處理訊息失敗：", repr(e))
    tg("getUpdates", offset=offset, timeout=0)          # 告訴 Telegram 這些訊息處理完了
    _OFFSET_FILE.write_text(str(offset))


# ================= 即時報價 =================
def get_quotes(items, tries=3, gap=2):
    """查報價，沒查到的最多再試到 tries 次（每次隔 gap 秒），不要連續猛打"""
    out, todo = {}, list(items)
    for k in range(tries):
        out.update(_get_quotes_once(todo))
        todo = [(c, sfx) for c, sfx in items if c not in out]
        if not todo:
            break
        if k < tries - 1:
            print(f"報價 {len(todo)} 檔沒查到，{gap} 秒後重試（{k + 1}/{tries}）")
            time.sleep(gap)
    return out


def _twse_fetch(ex):
    r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                     params={"ex_ch": ex, "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                     headers={"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
                     timeout=8)
    res = {}
    for q in r.json().get("msgArray", []):
        def f(k):
            try:
                return float(str(q.get(k, "")).split("_")[0])
            except ValueError:
                return None
        z = f("z")                     # 只用最後一筆成交價，不拿買賣報價
        if z:
            _LAST_Z[q["c"]] = (q.get("d", ""), z)
            last = z
        elif f("o"):                   # 今天已經有成交，只是這一瞬間沒有新成交（z 是「-」）：
            d0, zc = _LAST_Z.get(q["c"], ("", None))
            if d0 != q.get("d", "") or not zc:
                continue               # 沒有先前記下的成交價 → 當作沒查到，交給重試／Yahoo，絕不拿昨收冒充
            last = zc
        else:
            last = f("y")              # 還沒開盤、今天沒有任何成交 → 用昨收
        if last:
            res[q["c"]] = {"last": last, "high": f("h") or last, "low": f("l") or last, "prev": f("y"), "date": q.get("d", ""),
                           "name": (q.get("n") or "").strip() or None,
                           "suffix": {"otc": ".TWO", "emg": ".EMG"}.get(q.get("ex"), ".TW")}
    return res


def _get_quotes_once(items):
    """items：[(code, suffix)]。回傳 {code: {"last", "high", "prev", "date", "name", "suffix"}}。
    證交所即時報價（上市、上櫃；不知道是哪邊就兩邊都問），沒查到的興櫃股另外問一次，最後才用 Yahoo"""
    out = {}
    if not items:
        return out
    parts = []
    for code, sfx in items:
        if sfx == ".EMG":
            continue
        if sfx != ".TWO":
            parts.append(f"tse_{code}.tw")
        if sfx != ".TW":
            parts.append(f"otc_{code}.tw")
    try:
        if parts:
            out.update(_twse_fetch("|".join(parts)))
    except Exception as e:
        print("證交所即時報價失敗：", repr(e))
    emg = [c for c, sfx in items if c not in out and sfx in (None, ".EMG")]
    if emg:                                          # 興櫃：另外一次要求，失敗不影響上面查到的
        try:
            out.update(_twse_fetch("|".join(f"emg_{c}.tw" for c in emg)))
        except Exception as e:
            print("興櫃報價查詢失敗：", repr(e))
    missing = [(c, s) for c, s in items if c not in out]
    if missing:
        try:
            import yfinance as yf
            for code, sfx in missing:
                if sfx == ".EMG":
                    continue
                for sf in ([sfx] if sfx else [".TW", ".TWO"]):
                    try:
                        fi = yf.Ticker(code + sf).fast_info
                        last = float(fi["last_price"])
                    except Exception:
                        continue
                    out[code] = {"last": last, "high": float(fi.get("day_high") or last), "low": float(fi.get("day_low") or last),
                                 "prev": float(fi.get("previous_close") or 0) or None,
                                 "date": datetime.now(TZ).strftime("%Y%m%d"), "name": None, "suffix": sf}
                    break
        except Exception as e:
            print("Yahoo 報價失敗：", repr(e))
    return out


def guess_suffix(code):
    """股票清單載好了就回 .TW／.TWO；還沒載好回 None（查價時兩邊都問）"""
    d = _DIR
    try:
        if d is not None:
            row = d[d["code"] == code]
            if len(row):
                return str(row.iloc[0]["suffix"])
    except Exception:
        pass
    return None


def apply_quote_info(h, q):
    """用報價附帶的資料補上股票名稱、上市／上櫃（股票清單沒載好時買進的，名稱先是代號）"""
    for code, x in h.items():
        info = (q or {}).get(code) or {}
        if x.get("name") == code and info.get("name"):
            x["name"] = info["name"]
        if info.get("suffix") and not x.get("suffix"):
            x["suffix"] = info["suffix"]


def quotes_for(h):
    return get_quotes([(c, x.get("suffix") or guess_suffix(c)) for c, x in h.items()])


def quote_report(codes):
    """查價：任意股票的最新成交價、昨收、今日高低（最多 10 檔）"""
    q = quotes_quick({c: {"suffix": None} for c in codes}, 10)
    lines = [f"🔎 查價　{datetime.now(TZ).strftime('%m/%d %H:%M')}"]
    for c in codes:
        x = q.get(c)
        if not x:
            lines += ["", f"－ {c}　查不到"]
            continue
        last, prev = x["last"], x.get("prev")
        lamp, now_l = "－", f"成交價 {_n(last)}"
        if prev:
            pct = (last / prev - 1) * 100
            lamp = "▲" if pct > 0 else "▼" if pct < 0 else "－"
            now_l += f"　{'+' if pct >= 0 else '−'}{abs(pct):,.1f}%"
        lines += ["", f"{lamp} {c} {x.get('name') or c}", now_l]
        if prev:
            lines.append(f"昨收 {_n(prev)}")
        lines.append(f"今日高 {_n(x.get('high'))}　低 {_n(x.get('low'))}")
    return "\n".join(lines)


def holdings_report(h, q, title="📋 持股"):
    if not h:
        return "目前沒有持股。"
    return board_text(h, q, None, title)


# ================= 盤中監控 =================
def load_watch():
    """昨晚夜間掃描的明日追價名單（只取大盤啟用的策略）"""
    try:
        scan = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    active = set(scan.get("active") or [])
    out = []
    for name, items in (scan.get("watch") or {}).items():
        if name not in active:
            continue
        for it in items:
            out.append({**it, "strat": name})
    return out


def tv(code, sfx):
    ex = "TPEX" if sfx == ".TWO" else "TWSE"
    return f"[{code}](https://www.tradingview.com/chart/?symbol={ex}%3A{code})"


def _due(last, key, hit, now):
    """持股提醒要不要送：None＝不用、first＝第一次、again＝過了 30 分鐘還成立、tail＝尾盤補一次。條件消失就清掉紀錄"""
    if not hit:
        last.pop(key, None)
        return None
    rec = last.get(key)
    if rec is None:
        return "first"
    gap = (now - rec["t"]).total_seconds()
    if gap >= REPEAT_SEC:
        return "again"
    if (now.hour, now.minute) >= TAIL_AT and not rec["tail"] and gap >= 600:
        return "tail"
    return None


def _alert_once(cid, last, key, why, text, code, name, now):
    """送一則持股提醒（再次提醒時把上一則同種提醒刪掉，不堆積）"""
    old = last.get(key)
    extra = {"again": "還沒處理，再提醒一次", "tail": "快收盤了，最後提醒一次"}.get(why)
    head, _, rest = text.partition("\n")
    mid = send_alert(cid, head + ("\n" + extra if extra else "") + "\n" + rest, code, name)
    if old and old.get("mid") and why != "first":
        delete_msgs(cid, [old["mid"]])
    last[key] = {"t": now, "mid": mid,
                 "tail": (now.hour, now.minute) >= TAIL_AT or bool(old and old["tail"])}


def check_once(watch, books, sent, today, last=None, now=None):
    """查一次報價，送出該送的提醒。books：{chat id: 持股}；sent：今天已經送過的 (種類, 對象, 代號)"""
    last = {} if last is None else last
    now = now or datetime.now(TZ)
    items = [(w["code"], w["suffix"]) for w in watch]
    for h in books.values():
        for code, x in h.items():
            items.append((code, x.get("suffix") or guess_suffix(code)))
    q = get_quotes(list(dict.fromkeys(items)))
    traded = [v for v in q.values() if v.get("date") == today]
    for w in watch:                                         # 🎯 追價提醒（群組）
        v = q.get(w["code"])
        if not v or v.get("date") != today or ("chase", "group", w["code"]) in sent:
            continue
        if v["high"] >= w["chase"]:
            sent.add(("chase", "group", w["code"]))
            chg = (v["last"] / v["prev"] - 1) * 100 if v.get("prev") else 0
            back = "" if v["last"] >= w["chase"] else "，目前已拉回"
            to_group(f"🎯 *追價提醒*　{w['code']} {w['name']}\n"
                     f"盤中漲到追價點 {w['chase']:g}\n最高 {v['high']:g}\n"
                     f"成交價 {v['last']:g}（{chg:+.1f}%）{back}\n"
                     f"出場 {w['stop1']:g}｜目標 {w['target']:g}｜{cfg.STRATEGY_NAMES.get(w['strat'], '')}\n"
                     f"📊 {tv(w['code'], w['suffix'])}")
    for cid, h in books.items():                            # 持股提醒（各自傳給本人）
        for code, x in h.items():
            v = q.get(code)
            if not v or v.get("date") != today:
                continue
            p = v["last"]
            why = _due(last, ("stop", cid, code), bool(x["stop"] and p <= x["stop"]), now)
            if why:
                _alert_once(cid, last, ("stop", cid, code), why,
                            f"🛑 *跌破出場價*　{code} {x['name']}\n成交價 {p:g}，出場價 {x['stop']:g}\n"
                            f"均價 {x['avg']:g}（{(p / x['avg'] - 1) * 100:+.1f}%）\n要賣請按下方按鈕", code, x["name"], now)
            if x["key"] and not x["added"] and p >= x["key"] and ("add", cid, code) not in sent:
                sent.add(("add", cid, code))
                send_to(cid, f"⬆️ *站上前波高點*　{code} {x['name']}\n成交價 {p:g}，前高 {x['key']:g}\n"
                             f"可以加碼第二筆\n加碼後出場價移到 {st.tick(x['key'] * (1 - cfg.ATTACK_STOP2_PCT)):g}\n"
                             f"加碼請按「買進」\n或回覆：買 {code} 1張 價格")
            why = _due(last, ("target", cid, code), bool(x["target"] and p >= x["target"]), now)
            if why:
                _alert_once(cid, last, ("target", cid, code), why,
                            f"🎯 *碰到目標價*　{code} {x['name']}\n成交價 {p:g}，目標 {x['target']:g}\n"
                            f"均價 {x['avg']:g}（{(p / x['avg'] - 1) * 100:+.1f}%）\n要賣請按下方按鈕", code, x["name"], now)
    return len(traded)


def all_books():
    return {cid: load_holdings(cid)[0] for cid in allowed_users()}


def notify_users(text):
    """盤中監控開始／結束，通知每一位持股追蹤的人（靜音，不會響）"""
    for cid in allowed_users():
        try:
            tg("sendMessage", chat_id=str(cid), text=text, disable_notification=True)
        except Exception as e:
            print(f"通知監控狀態失敗（{cid}）：", repr(e))


NOTICE_AT = (8, 58)             # 「盤中監控開始」通知固定在 08:58 左右送出，讓人放心


def _wait_notice_time():
    """如果比 08:58 早啟動（最多提早 20 分鐘），等到 08:58 再通知；手動在別的時間跑就不用等"""
    while True:
        n = datetime.now(TZ)
        gap = (n.replace(hour=NOTICE_AT[0], minute=NOTICE_AT[1], second=0, microsecond=0) - n).total_seconds()
        if not 0 < gap <= 20 * 60:
            return
        time.sleep(min(gap, 20))


def run_market():
    _wait_notice_time()
    t0 = datetime.now(TZ)
    set_monitor_status(f"{STATUS_TAG}已啟動　{t0:%H:%M}")
    notify_users(f"▶️ 盤中監控開始\n{t0:%H:%M} 起每分鐘檢查\n到 13:30 收盤")
    reason = "crash"
    try:
        reason = _run_market() or "close"
    finally:
        t1 = datetime.now(TZ)
        set_monitor_status(f"{STATUS_TAG}已停止　{t1:%H:%M}")
        if reason == "holiday":
            notify_users(f"⏹ 盤中監控結束\n{t1:%H:%M}\n今天沒有開盤")
        elif reason == "crash":
            notify_users(f"⚠️ 盤中監控中斷\n{t1:%H:%M}\n已通知管理者")
            to_owner(f"⚠️ 盤中監控中斷 {t1:%H:%M}\n請到 Actions 看紀錄")
        else:
            notify_users(f"⏹ 盤中監控結束\n{t1:%H:%M} 已收盤")


def _run_market():
    now = datetime.now(TZ)
    today = now.strftime("%Y%m%d")
    watch = load_watch()
    print(f"追價名單 {len(watch)} 檔")
    sent = set()
    last = {}                                                # 持股提醒最近一次送出的時間
    no_trade_checks = 0
    while True:
        now = datetime.now(TZ)
        if (now.hour, now.minute) >= CLOSE_AT:
            break
        process_commands()
        if (now.hour, now.minute) >= OPEN_AT:
            books = all_books()
            n = check_once(watch, books, sent, today, last)
            if n == 0 and (watch or any(books.values())):
                no_trade_checks += 1
                if no_trade_checks >= 15:                    # 開盤 15 分鐘都沒有成交資料 → 今天休市
                    print("今天沒有開盤，結束")
                    return "holiday"
        time.sleep(POLL_SEC)
    for cid, h in all_books().items():                      # 收盤後傳一次持股狀況給每個人
        if h:
            send_to(cid, holdings_report(h, quotes_for(h), "📋 收盤持股"))


if __name__ == "__main__":
    if "--commands" in sys.argv:
        process_commands()
    else:
        run_market()
