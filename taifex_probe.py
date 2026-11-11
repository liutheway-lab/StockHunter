"""taifex_probe.py — 一次性探測：期交所的「期貨每日成交資料」能不能自動下載、能回溯到多久（用完可以刪掉）
目的：之後要用台指期的 5 分 K 歷史資料回測進出場訊號。只在 GitHub Actions 手動執行（taifex_probe.yml）。
每個日期列出：能不能下載、檔案大小、台指期筆數、日盤開高低收與成交量、夜盤筆數。結果只私訊管理者。
"""
import html
import io
import os
import zipfile

import pandas as pd
import requests

URL = "https://www.taifex.com.tw/file/taifex/Dailydownload/DailydownloadCSV/Daily_{d}.zip"
DATES = ["2026_11_10", "2026_10_14", "2025_04_07", "2024_08_05", "2020_03_19", "2015_08_24"]
HEAD = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData"}


def decode(raw):
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("cp950", errors="ignore")


def parse_ticks(zbytes):
    """回傳台指期近月的逐筆資料 DataFrame（欄位：time、price、qty）；格式不認得回 (None, 說明)"""
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    name = z.namelist()[0]
    text = decode(z.read(name))
    df = pd.read_csv(io.StringIO(text), dtype=str, skipinitialspace=True)
    df.columns = [str(c).strip() for c in df.columns]
    if df.shape[1] < 6:
        return None, f"欄位只有 {df.shape[1]} 個：{list(df.columns)[:6]}"
    c = list(df.columns)                           # 日期、商品、到期月份、時間、價格、數量
    df = df.rename(columns={c[1]: "prod", c[2]: "exp", c[3]: "time", c[4]: "price", c[5]: "qty"})
    df["prod"] = df["prod"].str.strip()
    df = df[(df["prod"] == "TX") & (~df["exp"].str.contains("/", na=False))].copy()
    if df.empty:
        return None, "沒有 TX 近月資料"
    near = sorted(df["exp"].str.strip().unique())[0]
    df = df[df["exp"].str.strip() == near].copy()
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce")
    df["time"] = pd.to_numeric(df["time"], errors="coerce")
    df = df.dropna(subset=["price", "qty", "time"])
    return df, f"{name}｜近月 {near}"


def summarize(d):
    out = [d.replace("_", "-")]
    try:
        r = requests.get(URL.format(d=d), headers=HEAD, timeout=90)
    except Exception as e:
        return out + [f"　連線失敗 {repr(e)[:30]}"]
    out.append(f"　HTTP {r.status_code}｜{len(r.content) / 1024:,.0f} KB")
    if r.status_code != 200 or not r.content.startswith(b"PK"):
        return out + ["　不是 zip 檔（沒有這天或被擋）"]
    try:
        df, info = parse_ticks(r.content)
    except Exception as e:
        return out + [f"　解析失敗 {repr(e)[:40]}"]
    if df is None:
        return out + [f"　{info}"]
    out.append(f"　{info}")
    day = df[(df["time"] >= 84500) & (df["time"] <= 134500)].sort_values("time", kind="stable")
    night = df[(df["time"] >= 150000) | (df["time"] < 50000)]
    if day.empty:
        out.append("　沒有日盤資料")
    else:
        p = day["price"]
        out += [f"　日盤 開{p.iloc[0]:,.0f} 高{p.max():,.0f}", f"　　　 低{p.min():,.0f} 收{p.iloc[-1]:,.0f}",
                f"　日盤量 {int(day['qty'].sum()):,} 口（{len(day):,} 筆）"]
    out.append(f"　夜盤 {len(night):,} 筆")
    return out


def main():
    lines = ["期交所逐筆資料探測", ""]
    for d in DATES:
        lines += summarize(d) + [""]
    lines.append("看 HTTP 200 能回溯到哪一年")
    text = "\n".join(lines)
    print(text)
    owner, tok = os.getenv("TG_OWNER_ID", "").strip(), os.getenv("TG_BOT_TOKEN", "").strip()
    if owner and tok:
        try:
            requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                          json={"chat_id": owner, "text": "<pre>" + html.escape(text) + "</pre>", "parse_mode": "HTML"}, timeout=20)
        except Exception as e:
            print("傳到 Telegram 失敗：", repr(e))
    else:
        print("沒有設定 TG_OWNER_ID，只印在日誌")


if __name__ == "__main__":
    main()
