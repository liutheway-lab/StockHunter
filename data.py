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


def fetch_twse_turnover(months=4):
    """證交所官方：加權市場每日成交金額（元）。回傳 Series（索引是日期）。
    Yahoo 的加權指數成交量不可靠，所以止跌確認的量能改用這份。"""
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
                    col = j["fields"].index("成交金額")
                    for row in j["data"]:
                        yy, mm, dd = (int(x) for x in row[0].strip().split("/"))
                        out[pd.Timestamp(yy + 1911, mm, dd)] = float(str(row[col]).replace(",", ""))
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
    return pd.Series(out).sort_index()


def fetch_market(period="2y", retries=3):
    """加權指數日線（^TWII）。價格來自 Yahoo；Volume 欄位換成證交所官方的「成交金額」。
    成交金額抓不到時 Volume 全部留空，止跌確認會顯示『無法確認』，而不是用錯的數字。"""
    last = None
    for k in range(retries):
        try:
            raw = yf.download("^TWII", period=period, interval="1d", progress=False, auto_adjust=False)
            df = _clean(raw)
            if df is not None and len(df) >= 70:
                try:
                    s = fetch_twse_turnover()
                    df["Volume"] = s.reindex(df.index)
                    df.attrs["vol_source"] = "twse"
                except Exception as e:
                    print("大盤成交金額抓取失敗：", repr(e))
                    df["Volume"] = np.nan
                    df.attrs["vol_source"] = "none"
                return df
            last = "資料不足"
        except Exception as e:
            last = repr(e)
        time.sleep(3 * (k + 1))
    raise RuntimeError(f"加權指數抓取失敗：{last}")


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
