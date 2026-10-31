"""app.py — 台股戰情室（Streamlit 網頁）
功能：大盤位階（五層＋止跌確認）、個股診斷（四套策略＋分批計畫）。
資料口徑跟夜間掃描完全相同（data.py、strategies.py 共用）。
"""
import html
import time
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
st.caption(f"台灣時間 {datetime.now(ZoneInfo('Asia/Taipei')):%Y-%m-%d %H:%M}")

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
    // {time.time()}
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


def L(*parts):
    """把一段話拆成幾行，每行都短，手機上不會被硬切斷"""
    return "  \n".join(p for p in parts if p)


def brk(t):
    """在逗號、分號後面換行，避免長句被螢幕寬度切在奇怪的地方"""
    return re.sub(r"([，；])", r"\1  \n", t)


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


def vol_low_info(df):
    """近 30 日成交量最大那天的最低點（main）。
    若它比現價高（壓力），再從近 60 日找「最低點在現價之下、量最大」的那天當短線支撐（sup）"""
    d = df[df["Volume"] > 0]
    if d.empty:
        return None
    close = float(df["Close"].iloc[-1])

    def pack(day):
        return {"key": f"{day:%m/%d}", "low": float(d.loc[day, "Low"]), "vol": float(d.loc[day, "Volume"]) / 1000.0}

    d30 = d.iloc[-30:]
    main = pack(d30["Volume"].idxmax())
    main["pressure"] = main["low"] > close
    sup = None
    if main["pressure"]:
        d60 = d.iloc[-60:]
        cand = d60[d60["Low"] < close]
        if not cand.empty:
            sup = pack(cand["Volume"].idxmax())
    return {"main": main, "sup": sup, "close": close}


