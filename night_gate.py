"""night_gate.py — 夜間掃描的閘門（night.yml 每次被排程叫醒，都先跑這支，判斷這次要不要真的掃描）

只用 Python 內建套件（這個時候還沒安裝 requests、pandas），結果寫到 GITHUB_OUTPUT 的 go=true/false。

判斷順序（台灣時間）：
  1. 手動執行（workflow_dispatch）→ 一定跑
  2. 週末 → 不跑
  3. 今天已經掃描過（scan_results.json 的 generated_at 是今天）→ 不跑（15:30 成功了，16:00、16:30 的排程就直接結束）
  4. 今天沒開市（證交所 MIS 加權指數的資料日不是今天）→ 不跑（平日國定假日）
  5. 證交所今天的大盤成交金額已經出現 → 跑
  6. 到了 NIGHT_FORCE_HM（16:30）資料還是沒齊 → 還是跑（報告會標示資料日，不要整晚沒消息）
  7. 其他 → 不跑，等下一個排程再看
排程時間在 night.yml（15:30、16:00、16:30），時間設定在 config.py（NIGHT_SCAN_HM、NIGHT_FORCE_HM）。
"""
import datetime as dt
import json
import os
import urllib.request
from zoneinfo import ZoneInfo

import config as cfg

TZ = ZoneInfo("Asia/Taipei")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
RESULT_FILE = "scan_results.json"


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def mis_trading_date():
    """證交所 MIS 加權指數的資料日。收盤後它仍會回傳最近一個交易日，所以平日國定假日會回傳舊的日期。抓不到回傳 None"""
    try:
        j = _get_json("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0")
        d = str((j.get("msgArray") or [{}])[0].get("d", "")).strip()
        return dt.datetime.strptime(d, "%Y%m%d").date()
    except Exception as e:
        print("MIS 資料日抓取失敗：", repr(e)[:100])
        return None


def twse_amount_ready(today):
    """證交所 FMTQIK 今天的大盤成交金額是否已經出現（大於 0）。抓不到回傳 False"""
    try:
        j = _get_json(f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={today:%Y%m}01")
        if j.get("stat") != "OK" or not j.get("data"):
            return False
        ca = j["fields"].index("成交金額")
        for row in j["data"]:
            y, m, d = (int(x) for x in row[0].strip().split("/"))
            if dt.date(y + 1911, m, d) == today:
                return float(str(row[ca]).replace(",", "")) > 0
        return False
    except Exception as e:
        print("證交所成交金額抓取失敗：", repr(e)[:100])
        return False


def already_scanned(today, path=RESULT_FILE):
    """scan_results.json 的 generated_at 是今天 → 今天已經掃描過"""
    try:
        with open(path, encoding="utf-8") as f:
            return str(json.load(f).get("generated_at", "")).startswith(f"{today:%Y-%m-%d}")
    except Exception:
        return False


def decide(now, event, scanned, mis_date, ready):
    """純判斷（不連網，方便測試）。回傳 (要不要跑, 原因)"""
    today = now.date()
    if event == "workflow_dispatch":
        return True, "手動執行，一定跑"
    if now.weekday() >= 5:
        return False, "週末，不跑"
    if scanned:
        return False, "今天已經掃描過，不重複跑"
    if mis_date is not None and mis_date != today:
        return False, f"今天沒開市（證交所資料日 {mis_date:%m/%d}），不跑"
    if ready:
        return True, "證交所成交金額已出現，開始掃描"
    if (now.hour, now.minute) >= cfg.NIGHT_FORCE_HM:
        return True, f"已到 {cfg.NIGHT_FORCE_HM[0]}:{cfg.NIGHT_FORCE_HM[1]:02d}，資料還沒齊也強制掃描"
    return False, "證交所成交金額還沒出現，等下一個排程再看"


def main(now=None, event=None):
    now = now or dt.datetime.now(TZ)
    event = event if event is not None else os.getenv("GITHUB_EVENT_NAME", "")
    today = now.date()
    if event == "workflow_dispatch" or now.weekday() >= 5:
        scanned, mis_date, ready = False, None, False             # 這兩種情況不需要連網確認
    else:
        scanned = already_scanned(today)
        mis_date = None if scanned else mis_trading_date()
        ready = False if scanned or (mis_date is not None and mis_date != today) else twse_amount_ready(today)
    go, why = decide(now, event, scanned, mis_date, ready)
    print(f"{now:%m/%d %H:%M}（{event or '本機'}）：{'✅ 跑' if go else '⏭ 不跑'}｜{why}")
    out = os.getenv("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"go={'true' if go else 'false'}\n")
    return go


if __name__ == "__main__":
    main()
