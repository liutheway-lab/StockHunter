# app.py
from datetime import datetime
from zoneinfo import ZoneInfo
from core.market_regime import get_market_regime
from core.strategies import scan_single_stock
import pandas as pd
import streamlit as st
import yfinance as yf

# 網頁排版優化 (適配手機垂直螢幕)
st.set_page_config(
    page_title="StockHunter 戰情室", page_icon="📈", layout="centered"
)

st.title("📈 StockHunter 智能操盤 App")
now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
st.caption(f"台灣時間：{now} ｜ 模式：多策略風控引擎")

# ==========================================
# 1. 大盤五層濾網儀表板
# ==========================================
st.subheader("🌐 大盤位階與策略通行證")
with st.spinner("正在分析加權指數即時盤態..."):
  market = get_market_regime()

col1, col2 = st.columns(2)
col1.metric("大盤多空分層", f"第 {market.level} 層", market.name)
col2.metric("空頭止跌訊號", "已確認 ✅" if market.is_rebounded else "未觸發 ❌")

if market.allowed_strategies:
  st.success(f"🚦 **准入狀態**：{market.note}")
  st.write("✅ **當前允許運作策略**：", ", ".join(market.allowed_strategies))
else:
  st.error(f"🛑 **禁止進場**：{market.note}")

st.divider()

# ==========================================
# 2. 自訂金字塔部位比例 (可動態調整)
# ==========================================
st.subheader("⚙️ 自訂三段進場資金權重")
st.caption("拖拉滑桿自由設定第 1 筆試單、第 2 筆突破加碼、第 3 筆延伸比例：")

c1, c2, c3 = st.columns(3)
w1 = c1.slider("第 1 筆 (%)", 10, 50, 30, step=5)
w2 = c2.slider("第 2 筆 (%)", 20, 60, 40, step=5)
w3 = c3.slider("第 3 筆 (%)", 10, 50, 30, step=5)

total_w = w1 + w2 + w3
if total_w != 100:
  st.warning(f"⚠️ 目前比例總和為 {total_w}%，系統將自動進行歸一化計算。")

st.divider()

# ==========================================
# 3. 即時個股診斷與點位試算
# ==========================================
st.subheader("🔍 個股即時診斷與點位換算")
ticker_input = st.text_input(
    "輸入股票代號 (例如 2330, 2454, 2382)：", "2330"
).strip()

if st.button("🚀 開始即時診斷", use_container_width=True):
  t = ticker_input.upper()
  if not (t.endswith(".TW") or t.endswith(".TWO")):
    t_full = t + ".TW"
  else:
    t_full = t

  with st.spinner(f"正在分析 {t_full} 技術籌碼結構..."):
    try:
      df = yf.download(
          t_full,
          period="8mo",
          interval="1d",
          progress=False,
          auto_adjust=True,
      )
      if isinstance(df.columns, pd.MultiIndex):
        df = df.xs(t_full, axis=1, level=1)
      twii = yf.download(
          "^TWII",
          period="1mo",
          interval="1d",
          progress=False,
          auto_adjust=True,
      )
      if isinstance(twii.columns, pd.MultiIndex):
        twii = twii.xs("^TWII", axis=1, level=1)

      plans = scan_single_stock(
          t_full, df, twii, weights=[w1 / 100, w2 / 100, w3 / 100]
      )

      curr_p = float(df["Close"].iloc[-1])
      st.info(f"📌 **{t} 現價**：`{curr_p:.2f}` 元")

      if not plans:
        st.warning("⚠️ 目前未達成四大策略中任何一項進場標準，請耐心觀望。")
      else:
        for p in plans:
          st.success(f"🎯 **符合策略**：【{p.strategy_name}】")
          st.write(f"💡 **進場理由**：{p.reason}")

          # 點位展開卡片
          st.markdown(f"""
                    | 階段 | 建議操作 | 目標點位 | 權重配比 |
                    | :--- | :--- | :--- | :--- |
                    | **Stage 1** | 首筆進場 | `{p.entry_p}` | **{w1}%** |
                    | **Stage 2** | 突破加碼 | `{p.add_p}` | **{w2}%** |
                    | **Stage 3** | 延伸打滿 | `{p.ext_p}` | **{w3}%** |
                    """)

          col_a, col_b, col_c = st.columns(3)
          col_a.metric("嚴格停損價", f"{p.stop_loss}")
          col_b.metric("加碼砍單保本線", f"{p.breakeven}")
          col_c.metric("波段預期目標", f"{p.target_p}")
          st.divider()

    except Exception as e:
      st.error(f"資料取得異常：{e}")
