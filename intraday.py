"""intraday.py — 盤中監控：追價提醒 + 持股監控 + Telegram 持股指令

每個交易日 08:50 啟動，09:00～13:30 每 1 分鐘查一次證交所即時報價：
  🎯 追價提醒（傳到群組）：明日追價名單上的股票，盤中漲到追價點（漲 4% 且突破盤整高點）
  🛑／⬆️／🎯 持股提醒（只傳給你）：跌破出場價、站上前波高點可加碼、碰到壓力、跌破支撐
同一檔、同一種提醒，一天只推播一次。

持股用 Telegram 私訊機器人輸入。管理者（TG_OWNER_ID）可以開通朋友：
  朋友私訊機器人 → 管理者收到通知 → 回覆「允許 朋友ID 名字」就開通；「移除 ID」取消；「使用者」看名單
  每個人的持股分開存在自己跟機器人的私訊裡，提醒也只傳給本人。
指令：
  追蹤 2330                加入追蹤（未持股，系統自動算價位）
  買 2330 2 950            已持股：2 張、950 元（已在追蹤就加進去，自動算均價）
  買 2330 2 950 出場 900 壓力 1100   自己指定出場價、壓力
  移除 2330                不再追蹤；移除 全部 ＝全部移除
  設 2330 出場 920         修改出場價（也可以改 壓力、支撐、前高）
  持股                     看追蹤清單（未持股與已持股都在這裡）
追蹤清單存在各自跟機器人的私人對話裡（置頂的那則「📌 追蹤清單」），repo 是公開的也看不到。

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
import spike

TZ = ZoneInfo("Asia/Taipei")
OWNER = os.getenv("TG_OWNER_ID", "").strip()
API = f"https://api.telegram.org/bot{TG_TOKEN}"
POLL_SEC = 60
OPEN_AT, CLOSE_AT = (9, 0), (13, 31)
REPEAT_SEC = 30 * 60          # 條件一直成立：每 30 分鐘再提醒一次
TAIL_AT = (13, 15)            # 尾盤：13:15 一定要再提醒一次（盤中監控每分鐘查，所以 13:15～13:16 就會送出）；送過就不再定期提醒
HOLD_TAG = "📌 追蹤清單"
OLD_HOLD_TAG = "📌 持股清單"                     # 舊版的置頂標題：還是讀得懂，下次存檔自動換成新標題
STATUS_TAG = "⏱ 盤中監控"
HIDE_URL = "https://t.me/zhezhe_users?d="      # 使用者名單藏在置頂標題 📌 的連結裡（畫面上看不到）
_HIDE_OK = {"v": True}                           # 藏在連結裡讀不回來時改成 False，退回「點一下才顯示」
RESULT_FILE = Path(__file__).parent / "scan_results.json"
BASE_FILE = Path(__file__).parent / "absorb_base.json"      # 夜間掃描存的全市場基準（20 日均量、昨收）
PRE_FILE = Path(__file__).parent / "absorb_1300.json"       # 13:00 預判通知過的名單（夜間掃描收盤後對照）


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


def send_alert(cid, text, code, name, kind="sell"):
    """提醒下面附一顆按鈕：「移除」進移除流程、「加碼」進已持股的買進流程（數量再問）"""
    label, data = (f"加碼 {code} {name}", f"buy:{code}") if kind == "buy" else (f"移除 {code} {name}", f"sell:{code}")
    row = [{"text": label, "callback_data": data}]
    if kind == "buy":
        row.append({"text": "不加碼", "callback_data": f"skip:{code}"})      # 不想加碼：按一下把這則提醒收掉
    kb = {"inline_keyboard": [row]}
    j = tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML", reply_markup=kb)
    if not j.get("ok"):
        j = tg("sendMessage", chat_id=cid, text=plain(text), reply_markup=kb)
    return (j.get("result") or {}).get("message_id")


def to_group(text):
    for cid in chat_ids():
        tg_send(cid, [text])


# ================= 持股（存在私人對話的置頂訊息） =================
UNHELD_RE = re.compile(r"^▫\s*(\d{4,6})\s+(\S+)\s+未持股$")     # 追蹤中、還沒持股的一張卡
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


# ---------- 訊息美編（HTML：標題粗體、數字放進灰底等寬區塊，跟早報一樣） ----------
def esc(v):
    return html.escape(str(v), quote=False)


def _cells(t):
    """顯示寬度：中文、全形符號算 2 格，其他算 1 格（等寬字對齊用）"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in t)


def pre(lines, pad=4, minw=18):
    """灰底等寬區塊。每行右邊補空白（至少 18 格、再多 4 格），Telegram 在區塊右上角會顯示一個「</>」小圖示，
    不補的話短短一行會被它蓋到字"""
    lines = [str(l) for l in lines]
    w = max(max((_cells(l) for l in lines), default=0) + pad, minw)
    return "<pre>" + "\n".join(esc(l) + " " * (w - _cells(l)) for l in lines) + "</pre>"


def kv(rows, gap=2):
    """[(名稱, 內容)] → 對齊的等寬區塊。名稱欄補空白到一樣寬，內容空的就略過"""
    rows = [(k, v) for k, v in rows if v not in (None, "")]
    w = max((_cells(k) for k, _ in rows), default=0)
    return pre([f"{k}{' ' * (w - _cells(k) + gap)}{v}" for k, v in rows])


def is_html(t):
    return "<b>" in t or "<pre>" in t


def plain(t):
    return html.unescape(re.sub(r"</?(b|pre|code|i)>", "", t))


HEAD_RE = re.compile(r"^(?:🔴|🟢|⚪|▲|▼|－)\s*(\d{4,6})\s+(\S+)\s+([\d,.]+) (張|股)(?:・已持股)?(・已加碼)?$")
FIELD_RES = (("avg", re.compile(r"均價 ([\d,.]+)")), ("stop", re.compile(r"出場 ([\d,.]+|—)")),
             ("target", re.compile(r"(?:壓力|目標) ([\d,.]+|—)")), ("sup", re.compile(r"支撐 ([\d,.]+|—)")),
             ("key", re.compile(r"前高 ([\d,.]+|—)")))
_LASTQ = {}
_LAST_Z = {}     # {代號: (日期, 最近一次成交價)}：證交所有時一瞬間沒回成交價，就用這個
_STATUS = {}    # {chat id: 「⏱ 盤中監控…」那一行}，跟著置頂看板一起保存
LOST_REPLIES = [
    "謝謝你傳訊息給我～😊\n不過哲哲老師看不太懂這句\n請用下方按鍵上的指令唷～",
    "哇～收到你的訊息了！🥰\n可是這句超出老師的功力\n麻煩點下面的按鈕吧～",
    "老師有聽到你說話喔👂\n但只聽得懂按鈕的語言\n請按下面的按鍵～😆",
    "謝謝你陪老師聊天～😄\n可惜老師只會做按鈕上的事\n請往下看看按鍵唷！",
    "咦？這句老師沒學過耶🤔\n下面的按鍵老師最拿手\n請點點看～",
    "你好呀～✨\n老師的字典裡只有按鍵上的字\n請從下面挑一個唷！",
    "哲哲老師努力理解中…💭\n還是失敗了😅\n請按下面的按鍵幫幫老師～",
    "謝謝你的熱情～🔥\n不過老師只接受按鍵指令\n請按下面的按鈕唷！",
    "老師超想幫你的！💪\n但要請你用下面的按鍵跟我說\n這樣我才聽得懂～",
    "收到！📮\n只是這句老師沒辦法處理\n請改用下方按鍵，一下就好囉😊",
    "哎呀～老師的腦袋當機了😵\n請用下面的按鍵重新開機🙏\n謝謝你～",
    "辛苦你打字了～👏\n但老師比較習慣按按鈕\n請點下面的按鍵唷！",
    "老師是按鍵派的🎹\n打字老師會害羞啦😳\n請按下面的按鍵～",
    "這題老師不會，但按鍵會！😎\n請從下面挑一個吧～",
    "感謝你傳訊息給老師～💌\n老師看不懂文字啦😂\n請按下面的按鍵就行～",
    "老師正在上課，只看得懂按鍵📚\n下面的按鈕請你挑一個唷～😉",
]
_LOST_LAST = [None]


def lost_reply():
    """看不懂的輸入：換著花樣、親切地請對方用按鍵（不連續重複同一句）"""
    pool = [t for t in LOST_REPLIES if t != _LOST_LAST[0]]
    _LOST_LAST[0] = random.choice(pool)
    return _LOST_LAST[0]


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
    if not text.startswith((HOLD_TAG, OLD_HOLD_TAG)):
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
        m = UNHELD_RE.match(ln)
        if m:                                        # 未持股：只有價位，沒有張數和均價
            code, name = m.groups()
            cur = {"name": name, "shares": 0, "avg": None, "stop": None, "target": None,
                   "sup": None, "key": None, "added": False, "held": False}
            h[code] = cur
            continue
        m = HEAD_RE.match(ln)
        if m:                                        # 卡片式：一檔一張卡
            code, name, qty, unit, added = m.groups()
            shares = float(qty.replace(",", "")) * (1000 if unit == "張" else 1)
            cur = {"name": name, "shares": int(round(shares)), "avg": None, "stop": None,
                   "target": None, "sup": None, "key": None, "added": bool(added)}
            h[code] = cur
            continue
        m = LINE_RE.match(ln)
        if m:                                        # 舊格式（一檔一行）：照樣讀得懂，下次存檔會自動換成新排版
            code, name, qty, unit, avg, stop, target, key, added = m.groups()
            shares = float(qty.replace(",", "")) * (1000 if unit == "張" else 1)
            h[code] = {"name": name, "shares": int(round(shares)), "avg": float(avg), "stop": _num(stop),
                       "target": _num(target), "sup": None, "key": _num(key), "added": added == "已"}
            cur = None
            continue
        if cur is not None:
            for k, rx in FIELD_RES:
                mm = rx.search(ln)
                if mm:
                    cur[k] = _val(mm.group(1))
    h = {c: x for c, x in h.items() if x["avg"] is not None or not x.get("held", True)}
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
    """追蹤清單（置頂那則也是它）：一檔一張卡，紅漲綠跌；也是機器人讀取追蹤清單的資料來源"""
    q = q or {}
    lines = [f"{title}　{datetime.now(TZ).strftime('%m/%d %H:%M')}"]
    if not h:
        lines += ["", "（目前沒有追蹤的股票）"]
    for code, x in sorted(h.items()):
        p = (q.get(code) or {}).get("last")
        if not x.get("held", True):                  # 未持股：只看現價跟價位，沒有均價和損益
            qq = q.get(code) or {}
            tag = "　延遲" if qq.get("delayed") else "　未成交" if qq.get("notrade") else ""
            now_l = f"成交價 {_n(p)}{tag}" if p else "成交價 —"
            stop_l = f"出場 {_n(x['stop'])}"
            if p and x["stop"]:
                d = (x["stop"] / p - 1) * 100
                stop_l += f"　距出場 {'+' if d >= 0 else '−'}{abs(d):.1f}%"
            lines += ["", f"▫ {code} {x['name']}　未持股", now_l, stop_l]
            if any(x.get(k) is not None for k in ("sup", "target", "key")):
                lines.append(f"支撐 {_n(x.get('sup'))}　壓力 {_n(x['target'])}　前高 {_n(x['key'])}")
            continue
        lamp, now_l, avg_l, stop_l = "－", "成交價 —", f"均價 {_n(x['avg'])}", f"出場 {_n(x['stop'])}"
        if p:
            pct = (p / x["avg"] - 1) * 100
            money = (p - x["avg"]) * x["shares"]
            lamp = "▲" if pct > 0 else "▼" if pct < 0 else "－"
            sg = lambda v: "+" if v >= 0 else "−"
            now_l = f"成交價 {_n(p)}　{sg(pct)}{abs(pct):,.1f}%" + ("　延遲" if (q.get(code) or {}).get("delayed") else "　未成交" if (q.get(code) or {}).get("notrade") else "")
            avg_l += f"　損益 {sg(money)}{abs(money):,.0f} 元"
            if x["stop"]:
                d = (x["stop"] / p - 1) * 100
                stop_l += f"　距出場 {sg(d)}{abs(d):.1f}%"
        lines += ["", f"{lamp} {code} {x['name']}　{fmt_qty(x['shares'])}・已持股{'・已加碼' if x['added'] else ''}",
                  now_l, avg_l, stop_l]
        if any(x.get(k) is not None for k in ("sup", "target", "key")):
            lines.append(f"支撐 {_n(x.get('sup'))}　壓力 {_n(x['target'])}　前高 {_n(x['key'])}")
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
    else:                                            # 新送的看板同時帶著下方按鈕：畫面只剩看板時，按鈕也還在
        kw["reply_markup"] = KEYBOARD_OWNER if str(cid) == str(OWNER) else KEYBOARD
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
    if pm.get("message_id") != me[1] or not (pm.get("text") or "").startswith((HOLD_TAG, OLD_HOLD_TAG)):
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
                if d.attrs.get("errors"):                    # 證交所清單抓不全（常擋國外主機）：改用富果的完整清單
                    fd = fugle_directory()
                    if fd is not None and len(fd) >= len(d):
                        d = fd
                _DIR = d
                if not d.attrs.get("errors"):
                    return
                print("股票清單只抓到一部分，稍後重試：", d.attrs["errors"], flush=True)
            except Exception as e:
                print("股票清單抓取失敗，一分鐘後重試：", repr(e), flush=True)
            time.sleep(60)

    _dir_thread = threading.Thread(target=run, daemon=True)
    _dir_thread.start()


