import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime

# ==========================================
# 1. 回測參數設定
# ==========================================
TICKERS = [
    "2382.TW", "2330.TW", "2317.TW", "2454.TW", "3231.TW", 
    "2603.TW", "3035.TW", "3443.TW", "2376.TW", "6669.TW"
] # 測試池：AI伺服器、權值股與高波段動能股

START_DATE = "2023-01-01"
END_DATE = "2026-10-01"

TRANCHE_1 = 0.30
TRANCHE_2 = 0.40
TRANCHE_3 = 0.30
EXT_RATIO = 1.08
LOOKBACK = 30
MAX_PULLBACK = 0.20
MIN_PULLBACK = 0.14
VOL_RATIO = 0.60
STOP_PCT = 0.10
STOP_ADD_PCT = 0.05
RRR = 2.5

def run_backtest_for_ticker(ticker, data):
    df = data.copy()
    if len(df) < 80:
        return []

    # 計算技術指標
    df["EMA20"] = df["Close"].ewm(span=20, adjust=False).mean()
    df["EMA60"] = df["Close"].ewm(span=60, adjust=False).mean()
    df["MAV20"] = df["Volume"].rolling(20).mean()
    df["MAV5"] = df["Volume"].rolling(5).mean()

    trades = []
    in_trade = False
    stage = 0
    entry_p = 0.0
    B_p = 0.0
    B2_p = 0.0
    entry_date = None

    for i in range(80, len(df)):
        cur_row = df.iloc[i]
        date = df.index[i].strftime("%Y-%m-%d")
        close = float(cur_row["Close"])
        vol = float(cur_row["Volume"])

        # -----------------------------
        # 1. 尋找進場訊號 (無持股時)
        # -----------------------------
        if not in_trade:
            # 尋找前波高點 B (近 30 天，排除近 2 天)
            search_window = df.iloc[i-LOOKBACK : i-2]
            B = float(search_window["High"].max())
            B_idx = search_window["High"].idxmax()
            vol_B = float(df.loc[B_idx, "Volume"])

            # 檢查條件：拉回 14%~20%、量縮至突破日 60% 以下、站上 60EMA 且 20EMA > 60EMA
            pullback = (B - close) / B
            if (MIN_PULLBACK <= pullback <= MAX_PULLBACK and
                vol <= VOL_RATIO * vol_B and
                vol <= cur_row["MAV20"] and
                cur_row["EMA20"] > cur_row["EMA60"] and
                close >= cur_row["EMA60"] * 0.98 and
                cur_row["MAV5"] >= 1000 * 1000):
                
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
            # 階段 1：持有 30% 基本單
            if stage == 1:
                stop_loss = entry_p * (1 - STOP_PCT)
                if close <= stop_loss: # 觸發 10% 停損
                    ret = -STOP_PCT * TRANCHE_1 # -3.0%
                    trades.append({"ticker": ticker, "entry_date": entry_date, "exit_date": date, "stage_exit": 1, "pnl_pct": ret, "status": "Loss (Stage 1 Stop)"})
                    in_trade = False
                elif close >= B_p: # 突破前高 B -> 加碼第二筆 40%
                    stage = 2

            # 階段 2：持有 70% 放大單
            elif stage == 2:
                stop_second = B_p * (1 - STOP_ADD_PCT)
                avg_2 = (TRANCHE_1 * entry_p + TRANCHE_2 * B_p) / 0.7
                target_p = avg_2 * (1 + STOP_PCT * RRR)

                if close <= stop_second: # 跌破前高 5% 砍第二筆
                    # 假設第一筆守保本，整趟打平 (0%)
                    trades.append({"ticker": ticker, "entry_date": entry_date, "exit_date": date, "stage_exit": 2, "pnl_pct": 0.0, "status": "Breakeven (Cut Add-on)"})
                    in_trade = False
                elif close >= target_p: # 達到目標價停利
                    pnl = (target_p - avg_2) / avg_2 * 0.7
                    trades.append({"ticker": ticker, "entry_date": entry_date, "exit_date": date, "stage_exit": 2, "pnl_pct": pnl, "status": "Win (Target Hit Stage 2)"})
                    in_trade = False
                elif close >= B2_p: # 突破延伸點 +8% -> 加碼第三筆 30%
                    stage = 3

            # 階段 3：100% 部位主升段奔跑
            elif stage == 3:
                stop_third = B_p # 停損直接拉至前高 B，鎖定獲利
                avg_3 = (TRANCHE_1 * entry_p + TRANCHE_2 * B_p + TRANCHE_3 * B2_p) / 1.0
                target_p = avg_3 * (1 + STOP_PCT * RRR)

                if close <= stop_third: # 回測破前高，全數鎖利出場
                    pnl = (stop_third - avg_3) / avg_3 * 1.0
                    trades.append({"ticker": ticker, "entry_date": entry_date, "exit_date": date, "stage_exit": 3, "pnl_pct": pnl, "status": "Win (Trailing Stop B)"})
                    in_trade = False
                elif close >= target_p: # 達到滿倉波段大目標
                    pnl = (target_p - avg_3) / avg_3 * 1.0
                    trades.append({"ticker": ticker, "entry_date": entry_date, "exit_date": date, "stage_exit": 3, "pnl_pct": pnl, "status": "Win (Target Hit Stage 3)"})
                    in_trade = False

    return trades

