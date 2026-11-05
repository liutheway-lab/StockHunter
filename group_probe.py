"""group_probe.py — 一次性探測：Yahoo 族群頁的成分股是從哪個網址載入的（用完可以刪掉）
只在 GitHub Actions 手動執行（group_probe.yml）。結果只私訊管理者（TG_OWNER_ID）。
上一輪結果：分頁參數都沒有用，頁面一開始只有 34 檔，其餘是網頁載入後才出現。
這次：抓網頁原始碼，找出它會去呼叫的網址（API），並試著直接打開看看能不能拿到完整成分股。
"""
import json
import os
import re

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
PAGE = "https://tw.stock.yahoo.com/class-quote?category=PCB&categoryLabel=電子產業"
CODE_RE = re.compile(r"(\d{4,6})\.(?:TWO|TW)\b")
lines = ["🔎 族群成分股 API 探測（PCB）"]

r = requests.get(PAGE, headers=UA, timeout=30)
html = r.text
lines.append(f"網頁 HTTP {r.status_code}，長度 {len(html)}，代號 {len(set(CODE_RE.findall(html)))} 個")

# 1) 網頁原始碼裡提到的 API 網址
urls = re.findall(r"""(?:https?:)?//tw\.stock\.yahoo\.com/[^\s"'<>\\]*(?:api|_td)[^\s"'<>\\]*""", html)
urls += re.findall(r"""["'](/[^\s"'<>\\]*(?:/api/|_td-stock)[^\s"'<>\\]*)["']""", html)
uniq = list(dict.fromkeys(urls))[:8]
lines.append("頁面提到的 API 網址：" + (" ｜ ".join(u[:90] for u in uniq) if uniq else "沒找到"))

# 2) 試著直接打開：看有沒有拿到完整成分股（58 檔）
cands = [u if u.startswith("http") else "https://tw.stock.yahoo.com" + u for u in uniq]
for u in cands[:5]:
    try:
        rr = requests.get(u, headers={**UA, "Accept": "application/json"}, timeout=30)
        codes = set(CODE_RE.findall(rr.text))
        if not codes:
            try:
                codes = {str(x) for x in re.findall(r'"symbol"\s*:\s*"(\d{4,6})', rr.text)}
            except Exception:
                codes = set()
        lines.append(f"試 {u[:60]}…：HTTP {rr.status_code}，代號 {len(codes)} 個")
    except Exception as e:
        lines.append(f"試 {u[:60]}…：失敗 {repr(e)[:60]}")

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
