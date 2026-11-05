"""concept_lookup.py — 查個股所屬的概念（族群）。只查候選股，不掃全部概念頁。
用法：concepts_of("2317") → ["FinTech", "Tesla", "HomePod", ...]
資料來源：Yahoo 股市個股頁（頁面上的「概念」連結）。抓不到回傳 None，呼叫的地方要當作「沒有概念資料」處理。
"""
import re
import time
import urllib.parse

import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
CAT_RE = re.compile(r'class-quote\?category=([^&"\'\s<>]+)')


def _names_from(html):
    names = []
    for raw in CAT_RE.findall(html):
        n = urllib.parse.unquote(raw).strip()
        if n and n not in names:
            names.append(n)
    return names


def concepts_of(code, timeout=30):
    """回傳這檔股票的概念名稱清單；上市（.TW）抓不到會再試上櫃（.TWO）。都抓不到回傳 None"""
    for ex in ("TW", "TWO"):
        try:
            r = requests.get(f"https://tw.stock.yahoo.com/quote/{code}.{ex}", headers=UA, timeout=timeout)
            if r.status_code != 200:
                continue
            names = _names_from(r.text)
            if names:
                return names
        except Exception as e:
            print(f"{code}.{ex} 讀取失敗：{repr(e)[:60]}")
        time.sleep(0.5)
    return None


if __name__ == "__main__":
    import sys
    for c in sys.argv[1:] or ["2317"]:
        print(c, concepts_of(c))
