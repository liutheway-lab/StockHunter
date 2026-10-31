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
        url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        r.encoding = "ms950"
        df = pd.read_html(io.StringIO(r.text))[0]
        df.columns = df.iloc[0]
        df = df.iloc[1:]
        df = df[df["CFICode"] == "ESVUFR"]
        return f"✅ 證交所清單：上市股票 {len(df)} 檔"
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
