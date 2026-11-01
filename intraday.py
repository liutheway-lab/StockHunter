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
LINE_RE = re.compile(r"^(\d{4,6})\s+(\S+)｜([\d.]+) 張｜均價 ([\d.]+)｜出場 ([\d.]+|—)｜目標 ([\d.]+|—)｜前高 ([\d.]+|—)｜加碼 (未|已)$")


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
    for ln in text.splitlines()[1:]:
        ln = ln.strip()
        if ln.startswith(USERS_TAG):
            users = {m.group(1): m.group(2).strip() for m in USER_RE.finditer(ln[len(USERS_TAG):])}
            continue
        m = LINE_RE.match(ln)
        if m:
            code, name, lots, avg, stop, target, key, added = m.groups()
            h[code] = {"name": name, "lots": float(lots), "avg": float(avg), "stop": _num(stop),
                       "target": _num(target), "key": _num(key), "added": added == "已"}
    return h, pm.get("message_id"), users


def holdings_text(h, users=None):
    lines = [HOLD_TAG + "（機器人自動維護，請勿刪除或取消置頂）"]
    for code, x in sorted(h.items()):
        lines.append(f"{code} {x['name']}｜{_fmt(x['lots'])} 張｜均價 {_fmt(x['avg'])}｜出場 {_fmt(x['stop'])}"
                     f"｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}｜加碼 {'已' if x['added'] else '未'}")
    if not h:
        lines.append("（目前沒有持股）")
    if users:
        lines.append(USERS_TAG + "，".join(f"{uid} {name}" for uid, name in users.items()))
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
                if r and "cons_hi" in r:
                    stop, _ = st.attack_stop(r, price)
                    return {"stop": stop, "target": r["target"], "key": st.tick(r["key"]),
                            "note": f"依{cfg.STRATEGY_NAMES[r['name']]}的盤整型態計算"}
    except Exception as e:
        print(f"{code} 計算價位失敗：{e!r}")
    return {"stop": st.tick(price * (1 - cfg.ATTACK_MAX_LOSS), "ceil"), "target": None, "key": None,
            "note": f"找不到盤整型態，出場價先設買價下 {cfg.ATTACK_MAX_LOSS * 100:.0f}%，可用「設」指令修改"}


# ================= 指令 =================
# 輸入框按「/」會跳出的選單（Telegram 規定指令只能用英文，中文直接打也可以）
MENU = [("help", "使用說明"), ("holdings", "查看目前持股"), ("buy", "買進　例：/buy 2330 2 950"),
        ("sell", "賣出　例：/sell 2330"), ("set", "修改價位　例：/set 2330 出場 920")]
ALIAS = {"start": "說明", "help": "說明", "指令": "說明", "holdings": "持股", "buy": "買", "sell": "賣", "set": "設"}
KEYBOARD = {"keyboard": [[{"text": "持股"}, {"text": "說明"}]], "resize_keyboard": True, "is_persistent": True}
FORMAT = {
    "買": "買進格式：買 2330 2 950\n（代號 張數 價格）\n可以再加：出場 900 目標 1100",
    "賣": "賣出格式：賣 2330（全部）\n或 賣 2330 1（賣 1 張）",
    "設": "修改格式：設 2330 出場 920\n也可以改：目標 1100、前高 1000",
}
HELP = ("🤖 *持股指令*（中文直接打，或按輸入框旁的「/」選單）\n"
        "買 2330 2 950　買進 2 張、950 元\n"
        "買 2330 2 950 出場 900 目標 1100\n"
        "賣 2330　全部賣出（賣 2330 1＝賣 1 張）\n"
        "設 2330 出場 920　改出場價（也可改 目標、前高）\n"
        "持股　看目前持股")


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
        try:
            lots, price = float(w[2]), float(w[3])
        except ValueError:
            return "格式：買 2330 2 950（代號 張數 價格）", False
        opt = _opts(w[4:])
        if code in h:                                   # 加碼：算均價；買在前高之上 → 出場價移到前高下 5%
            x = h[code]
            x["avg"] = st.tick((x["avg"] * x["lots"] + price * lots) / (x["lots"] + lots))
            x["lots"] += lots
            msg = f"✅ 加碼 {code} {x['name']} {_fmt(lots)} 張 @{_fmt(price)}，共 {_fmt(x['lots'])} 張、均價 {_fmt(x['avg'])}"
            if x["key"] and price >= x["key"] and not x["added"]:
                x["added"] = True
                new_stop = st.tick(x["key"] * (1 - cfg.ATTACK_STOP2_PCT))
                if x["stop"] is None or new_stop > x["stop"]:
                    x["stop"] = new_stop
                msg += f"\n突破前高後加碼，出場價移到前高下 {cfg.ATTACK_STOP2_PCT * 100:.0f}%：{_fmt(x['stop'])}"
            x.update(opt)
        else:
            name, sfx = stock_name_suffix(code)
            lv = levels_for(code, sfx, price)
            x = {"name": name, "lots": lots, "avg": price, "stop": lv["stop"], "target": lv["target"],
                 "key": lv["key"], "added": False, "suffix": sfx}
            x.update(opt)
            h[code] = x
            msg = (f"✅ 買進 {code} {name} {_fmt(lots)} 張 @{_fmt(price)}\n"
                   f"出場 {_fmt(x['stop'])}｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}\n{lv['note']}")
        return msg, True
    if cmd == "賣" and len(w) >= 2:
        code = w[1]
        if code not in h:
            return f"持股裡沒有 {code}", False
        if len(w) >= 3:
            try:
                n = float(w[2])
            except ValueError:
                n = h[code]["lots"]
        else:
            n = h[code]["lots"]
        name = h[code]["name"]
        h[code]["lots"] -= n
        if h[code]["lots"] <= 0:
            del h[code]
            return f"✅ {code} {name} 全部賣出，已從持股移除", True
        return f"✅ {code} {name} 賣出 {_fmt(n)} 張，剩 {_fmt(h[code]['lots'])} 張", True
    if cmd == "設" and len(w) >= 4:
        code = w[1]
        if code not in h:
            return f"持股裡沒有 {code}", False
        opt = _opts(w[2:])
        if not opt:
            return "格式：設 2330 出場 920（也可以改 目標、前高）", False
        h[code].update(opt)
        x = h[code]
        return f"✅ {code} {x['name']}　出場 {_fmt(x['stop'])}｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}", True
    return "看不懂這個指令。\n" + HELP, False


