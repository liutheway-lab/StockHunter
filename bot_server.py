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
import time
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import intraday as I

SECRET = hashlib.sha256(I.TG_TOKEN.encode()).hexdigest()[:32]     # 網址裡的暗號，避免別人亂打這個網址
HOOK_PATH = f"/tg/{SECRET}"
_asked = set()                    # 已經通知過管理者的人，不要每則訊息都重複通知
_lock = threading.Lock()          # 一次處理一則，避免兩則訊息同時改到同一份持股


def on_update(u):
    with _lock:
        try:
            if u.get("callback_query"):
                I.handle_callback(u["callback_query"], {}, _asked)
            else:
                I.handle_message(u.get("message") or {}, {}, _asked)
        except Exception as e:
            print("處理訊息失敗：", repr(e), flush=True)


# ---------- 排程備援（夜間掃描、早報）----------
# GitHub 的排程偶爾會延遲或被跳過。這裡每分鐘看一次：到了檢查時間（台灣時間、週一到週五），
# 如果這個工作還沒有今天的執行紀錄（或只有失敗的），就請 GitHub 馬上跑一次。
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()
GH_REPO = os.getenv("GH_REPO", "liutheway-lab/StockHunter")
GH_BRANCH = os.getenv("GH_BRANCH", "main")
TPE = dt.timezone(dt.timedelta(hours=8))
# name：顯示用；file：workflow 檔名；at：開始檢查；until：超過就不補；since：這個時間之後建立的紀錄才算「這一次」
JOBS = [
    {"name": "night", "file": "night.yml", "at": (22, 15), "until": (23, 50), "since": (21, 0)},
    {"name": "morning", "file": "morning.yml", "at": (8, 35), "until": (9, 30), "since": (7, 30)},
    {"name": "intraday", "file": "intraday.yml", "at": (8, 58), "until": (13, 0), "since": (8, 0)},
]


def _gh(method, path, **kw):
    import requests
    return requests.request(method, f"https://api.github.com/repos/{GH_REPO}{path}", timeout=20,
                            headers={"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
                                     "X-GitHub-Api-Version": "2022-11-28"}, **kw)


def backup_step(now, state, job):
    """做一次檢查。state 記錄今天做到哪：{'day','done','tries','wait'}。回傳說明文字（沒動作回 None）"""
    hm = (now.hour, now.minute)
    if now.weekday() >= 5 or hm < job["at"] or hm >= job["until"]:
        return None
    day = now.date().isoformat()
    if state.get("day") != day:
        state.clear()
        state.update({"day": day, "done": False, "tries": 0})
    if state["done"] or state["tries"] >= 2:
        return None
    if state.get("wait") and now < state["wait"]:            # 剛請 GitHub 補跑，等幾分鐘再看，避免重複補跑
        return None
    r = _gh("GET", f"/actions/workflows/{job['file']}/runs?per_page=10")
    if r.status_code != 200:
        return f"查 {job['name']} 執行紀錄失敗 {r.status_code}"
    start = now.replace(hour=job["since"][0], minute=job["since"][1], second=0, microsecond=0)
    mine = []
    for w in r.json().get("workflow_runs", []):
        t = dt.datetime.fromisoformat(w["created_at"].replace("Z", "+00:00")).astimezone(TPE)
        if t >= start:
            mine.append(w)
    if [w for w in mine if w["status"] != "completed" or w["conclusion"] == "success"]:
        state["done"] = True
        return f"{job['name']} 這一次已經有跑（或正在跑），不用補"
    if mine and state["tries"] >= 1:              # 失敗過、已經補跑過一次，就不再重複
        state["done"] = True
        return f"{job['name']} 補跑後仍失敗，不再重試"
    state["tries"] += 1
    state["wait"] = now + dt.timedelta(minutes=5)
    r = _gh("POST", f"/actions/workflows/{job['file']}/dispatches", json={"ref": GH_BRANCH})
    if r.status_code == 204:
        return f"{job['name']} 沒有成功的紀錄，已請 GitHub 補跑一次"
    return f"請 GitHub 補跑 {job['name']} 失敗 {r.status_code} {r.text[:120]}"


SHOW = {"night": "夜間掃描", "morning": "早報", "intraday": "盤中監控"}


def _notify_owner(job, msg):
    """只有真的補跑、或補跑失敗時，才私訊管理者一行（平常沒事就不吵）"""
    if "已請 GitHub 補跑" in msg:
        text = f"🔁 備援補跑：{SHOW[job['name']]}\nGitHub 排程沒準時"
    elif "補跑" in msg and "失敗" in msg:
        text = f"⚠️ {SHOW[job['name']]}補跑失敗\n請到 Actions 手動執行"
    else:
        return
    try:
        if I.OWNER:
            I.send_to(I.OWNER, text)
    except Exception as e:
        print("通知管理者失敗：", repr(e), flush=True)


def backup_loop():
    states = {j["name"]: {} for j in JOBS}
    while True:
        for j in JOBS:
            try:
                msg = backup_step(dt.datetime.now(TPE), states[j["name"]], j)
                if msg:
                    print("排程備援：", msg, flush=True)
                    _notify_owner(j, msg)
            except Exception as e:
                print("排程備援錯誤：", repr(e), flush=True)
        time.sleep(60)


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
        r = I.tg("setWebhook", url=base + HOOK_PATH, allowed_updates=["message", "callback_query"])
        print("設定 webhook：", "成功" if r.get("ok") else r, flush=True)
    else:
        print("沒有 RENDER_EXTERNAL_URL，沒有設定 webhook（本機測試模式）", flush=True)
    I.setup_menu()
    I.DIR_BACKGROUND = True            # 處理訊息時不現抓股票清單，避免證交所慢的時候卡住所有人
    I.start_dir_loader()               # 開機就在背景載入，失敗每分鐘重試
    if GH_TOKEN:
        threading.Thread(target=backup_loop, daemon=True).start()
        print("夜間掃描備援：已啟動（含早報、盤中監控）", flush=True)
    else:
        print("沒有 GH_TOKEN，夜間掃描備援沒有啟動（含早報、盤中監控）", flush=True)
    port = int(os.getenv("PORT", "10000"))
    print(f"機器人已啟動，port {port}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