def fugle_directory():
    """用富果的股票清單（上市＋上櫃，一共兩次請求）建出跟 lookup.load_directory 一樣格式的表；沒金鑰或失敗回 None"""
    if not FUGLE_KEY:
        return None
    try:
        import pandas as pd
        rows = []
        for ex, sfx, mk in (("TWSE", ".TW", "上市"), ("TPEx", ".TWO", "上櫃")):
            SRC["fugle_calls"].append(time.time())
            r = requests.get("https://api.fugle.tw/marketdata/v1.0/stock/intraday/tickers",
                             params={"type": "EQUITY", "exchange": ex},
                             headers={"X-API-KEY": FUGLE_KEY}, timeout=15)
            r.raise_for_status()
            for d in r.json().get("data") or []:
                code, name = str(d.get("symbol") or ""), str(d.get("name") or "").strip()
                if code.isdigit() and 4 <= len(code) <= 6 and name:
                    rows.append({"code": code, "name": name, "industry": "", "suffix": sfx, "market": mk})
        if len(rows) < 500:                                  # 太少，當作失敗（正常上市上櫃加起來有 1800 多檔）
            return None
        df = pd.DataFrame(rows).drop_duplicates("code")
        df.attrs["errors"] = []
        print(f"股票清單：改用富果，共 {len(df)} 檔", flush=True)
        return df
    except Exception as e:
        print("富果股票清單失敗：", repr(e), flush=True)
        return None


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
        if _DIR.attrs.get("errors"):
            fd = fugle_directory()
            if fd is not None and len(fd) >= len(_DIR):
                _DIR = fd
    except Exception as e:
        print("股票清單抓取失敗：", repr(e))
        _DIR = fugle_directory()
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


def pick_stock(cid, text, flow):
    """把輸入（代號、中文名稱，可以多檔）轉成代號。回傳 (代號清單, 要回給使用者的話或 None, 要顯示的選項按鈕或 None)"""
    tokens = [t for t in re.split(r"[\s,，、]+", text.strip()) if t]
    codes, notes, rows = [], [], None
    for tk in tokens:
        m = re.match(r"^(\d{4,6})(?:\.(?:TWO|TW))?$", tk, re.I)
        if m:
            codes.append(m.group(1))
            continue
        res = find_stock(tk)
        if res is None:
            return [], "股票清單還在載入中\n請先輸入代號，例：2330\n或過一分鐘再試", None
        if len(res) == 1:
            codes.append(str(res[0]["code"]))
        elif not res:
            notes.append(f"找不到「{tk}」")
        elif len(tokens) == 1:                              # 只輸入一個名稱、有好幾檔：做成按鈕讓他選
            rows = [[f"{r['code']} {r['name']}"] for r in res]
            return [], f"「{tk}」有 {len(res)} 檔\n請按下面要的那一檔", rows
        else:
            notes.append(f"「{tk}」有多檔，請打完整名稱或代號")
    return list(dict.fromkeys(codes)), "\n".join(notes) or None, None


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


def support_for(raw, price, days=5):
    """支撐＝近 5 個交易日裡「有量」（成交量不低於近 20 日平均）的那幾天中，最低的最低價（要低於買價）。
    這 5 天都沒出量，就退而用近 5 日最低點，並標「無量」（參考性較低）。回傳 (價位, 說明)；找不到回 (None, "")"""
    try:
        d = raw.tail(days)
        avg = float(raw["Volume"].tail(20).mean())
        vol = d[(d["Volume"] >= avg) & (d["Low"] < price)]
        if not vol.empty:
            return st.tick(float(vol["Low"].min())), f"近 {days} 日有量的低點"
        low = d[d["Low"] < price]
        if not low.empty:
            return st.tick(float(low["Low"].min())), f"近 {days} 日最低點（無量）"
    except Exception as e:
        print("算支撐失敗：", repr(e))
    return None, ""


def auto_support(code, suffix, price):
    """調價用：抓歷史資料算支撐（失敗回 None）"""
    try:
        import yfinance as yf
        from data import _clean
        for sfx in dict.fromkeys([suffix or ".TW", ".TW", ".TWO"]):
            raw = _clean(yf.Ticker(code + sfx).history(period="1y", auto_adjust=False))
            if raw is not None and len(raw) >= 20:
                return support_for(raw, price)[0]
        return None
    except Exception as e:
        print(f"{code} 抓支撐失敗：{e!r}")
        return None


def _now_price(code, sfx):
    """現在的成交價（查不到、今天還沒成交回 None，這時就不檢查出場價高低）"""
    try:
        v = quotes_quick({code: {"suffix": sfx}}, 5).get(code) or {}
        return None if v.get("notrade") else v.get("last")
    except Exception:
        return None


BUY_STOP_PCT = 0.08            # 買進時系統建議的出場價：成交價下 8%
_LV_CACHE = {}


def levels_for(code, suffix, price):
    """買進時自動算出場價、支撐、壓力、前波高點（同一檔同一價 10 分鐘內不重算，買進流程問出場價時已經算過）"""
    k = (code, suffix, round(float(price), 4))
    hit = _LV_CACHE.get(k)
    if hit and time.time() - hit[0] < 600:
        return dict(hit[1])
    lv = _levels_for(code, suffix, price)
    _LV_CACHE[k] = (time.time(), lv)
    return dict(lv)


def _levels_for(code, suffix, price):
    """有強勢續攻（創高拉回／噴出後整理）的盤整型態 → 照攻擊日規則；沒有 → 出場價＝買價下 8%，壓力留空、前高自動抓；支撐都會算"""
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
                    sup, sup_why = support_for(raw, price)
                    return {"stop": stop, "target": r["target"], "key": st.tick(r["key"]), "sup": sup, "sup_why": sup_why,
                            "why": f"依{cfg.STRATEGY_NAMES[r['name']]}型態", "pattern": True,
                            "pname": cfg.STRATEGY_NAMES[r['name']],
                            "note": f"符合「{cfg.STRATEGY_NAMES[r['name']]}」盤整型態\n出場、壓力、前高\n依攻擊日規則自動算出\n要改請按「調價」"}
    except Exception as e:
        print(f"{code} 計算價位失敗：{e!r}")
    key = volume_key(raw, price) if raw is not None else None
    kline = (f"前高 {key:g}\n（近 65 日量最大那天的最高價）" if key
             else "前高留空\n（近 65 日沒有更高的價位）")
    sup, sup_why = support_for(raw, price) if raw is not None else (None, "")
    return {"stop": st.tick(price * (1 - BUY_STOP_PCT), "ceil"), "target": None, "key": key, "sup": sup, "sup_why": sup_why,
            "why": f"買價下 {BUY_STOP_PCT * 100:.0f}%", "pattern": False,
            "note": "目前不是盤整型態\n（強勢續攻）\n壓力留空\n" + kline + "\n要改請按「調價」"}


# ================= 指令 =================
# 輸入框按「/」會跳出的選單（Telegram 規定指令只能用英文，中文直接打也可以）
MENU = []                      # 不再用輸入框旁的「Menu」選單，改用下方四個按鈕
ALIAS = {"start": "說明", "help": "說明", "指令": "說明", "holdings": "持股", "追蹤清單": "持股", "清單": "持股",
         "track": "追蹤", "remove": "移除", "buy": "買", "set": "設", "quote": "查價", "price": "查價"}
KEYBOARD = {"keyboard": [[{"text": "追蹤"}, {"text": "移除"}], [{"text": "追蹤清單"}, {"text": "調價"}], [{"text": "查價"}, {"text": "說明"}],
                         [{"text": "清畫面"}]],
            "resize_keyboard": True, "is_persistent": True}
KEYBOARD_OWNER = {"keyboard": KEYBOARD["keyboard"][:-1] + [[{"text": "清畫面"}, {"text": "管理者"}]],       # 只有管理者看得到「管理者」按鈕
                  "resize_keyboard": True, "is_persistent": True}
FORMAT = {
    "追蹤": "追蹤格式：追蹤 代號\n例：追蹤 2330\n（未持股，系統自動算價位）\n也可以按下方「追蹤」按鈕",
    "買": "已持股格式：買 代號 數量 價格\n例：買 2330 2張 950\n　　買 2330 500股 950\n可以再加：出場 900 壓力 1100",
    "移除": "移除格式：移除 代號\n或 移除 全部",
    "設": "修改格式：設 代號 出場 價格\n例：設 2330 出場 920\n也可以改：壓力 1100、支撐 900、前高 1000",
}
HELP = ("🤖 *股票追蹤*\n"
        "按下方按鈕就行：\n"
        "追蹤　→ 輸入名稱或代號，系統幫你算價位\n"
        "　　　　可以選已持股，再填張數和成交價\n"
        "移除　→ 不再追蹤這檔\n"
        "追蹤清單　→ 看目前追蹤的股票\n"
        "調價　→ 選股票、調整價位\n"
        "查價　→ 輸入名稱或代號看成交價\n"
        "清畫面　→ 只留釘選的追蹤清單\n"
        "\n"
        "價位碰到時，機器人會私訊你。")


