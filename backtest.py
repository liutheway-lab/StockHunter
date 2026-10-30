import os
import pandas as pd
import requests
import yfinance as yf

# ==========================================
# 1. 回測環境與參數設定 (5年跨度)
# ==========================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# 橫跨大型權值、高動能電子與波段飆股測試池
TICKERS = [
    "2382.TW", "2330.TW", "2317.TW", "2454.TW", "3231.TW",
    "2603.TW", "3035.TW", "3443.TW", "2376.TW", "6669.TW"
]

START_DATE = "2021-01-01"
END_DATE = "2026-10-01"

# 金字塔三筆部位分配 (30% / 40% / 30%)
TRANCHE_1 = 0.30
TRANCHE_2 = 0.40
TRANCHE_3 = 0.30
EXT_RATIO = 1.08          # 第 3 筆加碼點：前高 * 1.08 (+8%)

LOOKBACK = 30
MIN_PULLBACK = 0.08       # 修正：拉回至少 8%
MAX_PULLBACK = 0.16       # 修正：最大拉回不超過 16% (避開轉空破線股)
VOL_RATIO = 0.65          # 量縮至突破日的 65% 以下
STOP_PCT = 0.08           # 修正：第一筆停損收緊至 8% (小賠原則)
STOP_ADD_PCT = 0.05       # 第二筆加碼防守：跌破前高 5% 砍單
RRR = 2.5                 # 風報比目標 2.5 倍


def run_backtest_ticker(t, df):
    if len(df) < 100:
        return []

    # 計算技術指標
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA60"] = df["Close"].ewm(span=60, adjust=False).mean()
    df["MA5"] = df["Close"].rolling(5).mean()
    df["MAV20"] = df["Volume"].rolling(20).mean()
    df["MAV5"] = df["Volume"].rolling(5).mean()

    trades = []
    in_trade = False
    stage = 0
    entry_p, B_p, B2_p = 0.0, 0.0, 0.0
    entry_date = ""

    for i in range(100, len(df)):
        cur = df.iloc[i]
        date = df.index[i].strftime("%Y-%m-%d")
        close = float(cur["Close"])
        open_p = float(cur["Open"])
        vol = float(cur["Volume"])

        # -----------------------------
        # 1. 尋找進場訊號 (無持股時)
        # -----------------------------
        if not in_trade:
            # 尋找前波高點 B (近 30 天，排除近 2 天)
            search_window = df.iloc[i - LOOKBACK: i - 2]
            B = float(search_window["High"].max())
            B_idx = search_window["High"].idxmax()
            vol_B = float(df.loc[B_idx, "Volume"])

            pullback = (B - close) / B

            # 【關鍵升級】：右側紅 K 止跌確認 (Close > Open 且 Close >= MA5)
            is_reversal = (close > open_p) and (close >= cur["MA5"])

            if (MIN_PULLBACK <= pullback <= MAX_PULLBACK and
                is_reversal and
                vol <= VOL_RATIO * vol_B and
                vol <= cur["MAV20"] * 1.1 and
                cur["EMA20"] > cur["EMA60"] and
                close >= cur["EMA60"] and
                cur["MAV5"] >= 1000 * 1000):

                in_trade = True
                stage = 1
                entry_p = close
                B_p = B
                B2_p = B * EXT_RATIO
                entry_date = date
                continue

        # -----------------------------
        # 2. 持倉狀態管理 (有持股時)
        # -----------------------------
        if in_trade:
            # 階段 1：持有 30% 基本水位
            if stage == 1:
                stop_loss = entry_p * (1 - STOP_PCT)
                if close <= stop_loss:
                    trades.append({
                        "ticker": t, "entry": entry_date, "exit": date,
                        "pnl": -STOP_PCT * TRANCHE_1, "tag": "Stage 1 Stop"
                    })
                    in_trade = False
                elif close >= B_p:
                    stage = 2

            # 階段 2：持有 70% 放大水位
            elif stage == 2:
                stop_second = B_p * (1 - STOP_ADD_PCT)
                avg_2 = (TRANCHE_1 * entry_p + TRANCHE_2 * B_p) / 0.7
                target_p = avg_2 * (1 + STOP_PCT * RRR)

                if close <= stop_second:
                    # 假突破砍第二筆，第一筆保本退場 (損益打平 0.0%)
                    trades.append({
                        "ticker": t, "entry": entry_date, "exit": date,
                        "pnl": 0.0, "tag": "Cut Add-on Breakeven"
                    })
                    in_trade = False
                elif close >= target_p:
                    trades.append({
                        "ticker": t, "entry": entry_date, "exit": date,
                        "pnl": (target_p - avg_2) / avg_2 * 0.7, "tag": "Stage 2 Target"
                    })
                    in_trade = False
                elif close >= B2_p:
                    stage = 3

            # 階段 3：100% 部位主升段狂奔
            elif stage == 3:
                stop_third = B_p # 鎖定在前高 B
                avg_3 = (TRANCHE_1 * entry_p + TRANCHE_2 * B_p + TRANCHE_3 * B2_p) / 1.0
                target_p = avg_3 * (1 + STOP_PCT * RRR)

                if close <= stop_third:
                    trades.append({
                        "ticker": t, "entry": entry_date, "exit": date,
                        "pnl": (stop_third - avg_3) / avg_3 * 1.0, "tag": "Stage 3 Trailing Stop"
                    })
                    in_trade = False
                elif close >= target_p:
                    trades.append({
                        "ticker": t, "entry": entry_date, "exit": date,
                        "pnl": (target_p - avg_3) / avg_3 * 1.0, "tag": "Stage 3 Target"
                    })
                    in_trade = False

    return trades


