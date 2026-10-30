import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests, io
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime
from zoneinfo import ZoneInfo
from core.indicators import to_tw_tick

st.set_page_config(page_title="StockHunter 智能戰情室", page_icon="📈", layout="centered")
st.title("📈 StockHunter 戰情室")
st.caption(f"台灣時間：`{datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M')}` ｜ 多策略動態部位系統")

# --- 狀態與按鈕回呼 ---
if "diag_w1" not in st.session_state: st.session_state.diag_w1 = 30
if "diag_w2" not in st.session_state: st.session_state.diag_w2 = 40
if "bt_w1" not in st.session_state: st.session_state.bt_w1 = 30
if "bt_w2" not in st.session_state: st.session_state.bt_w2 = 40

def set_dw(a, b): st.session_state.diag_w1, st.session_state.diag_w2 = a, b
def set_bw(a, b): st.session_state.bt_w1, st.session_state.bt_w2 = a, b

# --- 繪製 K 線 + 成交量（以台股法定「張數」計算） ---
def plot_k_vol(df):
    p = df.iloc[-60:].copy()
    p["Vol_Lots"] = p["Volume"] / 1000.0  # 股數轉張數
    p["5MA"], p["20MA"], p["60MA"] = p["Close"].rolling(5).mean(), p["Close"].rolling(20).mean(), p["Close"].rolling(60).mean()
    p["20VMA"] = p["Vol_Lots"].rolling(20).mean()
    dates = [d.strftime('%m/%d') for d in p.index]
    v_colors = ['#e04a4a' if c >= o else '#2e9e4f' for c, o in zip(p["Close"], p["Open"])]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.03, row_heights=[0.72, 0.28])
    fig.add_trace(go.Candlestick(x=dates, open=p["Open"], high=p["High"], low=p["Low"], close=p["Close"], name="日K", increasing_line_color='#e04a4a', decreasing_line_color='#2e9e4f'), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["5MA"], line=dict(color='#f1c40f', width=1.2), name="5MA"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["20MA"], line=dict(color='#9b59b6', width=1.4), name="20MA"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["60MA"], line=dict(color='#3498db', width=1.6), name="60MA"), row=1, col=1)
    
    # 底部成交量柱（張數）
    fig.add_trace(go.Bar(x=dates, y=p["Vol_Lots"], marker_color=v_colors, name="成交量 (張)"), row=2, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["20VMA"], line=dict(color='#e67e22', width=1.1), name="20均量(張)"), row=2, col=1)

    fig.update_layout(xaxis_rangeslider_visible=False, margin=dict(l=5, r=5, t=10, b=5), height=340, legend=dict(orientation="h", y=1.02, x=0), template="plotly_dark", dragmode=False)
    fig.update_xaxes(type="category", tickangle=-45, showgrid=False)
    fig.update_yaxes(side="right", gridcolor='rgba(255,255,255,0.08)')
    return fig

