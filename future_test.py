"""futures_test.py — 一次性測試：富果（Fugle）期貨報價穩不穩
在 GitHub 的 Actions → futures_test → Run workflow 手動執行。結果印在紀錄裡，也私訊給管理者。

測的項目：
1. 台指期近月（TXF1!）與指定月份（TXF 月份代碼 + 年份末碼，例：2026/11 → TXFK6）
2. 日盤（預設）與夜盤（session=afterhours）的即時報價能不能拿到
3. 報價的時間是不是跟著變動（lastUpdated 或 lastTrade 有沒有更新）
4. 連續問 N 次、每次隔幾秒，看成功率與回應時間，也統計 429（超過次數）
5. 1 分 K 能不能拿到（之後做價格觸發要用）
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

KEY = os.environ.get("FUGLE_API_KEY", "").strip()
BASE = "https://api.fugle.tw/marketdata/v1.0/futopt"
TOKEN, OWNER = os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_OWNER_ID", "")
TZ = ZoneInfo("Asia/Taipei")
now = datetime.now(TZ)
MONTH_CODE = "ABCDEFGHIJKL"          # 1 月＝A … 12 月＝L（TXFC5＝2025 年 3 月）
next_code = f"TXF{MONTH_CODE[now.month - 1]}{now.year % 10}"   # 這個月的合約，例：TXFK6
SYMBOLS = ["TXF1!", next_code]
SAMPLES, GAP = 6, 10                  # 連續問 6 次，每次隔 10 秒
out = [f"🧪 期貨報價測試　{now:%m/%d %H:%M}"]
stats = {"ok": 0, "fail": 0, "429": 0, "times": []}


def get(path, **params):
    t0 = time.time()
    try:
        r = requests.get(BASE + path, params=params, headers={"X-API-KEY": KEY}, timeout=20)
    except Exception as e:
        return None, f"連線錯誤 {e!r}"[:160], time.time() - t0
    dt = time.time() - t0
    stats["times"].append(dt)
    if r.status_code == 429:
        stats["429"] += 1
    try:
        return r.status_code, r.json(), dt
    except Exception:
        return r.status_code, r.text[:160], dt


def line(s):
    out.append(s)
    print(s)


if not KEY:
    line("❌ 沒有讀到 FUGLE_API_KEY")
else:
    line(f"合約：{' / '.join(SYMBOLS)}")
    for sym in SYMBOLS:
        line(f"\n【{sym}】")
        for label, extra in (("日盤", {}), ("夜盤", {"session": "afterhours"})):
            code, j, dt = get(f"/intraday/quote/{sym}", **extra)
            print(sym, label, code, json.dumps(j, ensure_ascii=False)[:500] if isinstance(j, dict) else str(j)[:300])
            if code == 200 and isinstance(j, dict):
                stats["ok"] += 1
                line(f"{label}：成交 {j.get('lastPrice')}｜時間 {j.get('lastUpdated') or j.get('lastTrade')}"
                     f"｜回應 {dt:.2f} 秒")
            else:
                stats["fail"] += 1
                line(f"{label}：失敗 {code} {str(j)[:100]}")

    # 連續取樣：看報價會不會跟著變，有沒有延遲
    line(f"\n【連續取樣 {SAMPLES} 次・{SYMBOLS[0]} 日盤】")
    seen = []
    for i in range(SAMPLES):
        code, j, dt = get(f"/intraday/quote/{SYMBOLS[0]}")
        if code == 200 and isinstance(j, dict):
            stats["ok"] += 1
            stamp = j.get("lastUpdated") or j.get("lastTrade")
            seen.append((j.get("lastPrice"), stamp))
            line(f"第 {i + 1} 次：{j.get('lastPrice')}｜{stamp}｜{dt:.2f} 秒")
        else:
            stats["fail"] += 1
            line(f"第 {i + 1} 次：失敗 {code}")
        if i < SAMPLES - 1:
            time.sleep(GAP)
    changed = len({s for s in seen}) > 1
    line("價格有跟著變" if changed else "價格沒有變（可能是休市或盤間沒有成交，要在交易時間再測一次）")

    # 1 分 K（之後做價格觸發與進出場判斷用）
    line("\n【1 分 K】")
    code, j, dt = get(f"/intraday/candles/{SYMBOLS[0]}", timeframe="1")
    if code == 200 and isinstance(j, dict) and j.get("data"):
        stats["ok"] += 1
        d = j["data"]
        line(f"拿到 {len(d)} 根，最後一根 {d[-1].get('date')} 收 {d[-1].get('close')}")
    else:
        stats["fail"] += 1
        line(f"1 分 K 失敗 {code} {str(j)[:100]}")

    times = stats["times"]
    line("\n【總結】")
    line(f"成功 {stats['ok']}／失敗 {stats['fail']}／超過次數（429） {stats['429']}")
    if times:
        line(f"回應時間 最快 {min(times):.2f} 秒、最慢 {max(times):.2f} 秒")

text = "\n".join(out)
if TOKEN and OWNER:
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": OWNER, "text": text}, timeout=15)
    except Exception as e:
        print("私訊失敗", repr(e)[:120])