print(f"正在下載歷史資料並執行 10 年回測 ({START_DATE} ~ {END_DATE})...")
all_trades = []

for t in TICKERS:
    try:
        data = yf.download(t, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(data.columns, pd.MultiIndex):
            data = data.xs(t, axis=1, level=1) if t in data.columns.levels[1] else data[t]
        trades = run_backtest_ticker(t, data)
        all_trades.extend(trades)
    except Exception as e:
        print(f"下載/回測 {t} 失敗: {e}")

df_res = pd.DataFrame(all_trades)

if df_res.empty:
    report = f"⚠️ 在 5 年期間 ({START_DATE} ~ {END_DATE}) 未觸發任何交易訊號。"
else:
    wins = df_res[df_res["pnl"] > 0]
    losses = df_res[df_res["pnl"] < 0]
    breakevens = df_res[df_res["pnl"] == 0]

    win_rate = len(wins) / len(df_res) * 100
    avg_win = wins["pnl"].mean() * 100 if not wins.empty else 0
    avg_loss = losses["pnl"].mean() * 100 if not losses.empty else 0
    total_ret = df_res["pnl"].sum() * 100
    p_l_ratio = f"{abs(avg_win / avg_loss):.2f}:1" if avg_loss != 0 else "N/A"

    report = (
        f"📊 *【三段金字塔策略 · 5 年期歷史回測報告】*\n"
        f"📅 期間：`{START_DATE}` ~ `{END_DATE}`\n"
        f"────────────────────\n"
        f"• 總交易次數：`{len(df_res)}` 次\n"
        f"• 獲利次數：`{len(wins)}` 次\n"
        f"• 保本平手：`{len(breakevens)}` 次（假突破及時撤退）\n"
        f"• 停損次數：`{len(losses)}` 次（第1筆小賠出場）\n"
        f"────────────────────\n"
        f"🏆 *勝率統計*：\n"
        f"  - 真實勝率 (排除打平)：`{len(wins) / (len(wins) + len(losses)) * 100:.1f}%`\n"
        f"  - 綜合勝率 (含打平)：`{win_rate:.1f}%`\n"
        f"📈 *盈虧比與報酬*：\n"
        f"  - 平均單筆獲利：`+{avg_win:.2f}%`\n"
        f"  - 平均單筆虧損：`{avg_loss:.2f}%`\n"
        f"  - 風報比 (盈虧比)：`{p_l_ratio}`\n"
        f"  - 10年策略累計報酬率：`{total_ret:+.2f}%`\n"
        f"────────────────────"
    )

print(report)

if TG_TOKEN and TG_CHAT:
    for cid in [cid.strip() for cid in TG_CHAT.split(",") if cid.strip()]:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": report, "parse_mode": "Markdown"}
        )
