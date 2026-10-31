"""app.py — 台股戰情室（Streamlit 網頁）
功能：大盤位階（五層＋止跌確認）、個股診斷（四套策略＋分批計畫）。
資料口徑跟夜間掃描完全相同（data.py、strategies.py 共用）。
"""
import html
import json
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

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


def _toggle(key):
    st.session_state[key] = not st.session_state.get(key, False)


def _close(key):
    st.session_state[key] = False
    st.session_state["_scroll_to"] = key      # 收合後，捲回這個區塊的標題


def _scroll_script(key, title):
    """收合後頁面會變短、瀏覽器停在最底部，所以用小段 JS 把畫面捲回標題那一顆按鈕"""
    t = json.dumps(title)
    components.html(f"""<script>
    setTimeout(function () {{
      const d = window.parent.document;
      let el = d.querySelector('.st-key-open_{key}');
      if (!el) {{
        for (const b of d.querySelectorAll('button')) {{
          if (b.innerText.indexOf({t}) >= 0) {{ el = b; break; }}
        }}
      }}
      if (el) el.scrollIntoView({{block: 'center'}});
    }}, 40);
    </script>""", height=0)


_fragment = getattr(st, "fragment", None) or (lambda f: f)


@_fragment
def foldable(key, title, body, default=False, status=None):
    """可展開／收合的區塊：標題是一顆按鈕，展開後最下面也有一顆收合鈕（收合後會捲回標題）"""
    if key not in st.session_state:
        st.session_state[key] = default
    opened = st.session_state[key]
    st.button(f"{'▼' if opened else '▶'}　{title}", key=f"open_{key}", on_click=_toggle, args=(key,),
              use_container_width=True)
    if status:
        st.caption(status)
    if st.session_state.get("_scroll_to") == key:
        st.session_state["_scroll_to"] = None
        _scroll_script(key, title)
    if opened:
        body()
        st.button("▲　收合", key=f"close_{key}", on_click=_close, args=(key,), use_container_width=True)


foldable("guide", "📖 規則說明書", lambda: st.markdown(guide_markdown()))


# ---------- 圖表（plotly）----------
UP, DOWN = "#e04a4a", "#2e9e4f"
MA_STYLE = (("MA5", "#f1c40f", 1.2), ("MA10", "#1abc9c", 1.2), ("MA20", "#9b59b6", 1.4), ("MA60", "#3498db", 1.6))


def prepare_chart_data(df, is_market=False, bars=65):
    """回傳 (要畫的 DataFrame, 成交量欄位名稱)。個股量用「張」，大盤用證交所成交金額（億元）"""
    d = df.copy() if is_market else df[df["Volume"] > 0].copy()
    if is_market:
        d["Plot_Vol"] = d["Volume"] / 1e8
        label = "成交金額（證交所，億）"
    else:
        d["Plot_Vol"] = d["Volume"] / 1000.0
        label = "成交量（張）"
    for n in (5, 10, 20, 60):
        d[f"MA{n}"] = d["Close"].rolling(n).mean()
    d["VMA20"] = d["Plot_Vol"].rolling(20).mean()
    return d.iloc[-bars:], label


