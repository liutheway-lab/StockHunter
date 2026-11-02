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
import json
import os
import re
import sys
import time
import traceback
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
HOLD_TAG = "📌 持股清單"
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


def to_group(text):
    for cid in chat_ids():
        tg_send(cid, [text])


# ================= 持股（存在私人對話的置頂訊息） =================
LINE_RE = re.compile(r"^(\d{4,6})\s+(\S+)｜([\d,.]+) (張|股)｜均價 ([\d.]+)｜出場 ([\d.]+|—)｜目標 ([\d.]+|—)｜前高 ([\d.]+|—)｜加碼 (未|已)$")   # 舊格式（一檔一行）
HEAD_RE = re.compile(r"^(\d{4,6})\s+(\S+)｜([\d,.]+) (張|股)$")                                                           # 新格式標題行
FIELD_RE = re.compile(r"(均價|出場|目標|前高|加碼) ([\d.]+|—|未觸發|已完成)")
TRAIL_RE = re.compile(r"高點回測 ([\d.]+)%")                    # 移動停利：「　移動停利 高點回測 8%」


def stop_tag(x):
    """出場價低於均價 → 停損；高於均價 → 保護獲利"""
    if x.get("stop") is None or not x.get("avg"):
        return ""
    return "（保護獲利）" if x["stop"] > x["avg"] else "（停損）"


_QCACHE = {}   # {代號: (查價時間, 現價)}
_LVCACHE = {}  # {(代號, 買價): (時間, 價位)}


def ref_price(code, fallback=None):
    """目前市價，給按鈕畫面用：最多等 4 秒、不走 Yahoo 備援、30 秒內重複查直接用快取；
    查不到就用 fallback，寧可少一顆按鈕也不要讓畫面卡住"""
    c = _QCACHE.get(code)
    if c and time.time() - c[0] < 30:
        return c[1]
    try:
        v = get_quotes([(code, stock_name_suffix(code)[1])], timeout=4, yahoo=False).get(code)
        if v:
            _QCACHE[code] = (time.time(), v["last"])
            return v["last"]
    except Exception as e:
        print(f"{code} 查現價失敗：{e!r}")
    return fallback


def levels_cached(code, sfx, price):
    """levels_for 要下載一年的股價，比較慢；同一檔、同一個買價 10 分鐘內只算一次"""
    k = (code, round(price, 2))
    c = _LVCACHE.get(k)
    if c and time.time() - c[0] < 600:
        return c[1]
    lv = levels_for(code, sfx, price)
    _LVCACHE[k] = (time.time(), lv)
    return lv


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


def load_holdings(cid):
    """讀某個人私訊裡置頂的持股清單。回傳 (持股 dict, 置頂訊息 id, 使用者名單 dict)；使用者名單只存在管理者那則"""
    if not cid:
        return {}, None, {}
    j = tg("getChat", chat_id=cid)
    pm = (j.get("result") or {}).get("pinned_message") or {}
    text = pm.get("text") or ""
    if not text.startswith(HOLD_TAG):
        return {}, None, {}
    h, users = {}, {}
    cur = None                                       # 新格式：一檔分三行，標題行開頭、後面兩行接著補欄位
    for ln in text.splitlines()[1:]:
        ln = ln.strip()
        if ln.startswith(USERS_TAG):
            users = {m.group(1): m.group(2).strip() for m in USER_RE.finditer(ln[len(USERS_TAG):])}
            cur = None
            continue
        m = LINE_RE.match(ln)                        # 舊格式（一檔一行）：還是讀得懂，不用重建持股
        if m:
            code, name, qty, unit, avg, stop, target, key, added = m.groups()
            shares = float(qty.replace(",", "")) * (1000 if unit == "張" else 1)
            h[code] = {"name": name, "shares": int(round(shares)), "avg": float(avg), "stop": _num(stop),
                       "target": _num(target), "key": _num(key), "added": added == "已", "trail": None}
            cur = None
            continue
        m = HEAD_RE.match(ln)
        if m:
            code, name, qty, unit = m.groups()
            shares = float(qty.replace(",", "")) * (1000 if unit == "張" else 1)
            cur = h[code] = {"name": name, "shares": int(round(shares)), "avg": None, "stop": None,
                             "target": None, "key": None, "added": False, "trail": None}
            continue
        if cur is not None:
            mt = TRAIL_RE.search(ln)
            if mt:
                cur["trail"] = float(mt.group(1))
            for k, v in FIELD_RE.findall(ln):
                if k == "加碼":
                    cur["added"] = v.startswith("已")
                else:
                    cur[{"均價": "avg", "出場": "stop", "目標": "target", "前高": "key"}[k]] = _num(v)
    return h, pm.get("message_id"), users


RULE = "━━━━━━━━━━━━"


def holdings_text(h, users=None):
    """一檔三行（每行都短，手機上不會被切成兩半）；使用者名單前空兩行"""
    lines = [HOLD_TAG, "（自動維護，請勿刪除或取消置頂）", RULE]
    for code, x in sorted(h.items()):
        lines += ["",
                  f"{code} {x['name']}｜{x['shares']:,} 股",
                  f"　均價 {_fmt(x['avg'])}｜出場 {_fmt(x['stop'])}{stop_tag(x)}",
                  f"　目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}｜加碼 {'已完成' if x['added'] else '未觸發'}"]
        if x.get("trail"):
            lines.append(f"　移動停利 高點回測 {x['trail']:g}%")
    if not h:
        lines += ["", "（目前沒有持股）"]
    lines += ["", RULE]
    if users:
        lines += ["", "", USERS_TAG + "，".join(f"{uid} {name}" for uid, name in users.items())]
    return "\n".join(lines)


