import os
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import yfinance as yf
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")

# 策略一：創高拉回金字塔參數 (30/40/30)
TRANCHE_1 = 0.30
TRANCHE_2 = 0.40
TRANCHE_3 = 0.30
EXT_RATIO = 1.08
LOOKBACK = 30
MIN_PULLBACK = 0.08
MAX_PULLBACK = 0.16
VOL_RATIO = 0.65
STOP_PCT = 0.08
STOP_ADD_PCT = 0.05
RRR = 2.5

# 策略二：低檔起漲爆量突破
BOTTOM_PLATFORM = 30
BOTTOM_MAX_RISE = 0.20
BOTTOM_VOL_MULT = 1.8
BOTTOM_STOP_PCT = 0.07

def to_tw_tick(price: float) -> float:
    if price <= 0: return 0.01
    if price < 10.0: tick = 0.01
    elif price < 50.0: tick = 0.05
    elif price < 100.0: tick = 0.10
    elif price < 500.0: tick = 0.50
    elif price < 1000.0: tick = 1.00
    else: tick = 5.00
    return round(round(price / tick) * tick, 2)

def evaluate_ticker_realtime(ticker: str) -> str:
    """下載數據並執行即時條件檢驗與金字塔點位試算"""
    # 代號補齊
    t = ticker.upper().strip()
    if not (t.endswith(".TW") or t.endswith(".TWO")):
        t += ".TW"

    try:
        df = yf.download(t, period="8mo", interval="1d", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.xs(t, axis=1, level=1) if t in df.columns.levels[1] else df[t]
        df = df.dropna(subset=["Close"])
    except Exception as e:
        return f"❌ 取得 `{ticker}` 資料失敗: {e}"

    if len(df) < 60:
        return f"⚠️ 標的 `{ticker}` 歷史日 K 資料不足（需至少 60 根）。"

    # 指標運算
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA60"] = df["Close"].ewm(span=60, adjust=False).mean()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MAV20"] = df["Volume"].rolling(20).mean()

    curr = to_tw_tick(float(df["Close"].iloc[-1]))
    prev = float(df["Close"].iloc[-2])
    open_p = float(df["Open"].iloc[-1])
    vol = float(df["Volume"].iloc[-1])
    mav20 = float(df["MAV20"].iloc[-1])
    ma5 = float(df["MA5"].iloc[-1])
    ema20 = float(df["EMA20"].iloc[-1])
    ema60 = float(df["EMA60"].iloc[-1])

    diff_pts = curr - prev
    pct_chg = (diff_pts / prev) * 100
    icon = "🔴" if diff_pts > 0 else ("🟢" if diff_pts < 0 else "⚪")

    # ==========================================
    # 策略 1 檢驗：創高拉回 (8% ~ 16% + 量縮)
    # ==========================================
    p1_status = "未達成"
    p1_details = []
    
    search_window = df.iloc[-LOOKBACK:-2]
    B = float(search_window["High"].max()) if not search_window.empty else 0.0
    B_idx = search_window["High"].idxmax() if not search_window.empty else None
    vol_B = float(df.loc[B_idx, "Volume"]) if B_idx is not None else 0.0

    b_price = to_tw_tick(B)
    b2_price = to_tw_tick(B * EXT_RATIO)
    pullback = (B - curr) / B if B > 0 else 0

    # 條件 1-1：回測幅度
    c_pb = (MIN_PULLBACK <= pullback <= MAX_PULLBACK)
    p1_details.append(f"• 回測前高幅度：`{pullback*100:.1f}%` ({'✅ 符合 8-16%' if c_pb else '❌ 未在區間'})")

    # 條件 1-2：量縮
    c_vol = (vol <= VOL_RATIO * vol_B and vol <= mav20 * 1.1) if vol_B > 0 else False
    p1_details.append(f"• 量縮檢驗：今日量對比前高日量 `{vol/vol_B*100:.0f}%` ({'✅ 量縮及格' if c_vol else '❌ 未充分量縮'})")

    # 條件 1-3：右側止跌紅 K
    c_rev = (curr > open_p) and (curr >= ma5)
    p1_details.append(f"• 右側止跌確認：{'✅ 收紅 K 且站上 5MA' if c_rev else '❌ 尚未收紅站上 5MA'}")

    # 條件 1-4：多頭均線
    c_trend = (ema20 > ema60 and curr >= ema60)
    p1_details.append(f"• 均線保護：{'✅ 季線之上多頭排列' if c_trend else '❌ 破季線或均線蓋頭'}")

    if c_pb and c_vol and c_rev and c_trend:
        p1_status = "🎯 **【符合進場標準】**"
    elif c_pb and c_trend:
        p1_status = "👀 **【進入回測觀察區】** (等待右側量縮轉折)"

    # 金字塔三段點位計算
    avg_1 = curr
    avg_2 = to_tw_tick((TRANCHE_1 * curr + TRANCHE_2 * b_price) / 0.7)
    avg_3 = to_tw_tick((TRANCHE_1 * curr + TRANCHE_2 * b_price + TRANCHE_3 * b2_price) / 1.0)
    stop_first = to_tw_tick(curr * (1 - STOP_PCT))
    stop_second = to_tw_tick(b_price * (1 - STOP_ADD_PCT))
    loss_second = (stop_second - b_price) * TRANCHE_2
    breakeven = to_tw_tick((curr * TRANCHE_1 - loss_second) / TRANCHE_1)
    target_price = to_tw_tick(avg_3 * (1 + STOP_PCT * RRR))

    # ==========================================
    # 策略 2 檢驗：低檔起漲爆量突破
    # ==========================================
    p2_status = "未達成"
    low_60 = float(df["Low"].iloc[-60:].min())
    c2_low = ((curr - low_60) / low_60 <= BOTTOM_MAX_RISE) if low_60 > 0 else False
    
    platform_window = df.iloc[-(BOTTOM_PLATFORM + 1):-1]
    neckline = float(platform_window["High"].max()) if not platform_window.empty else 0.0
    c2_break = (curr > neckline)
    c2_vol = (vol / mav20 >= BOTTOM_VOL_MULT) if mav20 > 0 else False
    c2_ema = (curr >= ema60)

    if c2_low and c2_break and c2_vol and c2_ema:
        p2_status = "🚀 **【符合低檔起漲爆量突破】**"

    # ==========================================
    # 訊息排版組合
    # ==========================================
    msg = (
        f"🔍 *【個股即時診斷 · {t}】*\n"
        f"現價：`{curr}` ｜ {icon} `{diff_pts:+.2f}`（`{pct_chg:+.2f}%`）\n"
        f"────────────────────\n"
        f"📊 *策略一：創高拉回金字塔狀態* ➔ {p1_status}\n"
        + "\n".join(p1_details) + "\n\n"
        f"📐 *標準金字塔作戰點位（依現價推算）*：\n"
        f"• 第 1 筆試單 (30%)：`{avg_1}` ｜ 停損守 `{stop_first}` (-8%)\n"
        f"• 第 2 筆加碼 (40%)：突破前高 `{b_price}` ｜ 均價推估 `{avg_2}`\n"
        f"• 第 2 筆防守 (砍加碼)：`{stop_second}` ｜ 第 1 筆保本底線 `{breakeven}`\n"
        f"• 第 3 筆延伸 (30%)：突破 `{b2_price}` ｜ 打滿均價 `{avg_3}`\n"
        f"🎯 *波段目標價*：`{target_price}` (風報比 2.5:1)\n"
        f"────────────────────\n"
        f"🌱 *策略二：低檔爆量起漲狀態* ➔ {p2_status}\n"
        f"• 30 日頸線關卡：`{to_tw_tick(neckline)}` ｜ 今日量比：`{vol/mav20:.1f}x` (門檻 1.8x)\n"
        f"────────────────────\n"
        f"🔗 [TradingView 圖表](https://tw.tradingview.com/chart/?symbol=TWSE%3A{t.replace('.TW','').replace('.TWO','')})"
    )
    return msg