def plot_k_vol(df, is_market=False, bars=65, stab=None):
    """stab：大盤空頭時傳入 stabilization 結果，會畫出前低虛線與近 3 日色塊"""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    p, vol_label = prepare_chart_data(df, is_market, bars)
    dates = [d.strftime("%m/%d") for d in p.index]
    v_colors = [UP if c >= o else DOWN for c, o in zip(p["Close"], p["Open"])]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.74, 0.26])
    fig.add_trace(go.Candlestick(x=dates, open=p["Open"], high=p["High"], low=p["Low"], close=p["Close"],
                                 name="日K", increasing_line_color=UP, decreasing_line_color=DOWN,
                                 showlegend=False), row=1, col=1)
    for col, color, width in MA_STYLE:
        fig.add_trace(go.Scatter(x=dates, y=p[col], line=dict(color=color, width=width), name=col),
                      row=1, col=1)
    fig.add_trace(go.Bar(x=dates, y=p["Plot_Vol"], marker_color=v_colors, name=vol_label, showlegend=False),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["VMA20"], line=dict(color="#e67e22", width=1.1), name="20日均量"),
                  row=2, col=1)

    if stab and stab.get("prior_low") is not None:
        s0, s1 = f"{stab['recent_start']:%m/%d}", f"{stab['recent_end']:%m/%d}"
        if s0 in dates and s1 in dates:
            # 近 3 日：淡黃色底，只蓋住上面的 K 線圖（不蓋成交金額）
            fig.add_shape(type="rect", xref="x", yref="y domain", x0=dates.index(s0) - 0.5,
                          x1=dates.index(s1) + 0.5, y0=0, y1=1, fillcolor="rgba(255,221,0,0.18)",
                          line_width=0, layer="above")

        def mark(day, val, text, color, ax, ay):
            if day in dates:
                # 只拉一條細細的箭頭，在末端寫數字（不放標記）
                fig.add_annotation(x=day, y=val, xref="x", yref="y", text=text, showarrow=True,
                                   arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=color,
                                   ax=ax, ay=ay, font=dict(color=color, size=12),
                                   bgcolor="rgba(17,17,17,0.85)", borderpad=2)

        mark(f"{stab['prior_low_date']:%m/%d}", stab["prior_low"], f"前低 {stab['prior_low']:,.0f}",
             "#ff9f1a", -70, 40)
        if stab.get("recent_low") is not None and stab.get("recent_low_date") is not None:
            mark(f"{stab['recent_low_date']:%m/%d}", stab["recent_low"],
                 f"近3日低 {stab['recent_low']:,.0f}", "#00e5ff", -90, 95)

    fig.update_layout(
        xaxis_rangeslider_visible=False, margin=dict(l=5, r=5, t=35, b=5), height=440,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=10)),
        template="plotly_dark", dragmode=False, hovermode=False,
    )
    fig.update_xaxes(type="category", tickangle=-45, showgrid=False, nticks=10, range=[-0.5, len(dates) - 0.5])
    fig.update_yaxes(side="right", gridcolor="rgba(255,255,255,0.08)")
    fig.update_yaxes(title_text=vol_label, title_font_size=10, row=2, col=1)
    return fig


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


def px(x):
    """依台股跳動單位決定小數位數：1000 元以上不顯示小數、500～1000 元整數、100～500 元一位…"""
    t = S._tick_size(x)
    d = 0 if t >= 1 else (1 if t in (0.5, 0.1) else 2)
    return f"{x:,.{d}f}"


def sdiff(x, ref):
    """價差（帶正負號），小數位數跟著 ref 的跳動單位"""
    t = S._tick_size(ref)
    d = 0 if t >= 1 else (1 if t in (0.5, 0.1) else 2)
    return f"{x:+,.{d}f}"


def html_table(head, rows, wrap=(), left=(), bold_first=False):
    """有質感的表格：不換行（長文字欄除外）、淡色格線、表頭底色、數字靠右。放不下時可以左右滑動"""
    n = len(head)
    esc = lambda t: html.escape(str(t))
    border = "1px solid rgba(128,128,128,0.28)"

    def cell(tag, i, text, top):
        align = "left" if i == 0 or i in wrap or i in left else "right"
        ws = "normal" if i in wrap else "nowrap"
        bg = "background:rgba(128,128,128,0.14);font-weight:600;" if tag == "th" else ""
        bt = f"border-top:{border};" if top else ""
        return (f'<{tag} style="padding:7px 10px;text-align:{align};white-space:{ws};{bg}{bt}'
                f'font-size:0.9rem;line-height:1.35;">{esc(text)}</{tag}>')

    out = ['<div style="overflow-x:auto;margin:4px 0 10px 0;"><table style="width:100%;'
           f'border-collapse:separate;border-spacing:0;border:{border};border-radius:8px;overflow:hidden;">']
    out.append("<tr>" + "".join(cell("th", i, h, False) for i, h in enumerate(head)) + "</tr>")
    for r in rows:
        out.append("<tr>" + "".join(cell("td", i, c, True) for i, c in enumerate(r)) + "</tr>")
    out.append("</table></div>")
    return "".join(out)