def _opts(words):
    """把「出場 900 壓力 1100 支撐 800 前高 1000」轉成 dict"""
    out = {}
    names = {"出場": "stop", "目標": "target", "壓力": "target", "支撐": "sup", "前高": "key"}
    for k in range(len(words) - 1):
        if words[k] in names:
            try:
                out[names[words[k]]] = float(words[k + 1])
            except ValueError:
                pass
    return out


def _track_opts(words):
    """「出場 87 支撐 無 壓力 100 均價 90 股數 2000」→ {欄位: 數字或 None（無）}"""
    names = {"出場": "stop", "支撐": "sup", "壓力": "target", "目標": "target", "前高": "key", "均價": "均價", "股數": "股數"}
    out = {}
    for k in range(len(words) - 1):
        if words[k] in names:
            v = words[k + 1]
            if v == "無":
                out[names[words[k]]] = None
                continue
            try:
                out[names[words[k]]] = float(v.replace(",", ""))
            except ValueError:
                pass
    return out


def handle(text, h, cid=None):
    """處理一則持股指令（每個人自己的持股），回傳 (回覆文字, 持股是否有變)"""
    m_ = re.match(r"^設\s*(\d{4,6})\s*(出場|目標|壓力|支撐|前高)\s*價?格?\s*[:：]?\s*([\d.,]+)\s*$", text.strip())
    if m_:                                           # 「設6620出場價格123」這種寫法也看得懂
        text = f"設 {m_.group(1)} {m_.group(2)} {m_.group(3).replace(',', '')}"
    w = text.replace("/", " ").replace("，", " ").split()
    if not w:
        return None, False
    cmd = w[0].split("@")[0].lower()                 # /buy@機器人名稱 → buy
    cmd = ALIAS.get(cmd, cmd)
    if cmd == "說明":
        return HELP, False
    if cmd in FORMAT and len(w) < (2 if cmd in ("追蹤", "移除") else 4):
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
        lim = over_limit(cid, h, code) if cid else None
        if lim:
            return limit_msg(lim), False
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
            if x.get("avg") is None:                    # 原本只是追蹤（未持股）：這次變成已持股
                x["avg"], x["shares"], x["held"] = price, 0, True
            x["avg"] = round((x["avg"] * x["shares"] + price * qty) / (x["shares"] + qty), 2)
            x["shares"] += qty
            msg = (f"✅ <b>已加碼　{code} {esc(x['name'])}</b>\n"
                   + kv([("加碼", f"{fmt_qty(qty)} @{_fmt(price)}"), ("共", fmt_qty(x['shares'])), ("均價", _fmt(x['avg']))]))
            if x["key"] and price >= x["key"] and not x["added"]:
                x["added"] = True
                new_stop = st.tick(x["key"] * (1 - cfg.ATTACK_STOP2_PCT))
                if x["stop"] is None or new_stop > x["stop"]:
                    x["stop"] = new_stop
                msg += (f"\n⬆️ <b>突破前高後加碼</b>\n出場價移到前高下 {cfg.ATTACK_STOP2_PCT * 100:.0f}%\n"
                        + kv([("出場", _fmt(x['stop']))]))
            x.update(opt)
        else:
            name, sfx = stock_name_suffix(code)
            if name == code:                            # 股票清單沒載好：改用報價附帶的名稱和上市／上櫃
                qi = quotes_quick({code: {"suffix": None}}, 6).get(code) or {}
                name, sfx = qi.get("name") or code, qi.get("suffix") or sfx
            lv = levels_for(code, sfx, price)
            x = {"name": name, "shares": qty, "avg": price, "stop": lv["stop"], "target": lv["target"], "sup": lv.get("sup"),
                 "key": lv["key"], "added": False, "suffix": sfx, "held": True}
            h[code] = x
            sug = st.tick(price * (1 - BUY_STOP_PCT), "ceil")
            why = (lv["why"] if "stop" not in opt or abs(opt["stop"] - lv["stop"]) < 1e-9
                   else f"買價下 {BUY_STOP_PCT * 100:.0f}%" if abs(opt["stop"] - sug) < 1e-9 else "自己設定")
            x.update(opt)
            risk = (x["stop"] / price - 1) * 100
            msg = (f"✅ <b>已買進　{code} {esc(name)}</b>\n{fmt_qty(qty)} @{_fmt(price)}\n\n"
                   f"🎯 <b>價位</b>\n"
                   + kv([("出場", f"{_fmt(x['stop'])}　{risk:+.1f}%".replace("-", "−")), ("", f"↳ {why}"),
                         ("支撐", _fmt(x.get("sup"))), ("", f"↳ {lv['sup_why']}" if lv.get("sup_why") and x.get("sup") else ""),
                         ("壓力", _fmt(x["target"])),
                         ("前高", _fmt(x["key"]))])
                   + f"\n🧭 <b>策略判斷</b>\n{esc(lv['note'])}")
        return msg, True
    if cmd == "追蹤" and len(w) >= 2:                              # 未持股或已持股；價位可以一起帶入
        code = w[1]
        if code in h:
            return f"{code} {esc(h[code]['name'])} 已經在追蹤清單裡", False
        lim = over_limit(cid, h, code) if cid else None
        if lim:
            return limit_msg(lim), False
        name, sfx = stock_name_suffix(code)
        if name == code:
            qi = quotes_quick({code: {"suffix": None}}, 6).get(code) or {}
            name, sfx = qi.get("name") or code, qi.get("suffix") or sfx
        opt = _track_opts(w[2:])
        avg, qty = opt.pop("均價", None), opt.pop("股數", None)
        if avg is not None and not qty:
            return "已持股要有股數", False
        lv = None
        if any(k not in opt for k in ("stop", "sup", "target", "key")):
            now = _now_price(code, sfx)
            if not now:
                return f"{code} 現在查不到價格\n請稍後再試", False
            lv = levels_for(code, sfx, now)
        def pick(k):
            return opt[k] if k in opt else lv[k]
        x = {"name": name, "shares": int(qty) if avg is not None else 0, "avg": avg,
             "stop": pick("stop"), "target": pick("target"), "sup": pick("sup"), "key": pick("key"),
             "added": False, "suffix": sfx, "held": avg is not None}
        h[code] = x
        p = _now_price(code, sfx)                       # 跟追蹤清單的卡片一樣的排版，讓你一眼對得出來
        card = board_text({code: x}, {code: {"last": p}} if p else {}, None, title="✅ 已加入追蹤")
        msg = card
        if lv:
            msg += f"\n\n🧭 策略判斷\n{lv['note']}"
        return msg + "\n\n要改價位，按「調價」", True
    if cmd == "移除" and len(w) >= 2:                              # 不再追蹤（已持股的也一樣移除）
        if w[1] == "全部":
            if not h:
                return "追蹤清單是空的", False
            names = "\n".join(f"{c} {x['name']}" for c, x in sorted(h.items()))
            h.clear()
            return f"✅ <b>已全部移除</b>\n{pre(names.split(chr(10)))}\n追蹤清單已清空", True
        code = w[1]
        if code not in h:
            return f"追蹤清單裡沒有 {code}", False
        name = h.pop(code)["name"]
        return f"✅ <b>已移除　{code} {esc(name)}</b>\n已從追蹤清單拿掉", True
    if cmd == "設" and len(w) >= 4:
        code = w[1]
        if code not in h:
            return f"追蹤清單裡沒有 {code}", False
        opt = _opts(w[2:])
        if not opt:
            return "格式：設 2330 出場 920\n也可以改：壓力、支撐、前高", False
        h[code].update(opt)
        x = h[code]
        msg = (f"✅ <b>已更新　{code} {esc(x['name'])}</b>\n"
               + kv([("出場", _fmt(x["stop"])), ("支撐", _fmt(x.get("sup"))), ("壓力", _fmt(x["target"])), ("前高", _fmt(x["key"]))]))
        try:                                                   # 出場價已經高於現價 → 當場提醒，不用乾等
            pq = (quotes_for({code: x}).get(code) or {}).get("last")
            if pq and x["stop"] and pq <= x["stop"]:
                msg += f"\n⚠️ <b>現價 {pq:g} 已低於出場價</b>\n盤中監控下一輪（約 1 分鐘內）\n會送出跌破提醒"
        except Exception:
            pass
        return msg, True
    return lost_reply(), False


_OFFSET_FILE = Path("/tmp/tg_offset")


ADMIN_HELP = ("\n\n👑 *管理者說明*\n"
              "朋友私訊機器人後\n"
              "你會收到通知\n"
              "回覆「允許 ID 名字」就開通\n"
              "允許 12345 小明　開通\n"
              "移除 12345　取消\n"
              "使用者　看目前有誰可以用")


# ---------- 每人追蹤檔數的額度（存在名單的名字後面：「小明#3」＝可追蹤 3 檔；沒寫＝1 檔；管理者不限） ----------
_USERS_NOW = {}         # 最近一次讀到的使用者名單（給判斷額度用）


def user_name(v):
    return re.sub(r"#\d+$", "", str(v))


def user_limit(users, uid):
    """可以同時追蹤幾檔；管理者回 None（不限）"""
    if str(uid) == str(OWNER):
        return None
    m = re.search(r"#(\d+)$", str((users or {}).get(str(uid)) or ""))
    return int(m.group(1)) if m else 1


def set_user_limit(users, uid, n):
    nm = user_name(users[uid])
    users[uid] = nm if n == 1 else f"{nm}#{n}"


def limit_msg(n):
    return f"因免費資料庫額度有限😱\n一人僅限追蹤{'一' if n == 1 else n}檔🙏真感謝🙏"


def over_limit(cid, h, code):
    """要新增一檔不在追蹤清單裡的股票，是不是已經滿額度了（未持股與已持股合併計算）；回傳額度（滿了）或 None"""
    if code in h:
        return None
    n = user_limit(_USERS_NOW, cid)
    return n if (n is not None and len(h) >= n) else None


ADMIN = {}              # 管理者正在設定額度：{管理者 ID: {"uid", "name", "mode": approve/adjust, "step": pick/type}}


def admin(text, users):
    """管理者指令。回傳 (回覆, 名單是否有變)；不是管理者指令回傳 (None, False)"""
    w = text.replace("/", " ").split()
    if not w:
        return None, False
    if w[0] == "允許" and len(w) >= 2 and w[1].isdigit():
        users[w[1]] = " ".join(w[2:]) or "朋友"
        send_kb(w[1], "✅ 管理者已開通你\n" + limit_msg(1) + "\n請按下方按鈕開始\n\n" + HELP)
        return f"✅ 已允許 {w[1]} {users[w[1]]}", True
    if w[0] == "移除" and len(w) >= 2:
        if users.pop(w[1], None) is None:
            return f"名單裡沒有 {w[1]}", False
        return f"✅ 已移除 {w[1]}", True
    if w[0] == "使用者":
        if not users:
            return "目前只有你自己在用。\n朋友私訊機器人後\n你會收到通知\n回覆「允許 他的ID」開通", False
        return "👥 可以使用追蹤機器人的人：\n" + "\n".join(f"{u} {user_name(n)}　{user_limit(users, u)} 檔" for u, n in users.items()), False
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


