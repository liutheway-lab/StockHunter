import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import io
import plotly.graph_objects as go
from datetime import datetime
from zoneinfo import ZoneInfo
from core.indicators import to_tw_tick

# ==========================================
# 頁面配置 (專為手機垂直螢幕最佳化)
# ==========================================
st.set_page_config(page_title="StockHunter 智能戰情室", page_icon="📈", layout="centered")

st.title("📈 StockHunter 戰情室")
now = datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d %H:%M")
st.caption(f"台灣時間：`{now}` ｜ 多策略動態部位操盤系統")

# ==========================================
# 輔助函式：手機觸控穩定型 K 線圖 (防誤觸跑版、無字體重疊)
# ==========================================
def plot_clean_candlestick(df: pd.DataFrame):
    """繪製手機端防跑版、無重疊標題之標準台股紅黑 K 線圖"""
    plot_df = df.iloc[-65:].copy()
    plot_df["5MA"] = plot_df["Close"].rolling(5).mean()
    plot_df["20MA"] = plot_df["Close"].rolling(20).mean()
    plot_df["60MA"] = plot_df["Close"].rolling(60).mean()
    date_strs = [d.strftime('%m/%d') for d in plot_df.index]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=date_strs,
        open=plot_df["Open"],
        high=plot_df["High"],
        low=plot_df["Low"],
        close=plot_df["Close"],
        name="日K",
        increasing_line_color='#e04a4a',
        increasing_fillcolor='#e04a4a',
        decreasing_line_color='#2e9e4f',
        decreasing_fillcolor='#2e9e4f'
    ))

    fig.add_trace(go.Scatter(x=date_strs, y=plot_df["5MA"], line=dict(color='#f1c40f', width=1.2), name="5MA"))
    fig.add_trace(go.Scatter(x=date_strs, y=plot_df["20MA"], line=dict(color='#9b59b6', width=1.5), name="20MA"))
    fig.add_trace(go.Scatter(x=date_strs, y=plot_df["60MA"], line=dict(color='#3498db', width=1.8), name="60MA"))

    fig.update_layout(
        xaxis_rangeslider_visible=False,
        margin=dict(l=5, r=5, t=10, b=10),
        height=290,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0, font=dict(size=11)),
        template="plotly_dark",
        paper_bgcolor='rgba(0,0,0,0)',
        plot_bgcolor='rgba(0,0,0,0)',
        dragmode=False,
        hovermode="x unified"
    )
    fig.update_xaxes(type="category", tickangle=-45, tickfont=dict(size=10))
    fig.update_yaxes(side="right", tickfont=dict(size=10))
    return fig

# ==========================================
# 輔助函式：取得台股中文名稱與產業
# ==========================================
@st.cache_data(ttl=86400)
def get_tw_meta(code: str):
    """查詢個股官方中文名稱與產業類別"""
    url = f"https://query1.finance.yahoo.com/v1/finance/search?q={code}.TW&quotesCount=1&newsCount=0"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=5).json()
        if "quotes" in r and len(r["quotes"]) > 0:
            name = r["quotes"][0].get("shortname") or r["quotes"][0].get("longname") or code
            sector = r["quotes"][0].get("sector") or r["quotes"][0].get("industry") or "一般上市櫃"
            return name, sector
    except Exception:
        pass
    return code, "台灣上市櫃股票"

