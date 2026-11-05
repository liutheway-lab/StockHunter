"""group_probe.py — 一次性探測：Yahoo 個股頁有沒有列出「所屬概念」（用完可以刪掉）
只在 GitHub Actions 手動執行（group_probe.yml）。結果只私訊管理者（TG_OWNER_ID）。
做法：抓幾檔股票的個股頁，看頁面裡有沒有概念／族群的標籤，以及它們的網址格式。
"""
import os
import re

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
CODES = ["2317", "2610", "3037"]
lines = ["🔎 個股頁概念標籤探測"]
for c in CODES:
    try:
        html = requests.get(f"https://tw.stock.yahoo.com/quote/{c}.TW", headers=UA, timeout=30).text
        links = re.findall(r'href="(/class-quote\?[^"]+)"', html)
        names = list(dict.fromkeys(re.findall(r"class-quote\?category=([^&\"]+)", html)))
        lines.append(f"{c}：頁面 {len(html)} 字，概念連結 {len(set(links))} 個")
        if links:
            lines.append("  例：" + " ｜ ".join(list(dict.fromkeys(links))[:4])[:160])
        if names:
            lines.append("  概念：" + "、".join(n for n in names[:12]))
    except Exception as e:
        lines.append(f"{c}：失敗 {repr(e)[:60]}")

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