def clear_chat(cid, upto, keep):
    """清畫面：把最近 48 小時內的訊息全刪掉（釘選的持股清單除外）。
    私訊的訊息編號是連號的，所以從現在這則往回刪 400 則，一次 100 則；刪不到的（太舊、已刪）Telegram 會略過"""
    keep = {k for k in keep if k}
    ids = [i for i in range(upto, max(0, upto - 400), -1) if i not in keep]
    for k in range(0, len(ids), 100):
        part = ids[k:k + 100]
        if not tg("deleteMessages", chat_id=cid, message_ids=part).get("ok") and k == 0:
            delete_msgs(cid, [i for i in CHAT.get(cid, []) if i not in keep] + part[:30])   # 一次刪不行就一則一則刪最近的


def send_kb(cid, text, kb=None, html_mode=False):
    """傳訊息並附上鍵盤按鈕（預設是 追蹤／移除／追蹤清單／說明）。html_mode：文字是 HTML。回傳訊息編號"""
    kb = kb or (KEYBOARD_OWNER if cid == OWNER else KEYBOARD)
    if html_mode or is_html(text):
        j = tg("sendMessage", chat_id=cid, text=text, parse_mode="HTML", reply_markup=kb)
        if not j.get("ok"):                              # HTML 出錯就去掉標籤再送
            j = tg("sendMessage", chat_id=cid, text=plain(text), reply_markup=kb)
    else:
        j = tg("sendMessage", chat_id=cid, text=text.replace("*", ""), reply_markup=kb)
    mid = (j.get("result") or {}).get("message_id")
    if mid:
        CHAT.setdefault(cid, []).append(mid)
        LAST_BOT[cid] = mid
        if cid in SESSIONS:
            TRACK.setdefault(cid, []).append(mid)
    return mid


def _kb(rows):
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True, "one_time_keyboard": True}


def _track_rows(s_):
    """追蹤流程目前這一步的按鈕（含建議價）"""
    lv, st_ = s_["lv"], s_["step"]
    if st_ == "t_stop":
        return [[f"用 {_n(lv['stop'])}（出場）"], ["取消"]]
    key = {"t_sup": "sup", "t_target": "target", "t_key": "key"}.get(st_)
    label = {"t_sup": "支撐", "t_target": "壓力", "t_key": "前高"}.get(st_)
    if key:
        sug = lv.get(key)
        return ([[f"用 {_n(sug)}（{label}）"]] if sug else []) + [["略過"], ["取消"]]
    return [["取消"]]


def _track_ask(cid, s_):
    """追蹤流程：問目前這一步（出場、支撐、壓力、前高、持股與否、均價、張數）"""
    st_, lv, now = s_["step"], s_["lv"], s_["now"]
    head = f"{s_['code']} {s_['name']}\n現價 {_n(now)}\n"
    if st_ == "t_stop":
        send_kb(cid, head + f"出場價要設多少？\n建議 {_n(lv['stop'])}（{lv['why']}）\n可以按建議，或輸入數字", _kb(_track_rows(s_)))
    elif st_ in ("t_sup", "t_target", "t_key"):
        label = {"t_sup": "支撐", "t_target": "壓力", "t_key": "前高"}[st_]
        key = {"t_sup": "sup", "t_target": "target", "t_key": "key"}[st_]
        sug = lv.get(key)
        tip = f"建議 {_n(sug)}\n" if sug else "（目前抓不到建議）\n"
        send_kb(cid, head + f"{label}要設多少？\n{tip}可以按建議、輸入數字，\n不要就按「略過」", _kb(_track_rows(s_)))
    elif st_ == "t_held":
        send_kb(cid, head + "要怎麼追蹤？\n僅追蹤：只看價位提醒\n已持股：再填均價和張數", _kb([["僅追蹤"], ["已持股"], ["取消"]]))
    elif st_ == "t_avg":
        send_kb(cid, head + "均價是多少？\n可以按現價，或輸入數字", _kb([[f"用現價 {_n(now)}"], ["取消"]]))
    elif st_ == "t_qty":
        send_kb(cid, head + f"均價 {_n(s_['avg'])}\n持有多少？\n請輸入張數或股數\n例：2張、500股", _kb([["取消"]]))


def _track_cmd(s_, held):
    """把追蹤流程的結果組成一行指令，交給 handle() 去存"""
    o = s_["opt"]
    parts = [s_["code"]]
    for key, lab in (("stop", "出場"), ("sup", "支撐"), ("target", "壓力"), ("key", "前高")):
        parts += [lab, "無" if o.get(key) is None else f"{o[key]:g}"]
    if held:
        parts += ["均價", f"{s_['avg']:g}", "股數", str(s_["qty"])]
    return "追蹤 " + " ".join(parts)


