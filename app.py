import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
import yfinance as yf

import charts
import config as cfg
import lookup
import strategies as S

st.set_page_config(page_title="StockHunter 智能戰情室", page_icon="📈", layout="centered")
st.title("📈 StockHunter 戰情室")
st.caption(f"台灣時間：`{datetime.now(ZoneInfo('Asia/Taipei')).strftime('%Y-%m-%d %H:%M')}` ｜ 大盤五層 × 四套策略")


# ==========================================
# 規則說明（數字直接讀 config.py，不會和程式不一致）
# ==========================================
def rules_markdown():
    return f"""
### 🌐 大盤五層（MA5／MA10／MA20／MA60）
1. **強多頭**：收盤 > MA5 > MA10 > MA20 > MA60
2. **多頭**：MA20 > MA60，且收盤 > MA20
3. **盤整／轉折**：其餘情況（多頭回檔或空頭反彈）。多頭策略可用，但會標示風險
4. **空頭**：MA20 < MA60，且收盤 < MA20
5. **強空頭**：收盤 < MA5 < MA10 < MA20 < MA60

**空頭「止跌確認」**：最近 {cfg.STAB_DAYS} 天的最低點都沒跌破前低（前 {cfg.STAB_LOOKBACK} 日最低點），
且這 {cfg.STAB_DAYS} 天任一天的量 ≥ 近 {cfg.STAB_LOOKBACK} 日量最大的下跌日。通過後才啟用「盤整後噴出」「量縮抗跌」。

---
### 🎯 四套策略
1. **創高拉回**（第 1～3 層）：前高創新高後回檔、量縮（≤ 高點日 {cfg.PB_VOL_RATIO * 100:.0f}%），趨勢仍向上。
   停損進場價下 {cfg.PB_STOP_PCT * 100:.0f}%，目標 = 平均成本 +{cfg.PB_STOP_PCT * cfg.PB_RRR * 100:.0f}%
2. **盤整後噴出**（空頭止跌後）：帶量（≥ {cfg.BO_VOL_MULT:.0f} 倍）突破 {cfg.BO_PLATFORM} 日頸線，離 60 日低點 ≤ {cfg.BO_MAX_RISE * 100:.0f}%。
   停損頸線下 2% 或買價下 {cfg.BO_STOP_PCT * 100:.0f}% 取低，目標 +{cfg.BO_TARGET_PCT * 100:.0f}%
3. **量縮抗跌**（空頭止跌後）：3 日均量 ≤ 20 日均量 {cfg.DRY_VOL_RATIO * 100:.0f}%、不破個股前低、跌幅 ≤ 大盤 {cfg.DRY_RS_RATIO * 100:.0f}%。
   停損跌破前低，目標風報比 {cfg.DRY_RRR}
4. **噴出後整理再攻**（第 1～3 層）：近 {cfg.FLAG_WINDOW} 日有噴出（3 日 ≥ {cfg.FLAG_BURST_3D * 100:.0f}% 或單日 ≥ {cfg.FLAG_BURST_1D * 100:.0f}%，且爆量），
   整理 ≥ {cfg.FLAG_MIN_CONSOL} 天、回檔 ≤ 噴出一半、守月線、量縮，距前波高點 {cfg.FLAG_NEAR * 100:.0f}% 內（含已突破）。
   停損取「噴出一半」與「買價下 {cfg.FLAG_STOP_MIN_PCT * 100:.0f}%」較低者，目標等幅測量

**分批**：第 1 筆訊號價、第 2 筆突破關鍵價、第 3 筆關鍵價 +{(cfg.EXT_RATIO - 1) * 100:.0f}%；第 2 筆防守為關鍵價下 {cfg.STOP_ADD_PCT * 100:.0f}%。
"""


with st.expander("📖 點此查看大盤五層與四套策略規則"):
    st.markdown(rules_markdown())

# --- 資金比例的狀態與按鈕 ---
if "diag_w1" not in st.session_state:
    st.session_state.diag_w1 = int(round(cfg.TRANCHES[0] * 100))
if "diag_w2" not in st.session_state:
    st.session_state.diag_w2 = int(round(cfg.TRANCHES[1] * 100))