def save_holdings(cid, h, msg_id, users=None):
    text = holdings_text(h, users)
    if msg_id:
        j = tg("editMessageText", chat_id=cid, message_id=msg_id, text=text)
        if j.get("ok") or "not modified" in str(j):
            return msg_id
    j = tg("sendMessage", chat_id=cid, text=text)
    mid = (j.get("result") or {}).get("message_id")
    if mid:
        tg("pinChatMessage", chat_id=cid, message_id=mid, disable_notification=True)
    return mid


def allowed_users():
    """可以使用持股追蹤的人：{chat id: 名字}。管理者（TG_OWNER_ID）＋管理者允許的朋友"""
    if not OWNER:
        return {}
    _, _, users = load_holdings(OWNER)
    return {OWNER: "管理者", **users}


# ================= 股票資料 =================
_DIR = None


def stock_name_suffix(code):
    """(名稱, .TW/.TWO)。官方清單抓不到時名稱用代號"""
    global _DIR
    try:
        if _DIR is None:
            from lookup import load_directory
            _DIR = load_directory()
        row = _DIR[_DIR["code"] == code]
        if len(row):
            return str(row.iloc[0]["name"]), str(row.iloc[0]["suffix"])
    except Exception as e:
        print("股票清單抓取失敗：", repr(e))
    return code, ".TW"


def levels_for(code, suffix, price):
    """買進時自動算出場價、目標價、前波高點：
    有創高拉回／噴後整理的盤整型態 → 照攻擊日規則；沒有 → 出場價＝買價下 10%，目標、前高留空"""
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
                            "note": f"依{cfg.STRATEGY_NAMES[r['name']]}的盤整型態計算"}
    except Exception as e:
        print(f"{code} 計算價位失敗：{e!r}")
    return {"stop": st.tick(price * (1 - cfg.ATTACK_MAX_LOSS), "ceil"), "target": None, "key": None,
            "note": f"這檔目前不符合創高拉回／噴後整理的盤整條件，出場價先設買價下 {cfg.ATTACK_MAX_LOSS * 100:.0f}%，目標、前高留空，可用「設」指令修改"}


# ================= 指令 =================
# 輸入框按「/」會跳出的選單（Telegram 規定指令只能用英文，中文直接打也可以）
MENU = []                      # 不再用輸入框旁的「Menu」選單，改用下方四個按鈕
ALIAS = {"start": "說明", "help": "說明", "指令": "說明", "holdings": "持股", "buy": "買", "sell": "賣", "set": "設"}
KEYBOARD = {"keyboard": [[{"text": "買進"}, {"text": "賣出"}], [{"text": "持股"}, {"text": "改價位"}], [{"text": "說明"}]],
            "resize_keyboard": True, "is_persistent": True}
FORMAT = {
    "買": "買進格式：買 代號 數量 價格\n例：買 2330 2張 950\n　　買 2330 500股 950\n可以再加：出場 900 目標 1100\n移動停利再加：回測 8（高點回測 8%）\n（也可以按下方「買進」按鈕，一步一步輸入）",
    "賣": "賣出格式：賣 代號（全部賣出）\n或 賣 2330 1張、賣 2330 500股",
    "設": "修改格式：設 代號 出場 價格\n例：設 2330 出場 920\n也可以改：目標 1100、前高 1000\n移動停利：設 2330 回測 8（0 ＝ 關閉）",
}
HELP = ("🤖 *持股追蹤*\n"
        "按下方按鈕就行：\n"
        "買進　→ 輸入代號、數量、成交價\n"
        "賣出　→ 選股票、數量\n"
        "持股　→ 看目前持股與即時損益\n"
        "改價位　→ 選股票、改出場價、目標價或移動停利\n"
        "\n"
        "數量可以打「2張」或「500股」。\n"
        "出場、目標價碰到時，機器人會私訊提醒你。")


def _opts(words):
    """把「出場 900 目標 1100 前高 1000」轉成 dict"""
    out = {}
    names = {"出場": "stop", "目標": "target", "前高": "key", "回測": "trail"}
    for k in range(len(words) - 1):
        if words[k] in names:
            try:
                v = float(words[k + 1].rstrip("%％"))
            except ValueError:
                continue
            if names[words[k]] == "trail":
                if not 0 <= v < 100:
                    continue
                v = v or None                           # 回測 0 ＝ 關閉移動停利
            out[names[words[k]]] = v
    return out