def guided(cid, text, h):
    """按鈕流程：追蹤 → 代號 → 未持股（直接加入）或已持股（數量 → 價格 → 出場價）；移除 → 選股票 → 確認。
    回傳要執行的一行指令（例如「買 2330 2000股 950」），還在問問題時回傳 None"""
    t = text.strip()
    if t == "取消":
        SESSIONS.pop(cid, None)
        delete_msgs(cid, TRACK.pop(cid, []))          # 這一次操作的問答全部清掉
        return "持股"                                  # 取消後回到持股畫面
    if t == "追蹤":
        SESSIONS[cid] = {"flow": "track", "step": "code"}
        send_kb(cid, "要追蹤哪一檔？\n請輸入股票名稱或代號\n例：台積電、2330", _kb([["取消"]]))
        return None
    if t == "移除":
        if not h:
            send_kb(cid, "追蹤清單是空的")
            return None
        SESSIONS[cid] = {"flow": "remove", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["全部移除"], ["取消"]]
        send_kb(cid, "要移除哪一檔？\n移除後不再提醒", _kb(rows))
        return None
    if t == "查價":
        SESSIONS[cid] = {"flow": "quote", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]
        send_kb(cid, "要查哪幾檔？\n輸入名稱或代號，可以多檔\n例：台積電 2317" + ("\n或按下面的追蹤股票" if h else ""), _kb(rows))
        return None
    if t == "調價":
        if not h:
            send_kb(cid, "追蹤清單是空的")
            return None
        SESSIONS[cid] = {"flow": "edit", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]
        send_kb(cid, "要改哪一檔？", _kb(rows))
        return None
    s_ = SESSIONS[cid]
    if s_["flow"] == "remove":
        if s_["step"] == "confirm_all":
            if t == "確定全部移除":
                SESSIONS.pop(cid, None)
                return "移除 全部"
            send_kb(cid, "請按「確定全部移除」\n或打「取消」", _kb([["確定全部移除"], ["取消"]]))
            return None
        if s_["step"] == "confirm":
            if t == "確定移除":
                SESSIONS.pop(cid, None)
                return f"移除 {s_['code']}"
            send_kb(cid, "請按「確定移除」\n或打「取消」", _kb([["確定移除"], ["取消"]]))
            return None
        if t == "全部移除":
            s_["step"] = "confirm_all"
            send_kb(cid, f"確定要移除全部 {len(h)} 檔嗎？", _kb([["確定全部移除"], ["取消"]]))
            return None
        cm = re.search(r"\d{4,6}", t)
        if not cm or cm.group(0) not in h:
            send_kb(cid, "請按下面的股票，或打「取消」",
                    _kb([[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["全部移除"], ["取消"]]))
            return None
        s_["code"], s_["step"] = cm.group(0), "confirm"
        send_kb(cid, f"確定要移除 {s_['code']} {h[s_['code']]['name']} 嗎？", _kb([["確定移除"], ["取消"]]))
        return None
    if s_["flow"] == "quote":
        codes, note, rows = pick_stock(cid, t, "quote")
        if not codes or note:                                 # 有一個認不得就先停下來，不要默默少查一檔
            send_kb(cid, (note + "\n請重新輸入") if note and codes else (note or "請輸入股票名稱或代號\n例：台積電、2330"),
                    _kb((rows or []) + [["取消"]]))
            return None
        SESSIONS.pop(cid, None)
        return "查價 " + " ".join(codes[:10])
    if s_["flow"] == "edit":
        if s_["step"] == "code":
            cm = re.search(r"\d{4,6}", t)
            if not cm or cm.group(0) not in h:
                send_kb(cid, "請按下面的股票，或打「取消」", _kb([[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]))
                return None
            s_["code"], s_["step"] = cm.group(0), "field"
            x = h[s_["code"]]
            send_kb(cid, f"{s_['code']} {x['name']}\n出場價 {_fmt(x['stop'])}\n支撐 {_fmt(x.get('sup'))}\n壓力 {_fmt(x['target'])}\n前高 {_fmt(x['key'])}\n要改哪一個？",
                    _kb([["出場價", "支撐"], ["壓力", "前高"], ["取消"]]))
            return None
        if s_["step"] == "field":
            t = {"目標價": "壓力", "目標": "壓力"}.get(t, t)
            if t not in ("出場價", "支撐", "壓力", "前高"):
                send_kb(cid, "請按下面的按鈕", _kb([["出場價", "支撐"], ["壓力", "前高"], ["取消"]]))
                return None
            s_["field"], s_["step"] = t[:2], "price"
            if t == "前高":
                send_kb(cid, "新的前高是多少？\n輸入數字，例：123\n或按「自動抓」\n（近 65 日量最大那天的最高價）",
                        _kb([["自動抓"], ["取消"]]))
            elif t == "支撐":
                send_kb(cid, "新的支撐是多少？\n輸入數字，例：123\n或按「自動抓」\n（近 5 日有量的低點）", _kb([["自動抓"], ["取消"]]))
            else:
                send_kb(cid, f"新的{t}是多少？\n輸入數字，例：123", _kb([["取消"]]))
            return None
        if s_["field"] == "支撐" and t == "自動抓":
            x = h[s_["code"]]
            v = auto_support(s_["code"], x.get("suffix"), x["avg"] or _now_price(s_["code"], x.get("suffix") or stock_name_suffix(s_["code"])[1]))
            if v is None:
                send_kb(cid, "抓不到合適的支撐\n（近 5 日沒有比均價更低的價）\n請自己輸入數字", _kb([["取消"]]))
                return None
            SESSIONS.pop(cid, None)
            return f"設 {s_['code']} 支撐 {v:g}"
        if s_["field"] == "前高" and t == "自動抓":
            x = h[s_["code"]]
            v = auto_key(s_["code"], x.get("suffix"), x["avg"] or _now_price(s_["code"], x.get("suffix") or stock_name_suffix(s_["code"])[1]))
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
    if s_["flow"] == "track" and s_["step"] in ("t_stop", "t_sup", "t_target", "t_key"):
        field = {"t_stop": "stop", "t_sup": "sup", "t_target": "target", "t_key": "key"}[s_["step"]]
        skip = t == "略過" and field != "stop"
        if skip:
            val = None
        else:
            m_ = re.match(r"^用(?:現價)?\s*([\d,.]+)", t)
            try:
                val = float((m_.group(1) if m_ else t).replace(",", ""))
            except ValueError:
                _track_ask(cid, s_)
                return None
            if field == "stop" and (val <= 0 or val >= s_["now"]):
                send_kb(cid, f"出場價要低於現價 {_n(s_['now'])}\n（不然一設定就會觸發）\n請重新輸入", _kb(_track_rows(s_)))
                return None
            if val <= 0:
                _track_ask(cid, s_)
                return None
        s_["opt"][field] = val
        nxt = {"t_stop": "t_sup", "t_sup": "t_target", "t_target": "t_key", "t_key": "t_held"}[s_["step"]]
        s_["step"] = nxt
        _track_ask(cid, s_)
        return None
    if s_["flow"] == "track" and s_["step"] == "t_held":
        if t == "僅追蹤":
            SESSIONS.pop(cid, None)
            return _track_cmd(s_, held=False)
        if t == "已持股":
            s_["step"] = "t_avg"
            _track_ask(cid, s_)
            return None
        _track_ask(cid, s_)
        return None
    if s_["flow"] == "track" and s_["step"] == "t_avg":
        m_ = re.match(r"^用(?:現價)?\s*([\d,.]+)", t)
        try:
            avg = float((m_.group(1) if m_ else t).replace(",", ""))
        except ValueError:
            avg = None
        if avg is None or avg <= 0:
            _track_ask(cid, s_)
            return None
        s_["avg"], s_["step"] = avg, "t_qty"
        _track_ask(cid, s_)
        return None
    if s_["flow"] == "track" and s_["step"] == "t_qty":
        q = parse_qty(t)
        if q is None or q <= 0:
            _track_ask(cid, s_)
            return None
        s_["qty"] = q
        SESSIONS.pop(cid, None)
        return _track_cmd(s_, held=True)
    if s_["step"] == "code":
        if not code_m:
            codes, note, rows = pick_stock(cid, t, s_["flow"])
            if len(codes) != 1 or note:
                send_kb(cid, (note + "\n請重新輸入") if note and codes else (note or "請輸入股票名稱或代號\n例：台積電、2330"),
                        _kb((rows or []) + [["取消"]]))
                return None
            code_m = re.match(r"(\d+)", codes[0])
        s_["code"] = code_m.group(0)
        lim = over_limit(cid, h, s_["code"])
        if lim:
            SESSIONS.pop(cid, None)
            send_kb(cid, limit_msg(lim))
            return None
        name, sfx = stock_name_suffix(s_["code"])
        now = _now_price(s_["code"], sfx)
        if not now:
            SESSIONS.pop(cid, None)
            send_kb(cid, f"{s_['code']} 現在查不到價格\n請稍後再試")
            return None
        s_.update({"name": name, "sfx": sfx, "now": now, "lv": levels_for(s_["code"], sfx, now), "opt": {}, "step": "t_stop"})
        _track_ask(cid, s_)
        return None
    if s_["step"] == "qty" or s_["step"] == "unit":
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
        s_["qty"], s_["step"] = q, "price"
        now = _now_price(s_["code"], (h.get(s_["code"]) or {}).get("suffix") or stock_name_suffix(s_["code"])[1])
        if now:                                        # 剛好用現價成交的話，按一下就好
            s_["now_btn"] = f"用現價 {_n(now)}"
            send_kb(cid, f"成交價是多少？\n現價 {_n(now)}\n按下方按鈕，或輸入數字", _kb([[s_["now_btn"]], ["取消"]]))
        else:
            send_kb(cid, "成交價是多少？\n輸入數字，例：950", _kb([["取消"]]))
        return None
    if s_["step"] == "price":
        try:
            m_ = re.match(r"^用現價\s*([\d,.]+)", t)
            price = float((m_.group(1) if m_ else t).replace(",", ""))
        except ValueError:
            send_kb(cid, "請輸入數字，例如 950", _kb(([[s_["now_btn"]]] if s_.get("now_btn") else []) + [["取消"]]))
            return None
        if price <= 0:
            send_kb(cid, "價格要大於 0", _kb([["取消"]]))
            return None
        s_["price"] = price
        if s_["code"] in h:                            # 加碼：出場價沿用原本的（突破前高加碼會自動上移）
            SESSIONS.pop(cid, None)
            return f"買 {s_['code']} {s_['qty']}股 {price:g}"
        s_["step"] = "stop"
        sfx = stock_name_suffix(s_["code"])[1]
        now = _now_price(s_["code"], sfx)                 # 出場價要跟「現價」比：股價上漲後可以把出場價拉高鎖利
        sug, base = st.tick(price * (1 - BUY_STOP_PCT), "ceil"), "成交價"
        if now and sug >= now:                             # 買了之後已經跌了：建議價改用現價往下算，才不會一設就被觸發
            sug, base = st.tick(now * (1 - BUY_STOP_PCT), "ceil"), "現價"
        lines = [f"出場價要設多少？", ""] + ([f"現價 {_n(now)}"] if now else []) + [f"建議 {_n(sug)}", f"（{base}下 {BUY_STOP_PCT * 100:.0f}%）"]
        rows = [[f"用 {_n(sug)}（−{BUY_STOP_PCT * 100:.0f}%）"]]
        s_["sug"], s_["now"] = sug, now
        lv = levels_for(s_["code"], sfx, price)
        if lv.get("pattern") and lv["stop"] != sug:
            s_["pat"] = lv["stop"]
            pname = lv.get("pname") or "盤整"
            lines += [f"型態 {_n(lv['stop'])}", f"（符合「{pname}」，盤整低點）"]
            rows.append([f"用 {_n(lv['stop'])}（{pname}）"])
        lines += ["", "按下面的建議", "或直接輸入出場價"]
        s_["rows"] = rows + [["取消"]]
        send_kb(cid, "\n".join(lines), _kb(s_["rows"]))
        return None
    if s_["step"] == "stop":
        m_ = re.match(r"^用\s*([\d,.]+)", t)
        try:
            stop = float((m_.group(1) if m_ else t).replace(",", ""))
        except ValueError:
            send_kb(cid, "請按下面的建議\n或輸入數字", _kb(s_["rows"]))
            return None
        now = _now_price(s_["code"], stock_name_suffix(s_["code"])[1]) or s_.get("now")      # 重新查一次現價
        if stop <= 0 or (now and stop >= now):
            send_kb(cid, ("出場價要大於 0" if stop <= 0 else f"出場價要低於現價 {_n(now)}\n（不然一設定就會觸發）") + "\n請重新輸入", _kb(s_["rows"]))
            return None
        SESSIONS.pop(cid, None)
        return f"買 {s_['code']} {s_['qty']}股 {s_['price']:g} 出場 {stop:g}"
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
    text = {"追蹤清單": "持股", "清單": "持股"}.get(text, text)
    if chat.get("type") in ("group", "supergroup") and text.split("@")[0] in ("/id", "/start"):
        if not OWNER or str((m.get("from") or {}).get("id", "")) != str(OWNER):
            return                                          # 群組裡只有管理者能用 /id，其他人打了不回應
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
    _USERS_NOW.clear()
    _USERS_NOW.update(users)
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

        adm = ADMIN.get(cid)
        if adm:                                       # 正在設定某位朋友的額度（引導：按鈕或自己輸入）
            u = adm["uid"]
            if u not in users:
                ADMIN.pop(cid, None)
            elif text == "取消":
                ADMIN.pop(cid, None)
                tidy()
                MENU_WAIT.pop(cid, None)
                send_kb(cid, f"額度維持 {user_limit(users, u)} 檔　{user_name(users[u])}")
                return
            else:
                n = None
                if adm["step"] == "pick" and text == "自己輸入":
                    adm["step"] = "type"
                    TRACK.setdefault(cid, []).extend([mid0, send_kb(cid, "請輸入可以追蹤幾檔\n（數字，例：5）", _kb([["取消"]]))])
                    return
                mm = re.fullmatch(r"(\d{1,3})\s*檔?", text)
                if mm and 1 <= int(mm.group(1)) <= 100:
                    n = int(mm.group(1))
                if n is None:
                    TRACK.setdefault(cid, []).extend([mid0, send_kb(cid, "請按下面的按鈕\n或輸入 1～100 的數字",
                                                                    _kb([["1檔", "2檔", "3檔"], ["自己輸入"], ["取消"]]) if adm["step"] == "pick" else _kb([["取消"]]))])
                    return
                old = user_limit(users, u)
                set_user_limit(users, u, n)
                ADMIN.pop(cid, None)
                owner[1] = save_holdings(OWNER, owner[0], owner[1], users)
                tidy()
                MENU_WAIT.pop(cid, None)
                send_kb(cid, f"✅ {user_name(users[u])} 可以追蹤 {n} 檔")
                if n != old and (adm["mode"] == "adjust" or n != 1):
                    send_kb(u, f"管理者已設定\n你現在可以追蹤 {n} 檔")
                return
        if text == "管理者":                          # 管理者功能：全部用按鈕操作
            MENU_WAIT[cid] = True
            TRACK[cid] = [mid0, send_kb(cid, "👑 管理者功能", _kb([["待開通名單", "移除使用者"], ["使用者名單", "調整追蹤檔數"], ["取消"]]))]
            return
        if text in ("調整追蹤檔數", "調整額度"):
            MENU_WAIT[cid] = bool(users)
            if not users:
                tidy()
                send_kb(cid, "目前沒有朋友")
            else:
                TRACK.setdefault(cid, []).extend([mid0, send_kb(cid, "要調整誰的額度？",
                                                                _kb([[f"額度 {c} {user_name(n)}（{user_limit(users, c)}檔）"] for c, n in users.items()] + [["取消"]]))])
            return
        mq = re.match(r"^額度\s+(\d+)", text)
        if mq and mq.group(1) in users:
            u = mq.group(1)
            ADMIN[cid] = {"uid": u, "mode": "adjust", "step": "pick"}
            TRACK.setdefault(cid, []).extend([mid0, send_kb(cid, f"{user_name(users[u])} 目前可追蹤 {user_limit(users, u)} 檔\n要改成幾檔？",
                                                            _kb([["1檔", "2檔", "3檔"], ["自己輸入"], ["取消"]]))])
            MENU_WAIT[cid] = True
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
                                                                _kb([[f"{verb} {c} {user_name(n)}"] for c, n in items.items()] + [["取消"]]))])
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
            w_ = text.split()
            if changed and w_[0] == "允許" and len(w_) >= 2 and w_[1] in users:
                ADMIN[cid] = {"uid": w_[1], "mode": "approve", "step": "pick"}
                MENU_WAIT[cid] = True
                send_kb(cid, f"{user_name(users[w_[1]])} 要開放幾檔？\n（不選的話維持 1 檔）", _kb([["1檔", "2檔", "3檔"], ["自己輸入"], ["取消"]]))
            return
    me = get(cid)
    if text in ("清畫面", "清除畫面", "清空"):                    # 畫面只剩一則看板（釘選），不再多送任何提示
        SESSIONS.pop(cid, None)
        MENU_WAIT.pop(cid, None)
        for d in (TRACK, LAST_HELP, LAST_VIEW):
            d.pop(cid, None)
        clear_chat(cid, m.get("message_id") or 0, set())            # 連舊看板也刪，下面重送一則新的（帶按鈕）
        me[1] = save_holdings(cid, me[0], None, owner[2] if cid == OWNER else None, rotate=True)
        CHAT[cid] = []
        return
    if cid in SESSIONS and text in ("持股", "說明"):            # 做到一半按了別的功能：先中止目前這一串
        SESSIONS.pop(cid, None)
    if text in ("追蹤", "移除", "調價", "查價", "取消") or cid in SESSIONS:   # 按鈕一步一步輸入
        if text == "取消" and cid not in SESSIONS:
            TRACK.setdefault(cid, []).append(m.get("message_id"))   # 管理者選單的取消：連同選單一起清掉
        if text in ("追蹤", "移除", "調價", "查價"):
            TRACK[cid] = []                            # 開始新的一次操作
        if cid in SESSIONS or text in ("追蹤", "移除", "調價", "查價"):
            TRACK.setdefault(cid, []).append(m.get("message_id"))
        line = guided(cid, text, me[0])
        if line is None:
            return
        delete_msgs(cid, TRACK.pop(cid, []))            # 做完了：中間的問答清掉，只留最後的結果
        text = line
    _LASTQ.pop("q", None)
    reply, changed = handle(text, me[0], cid)
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
    if (m.get("text") or "").strip() in ("改價位", "價位調整"):   # 舊按鈕名稱照樣看得懂
        m = {**m, "text": "調價"}
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
    """按了提醒訊息下面的按鈕：「移除」跳到確認移除，「加碼」跳到「買了多少」"""
    tg("answerCallbackQuery", callback_query_id=cq.get("id"))
    m = cq.get("message") or {}
    chat = m.get("chat") or {}
    data = cq.get("data") or ""
    if chat.get("type") != "private" or not data.startswith(("sell:", "buy:", "skip:")):
        return
    if data.startswith("skip:"):                       # 不加碼：把這則提醒刪掉就好
        delete_msgs(str(chat["id"]), [m.get("message_id")])
        return
    cid, code = str(chat["id"]), data.split(":", 1)[1]
    h, mid, _ = load_holdings(cid)
    if not h and cid in _BOOK:
        h = _BOOK[cid][0]
    if code not in h:
        send_kb(cid, f"追蹤清單裡已經沒有 {code}")
        return
    delete_msgs(cid, TRACK.pop(cid, []))               # 前一個沒做完的操作清掉
    x = h[code]
    if data.startswith("buy:"):                        # 加碼：代號已經知道，直接問買了多少
        SESSIONS[cid] = {"flow": "buy", "step": "qty", "code": code}
        TRACK[cid] = []
        send_kb(cid, f"{code} {x['name']} 目前 {fmt_qty(x['shares'])}\n加碼買了多少？\n請輸入張數或股數\n例：1張、500股", _kb([["取消"]]))
        return
    SESSIONS[cid] = {"flow": "remove", "step": "confirm", "code": code}
    TRACK[cid] = []
    send_kb(cid, f"確定要移除 {code} {x['name']} 嗎？\n移除後不再提醒", _kb([["確定移除"], ["取消"]]))


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
# ================= 報價來源（依序：證交所 → 富果 → Yahoo 延遲報價） =================
# 證交所即時報價免費又快，但常擋國外雲端主機（GitHub、Render 都在國外）。
# 連不上時改用富果行情 API（免費會員每分鐘 60 次，要在 Secrets／Render 設 FUGLE_API_KEY）；
# 兩個都不行才用 Yahoo（約延遲 20 分，會標示「延遲」）。
FUGLE_KEY = os.environ.get("FUGLE_API_KEY", "").strip()
FUGLE_PER_MIN = 40             # 每個程式每分鐘最多問富果幾次（免費 60 次，GitHub 和 Render 共用同一把金鑰，留餘裕）
TWSE_PAUSE = 300               # 證交所連不上時，先暫停 5 分鐘不去問（不然每次都要等它逾時）
SRC = {"twse_off_until": 0.0, "twse_err": "", "fugle_calls": [], "fugle_err": "", "fugle_off_until": 0.0}


