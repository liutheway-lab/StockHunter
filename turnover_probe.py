"""turnover_probe.py — 一次性探測：用個股即時報價加總，估算「全市場今天到目前的成交金額」（用完可以刪掉）
只在 GitHub Actions 手動執行（turnover_probe.yml），結果只私訊管理者（TG_OWNER_ID）。

做法：對全部上市櫃股票抓一次 MIS 即時報價（欄位 v＝今天累計成交量（張）、z＝最新成交價），
成交金額 ≈ Σ v × 1000 × z。跟 App 上的「總量」比對，看這個算法準不準。
注意：z 是最新成交價，不是每筆成交價，所以是近似值。
"""
import datetime as dt
import os
import time

import requests

import data as D
import rebound_watch as rw

TOK, OWNER = os.getenv("TG_BOT_TOKEN", "").strip(), os.getenv("TG_OWNER_ID", "").strip()


def main():
    universe = D.get_directory().to_dict("records")
    sess = requests.Session()
    try:
        sess.get("https://mis.twse.com.tw/stock/index.jsp", headers=D._MIS_HEAD, timeout=5)
    except Exception:
        pass
    exs = [("tse_" if r["suffix"] == ".TW" else "otc_") + f"{r['code']}.tw" for r in universe]
    total_val, total_lots, n_ok, n_fail = 0.0, 0.0, 0, 0
    for k in range(0, len(exs), rw.BATCH):
        try:
            for q in rw.fetch_batch(sess, exs[k:k + rw.BATCH]):
                v, z = rw._cum_vol(q.get("v")), rw._num(q.get("z"))
                if v and z:
                    total_val += v * 1000 * z
                    total_lots += v
                    n_ok += 1
        except Exception as e:
            n_fail += 1
            print("批次失敗", repr(e)[:80])
        time.sleep(0.2)
    now = dt.datetime.now(rw.TPE)
    text = (f"🔎 全市場成交金額估算（{now:%H:%M}）\n"
            f"估算成交金額：{total_val / 1e8:,.0f} 億\n"
            f"累計成交張數：{total_lots / 1e4:,.0f} 萬張\n"
            f"有成交的股票：{n_ok} 檔（失敗批次 {n_fail}）\n"
            f"請跟 App 的「總量」同時間比對")
    print(text)
    if TOK and OWNER:
        requests.post(f"https://api.telegram.org/bot{TOK}/sendMessage",
                      json={"chat_id": OWNER, "text": text}, timeout=20)


if __name__ == "__main__":
    main()