def show_table(head, rows, **kw):
    st.markdown(html_table(head, rows, **kw), unsafe_allow_html=True)


def md_cond(rows):
    """條件表（結果 | 條件 | 現況）。現況欄可以換行，其他欄不換行"""
    show_table(["", "條件", "現況"], rows, wrap=(2,), left=(1,))


def plan_view(p, capital, close):
    """分批計畫：價位、距現價、均價、投入、股數、各階段停損與損益"""
    r = p["ratios"]
    rows = [("① 進場", p["e"], r[0]), ("② 突破", p["b"], r[1]), ("③ 加碼", p["b2"], r[2])]
    gap = lambda price: f"{(price / close - 1) * 100:+.1f}%"
    st.markdown(f"**預期第 1 筆進場 {px(p['e'])}**　現價 {px(close)}，差 {sdiff(p['e'] - close, close)} 元（{gap(p['e'])}）")

    shares, costs = [], []
    for _, price, ratio in rows:
        sh = int(capital * ratio // price) if price > 0 else 0
        shares.append(sh)
        costs.append(sh * price)
    avgs = []
    for k in range(3):
        sh, cost = sum(shares[:k + 1]), sum(costs[:k + 1])
        avgs.append(S.tick(cost / sh) if sh > 0 else p["e"])

    show_table(["階段", "價位", "距現價", "累計均價"],
               [(lab, px(price), gap(price), px(avgs[i])) for i, (lab, price, _) in enumerate(rows)])
    show_table(["階段", "投入（元）", "股數"],
               [(lab, f"{capital * ratio:,.0f}", f"{shares[i]:,}") for i, (lab, _, ratio) in enumerate(rows)]
               + [("合計", f"{sum(costs):,.0f}", f"{sum(shares):,}")])
    st.caption(f"② 突破＝突破價；③ 加碼＝突破價 +{(cfg.EXT_RATIO - 1) * 100:.0f}%。"
               f"可用資金 {capital:,.0f} 元，剩餘 {capital - sum(costs):,.0f} 元。")

    # 各階段停損：只買第 1 筆時停損在停損價；突破加碼後，停損拉到「累計均價（進位到可下單價）」，扣掉手續費與證交稅
    st.markdown("**各階段停損**")
    risk = []
    for k, name in enumerate(("① 第 1 筆", "② 前 2 筆", "③ 前 3 筆")):
        sh, cost = sum(shares[:k + 1]), sum(costs[:k + 1])
        if sh <= 0:
            continue
        stop = p["stop1"] if k == 0 else S.tick(cost / sh, "ceil")
        pnl = sh * stop - cost - cost * cfg.FEE_RT
        risk.append((name, px(stop), f"{(stop / close - 1) * 100:+.1f}%", f"{pnl:+,.0f}"))
    show_table(["持有", "停損價", "距現價", "停損損益（元）"], risk)
    st.caption("第 2 筆（突破）買進後，停損拉到累計均價，損益只剩手續費與證交稅（來回約 "
               f"{cfg.FEE_RT * 100:.1f}%）。若跳空跌破停損價，實際虧損會比表上多。")

    st.markdown(f"🎯 **目標價　{px(p['targets'][0][1])}**（距現價 {gap(p['targets'][0][1])}）")
    if p.get("stop1_note"):
        st.caption(f"第 1 筆停損依據：{p['stop1_note']}")
    if len(p["targets"]) > 1:
        st.caption("目標價：" + "；".join(f"{lbl} {px(v)}" for lbl, v in p["targets"]))
    for n in p.get("notes", []):
        st.caption(n)


def show_plan(p, capital, close):
    plan_view(p, capital, close)


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

LAYER_ICON = {0: "⚪", 1: "🟢", 2: "🟢", 3: "🟡", 4: "🔴", 5: "🔴"}

if mk:
    c1, c2 = st.columns(2)
    c1.metric("加權指數", f"{mk['close']:,.1f}")
    c2.metric(f"大盤層級（第 {mk['layer']} 層）", f"{LAYER_ICON[mk['layer']]} {mk['layer_name']}")
    ma = mk["ma"]
    st.markdown(f"收盤 `{mk['close']:,.0f}`　MA5 `{ma[5]:,.0f}`  \n"
                f"MA10 `{ma[10]:,.0f}`　MA20 `{ma[20]:,.0f}`  \n"
                f"MA60 `{ma[60]:,.0f}`")

    show_stab = mk["stab"] if mk["layer"] in (4, 5) else None
    if show_stab is not None or mk["layer"] in (4, 5):
        s_ = mk["stab"]
        if s_ is None:
            st.markdown("🧱 **止跌確認**：資料不足")
        elif s_["vol_missing"]:
            st.markdown("🧱 **止跌確認**：❌ 無法確認（大盤成交金額抓不到）")
        else:
            ok = lambda b: "✅" if b else "❌"
            ratio = s_["recent_vol"] / s_["dump_vol"] * 100 if s_["dump_vol"] else 0
            st.markdown(f"🧱 **止跌確認**　{'✅ 通過' if s_['ok'] else '🔴 尚未止跌'}（兩項都要通過）")
            md_cond([
                (ok(s_["no_break"]), "不破低",
                 f"近 3 日最低 {s_['recent_low']:,.0f}（{s_['recent_low_date']:%m/%d}），"
                 f"前低 {s_['prior_low']:,.0f}（{s_['prior_low_date']:%m/%d}）"),
                (ok(s_["vol_ok"]), "量承接",
                 f"近 3 日最大量為爆量下跌日（{s_['dump_date']:%m/%d}）的 {ratio:.0f}%，需 100% 以上"),
            ])

    st.plotly_chart(plot_k_vol(mk["df"], is_market=True, stab=show_stab),
                    use_container_width=True, config={"displayModeBar": False})
    if show_stab:
        st.caption("黃色底＝近 3 日。")
        st.caption("成交金額採用證交所數據，看相對比例。")
    if mk["active"]:
        (st.warning if mk["risk"] else st.success)(
            "大盤目前允許：" + "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"]))
    else:
        st.error("🔴 尚未止跌，不建立多頭部位")