# ==========================================
# Telegram 事件處理器
# ==========================================
async def check_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """處理 /check 2330"""
    if not context.args:
        await update.message.reply_text("請輸入股票代號，例如：`/check 2330` 或 `/check 2454`", parse_mode="Markdown")
        return
    ticker = context.args[0]
    await update.message.reply_text(f"⏳ 正在即時抓取並計算 `{ticker}`，請稍候...", parse_mode="Markdown")
    res = evaluate_ticker_realtime(ticker)
    await update.message.reply_text(res, parse_mode="Markdown", disable_web_page_preview=True)

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """使用者直接輸入 4 碼數字時自動觸發查詢 (例如直接打 2330)"""
    text = update.message.text.strip()
    if re.match(r"^\d{4}$", text):
        await update.message.reply_text(f"⏳ 正在即時診斷 `{text}`...", parse_mode="Markdown")
        res = evaluate_ticker_realtime(text)
        await update.message.reply_text(res, parse_mode="Markdown", disable_web_page_preview=True)

if __name__ == "__main__":
    if not TG_TOKEN:
        print("未設定 TG_BOT_TOKEN，無法啟動機器人。")
        exit(1)

    print("🤖 即時股票查詢機器人啟動中 (監聽 Telegram 訊息)...")
    app = ApplicationBuilder().token(TG_TOKEN).build()
    
    app.add_handler(CommandHandler("check", check_command))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), text_handler))
    
    app.run_polling()
