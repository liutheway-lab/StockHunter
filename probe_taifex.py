"""探測：期交所「近30個交易日 逐筆成交資料」能不能從 GitHub Actions 抓到。
只讀不寫，不會動到任何其他檔案。結果全部印在 Actions 的 log 裡。"""
import re
import sys
import requests

HEADERS = {"User-Agent": "Mozilla/5.0"}
PAGES = [
    "https://www.taifex.com.tw/cht/3/dlFutPrevious30DaysSalesData",
    "https://www.taifex.com.tw/cht/3/futDailyMarketReport",
]


def show(title):
    print("\n" + "=" * 8, title, "=" * 8)


for url in PAGES:
    show(url)
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        print("狀態碼:", r.status_code, "長度:", len(r.text))
        html = r.text
        links = sorted(set(re.findall(r'href="([^"]+\.(?:zip|csv|rpt)[^"]*)"', html, re.I)))
        print("下載連結數:", len(links))
        for x in links[:20]:
            print("  連結:", x)
        forms = re.findall(r"<form[^>]*>", html, re.I)
        print("表單數:", len(forms))
        for f in forms[:5]:
            print("  表單:", f[:200])
        names = sorted(set(re.findall(r'name="([^"]+)"', html)))
        print("欄位名稱:", names[:40])
    except Exception as e:
        print("失敗:", repr(e))
        sys.stdout.flush()

show("完成")
print("把以上整段 log 貼回來給我。")
