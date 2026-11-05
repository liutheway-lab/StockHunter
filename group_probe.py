"""group_probe.py — 一次性探測：Yahoo 族群頁的分頁怎麼翻（用完可以刪掉）
只在 GitHub Actions 手動執行（group_probe.yml）。結果會私訊到 Telegram。
"""
import re
from urllib.parse import quote

import requests

BASE = "https://tw.stock.yahoo.com/class-quote"
CAT = {"category": "PCB", "categoryLabel": "電子產業"}
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
CODE_RE = re.compile(r"(\d{4,6})\.(?:TWO|TW)\b")

tries = [("第一頁", {}), ("page=2", {"page": 2}), ("pn=2", {"pn": 2}), ("offset=30", {"offset": 30}),
         ("start=30", {"start": 30}), ("p=2", {"p": 2}), ("pageNo=2", {"pageNo": 2})]

lines = ["🔎 族群分頁探測（PCB）"]
first = None
for label, extra in tries:
    try:
        r = requests.get(BASE, params={**CAT, **extra}, headers=UA, timeout=30)
        codes = list(dict.fromkeys(CODE_RE.findall(r.text)))
        total = re.search(r"共\s*(\d+)\s*筆", r.text)
        if first is None:
            first = set(codes)
            new = "—"
        else:
            new = str(len(set(codes) - first))
        lines.append(f"{label}：HTTP {r.status_code}，代號 {len(codes)} 個，不同於第一頁的 {new} 個，頁面標示 {total.group(1) if total else '?'} 筆")
    except Exception as e:
        lines.append(f"{label}：失敗 {repr(e)[:80]}")

text = "\n".join(lines)
print(text)
try:
    import os
    import requests
    owner = os.getenv("TG_OWNER_ID", "").strip()
    tok = os.getenv("TG_BOT_TOKEN", "").strip()
    if owner and tok:          # 只私訊管理者，不發到群組
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": owner, "text": text}, timeout=20)
    else:
        print("沒有設定 TG_OWNER_ID，只印在日誌，不傳到群組")
except Exception as e:
    print("傳到 Telegram 失敗：", repr(e))
