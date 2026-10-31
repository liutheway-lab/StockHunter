"""app.py — 台股戰情室（Streamlit 網頁）
功能：大盤位階（五層＋止跌確認）、個股診斷（四套策略＋分批計畫）。
資料口徑跟夜間掃描完全相同（data.py、strategies.py 共用）。
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf

import charts
import config as cfg
import data
import guide
import lookup
import strategies as S

st.set_page_config(page_title="台股戰情室", page_icon="📈", layout="centered")
st.title("📈 台股戰情室")
st.caption(f"台灣時間 {datetime.now(ZoneInfo('Asia/Taipei')):%Y-%m-%d %H:%M}｜大盤五層 × 四套策略")

def guide_markdown():
    """說明書原本是 Telegram 格式；轉成網頁 Markdown：*粗體* → **粗體**，每個換行都保留"""
    t = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"**\1**", guide.build()[0])
    return t.replace("\n", "  \n")


with st.expander("📖 規則說明書"):
    st.markdown(guide_markdown())


# ---------- 資料（快取，避免每次操作都重抓） ----------
@st.cache_data(ttl=600, show_spinner=False)
def load_market():
    return data.fetch_market()


@st.cache_data(ttl=600, show_spinner=False)
def load_stock(code, suffix):
    # auto_adjust=False：價格不還原股利，跟看盤軟體、夜間掃描一致
    return yf.Ticker(code + suffix).history(period="1y", auto_adjust=False)


@st.cache_data(ttl=60 * 60 * 24, show_spinner="載入證交所官方股票清單...")
def load_directory():
    return lookup.load_directory()


def plan_table(p):
    r = p["ratios"]
    return (
        "| 階段 | 動作 | 價位 | 資金比例 | 累計均價 |\n| :--- | :--- | :--- | :--- | :--- |\n"
        f"| **第 1 筆** | 進場 | `{p['e']:.2f}` | {r[0] * 100:.0f}% | `{p['e']:.2f}` |\n"
        f"| **第 2 筆** | {p['key_label']} | `{p['b']:.2f}` | {r[1] * 100:.0f}% | `{p['avg2']:.2f}` |\n"
        f"| **第 3 筆** | 突破價 +{(cfg.EXT_RATIO - 1) * 100:.0f}% | `{p['b2']:.2f}` | {r[2] * 100:.0f}% | `{p['avg3']:.2f}` |"
    )


def show_plan(p):
    st.markdown(plan_table(p))
    c1, c2, c3 = st.columns(3)
    c1.metric("第 1 筆停損價", f"{p['stop1']:.2f}")
    c2.metric("第 2 筆防守價", f"{p['stop2']:.2f}")
    c3.metric("目標價", f"{p['targets'][0][1]:.2f}")
    if p.get("stop1_note"):
        st.caption(f"停損依據：{p['stop1_note']}")
    if p.get("breakeven"):
        st.caption(f"第 2 筆被砍掉後，第 1 筆要漲到 `{p['breakeven']:.2f}` 整趟才打平")
    if len(p["targets"]) > 1:
        st.caption("目標價：" + "；".join(f"{lbl} `{px:.2f}`" for lbl, px in p["targets"]))
    for n in p.get("notes", []):
        st.caption(n)


# ==================== 大盤 ====================
st.subheader("🌐 大盤位階")
mk = None
try:
    mkt_df = load_market()
    if len(mkt_df) >= 70:
        mk = S.market_summary(mkt_df)
except Exception as e:
    mkt_df = None
    st.error(f"大盤資料取得失敗：{e}")

if mk:
    c1, c2 = st.columns(2)
    c1.metric("加權指數", f"{mk['close']:,.1f}")
    c2.metric("大盤層級", f"第 {mk['layer']} 層", mk["layer_name"], delta_color="off")
    for line in S.describe_market(mk):
        st.markdown(line)
    show_stab = mk["stab"] if mk["layer"] in (4, 5) else None
    st.plotly_chart(charts.plot_k_vol(mk["df"], is_market=True, stab=show_stab),
                    use_container_width=True, config={"displayModeBar": False})
    if show_stab:
        st.caption("虛線＝前低，黃色區＝近 3 日。成交金額採用證交所數據，看相對比例。")
    if mk["active"]:
        (st.warning if mk["risk"] else st.success)(
            "大盤目前允許：" + "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"]))
    else:
        st.error("🛑 大盤空頭尚未止跌確認，目前不新進場。")

# ==================== 資金比例 ====================
st.divider()
st.subheader("⚙️ 三段投入資金比例")
if "w1" not in st.session_state:
    st.session_state.w1 = int(round(cfg.TRANCHES[0] * 100))
    st.session_state.w2 = int(round(cfg.TRANCHES[1] * 100))


def set_w(a, b):
    st.session_state.w1, st.session_state.w2 = a, b


b1, b2, b3 = st.columns(3)
b1.button("平衡 30/40/30", use_container_width=True, on_click=set_w, args=(30, 40))
b2.button("重壓 30/50/20", use_container_width=True, on_click=set_w, args=(30, 50))
b3.button("右側 20/50/30", use_container_width=True, on_click=set_w, args=(20, 50))
cw1, cw2, cw3 = st.columns(3)
w1 = cw1.number_input("第 1 筆 (%)", 5, 90, step=5, key="w1")
w2 = cw2.number_input("第 2 筆 (%)", 0, max(0, 100 - w1), step=5, key="w2")
w3 = max(0, 100 - w1 - w2)
cw3.metric("第 3 筆 (%)", f"{w3}%")
ratios = S.norm_ratios((w1, w2, w3))
st.caption(f"{w1}% + {w2}% + {w3}% = 100%。比例會影響「創高拉回」的進場區間和所有價位試算。")

# ==================== 個股診斷 ====================
st.divider()
st.subheader("🔍 個股診斷")
try:
    directory = load_directory()
except Exception as e:
    directory = None
    st.warning(f"官方股票清單暫時抓不到（{e}），目前只能用代號查詢。")

query = st.text_input("輸入股票代號或中文名稱（例：2330、台積電）", "台積電").strip()
choice = None
matches = lookup.resolve(query, directory) if (directory is not None and query) else []
if len(matches) == 1:
    choice = matches[0]
elif len(matches) > 1:
    labels = [f"{m['code']} {m['name']}（{m['industry'] or m['market']}）" for m in matches]
    pick = st.selectbox("找到多檔，請選擇", labels)
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
        if df is not None:
            df = data._clean(df)

    if df is None:
        st.error(f"查無代號【{code}】的歷史資料，請確認是否為有效掛牌標的。")
    else:
        ev = S.evaluate(df, mkt_df, ratios)
        name = choice["name"] or code
        close = float(df["Close"].iloc[-1])
        chg = (close / float(df["Close"].iloc[-2]) - 1) * 100
        st.markdown(f"### {'🔴' if chg > 0 else '🟢'} {code} {name}｜現價 `{close:.2f}`（{chg:+.2f}%）")
        if choice["industry"]:
            st.caption(f"產業別：{choice['industry']}")
        ex = "TPEX" if choice.get("suffix") == ".TWO" else "TWSE"
        st.markdown(f"[📊 在 TradingView 開啟線圖](https://www.tradingview.com/chart/?symbol={ex}%3A{code})")

        if "error" in ev:
            st.error(ev["error"])
        else:
            level, text = S.verdict(ev)
            {"go": st.success, "watch": st.warning, "stop": st.error, "wait": st.info}[level](text)
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
                    for i, (cname, cok, detail) in enumerate(r["checks"], 1):
                        st.markdown(f"{'✅' if cok else '❌'} {i}. **{cname}**：{detail}")
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
