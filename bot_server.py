"""bot_server.py — 讓 Telegram 機器人「隨傳隨回」（架在 Render 免費主機）

原本持股指令要等 GitHub 每 30 分鐘跑一次才回覆；這支程式一直開著，訊息一到就處理。
Telegram 收到訊息 → 直接通知這台主機（webhook）→ 馬上回覆。
指令內容、持股存放方式都跟 intraday.py 一樣（共用同一套程式）。

Render 免費主機閒置 15 分鐘會休眠，休眠後第一則訊息大約要 1 分鐘才回覆，之後就是即時的。
盤中監控（追價提醒、持股提醒）還是由 GitHub 的 intraday 負責。
"""
import hashlib
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import intraday as I

SECRET = hashlib.sha256(I.TG_TOKEN.encode()).hexdigest()[:32]     # 網址裡的暗號，避免別人亂打這個網址
HOOK_PATH = f"/tg/{SECRET}"
_lock = threading.Lock()          # 一次處理一則，避免兩則訊息同時改到同一份持股


def on_update(u):
    with _lock:
        try:
            I.handle_message(u.get("message") or {}, {}, set())
        except Exception as e:
            print("處理訊息失敗：", repr(e), flush=True)


class Handler(BaseHTTPRequestHandler):
    def _ok(self, body=b"ok"):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):              # Render 的健康檢查、或用瀏覽器打開網址時看到 ok
        self._ok("stockhunter bot ok".encode())

    def do_HEAD(self):
        self._ok(b"")

    def do_POST(self):
        if self.path != HOOK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            u = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            u = {}
        self._ok()                 # 先回 Telegram「收到了」，再慢慢處理，避免逾時重送
        threading.Thread(target=on_update, args=(u,), daemon=True).start()

    def log_message(self, *a):
        pass


def main():
    base = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not I.TG_TOKEN:
        raise SystemExit("沒有設定 TG_BOT_TOKEN")
    if base:
        r = I.tg("setWebhook", url=base + HOOK_PATH, allowed_updates=["message"])
        print("設定 webhook：", "成功" if r.get("ok") else r, flush=True)
    else:
        print("沒有 RENDER_EXTERNAL_URL，沒有設定 webhook（本機測試模式）", flush=True)
    I.setup_menu()
    port = int(os.getenv("PORT", "10000"))
    print(f"機器人已啟動，port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
