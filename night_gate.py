"""night_gate.py — 決定「這一次排程要不要跑盤後掃描」。只用 Python 內建功能，所以不用先安裝套件，
不需要跑的時候幾秒就結束，不會浪費 GitHub 的執行時間。

排程：台灣時間 14:30、15:00、15:30、16:00、16:30 各觸發一次，但只會有一次真的去掃描：
  · 手動執行（workflow_dispatch）→ 一定跑
  · 週末 → 不跑
  · 今天已經掃過了（scan_results.json 的產生日期是今天）→ 不跑
  · 證交所今天的「成交金額」已經公布 → 跑
  · 16:30 起 → 不管齊不齊都跑（會在訊息裡標註「資料可能不完整」）
  · 其他 → 不跑，等下一次
結果寫到 $GITHUB_OUTPUT：go=true/false、ready=true/false
"""
import json
import os
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
LAST_CALL = (16, 30)
RESULT_FILE = "scan_results.json"


def twse_ready(now):
    """證交所『每日市場成交資訊』有沒有今天這一列、成交金額有值。查不到（網路問題）回傳 None"""
    roc = f"{now.year - 1911}/{now:%m/%d}"
    url = f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={now:%Y%m%d}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            j = json.loads(r.read().decode("utf-8"))
        fields = j.get("fields") or []
        k = fields.index("成交金額") if "成交金額" in fields else 2
        for row in j.get("data") or []:
            if str(row[0]).strip() == roc:
                return int(str(row[k]).replace(",", "")) > 0
        return False
    except Exception as e:
        print("證交所成交資訊查詢失敗：", repr(e))
        return None


def already_done(now):
    try:
        with open(RESULT_FILE, encoding="utf-8") as f:
            j = json.load(f)
        return str(j.get("generated_at", "")).startswith(f"{now:%Y-%m-%d}")
    except Exception:
        return False


def decide(now, event, ready, done):
    """回傳 (go, ready_flag, 原因)"""
    if event == "workflow_dispatch":
        return True, ready is not False, "手動執行"
    if now.weekday() >= 5:
        return False, True, "週末"
    if done:
        return False, True, "今天已經掃過了"
    if ready is True:
        return True, True, "資料已齊"
    if (now.hour, now.minute) >= LAST_CALL:
        return True, False, "已到最後時間，不管資料齊不齊都跑"
    return False, False, "資料還沒齊，等下一次"


def main():
    now = datetime.now(TZ)
    event = os.getenv("GITHUB_EVENT_NAME", "")
    done = already_done(now)
    ready = None if (event == "workflow_dispatch" or done or now.weekday() >= 5) else twse_ready(now)
    go, rdy, why = decide(now, event, ready, done)
    print(f"{now:%m/%d %H:%M}｜{event}｜證交所資料 {ready}｜已掃過 {done}｜→ {'執行' if go else '跳過'}（{why}）")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\nready={'true' if rdy else 'false'}\n")


if __name__ == "__main__":
    main()