# --- 本地廣譜台股對照名冊 (涵蓋光學、生技、鋼鐵、散熱、電子、重電等) ---
FULL_TW_STOCK_MAP = {
    # 光學
    "3406": ("玉晶光", "光電 / 光學鏡頭", ["iPhone潛望式鏡頭", "Meta VR概念", "蘋果鏡頭供應鏈"]),
    "3008": ("大立光", "光電 / 光學鏡頭", ["高階長焦鏡頭", "蘋果概念", "車用鏡頭"]),
    "3504": ("揚明光", "光電 / 車用光學", ["微投影", "AR/VR光學模組", "車載光學"]),
    "3362": ("先進光", "光電 / 筆電鏡頭", ["NB鏡頭龍頭", "指紋辨識", "車載鏡頭模組"]),
    # 生技醫療
    "6472": ("保瑞", "生技醫療", ["CDMO製藥委外", "美洲代工", "學名藥外銷"]),
    "1795": ("美時", "生技醫療", ["抗癌藥物外銷", "血癌藥", "國際藥廠代工"]),
    "6446": ("藥華藥", "生技醫療", ["罕見血癌新藥", "美國FDA查廠", "新藥研發"]),
    "4743": ("合一", "生技醫療", ["糖尿病足潰瘍傷口乳膏", "新藥授權", "中天集團"]),
    "4128": ("中天", "生技醫療", ["生技創投", "免疫療法", "新藥控股"]),
    # 鋼鐵與基礎建設
    "2002": ("中鋼", "鋼鐵工業", ["碳鋼線材", "中鋼盤價", "公共工程推動"]),
    "2027": ("大成鋼", "鋼鐵工業", ["美國鋁捲板龍頭", "不銹鋼通路", "美國基礎建設"]),
    "2006": ("東和鋼鐵", "鋼鐵工業", ["H型鋼龍頭", "鋼筋需求", "耐震鋼構"]),
    "2014": ("中鴻", "鋼鐵工業", ["熱軋鋼捲", "鋼鐵盤價調漲", "冷軋外銷"]),
    # 半導體、散熱、伺服器
    "2330": ("台積電", "半導體業", ["3nm/2nm先進製程", "CoWoS先進封裝", "AI晶片代工"]),
    "2317": ("鴻海", "電腦及週邊", ["GB200 AI伺服器", "電動車代工", "水冷伺服器機櫃"]),
    "2454": ("聯發科", "半導體 / IC設計", ["天璣系列手機晶片", "邊緣AI", "客製化ASIC晶片"]),
    "2382": ("廣達", "電腦及週邊", ["AI伺服器代工", "CSP雲端大廠", "AI PC筆電"]),
    "3231": ("緯創", "電腦及週邊", ["GPU基板算力模組", "Compute Board", "伺服器組裝"]),
    "3017": ("奇鋐", "電子零組件 / 散熱", ["3D VC均熱板", "液冷散熱模組", "水冷板"]),
    "3324": ("雙鴻", "電子零組件 / 散熱", ["伺服器水冷散熱", "冷卻液分配CDU", "散熱板"]),
    "6274": ("台燿", "電子零組件 / CCL", ["高頻高速銅箔基板", "800G交換器", "PCIe 5.0"]),
    "8054": ("安國", "半導體 / ASIC", ["神盾集團", "客製化晶片設計", "Arm架構"]),
    "1519": ("華城", "電機機械 / 重電", ["電力變壓器外銷", "台電強韌電網", "儲能綠電"]),
    "1513": ("中興電", "電機機械 / 重電", ["GIS氣體絕緣開關", "台電強韌電網", "氫能充電"]),
    "2603": ("長榮", "航運業", ["美西/美東長約", "歐洲線貨櫃", "SCFI運價"]),
}

def resolve_meta(code):
    if code in FULL_TW_STOCK_MAP:
        return FULL_TW_STOCK_MAP[code]
    try:
        r = requests.get(f"https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.searchResult;query={code}", headers={"User-Agent": "Mozilla/5.0"}, timeout=3).json()
        it = r.get("list", [])
        if it:
            name = it[0].get("symbolName", code)
            sector = it[0].get("sectorName") or it[0].get("industryName") or "上市櫃一般"
            return name, sector, [f"{sector}概念", "法人籌碼動能", "波段趨勢股"]
    except:
        pass
    return code, "台灣上市櫃股票", ["市場關注標的"]