def get_quotes(items, tries=2, gap=2):
    """items：[(code, suffix)]。回傳 {code: {"last", "high", "low", "prev", "date", "name", "suffix", "src"}}。
    另外可能有："notrade"＝今天還沒成交（last 是昨收）、"delayed"＝延遲報價"""
    items = list(dict.fromkeys(items))
    out, todo = {}, list(items)
    for k in range(tries):                                  # ① 證交所
        if not todo or time.time() < SRC["twse_off_until"]:
            break
        out.update(_twse_quotes(todo))
        todo = [(c, s) for c, s in items if c not in out]
        if todo and k < tries - 1 and time.time() >= SRC["twse_off_until"]:
            time.sleep(gap)
    if todo and FUGLE_KEY:                                  # ② 富果
        out.update(_fugle_quotes(todo))
        todo = [(c, s) for c, s in items if c not in out]
    if todo:                                                # ③ Yahoo（延遲）
        out.update(_yahoo_quotes(todo))
    return out


_TWSE_HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
              "Referer": "https://mis.twse.com.tw/stock/index.jsp", "Accept": "application/json, text/javascript, */*; q=0.01",
              "Accept-Language": "zh-TW,zh;q=0.9"}
_TWSE_SESS = {}


def _twse_session(fresh=False):
    """證交所要先打開首頁拿到 cookie，不然常常只回空資料"""
    if fresh:
        _TWSE_SESS.clear()
    if "s" not in _TWSE_SESS:
        try:
            ss = requests.Session()
        except Exception:
            _TWSE_SESS["s"] = requests
            return requests
        try:
            ss.get("https://mis.twse.com.tw/stock/index.jsp", headers=_TWSE_HEAD, timeout=4)
        except Exception:
            pass
        _TWSE_SESS["s"] = ss
    return _TWSE_SESS["s"]