# ==========================================
# 3. 執行批次回測並輸出績效統計
# ==========================================
print(f"正在下載歷史資料並執行回測 ({START_DATE} ~ {END_DATE})...")
all_trades = []

for t in TICKERS:
    try:
        data = yf.download(t, start=START_DATE, end=END_DATE, progress=False, auto_adjust=True)
        if isinstance(data.columns, pd.MultiIndex):
            data = data.xs(t, axis=1, level=1) if t in data.columns.levels[1] else data[t]
        trades = run_backtest_for_ticker(t, data)
        all_trades.extend(trades)
    except Exception as e:
        print(f"回測標的 {t} 失敗: {e}")

df_results = pd.DataFrame(all_trades)

if df_results.empty:
    print("在設定期間內未觸發任何完整交易訊號。")
else:
    wins = df_results[df_results["pnl_pct"] > 0]
    losses = df_results[df_results["pnl_pct"] < 0]
    breakevens = df_results[df_results["pnl_pct"] == 0]

    win_rate = len(wins) / len(df_results) * 100
    avg_win = wins["pnl_pct"].mean() * 100 if not wins.empty else 0
    avg_loss = losses["pnl_pct"].mean() * 100 if not losses.empty else 0
    total_cum_ret = df_results["pnl_pct"].sum() * 100

    print("\n" + "="*50)
    print("📊 【三段金字塔加碼波段策略 · 歷史回測摘要報告】")
    print("="*50)
    print(f"總交易次數      : {len(df_results)} 次")
    print(f"獲利交易次數    : {len(wins)} 次")
    print(f"保本平手次數    : {len(breakevens)} 次 (砍第二筆保本)")
    print(f"虧損交易次數    : {len(losses)} 次 (第一筆 -10% 出場)")
    print(f"真實勝率 (排除打平): {len(wins) / (len(wins) + len(losses)) * 100:.1f}%")
    print(f"綜合勝率 (含打平)  : {win_rate:.1f}%")
    print(f"平均單筆獲利    : +{avg_win:.2f}%")
    print(f"平均單筆虧損    : {avg_loss:.2f}%")
    print(f"風報比 (盈虧比)  : {abs(avg_win / avg_loss):.2f}:1" if avg_loss != 0 else "N/A")
    print(f"策略累積報酬率  : {total_cum_ret:+.2f}%")
    print("="*50)
    print("\n明細抽樣前 5 筆：")
    print(df_results.head())
