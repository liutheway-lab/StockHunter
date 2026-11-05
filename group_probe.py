"""group_probe.py — 一次性探測：掃 Yahoo 族群頁載入的 JavaScript 檔，找成分股的資料網址（用完可以刪掉）
只在 GitHub Actions 手動執行（group_probe.yml）。結果只私訊管理者（TG_OWNER_ID）。
前幾次結果：分頁參數沒有用；網頁原始碼裡只有 34 檔，也找不到 API 網址。
這次：把網頁載入的 JS 檔抓下來，找出跟「成分股、分類、sectorId、category」有關的網址片段。
"""
import os
import re
from urllib.parse import urljoin

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
PAGE = "https://tw.stock.yahoo.com/class-quote?category=PCB&categoryLabel=電子產業"
KEYS = re.compile(r"class-quote|categoryLabel|sectorId|/_td[-/a-z]*api|/api/[a-zA-Z/_-]*(?:class|sector|category|quote|stock)", re.I)
lines = ["🔎 族群頁 JS 探測（PCB）"]

html = requests.get(PAGE, headers=UA, timeout=30).text
srcs = list(dict.fromkeys(re.findall(r'<script[^>]+src="([^"]+)"', html)))
lines.append(f"網頁載入 {len(srcs)} 支 JS，掃前 12 支")

hits = []
for s in srcs[:12]:
    u = urljoin(PAGE, s)
    try:
        js = requests.get(u, headers=UA, timeout=30).text
    except Exception as e:
        lines.append(f"讀取失敗 {u[-40:]}：{repr(e)[:50]}")
        continue
    for m in KEYS.finditer(js):
        a, b = max(0, m.start() - 50), min(len(js), m.end() + 70)
        hits.append(f"[{u.split('/')[-1][:25]}] …{js[a:b]}…".replace("\n", " "))
        if len(hits) >= 12:
            break
    if len(hits) >= 12:
        break

lines.append(f"找到 {len(hits)} 處可能的網址片段：")
lines += [h[:200] for h in hits[:12]] or ["沒找到"]

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