# ==================== 今日推薦 ====================
RESULT_PATH = Path(__file__).parent / "scan_results.json"
ORDER = ("pullback", "flag", "breakout", "dry")


@st.cache_data(ttl=300, show_spinner=False)
def _load_scan(mtime):
    with open(RESULT_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_scan():
    try:
        return _load_scan(RESULT_PATH.stat().st_mtime)
    except Exception:
        return None


def pick(item):
    """點推薦清單的股票：帶入下面的個股診斷"""
    st.session_state["target"] = {k: item.get(k, "") for k in ("code", "name", "industry", "suffix", "market")}
    st.session_state["_scroll_diag"] = True


def rec_buttons(prefix, strategy, items, limit=15):
    for it in items[:limit]:
        arrow = "↑" if it.get("broke") else ""
        st.button(f"{it['code']} {it['name']}{arrow}　現價 {px(it['close'])}｜突破價 {px(it['key'])}",
                  key=f"{prefix}_{strategy}_{it['code']}", on_click=pick, args=(it,), use_container_width=True)
    if len(items) > limit:
        st.caption(f"還有 {len(items) - limit} 檔未列出（依成交金額排序）")


st.divider()
st.subheader("🌟 今日推薦")
scan = get_scan()
if not scan:
    st.info("還沒有夜間掃描資料。每個交易日 22:30 掃描完成後，這裡就會出現推薦。")
else:
    hits, active = scan["hits"], scan["active"]
    st.caption(f"資料日 {scan['data_date']}｜夜間掃描 {scan['generated_at']}｜依成交金額由大到小｜點股票會帶入下方診斷")
    if active:
        names_on = "、".join(cfg.STRATEGY_NAMES[a] for a in active)
        (st.warning if scan["risk"] else st.success)(
            f"大盤第 {scan['layer']} 層・{scan['layer_name']}，啟用：{names_on}" + ("（方向未明，請縮小部位）" if scan["risk"] else ""))
        for name in ORDER:
            if name in active:
                items = hits.get(name, [])
                with st.expander(f"{cfg.STRATEGY_NAMES[name]}｜{len(items)} 檔", expanded=bool(items)):
                    if items:
                        rec_buttons("rec", name, items)
                    else:
                        st.caption("今天沒有符合的股票。")
    else:
        st.error("🔴 尚未止跌，不建立多頭部位，今日不推薦。")

    watch = [(n, hits.get(n, [])) for n in ORDER if n not in active and hits.get(n)]
    if watch:
        st.markdown("**👀 觀察名單**（型態符合，但大盤目前不適用，僅供追蹤）")
        for name, items in watch:
            with st.expander(f"{cfg.STRATEGY_NAMES[name]}｜{len(items)} 檔", expanded=False):
                rec_buttons("obs", name, items)

# ==================== 資金比例 ====================
st.divider()
st.subheader("⚙️ 資金與三段投入比例")
capital = st.number_input("可用資金（元）", min_value=10000, max_value=100_000_000, value=1_000_000,
                          step=50000, key="capital",
                          help="可以直接輸入金額，或按 + / − 調整，每次 5 萬")
st.caption("💡 可直接輸入金額，或按 ＋／－ 調整（每次 5 萬）。選好標的後，各策略的分批計畫會依這筆資金換算每一筆可買的股數與停損金額。")
if "w1" not in st.session_state:
    st.session_state.w1 = int(round(cfg.TRANCHES[0] * 100))
    st.session_state.w2 = int(round(cfg.TRANCHES[1] * 100))


def set_w(a, b):
    st.session_state.w1, st.session_state.w2 = a, b


b1, b2, b3 = st.columns(3)
b1.button("平衡策略 30/40/30", use_container_width=True, on_click=set_w, args=(30, 40))
b2.button("突破策略 30/50/20", use_container_width=True, on_click=set_w, args=(30, 50))
b3.button("順勢策略 20/50/30", use_container_width=True, on_click=set_w, args=(20, 50))
cw1, cw2, cw3 = st.columns(3)
w1 = cw1.number_input("第 1 筆 (%)", 5, 90, step=5, key="w1")
w2 = cw2.number_input("第 2 筆 (%)", 0, max(0, 100 - w1), step=5, key="w2")
w3 = max(0, 100 - w1 - w2)
cw3.metric("第 3 筆 (%)", f"{w3}%")
ratios = S.norm_ratios((w1, w2, w3))
st.markdown(f"**各階段可投入**  \n"
            f"第 1 筆　{capital * ratios[0]:,.0f} 元（{w1}%）  \n"
            f"第 2 筆　{capital * ratios[1]:,.0f} 元（{w2}%）  \n"
            f"第 3 筆　{capital * ratios[2]:,.0f} 元（{w3}%）")
st.caption("比例會影響「創高拉回」的進場區間；資金會用來試算每一筆可以買幾股。")

# ==================== 個股診斷 ====================
st.divider()
st.subheader("🔍 個股診斷")
if st.session_state.pop("_scroll_diag", False):
    components.html("""<script>
    setTimeout(function () {
      for (const h of window.parent.document.querySelectorAll('h1,h2,h3')) {
        if (h.innerText.indexOf('個股診斷') >= 0) { h.scrollIntoView({block: 'start'}); break; }
      }
    }, 150);
    </script>""", height=0)
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
    st.session_state["target"] = choice      # 記住結果，按收合鈕時才不會消失

target = st.session_state.get("target")
if target:
    code = target["code"]
    with st.spinner(f"載入 {code} {target['name']} 的行情..."):
        df = None
        for sfx in ([target["suffix"]] if target["suffix"] else [".TW", ".TWO"]):
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
        name = target["name"] or code
        close = float(df["Close"].iloc[-1])
        chg = (close / float(df["Close"].iloc[-2]) - 1) * 100
        st.markdown(f"### {'🔴' if chg > 0 else '🟢'} {code} {name}｜現價 `{px(close)}`（{chg:+.2f}%）")
        if target["industry"]:
            st.caption(f"產業別：{target['industry']}")
        ex = "TPEX" if target.get("suffix") == ".TWO" else "TWSE"
        st.markdown(f"[📊 在 TradingView 開啟線圖](https://www.tradingview.com/chart/?symbol={ex}%3A{code})")

        if "error" in ev:
            st.error(ev["error"])
        else:
            level, text = S.verdict(ev)
            {"go": st.success, "watch": st.warning, "stop": st.error, "wait": st.info}[level](text)
            st.plotly_chart(plot_k_vol(df, is_market=False), use_container_width=True,
                            config={"displayModeBar": False})

            st.caption(f"以下依可用資金 {capital:,.0f} 元、比例 {w1}/{w2}/{w3} 換算股數與停損金額")
            for sname in ("pullback", "flag", "breakout", "dry"):
                r = ev["results"][sname]
                label = cfg.STRATEGY_NAMES[sname]
                if r is None:
                    st.caption(f"{label}：資料不足，無法判斷")
                    continue
                applicable = "大盤適用" if r["active"] else "大盤不適用"

                def body(r=r, sname=sname):
                    st.markdown(f"**現價 {px(close)}**")
                    md_cond([("✅" if cok else "❌", cname, detail) for cname, cok, detail in r["checks"]])
                    if sname == "pullback":
                        st.markdown(f"🎯 **理想第 1 筆進場區　{px(r['zone_lo'])} ~ {px(r['zone_hi'])}**")
                        st.caption(f"依你的比例 {w1}/{w2}/{w3} 推算，前高 {px(r['key'])}")
                        if r["in_zone"]:
                            st.caption("現價已在進場區內，以下以現價為第 1 筆試算。")
                        elif r["close"] > r["zone_hi"]:
                            st.caption(f"現價還沒回檔到進場區，以下假設第 1 筆掛在 {px(r['zone_hi'])} 試算。")
                        else:
                            st.caption("回檔已超過上限，規則上不進場；以下仍以進場區上緣試算供參考。")
                    if r["ok"] and not r["active"]:
                        st.warning("型態符合，但目前大盤層級不適用這套策略。")
                    if "key" not in r or ("stop1" not in r and sname != "pullback"):
                        st.info("目前沒有符合的型態，無法試算。")
                    else:
                        plan = S.plan_for(sname, r, ratios)
                        if sname != "pullback" and plan["stop1"] >= plan["e"]:
                            st.info("現價已低於停損價，無法試算。")
                        else:
                            if not r["ok"]:
                                st.warning("尚未符合條件，以下試算僅供參考。")
                            show_plan(plan, capital, close)

                foldable(f"{code}_{sname}", label, body, default=r["ok"],
                         status=f"{'✅ 符合' if r['ok'] else '❌ 尚未符合'}｜{applicable}")

st.caption("最後更新：2026 年 10 月")