def _twse_raw(ex, fresh=False):
    r = _twse_session(fresh).get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                                 params={"ex_ch": ex, "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                                 headers=_TWSE_HEAD, timeout=4)
    return r.json()


def _twse_fetch(ex):
    """問一次證交所；連不上（逾時、被擋）直接丟出錯誤，回空資料就換新連線再問一次"""
    j = _twse_raw(ex)
    if not j.get("msgArray"):
        j = _twse_raw(ex, fresh=True)
    res = {}
    for q in j.get("msgArray", []):
        def f(k):
            try:
                return float(str(q.get(k, "")).split("_")[0])
            except ValueError:
                return None
        z, notrade = f("z"), False                 # 只用最後一筆成交價，不拿買賣報價
        if z:
            _LAST_Z[q["c"]] = (q.get("d", ""), z)
            last = z
        elif f("o"):                               # 今天有成交，只是這一瞬間沒有新成交（z 是「-」）
            d0, zc = _LAST_Z.get(q["c"], ("", None))
            if d0 != q.get("d", "") or not zc:
                continue                           # 沒記到先前的成交價 → 當作沒查到，交給下一個來源，絕不拿昨收冒充
            last = zc
        else:
            last, notrade = f("y"), True           # 今天還沒有任何成交 → 先放昨收（不會拿來觸發提醒）
        if last:
            vol = f("v")                               # 證交所的 v ＝ 累計成交量（張）
            res[q["c"]] = {"open": f("o"), "vol": vol * 1000 if vol else None, "up": f("u"),
                           "last": last, "high": f("h") or last, "low": f("l") or last, "prev": f("y"), "date": q.get("d", ""),
                           "name": (q.get("n") or "").strip() or None, "notrade": notrade, "src": "證交所",
                           "suffix": {"otc": ".TWO", "emg": ".EMG"}.get(q.get("ex"), ".TW")}
    return res


def _twse_quotes(items):
    """證交所即時報價（上市、上櫃不知道是哪邊就兩邊都問；興櫃另外問）。連不上就暫停 5 分鐘"""
    out, parts = {}, []
    for code, sfx in items:
        if sfx != ".EMG":
            if sfx != ".TWO":
                parts.append(f"tse_{code}.tw")
            if sfx != ".TW":
                parts.append(f"otc_{code}.tw")
    try:
        if parts:
            out.update(_twse_fetch("|".join(parts)))
        emg = [c for c, sfx in items if c not in out and sfx in (None, ".EMG")]
        if emg:
            out.update(_twse_fetch("|".join(f"emg_{c}.tw" for c in emg)))
    except Exception as e:
        SRC["twse_err"] = repr(e)[:80]
        SRC["twse_off_until"] = time.time() + TWSE_PAUSE
        _TWSE_SESS.clear()
        print("證交所報價連不上，5 分鐘內改用其他來源：", SRC["twse_err"])
        return out
    if parts and not out:                          # 一檔都沒回：多半是被擋，也先暫停
        SRC["twse_err"] = "沒有回資料"
        SRC["twse_off_until"] = time.time() + TWSE_PAUSE
        print("證交所沒回任何資料，5 分鐘內改用其他來源")
    return out


def _fugle_budget():
    now = time.time()
    SRC["fugle_calls"] = [t for t in SRC["fugle_calls"] if now - t < 60]
    return len(SRC["fugle_calls"]) < FUGLE_PER_MIN and now >= SRC["fugle_off_until"]


def _fugle_one(code):
    SRC["fugle_calls"].append(time.time())
    r = requests.get(f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/quote/{code}",
                     headers={"X-API-KEY": FUGLE_KEY}, timeout=5)
    if r.status_code == 429:                                   # 超過次數：先停 1 分鐘
        SRC["fugle_off_until"] = time.time() + 60
        raise RuntimeError("富果：超過每分鐘次數")
    if r.status_code in (401, 403):
        SRC["fugle_off_until"] = time.time() + 600
        raise RuntimeError("富果：金鑰錯誤或沒有權限")
    if r.status_code == 404:
        return None                                            # 沒這檔（例如興櫃）
    r.raise_for_status()
    j = r.json()
    lt = j.get("lastTrade") or {}
    last = j.get("closePrice") or lt.get("price") or j.get("lastPrice")
    prev = j.get("previousClose") or j.get("referencePrice")
    notrade = not last
    last = last or prev
    if not last:
        return None
    mk = f"{j.get('exchange', '')} {j.get('market', '')}".upper()
    return {"last": float(last), "high": float(j.get("highPrice") or last), "low": float(j.get("lowPrice") or last),
            "prev": float(prev) if prev else None, "date": str(j.get("date") or "").replace("-", ""),
            "name": j.get("name") or None, "notrade": notrade, "src": "富果",
            "suffix": ".TWO" if ("TPEX" in mk or "OTC" in mk) else ".TW"}


def _fugle_quotes(items):
    """富果行情 API：一次問一檔，照順序問（盤中監控把持股排前面），超過每分鐘額度就先停"""
    out = {}
    for code, sfx in items:
        if sfx == ".EMG":
            continue
        if not _fugle_budget():
            print("富果這一分鐘的額度用完，其餘改用延遲報價")
            break
        try:
            v = _fugle_one(code)
            if v:
                out[code] = v
            SRC["fugle_err"] = ""
        except Exception as e:
            SRC["fugle_err"] = str(e)[:80]
            print("富果報價失敗：", SRC["fugle_err"])
            if time.time() < SRC["fugle_off_until"]:
                break
    return out


def _yahoo_quotes(items):
    """最後手段：Yahoo（台股約延遲 20 分，標示 delayed）"""
    out = {}
    try:
        import yfinance as yf
    except Exception:
        return out
    for code, sfx in items:
        if sfx == ".EMG":
            continue
        for sf in ([sfx] if sfx else [".TW", ".TWO"]):
            try:
                fi = yf.Ticker(code + sf).fast_info
                last = float(fi["last_price"])
            except Exception:
                continue
            out[code] = {"last": last, "high": float(fi.get("day_high") or last), "low": float(fi.get("day_low") or last),
                         "prev": float(fi.get("previous_close") or 0) or None, "src": "Yahoo",
                         "date": datetime.now(TZ).strftime("%Y%m%d"), "name": None, "suffix": sf, "delayed": True}
            break
    return out


def source_problem():
    """盤中監控用：目前報價有沒有問題（回傳要告訴管理者的話，沒問題回空字串）"""
    if time.time() < SRC["twse_off_until"]:
        if not FUGLE_KEY:
            return "證交所報價連不上\n也還沒設定富果金鑰\n目前用延遲約 20 分的報價"
        if SRC["fugle_err"]:
            return f"證交所報價連不上\n富果也失敗：{SRC['fugle_err']}\n目前用延遲約 20 分的報價"
    return ""


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


def _dir_name(code):
    """只從股票清單找名稱（不另外查報價）"""
    try:
        d = _DIR
        if d is not None:
            row = d[d["code"] == code]
            if len(row):
                return str(row.iloc[0]["name"])
    except Exception:
        pass
    return None


def range_pos(last, high, low):
    """成交價在今天高低區間的位置：0＝最低、100＝最高"""
    if not (high and low) or high <= low:
        return None
    return max(0.0, min(100.0, (last - low) / (high - low) * 100))


def range_bar(pos, n=10):
    """低 ━━━●━━━━━━ 高（等寬字，● 是現在的位置）"""
    k = min(n - 1, int(round(pos / 100 * (n - 1))))
    return "低 " + "━" * k + "●" + "━" * (n - 1 - k) + " 高"


def volume_profile(code, last):
    """富果分價量表 → (今天買的人幾 % 賺錢, POC)。POC＝今天成交量最多的價位。沒有金鑰或查不到回 None"""
    if not FUGLE_KEY or not _fugle_budget():
        return None
    try:
        SRC["fugle_calls"].append(time.time())
        r = requests.get(f"https://api.fugle.tw/marketdata/v1.0/stock/intraday/volumes/{code}",
                         headers={"X-API-KEY": FUGLE_KEY}, timeout=4)
        if r.status_code != 200:
            return None
        rows = [(float(d.get("price") or 0), float(d.get("volume") or 0)) for d in r.json().get("data") or []]
        rows = [(pr, v) for pr, v in rows if pr > 0 and v > 0]
        tot = sum(v for _, v in rows)
        if tot <= 0:
            return None
        win = sum(v for pr, v in rows if pr < last)
        poc = max(rows, key=lambda t: (t[1], -abs(t[0] - last)))[0]      # 量一樣多時取離現價近的
        return win / tot * 100, poc
    except Exception as e:
        print(f"{code} 分價量表失敗：{e!r}")
        return None


def quote_report(codes):
    """查價（HTML 格式）：成交價、漲跌、昨收、今日高低、位置條、今天買的人幾 % 賺錢（最多 10 檔）"""
    q = quotes_quick({c: {"suffix": None} for c in codes}, 10)
    esc = lambda v: html.escape(str(v), quote=False)
    out = [f"🔎 <b>查價</b>　{datetime.now(TZ).strftime('%m/%d %H:%M')}"]
    t_end = time.time() + 8                                   # 分價量表最多花 8 秒，超過就不查
    for c in codes:
        x = q.get(c)
        if not x:
            out += ["", f"－ {c}　查不到"]
            continue
        last, prev = x["last"], x.get("prev")
        name = esc(x.get("name") or _dir_name(c) or c)
        if x.get("notrade"):
            out += ["", f"－ <b>{name}</b>　{c}", f"今天還沒成交（昨收 {_n(last)}）"]
            continue
        lamp, chg = "－", ""
        if prev:
            diff = last - prev
            sg = "+" if diff > 0 else "−"
            lamp = "▲" if diff > 0 else "▼" if diff < 0 else "－"
            chg = f"　{sg}{_n(round(abs(diff), 2))}　{sg}{abs(diff / prev * 100):,.2f}%" if diff else "　平盤"
        out += ["", f"{lamp} <b>{name}</b>　{c}", f"<b>{_n(last)}</b>{chg}"]
        rows = ([("昨收", _n(prev))] if prev else []) + [("最高", _n(x.get("high"))), ("最低", _n(x.get("low")))]
        w = max(len(v) for _, v in rows)
        body = [f"{k}  {v:>{w}}" for k, v in rows]
        pos = range_pos(last, x.get("high"), x.get("low"))
        if pos is not None:
            body.append(range_bar(pos))
        out.append(pre(body))
        if pos is not None:
            vp = volume_profile(c, last) if time.time() < t_end else None
            if vp:
                ps, poc = vp
                where = "之上" if last > poc else "之下" if last < poc else "剛好在"
                out.append(f"今天買的人 <b>{ps:.0f}%</b> 賺錢")
                out.append(f"POC <b>{_n(poc)}</b>（成交最集中）")
                out.append(f"現價在 POC {where}" if where != "剛好在" else "現價剛好在 POC")
            else:
                tag = ("高檔" if pos >= 80 else "偏高" if pos >= 60 else "中間" if pos > 40 else "偏低" if pos > 20 else "低檔")
                out.append(f"區間位置 <b>{pos:.0f}%</b>・{tag}")
                out.append("（今日最低＝0%，最高＝100%）")
        if x.get("delayed"):
            out.append("⚠️ 備用來源，約延遲 20 分")
    return "\n".join(out)


def holdings_report(h, q, title="📋 追蹤清單"):
    if not h:
        return "追蹤清單是空的。"
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


def load_absorb():
    """昨晚夜間掃描的恐慌承接名單（只供追蹤：盤中跌破發現日的最低點 ＝ 承接失敗）"""
    try:
        scan = json.loads(RESULT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return [dict(it) for it in (scan.get("absorb") or []) if it.get("low")]


def absorb_scan(today_iso):
    """13:00 恐慌承接預判的計算（12:50～12:59 跑一次）：用盤中開高低現價和累計量套型態規則。
    回傳 {"hits": 全部符合的, "shown": 要通知的, "seen": 檢查幾檔}；沒做成回傳 None"""
    try:
        base = json.loads(BASE_FILE.read_text(encoding="utf-8"))
    except Exception:
        print("13:00 預判：沒有 absorb_base.json（昨晚夜間掃描還沒更新？）")
        return None
    stocks = base.get("stocks") or {}
    if not stocks:
        return None
    if time.time() < SRC["twse_off_until"]:
        to_owner("⚠️ 13:00 恐慌承接預判跳過：證交所報價暫時連不上")
        return None
    codes = list(stocks)
    quotes = {}
    for i in range(0, len(codes), 80):                       # 80 檔一批，避免網址太長
        chunk = codes[i:i + 80]
        parts = [f"{'otc' if stocks[c]['s'] == '.TWO' else 'tse'}_{c}.tw" for c in chunk]
        try:
            quotes.update(_twse_fetch("|".join(parts)))
        except Exception as e:
            print("13:00 預判：證交所連不上", repr(e)[:80])
            SRC["twse_off_until"] = time.time() + TWSE_PAUSE
            to_owner("⚠️ 13:00 恐慌承接預判失敗：證交所報價連不上")
            return None
        time.sleep(0.4)
    hits, seen = [], 0
    for c, q in quotes.items():
        b = stocks.get(c)
        if not b or q.get("notrade") or q.get("date") != today_iso.replace("-", ""):
            continue
        o, h, l, px = q.get("open"), q.get("high"), q.get("low"), q.get("last")
        if not all([o, h, l, px]) or not q.get("vol"):
            continue
        seen += 1
        r = st.check_absorb_live(o, h, l, px, q.get("prev") or b["p"], q["vol"], b["m"], q.get("up"))
        if r is None:                                          # 漲停鎖住、量不夠、型態不對都直接不列
            continue
        hits.append({"code": c, "sfx": b["s"], "name": q.get("name") or b["n"], **r})
    if seen == 0:
        print("13:00 預判：沒有今天的成交資料")
        return None
    hits.sort(key=lambda a: (-st.rank_absorb(a), a["risk"]))
    return {"hits": hits, "shown": hits[:cfg.ABSORB_PRE_MAX], "seen": seen}


def absorb_send(res, now=None):
    """13:00 推播預判結果，並存下通知過的代號（收盤後夜間掃描會對照）。回傳通知的代號清單"""
    if res is None:                                          # 12:50 沒算成（證交所斷線等），13:00 不推播
        return []
    hits, shown = res["hits"], res["shown"]
    if shown:
        lines = [f"🧺 *恐慌承接預判（{(now or datetime.now(TZ)):%H:%M}）*",
                 f"今天殺低後被買回、量已放大的股票（共 {len(hits)} 檔）", ""]
        for a in shown:
            lines.append(f"*{tv(a['code'], a['sfx'])} {a['name']}*　{a['kind']}")
            lines.append(f"現價 {a['close']:g}｜今日最低 {a['low']:g}｜風險 {a['risk'] * 100:.1f}%｜量 {a['vol_mult']:.1f} 倍")
        if len(hits) > len(shown):
            lines.append(f"…另有 {len(hits) - len(shown)} 檔未列出")
        lines += ["", "停損參考＝今日最低點", "13:00 預判，收盤後夜間掃描會確認",
                  "最後 30 分鐘型態可能改變", "沒有追價點，不是買進建議"]
        to_group("\n".join(lines))
    else:
        to_group("🧺 *恐慌承接預判（13:00）*\n目前沒有符合的股票")
    codes = [a["code"] for a in shown]
    try:
        PRE_FILE.write_text(json.dumps({"date": now.strftime("%Y-%m-%d") if now else "", "codes": codes},
                                       ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print("13:00 預判名單存檔失敗", repr(e)[:80])
    return codes


def tv(code, sfx):
    ex = "TPEX" if sfx == ".TWO" else "TWSE"
    return f"[{code}](https://www.tradingview.com/chart/?symbol={ex}%3A{code})"


def _due(last, key, hit, now):
    """持股提醒要不要送：None＝不用、first＝第一次、again＝過了 30 分鐘還成立、tail＝尾盤最後提醒。條件消失就清掉紀錄。
    尾盤（13:15 起）：還成立、而且還沒送過尾盤提醒 → 馬上送一次（不管離上一則多久），優先於 30 分鐘一次的定期提醒；
    送過尾盤提醒之後，就不再定期提醒了
    同一天反彈後又跌回來：不再提醒（reminded＝True）。持續跌破中，才會每 30 分鐘再提醒"""
    rec = last.get(key)
    if not hit:
        if rec is not None:
            rec["reminded"] = True              # 反彈回來了：記住已經提醒過，之後又跌回來不再重複提醒
        return None
    if rec is None:
        return "first"
    if rec.get("reminded"):
        return None                           # 同一天反彈後又跌破：不再提醒（避免一直跳）
    if (now.hour, now.minute) >= TAIL_AT:
        return None if rec["tail"] else "tail"
    if (now - rec["t"]).total_seconds() >= REPEAT_SEC:
        return "again"
    return None


def _alert_once(cid, last, key, why, text, code, name, now):
    """送一則持股提醒（再次提醒時把上一則同種提醒刪掉，不堆積）"""
    old = last.get(key)
    extra = {"again": "還沒處理，再提醒一次", "tail": "快收盤了，最後提醒一次"}.get(why)
    head, _, rest = text.partition("\n")
    mid = send_alert(cid, head + ("\n" + extra if extra else "") + "\n" + rest, code, name)
    if old and old.get("mid") and why != "first":
        delete_msgs(cid, [old["mid"]])
    last[key] = {"t": now, "mid": mid, "reminded": False,
                 "tail": (now.hour, now.minute) >= TAIL_AT or bool(old and old["tail"])}


SPIKE = spike.Tracker()          # 放量急殺（試跑）：每檔的累計量，每分鐘記一筆


def spike_update(q, today, now):
    for code, v in q.items():
        if v.get("date") == today and not v.get("notrade") and not v.get("delayed") and v.get("vol") is not None:
            SPIKE.update(code, now, v["vol"], v["last"])


def spike_check(books, q, today, now, sent):
    """試跑：只看管理者自己的追蹤清單，成立就私訊管理者（每檔每天一次）"""
    hm = (now.hour, now.minute)
    if spike.threshold(hm) is None:
        return
    for cid, h in books.items():
        if str(cid) != str(OWNER):
            continue
        for code, x in h.items():
            v = q.get(code)
            if ("spike", code) in sent or not v or v.get("date") != today or v.get("notrade") or v.get("delayed") or v.get("vol") is None:
                continue
            m = SPIKE.measure(code, now)
            hit = spike.judge(m, hm, x.get("stop"), x.get("sup"))
            if hit:
                sent.add(("spike", code))
                try:
                    to_owner(spike.message(code, x["name"], m, hit, spike.threshold(hm)))
                except Exception as e:
                    print("放量急殺提醒失敗", repr(e)[:120])


def check_once(watch, books, sent, today, last=None, now=None, absorb=None):
    """查一次報價，送出該送的提醒。books：{chat id: 持股}；sent：今天已經送過的 (種類, 對象, 代號)
    absorb：恐慌承接名單（跌破發現日最低＝承接失敗，通知群組一次）"""
    last = {} if last is None else last
    now = now or datetime.now(TZ)
    items = []                                              # 持股排前面：報價額度不夠時，先顧持股
    for h in books.values():
        for code, x in h.items():
            items.append((code, x.get("suffix") or guess_suffix(code)))
    items += [(w["code"], w["suffix"]) for w in watch]
    items += [(a["code"], a.get("suffix") or guess_suffix(a["code"])) for a in (absorb or [])]
    q = get_quotes(list(dict.fromkeys(items)))
    spike_update(q, today, now)
    traded =[v for v in q.values() if v.get("date") == today and not v.get("notrade")]
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
                     f"出場 {w['stop1']:g}｜壓力 {w['target']:g}｜{cfg.STRATEGY_NAMES.get(w['strat'], '')}\n"
                     f"📊 {tv(w['code'], w['suffix'])}")
    for a in (absorb or []):                                # 👀 恐慌承接：跌破發現日最低 ＝ 承接失敗（群組）
        v = q.get(a["code"])
        if not v or v.get("date") != today or v.get("notrade") or ("absorb", "group", a["code"]) in sent:
            continue
        if v["last"] < a["low"]:
            sent.add(("absorb", "group", a["code"]))
            chg = (v["last"] / v["prev"] - 1) * 100 if v.get("prev") else 0
            to_group(f"🧺 *恐慌承接失敗*　{a['code']} {a['name']}\n"
                     f"盤中跌破發現日最低 {a['low']:g}\n"
                     f"成交價 {v['last']:g}（{chg:+.1f}%）\n"
                     f"發現日收盤 {a['close']:g}｜量能 {a['vol_mult']:g} 倍\n"
                     f"📊 {tv(a['code'], a.get('suffix') or guess_suffix(a['code']))}")
    for cid, h in books.items():                            # 持股提醒（各自傳給本人）
        for code, x in h.items():
            v = q.get(code)
            if not v or v.get("date") != today or v.get("notrade"):
                continue                                    # 今天還沒成交：last 是昨收，不拿來觸發提醒
            p = v["last"]
            lag = "\n（備用報價，約延遲 20 分）" if v.get("delayed") else ""
            held = x.get("held", True) and x.get("avg") is not None
            pl = f"{(p / x['avg'] - 1) * 100:+.1f}%".replace("-", "−") if held else ""
            avg_row = [("均價", f"{x['avg']:g}　{pl}")] if held else []
            nm = esc(x["name"])
            why = _due(last, ("stop", cid, code), bool(x["stop"] and p <= x["stop"]), now)
            if why:
                _alert_once(cid, last, ("stop", cid, code), why,
                            f"🛑 <b>跌破出場價</b>　{code} {nm}\n"
                            + kv([("成交價", f"{p:g}"), ("出場價", f"{x['stop']:g}")] + avg_row)
                            + f"{lag}\n請按下方按鈕處理", code, x["name"], now)
            if x["key"] and not x["added"] and p >= x["key"] and ("add", cid, code) not in sent and not held:
                sent.add(("add", cid, code))                 # 未持股：只提醒站上前高，沒有加碼按鈕
                send_kb(cid, f"⬆️ <b>站上前高</b>　{code} {nm}\n"
                        + kv([("成交價", f"{p:g}"), ("前高", f"{x['key']:g}")])
                        + f"\n未持股，可以考慮進場", html_mode=True)
            elif x["key"] and not x["added"] and p >= x["key"] and ("add", cid, code) not in sent:
                sent.add(("add", cid, code))
                send_alert(cid, f"⬆️ <b>站上前波高點</b>　{code} {nm}\n"
                           + kv([("成交價", f"{p:g}"), ("前高", f"{x['key']:g}"),
                                 ("加碼後出場", f"{st.tick(x['key'] * (1 - cfg.ATTACK_STOP2_PCT)):g}")])
                           + "\n可以加碼第二筆", code, x["name"], kind="buy")
            sup = x.get("sup")                              # 跌破支撐：先預警；已經跌破出場價的話 🛑 那則就夠了
            stop_fired = ("stop", cid, code) in last        # 今天已經跌破出場價提醒過：支撐不再另外提醒
            why = _due(last, ("sup", cid, code), bool(sup and p <= sup and not stop_fired and not (x["stop"] and p <= x["stop"])), now)
            if why:
                _alert_once(cid, last, ("sup", cid, code), why,
                            f"⚠️ <b>跌破支撐</b>　{code} {nm}\n"
                            + kv([("成交價", f"{p:g}"), ("支撐", f"{sup:g}"), ("出場價", _fmt(x["stop"]))] + avg_row)
                            + f"{lag}\n還沒到出場價，先預警\n請按下方按鈕處理", code, x["name"], now)
            why = _due(last, ("target", cid, code), bool(x["target"] and p >= x["target"]), now)
            if why:
                _alert_once(cid, last, ("target", cid, code), why,
                            f"🎯 <b>碰到壓力</b>　{code} {nm}\n"
                            + kv([("成交價", f"{p:g}"), ("壓力", f"{x['target']:g}")] + avg_row)
                            + f"{lag}\n請按下方按鈕處理", code, x["name"], now)
    spike_check(books, q, today, now, sent)
    return len(traded)


def all_books():
    return {cid: load_holdings(cid)[0] for cid in allowed_users()}


def notify_users(text):
    """盤中監控開始／結束，通知每一位使用追蹤功能的人（靜音，不會響）"""
    for cid in allowed_users():
        try:
            tg("sendMessage", chat_id=str(cid), text=text, disable_notification=True,
               reply_markup=KEYBOARD_OWNER if str(cid) == str(OWNER) else KEYBOARD)     # 順便把下方按鈕補回來（對話被刪光時用）
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
    t = datetime.now(TZ)
    if (t.hour, t.minute) >= CLOSE_AT:                       # GitHub 排程有時延遲到收盤後才叫起來：什麼都不做、不打擾任何人
        print(f"{t:%H:%M} 已經收盤，不啟動盤中監控（排程延遲）")
        return
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
    absorb = load_absorb()
    print(f"追價名單 {len(watch)} 檔｜恐慌承接 {len(absorb)} 檔")
    sent = set()
    last = {}                                                # 持股提醒最近一次送出的時間
    no_trade_checks = 0
    pre_res, pre_sent = None, False                           # 12:50 算好、13:00 推播
    pre_scanned = False
    warned, bad = [datetime(2000, 1, 1, tzinfo=TZ)], [0]
    while True:
        now = datetime.now(TZ)
        if (now.hour, now.minute) >= CLOSE_AT:
            break
        process_commands()
        hm = (now.hour, now.minute)
        if not pre_scanned and cfg.ABSORB_PRE_SCAN <= hm < cfg.ABSORB_PRE_AT:
            pre_scanned = True                                # 12:50～12:59 只算一次
            try:
                pre_res = absorb_scan(now.strftime("%Y-%m-%d"))
            except Exception as e:
                print("13:00 預判計算出錯", repr(e)[:120])
        if pre_res is not None and not pre_sent and cfg.ABSORB_PRE_AT <= hm < (13, 20):
            pre_sent = True                                   # 13:00 推播；失敗不影響其他監控
            try:
                absorb_send(pre_res, now)
            except Exception as e:
                print("13:00 預判推播出錯", repr(e)[:120])
        if (now.hour, now.minute) >= OPEN_AT:
            books = all_books()
            n = check_once(watch, books, sent, today, last, absorb=absorb)
            prob = source_problem()
            bad[0] = bad[0] + 1 if prob else 0
            if bad[0] >= 3 and (now - warned[0]).total_seconds() >= 1800:
                warned[0] = now                              # 連續 3 分鐘只有延遲報價：通知管理者
                to_owner(f"⚠️ 報價來源有問題　{now:%H:%M}\n{prob}\n停損提醒可能慢")
            if n == 0 and (watch or any(books.values())):
                no_trade_checks += 1
                if no_trade_checks >= 15:                    # 開盤 15 分鐘都沒有成交資料 → 今天休市
                    print("今天沒有開盤，結束")
                    return "holiday"
        time.sleep(POLL_SEC)
    for cid, h in all_books().items():                      # 收盤後傳一次持股狀況給每個人
        if h:
            send_to(cid, holdings_report(h, quotes_for(h), "📋 收盤追蹤清單"))


if __name__ == "__main__":
    if "--commands" in sys.argv:
        process_commands()
    else:
        run_market()
