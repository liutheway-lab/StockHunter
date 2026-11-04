"""volume_probe.py — 量測「大盤成交金額」今天幾點才抓得到（證交所每日成交金額，就是 App 大盤圖上的那份）。

從下午 1:25 開始，每 2 分鐘抓一次；第一次抓到今天的數字就記下時間、私訊管理者並結束。
到 15:30 還抓不到也會私訊告知並結束。
注意：時間精度是 2 分鐘（抓的頻率），實際出現時間會落在前一次檢查到這次檢查之間。
"""
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import data

TZ = ZoneInfo("Asia/Taipei")
TOKEN = os.environ.get("TG_BOT_TOKEN", "")
OWNER = os.environ.get("TG_OWNER_ID", "")
EVERY_SEC = 120
STOP_AT = (15, 30)


def send(text):
    print(text)
    if TOKEN and OWNER:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": OWNER, "text": text}, timeout=15)
        except Exception as e:
            print("私訊失敗", repr(e)[:100])


def has_today(today):
    """證交所今天的成交金額是否已經出現（大於 0）"""
    s = data.fetch_twse_turnover(months=1)
    return any(pd.Timestamp(i).date() == today and float(v) > 0 for i, v in s.items())


def main(now_fn=lambda: datetime.now(TZ), sleep_fn=time.sleep, check_fn=has_today):
    now = now_fn()
    if now.weekday() >= 5:
        print("週末，不量測")
        return None
    today = now.date()
    first = True
    while True:
        t = now_fn()
        try:
            ok, err = check_fn(today), ""
        except Exception as e:
            ok, err = False, repr(e)[:80]
        print(f"{t:%H:%M:%S}  {'✅ 抓得到' if ok else '— 還沒有'}{('  ' + err) if err else ''}")
        if ok:
            if first:
                send(f"📊 大盤成交金額量測｜{t:%m/%d}\n開始檢查的時候就已經抓得到了（實際時間早於 {t:%H:%M}）")
            else:
                send(f"📊 大盤成交金額量測｜{t:%m/%d}\n今天 {t:%H:%M} 第一次抓得到（誤差約 2 分鐘）")
            return t
        first = False
        if (t.hour, t.minute) >= STOP_AT:
            send(f"📊 大盤成交金額量測｜{t:%m/%d}\n到 15:30 還抓不到")
            return None
        sleep_fn(EVERY_SEC)


def earlier_run_exists():
    """GitHub 排程和 Render 備援可能各觸發一次。如果有比我更早建立、還在跑或已經成功的紀錄，我就退出，避免重複私訊。
    本機測試沒有 GitHub 環境變數，就不擋。"""
    import requests as rq
    repo, token, rid = (os.getenv(k, "") for k in ("GITHUB_REPOSITORY", "GITHUB_TOKEN", "GITHUB_RUN_ID"))
    if not (repo and token and rid):
        return False
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    base = f"https://api.github.com/repos/{repo}"
    me = rq.get(f"{base}/actions/runs/{rid}", headers=h, timeout=20).json()
    runs = rq.get(f"{base}/actions/workflows/volume_probe.yml/runs?per_page=20", headers=h, timeout=20).json()
    for w in runs.get("workflow_runs", []):
        if str(w["id"]) != str(rid) and w["created_at"] < me["created_at"] and \
                (w["status"] != "completed" or w["conclusion"] == "success"):
            return True
    return False


if __name__ == "__main__":
    if earlier_run_exists():
        print("已經有更早的量測在跑或已完成，本次結束")
    else:
        main()