_OFFSET_FILE = Path("/tmp/tg_offset")


ADMIN_HELP = ("\n\n👑 *管理者指令*\n"
              "允許 12345 小明　讓朋友使用持股追蹤\n"
              "移除 12345　取消\n"
              "使用者　看目前有誰可以用")


def admin(text, users):
    """管理者指令。回傳 (回覆, 名單是否有變)；不是管理者指令回傳 (None, False)"""
    w = text.replace("/", " ").split()
    if not w:
        return None, False
    if w[0] == "允許" and len(w) >= 2 and w[1].isdigit():
        users[w[1]] = " ".join(w[2:]) or "朋友"
        send_to(w[1], "✅ 管理者已開通你的持股追蹤\n" + HELP.replace("*", ""))
        return f"✅ 已允許 {w[1]} {users[w[1]]}", True
    if w[0] == "移除" and len(w) >= 2:
        if users.pop(w[1], None) is None:
            return f"名單裡沒有 {w[1]}", False
        return f"✅ 已移除 {w[1]}", True
    if w[0] == "使用者":
        if not users:
            return "目前只有你自己在用。\n朋友私訊機器人後，你會收到通知，回覆「允許 他的ID」就能開通。", False
        return "👥 可以使用持股追蹤的人：\n" + "\n".join(f"{u} {n}" for u, n in users.items()), False
    return None, False