def set_dw(a, b):
    st.session_state.diag_w1, st.session_state.diag_w2 = a, b


# ==========================================
# 資料（全部從網路抓）
# ==========================================
def _flat(df, ticker):
    if isinstance(df.columns, pd.MultiIndex):
        for lvl in range(df.columns.nlevels):
            if ticker in df.columns.get_level_values(lvl):
                df = df.xs(ticker, axis=1, level=lvl)
                break
    return df.dropna(subset=["Close"])


@st.cache_data(ttl=600, show_spinner=False)
def load_index():
    df = yf.download("^TWII", period="1y", interval="1d", progress=False, auto_adjust=True)
    return _flat(df, "^TWII")


@st.cache_data(ttl=600, show_spinner=False)
def load_stock(code, suffix):
    return yf.Ticker(code + suffix).history(period="1y")


@st.cache_data(ttl=60 * 60 * 24, show_spinner="載入證交所官方股票清單...")
def get_directory():
    return lookup.load_directory()


@st.cache_data(ttl=60 * 60 * 6, show_spinner=False)
def get_themes():
    """題材表：自己維護的 Google 試算表 CSV（選填，設定 THEMES_CSV_URL）。"""
    import os
    url = os.getenv("THEMES_CSV_URL", "")
    if not url:
        try:
            url = st.secrets.get("THEMES_CSV_URL", "")
        except Exception:
            url = ""
    try:
        return lookup.load_themes(url)
    except Exception:
        return {}