def handle(text, h):
    """處理一則持股指令（每個人自己的持股），回傳 (回覆文字, 持股是否有變)"""
    m_ = re.match(r"^設\s*(\d{4,6})\s*(出場|目標|前高|回測)\s*價?格?\s*[:：]?\s*([\d.,]+%?)\s*$", text.strip())
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
    if cmd == "持股":
        return holdings_report(h, quotes_for(h)), False
    if cmd == "買" and len(w) >= 4:
        code = w[1]
        qty = parse_qty(w[2])
        try:
            price = float(w[3])
        except ValueError:
            price = None
        if qty is None or not price or qty <= 0:
            return "數量要寫單位，例如：買 2330 2張 950 或 買 2330 500股 950", False
        opt = _opts(w[4:])
        if code in h:                                   # 加碼：算均價；買在前高之上 → 出場價移到前高下 5%
            x = h[code]
            x["avg"] = round((x["avg"] * x["shares"] + price * qty) / (x["shares"] + qty), 2)
            x["shares"] += qty
            msg = f"✅ 加碼 {code} {x['name']} {fmt_qty(qty)} @{_fmt(price)}\n共 {fmt_qty(x['shares'])}、均價 {_fmt(x['avg'])}"
            if x["key"] and price >= x["key"] and not x["added"]:
                x["added"] = True
                new_stop = st.tick(x["key"] * (1 - cfg.ATTACK_STOP2_PCT))
                if x["stop"] is None or new_stop > x["stop"]:
                    x["stop"] = new_stop
                msg += f"\n突破前高後加碼，出場價移到前高下 {cfg.ATTACK_STOP2_PCT * 100:.0f}%：{_fmt(x['stop'])}"
            x.update(opt)
        else:
            name, sfx = stock_name_suffix(code)
            lv = levels_cached(code, sfx, price)
            x = {"name": name, "shares": qty, "avg": price, "stop": lv["stop"], "target": lv["target"],
                 "key": lv["key"], "added": False, "trail": None, "suffix": sfx}
            x.update(opt)
            h[code] = x
            note = lv["note"]
            if "stop" in opt:                               # 出場價是使用者自己訂的，不要再說「系統先設買價下 10%」
                note = "出場價用你設定的" + ("，目標、前高依盤整型態計算" if lv["target"] else "，目標、前高留空")
            msg = (f"✅ 買進 {code} {name} {fmt_qty(qty)} @{_fmt(price)}\n"
                   f"出場 {_fmt(x['stop'])}｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}\n"
                   + (f"移動停利：高點回測 {x['trail']:g}%\n" if x.get("trail") else "") + f"{note}\n"
                   "價位要改的話：設 " + code + " 出場 價格")
        return msg, True
    if cmd == "賣" and len(w) >= 2:
        code = w[1]
        if code not in h:
            return f"持股裡沒有 {code}", False
        n = h[code]["shares"]
        if len(w) >= 3 and w[2] != "全部":
            n = parse_qty(w[2])
            if n is None:
                return "數量要寫單位，例如：賣 2330 1張 或 賣 2330 500股（全部賣出就打：賣 2330）", False
        name = h[code]["name"]
        h[code]["shares"] -= n
        if h[code]["shares"] <= 0:
            del h[code]
            return f"✅ {code} {name} 全部賣出，已從持股移除", True
        return f"✅ {code} {name} 賣出 {fmt_qty(n)}，剩 {fmt_qty(h[code]['shares'])}", True
    if cmd == "設" and len(w) >= 4:
        code = w[1]
        if code not in h:
            return f"持股裡沒有 {code}", False
        opt = _opts(w[2:])
        if not opt:
            return "格式：設 2330 出場 920（也可以改 目標、前高）", False
        h[code].update(opt)
        x = h[code]
        trail = f"\n移動停利：高點回測 {x['trail']:g}%" if x.get("trail") else "\n移動停利：關閉"
        return (f"✅ {code} {x['name']}\n出場 {_fmt(x['stop'])}{stop_tag(x)}｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}"
                + (trail if "trail" in opt else "")), True
    return "看不懂這個指令。\n" + HELP, False


_OFFSET_FILE = Path("/tmp/tg_offset")


ADMIN_HELP = ("\n\n👑 管理者\n"
              "朋友私訊機器人後，你會收到申請，按下方「允許」或「不允許」就好。\n"
              "「使用者」→ 看名單，也能在那裡移除")

PENDING = {}           # {chat id: 名字}：正在等管理者按「允許／不允許」的申請


def keyboard_for(cid):
    """一般人是四顆主按鈕；管理者平常多一顆「使用者」，有人申請時最上面多出「允許／不允許」"""
    if not OWNER or str(cid) != OWNER:
        return KEYBOARD
    rows = []
    for u, n in PENDING.items():
        rows += [[{"text": f"允許 {u} {n}"}], [{"text": f"不允許 {u} {n}"}]]
    rows += KEYBOARD["keyboard"][:-1] + [[{"text": "說明"}, {"text": "使用者"}]]
    return {**KEYBOARD, "keyboard": rows}


def admin(text, users):
    """管理者指令。回傳 (回覆, 名單是否有變)；不是管理者指令回傳 (None, False)"""
    w = text.replace("/", " ").split()
    if not w:
        return None, False
    if w[0] == "允許" and len(w) >= 2 and w[1].isdigit():
        users[w[1]] = " ".join(w[2:]) or PENDING.get(w[1]) or "朋友"
        PENDING.pop(w[1], None)
        send_kb(w[1], "✅ 管理者已開通你的持股追蹤，請按下方按鈕開始\n\n" + HELP)
        return f"✅ 已允許 {w[1]} {users[w[1]]}", True
    if w[0] == "不允許" and len(w) >= 2 and w[1].isdigit():
        PENDING.pop(w[1], None)
        send_kb(w[1], "管理者暫時沒有開通你的持股追蹤。\n追價提醒、夜間選股結果還是會發在群組裡，不用開通就能看。")
        return f"已不允許 {w[1]}", False
    if w[0] == "移除" and len(w) >= 2:
        if users.pop(w[1], None) is None:
            return f"名單裡沒有 {w[1]}", False
        return f"✅ 已移除 {w[1]}", True
    if w[0] == "使用者":
        if not users:
            return "目前只有你自己在用。\n朋友私訊機器人後，你會收到申請，按「允許」就能開通。", False
        return "👥 可以使用持股追蹤的人：\n" + "\n".join(f"{u} {n}" for u, n in users.items()) + "\n\n要移除的話，按下面那個人", False
    return None, False


