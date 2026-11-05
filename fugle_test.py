"""fugle_test.py — 一次性測試：富果的「大盤、個股成交量」跟看盤軟體一不一樣
在 GitHub 的 Actions → fugle_test → Run workflow 手動執行，結果會印在紀錄裡，也私訊給管理者。"""
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

KEY = os.environ.get("FUGLE_API_KEY", "").strip()
BASE = "https://api.fugle.tw/marketdata/v1.0/stock"
TOKEN, OWNER = os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_OWNER_ID", "")
now = datetime.now(ZoneInfo("Asia/Taipei"))
today = now.strftime("%Y-%m-%d")
out = [f"🧪 富果成交量測試　{now:%m/%d %H:%M}"]


def get(path, **params):
    r = requests.get(BASE + path, params=params, headers={"X-API-KEY": KEY}, timeout=20)
    try:
        return r.status_code, r.json()
    except Exception:
        return r.status_code, r.text[:200]


def yi(v):
    return f"{v / 1e8:,.1f} 億"


if not KEY:
    out.append("❌ 沒有讀到 FUGLE_API_KEY")
else:
    # 1. 加權指數：即時報價（收盤後就是今天全天）
    code, j = get("/intraday/quote/IX0001")
    print("IX0001 quote", code, json.dumps(j, ensure_ascii=False)[:600])
    if code == 200 and isinstance(j, dict):
        tot = j.get("total") or {}
        out.append("【加權指數・即時報價】")
        out.append(f"收盤 {j.get('closePrice') or j.get('lastPrice')}")
        out.append(f"成交金額 {yi(tot['tradeValue']) if tot.get('tradeValue') else tot}")
        if tot.get("tradeVolume") is not None:
            out.append(f"成交量欄位 {tot['tradeVolume']:,}")
    else:
        out.append(f"加權指數報價失敗 {code} {str(j)[:100]}")

    # 2. 加權指數：日 K（含成交金額）
    code, j = get("/historical/candles/IX0001", **{"from": today, "to": today, "timeframe": "D",
                                                       "fields": "open,high,low,close,volume,turnover"})
    print("IX0001 candle", code, json.dumps(j, ensure_ascii=False)[:600])
    if code == 200 and isinstance(j, dict) and j.get("data"):
        d = j["data"][0]
        out.append("\n【加權指數・日K】")
        out.append(f"日期 {d.get('date')}　收 {d.get('close')}")
        out.append(f"volume {d.get('volume'):,}（日K 單位：股數）" if d.get("volume") is not None else "沒有 volume")
        if d.get("turnover") is not None:
            out.append(f"成交金額 {yi(d['turnover'])}")
    else:
        out.append(f"\n加權指數日K失敗 {code} {str(j)[:100]}")

    # 2.5 台積電：即時報價的全日累計（看盤軟體的「總量」應該對這個）
    code, j = get("/intraday/quote/2330")
    print("2330 quote", code, json.dumps(j, ensure_ascii=False)[:800])
    if code == 200 and isinstance(j, dict):                  # 完整欄位清單（找有沒有 avgPrice／均價欄位，不被上面的 800 字截掉）
        print("2330 quote 所有欄位：", list(j.keys()))
        for k, v in j.items():
            if isinstance(v, dict):
                print("  ", k, "→", list(v.keys()))
        print("2330 avgPrice / 均價相關：", {k: v for k, v in j.items() if "avg" in k.lower()})
    if code == 200 and isinstance(j, dict):
        tot = j.get("total") or {}
        out.append("\n【台積電 2330・即時報價】")
        out.append(f"收盤 {j.get('closePrice') or j.get('lastPrice')}")
        out.append(f"成交量 {tot.get('tradeVolume'):,} 張" if tot.get("tradeVolume") is not None else f"total={tot}")
        if tot.get("tradeValue"):
            out.append(f"成交金額 {yi(tot['tradeValue'])}")
        for k, nm in (("tradeVolumeAtBid", "內盤"), ("tradeVolumeAtAsk", "外盤")):
            if tot.get(k) is not None:
                out.append(f"{nm} {tot[k]:,}")
    else:
        out.append(f"\n台積電報價失敗 {code} {str(j)[:100]}")

    # 3. 台積電：日 K（volume 是「股」，除以 1000 就是張）
    code, j = get("/historical/candles/2330", **{"from": today, "to": today, "timeframe": "D",
                                                     "fields": "open,high,low,close,volume,turnover"})
    print("2330 candle", code, json.dumps(j, ensure_ascii=False)[:600])
    if code == 200 and isinstance(j, dict) and j.get("data"):
        d = j["data"][0]
        out.append("\n【台積電 2330・日K】")
        out.append(f"收 {d.get('close')}")
        out.append(f"成交量 {d['volume'] / 1000:,.0f} 張")
        if d.get("turnover") is not None:
            out.append(f"成交金額 {yi(d['turnover'])}")
    else:
        out.append(f"\n台積電日K失敗 {code} {str(j)[:100]}")


    # ---------- 歷史量比對（看盤軟體 11/02：大盤 7589 億、台積電 34,279 張）----------
    frm = (now - timedelta(days=9)).strftime("%Y-%m-%d")

    def hist(sym):
        c, jj = get(f"/historical/candles/{sym}", **{"from": frm, "to": today, "timeframe": "D", "sort": "asc",
                                                        "fields": "open,high,low,close,volume,turnover"})
        print(sym, "hist", c, str(jj)[:300])
        return (jj.get("data") or []) if c == 200 and isinstance(jj, dict) else []

    sh = lambda d: d[5:].replace("-", "/")
    out.append("\n══ 歷史量比對 ══")
    out.append("看盤軟體 11/02：大盤 7589 億")
    out.append("　　　　　　　　台積電 34,279 張")
    out.append("\n【富果日K・大盤金額】")
    for d in hist("IX0001"):
        out.append(f"{sh(d['date'])}　{d['turnover'] / 1e8:,.0f} 億" if d.get("turnover") else f"{sh(d['date'])}　無金額")
    out.append("\n【富果日K・台積電 張】")
    for d in hist("2330"):
        out.append(f"{sh(d['date'])}　{d['volume'] / 1000:,.0f}")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "yfinance", "pandas", "lxml"], check=True)
        import data
        sr = data.fetch_twse_turnover(months=1)
        out.append("\n【證交所官方・大盤金額（現在 app 用）】")
        for d, v in sr.iloc[-6:].items():
            out.append(f"{d:%m/%d}　{v / 1e8:,.0f} 億")
    except Exception as e:
        out.append(f"\n證交所官方抓不到：{str(e)[:60]}")
    try:
        import yfinance as yf
        hh = yf.Ticker("2330.TW").history(period="10d", auto_adjust=False)
        out.append("\n【Yahoo・台積電 張（現在 app 用）】")
        for d, v in hh["Volume"].iloc[-6:].items():
            out.append(f"{d:%m/%d}　{v / 1000:,.0f}")
    except Exception as e:
        out.append(f"\nYahoo 抓不到：{str(e)[:60]}")
    out.append("\n請拿去跟看盤軟體比：")
    out.append("大盤總成交金額")
    out.append("台積電今天成交張數")

text = "\n".join(out)
print(text)
if TOKEN and OWNER:
    requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage", json={"chat_id": OWNER, "text": text}, timeout=20)
