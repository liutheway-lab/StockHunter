"""探測 v3：確認逐筆檔涵蓋的日期與時段、以及檔案公布時間。
只讀不寫，結果全部印在 Actions 的 log。"""
import csv
import io
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date, timedelta

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
URL = ("https://www.taifex.com.tw/file/taifex/Dailydownload/"
       "DailydownloadCSV/Daily_{y}_{m:02d}_{d:02d}.zip")


def show(title):
    print("\n" + "=" * 8, title, "=" * 8)
    sys.stdout.flush()


def decode(raw):
    for enc in ("cp950", "utf-8-sig"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp950", errors="replace")


def bucket(t):
    """把成交時間(HHMMSS)分成日盤/夜盤/其他。"""
    t = t.strip().zfill(6)
    hhmm = int(t[:4])
    if 845 <= hhmm <= 1345:
        return "日盤 08:45-13:45"
    if hhmm >= 1500:
        return "夜盤前段 15:00-23:59"
    if hhmm <= 500:
        return "夜盤後段 00:00-05:00"
    return "其他"


def analyze(day, content):
    z = zipfile.ZipFile(io.BytesIO(content))
    rows = list(csv.reader(io.StringIO(decode(z.read(z.namelist()[0])))))
    header, rows = rows[0], rows[1:]
    tx = [r for r in rows if len(r) >= 6 and r[1].strip() == "TX"
          and "/" not in r[2]]
    near = sorted({r[2].strip() for r in tx})
    print("TX 單一月份合約:", near, "筆數:", len(tx))
    if not tx:
        return
    near_m = near[0]
    tx = [r for r in tx if r[2].strip() == near_m]
    print("近月:", near_m, "筆數:", len(tx))
    by_date = defaultdict(Counter)
    tmin, tmax = {}, {}
    for r in tx:
        d, t = r[0].strip(), r[3].strip().zfill(6)
        by_date[d][bucket(t)] += 1
        tmin[d] = min(tmin.get(d, t), t)
        tmax[d] = max(tmax.get(d, t), t)
    for d in sorted(by_date):
        print(f"成交日期 {d}: 時間 {tmin[d]}~{tmax[d]}", dict(by_date[d]))


def main():
    show("最近 6 天的檔案：大小、上傳時間、日期與時段分布")
    day = date.today()
    for _ in range(6):
        url = URL.format(y=day.year, m=day.month, d=day.day)
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            lm = r.headers.get("Last-Modified")
            print(f"\n檔名日期 {day} 狀態碼 {r.status_code} "
                  f"大小 {len(r.content)} 上傳時間(GMT) {lm}")
            if r.status_code == 200 and r.content[:2] == b"PK":
                analyze(day, r.content)
        except Exception as e:
            print(day, "失敗:", repr(e))
        day -= timedelta(days=1)
    show("完成")
    print("把以上整段 log 貼回來給我。")


if __name__ == "__main__":
    main()
