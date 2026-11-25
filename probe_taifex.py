"""探測 v2：直接試抓期交所「期貨每筆成交資料」每日檔，並試算一次 POC。
只讀不寫，結果全部印在 Actions 的 log。"""
import csv
import io
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, timedelta

import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
URL = ("https://www.taifex.com.tw/file/taifex/Dailydownload/"
       "DailydownloadCSV/Daily_{y}_{m:02d}_{d:02d}.zip")


def show(title):
    print("\n" + "=" * 8, title, "=" * 8)
    sys.stdout.flush()


def parse_rows(raw_csv_bytes):
    """回傳 (表頭, 資料列)。編碼先試 cp950，失敗改 utf-8。"""
    for enc in ("cp950", "utf-8-sig"):
        try:
            text = raw_csv_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            text = None
    if text is None:
        text = raw_csv_bytes.decode("cp950", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    return rows[0], rows[1:]


def calc_poc(header, rows, product="TX"):
    """用逐筆成交算 POC。欄位用名稱找，找不到就回報。"""
    def col(*keys):
        for i, h in enumerate(header):
            if all(k in h for k in keys):
                return i
        return None

    i_prod = col("商品")
    i_month = col("到期")
    i_time = col("時間")
    i_price = col("價格")
    i_vol = col("數量")
    print("欄位位置:", dict(商品=i_prod, 到期=i_month, 時間=i_time,
                          價格=i_price, 數量=i_vol))
    if None in (i_prod, i_month, i_price, i_vol):
        print("找不到必要欄位，表頭:", header)
        return
    sel = [r for r in rows if len(r) > max(i_prod, i_month, i_price, i_vol)
           and r[i_prod].strip() == product]
    print(f"{product} 筆數:", len(sel))
    if not sel:
        return
    months = defaultdict(int)
    for r in sel:
        months[r[i_month].strip()] += 1
    near = sorted(m for m in months if "/" not in m)[0] if months else None
    print("各到期月份筆數:", dict(months), "近月:", near)
    sel = [r for r in sel if r[i_month].strip() == near]
    times = [r[i_time] for r in sel if i_time is not None]
    if times:
        print("成交時間範圍:", min(times), "~", max(times))
    vol = defaultdict(int)
    for r in sel:
        try:
            vol[float(r[i_price])] += int(r[i_vol])
        except ValueError:
            continue
    if not vol:
        print("沒有可用的價格/數量")
        return
    poc = max(vol, key=vol.get)
    print("POC 價位:", poc, "該價位成交量:", vol[poc])
    top = sorted(vol.items(), key=lambda x: -x[1])[:5]
    print("成交量前5價位:", top)
    print("價格範圍:", min(vol), "~", max(vol))


def main():
    show("試抓每日逐筆檔（往前最多 10 天）")
    day = date.today()
    for _ in range(10):
        url = URL.format(y=day.year, m=day.month, d=day.day)
        try:
            r = requests.get(url, headers=HEADERS, timeout=40)
            print(day, "狀態碼:", r.status_code, "大小:", len(r.content))
            if r.status_code == 200 and r.content[:2] == b"PK":
                z = zipfile.ZipFile(io.BytesIO(r.content))
                print("zip 內檔案:", z.namelist())
                raw = z.read(z.namelist()[0])
                header, rows = parse_rows(raw)
                print("表頭:", header)
                print("前3列:", rows[:3])
                print("總列數:", len(rows))
                calc_poc(header, rows)
                break
        except Exception as e:
            print(day, "失敗:", repr(e))
        day -= timedelta(days=1)
    else:
        print("10 天內都沒抓到，改看頁面線索")
        r = requests.get(
            "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData",
            headers=HEADERS, timeout=30)
        for line in r.text.splitlines():
            if re.search(r"zip|csv|download|Dailydownload|onclick", line, re.I):
                print(line.strip()[:200])
    show("完成")
    print("把以上整段 log 貼回來給我。")


if __name__ == "__main__":
    main()
