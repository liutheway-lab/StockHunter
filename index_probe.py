"""index_probe.py — 一次性探測：證交所即時資料有沒有加權指數的估量（用完可以刪掉）
只在 GitHub Actions 手動執行（index_probe.yml）。結果只私訊管理者（TG_OWNER_ID）。
做法：抓加權指數的即時資料（MIS），列出所有欄位名稱與數值，看有沒有跟成交量有關的欄位（v、tv、估量之類）。
"""
import json
import os

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Referer": "https://mis.twse.com.tw/stock/index.jsp"}
URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
lines = ["🔎 加權指數即時估量探測"]
try:
    r = requests.get(URL, params={"ex_ch": "tse_t00.tw", "json": 1, "delay": 0}, headers=UA, timeout=30)
    lines.append(f"HTTP {r.status_code}")
    data = r.json()
    arr = data.get("msgArray") or []
    if arr:
        item = arr[0]
        keys = sorted(item.keys())
        lines.append("欄位：" + ", ".join(keys)[:700])
        for k in ("v", "tv", "t", "z", "y", "o", "h", "l"):
            if k in item:
                lines.append(f"{k} = {item[k]}")
    else:
        lines.append("沒有資料：" + json.dumps(data, ensure_ascii=False)[:200])
except Exception as e:
    lines.append(f"失敗 {repr(e)[:120]}")

text = "\n".join(lines)
print(text)
try:
    owner = os.getenv("TG_OWNER_ID", "").strip()
    tok = os.getenv("TG_BOT_TOKEN", "").strip()
    if owner and tok:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": owner, "text": text[:3900]}, timeout=20)
    else:
        print("沒有設定 TG_OWNER_ID，只印在日誌，不傳到群組")
except Exception as e:
    print("傳到 Telegram 失敗：", repr(e))
