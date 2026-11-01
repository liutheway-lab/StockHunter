"""ping.py — 第 1 步：連線測試
檢查三件事，並把結果傳到你的 Telegram：
1. Telegram 能不能發訊息
2. Yahoo 行情抓不抓得到（加權指數）
3. 證交所官方股票清單抓不抓得到
"""
import io
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT = os.getenv("TG_CHAT_ID", "").strip()


def check_yahoo():
    try:
        df = yf.download("^TWII", period="1mo", interval="1d", progress=False, auto_adjust=True)
        if df is None or df.empty:
            return "❌ Yahoo 行情：抓不到資料"
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        last = df.dropna(subset=["Close"]).iloc[-1]
        return f"✅ Yahoo 行情：加權指數 {float(last['Close']):,.0f}（{df.index[-1]:%Y-%m-%d}）"
    except Exception as e:
        return f"❌ Yahoo 行情：{e}"


def check_twse():
    try:
        import lookup
        d = lookup.load_directory()
        n1 = int((d["market"] == "上市").sum())
        n2 = int((d["market"] == "上櫃").sum())
        if n1 == 0 or n2 == 0:
            return f"❌ 證交所清單：上市 {n1} 檔、上櫃 {n2} 檔（有一邊抓不到）"
        return f"✅ 證交所清單：上市 {n1} 檔、上櫃 {n2} 檔"
    except Exception as e:
        return f"❌ 證交所清單：{e}"


def send(text):
    if not TG_TOKEN or not TG_CHAT:
        print("沒有讀到 TG_BOT_TOKEN 或 TG_CHAT_ID，請檢查 GitHub Secrets 的名稱。")
        print(text)
        raise SystemExit(1)
    for cid in [c.strip() for c in TG_CHAT.split(",") if c.strip()]:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text},
            timeout=20,
        )
        print("Telegram 回應：", r.status_code, r.text[:200])
        if not r.ok:
            raise SystemExit(1)


if __name__ == "__main__":
    now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
    lines = [f"🏓 連線測試 {now}", check_yahoo(), check_twse(), "✅ Telegram：你收到這則訊息，代表發送正常"]
    msg = "\n".join(lines)
    print(msg)
    send(msg)