def get_chip_analysis(code, df):
    """400 張以上大戶持股；抓不到時改用量價推估（會明確標示不是大戶資料）"""
    try:
        url = f"https://norway.twsthr.info/StockHolders.aspx?stock={code}"
        r = requests.get(url, timeout=3.5, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200 and "StockHolders" in r.text:
            tb = pd.read_html(io.StringIO(r.text))[0]
            col = 7 if len(tb.columns) > 7 else -2
            w0, w1, w2 = (float(str(tb.iloc[k, col]).replace("%", "").strip()) for k in (1, 2, 3))
            diff = round(w0 - w2, 2)
            if w0 > w1 > w2:
                txt = f"🟢 400張大戶近3週連續增持 +{diff}%"
            elif w0 < w1 < w2:
                txt = f"🔴 400張大戶近3週連續減碼 {diff}%"
            else:
                txt = f"🟡 400張大戶近3週微幅震盪 {diff:+.2f}%"
            return f"{w0:.2f}%", txt
    except Exception:
        pass
    v_recent, v_prev = df["Volume"].iloc[-5:].mean(), df["Volume"].iloc[-15:-5].mean()
    p_chg = (df["Close"].iloc[-1] - df["Close"].iloc[-15]) / df["Close"].iloc[-15] * 100
    if p_chg > 0 and v_recent > v_prev:
        return "量價推估", "🟢 （抓不到大戶資料，以下是量價推估）近3週量增價漲"
    if p_chg < 0 and v_recent > v_prev:
        return "量價推估", "🔴 （抓不到大戶資料，以下是量價推估）近3週量增下跌"
    return "量價推估", "🟡 （抓不到大戶資料，以下是量價推估）近3週量能平穩"


def plan_markdown(p):
    r = p["ratios"]
    return (
        "| 階段 | 動作 | 價位 | 資金比例 | 累計均價 |\n| :--- | :--- | :--- | :--- | :--- |\n"
        f"| **第 1 筆** | 進場 | `{p['e']:.2f}` | {r[0] * 100:.0f}% | `{p['e']:.2f}` |\n"
        f"| **第 2 筆** | {p['key_label']} | `{p['b']:.2f}` | {r[1] * 100:.0f}% | `{p['avg2']:.2f}` |\n"
        f"| **第 3 筆** | 關鍵價 +{(cfg.EXT_RATIO - 1) * 100:.0f}% | `{p['b2']:.2f}` | {r[2] * 100:.0f}% | `{p['avg3']:.2f}` |"
    )


def show_plan(p):
    st.markdown(plan_markdown(p))
    c1, c2, c3 = st.columns(3)
    c1.metric("第 1 筆停損", f"{p['stop1']:.2f}")
    c2.metric("第 2 筆防守", f"{p['stop2']:.2f}")
    c3.metric("目標價", f"{p['targets'][0][1]:.2f}")
    if p.get("stop1_note"):
        st.caption(f"停損依據：{p['stop1_note']}")
    if p.get("breakeven"):
        st.caption(f"第 2 筆被砍掉後，第 1 筆要漲到 `{p['breakeven']:.2f}` 整趟才打平")
    if len(p["targets"]) > 1:
        st.caption("目標價：" + "；".join(f"{lbl} `{px:.2f}`" for lbl, px in p["targets"]))
    for n in p.get("notes", []):
        st.caption(n)


tab_diag, tab_bt = st.tabs(["📱 即時診斷與部位試算", "🧪 歷史策略回測實驗室"])

# ==================== 分頁一：即時診斷 ====================
with tab_diag:
    # ---------- 大盤 ----------
    st.subheader("🌐 大盤位階")
    try:
        mkt_df = load_index()
    except Exception as e:
        mkt_df = None
        st.error(f"大盤資料取得失敗：{e}")

    mk = S.market_summary(mkt_df) if (mkt_df is not None and len(mkt_df) >= 70) else None
    if mk:
        c1, c2 = st.columns(2)
        c1.metric("加權指數", f"{mk['close']:,.1f}")
        c2.metric("大盤層級", f"第 {mk['layer']} 層", mk["layer_name"])
        for line in S.describe_market(mk):
            st.markdown(line)
        st.caption("加權指數日 K 線與成交量（Yahoo 資料）：")
        st.plotly_chart(charts.plot_k_vol(mk["df"], is_market=True), use_container_width=True,
                        config={"displayModeBar": False})
        if mk["active"]:
            (st.warning if mk["risk"] else st.success)(
                "大盤目前允許：" + "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"]))
        else:
            st.error("🛑 大盤空頭尚未止跌確認，目前不新進場。")

    # ---------- 資金比例 ----------
    st.divider()
    st.subheader("⚙️ 三段投入資金比例")
    b1, b2, b3 = st.columns(3)
    b1.button("平衡 30/40/30", use_container_width=True, on_click=set_dw, args=(30, 40))
    b2.button("重壓 30/50/20", use_container_width=True, on_click=set_dw, args=(30, 50))
    b3.button("右側 20/50/30", use_container_width=True, on_click=set_dw, args=(20, 50))
    cw1, cw2, cw3 = st.columns(3)
    w1 = cw1.number_input("第 1 筆 (%)", 5, 90, step=5, key="diag_w1")
    w2 = cw2.number_input("第 2 筆 (%)", 0, max(0, 100 - w1), step=5, key="diag_w2")
    w3 = max(0, 100 - w1 - w2)
    cw3.metric("第 3 筆 (%)", f"{w3}%")
    ratios = S.norm_ratios((w1, w2, w3))
    st.caption(f"🔒 `{w1}%` + `{w2}%` + `{w3}%` = 100%。比例會同時影響「創高拉回」的進場區間與所有點位試算。")

    # ---------- 個股 ----------
    st.divider()
    st.subheader("🔍 個股診斷：適不適合進場？")
    try:
        directory = get_directory()
    except Exception as e:
        directory = None
        st.warning(f"官方股票清單暫時抓不到（{e}），目前只能用代號查詢，且不顯示中文名稱與產業。")

    query = st.text_input("輸入股票代號或中文名稱（例：2330、台積電、保瑞）：", "台積電").strip()
    choice = None
    matches = lookup.resolve(query, directory) if (directory is not None and query) else []
    if len(matches) == 1:
        choice = matches[0]
    elif len(matches) > 1:
        labels = [f"{m['code']} {m['name']}（{m['industry'] or m['market']}）" for m in matches]
        pick = st.selectbox("找到多檔，請選擇：", labels)
        choice = matches[labels.index(pick)]
    elif query:
        q = re.sub(r"\.(TWO|TW)$", "", query.upper())
        if re.fullmatch(r"\d{4,6}", q):
            choice = {"code": q, "name": "", "industry": "", "suffix": None, "market": ""}
            st.caption("官方清單查不到這個代號（可能是 ETF 或特別股），仍會嘗試抓行情。")
        else:
            st.warning("找不到符合的股票，請確認名稱，或改用 4 碼代號。")

    if st.button("🚀 開始診斷", use_container_width=True, disabled=choice is None):
        code = choice["code"]
        with st.spinner(f"載入 {code} {choice['name']} 的行情..."):
            df = None
            for sfx in ([choice["suffix"]] if choice["suffix"] else [".TW", ".TWO"]):
                try:
                    h = load_stock(code, sfx)
                except Exception:
                    h = None
                if h is not None and not h.empty and len(h) >= 10:
                    df = h
                    break

        if df is None:
            st.error(f"查無代號【{code}】的歷史資料，請確認是否為有效掛牌標的。")
        else:
            ev = S.evaluate(df, mkt_df, ratios)
            name = choice["name"] or code
            close = ev.get("close", float(df["Close"].iloc[-1]))
            chg = (float(df["Close"].iloc[-1]) / float(df["Close"].iloc[-2]) - 1) * 100
            st.markdown(f"### {'🔴' if chg > 0 else '🟢'} **{code} {name}** ｜ 現價 `{close:.2f}`（{chg:+.2f}%）")
            st.markdown(f"🧩 **官方產業別**：`{choice['industry'] or '（官方清單未提供）'}`")
            themes = get_themes().get(code, [])
            if themes:
                st.markdown("🔥 **題材**：" + " ".join(f"`{t}`" for t in themes))
            else:
                st.caption("🔥 題材：尚未設定題材表（可用 Google 試算表自行維護，設定 THEMES_CSV_URL）")

            if "error" in ev:
                st.error(ev["error"])
            else:
                level, text = S.verdict(ev)
                {"go": st.success, "watch": st.warning, "stop": st.error, "wait": st.info}[level](text)

                _, chip_txt = get_chip_analysis(code, df)
                st.info(f"📊 {chip_txt}")
                st.plotly_chart(charts.plot_k_vol(df, is_market=False), use_container_width=True,
                                config={"displayModeBar": False})

                for sname in ("pullback", "flag", "breakout", "dry"):
                    r = ev["results"][sname]
                    label = cfg.STRATEGY_NAMES[sname]
                    if r is None:
                        st.caption(f"{label}：資料不足，無法判斷")
                        continue
                    applicable = "大盤目前適用" if r["active"] else "大盤目前不適用"
                    with st.expander(f"{'✅' if r['ok'] else '❌'} {label}｜{'符合' if r['ok'] else '尚未符合'}｜{applicable}",
                                     expanded=r["ok"] or sname == "pullback"):
                        for cname, cok, detail in r["checks"]:
                            st.markdown(f"{'✅' if cok else '❌'} **{cname}**：{detail}")
                        if sname == "pullback":
                            st.markdown(f"🎯 **理想第 1 筆進場區**：`{r['zone_lo']}` ~ `{r['zone_hi']}`"
                                        f"（依你的比例 {w1}/{w2}/{w3} 推算，前高 `{r['key']:.2f}`）")
                            if r["in_zone"]:
                                st.caption("現價已在進場區內，以下以現價為第 1 筆試算。")
                            elif r["close"] > r["zone_hi"]:
                                st.caption(f"現價還沒回檔到進場區，以下假設第 1 筆掛在 {r['zone_hi']} 試算。")
                            else:
                                st.caption("回檔已超過上限，規則上不進場；以下仍以進場區上緣試算供參考。")
                        if r["ok"] or sname == "pullback":
                            if not r["active"] and r["ok"]:
                                st.warning("型態符合，但目前大盤層級不適用這套策略。")
                            show_plan(S.plan_for(sname, r, ratios))

# ==================== 分頁二：歷史回測 ====================
with tab_bt:
    st.subheader("🧪 歷史策略回測實驗室")
    st.warning(
        "⚠️ 這個分頁原本顯示的報酬、勝率、回撤，是用**亂數**產生的示意數字，不是真實回測，已經移除。\n\n"
        "真實回測用真實歷史價格計算，會放在 GitHub Actions 的 backtest 功能，之後再把結果接到這裡。"
    )