SESSIONS = {}          # 按鈕一步一步輸入時，記住每個人目前問到哪一步


CLEAN = {}     # {chat id: {"steps": [這一輪的訊息 id], "last": [上一輪最後一則回覆的 id]}}，用來自動清畫面


def _track(cid):
    return CLEAN.setdefault(str(cid), {"steps": [], "last": []})


def wipe(cid, ids):
    """刪訊息（機器人自己發的、以及私訊裡你打的字都能刪；超過 48 小時的刪不掉）"""
    for mid in ids:
        tg("deleteMessage", chat_id=cid, message_id=mid)


def _tidy(cid, mark):
    """這一輪結束（不在按鈕流程中）→ 把過程中的提示、你打的字全刪掉，只留最後一則回覆；下一輪開始時再刪掉它"""
    c = _track(cid)
    if cid in SESSIONS:
        return
    wipe(cid, c["last"] + c["steps"][:mark])         # 上一輪的結果＋這一輪過程中的提示、你打的字，一次刪光
    c["last"], c["steps"] = c["steps"][mark:], []


def send_kb(cid, text, kb=None):
    """傳訊息並附上鍵盤按鈕（預設是 買進／賣出／持股／說明）。傳出去的訊息會記下來，之後自動清掉"""
    j = tg("sendMessage", chat_id=cid, text=text.replace("*", ""), reply_markup=kb or keyboard_for(cid))
    mid = (j.get("result") or {}).get("message_id")
    if mid:
        _track(cid)["steps"].append(mid)


def _kb(rows):
    return {"keyboard": [[{"text": t} for t in r] for r in rows], "resize_keyboard": True, "one_time_keyboard": True}


BTN = ("系統建議", "保本", "買價下", "現價下", "現價", "均價下", "均價上", "前高")        # 按鈕開頭的字，後面接價位
TRAIL_ROWS = [["不用"], ["5%", "8%", "10%"], ["取消"]]


def _stop_rows(price, sug, ref):
    """買進時出場價的選項：系統建議／保本（買價低於現價才有）／買價下 5%、10%"""
    p5, p10 = st.tick(price * 0.95, "ceil"), st.tick(price * 0.90, "ceil")
    rows = [[f"系統建議 {sug:g}"]]
    if price < ref:
        rows.append([f"保本 {price:g}"])
    return rows + [[f"買價下5% {p5:g}", f"買價下10% {p10:g}"], ["取消"]]


def _pick(t):
    """按鈕文字 → 最後那個數字（「買價下5% 844」→ 844）；不是按鈕就回傳 None（表示是自己打的）"""
    if t.startswith(BTN):
        n = re.findall(r"\d+(?:\.\d+)?", t.replace(",", ""))
        return float(n[-1]) if n else None
    return None


def _buy_rows(h):
    """買進第一步的選項：目前持股（加碼）＋昨晚追價名單，也可以自己打代號"""
    rows, seen = [], set()
    for c, x in sorted(h.items()):
        rows.append([f"{c} {x['name']}"])
        seen.add(c)
    try:
        for w in load_watch()[:6]:
            if w["code"] not in seen:
                rows.append([f"{w['code']} {w['name']}"])
                seen.add(w["code"])
    except Exception as e:
        print("追價名單讀取失敗：", repr(e))
    return rows + [["取消"]]