RED, GREEN = "#e03131", "#2b9a3f"      # 跟大盤燈號邏輯一致：紅＝壓力／風險，綠＝支撐


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

        def mark(day, val, text, color, ax, ay, row=1, xanchor="right", yanchor="middle"):
            if day in dates:
                # 細箭頭 + 靠右對齊的文字，沒有底色；文字放在圖上空白的位置，避免壓到 K 線
                fig.add_annotation(x=day, y=val, text=text, showarrow=True, row=row, col=1,
                                   arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=color,
                                   ax=ax, ay=ay, xanchor=xanchor, yanchor=yanchor, font=dict(color=color, size=11))

        # 前低：細虛線橫跨整張圖 + 箭頭，文字放在 K 線下方的空白處
        fig.add_hline(y=stab["prior_low"], row=1, col=1,
                      line=dict(color="rgba(255,140,0,0.7)", width=1, dash="dot"))
        mark(f"{stab['prior_low_date']:%m/%d}", stab["prior_low"],
             f"前低 {stab['prior_low']:,.0f}（{stab['prior_low_date']:%m/%d}）", "#ff8c00", -4, 62)
        if stab.get("recent_low") is not None and stab.get("recent_low_date") is not None:
            mark(f"{stab['recent_low_date']:%m/%d}", stab["recent_low"],
                 f"近3日最低 {stab['recent_low']:,.0f}（{stab['recent_low_date']:%m/%d}）", "#00a8d6", -4, 108)

        # 成交金額：爆量下跌日的量（橘框 + 虛線），以及近 3 日最大量是它的幾 %
        dd, dv, rv = stab.get("dump_date"), stab.get("dump_vol"), stab.get("recent_vol")
        if dd is not None and dv:
            dv_b = dv / 1e8
            fig.update_yaxes(range=[0, max(float(p["Plot_Vol"].max()), dv_b) * 2.3], row=2, col=1)
            fig.add_hline(y=dv_b, row=2, col=1, line=dict(color="rgba(255,140,0,0.7)", width=1, dash="dot"))
            dkey = f"{dd:%m/%d}"
            if dkey in dates:
                i = dates.index(dkey)
                fig.add_shape(type="rect", x0=i - 0.5, x1=i + 0.5, y0=0, y1=dv_b, row=2, col=1,
                              line=dict(color="#ff8c00", width=2), fillcolor="rgba(0,0,0,0)")
                # 文字往左移開，用細箭頭指到橘框，避免跟右邊「近3日最大量」重疊
                mark(dkey, dv_b, f"爆量日 {dkey}", "#ff8c00", -70, -6, row=2)
            if rv:
                # 兩行短字放在右側長條上方（虛線以上的空白），只往左伸展，不碰到「爆量日」文字
                mark(f"{stab['recent_end']:%m/%d}", rv / 1e8, f"近3日最大量<br>為爆量日 {rv / dv * 100:.0f}%",
                     "#00a8d6", -4, -40, row=2, yanchor="bottom")

    # ---- 前高 + 近期大量低點（最接近現價、成交量最大與次大的兩天，標出當天最低點）----
    hi_i = int(p["High"].to_numpy().argmax())
    hi_v = float(p["High"].iloc[hi_i])
    last_c = float(p["Close"].iloc[-1])
    n_bar = len(dates)

    def side(i):
        return "right" if i > n_bar - 9 else ("left" if i < 8 else "center")

    fig.add_hline(y=hi_v, row=1, col=1, line=dict(color="rgba(214,51,108,0.7)", width=1, dash="dot"))
    fig.add_annotation(x=dates[hi_i], y=hi_v, row=1, col=1, text=f"前高 {px(hi_v)}（{dates[hi_i]}）",
                       showarrow=True, arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor="#d6336c",
                       ax=-4 if side(hi_i) == "right" else 4, ay=-22, xanchor=side(hi_i),
                       font=dict(color="#d6336c", size=12))
    lo_pad = float(p["Low"].min())
    rng_p = max(hi_v - lo_pad, 1e-9)
    # 個股圖：下方多留一條空白帶，專門放「大量低點」的文字，這樣文字不會蓋到 K 線或成交量
    H = 280.0                                   # 價格圖大約的像素高度
    info = None if is_market else vol_low_info(df)
    band_px = 0.0
    if not is_market and info:
        band_px = 100.0 if info["sup"] else 56.0   # 下方空白帶：放「大量低點」文字的空間
    elif is_market and stab and stab.get("prior_low") is not None:
        band_px = 112.0
    # 總範圍 T：上方留 10%、下方留 4%，其餘 band_px 像素給文字
    T = rng_p * 1.14 / (1 - band_px / H)
    y_hi = hi_v + rng_p * 0.10
    y_lo = y_hi - T
    ppu = H / T                                  # 每 1 元約幾像素
    band_top = lo_pad - rng_p * 0.04
    fig.update_yaxes(range=[y_lo, y_hi], row=1, col=1)

    if not is_market and info:
        def tw(text):                       # 估計文字寬度（像素）：中文約 12，數字符號約 6.5
            return max(sum(12 if ord(ch) > 255 else 6.5 for ch in ln) for ln in text.split("<br>"))

        main, sup = info["main"], info["sup"]
        labels = [(main, RED if main["pressure"] else GREEN,
                   f"近30日最大量（{main['key']}）<br>當天最低點 {px(main['low'])}　{'壓力' if main['pressure'] else '支撐'}")]
        if sup:
            labels.append((sup, GREEN, f"短線支撐（近60日，{sup['key']}）<br>當天最低點 {px(sup['low'])}"))
        W, bar_px = 318.0, 318.0 / max(n_bar, 1)
        xs = [dates.index(it["key"]) * bar_px if it["key"] in dates else None for it, _, _ in labels]

        def span(x, w, anchor):
            return {"right": (x - w, x), "left": (x, x + w), "center": (x - w / 2, x + w / 2)}[anchor]

        # 兩個文字上下錯開成兩排，並挑選對齊方式，讓文字不超出圖、也不會被另一條箭頭穿過
        plan = None
        orders = [(0,), (0, 1), (1, 0)] if len(labels) == 2 else [(0,)]
        for order in ([o for o in orders if len(o) == len(labels)]):
            for ancs in (("right", "right"), ("right", "left"), ("left", "right"), ("left", "left"),
                         ("center", "right"), ("right", "center"), ("center", "left"), ("left", "center"),
                         ("center", "center")):
                ok = True
                spans = {}
                for row_i, li in enumerate(order):
                    x = xs[li]
                    if x is None:
                        continue
                    a_ = ancs[row_i]
                    x0, x1 = span(x, tw(labels[li][2]), a_)
                    if x0 < -4 or x1 > W + 4:
                        ok = False
                    spans[row_i] = (x0, x1)
                if ok and len(order) == 2 and xs[order[0]] is not None and xs[order[1]] is not None:
                    # 上排文字不能蓋住下排那條箭頭
                    if spans[0][0] - 6 <= xs[order[1]] <= spans[0][1] + 6:
                        ok = False
                if ok:
                    plan = (order, ancs)
                    break
            if plan:
                break
        if plan is None:
            plan = (tuple(range(len(labels))), ("right",) * 2)
        order, ancs = plan
        for row_i, li in enumerate(order):
            it, color, text = labels[li]
            if it["key"] not in dates:
                continue
            text_y = band_top - (24 + 46 * row_i) / ppu          # 兩排文字各自固定高度
            anc = ancs[row_i]
            fig.add_annotation(x=it["key"], y=it["low"], row=1, col=1, text=text,
                               showarrow=True, arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=color,
                               ax={"right": -4, "left": 4, "center": 0}[anc], ay=max(18.0, (it["low"] - text_y) * ppu),
                               xanchor=anc, font=dict(color=color, size=12))
            fig.add_hline(y=it["low"], row=1, col=1, line=dict(color=color, width=1, dash="dot"))

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
        return (f'<{tag} style="padding:6px 7px;text-align:{align};white-space:{ws};{bg}{bt}'
                f'font-size:0.84rem;line-height:1.35;">{esc(text)}</{tag}>')

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
    st.markdown(f"**預期第 1 筆進場 {px(p['e'])}**　（現價 {px(close)}，差 {gap(p['e'])}）")

    shares, costs = [], []
    for _, price, ratio in rows:
        sh = int(capital * ratio // price) if price > 0 else 0
        shares.append(sh)
        costs.append(sh * price)
    avgs = []
    for k in range(3):
        sh, cost = sum(shares[:k + 1]), sum(costs[:k + 1])
        avgs.append(S.tick(cost / sh) if sh > 0 else p["e"])

    # 一張表看完：每一筆的進場價、股數、累計均價、出場價、出場損益
    # 出場價：只買第 1 筆時是原本的出場價；突破加碼後，拉到累計均價（進位到可下單價），損益只剩手續費與證交稅
    out = []
    for k, (lab, price, _) in enumerate(rows):
        sh_k, cost_k = sum(shares[:k + 1]), sum(costs[:k + 1])
        if shares[k] <= 0:
            continue
        exit_p = p["stop1"] if k == 0 else S.tick(cost_k / sh_k, "ceil")
        pnl = sh_k * exit_p - cost_k - cost_k * cfg.FEE_RT
        out.append((lab, px(price), f"{shares[k]:,}", px(avgs[k]), px(exit_p), f"{pnl:+,.0f}"))
    show_table(["階段", "進場價", "股數", "均價", "出場價", "損益"], out)
    st.caption(L(f"② 突破＝突破價　③ 加碼＝突破價 +{(cfg.EXT_RATIO - 1) * 100:.0f}%",
                 "均價、出場價、損益＝買到該階段後的累計狀況",
                 f"合計投入 {sum(costs):,.0f} 元、{sum(shares):,} 股",
                 f"可用資金 {capital:,.0f} 元，剩餘 {capital - sum(costs):,.0f} 元"))
    st.caption(L("買進第 2 筆後，出場價拉到累計均價",
                 f"損益只剩手續費與證交稅（來回約 {cfg.FEE_RT * 100:.1f}%）",
                 "若跳空跌破出場價，實際虧損會比表上多"))

    st.markdown(f"🎯 **目標價　{px(p['targets'][0][1])}**（距現價 {gap(p['targets'][0][1])}）")
    if p.get("stop1_note"):
        st.caption(L("第 1 筆出場依據：", brk(p["stop1_note"])))
    if len(p["targets"]) > 1:
        st.caption(L(*[f"{lbl} {px(v)}" for lbl, v in p["targets"]]))
    for n in p.get("notes", []):
        st.caption(brk(n))


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
        st.caption(L("黃色底＝近 3 日　虛線＝止跌確認的門檻", "成交金額採用證交所數據，看相對比例"))
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
    st.session_state["query"] = item["code"]      # 同步換掉查詢欄，避免之後按「開始診斷」又跳回舊的
    st.session_state["_scroll_diag"] = True


def rec_buttons(prefix, strategy, items, limit=15):
    for it in items[:limit]:
        arrow = "↑" if it.get("broke") else ""
        vs = {"壓": "　🔴壓", "撐": "　🟢撐"}.get(it.get("vl_side"), "")
        if st.button(f"{it['code']} {it['name']}{arrow}　現價 {px(it['close'])}｜突破價 {px(it['key'])}{vs}",
                     key=f"{prefix}_{strategy}_{it['code']}", use_container_width=True):
            pick(it)
            st.rerun()      # 清單在可收合區塊（fragment）裡，要整頁重跑才會帶入下方診斷並捲動
    st.caption(L("🔴壓＝近 30 日最大量當天最低點在現價之上（短線壓力）", "🟢撐＝在現價之下（支撐、出場參考）"))
    if len(items) > limit:
        st.caption(f"還有 {len(items) - limit} 檔未列出")


st.divider()
st.subheader("🌟 今日推薦")
scan = get_scan()
if not scan:
    st.info(L("還沒有夜間掃描資料", "每個交易日 22:30 掃描完成後，這裡就會出現推薦"))
else:
    hits, active = scan["hits"], scan["active"]
    st.caption(L(f"資料日 {scan['data_date']}｜夜間掃描 {scan['generated_at']}",
                 "依成交金額由大到小｜點股票會帶入下方診斷"))
    if active:
        names_on = "、".join(cfg.STRATEGY_NAMES[a] for a in active)
        (st.warning if scan["risk"] else st.success)(
            L(f"大盤第 {scan['layer']} 層・{scan['layer_name']}", f"啟用：{names_on}" + ("（方向未明，請縮小部位）" if scan["risk"] else "")))
        for name in ORDER:
            if name in active:
                items = hits.get(name, [])
                def rec_body(name=name, items=items):
                    if items:
                        rec_buttons("rec", name, items)
                    else:
                        st.caption("今天沒有符合的股票。")
                foldable(f"recfold_{name}", f"{cfg.STRATEGY_NAMES[name]}｜{len(items)} 檔", rec_body, default=bool(items))
    else:
        st.error(L("🔴 尚未止跌，不建立多頭部位", f"已讀取 {scan['data_date']} 夜間掃描：今日無推薦"))

    watch = [(n, hits.get(n, [])) for n in ORDER if n not in active and hits.get(n)]
    n_active = sum(len(hits.get(a_, [])) for a_ in active)
    if active and n_active == 0:
        st.info(L(f"✅ 已讀取 {scan['data_date']} 夜間掃描", "今日無推薦：啟用的策略都沒有符合的股票"))
    if not watch:
        st.caption("👀 觀察名單：無。" if not active else "")
    if watch:
        st.markdown(L("**👀 觀察名單**", "型態符合，但大盤目前不適用，僅供追蹤"))
        for name, items in watch:
            foldable(f"obsfold_{name}", f"{cfg.STRATEGY_NAMES[name]}｜{len(items)} 檔",
                     lambda name=name, items=items: rec_buttons("obs", name, items))

# ==================== 資金比例 ====================
st.divider()
st.subheader("⚙️ 資金與三段投入比例")
capital = st.number_input("可用資金（元）", min_value=10000, max_value=100_000_000, value=1_000_000,
                          step=50000, key="capital",
                          help="可以直接輸入金額，或按 + / − 調整，每次 5 萬")
st.caption(L("可直接輸入金額，或按 ＋／－ 調整（每次 5 萬）", "選好標的後，各策略會依這筆資金", "換算每一筆可買的股數與出場損益"))
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
st.caption(L("比例會影響「創高拉回」的進場區間", "資金用來試算每一筆可以買幾股"))

# ==================== 個股診斷 ====================
st.divider()
st.subheader("🔍 個股診斷")
if st.session_state.pop("_scroll_diag", False):
    # 每次內容都不同（加時間戳），否則 Streamlit 會沿用上次的 iframe，script 不會再執行
    components.html(f"""<script>
    // {time.time()}
    setTimeout(function () {{
      for (const h of window.parent.document.querySelectorAll('h1,h2,h3')) {{
        if (h.innerText.indexOf('個股診斷') >= 0) {{ h.scrollIntoView({{block: 'start'}}); break; }}
      }}
    }}, 150);
    </script>""", height=0)
try:
    directory = load_directory()
except Exception as e:
    directory = None
    st.warning(L("官方股票清單暫時抓不到", "目前只能用代號查詢"))

st.session_state.setdefault("query", "台積電")
query = st.text_input("輸入股票代號或中文名稱", key="query", help="例：2330、台積電").strip()
choice = None
matches = lookup.resolve(query, directory) if (directory is not None and query) else []
if len(matches) == 1:
    choice = matches[0]
elif len(matches) > 1:
    labels = [f"{m['code']} {m['name']}（{m['industry'] or m['market']}）" for m in matches]
    sel = st.selectbox("找到多檔，請選擇", labels)
    choice = matches[labels.index(sel)]
elif query:
    q = re.sub(r"\.(TWO|TW)$", "", query.upper())
    if re.fullmatch(r"\d{4,6}", q):
        choice = {"code": q, "name": "", "industry": "", "suffix": None, "market": ""}
        st.caption(L("官方清單查不到這個代號", "（可能是 ETF 或特別股），仍會嘗試抓行情"))
    else:
        st.warning(L("找不到符合的股票", "請確認名稱，或改用 4 碼代號"))

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
        st.error(L(f"查無代號【{code}】的歷史資料", "請確認是否為有效掛牌標的"))
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
            {"go": st.success, "watch": st.warning, "stop": st.error, "wait": st.info}[level](brk(text))
            st.plotly_chart(plot_k_vol(df, is_market=False), use_container_width=True,
                            config={"displayModeBar": False})

            vi = vol_low_info(df)
            if vi:
                m_ = vi["main"]
                head = f"**近 30 日最大量　{m_['key']}（{m_['vol']:,.0f} 張）**"
                if m_["pressure"]:
                    st.markdown(L(head, f"當天最低點 {px(m_['low'])}，高於現價 {px(close)}",
                                  "🔴 很可能是短線壓力，不利上攻"))
                    sp = vi["sup"]
                    if sp:
                        st.markdown(L(f"**🟢 短線支撐　{px(sp['low'])}**（{(sp['low'] / close - 1) * 100:+.1f}%）",
                                      f"近 60 日、最低點在現價之下的最大量日 {sp['key']}",
                                      f"當天最低點 {px(sp['low'])}，可作支撐或出場價的參考"))
                    else:
                        st.caption("近 60 日沒有最低點在現價之下的大量日，下方沒有明確支撐")
                else:
                    st.markdown(L(head, f"當天最低點 {px(m_['low'])}，低於現價 {px(close)}",
                                  "🟢 可作為短期支撐，或出場價的參考"))
            st.caption(L(f"以下依可用資金 {capital:,.0f} 元", f"比例 {w1}/{w2}/{w3} 換算股數與出場損益"))
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
                        st.caption(L(f"前高 {px(r['key'])}（依你的比例 {w1}/{w2}/{w3} 推算）",
                                     f"上緣：第 1 筆買在這個價、第 2 筆買在前高時，",
                                     f"前兩筆平均成本剛好是前高的 {cfg.PB_MAX_AVG_RATIO * 100:.0f}%",
                                     f"下緣：前高回檔 {cfg.PB_PULLBACK_MAX * 100:.0f}%，再深就視為轉弱"))
                        if r["in_zone"]:
                            st.caption("現價已在進場區內，以現價為第 1 筆試算")
                        elif r["close"] > r["zone_hi"]:
                            st.caption(L("現價還沒回檔到進場區", f"以下假設第 1 筆掛在 {px(r['zone_hi'])} 試算"))
                        else:
                            st.caption(L("回檔已超過上限，規則上不進場", "以下仍以進場區上緣試算供參考"))
                    if r["ok"] and not r["active"]:
                        st.warning(L("型態符合", "但目前大盤層級不適用這套策略"))
                    if "key" not in r or ("stop1" not in r and sname != "pullback"):
                        st.info("目前沒有符合的型態，無法試算。")
                    else:
                        plan = S.plan_for(sname, r, ratios)
                        if sname != "pullback" and plan["stop1"] >= plan["e"]:
                            st.info("現價已低於出場價，無法試算。")
                        else:
                            if not r["ok"]:
                                st.warning(L("尚未符合條件", "以下試算僅供參考"))
                            show_plan(plan, capital, close)

                foldable(f"{code}_{sname}", label, body, default=r["ok"],
                         status=f"{'✅ 符合' if r['ok'] else '❌ 尚未符合'}｜{applicable}")

st.caption("最後更新：2026 年 10 月")
