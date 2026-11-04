"""volume_profile.py — 一次性量測：台股盤中各時段的累計成交量，平均佔「全日」（09:00～13:30）的比例。
在 GitHub Actions → volume_profile → Run workflow 手動執行一次。結果印在紀錄裡，也私訊給管理者。
量完把結果告訴我，我會寫進 config.py，之後就不用再跑。

資料來源：yfinance 的 5 分鐘資料（最近約 60 個交易日），抽樣流動性高的股票。
注意：Yahoo 的成交量跟證交所不一定一樣，但「比例」受影響很小，拿來換算夠用。
"""
import os
from datetime import time as dtime

import numpy as np
import pandas as pd
import requests
import yfinance as yf

TOKEN, OWNER = os.environ.get("TG_BOT_TOKEN", ""), os.environ.get("TG_OWNER_ID", "")
TZ = "Asia/Taipei"

# 流動性高、交易正常的上市股票（分散各產業）
STOCKS = ["2330", "2317", "2454", "2382", "2308", "2881", "2882", "2891", "2303", "1301",
          "1303", "2412", "3231", "2603", "2609", "2615", "3008", "2357", "2002", "1216",
          "2886", "2884", "2885", "2892", "5880", "3037", "2379", "3045", "4938", "2395",
          "2327", "3711", "6669", "2408", "3034", "2344", "2345", "6505", "1101", "1102"]

# 要量的時段：累計量＝開始時間早於這個時間的 5 分鐘 K 棒加總
CHECK = ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "12:50", "13:00", "13:15", "13:25", "13:30"]


def fetch(code):
    try:
        df = yf.download(f"{code}.TW", period="60d", interval="5m", progress=False, auto_adjust=False)
    except Exception as e:
        print(code, "抓取失敗", repr(e)[:100])
        return None
    if df is None or df.empty:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    if "Volume" not in df.columns:
        return None
    idx = pd.DatetimeIndex(df.index)
    if idx.tz is None:
        idx = idx.tz_localize("UTC")
    df.index = idx.tz_convert(TZ)
    return df


def day_profiles(df):
    """每個交易日：各檢查時段的累計量佔全日比例。只用 09:00～13:29 開始的 K 棒（一般交易時段）"""
    out = []
    for _, g in df.groupby(df.index.date):
        g = g.between_time("09:00", "13:29")
        total = float(g["Volume"].sum())
        if total <= 0 or len(g) < 50:          # K 棒太少（停牌或資料缺）就跳過
            continue
        row = {}
        for t in CHECK:
            hh, mm = map(int, t.split(":"))
            before = g.index.time < dtime(hh, mm)
            row[t] = float(g["Volume"][before].sum()) / total
        out.append(row)
    return out


def main():
    rows, used = [], 0
    for code in STOCKS:
        df = fetch(code)
        if df is None:
            continue
        ps = day_profiles(df)
        if ps:
            used += 1
            rows.extend(ps)
    if not rows:
        msg = "⚠️ 量測失敗：沒有抓到任何分鐘資料（Yahoo 可能暫時連不上）"
        print(msg)
        send(msg)
        return
    data = pd.DataFrame(rows)
    lines = [f"📐 盤中成交量比例量測（{used} 檔、{len(data)} 個交易日）",
             "時段｜中位數｜25%～75%"]
    print(lines[0])
    print(lines[1])
    for t in CHECK:
        s = data[t]
        med, p25, p75 = s.median(), s.quantile(0.25), s.quantile(0.75)
        line = f"{t}｜{med * 100:.1f}%｜{p25 * 100:.1f}%～{p75 * 100:.1f}%"
        print(line)
        lines.append(line)
    text = "\n".join(lines)
    send(text)


def send(text):
    print(text)
    if TOKEN and OWNER:
        try:
            requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                          data={"chat_id": OWNER, "text": text}, timeout=15)
        except Exception as e:
            print("私訊失敗", repr(e)[:100])


if __name__ == "__main__":
    main()
