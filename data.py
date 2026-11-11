"""data.py — 抓股票清單與日線資料（掃描用）
- get_directory()：全上市櫃股票清單（代號、中文名稱、產業別）
- fetch_market()：加權指數日線
- fetch_prices()：分批下載全部個股日線
價格用「不還原股利」的收盤價（只還原分割），這樣前高、進場價才跟看盤軟體上看到的一樣。
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import yfinance as yf

import config as cfg
from lookup import load_directory

COLS = ["Open", "High", "Low", "Close", "Volume"]


def get_directory():
    """全市場清單。官方清單抓不到時，退回 config.FALLBACK 的少數幾檔（並在 attrs 標記）"""
    try:
        d = load_directory()
        d.attrs["fallback"] = False
        return d
    except Exception as e:
        print("股票清單抓取失敗，改用備用清單：", repr(e))
        rows = [{"code": s.split(".")[0], "name": s.split(".")[0], "industry": "",
                 "suffix": "." + s.split(".")[1], "market": ""} for s in cfg.FALLBACK]
        d = pd.DataFrame(rows)
        d.attrs["fallback"] = True
        return d


def _clean(df):
    """整理成標準欄位、去掉空值、時區拿掉"""
    if df is None or len(df) == 0:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if not all(c in df.columns for c in COLS):
        return None
    df = df[COLS].dropna(subset=["Close", "High", "Low"]).copy()
    df["Volume"] = df["Volume"].fillna(0)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df if len(df) else None


def fetch_twse_daily(months=4):
    """證交所官方 FMTQIK：每天的成交金額（元）和加權指數收盤。回傳 DataFrame（amount, close），索引是日期"""
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    y, m = now.year, now.month
    out = {}
    for _ in range(months):
        url = f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={y}{m:02d}01"
        for k in range(3):
            try:
                r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                j = r.json()
                if j.get("stat") == "OK" and j.get("data"):
                    f = j["fields"]
                    ca = f.index("成交金額")
                    cc = next((n for n, x in enumerate(f) if "加權" in str(x)), None)
                    for row in j["data"]:
                        yy, mm, dd = (int(x) for x in row[0].strip().split("/"))
                        out[pd.Timestamp(yy + 1911, mm, dd)] = {
                            "amount": _numf(row[ca]), "close": _numf(row[cc]) if cc is not None else None}
                break
            except Exception as e:
                print(f"證交所成交金額 {y}/{m:02d} 第 {k + 1} 次失敗：{e!r}")
                time.sleep(2)
        time.sleep(0.5)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    if not out:
        raise RuntimeError("證交所成交金額抓不到")
    return pd.DataFrame.from_dict(out, orient="index").sort_index()


def fetch_twse_turnover(months=4):
    """證交所官方：加權市場每日成交金額（元）。回傳 Series（索引是日期）。
    Yahoo 的加權指數成交量不可靠，所以止跌確認的量能改用這份。"""
    return fetch_twse_daily(months)["amount"].dropna()


_MIS_HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
             "Referer": "https://mis.twse.com.tw/stock/index.jsp", "Accept": "application/json, text/javascript, */*; q=0.01"}
_MIS_SESS = {}
_LIVE_LAST = {}          # {(代號, 後綴): (日期, 最近一次成交價)}：證交所有時一瞬間沒回成交價，就用這個


def fetch_live_quote(code, suffix):
    """即時成交價（證交所 MIS，跟券商看盤同一個來源）。回傳 {"last", "prev", "time"}；抓不到回傳 None，呼叫端改用日線收盤"""
    ex = ("tse_" if suffix == ".TW" else "otc_") + f"{code}.tw"
    try:
        if "s" not in _MIS_SESS:
            ss = requests.Session()
            try:
                ss.get("https://mis.twse.com.tw/stock/index.jsp", headers=_MIS_HEAD, timeout=4)
            except Exception:
                pass
            _MIS_SESS["s"] = ss
        r = _MIS_SESS["s"].get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp",
                               params={"ex_ch": ex, "json": "1", "delay": "0", "_": int(time.time() * 1000)},
                               headers=_MIS_HEAD, timeout=4)
        arr = (r.json() or {}).get("msgArray") or []
        if not arr:
            return None
        q = arr[0]

        def num(k):
            try:
                return float(str(q.get(k, "")).split("_")[0])
            except ValueError:
                return None
        day, prev = q.get("d", ""), num("y")                  # y＝昨收（漲跌的基準）
        key = (code, suffix)
        z = num("z")                                          # z＝最新成交價；這一瞬間沒有新成交時是「-」
        if z:
            _LIVE_LAST[key] = (day, z)
        elif key in _LIVE_LAST and _LIVE_LAST[key][0] == day:
            z = _LIVE_LAST[key][1]                            # 同一天記到的最近成交價（不拿開盤或昨收冒充）
        else:
            return None                                       # 今天還沒有成交價 → 讓頁面退回日線收盤
        if not prev:
            return None
        return {"last": z, "prev": prev, "time": f"{day} {q.get('t', '')}"}
    except Exception as e:
        print(f"{code} 即時報價抓不到：{repr(e)[:80]}")
        _MIS_SESS.clear()
        return None


def fetch_twse_stock_volume(code, months=4):
    """證交所官方：上市個股每日成交股數（股）。回傳 Series（索引是日期）。
    跟看盤軟體、券商用同一份數字；Yahoo 的台股量有時會跟它對不上。上櫃股票這裡抓不到（會丟例外）。"""
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    y, m = now.year, now.month
    out = {}
    for _ in range(months):
        url = f"https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date={y}{m:02d}01&stockNo={code}"
        for k in range(2):                                   # 兩次就放棄，不讓 App 一直轉
            try:
                j = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"}).json()
                if j.get("stat") == "OK" and j.get("data"):
                    ca = j["fields"].index("成交股數")
                    for row in j["data"]:
                        yy, mm, dd = (int(x) for x in row[0].strip().split("/"))
                        v = _numf(row[ca])
                        if v is not None:
                            out[pd.Timestamp(yy + 1911, mm, dd)] = v
                break
            except Exception as e:
                print(f"證交所個股成交量 {code} {y}/{m:02d} 第 {k + 1} 次失敗：{e!r}")
                time.sleep(2)
        time.sleep(0.5)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    if not out:
        raise RuntimeError(f"證交所個股成交量 {code} 抓不到")
    return pd.Series(out).sort_index()


def fetch_twse_market_volume(days, sleep=1.0, budget_sec=300):
    """證交所官方：指定的每個交易日，全部上市股票的成交股數。
    一天只要一次請求（MI_INDEX），所以 70 天大約 70 次。回傳 {日期: {代號: 股數}}，抓不到的日子不會出現在結果裡。
    總時間最多 budget_sec 秒，超過就停，已抓到的照樣回傳（不讓整個夜間掃描被拖到逾時）。"""
    out = {}
    t0 = time.time()
    for day in days:
        if time.time() - t0 > budget_sec:
            print(f"證交所全市場成交股數超過 {budget_sec} 秒，先停止，已抓 {len(out)} 天", flush=True)
            break
        day = pd.Timestamp(day).normalize()
        url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={day:%Y%m%d}&type=ALLBUT0999"
        for k in range(3):
            try:
                j = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"}).json()
                tables = list(j.get("tables") or [])
                for key in list(j):                          # 舊格式：fields1/data1 ...
                    if key.startswith("fields") and key.replace("fields", "data") in j:
                        tables.append({"fields": j[key], "data": j[key.replace("fields", "data")]})
                got = {}
                for t in tables:
                    f = [str(x) for x in (t.get("fields") or [])]
                    if "證券代號" not in f or "成交股數" not in f:
                        continue
                    si, vi = f.index("證券代號"), f.index("成交股數")
                    for row in t.get("data") or []:
                        v = _numf(row[vi])
                        if v is not None:
                            got[str(row[si]).strip()] = v
                if got:
                    out[day] = got
                break
            except Exception as e:
                print(f"證交所全市場成交股數 {day:%Y-%m-%d} 第 {k + 1} 次失敗：{e!r}"[:200])
                time.sleep(2)
        time.sleep(sleep)
    return out


def apply_twse_volume(prices, days):
    """把 Yahoo 的個股量，換成證交所官方的量（只換上市股票、只換抓得到的日子）。
    證交所整批抓不到時，原樣回傳 Yahoo 的資料。回傳 (prices, 換了幾檔)"""
    prices = dict(prices)                                    # 不改到呼叫端的原字典
    try:
        vols = fetch_twse_market_volume(days)
    except Exception as e:
        print("證交所成交股數整批抓取失敗，沿用 Yahoo：", repr(e)[:120])
        return prices, 0
    if not vols:
        print("證交所成交股數沒有抓到任何一天，沿用 Yahoo")
        return prices, 0
    changed = 0
    for sym, df in prices.items():
        if not sym.endswith(".TW"):
            continue
        code = sym.split(".")[0]
        m = pd.Series({d: vols[d][code] for d in vols if code in vols[d]}, dtype=float)
        if m.empty:
            continue
        idx = pd.DatetimeIndex(df.index)
        if idx.tz is not None:
            idx = idx.tz_localize(None)
        new = m.reindex(idx).to_numpy()
        old = df["Volume"].to_numpy(dtype=float)
        df = df.copy()
        df["Volume"] = np.where(np.isnan(new), old, new)
        prices[sym] = df
        changed += 1
    print(f"成交量改用證交所官方：{len(vols)} 天，換了 {changed} 檔上市股票")
    return prices, changed


def fetch_twse_ohlc(months=3):
    """證交所官方：加權指數每日開高低收（跟看盤軟體同一份數字）。回傳 DataFrame（索引是日期）。
    Yahoo 偶爾少最後一天，或某一天的高低點有落差，用這份補上、校正。"""
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    y, m = now.year, now.month
    rows = {}
    for _ in range(months):
        urls = [f"https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={y}{m:02d}01&response=json",   # 新網址
                f"https://www.twse.com.tw/indicesReport/MI_5MINS_HIST?response=json&date={y}{m:02d}01"]  # 舊網址
        for k in range(4):
            try:
                r = requests.get(urls[k % 2], timeout=20, headers={"User-Agent": "Mozilla/5.0"})
                j = r.json()
                if j.get("stat") == "OK" and j.get("data"):
                    for row in j["data"]:
                        yy, mm, dd = (int(x) for x in str(row[0]).strip().split("/"))
                        o, h, l, c = (_numf(v) for v in row[1:5])
                        if None not in (o, h, l, c):
                            rows[pd.Timestamp(yy + 1911, mm, dd)] = {"Open": o, "High": h, "Low": l, "Close": c}
                break
            except Exception as e:
                print(f"證交所加權指數 {y}/{m:02d} 第 {k + 1} 次失敗：{e!r}")
                time.sleep(1)
        time.sleep(0.5)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    if not rows:
        raise RuntimeError("證交所加權指數抓不到")
    return pd.DataFrame.from_dict(rows, orient="index").sort_index()


def patch_with_twse(df):
    """用證交所官方的開高低收，補上 Yahoo 缺的日子、校正有落差的日子。失敗就原樣回傳"""
    try:
        t = fetch_twse_ohlc()
    except Exception as e:
        print("證交所加權指數補資料失敗：", repr(e))
        df = df.copy()
        df.attrs["twse_note"] = f"證交所官方指數抓不到（{e}）"
        return df
    df = df.copy()
    t = t[t.index >= df.index[0]]
    n_new = int((~t.index.isin(df.index)).sum())
    for d in t.index:
        if d in df.index:
            df.loc[d, ["Open", "High", "Low", "Close"]] = t.loc[d, ["Open", "High", "Low", "Close"]].to_numpy(float)
        else:
            df.loc[d, ["Open", "High", "Low", "Close", "Volume"]] = list(t.loc[d, ["Open", "High", "Low", "Close"]]) + [0.0]
    df = df.sort_index()
    df.attrs["patched_days"] = n_new
    df.attrs["twse_note"] = f"證交所官方指數最新到 {t.index[-1]:%m/%d}，補上 {n_new} 天"
    return df


def _numf(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def fetch_mi_index_amounts(day):
    """證交所『大盤統計資訊』（MI_INDEX）當天各類成交金額（元）。回傳 {項目名稱: 金額}。
    用來跟 FMTQIK 的成交金額對照，找出跟看盤軟體數字一致的口徑。"""
    url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={day:%Y%m%d}&type=MS"
    j = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).json()
    tables = list(j.get("tables") or [])
    for k in list(j):                      # 舊格式：fields1/data1 ...
        if k.startswith("fields") and k.replace("fields", "data") in j:
            tables.append({"fields": j[k], "data": j[k.replace("fields", "data")]})
    out = {}
    for t in tables:
        cols = [i for i, f in enumerate(t.get("fields") or []) if "成交金額" in str(f)]
        if not cols:
            continue
        for row in t.get("data") or []:
            v = _numf(row[cols[0]])
            if v:
                out[str(row[0]).strip()] = v
    return out


def _fix_hl(raw):
    """Yahoo 最後一天有時只有收盤、沒有開高低：用收盤補上，才不會整天被丟掉"""
    if raw is None or len(raw) == 0:
        return raw
    raw = raw.copy()
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = raw.columns.get_level_values(0)
    if "Close" in raw.columns:
        for c in ("Open", "High", "Low"):
            if c in raw.columns:
                raw[c] = raw[c].fillna(raw["Close"])
    return raw


def _add_missing(df, extra, note):
    """把 extra 裡 df 沒有的日子加進來（只加新的日子，不改舊的）"""
    if extra is None or len(extra) == 0:
        return df, 0
    extra = extra[extra.index > df.index[-1]]
    if len(extra) == 0:
        return df, 0
    df = pd.concat([df, extra[COLS]]).sort_index()
    df = df[~df.index.duplicated(keep="first")]
    return df, len(extra)


def _yahoo_recent():
    """Yahoo 另一種抓法（跟個股一樣用 history），只抓最近一個月"""
    h = yf.Ticker("^TWII").history(period="1mo", interval="1d", auto_adjust=False)
    return _clean(_fix_hl(h))


def _yahoo_intraday_days():
    """Yahoo 盤中 5 分 K 合成日 K：日線還沒更新時，用它補最近幾天"""
    h = yf.Ticker("^TWII").history(period="5d", interval="5m", auto_adjust=False)
    h = _clean(_fix_hl(h))
    if h is None:
        return None
    g = h.groupby(h.index.normalize())
    out = pd.DataFrame({"Open": g["Open"].first(), "High": g["High"].max(), "Low": g["Low"].min(),
                        "Close": g["Close"].last(), "Volume": 0.0})
    return out


def fetch_market(period="2y", retries=3):
    """加權指數日線（^TWII）。價格來自 Yahoo；Volume 欄位換成證交所官方的「成交金額」。
    Yahoo 日線偶爾少最近一天，依序用這些補：Yahoo 另一種抓法 → Yahoo 盤中 5 分 K → 證交所官方開高低收 → 證交所收盤。
    成交金額抓不到時 Volume 全部留空，止跌確認會顯示『無法確認』，而不是用錯的數字。"""
    last = None
    for k in range(retries):
        try:
            raw = yf.download("^TWII", period=period, interval="1d", progress=False, auto_adjust=False)
            df = _clean(_fix_hl(raw))
            if df is not None and len(df) >= 70:
                break
            last = "資料不足"
        except Exception as e:
            last = repr(e)
        time.sleep(3 * (k + 1))
    else:
        raise RuntimeError(f"加權指數抓取失敗：{last}")
    notes = []                                                   # 補了什麼，顯示在 App 的提醒裡
    for name, fn in (("Yahoo 日線（另一種抓法）", _yahoo_recent), ("Yahoo 盤中資料", _yahoo_intraday_days)):
        try:
            df, n = _add_missing(df, fn(), name)
            if n:
                notes.append(f"{name}補上 {n} 天")
        except Exception as e:
            print(f"{name} 補資料失敗：", repr(e))
    df = patch_with_twse(df)                                     # 證交所官方開高低收：補缺的、校正有落差的
    notes.append(df.attrs.get("twse_note", ""))
    try:
        tw = fetch_twse_daily()
        if tw["close"].notna().any():                            # 還是缺的日子：至少用證交所收盤補上
            c = tw["close"].dropna()
            c = c[c.index > df.index[-1]]
            if len(c):
                extra = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": 0.0})
                df, n = _add_missing(df, extra, "")
                if n:
                    notes.append(f"證交所收盤補上 {n} 天（沒有開高低）")
        df["Volume"] = tw["amount"].reindex(df.index)
        df.attrs["vol_source"] = "twse"
    except Exception as e:
        print("大盤成交金額抓取失敗：", repr(e))
        df["Volume"] = np.nan
        df.attrs["vol_source"] = "none"
    df.attrs["twse_note"] = "\n".join(x for x in notes if x)
    return df


def _extract(raw, t, single=False):
    """從批次下載的結果取出單一股票（single=True：這一批只有一檔，部分 yfinance 版本不會分層）"""
    if raw is None or len(raw) == 0:
        return None
    if not isinstance(raw.columns, pd.MultiIndex):
        return _clean(raw.copy()) if single else None
    lv0 = raw.columns.get_level_values(0)
    lv1 = raw.columns.get_level_values(1)
    if t in lv0:
        sub = raw[t]
    elif t in lv1:
        sub = raw.xs(t, axis=1, level=1)
    else:
        return None
    return _clean(sub.copy())


def fetch_prices(tickers, period="9mo", batch=None, sleep=1.0, retries=2):
    """分批下載。tickers：Yahoo 代號清單（如 2330.TW）。
    回傳 (prices: {代號: DataFrame}, failed: 沒抓到的代號清單)"""
    batch = batch or cfg.BATCH
    prices, failed = {}, []
    for s in range(0, len(tickers), batch):
        chunk = tickers[s:s + batch]
        raw = None
        for k in range(retries + 1):
            try:
                raw = yf.download(chunk, period=period, interval="1d", group_by="ticker",
                                  auto_adjust=False, progress=False, threads=True)
                if raw is not None and len(raw):
                    break
            except Exception as e:
                print(f"批次 {s // batch + 1} 第 {k + 1} 次失敗：{e!r}")
            time.sleep(3 * (k + 1))
        for t in chunk:
            df = _extract(raw, t, single=len(chunk) == 1)
            if df is None:
                failed.append(t)
            else:
                prices[t] = df
        done = min(s + batch, len(tickers))
        print(f"下載進度 {done}/{len(tickers)}　成功 {len(prices)}　失敗 {len(failed)}", flush=True)
        time.sleep(sleep)
    return prices, failed