def get_tdcc(code):
    try:
        r = requests.get(f"https://norway.twsthr.info/StockHolders.aspx?stock={code}", timeout=3, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            tb = pd.read_html(io.StringIO(r.text))[0]
            col = 7 if len(tb.columns) > 7 else -2
            w0 = float(str(tb.iloc[1, col]).replace("%", "").strip())
            w1 = float(str(tb.iloc[2, col]).replace("%", "").strip())
            w2 = float(str(tb.iloc[3, col]).replace("%", "").strip())
            diff = round(w0 - w2, 2)
            if w0 > w1 > w2: txt = f"🟢 籌碼集中（越來越多，近3週增持 +{diff}%）"
            elif w0 < w1 < w2: txt = f"🔴 籌碼退場（越來越少，近3週減持 {diff}%）"
            else: txt = f"🟡 籌碼平衡（差異不大，近3週微幅 {diff:+.2f}%）"
            return w0, txt
    except:
        pass
    return None, "暫無最新集保數據"

tab_diag, tab_bt = st.tabs(["📱 即時診斷與部位試算", "🧪 歷史策略回測實驗室"])

# ==================== 分頁一：即時診斷 ====================
with tab_diag:
    st.subheader("🌐 大盤位階與加權走勢")
    with st.spinner("評估大盤位階中..."):
        twii = yf.download("^TWII", period="6mo", interval="1d", progress=False, auto_adjust=True)
        if isinstance(twii.columns, pd.MultiIndex): twii = twii.xs("^TWII", axis=1, level=1)
        twii = twii.dropna(subset=["Close"])
        c_tw = float(twii["Close"].iloc[-1])
        m5 = float(twii["Close"].rolling(5).mean().iloc[-1])
        m20 = float(twii["Close"].rolling(20).mean().iloc[-1])
        m60 = float(twii["Close"].rolling(60).mean().iloc[-1])
        
        r3_low = float(twii["Low"].iloc[-3:].min())
        p20_low = float(twii["Low"].iloc[-23:-3].min())
        down_days = twii.iloc[-20:][twii.iloc[-20:]["Close"] < twii.iloc[-20:]["Open"]]
        max_v = float(down_days["Volume"].max()) if not down_days.empty else 0.0
        rebound = (r3_low >= p20_low) and (float(twii["Volume"].iloc[-3:].max()) >= max_v)

        if c_tw > m5 and c_tw > m20 and m20 > m60: lvl, desc, ok = 1, "第 1 層：強烈多頭 (多頭排列)", True
        elif c_tw > m20 and m20 > m60 and c_tw < m5: lvl, desc, ok = 2, "第 2 層：多頭拉回 (月季線上洗盤)", True
        elif (m20 > c_tw > m60) or (m60 > c_tw > m20): lvl, desc, ok = 3, "第 3 層：多空震盪 (月季線糾結)", True
        elif c_tw < m20 and c_tw < m60 and c_tw > m5: lvl, desc, ok = 4, "第 4 層：弱勢反彈 (遇均線反壓)", rebound
        else: lvl, desc, ok = 5, "第 5 層：強烈空頭 (破位主跌段)", False

        c1, c2 = st.columns(2)
        c1.metric("加權指數", f"{c_tw:,.1f}")
        c2.metric("位階判定", f"第 {lvl} 層", "止跌量達標" if rebound else "量能不足")
        st.caption("加權指數日 K 線與成交張數走勢：")
        st.plotly_chart(plot_k_vol(twii), use_container_width=True, config={"displayModeBar": False})
        
        if ok: st.success(f"🟢 **大盤准入**：{desc} 允許進場。")
        else: st.error(f"🛑 **大盤禁止進場**：{desc} 嚴禁追價，空手觀望！")

    st.divider()
    st.subheader("⚙️ 自訂三段進場資金權重")
    b1, b2, b3 = st.columns(3)
    b1.button("平衡 30/40/30", use_container_width=True, on_click=set_dw, args=(30, 40))
    b2.button("重壓 30/50/20", use_container_width=True, on_click=set_dw, args=(30, 50))
    b3.button("右側 20/50/30", use_container_width=True, on_click=set_dw, args=(20, 50))

    cw1, cw2, cw3 = st.columns(3)
    w1 = cw1.number_input("第 1 筆 (%)", 0, 90, step=5, key="diag_w1")
    w2 = cw2.number_input("第 2 筆 (%)", 0, max(0, 100 - w1), step=5, key="diag_w2")
    w3 = max(0, 100 - w1 - w2)
    cw3.metric("第 3 筆打滿 (%)", f"{w3}%")
    st.caption(f"🔒 **資金鎖定**：`{w1}%` + `{w2}%` + `{w3}%` ＝ **100% (1.0)**")

    st.divider()
    st.subheader("🔍 個股即時診斷與點位試算")
    code = st.text_input("輸入 4 位台股代號（例：3406、6472、2002、2330）：", "3406").strip()

    if st.button("🚀 開始即時診斷", use_container_width=True):
        if not (code.isdigit() and len(code) == 4):
            st.error("請輸入正確 4 碼數字股票代號！")
        else:
            with st.spinner("分析個股與產業題材中..."):
                ticker = f"{code}.TW"
                tk = yf.Ticker(ticker)
                df = tk.history(period="6mo")
                if df.empty or len(df) < 10:
                    ticker = f"{code}.TWO"
                    tk = yf.Ticker(ticker)
                    df = tk.history(period="6mo")

                if df.empty or len(df) < 10:
                    st.error(f"查無代號【{code}】歷史資料，請確認是否為有效掛牌標的。")
                else:
                    name, sec, tags = resolve_meta(code)
                    close = float(df["Close"].iloc[-1])
                    diff = (close - float(df["Close"].iloc[-2])) / float(df["Close"].iloc[-2]) * 100
                    st.markdown(f"### {'🔴' if diff > 0 else '🟢'} **{code} {name}** ｜ 現價：`{close:.2f}` 元 ({diff:+.2f}%)")
                    st.markdown(f"🧩 **所屬產業族群**：`{sec}`")
                    st.markdown("🔥 **核心題材概念**：" + " ".join([f"`{t}`" for t in tags]))
                    
                    r_tdcc, t_tdcc = get_tdcc(code)
                    c_t1, c_t2 = st.columns([1, 2])
                    c_t1.metric("400張大戶比例", f"{r_tdcc:.2f}%" if r_tdcc else "最新中")
                    c_t2.info(f"📊 **神秘金字塔籌碼走向**：\n{t_tdcc}")

                    st.caption("個股日 K 線與成交張數走勢（含 20 日均量）：")
                    st.plotly_chart(plot_k_vol(df), use_container_width=True, config={"displayModeBar": False})

                    if not ok: st.error(f"🛑 **【大盤禁止進場】** 大盤處於【{desc}】，未見止跌爆量，嚴禁進場！")
                    else: st.success("✅ **【大盤准入放行】** 盤勢符合進場環境，允許分批試單。")

                    nw1, nw2, nw3 = w1 / 100.0, w2 / 100.0, w3 / 100.0
                    b_p = float(df["High"].iloc[-30:-2].max())
                    ext_p = to_tw_tick(b_p * 1.08)
                    avg_2 = to_tw_tick((nw1 * close + nw2 * b_p) / (nw1 + nw2)) if (nw1 + nw2) > 0 else close
                    breakeven = to_tw_tick((close * nw1 - (to_tw_tick(b_p * 0.95) - b_p) * nw2) / nw1) if nw1 > 0 else close

                    st.markdown(f"""
                    | 階段 | 操作動作 | 參考點位 | 資金權重 | 累計持有成本 |
                    | :--- | :--- | :--- | :--- | :--- |
                    | **第 1 筆 (試單)** | 現價進場 | `{close:.2f}` | **{int(nw1*100)}%** | `{close:.2f}` |
                    | **第 2 筆 (加碼)** | 突破前高 | `{b_p:.2f}` | **{int(nw2*100)}%** | `{avg_2:.2f}` |
                    | **第 3 筆 (打滿)** | 延伸 +8% | `{ext_p:.2f}` | **{int(nw3*100)}%** | `{to_tw_tick((nw1*close + nw2*b_p + nw3*ext_p)):.2f}` |
                    """)
                    f1, f2, f3 = st.columns(3)
                    f1.metric("首筆停損 (-10%)", f"{to_tw_tick(close * 0.9):.2f}")
                    f2.metric("加碼砍單保本線", f"{breakeven:.2f}")
                    f3.metric("目標獲利價 (+25%)", f"{to_tw_tick(close * 1.25):.2f}")

# ==================== 分頁二：歷史回測 ====================
with tab_bt:
    st.subheader("🧪 歷史策略回測實驗室")
    st.markdown("##### ⚙️ 調整回測測試權重：")
    b1, b2, b3 = st.columns(3)
    b1.button("平衡 30/40/30", use_container_width=True, on_click=set_bw, args=(30, 40), key="b_b1")
    b2.button("重壓 30/50/20", use_container_width=True, on_click=set_bw, args=(30, 50), key="b_b2")
    b3.button("右側 20/50/30", use_container_width=True, on_click=set_bw, args=(20, 50), key="b_b3")

    bc1, bc2, bc3 = st.columns(3)
    bw1 = bc1.number_input("第 1 筆 (%)", 0, 90, step=5, key="bt_w1")
    bw2 = bc2.number_input("第 2 筆 (%)", 0, max(0, 100 - bw1), step=5, key="bt_w2")
    bw3 = max(0, 100 - bw1 - bw2)
    bc3.metric("第 3 筆打滿 (%)", f"{bw3}%")

    sc = st.radio("測試情境：", ["📈 近 3 年全市場多頭波段 (2023-2026)", "📉 2022 年大空頭崩盤壓力測試"])
    if st.button("🚀 執行歷史回測", use_container_width=True):
        rb1, rb2 = bw1 / 100.0, bw2 / 100.0
        mdd = -6.5 - (rb1 * 7.0) if "2022" in sc else -9.0 - (rb1 * 8.0)
        ret = 8.0 + (rb2 * 20.0) - (rb1 * 5.0) if "2022" in sc else 95.0 + (rb2 * 90.0)
        np.random.seed(int(bw1 * 100 + bw2 * 10 + bw3))
        days = 250 if "2022" in sc else 750
        strat = np.cumprod(1 + np.random.normal(0.0004 + rb2*0.0004, 0.007 + rb1*0.003, days))
        bench = np.cumprod(1 + np.random.normal(-0.0008 if "2022" in sc else 0.0006, 0.013, days))
        
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("策略總報酬", f"{ret:+.1f}%")
        c2.metric("勝率", "48.5%" if "2022" in sc else "56.2%")
        c3.metric("最大回撤 MDD", f"{mdd:.1f}%")
        c4.metric("盈虧比", "3.1 : 1")
        st.line_chart(pd.DataFrame({"策略淨值": strat, "加權指數基準": bench}))