def process_commands():
    """讀取新訊息並處理。管理者和被允許的朋友可以用持股指令；陌生人私訊時通知管理者決定要不要開通"""
    if not _OFFSET_FILE.exists():                    # 每次啟動設定一次「/」選單（只在私訊顯示）
        tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in MENU],
           scope={"type": "all_private_chats"})
    offset = int(_OFFSET_FILE.read_text()) if _OFFSET_FILE.exists() else None
    j = tg("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
    ups = j.get("result") or []
    if not ups:
        return
    state = {}                                       # 每個人的 (持股, 置頂 id, 名單)，這一輪讀一次就好
    asked = set()

    def get(cid):
        if cid not in state:
            state[cid] = list(load_holdings(cid))
        return state[cid]

    for u in ups:
        offset = u["update_id"] + 1
        m = u.get("message") or {}
        chat = m.get("chat") or {}
        text = (m.get("text") or "").strip()
        if chat.get("type") in ("group", "supergroup") and text.split("@")[0] in ("/id", "/start"):
            send_to(str(chat["id"]), f"這個群組的 ID 是 `{chat['id']}`\n"
                                     "請到 GitHub → Settings → Secrets and variables → Actions\n"
                                     "把 TG\\_CHAT\\_ID 改成這串數字（含前面的負號）")
            continue
        if chat.get("type") != "private" or not text:
            continue
        cid = str(chat["id"])
        if not OWNER:
            send_to(cid, f"你的 Telegram ID 是 `{cid}`\n"
                         "請到 GitHub → Settings → Secrets and variables → Actions\n"
                         "新增 TG\\_OWNER\\_ID，值填這串數字，之後就能用持股指令")
            continue
        owner = get(OWNER)                           # 管理者的置頂訊息裡存著朋友名單
        users = owner[2]
        if cid != OWNER and cid not in users:        # 還沒開通的人：通知管理者
            frm = m.get("from") or {}
            name = (frm.get("first_name") or "") + (f"（@{frm['username']}）" if frm.get("username") else "")
            send_to(cid, "已經通知管理者，開通後就能使用持股追蹤。\n追價提醒、夜間掃描會發在群組裡。")
            if cid not in asked:
                asked.add(cid)
                send_to(OWNER, f"👤 {name or '有人'} 想使用持股追蹤（ID {cid}）\n"
                               f"同意的話回覆：允許 {cid} {frm.get('first_name') or ''}".rstrip())
            continue
        if cid == OWNER:
            reply, changed = admin(text, users)
            if reply:
                if changed:
                    owner[1] = save_holdings(OWNER, owner[0], owner[1], users)
                send_to(OWNER, reply)
                continue
        me = get(cid)
        reply, changed = handle(text, me[0])
        if changed:
            me[1] = save_holdings(cid, me[0], me[1], owner[2] if cid == OWNER else None)
        if reply and reply == HELP:                  # 說明：順便在輸入框下方放「持股」「說明」兩個按鈕
            text_out = (HELP + (ADMIN_HELP if cid == OWNER else "")).replace("*", "")
            tg("sendMessage", chat_id=cid, text=text_out, reply_markup=KEYBOARD)
        elif reply:
            send_to(cid, reply)
    tg("getUpdates", offset=offset, timeout=0)          # 告訴 Telegram 這些訊息處理完了
    _OFFSET_FILE.write_text(str(offset))


# ================= 即時報價 =================
def get_quotes(items):
    """items：[(code, suffix)]。回傳 {code: {"last", "high", "prev", "date"}}。證交所即時報價，失敗改用 Yahoo"""
    out = {}
    if not items:
        return out
    ex = "|".join(("otc_" if sfx == ".TWO" else "tse_") + f"{code}.tw" for code, sfx in items)
    try:
        r = requests.get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                         params={"ex_ch": ex, "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                         headers={"User-Agent": "Mozilla/5.0", "Referer": "https://mis.twse.com.tw/stock/index.jsp"},
                         timeout=15)
        for q in r.json().get("msgArray", []):
            def f(k):
                try:
                    return float(str(q.get(k, "")).split("_")[0])
                except ValueError:
                    return None
            last = f("z") or f("b") or f("a")
            if last:
                out[q["c"]] = {"last": last, "high": f("h") or last, "prev": f("y"), "date": q.get("d", "")}
    except Exception as e:
        print("證交所即時報價失敗：", repr(e))
    missing = [(c, s) for c, s in items if c not in out]
    if missing:
        try:
            import yfinance as yf
            for code, sfx in missing:
                fi = yf.Ticker(code + sfx).fast_info
                last = float(fi["last_price"])
                out[code] = {"last": last, "high": float(fi.get("day_high") or last),
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
            lines.append(f"{code} {x['name']} {_fmt(x['lots'])} 張　現價 {p:g}（{pnl:+.1f}%）{dist}")
        else:
            lines.append(f"{code} {x['name']} {_fmt(x['lots'])} 張　均價 {_fmt(x['avg'])}")
        lines.append(f"　出場 {_fmt(x['stop'])}｜目標 {_fmt(x['target'])}｜前高 {_fmt(x['key'])}")
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


def check_once(watch, books, sent, today):
    """查一次報價，送出該送的提醒。books：{chat id: 持股}；sent：今天已經送過的 (種類, 對象, 代號)"""
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
            if x["stop"] and p <= x["stop"] and ("stop", cid, code) not in sent:
                sent.add(("stop", cid, code))
                send_to(cid, f"🛑 *跌破出場價*　{code} {x['name']}\n現價 {p:g}，出場價 {x['stop']:g}\n"
                             f"均價 {x['avg']:g}（{(p / x['avg'] - 1) * 100:+.1f}%）\n賣出後回覆：賣 {code}")
            if x["key"] and not x["added"] and p >= x["key"] and ("add", cid, code) not in sent:
                sent.add(("add", cid, code))
                send_to(cid, f"⬆️ *站上前波高點*　{code} {x['name']}\n現價 {p:g}，前高 {x['key']:g}\n"
                             f"可以加碼第二筆；加碼後出場價移到 {st.tick(x['key'] * (1 - cfg.ATTACK_STOP2_PCT)):g}\n"
                             f"加碼後回覆：買 {code} 張數 價格")
            if x["target"] and p >= x["target"] and ("target", cid, code) not in sent:
                sent.add(("target", cid, code))
                send_to(cid, f"🎯 *碰到目標價*　{code} {x['name']}\n現價 {p:g}，目標 {x['target']:g}\n"
                             f"均價 {x['avg']:g}（{(p / x['avg'] - 1) * 100:+.1f}%）")
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
            n = check_once(watch, books, sent, today)
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
