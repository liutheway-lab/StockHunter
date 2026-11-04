"""futures_test.py — 一次性測試：期交所（TAIFEX）即時報價能不能穩定取得（免費、不用金鑰）
在 GitHub 的 Actions → futures_test → Run workflow 手動執行。結果印在紀錄裡，也私訊給管理者。

為什麼換成期交所：富果的期貨行情要另外開通權限（用同一把金鑰測出來是 403 Forbidden）。
期交所自己的「期貨行情」網站（mis.taifex.com.tw）有公開的報價介面，日盤、夜盤都有。

測的項目：
1. 日盤（MarketType=0）與夜盤（MarketType=1）各問一次，看有沒有回資料、台指期近月是哪一檔
2. 連續取樣：每隔幾秒問一次，看價格與時間有沒有跟著更新、成功率、回應時間
3. 「台指近月全」＝近月合約的夜盤（15:00～05:00）＋日盤（08:45～13:45）合計；近月用到期日判斷（每月第 3 個星期三）
4. 回傳的欄位名稱（我先猜 CLastPrice、CTime…，不對的話把欄位印出來，下一版再修正）
5. 備援：萬一被擋，記錄 HTTP 狀態碼（有人回報 Cloudflare 會對部分主機回 520）
"""
import json
import os
import time
from datetime import datetime, timedelta
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


def month_list(qs):
    """所有月份的台指期（不含價差合約 /），回傳 [(合約, 累計量, 成交價)]，依成交量由大到小"""
    out_ = []
    for q in qs:
        sid = str(q.get("SymbolID", ""))
        if not sid.startswith("TXF") or "/" in sid:
            continue
        try:
            v = float(str(q.get("CTotalVolume") or 0).replace(",", ""))
        except Exception:
            v = 0
        out_.append((sid, v, q.get("CLastPrice")))
    return sorted(out_, key=lambda x: -x[1])


def total_volume(qs):
    """所有月份合計（看盤軟體的「台指全」通常是這個數字）"""
    return sum(v for _, v, _ in month_list(qs))


MONTH_CODE = "ABCDEFGHIJKL"          # 1 月＝A … 12 月＝L


def expiry_of(sid):
    """TXFK6-F → 2026/11 合約的到期日（每月第 3 個星期三）；看不懂就回 None"""
    try:
        month = MONTH_CODE.index(sid[3]) + 1
        year = now.year // 10 * 10 + int(sid[4])
        if year < now.year - 1:
            year += 10
    except Exception:
        return None
    first = datetime(year, month, 1, tzinfo=TZ)
    wed = [first + timedelta(days=i) for i in range(21) if (first + timedelta(days=i)).weekday() == 2]
    return wed[2].date()


def near_prefix(qs):
    """近月合約的前綴（例：TXFK6）：還沒到期的合約裡到期日最近的那一個。
    到期日當天 13:30 結算後就算過期（夜盤開始就換下一個月）"""
    best = None
    for sid, _, _ in month_list(qs):
        ex = expiry_of(sid)
        if ex is None:
            continue
        if ex < now.date() or (ex == now.date() and (now.hour, now.minute) >= (13, 30)):
            continue
        if best is None or ex < best[0]:
            best = (ex, sid[:5])
    return best                           # (到期日, 前綴) 或 None


def pick_near(qs):
    """近月合約那一筆；判斷不出來就退回「成交量最大」"""
    nb = near_prefix(qs)
    cand = [q for q in qs if str(q.get("SymbolID", "")).startswith("TXF") and "/" not in str(q.get("SymbolID", ""))]
    if nb:
        hit = [q for q in cand if str(q.get("SymbolID", "")).startswith(nb[1])]
        if hit:
            return hit[0]
    def vol(q):
        try:
            return float(str(q.get("CTotalVolume") or 0).replace(",", ""))
        except Exception:
            return 0
    return max(cand, key=vol) if cand else None


def near_volume(qs, prefix):
    """指定近月（前綴）的累計量（不含價差）"""
    for sid, v, _ in month_list(qs):
        if sid.startswith(prefix):
            return v
    return 0


try:
    SESS.get(PAGE, headers={"User-Agent": HEAD["User-Agent"]}, timeout=15)       # 先逛一下首頁拿 cookie
except Exception as e:
    line(f"首頁連線失敗：{e!r}"[:120])

TOT = {}                                  # 近月合約在各盤別的累計量，最後加總成「近月全」
NEAR = {}
for label, m in (("日盤", 0), ("夜盤", 1)):
    code, j, dt = fetch(m)
    qs = quotes(j) if isinstance(j, dict) else []
    near = pick_near(qs) if qs else None
    if near:
        line(f"{label}　{near.get('SymbolID')}｜成交 {near.get('CLastPrice')}｜{near.get('CTime')}｜量 {near.get('CTotalVolume')}")
    else:
        line(f"{label}　失敗 HTTP {code}")
    nb = near_prefix(qs) if qs else None
    if nb:
        NEAR.setdefault("prefix", nb[1])
        TOT[label] = near_volume(qs, nb[1])
        NEAR.setdefault("expiry", nb[0])

# 連續取樣：近月合約的價格與時間有沒有更新（日盤）
line(f"\n【連續取樣 {SAMPLES} 次】")
seen = []
for i in range(SAMPLES):
    code, j, dt = fetch(0)
    near = pick_near(quotes(j)) if isinstance(j, dict) else None
    if near:
        seen.append((near.get("CLastPrice"), near.get("CTime")))
        line(f"{i + 1}. {near.get('CLastPrice')}｜{near.get('CTime')}｜{dt:.2f} 秒")
    else:
        line(f"{i + 1}. 沒有資料 HTTP {code}")
    if i < SAMPLES - 1:
        time.sleep(GAP)
line("✅ 有在更新" if len(set(seen)) > 1 else "⚠️ 沒有變動（休市或盤間沒成交，要在交易時間再測）")

if TOT:
    line(f"\n【台指近月全】{NEAR.get('prefix', '')}（{NEAR['expiry']:%m/%d} 到期）" if NEAR.get("expiry") else f"\n【台指近月全】{NEAR.get('prefix', '')}")
    line(f"合計：{sum(TOT.values()):,.0f} 口")

times = stats["times"]
line(f"\n成功 {stats['ok']}／失敗 {stats['fail']}｜狀態碼 {stats['codes']}")
if times:
    line(f"回應 最快 {min(times):.2f} 秒、最慢 {max(times):.2f} 秒")

text = "\n".join(out)
if TOKEN and OWNER:
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": OWNER, "text": text[:3900]}, timeout=15)
    except Exception as e:
        print("私訊失敗", repr(e)[:120])
