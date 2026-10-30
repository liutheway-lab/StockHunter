import os
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import yfinance as yf

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")


def send_tg(text: str):
    if not TG_TOKEN or not TG_CHAT:
        print(text)
        return
    for cid in [c.strip() for c in TG_CHAT.split(",") if c.strip()]:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )


def check_crash_conditions():
    now = datetime.now(ZoneInfo("Asia/Taipei"))

    # 下載監控母體：加權指數、櫃買指數、恐慌指數 VIX
    tickers = ["^TWII", "^TWOII", "^VIX"]
    data = yf.download(
        tickers,
        period="6mo",
        interval="1d",
        group_by="ticker",
        auto_adjust=True,
        progress=False,
    )

    alerts = []
    status_summary = []

    # 1. 檢驗加權指數 (大盤趨勢與季線防守)
    twii = (
        data["^TWII"].dropna(subset=["Close"])
        if "^TWII" in data
        else pd.DataFrame()
    )
    if not twii.empty and len(twii) >= 60:
        close = float(twii["Close"].iloc[-1])
        prev = float(twii["Close"].iloc[-2])
        ema20 = float(twii["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
        ema60 = float(twii["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
        day_chg = (close - prev) / prev * 100

        if close < ema60 and ema20 < ema60:
            alerts.append(
                f"🚨 *【大盤空頭警報】加權指數跌破季線！*\n"
                f"• 現指數：`{close:,.0f}` (單日 `{day_chg:+.2f}%`)\n"
                f"• 均線狀態：20EMA 已下穿 60EMA 呈死亡交叉。\n"
                f"👉 *操作指令*：**全面暫停所有做多買進訊號，持股強制進入防禦狀態**。"
            )
        else:
            status_summary.append(
                f"🟢 *加權指數*：`{close:,.0f}` (站上季線，大盤多頭架構尚存)"
            )

    # 2. 檢驗櫃買指數 (中小型主力撤出檢測)
    twoii = (
        data["^TWOII"].dropna(subset=["Close"])
        if "^TWOII" in data
        else pd.DataFrame()
    )
    if not twoii.empty and len(twoii) >= 20:
        c_otc = float(twoii["Close"].iloc[-1])
        p_otc = float(twoii["Close"].iloc[-2])
        otc_chg = (c_otc - p_otc) / p_otc * 100

        if otc_chg <= -2.5:
            alerts.append(
                f"⚠️ *【櫃買重挫警報】中小型股爆踩踏！*\n"
                f"• OTC 單日大跌：`{otc_chg:+.2f}%`\n"
                f"👉 *操作指令*：高 Beta 電子股易出現補跌，嚴格檢查 Google 試算表持股是否破線。"
            )
        else:
            status_summary.append(
                f"🟢 *櫃買指數*：單日 `{otc_chg:+.2f}%` (未觸發流動性踩踏)"
            )

    # 3. 檢驗 VIX 恐慌指數 (國際總經黑天鵝)
    vix = (
        data["^VIX"].dropna(subset=["Close"]) if "^VIX" in data else pd.DataFrame()
    )
    if not vix.empty and len(vix) >= 5:
        cur_vix = float(vix["Close"].iloc[-1])
        prev_vix = float(vix["Close"].iloc[-2])
        vix_pct = (cur_vix - prev_vix) / prev_vix * 100

        if cur_vix >= 25.0 or vix_pct >= 20.0:
            alerts.append(
                f"💥 *【全球恐慌爆發】VIX 指數飆升至 `{cur_vix:.1f}`！*\n"
                f"• 單日激增：`{vix_pct:+.1f}%`\n"
                f"👉 *操作指令*：美股與外資極端避險，次日台股開低機率極高，嚴禁盲目摸底。"
            )
        else:
            status_summary.append(
                f"🟢 *恐慌指數 VIX*：`{cur_vix:.1f}` (市場處於正常波動區間)"
            )

    # 4. 推播邏輯：若有警報發出紅色熔斷提醒；若完全正常則只在每日收盤回報平靜
    if alerts:
        msg = (
            f"🔥 *【台股市場崩盤防禦警報 · 熔斷觸發】*\n"
            f"📅 檢測時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
            f"────────────────────\n"
            + "\n\n".join(alerts)
            + "\n────────────────────\n"
            f"🛑 *系統建議*：夜間選股名單即使有跳出標的，亦建議放棄進場，保持現金為王。"
        )
        send_tg(msg)
    else:
        # 全部正常時的日常報平安回報
        msg = (
            f"🛡️ *【市場風險哨兵日報 · 環境安全】*\n"
            f"📅 檢測時間：`{now.strftime('%Y-%m-%d %H:%M')}`\n"
            f"────────────────────\n"
            + "\n".join(status_summary)
            + "\n────────────────────\n"
            f"✅ 目前未達崩盤熔斷門檻，多頭作戰策略可按原定紀律運行。"
        )
        send_tg(msg)


if __name__ == "__main__":
    check_crash_conditions()