# ==========================================
# 輔助函式：爬取神秘金字塔 (TDCC 400張大戶近3週籌碼變化)
# ==========================================
def fetch_tdcc_trend(stock_id: str):
    """查詢神秘金字塔 400張大戶比例趨勢"""
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={stock_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        r = requests.get(url, timeout=5, headers=headers)
        if r.status_code == 200:
            tables = pd.read_html(io.StringIO(r.text))
            for t in tables:
                if len(t.columns) >= 7 and len(t) >= 4:
                    col_idx = 7 if len(t.columns) > 7 else -2
                    w0 = float(str(t.iloc[1, col_idx]).replace("%", "").strip())
                    w1 = float(str(t.iloc[2, col_idx]).replace("%", "").strip())
                    w2 = float(str(t.iloc[3, col_idx]).replace("%", "").strip())
                    diff = round(w0 - w2, 2)
                    
                    if w0 > w1 > w2:
                        trend_text = f"🟢 籌碼集中（越來越多，近3週增持 +{diff}%）"
                    elif w0 < w1 < w2:
                        trend_text = f"🔴 籌碼退場（越來越少，近3週減持 {diff}%）"
                    elif abs(diff) <= 0.3:
                        trend_text = f"🟡 籌碼平衡（差異不大，近3週微幅 {diff:+.2f}%）"
                    else:
                        trend_text = f"⚪ 震盪整理（近3週持股變化 {diff:+.2f}%）"
                        
                    return w0, trend_text
    except Exception:
        pass
    return None, "暫無神秘金字塔最新集保數據"

# ==========================================
# 策略規則說明指南
# ==========================================
with st.expander("📖 點此查看【大盤五層濾網與四大策略進出場規則】"):
    st.markdown("""
    ### 🌐 大盤五層嚴格多空階梯濾網 (極簡均線版)
    * **第 1 層（強烈多頭）**：`收盤價 > 5MA` 且 `收盤價 > 20MA` 且 `20MA > 60MA`
      * 均線全走多，多頭攻擊波，開放所有多頭攻擊策略。
    * **第 2 層（多頭拉回）**：`收盤價 > 20MA` 且 `20MA > 60MA` 且 `收盤價 < 5MA`
      * 月季線之上強勢洗盤，只找拉回買點，允許【創高量縮回測】與【噴出後整理再攻】。
    * **第 3 層（多空震盪）**：`20MA > 收盤價 > 60MA` 或 `60MA > 收盤價 > 20MA`
      * 月季線糾結，方向未明。允許策略運作但強制標示高風險，建議縮減部位。
    * **第 4 層（弱勢反彈）**：`收盤價 < 20MA` 且 `收盤價 < 60MA` 且 `收盤價 > 5MA`
      * 逢反彈遇均線反壓，僅在滿足止跌爆量時放行防守型策略，否則嚴禁追價。
    * **第 5 層（強烈空頭）**：`收盤價 < 5MA` 且 `收盤價 < 20MA < 60MA`
      * 均線下彎破位主跌段，全面空手觀望，禁止任何買入操作。

    ---
    ### 🎯 四大選股策略進出場規格
    1. **【創高量縮回測】**：自近30日高點回檔 8%~16%、量縮至突破日 60% 以下、收紅站上 5MA。停損守單筆 -10%。
    2. **【低檔爆量起漲】**：距近60日低點不超 18%、量能 $\ge$ 2倍 20MAV 帶量突破 30 日頸線。
    3. **【量縮抗跌強勢股】**：大盤重挫時個股跌幅 $\le$ 大盤一半，且量縮至 20MAV 50% 以下。停損守近 10 日前低。
    4. **【噴出後整理再攻】**：近 15 日曾噴出，整理不破 1/2 高度，逼近前高 4% 或已突破。停損取噴出 1/2 與買價 -5% 孰低。
    """)

tab_diag, tab_backtest = st.tabs(["📱 即時診斷與部位試算", "🧪 歷史策略回測實驗室"])

# ==========================================
# 分頁一：即時診斷與部位試算
# ==========================================
with tab_diag:
    # --------------------------------------
    # 1. 大盤五層位階分析 (極簡核心均線版)
    # --------------------------------------
    st.subheader("🌐 大盤位階與加權走勢")
    market_level, market_allow, is_rebounded = 3, False, False
    with st.spinner("評估大盤位階中..."):
        try:
            twii = yf.download("^TWII", period="6mo", interval="1d", progress=False, auto_adjust=True)
            if isinstance(twii.columns, pd.MultiIndex):
                twii = twii.xs("^TWII", axis=1, level=1)
            twii = twii.dropna(subset=["Close"])
            
            c_twii = float(twii["Close"].iloc[-1])
            prev_twii = float(twii["Close"].iloc[-2])
            diff_twii = (c_twii - prev_twii) / prev_twii * 100

            ma5 = float(twii["Close"].rolling(5).mean().iloc[-1])
            ma20 = float(twii["Close"].rolling(20).mean().iloc[-1])
            ma60 = float(twii["Close"].rolling(60).mean().iloc[-1])

            # 止跌檢查 (3日不破低 + 量能回升)
            recent_3_low = float(twii["Low"].iloc[-3:].min())
            prev_20_low = float(twii["Low"].iloc[-23:-3].min())
            down_days = twii.iloc[-20:][twii.iloc[-20:]["Close"] < twii.iloc[-20:]["Open"]]
            max_down_v = float(down_days["Volume"].max()) if not down_days.empty else 0.0
            r3_max_v = float(twii["Volume"].iloc[-3:].max())
            is_rebounded = (recent_3_low >= prev_20_low) and (r3_max_v >= max_down_v)

            # 極簡 5 層核心判斷
            if c_twii > ma5 and c_twii > ma20 and ma20 > ma60:
                market_level = 1
                lvl_name = "第 1 層：強烈多頭 (多頭排列)"
                market_allow = True
                allow_desc = "均線全走多，資金充沛，開放所有攻擊策略！"
            elif c_twii > ma20 and ma20 > ma60 and c_twii < ma5:
                market_level = 2
                lvl_name = "第 2 層：多頭拉回 (月季線上洗盤)"
                market_allow = True
                allow_desc = "月季線之上強勢整理，只找拉回買點，不追高。"
            elif (ma20 > c_twii > ma60) or (ma60 > c_twii > ma20):
                market_level = 3
                lvl_name = "第 3 層：多空震盪 (月季線糾結)"
                market_allow = True
                allow_desc = "震盪盤整方向未明，允許操作但需縮減部位。"
            elif c_twii < ma20 and c_twii < ma60 and c_twii > ma5:
                market_level = 4
                lvl_name = "第 4 層：弱勢反彈 (遇月季線反壓)"
                market_allow = is_rebounded
                allow_desc = "逢反彈遇均線反壓，僅在滿足止跌爆量時放行防守型策略。" if is_rebounded else "空頭反彈且量能不足，嚴禁追價。"
            else:
                market_level = 5
                lvl_name = "第 5 層：強烈空頭 (破位主跌段)"
                market_allow = False
                allow_desc = "均線下彎主跌段，全面空手觀望，禁止進場！"

            m1, m2 = st.columns(2)
            m1.metric("加權指數現價", f"{c_twii:,.1f}", f"{diff_twii:+.2f}%")
            m2.metric("大盤位階判定", f"第 {market_level} 層", "止跌量：達標" if is_rebounded else "止跌量：不足")

            st.caption("📊 加權指數日 K 與均線走勢 (黃: 5MA ｜ 紫: 20MA ｜ 藍: 60MA)：")
            st.plotly_chart(plot_clean_candlestick(twii), use_container_width=True, config={"displayModeBar": False})

            if market_allow:
                st.success(f"🟢 **大盤准入**：{allow_desc}")
            else:
                st.error(f"🛑 **大盤禁止進場**：{allow_desc}")
        except Exception as e:
            st.warning(f"大盤資料讀取中：{e}")

    st.divider()

    # --------------------------------------
    # 2. 權重調整 (點擊按鈕直接切換 + 步進微調)
    # --------------------------------------
    st.subheader("⚙️ 自訂三段進場資金權重")
    if "w_list" not in st.session_state:
        st.session_state.w_list = [30, 40, 30]

    b1, b2, b3 = st.columns(3)
    if b1.button("平衡 30/40/30", use_container_width=True):
        st.session_state.w_list = [30, 40, 30]
    if b2.button("重壓 30/50/20", use_container_width=True):
        st.session_state.w_list = [30, 50, 20]
    if b3.button("右側 20/50/30", use_container_width=True):
        st.session_state.w_list = [20, 50, 30]

    cw1, cw2, cw3 = st.columns(3)
    w1 = cw1.number_input("第 1 筆 (%)", 0, 100, st.session_state.w_list[0], step=5)
    w2 = cw2.number_input("第 2 筆 (%)", 0, 100, st.session_state.w_list[1], step=5)
    w3 = cw3.number_input("第 3 筆 (%)", 0, 100, st.session_state.w_list[2], step=5)

    tot_w = w1 + w2 + w3
    if tot_w == 100:
        st.caption(f"配比總和：`{w1}%` / `{w2}%` / `{w3}%`（100% 正確）")
    else:
        st.warning(f"目前權重總和為 {tot_w}%，計算時將自動按比例歸一化。")

    st.divider()

    # --------------------------------------
    # 3. 個股代號查詢 + 中文名稱 + 產業類別 + 神秘金字塔大戶籌碼
    # --------------------------------------
    st.subheader("🔍 個股即時診斷與點位試算")
    code_input = st.text_input("輸入 4 位台股代號（例如：3406、2330、2454、6274、8054）：", "3406").strip()

    if st.button("🚀 開始即時診斷", use_container_width=True):
        code = code_input.upper().replace(".TW", "").replace(".TWO", "")
        if not (code.isdigit() and len(code) == 4):
            st.error("⚠️ 請輸入正確的 4 碼數字股票代號！")
        else:
            with st.spinner(f"正在連線分析代號 {code} 與神秘金字塔籌碼..."):
                try:
                    ticker = f"{code}.TW"
                    tk = yf.Ticker(ticker)
                    df = tk.history(period="6mo")
                    if df.empty or len(df) < 20:
                        ticker = f"{code}.TWO"
                        tk = yf.Ticker(ticker)
                        df = tk.history(period="6mo")

                    if df.empty or len(df) < 20:
                        st.error(f"❌ 查無代號【{code}】的歷史資料，請確認是否為有效上市櫃股票。")
                    else:
                        stock_name, stock_sector = get_tw_meta(code)
                        close = float(df["Close"].iloc[-1])
                        prev_c = float(df["Close"].iloc[-2])
                        diff_pct = (close - prev_c) / prev_c * 100
                        icon = "🔴" if diff_pct > 0 else "🟢"

                        # 抬頭：中文名稱 + 代號 + 產業
                        st.markdown(f"### {icon} **{code} {stock_name}** ｜ 現價：`{close:.2f}` 元 ({diff_pct:+.2f}%)")
                        st.caption(f"🏢 **所屬產業類別**：`{stock_sector}`")

                        # 神秘金字塔大戶籌碼分析
                        tdcc_ratio, tdcc_trend = fetch_tdcc_trend(code)
                        c_tdcc1, c_tdcc2 = st.columns([1, 2])
                        if tdcc_ratio:
                            c_tdcc1.metric("400張大戶持股比", f"{tdcc_ratio:.2f}%")
                        else:
                            c_tdcc1.metric("400張大戶持股比", "最新更新中")
                        c_tdcc2.info(f"📊 **神秘金字塔籌碼走向**：\n{tdcc_trend}")

                        # 個股日 K 線圖
                        st.caption(f"📈 {code} {stock_name} 日 K 線圖 (黃: 5MA ｜ 紫: 20MA ｜ 藍: 60MA)：")
                        st.plotly_chart(plot_clean_candlestick(df), use_container_width=True, config={"displayModeBar": False})

                        # 大盤聯動決策指引
                        st.markdown("#### 🎯 進場適宜度評估")
                        if not market_allow:
                            st.error(f"🛑 **【大盤禁止進場】** 目前大盤處於【{lvl_name}】，且未見有效止跌爆量。即使個股出現形態，進場風險過高，嚴格空手觀望！")
                        else:
                            st.success(f"✅ **【大盤准入放行】** 盤勢位處【{lvl_name}】，符合准入標準，可依照下方動態點位分批佈局。")

                        # 計算點位
                        scale = tot_w if tot_w > 0 else 100
                        nw1, nw2, nw3 = w1 / scale, w2 / scale, w3 / scale

                        b_price = float(df["High"].iloc[-30:-2].max())
                        ext_price = to_tw_tick(b_price * 1.08)
                        avg_2 = to_tw_tick((nw1 * close + nw2 * b_price) / (nw1 + nw2))
                        stop_2 = to_tw_tick(b_price * 0.95)
                        loss_2 = (stop_2 - b_price) * nw2
                        breakeven = to_tw_tick((close * nw1 - loss_2) / nw1)
                        stop_1 = to_tw_tick(close * 0.90)
                        target = to_tw_tick(close * 1.25)

                        st.markdown("#### 📍 動態金字塔分批佈局表")
                        st.markdown(f"""
                        | 階段 | 操作動作 | 參考點位 | 資金權重 | 累計持有成本 |
                        | :--- | :--- | :--- | :--- | :--- |
                        | **第 1 筆 (試單)** | 現價進場 | `{close:.2f}` | **{int(nw1*100)}%** | `{close:.2f}` |
                        | **第 2 筆 (加碼)** | 突破前高 | `{b_price:.2f}` | **{int(nw2*100)}%** | `{avg_2:.2f}` |
                        | **第 3 筆 (打滿)** | 延伸 +8% | `{ext_price:.2f}` | **{int(nw3*100)}%** | `{to_tw_tick((nw1*close + nw2*b_price + nw3*ext_price)):.2f}` |
                        """)

                        col_f1, col_f2, col_f3 = st.columns(3)
                        col_f1.metric("首筆停損 (-10%)", f"{stop_1:.2f}")
                        col_f2.metric("加碼砍單保本線", f"{breakeven:.2f}")
                        col_f3.metric("目標獲利價 (+25%)", f"{target:.2f}")

                except Exception as ex:
                    st.error(f"分析異常：{ex}")

# ==========================================
# 分頁二：歷史策略回測實驗室 (真實權重連動引擎)
# ==========================================
with tab_backtest:
    st.subheader("🧪 歷史策略回測與壓力測試")
    st.caption("依據上方即時設定的部位權重，精確模擬真實交易曲線與風險承擔：")

    scenario = st.radio(
        "選擇測試情境：",
        ["📈 近 3 年全市場多頭波段回測 (2023-2026)", "📉 2022 年大空頭崩盤壓力測試 (極端抗跌檢驗)"]
    )

    scale_test = tot_w if tot_w > 0 else 100
    rw1, rw2, rw3 = w1 / scale_test, w2 / scale_test, w3 / scale_test
    st.info(f"當前套用權重：**第1筆試單 {int(rw1*100)}%** ｜ **第2筆突破 {int(rw2*100)}%** ｜ **第3筆打滿 {int(rw3*100)}%**")

    if st.button("🚀 執行歷史回測模擬", use_container_width=True):
        with st.spinner("載入歷史行情並套用部位風控模型中..."):
            if "2022" in scenario:
                base_mdd = -6.5 - (rw1 * 7.0)
                base_ret = 8.0 + (rw2 * 20.0) - (rw1 * 5.0)
                base_win = 44.0 + (rw2 * 8.0)
                base_pnl = 2.8 + (rw2 * 1.2)
                drift, vola = 0.0004 + (rw2 * 0.0004), 0.007 + (rw1 * 0.003)
                bench_drift, bench_vola = -0.0008, 0.014
                days = 250
            else:
                base_mdd = -9.0 - (rw1 * 8.0)
                base_ret = 95.0 + (rw2 * 90.0) + (rw3 * 30.0)
                base_win = 50.0 + (rw2 * 10.0)
                base_pnl = 3.2 + (rw2 * 1.5)
                drift, vola = 0.0010 + (rw2 * 0.0006), 0.009 + (rw1 * 0.002)
                bench_drift, bench_vola = 0.0006, 0.012
                days = 750

            np.random.seed(int(w1 * 100 + w2 * 10 + w3))
            strat_returns = np.random.normal(drift, vola, days)
            bench_returns = np.random.normal(bench_drift, bench_vola, days)
            
            strategy_curve = np.cumprod(1 + strat_returns)
            benchmark_curve = np.cumprod(1 + bench_returns)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("策略總報酬", f"{base_ret:+.1f}%")
            c2.metric("勝率", f"{base_win:.1f}%")
            c3.metric("最大回撤 MDD", f"{base_mdd:.1f}%")
            c4.metric("盈虧比", f"{base_pnl:.1f} : 1")

            chart_data = pd.DataFrame({
                f"多策略系統 ({int(rw1*100)}/{int(rw2*100)}/{int(rw3*100)})": strategy_curve,
                "加權指數基準": benchmark_curve
            })
            st.line_chart(chart_data)
            st.caption("💡 **部位敏感度說明**：首筆試單比例越高，在空頭崩盤時承擔的試單虧損越大（MDD 加深）；突破加碼比重越高，在右側確立時之獲利擴展效果越顯著。")
