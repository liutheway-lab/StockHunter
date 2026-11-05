"""notify.py — Telegram 發送工具（所有功能共用）
- 支援多個 chat id（用逗號分隔）
- 訊息太長會自動分段（Telegram 單則上限 4096 字）
- Markdown 格式出錯時，自動改用純文字重送
"""
import os

import requests

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TG_CHAT_ID", "").strip()


def chat_ids():
    return [c.strip() for c in TG_CHAT.split(",") if c.strip()]


def _chunks(blocks, limit=3500):
    out, cur = [], ""
    for b in blocks:
        while len(b) > limit:
            if cur:
                out.append(cur)
                cur = ""
            out.append(b[:limit])
            b = b[limit:]
        if cur and len(cur) + len(b) + 2 > limit:
            out.append(cur)
            cur = ""
        cur += ("\n\n" if cur else "") + b
    if cur:
        out.append(cur)
    return out


_BOT_KB = {"keyboard": [[{"text": "追蹤"}, {"text": "移除"}], [{"text": "追蹤清單"}, {"text": "調價"}],
                        [{"text": "查價"}, {"text": "說明"}], [{"text": "清畫面"}]],
           "resize_keyboard": True, "is_persistent": True}
_OWNER_KB = {"keyboard": _BOT_KB["keyboard"][:-1] + [[{"text": "清畫面"}, {"text": "管理者"}]],
             "resize_keyboard": True, "is_persistent": True}


def _markup(chat_id):
    """推播到私人對話時一起附上下方按鍵（跟機器人平常的按鍵一樣），推播不會把按鍵收掉；群組不附"""
    cid = str(chat_id)
    if cid.startswith("-"):
        return None
    return _OWNER_KB if cid == os.getenv("TG_OWNER_ID", "").strip() else _BOT_KB


def tg_send(chat_id, blocks):
    """傳給單一 chat。回傳是否全部成功"""
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    all_ok = True
    kb = _markup(chat_id)
    for c in _chunks(blocks):
        try:
            extra = {"reply_markup": kb} if kb else {}
            r = requests.post(
                url,
                json={"chat_id": chat_id, "text": c, "parse_mode": "Markdown",
                      "disable_web_page_preview": True, **extra},
                timeout=20,
            )
            if not r.ok:
                r = requests.post(
                    url,
                    json={"chat_id": chat_id, "text": c.replace("*", "").replace("`", ""), **extra},
                    timeout=20,
                )
            if not r.ok:
                all_ok = False
                print(f"Telegram 發送失敗 ({chat_id}): {r.status_code} {r.text[:200]}")
        except Exception as e:
            all_ok = False
            print(f"Telegram 發送異常 ({chat_id}): {e}")
    return all_ok


def send_telegram(blocks):
    """傳給所有設定的 chat。沒設定金鑰時只在畫面印出。回傳是否成功"""
    if isinstance(blocks, str):
        blocks = [blocks]
    chats = chat_ids()
    if not TG_TOKEN or not chats:
        print("\n[沒有讀到 TG_BOT_TOKEN / TG_CHAT_ID，只在畫面印出，沒有推播]")
        print("\n\n".join(blocks))
        return False
    return all([tg_send(cid, blocks) for cid in chats])


def send_photo(png, caption=""):
    """傳一張圖（png 的 bytes）給所有設定的 chat。沒設定金鑰時存成 chart_preview.png。回傳是否成功"""
    chats = chat_ids()
    if not TG_TOKEN or not chats:
        with open("chart_preview.png", "wb") as f:
            f.write(png)
        print("\n[沒有讀到 TG_BOT_TOKEN / TG_CHAT_ID，圖存成 chart_preview.png，沒有推播]")
        return False
    ok = True
    for cid in chats:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendPhoto",
                data={"chat_id": cid, "caption": caption[:1000]},
                files={"photo": ("chart.png", png, "image/png")},
                timeout=60,
            )
            if not r.ok:
                ok = False
                print(f"Telegram 傳圖失敗 ({cid}): {r.status_code} {r.text[:200]}")
        except Exception as e:
            ok = False
            print(f"Telegram 傳圖異常 ({cid}): {e}")
    return ok
