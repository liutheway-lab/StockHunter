"""history_check.py — 一次性檢查：2610 的股價資料最長能抓到多久（用完可以刪掉）
只在 GitHub Actions 手動執行（history_check.yml）。結果印在日誌，也會私訊到 Telegram。
"""
import os

import yfinance as yf

CODE = "2610.TW"
lines = [f"📅 資料長度檢查 {CODE}"]

for period in ("10y", "max"):
    try:
        h = yf.Ticker(CODE).history(period=period, interval="1mo", auto_adjust=False)
        if h is None or h.empty:
            lines.append(f"{period}：沒有抓到資料")
            continue
        lines.append(
            f"{period}：{len(h)} 個月，最早 {h.index[0]:%Y-%m}，最新 {h.index[-1]:%Y-%m}\n"
            f"  最高 {h['High'].max():.2f}、最低 {h['Low'].min():.2f}（月線高低）"
        )
    except Exception as e:
        lines.append(f"{period}：抓取失敗 {e!r}"[:200])

text = "\n".join(lines)
print(text)

try:
    import os
    import requests
    owner = os.getenv("TG_OWNER_ID", "").strip()
    tok = os.getenv("TG_BOT_TOKEN", "").strip()
    if owner and tok:          # 只私訊管理者，不發到群組
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": owner, "text": text}, timeout=20)
    else:
        print("沒有設定 TG_OWNER_ID，只印在日誌，不傳到群組")
except Exception as e:
    print("傳到 Telegram 失敗：", repr(e))