def guided(cid, text, h):
    """按鈕流程：買進 → 代號 → 數量 → 價格 → 出場價 → 移動停利；賣出 → 選股票 → 數量；改價位。
    每一步都有按鈕可以按，也可以自己打字。回傳要執行的一行指令（例如「買 2330 2000股 950」），還在問問題時回傳 None"""
    t = text.strip()
    if t == "取消":
        SESSIONS.pop(cid, None)
        send_kb(cid, "已取消")
        return None
    if t == "買進":
        SESSIONS[cid] = {"flow": "buy", "step": "code"}
        send_kb(cid, "要買哪一檔？\n按下面的股票，或直接輸入代號（例：2330）", _kb(_buy_rows(h)))
        return None
    if t == "賣出":
        if not h:
            send_kb(cid, "目前沒有持股")
            return None
        SESSIONS[cid] = {"flow": "sell", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]
        send_kb(cid, "要賣哪一檔？", _kb(rows))
        return None
    if t == "改價位":
        if not h:
            send_kb(cid, "目前沒有持股")
            return None
        SESSIONS[cid] = {"flow": "edit", "step": "code"}
        rows = [[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]
        send_kb(cid, "要改哪一檔？", _kb(rows))
        return None
    s_ = SESSIONS[cid]
    if s_["flow"] == "edit":
        if s_["step"] == "code":
            cm = re.search(r"\d{4,6}", t)
            if not cm or cm.group(0) not in h:
                send_kb(cid, "請按下面的股票，或打「取消」", _kb([[f"{c} {x['name']}"] for c, x in sorted(h.items())] + [["取消"]]))
                return None
            s_["code"], s_["step"] = cm.group(0), "field"
            x = h[s_["code"]]
            trail = f"高點回測 {x['trail']:g}%" if x.get("trail") else "關閉"
            send_kb(cid, f"{s_['code']} {x['name']}\n出場價 {_fmt(x['stop'])}{stop_tag(x)}\n目標價 {_fmt(x['target'])}\n"
                         f"移動停利 {trail}\n要改哪一個？", _kb([["出場價", "目標價"], ["移動停利"], ["取消"]]))
            return None
        if s_["step"] == "field":
            if t not in ("出場價", "目標價", "移動停利"):
                send_kb(cid, "請按「出場價」、「目標價」或「移動停利」", _kb([["出場價", "目標價"], ["移動停利"], ["取消"]]))
                return None
            s_["field"], s_["step"] = ("回測" if t == "移動停利" else t[:2]), "price"
            code, x = s_["code"], h[s_["code"]]
            if t == "移動停利":
                send_kb(cid, "高點回測幾 % 就出場？\n股價創新高時，出場價會跟著往上拉（只升不降）。\n"
                             "按下面的選項，或自己輸入數字，例：8", _kb([["關閉"], ["5%", "8%", "10%"], ["取消"]]))
                return None
            ref = ref_price(code)                         # 查不到（或太慢）就用均價當基準，不讓畫面卡住
            base, lab = (ref, "現價") if ref else (x["avg"], "均價")
            if t == "出場價":
                rows = [[f"保本 {x['avg']:g}"]] if ref and x["avg"] < ref else []
                rows += [[f"{lab}下5% {st.tick(base * 0.95, 'ceil'):g}", f"{lab}下10% {st.tick(base * 0.90, 'ceil'):g}"], ["取消"]]
                send_kb(cid, "新的出場價是多少？" + (f"（現價 {ref:g}）" if ref else "") + "\n按下面的選項，或自己輸入價位\n"
                             f"或百分比（例：-5% ＝ {lab}下 5%）", _kb(rows))
            else:
                rows = [[f"前高 {x['key']:g}"]] if x.get("key") else []
                rows += [[f"均價上10% {st.tick(x['avg'] * 1.10):g}", f"均價上20% {st.tick(x['avg'] * 1.20):g}"], ["取消"]]
                send_kb(cid, "新的目標價是多少？\n按下面的選項，或自己輸入價位，例：123", _kb(rows))
            return None
        price = _pick(t)                                  # 按鈕
        if price is None and s_["field"] == "回測" and t == "關閉":
            price = 0.0
        if price is None:                                 # 自己打
            tt = t.replace(",", "").replace(" ", "").replace("％", "%")
            pm = re.fullmatch(r"-?(\d+(?:\.\d+)?)%", tt)
            try:
                if pm and s_["field"] == "出場":           # 百分比 ＝ 現價下 N%
                    pct = float(pm.group(1))
                    base = ref_price(s_["code"], h[s_["code"]]["avg"])
                    price = st.tick(base * (1 - pct / 100), "ceil") if 0 < pct < 100 else None
                else:
                    price = float(tt.replace("%", ""))
            except ValueError:
                price = None
        if price is None:
            send_kb(cid, "請輸入數字，或按選項，例如 123", _kb([["取消"]]))
            return None
        if s_["field"] == "回測" and not 0 <= price < 100:
            send_kb(cid, "回測百分比要在 0～100 之間，例如 8", _kb([["0"], ["取消"]]))
            return None
        if s_["field"] == "出場":                       # 出場價高過現價 ＝ 一設好就立刻觸發，先擋下來
            ref = ref_price(s_["code"])
            if ref and price >= ref:
                send_kb(cid, f"出場價要低於現價 {ref:g}，請重新輸入", _kb([["取消"]]))
                return None
        SESSIONS.pop(cid, None)
        return f"設 {s_['code']} {s_['field']} {price:g}"
    code_m = re.search(r"\d{4,6}", t)
    if s_["step"] == "code":
        if not code_m:
            send_kb(cid, "請輸入股票代號，例如 2330（打「取消」可以中止）", _kb([["取消"]]))
            return None
        s_["code"] = code_m.group(0)
        if s_["flow"] == "sell":
            if s_["code"] not in h:
                send_kb(cid, f"持股裡沒有 {s_['code']}", _kb([["取消"]]))
                return None
            s_["step"] = "qty"
            x = h[s_["code"]]
            send_kb(cid, f"{s_['code']} {x['name']} 目前 {fmt_qty(x['shares'])}\n要賣多少？按「全部」，或輸入數量，記得加單位：「1張」「500股」",
                    _kb([["全部"], ["取消"]]))
            return None
        name, _ = stock_name_suffix(s_["code"])
        s_["step"] = "qty"
        send_kb(cid, f"{s_['code']} {name}\n買了多少？\n請輸入數量，記得加單位：「2張」或「500股」", _kb([["取消"]]))
        return None
    if s_["step"] == "qty":
        if s_["flow"] == "sell" and t == "全部":
            SESSIONS.pop(cid, None)
            return f"賣 {s_['code']}"
        q = parse_qty(t)
        if q is None:
            send_kb(cid, "要寫單位喔：例如「2張」或「500股」", _kb([["取消"]]))
            return None
        if s_["flow"] == "sell":
            SESSIONS.pop(cid, None)
            return f"賣 {s_['code']} {q}股"
        s_["qty"], s_["step"] = q, "price"
        now = ref_price(s_["code"])
        send_kb(cid, "成交價是多少？\n按「現價」，或自己輸入，例：950" if now else "成交價是多少？例：950",
                _kb(([[f"現價 {now:g}"]] if now else []) + [["取消"]]))
        return None
    if s_["step"] == "price":
        price = _pick(t)
        if price is None:
            try:
                price = float(t.replace(",", ""))
            except ValueError:
                send_kb(cid, "請輸入數字，例如 950", _kb([["取消"]]))
                return None
        if s_["code"] in h:                          # 加碼：出場價沿用原本的，不再問
            SESSIONS.pop(cid, None)
            return f"買 {s_['code']} {s_['qty']}股 {price:g}"
        name, sfx = stock_name_suffix(s_["code"])
        sug = levels_cached(s_["code"], sfx, price)["stop"]
        s_["price"], s_["sug"], s_["step"] = price, sug, "stop"
        send_kb(cid, f"{s_['code']} {name} @{price:g}\n出場價要設多少？\n"
                     f"按下面的選項，或自己輸入價位（例：{sug:g}）\n或百分比（例：-8% ＝ 買價下 8%）",
                _kb(_stop_rows(price, sug, ref_price(s_['code'], price))))
        return None
    if s_["step"] == "stop":
        price = s_["price"]
        tt = t.replace(",", "").replace(" ", "").replace("％", "%")
        stop = _pick(t)                                  # 按鈕
        if stop is None:                                 # 自己打：百分比（買價下 N%）或價位
            pm = re.fullmatch(r"-?(\d+(?:\.\d+)?)%", tt)
            try:
                if pm:
                    pct = float(pm.group(1))
                    stop = st.tick(price * (1 - pct / 100), "ceil") if 0 < pct < 100 else None
                else:
                    stop = float(tt)
            except ValueError:
                stop = None
        ref = ref_price(s_["code"], price)               # 出場價只要低於「現價」就行：低於買價是停損，高於買價是保護獲利
        if stop is None or stop <= 0 or stop >= ref:
            send_kb(cid, f"出場價要低於現價 {ref:g}\n請按選項，或輸入價位（例：{s_['sug']:g}）／百分比（例：-8%）",
                    _kb(_stop_rows(price, s_["sug"], ref)))
            return None
        s_["stop"], s_["step"] = stop, "trail"
        kind = f"保護獲利，鎖定 {(stop / price - 1) * 100:+.1f}%" if stop > price else f"停損，最多虧 {(1 - stop / price) * 100:.1f}%"
        send_kb(cid, f"出場價 {stop:g}（{kind}）\n要開啟移動停利嗎？\n股價創新高時，出場價會跟著往上拉（只升不降）。\n"
                     "按下面的選項，或自己輸入高點回測幾 %，例：8", _kb(TRAIL_ROWS))
        return None
    if s_["step"] == "trail":
        pct = None
        if t != "不用":
            try:
                pct = float(t.replace("%", "").replace("％", ""))
            except ValueError:
                pct = -1
            if not 0 < pct < 100:
                send_kb(cid, "請輸入 0～100 之間的數字（例：8），或按選項", _kb(TRAIL_ROWS))
                return None
        SESSIONS.pop(cid, None)
        return (f"買 {s_['code']} {s_['qty']}股 {s_['price']:g} 出場 {s_['stop']:g}"
                + (f" 回測 {pct:g}" if pct else ""))
    SESSIONS.pop(cid, None)
    return None


def handle_message(m, state, asked):
    """處理一則訊息（GitHub 輪詢和 bot_server.py 的即時模式共用）"""
    def get(cid):
        if cid not in state:
            state[cid] = list(load_holdings(cid))
        return state[cid]

    chat = m.get("chat") or {}
    text = (m.get("text") or "").strip()
    if chat.get("type") in ("group", "supergroup") and text.split("@")[0] in ("/id", "/start"):
        send_to(str(chat["id"]), f"這個群組的 ID 是 `{chat['id']}`\n"
                                 "請到 GitHub → Settings → Secrets and variables → Actions\n"
                                 "把 TG\\_CHAT\\_ID 改成這串數字（含前面的負號）")
        return
    if chat.get("type") != "private" or not text:
        return
    cid = str(chat["id"])
    if not OWNER:
        send_to(cid, f"你的 Telegram ID 是 `{cid}`\n"
                     "請到 GitHub → Settings → Secrets and variables → Actions\n"
                     "新增 TG\\_OWNER\\_ID，值填這串數字，之後就能用持股指令")
        return
    owner = get(OWNER)                               # 管理者的置頂訊息裡存著朋友名單
    users = owner[2]
    if cid != OWNER and cid not in users:            # 還沒開通的人：通知管理者
        frm = m.get("from") or {}
        name = (frm.get("first_name") or "") + (f"（@{frm['username']}）" if frm.get("username") else "")
        send_to(cid, f"👋 這是持股追蹤機器人，需要管理者開通才能使用。\n"
                     f"已經把你的申請（ID {cid}）送給管理者，開通後會通知你，請稍等。\n"
                     "不用重複傳訊息。\n\n"
                     "追價提醒、夜間選股結果會發在群組裡，不用開通就能看。")
        if cid not in asked:
            asked.add(cid)
            PENDING[cid] = frm.get("first_name") or frm.get("username") or "朋友"
            tg("sendMessage", chat_id=OWNER, text=f"👤 {name or '有人'} 想使用持股追蹤（ID {cid}）\n請按下方「允許」或「不允許」",
               reply_markup=keyboard_for(OWNER))
        return
    if cid == OWNER:
        reply, changed = admin(text, users)
        if reply:
            if changed:
                owner[1] = save_holdings(OWNER, owner[0], owner[1], users)
            if m.get("message_id"):                      # 你按的那顆按鈕（其實是一則訊息）不用留在畫面上
                tg("deleteMessage", chat_id=OWNER, message_id=m["message_id"])
            kb = None
            if text.strip().lstrip("/") == "使用者" and users:   # 看名單時，每個人一顆「移除」按鈕
                kb = _kb([[f"移除 {u} {n}"] for u, n in users.items()] + [["取消"]])
            send_kb(OWNER, reply, kb)                    # 用 send_kb 傳，順便把鍵盤換回最新的
            return
    me = get(cid)
    c = _track(cid)
    if m.get("message_id"):
        c["steps"].append(m["message_id"])               # 你打的字也記下來，這一輪結束時一起刪
    mark = len(c["steps"])
    try:
        if text in ("買進", "賣出", "改價位", "取消") or cid in SESSIONS:   # 按鈕一步一步輸入
            line = guided(cid, text, me[0])
            if line is None:
                _tidy(cid, mark)
                return
            text = line
        reply, changed = handle(text, me[0])
        if changed:
            me[1] = save_holdings(cid, me[0], me[1], owner[2] if cid == OWNER else None)
        if reply == HELP:
            send_kb(cid, HELP + (ADMIN_HELP if cid == OWNER else ""))
        elif reply:
            send_kb(cid, reply)
    except Exception:                                    # 出錯就回到主選單，不要讓流程卡在半途
        traceback.print_exc()
        SESSIONS.pop(cid, None)
        send_kb(cid, "剛剛出了點問題，已回到主選單，請再試一次。")
    _tidy(cid, mark)


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
    j = tg("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
    ups = j.get("result") or []
    if not ups:
        return
    state, asked = {}, set()
    for u in ups:
        offset = u["update_id"] + 1
        try:
            handle_message(u.get("message") or {}, state, asked)
        except Exception as e:
            print("處理訊息失敗：", repr(e))
    tg("getUpdates", offset=offset, timeout=0)          # 告訴 Telegram 這些訊息處理完了
    _OFFSET_FILE.write_text(str(offset))


# ================= 即時報價 =================
def get_quotes(items, timeout=15, yahoo=True):
    """items：[(code, suffix)]。回傳 {code: {"last", "high", "prev", "date"}}。證交所即時報價，失敗改用 Yahoo"""
    out = {}
    if not items:
        return out
    ex = "|".join(("otc_" if sfx == ".TWO" else "tse_") + f"{code}.tw" for code, sfx in items)
    try:
        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": ex, "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
                         timeout=timeout)
        for q in r.json().get("msgArray", []):
            def f(k):
                try:
                    return float(str(q.get(k, "")).split("_")[0])
                except ValueError:
                    return None
            last = f("z") or f("b") or f("a")
            if last:
                out[q["c"]] = {"last": last, "high": f("h") or last, "low": f("l") or last,
                               "prev": f("y"), "date": q.get("d", "")}
    except Exception as e:
        print("證交所即時報價失敗：", repr(e))
    missing = [(c, s) for c, s in items if c not in out]
    if missing and yahoo:
        try:
            import yfinance as yf
            for code, sfx in missing:
                fi = yf.Ticker(code + sfx).fast_info
                last = float(fi["last_price"])
                out[code] = {"last": last, "high": float(fi.get("day_high") or last),
                             "low": float(fi.get("day_low") or last),
                             "prev": float(fi.get("previous_close") or 0) or None,
                             "date": datetime.now(TZ).strftime("%Y%m%d")}
        except Exception as e:
            print("Yahoo 報價失敗：", repr(e))
    return out


def quotes_for(h):
    return get_quotes([(c, x.get("suffix") or stock_name_suffix(c)[1]) for c, x in h.items()])


def holdings_report(h, q):
    if not h:
        return "目前沒有持股。\n" + HELP
    lines = ["📋 *目前持股*"]
    for code, x in sorted(h.items()):
        p = (q.get(code) or {}).get("last")
        if p:
            pnl = (p / x["avg"] - 1) * 100
            dist = f"，距出場 {(x['stop'] / p - 1) * 100:+.1f}%" if x["stop"] else ""
            money = (p - x["avg"]) * x["shares"]
            lines.append(f"{code} {x['name']} {fmt_qty(x['shares'])}　現價 {p:g}（{pnl:+.1f}%，{money:+,.0f} 元）{dist}")
        else:
            lines.append(f"{code} {x['name']} {fmt_qty(x['shares'])}　均價 {_fmt(x['avg'])}")
        lines.append(f"　均價 {_fmt(x['avg'])}｜出場 {_fmt(x['stop'])}｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}")
    return "\n".join(lines)


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


_SEEN = {}     # {代號: (上一輪的盤中最低, 上一輪的盤中最高)}，用來抓兩次查價之間「碰一下又彈回」的價位


def touched(code, v, level, side):
    """成交價已到價位 → True；或這一輪盤中出現新低（side="down"）／新高（side="up"）而且到了價位 → True。
    第一次看到這檔（剛買進、剛啟動）只看成交價，不翻買進之前的舊帳"""
    p, prev = v["last"], _SEEN.get(code)
    if side == "down":
        return p <= level or bool(prev and v.get("low") and v["low"] < prev[0] and v["low"] <= level)
    return p >= level or bool(prev and v.get("high") and v["high"] > prev[1] and v["high"] >= level)


_PEAK = {}     # {(chat id, 代號): 開始監控後看到的最高價}，移動停利用


def push_stop_updates(upd):
    """移動停利把出場價往上拉之後，存回各人的持股清單。
    先重新讀一次最新持股、只改出場價，避免蓋掉你剛好在這一分鐘下的指令"""
    for cid, d in upd.items():
        h, mid, users = load_holdings(cid)
        changed = False
        for code, s in d.items():
            if code in h and (h[code]["stop"] is None or s > h[code]["stop"]):
                h[code]["stop"] = s
                changed = True
        if changed:
            save_holdings(cid, h, mid, users if cid == OWNER else None)


def check_once(watch, books, sent, today, upd=None):
    """查一次報價，送出該送的提醒。books：{chat id: 持股}；sent：今天已經送過的 (種類, 對象, 代號)；
    upd：收集移動停利調高的出場價 {chat id: {代號: 新出場價}}"""
    items = [(w["code"], w["suffix"]) for w in watch]
    for h in books.values():
        for code, x in h.items():
            items.append((code, x.get("suffix") or stock_name_suffix(code)[1]))
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
                     f"盤中漲到追價點 {w['chase']:g}（最高 {v['high']:g}）\n"
                     f"現價 {v['last']:g}（{chg:+.1f}%）{back}\n"
                     f"出場 {w['stop1']:g}｜目標 {w['target']:g}｜{cfg.STRATEGY_NAMES.get(w['strat'], '')}\n"
                     f"📊 {tv(w['code'], w['suffix'])}")
    for cid, h in books.items():                            # 持股提醒（各自傳給本人）
        for code, x in h.items():
            v = q.get(code)
            if not v or v.get("date") != today:
                continue
            p = v["last"]
            if x.get("trail"):                              # 移動停利：出場價跟著高點往上拉，只升不降
                prev = _SEEN.get(code)
                pk = _PEAK.get((cid, code))
                if pk is None:                              # 第一次看到這筆持股：從現價起算，不採用買進前的高點
                    pk = p
                else:
                    pk = max(pk, p, v["high"] if prev and v.get("high") and v["high"] > prev[1] else 0)
                _PEAK[(cid, code)] = pk
                new_stop = st.tick(pk * (1 - x["trail"] / 100), "ceil")
                if x["stop"] is None or new_stop > x["stop"]:
                    x["stop"] = new_stop
                    if upd is not None:
                        upd.setdefault(cid, {})[code] = new_stop
            if x["stop"] and touched(code, v, x["stop"], "down") and ("stop", cid, code) not in sent:
                sent.add(("stop", cid, code))
                extra = f"\n（兩次查價之間盤中最低曾到 {v['low']:g}）" if p > x["stop"] else ""
                if x.get("trail"):
                    extra += f"\n移動停利：高點 {_PEAK[(cid, code)]:g} 回測 {x['trail']:g}%"
                send_to(cid, f"🛑 *碰到出場價{stop_tag(x)}*　{code} {x['name']}\n現價 {p:g}，出場價 {x['stop']:g}{extra}\n"
                             f"均價 {x['avg']:g}（{(p / x['avg'] - 1) * 100:+.1f}%）\n賣出後回覆：賣 {code}")
            if x["key"] and not x["added"] and touched(code, v, x["key"], "up") and ("add", cid, code) not in sent:
                sent.add(("add", cid, code))
                send_to(cid, f"⬆️ *站上前波高點*　{code} {x['name']}\n現價 {p:g}，前高 {x['key']:g}\n"
                             f"可以加碼第二筆；加碼後出場價移到 {st.tick(x['key'] * (1 - cfg.ATTACK_STOP2_PCT)):g}\n"
                             f"加碼後回覆：買 {code} 數量 價格（例：買 {code} 1張 價格）")
            if x["target"] and touched(code, v, x["target"], "up") and ("target", cid, code) not in sent:
                sent.add(("target", cid, code))
                send_to(cid, f"🎯 *碰到目標價*　{code} {x['name']}\n現價 {p:g}，目標 {x['target']:g}\n"
                             f"均價 {x['avg']:g}（{(p / x['avg'] - 1) * 100:+.1f}%）")
    for code, v in q.items():                               # 記下這一輪的最低／最高，下一輪才看得出「又創新低／新高」
        if v.get("date") == today:
            _SEEN[code] = (v.get("low") or v["last"], v.get("high") or v["last"])
    return len(traded)


def all_books():
    return {cid: load_holdings(cid)[0] for cid in allowed_users()}


def run_market():
    now = datetime.now(TZ)
    today = now.strftime("%Y%m%d")
    watch = load_watch()
    print(f"追價名單 {len(watch)} 檔")
    sent = set()
    no_trade_checks = 0
    while True:
        now = datetime.now(TZ)
        if (now.hour, now.minute) >= CLOSE_AT:
            break
        process_commands()
        if (now.hour, now.minute) >= OPEN_AT:
            books = all_books()
            upd = {}
            n = check_once(watch, books, sent, today, upd)
            push_stop_updates(upd)
            if n == 0 and (watch or any(books.values())):
                no_trade_checks += 1
                if no_trade_checks >= 15:                    # 開盤 15 分鐘都沒有成交資料 → 今天休市
                    print("今天沒有開盤，結束")
                    return
        time.sleep(POLL_SEC)
    for cid, h in all_books().items():                      # 收盤後傳一次持股狀況給每個人
        if h:
            send_to(cid, holdings_report(h, quotes_for(h)).replace("目前持股", "收盤持股"))


if __name__ == "__main__":
    if "--commands" in sys.argv:
        process_commands()
    else:
        run_market()
