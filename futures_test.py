"""futures_test.py — 期交所（TAIFEX）即時報價測試（免費、不用金鑰）
在 GitHub 的 Actions → futures_test → Run workflow 手動執行。結果印在紀錄裡，也私訊給管理者。

為什麼換成期交所：富果的期貨行情要另外開通權限（用同一把金鑰測出來是 403 Forbidden）。
期交所自己的「期貨行情」網站（mis.taifex.com.tw）有公開的報價介面，日盤、夜盤都有。

輸出順序：
1. 【結果】台指近全（目前交易日）合計
2. 【計算方式】每一步怎麼算的
3. 日盤、夜盤的報價明細
4. 連續取樣、回應狀態

交易日規則（期交所）：
- 日盤 08:45～13:45（平日）屬於當天的交易日
- 夜盤 15:00 開始，算到「下一個交易日」；05:00 前的資料屬於前一晚的夜盤
- 所以「台指近全」＝ 夜盤（前一個平日 15:00 起）＋ 日盤（交易日當天 08:45～15:00）
- 國定假日沒有處理（遇到連假要自己確認）
"""
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


def num(x):
    """整數顯示：42355.00 → 42,355（不留小數）"""
    try:
        return f"{int(float(str(x).replace(',', ''))):,}"
    except Exception:
        return str(x)


def hms(t):
    """121940 → 12:19:40"""
    t = str(t or "")
    return f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 else t


def ym(sid):
    """TXFK6-F → 2026年11月台指期"""
    ex = expiry_of(sid)
    return f"{ex.year}年{ex.month}月台指期" if ex else sid


def next_weekday(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def prev_weekday(d):
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def trading_date(t):
    """t 這個時間屬於哪一個交易日（不處理國定假日）"""
    hm = (t.hour, t.minute)
    if hm >= (15, 0):                     # 夜盤開始，算到下一個交易日
        return next_weekday(t.date())
    if hm < (5, 0):                       # 05:00 前是前一晚的夜盤
        return next_weekday(t.date() - timedelta(days=1))
    return t.date() if t.weekday() < 5 else next_weekday(t.date())


def day_open(t):
    """日盤是否計入：平日 08:45～15:00（13:45 收盤後的資料也算當天）"""
    return t.weekday() < 5 and (8, 45) <= (t.hour, t.minute) < (15, 0)


def show(label, r):
    if r["near"]:
        n = r["near"]
        line(f"{label}　{ym(n.get('SymbolID', ''))}")
        line(f"　成交 {num(n.get('CLastPrice'))}｜{hms(n.get('CTime'))}｜累計量 {num(n.get('CTotalVolume'))}")
    else:
        line(f"{label}　失敗 HTTP {r['code']}")


# ---------- 1. 先抓資料（不輸出） ----------
try:
    SESS.get(PAGE, headers={"User-Agent": HEAD["User-Agent"]}, timeout=15)       # 先逛一下首頁拿 cookie
except Exception as e:
    print(f"首頁連線失敗：{e!r}"[:120])

raw = {}
for label, m in (("日盤", 0), ("夜盤", 1)):
    code, j, dt = fetch(m)
    qs = quotes(j) if isinstance(j, dict) else []
    raw[m] = {"label": label, "code": code, "qs": qs,
              "near": pick_near(qs) if qs else None, "nb": near_prefix(qs) if qs else None}

# ---------- 2. 算結果 ----------
T = trading_date(now)                     # 目前交易日
prev = prev_weekday(T)                    # 夜盤開始的那一天（前一個平日）
nb = raw[1]["nb"] or raw[0]["nb"]         # 近月合約
prefix = nb[1] if nb else None
counted = day_open(now)
night_vol = near_volume(raw[1]["qs"], prefix) if prefix else 0
day_vol = near_volume(raw[0]["qs"], prefix) if (prefix and counted) else 0
total = night_vol + day_vol
exp_txt = f"{nb[0].year}年{nb[0].month}月" if nb else "近月"

# ---------- 3. 輸出：結果在最上面 ----------
line(f"【結果】台指近全（交易日 {T:%m/%d}）：{total:,} 口")
line(f"　夜盤（{prev:%m/%d} 15:00 起）{night_vol:,} 口")
line(f"　日盤（{T:%m/%d}）" + (f"{day_vol:,} 口" if counted else "還沒開盤，不計入"))
line("")
line("【計算方式】")
line(f"1. 現在 {now:%H:%M}，屬於交易日 {T:%m/%d}（15:00 後換成下一個交易日；05:00 前算前一晚的夜盤）")
line(f"2. 夜盤：{exp_txt}台指期的夜盤累計量 {night_vol:,} 口，從 {prev:%m/%d} 15:00 開始，算到 {T:%m/%d}")
line(f"3. 日盤：只有平日 08:45～15:00 才計入，現在{'計入' if counted else '不計入'}")
line(f"4. 合計 ＝ 夜盤 {night_vol:,} ＋ 日盤 {day_vol:,} ＝ {total:,}")
line("5. 國定假日沒有處理，假日前後要自己確認")
line("")
line("【報價明細】")
show("📈 日盤", raw[0])
show("🌙 夜盤", raw[1])

# ---------- 4. 連續取樣 ----------
line(f"\n【連續取樣 {SAMPLES} 次（日盤）】")
seen = []
for i in range(SAMPLES):
    code, j, dt = fetch(0)
    near = pick_near(quotes(j)) if isinstance(j, dict) else None
    if near:
        seen.append((near.get("CLastPrice"), near.get("CTime")))
        line(f"{i + 1}. {num(near.get('CLastPrice'))}｜{hms(near.get('CTime'))}｜{dt:.2f} 秒")
    else:
        line(f"{i + 1}. 沒有資料 HTTP {code}")
    if i < SAMPLES - 1:
        time.sleep(GAP)
line("✅ 有在更新" if len(set(seen)) > 1 else "⚠️ 沒有變動（休市、盤間沒成交，或目前是夜盤時段，要在交易時間再測）")

# ---------- 5. 狀態 ----------
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
