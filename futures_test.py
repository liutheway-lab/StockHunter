"""futures_test.py — 一次性測試：期交所（TAIFEX）即時報價能不能穩定取得（免費、不用金鑰）
在 GitHub 的 Actions → futures_test → Run workflow 手動執行。結果印在紀錄裡，也私訊給管理者。

為什麼換成期交所：富果的期貨行情要另外開通權限（用同一把金鑰測出來是 403 Forbidden）。
期交所自己的「期貨行情」網站（mis.taifex.com.tw）有公開的報價介面，日盤、夜盤都有。

測的項目：
1. 日盤（MarketType=0）與夜盤（MarketType=1）各問一次，看有沒有回資料、台指期近月是哪一檔
2. 連續取樣：每隔幾秒問一次，看價格與時間有沒有跟著更新、成功率、回應時間
3. 回傳的欄位名稱（我先猜 CLastPrice、CTime…，不對的話把欄位印出來，下一版再修正）
4. 備援：萬一被擋，記錄 HTTP 狀態碼（有人回報 Cloudflare 會對部分主機回 520）
"""
import json
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

URL = "https://mis.taifex.com.tw/futures/api/getQuoteList"
PAGE = "https://mis.taifex.com.tw/futures/"
TOKEN, OWNER = os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_OWNER_ID", "")
TZ = ZoneInfo("Asia/Taipei")
now = datetime.now(TZ)
SAMPLES, GAP = 6, 10
HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Content-Type": "application/json;charset=UTF-8", "Accept": "application/json, text/plain, */*",
        "Origin": "https://mis.taifex.com.tw", "Referer": PAGE}
out = [f"🧪 期交所期貨報價測試　{now:%m/%d %H:%M}"]
stats = {"ok": 0, "fail": 0, "codes": {}, "times": []}
SESS = requests.Session()


def line(s):
    out.append(s)
    print(s)


def body(market):
    # 台指期（TXF）。MarketType：0＝日盤、1＝夜盤（盤後）
    return {"MarketType": str(market), "SymbolType": "F", "KindID": "1", "CID": "TXF",
            "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"}


def fetch(market):
    t0 = time.time()
    try:
        r = SESS.post(URL, json=body(market), headers=HEAD, timeout=15)
    except Exception as e:
        stats["fail"] += 1
        stats["codes"]["連線錯誤"] = stats["codes"].get("連線錯誤", 0) + 1
        return None, f"連線錯誤 {e!r}"[:160], time.time() - t0
    dt = time.time() - t0
    stats["times"].append(dt)
    stats["codes"][r.status_code] = stats["codes"].get(r.status_code, 0) + 1
    try:
        j = r.json()
    except Exception:
        stats["fail"] += 1
        return r.status_code, r.text[:160], dt
    if r.status_code == 200:
        stats["ok"] += 1
    else:
        stats["fail"] += 1
    return r.status_code, j, dt


def quotes(j):
    try:
        return (j.get("RtData") or {}).get("QuoteList") or []
    except Exception:
        return []


def pick_near(qs):
    """近月：SymbolID 以「TXF」開頭、結尾是「-F」（日盤）或「-M」（夜盤），挑成交量最大的月份合約"""
    cand = [q for q in qs if str(q.get("SymbolID", "")).startswith("TXF") and "/" not in str(q.get("SymbolID", ""))]
    cand = [q for q in cand if not str(q.get("SymbolID", "")).startswith("TXFR")] or cand
    def vol(q):
        try:
            return float(str(q.get("CTotalVolume") or 0).replace(",", ""))
        except Exception:
            return 0
    return max(cand, key=vol) if cand else None


try:
    SESS.get(PAGE, headers={"User-Agent": HEAD["User-Agent"]}, timeout=15)       # 先逛一下首頁拿 cookie
except Exception as e:
    line(f"首頁連線失敗：{e!r}"[:120])

for label, m in (("日盤", 0), ("夜盤", 1)):
    line(f"\n【{label}（MarketType={m}）】")
    code, j, dt = fetch(m)
    if isinstance(j, dict):
        print(label, code, json.dumps(j, ensure_ascii=False)[:800])
        qs = quotes(j)
        line(f"HTTP {code}｜RtCode {j.get('RtCode')}｜{len(qs)} 檔｜{dt:.2f} 秒")
        if qs:
            line("欄位：" + "、".join(list(qs[0].keys())[:25]))
            near = pick_near(qs)
            if near:
                line(f"主力合約：{near.get('SymbolID')} {near.get('DispCName')}")
                line(f"成交 {near.get('CLastPrice')}｜時間 {near.get('CTime')}｜累計量 {near.get('CTotalVolume')}"
                     f"｜開 {near.get('COpenPrice')} 高 {near.get('CHighPrice')} 低 {near.get('CLowPrice')}")
        else:
            line("沒有資料（休市時間，或欄位名稱跟我猜的不一樣）")
    else:
        line(f"失敗：HTTP {code} {str(j)[:120]}")

# 連續取樣：看價格與時間有沒有更新
line(f"\n【連續取樣 {SAMPLES} 次・日盤主力合約】")
seen = []
for i in range(SAMPLES):
    code, j, dt = fetch(0)
    near = pick_near(quotes(j)) if isinstance(j, dict) else None
    if near:
        seen.append((near.get("CLastPrice"), near.get("CTime")))
        line(f"第 {i + 1} 次：{near.get('CLastPrice')}｜{near.get('CTime')}｜{dt:.2f} 秒")
    else:
        line(f"第 {i + 1} 次：沒有資料 HTTP {code}")
    if i < SAMPLES - 1:
        time.sleep(GAP)
line("價格或時間有更新" if len(set(seen)) > 1 else "價格與時間都沒變（休市，或盤間沒成交；要在交易時間再測）")

times = stats["times"]
line("\n【總結】")
line(f"成功 {stats['ok']}／失敗 {stats['fail']}｜狀態碼 {stats['codes']}")
if times:
    line(f"回應時間 最快 {min(times):.2f} 秒、最慢 {max(times):.2f} 秒")

text = "\n".join(out)
if TOKEN and OWNER:
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": OWNER, "text": text[:3900]}, timeout=15)
    except Exception as e:
        print("私訊失敗", repr(e)[:120])
