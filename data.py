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
