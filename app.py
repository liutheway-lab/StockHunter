"""app.py — 台股盤後戰情室（Streamlit 網頁）
功能：大盤位階（五層＋止跌確認）、個股診斷（四套策略＋分批計畫）。
資料口徑跟夜間掃描完全相同（data.py、strategies.py 共用）。
"""
import html
import os
import time
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
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
import backtest
import chart_png
import importlib
try:                                                    # 重大事件日曆；沒上傳 events.py 時只是不顯示，不影響其他功能
    import events
except Exception:
    events = None

# Streamlit 會把 config.py、strategies.py 這些檔案留在記憶體裡；
# 只換檔案沒有 Reboot 時可能還在用舊版，所以每次都重新讀一次（很快，不影響速度）
for _m in (cfg, lookup, data, S, backtest, guide, chart_png) + ((events,) if events else ()):
    importlib.reload(_m)

st.set_page_config(page_title="台股盤後戰情室", page_icon="📈", layout="wide")

# 載入中的小卡片：第一次進來、資料還在更新時顯示；整頁跑完會自動拿掉（之後切換功能不再出現）
_SPLASH = """
<style>
@keyframes sh-l{0%{opacity:0;transform:translateY(3px)}20%{opacity:1;transform:translateY(-3px)}55%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(0)}}
@keyframes sh-r{0%{opacity:0;transform:translateY(-3px)}20%{opacity:1;transform:translateY(3px)}55%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(0)}}
@keyframes sh-blink{0%,100%{opacity:.2}50%{opacity:1}}
.sh-card{margin:18px auto;max-width:480px;text-align:center;padding:1.6rem 1rem 1.3rem;border-radius:24px;
  background:linear-gradient(135deg,rgba(255,200,226,.32),rgba(180,226,255,.32));border:1.5px dashed rgba(128,128,128,.35)}
.sh-track{font-size:1.9rem;display:flex;justify-content:center;gap:.9rem;height:2.4rem;align-items:center}
.sh-track span{display:inline-block;opacity:0;animation:sh-l 2.4s ease-in-out infinite}
.sh-track span:nth-child(even){animation-name:sh-r}
.sh-track span:nth-child(1){animation-delay:0s}
.sh-track span:nth-child(2){animation-delay:.4s}
.sh-track span:nth-child(3){animation-delay:.8s}
.sh-track span:nth-child(4){animation-delay:1.2s}
.sh-track span:nth-child(5){animation-delay:1.6s}
.sh-t{font-weight:700;font-size:1.15rem;margin:.9rem 0 .25rem}
.sh-dots i{font-style:normal;animation:sh-blink 1.2s infinite;margin-left:2px}
.sh-dots i:nth-child(2){animation-delay:.2s}
.sh-dots i:nth-child(3){animation-delay:.4s}
.sh-s{opacity:.7;font-size:.92rem}
</style>
<div class="sh-card">
  <div class="sh-track"><span>🐾</span><span>🐾</span><span>🐾</span><span>🐾</span><span>🐾</span></div>
  <div class="sh-t">獵人正在狩獵股票中<span class="sh-dots"><i>.</i><i>.</i><i>.</i></span></div>
  <div class="sh-s">請稍等一下，獲取獵物後轉入主頁面 🏹</div>
</div>
"""
_splash = st.empty()
if not st.session_state.get("_booted"):
    _splash.markdown(_SPLASH, unsafe_allow_html=True)

st.markdown("""<style>
@media (max-width: 640px) {       /* 手機：表格字小一點、間距縮小，第一欄可以換行，不要被切掉 */
  table.ht th, table.ht td { padding: 5px 3px !important; font-size: 0.72rem !important; }
  table.ht th { white-space: normal !important; }
  table.ht td:first-child, table.ht th:first-child { white-space: normal !important; }
}
.cf { margin: 0.7rem 0 0.4rem 0; }
.cf .cd { opacity: 0.65; font-size: 0.875rem; line-height: 1.6; margin-top: 0.2rem; }
.p { display: block; }
@media (min-width: 1000px) {     /* 電腦：一個句子不換行，句子之間用 ｜ 隔開，排在同一行 */
  .cf .p, table.ht .p { display: inline; white-space: nowrap; }
  .cf .p:not(:last-child)::after, table.ht .p:not(:last-child)::after { content: "｜"; margin: 0 0.7em; opacity: 0.4; }
}
@media (min-width: 1000px) { .block-container { max-width: 1500px; padding-left: 3rem; padding-right: 3rem; } }
</style>""", unsafe_allow_html=True)
# 檢查其他檔案是不是也更新了（只換了 app.py、沒換 config.py／strategies.py 時，給清楚的提示）
_missing = [f for f, ok in (("config.py", hasattr(cfg, "names_join")),
                            ("strategies.py", "split" in S.attack_plan.__code__.co_varnames),
                            ("backtest.py", "entry_mode" in backtest.run_stock.__code__.co_varnames),
                            ("guide.py", "強勢續攻" in open(guide.__file__, encoding="utf-8").read()),
                            ("chart_png.py", hasattr(chart_png, "_place_label")),
                            ("data.py", hasattr(data, "patch_with_twse"))) if not ok]
if _missing:
    st.error("⚠️ GitHub 上這幾個檔案還是舊版，請重新上傳：" + "、".join(_missing))
    st.stop()
st.markdown('<h1 style="white-space:nowrap;font-size:clamp(1.3rem,6vw,2.4rem);margin:0.2em 0">📈 台股盤後戰情室</h1>', unsafe_allow_html=True)
try:                                                    # 程式更新時間＝app.py 這個檔案最後一次更新（重新部署）的時間
    _upd = datetime.fromtimestamp(os.path.getmtime(__file__), ZoneInfo("Asia/Taipei"))
    _upd_txt = f"｜程式更新 {_upd:%m-%d %H:%M}"
except Exception:
    _upd_txt = ""
st.caption(f"台灣時間 {datetime.now(ZoneInfo('Asia/Taipei')):%Y-%m-%d %H:%M}{_upd_txt}")

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


VOL_DAYS = 65       # 看近幾個交易日的成交量排名（跟圖上看得到的 65 個交易日一致）
VOL_SUP_RANK = 5    # 找短線支撐時，最多往下找到第幾大量（再後面的量太小，不具參考價值）


def rank_name(r):
    return f"第{r}大量"


def vol_low_info(df):
    """近 65 個交易日依成交量排名。main＝量最大那天；若它的最高點比現價高（壓力，標最高點），
    sup＝排名最前、而且最低點在現價之下的那天（短線支撐，標示它是第幾大量）"""
    d = df[df["Volume"] > 0].iloc[-VOL_DAYS:]
    if d.empty:
        return None
    close = float(df["Close"].iloc[-1])
    order = list(d["Volume"].sort_values(ascending=False).index)

    def pack(day, rank):
        lo_, hi_ = float(d.loc[day, "Low"]), float(d.loc[day, "High"])
        return {"key": f"{day:%m/%d}", "low": lo_, "high": hi_, "level": lo_, "vol": float(d.loc[day, "Volume"]) / 1000.0,
                "rank": rank, "name": rank_name(rank)}

    main = pack(order[0], 1)
    main["pressure"] = main["high"] > close          # 壓力看最高點、支撐看最低點
    if main["pressure"]:
        main["level"] = main["high"]
    sup = None
    if main["pressure"]:
        for r, day in enumerate(order[1:VOL_SUP_RANK], start=2):
            if float(d.loc[day, "Low"]) < close:
                sup = pack(day, r)
                break
    return {"main": main, "sup": sup, "close": close}


RED, GREEN = "#f03e3e", "#37b24d"      # 跟大盤燈號邏輯一致：紅＝壓力／風險，綠＝支撐


def soft(color, a=0.5):
    """箭頭用：同一個顏色但半透明，線條比較柔和，不會搶過 K 線"""
    if color.startswith("#") and len(color) == 7:
        r, g, b = (int(color[k:k + 2], 16) for k in (1, 3, 5))
        return f"rgba({r},{g},{b},{a})"
    return color


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
                                   arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=soft(color),
                                   ax=ax, ay=ay, xanchor=xanchor, yanchor=yanchor, font=dict(color=color, size=11))

        # 前低：細虛線橫跨整張圖 + 箭頭，文字放在 K 線下方的空白處
        fig.add_hline(y=stab["prior_low"], row=1, col=1,
                      line=dict(color="rgba(0,168,214,0.8)", width=1, dash="dot"))
        mark(f"{stab['prior_low_date']:%m/%d}", stab["prior_low"],
             f"近 {cfg.STAB_DAYS} 日最低點 {stab['prior_low']:,.0f}（{stab['prior_low_date']:%m/%d}）", "#00a8d6", -4, 62)
        if stab.get("recent_low") is not None and stab.get("recent_low_date") is not None:
            mark(f"{stab['recent_low_date']:%m/%d}", stab["recent_low"],
                 f"低點日後最低 {stab['recent_low']:,.0f}（{stab['recent_low_date']:%m/%d}）", "#b388ff", -4, 108)

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
                # 價格圖上也把爆量日那根 K 線框起來
                lo_k, hi_k = float(p["Low"].iloc[i]), float(p["High"].iloc[i])
                pad_k = (hi_k - lo_k) * 0.15 + 1e-9
                fig.add_shape(type="rect", x0=i - 0.55, x1=i + 0.55, y0=lo_k - pad_k, y1=hi_k + pad_k, row=1, col=1,
                              line=dict(color="#ff8c00", width=1.5), fillcolor="rgba(0,0,0,0)", layer="above")
                # 文字往左移開，用細箭頭指到橘框，避免跟右邊「近3日最大量」重疊
                mark(dkey, dv_b, f"爆量日 {dkey}", "#ff8c00", -70, -6, row=2)
            if rv:
                # 兩行短字放在右側長條上方（虛線以上的空白），只往左伸展，不碰到「爆量日」文字
                mark(f"{stab.get('vol_date', stab['prior_low_date']):%m/%d}", rv / 1e8, f"低點日量<br>約為爆量日 {rv / dv * 100:.0f}%",
                     "#00a8d6", -4, -40, row=2, yanchor="bottom")

    # ---- 前高 + 近期大量低點（最接近現價、成交量最大與次大的兩天，標出當天最低點）----
    hi_i = int(p["High"].to_numpy().argmax())
    hi_v = float(p["High"].iloc[hi_i])
    last_c = float(p["Close"].iloc[-1])
    n_bar = len(dates)

    def side(i):
        return "right" if i > n_bar - 9 else ("left" if i < 8 else "center")

    fig.add_hline(y=hi_v, row=1, col=1, line=dict(color="rgba(230,73,128,0.6)", width=1, dash="dot"))
    fig.add_annotation(x=dates[hi_i], y=hi_v, row=1, col=1, text=f"前高 {px(hi_v)}（{dates[hi_i]}）",
                       showarrow=True, arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=soft("#e64980"),
                       ax=-4 if side(hi_i) == "right" else 4, ay=-22, xanchor=side(hi_i),
                       font=dict(color="#e64980", size=12))
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
                   f"{main['name']}（{main['key']}）<br>{'最高點' if main['pressure'] else '最低點'} {px(main['level'])}")]
        if sup:
            labels.append((sup, GREEN, f"{sup['name']}（{sup['key']}）<br>最低點 {px(sup['low'])}"))
        # 那天太久以前、不在這張圖的範圍內：只畫一條同色點線，文字放在線的左上方（寫明日期）
        far = [(it, c, t) for it, c, t in labels if it["key"] not in dates]
        labels = [(it, c, t) for it, c, t in labels if it["key"] in dates]
        for it, c, t in far:
            fig.add_hline(y=it["level"], row=1, col=1, line=dict(color=c, width=1, dash="dot"))
            fig.add_annotation(x=dates[0], y=it["level"], row=1, col=1, text=t.replace("<br>", "　"), showarrow=False,
                               xanchor="left", yanchor="bottom", font=dict(color=c, size=11))
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
            fig.add_annotation(x=it["key"], y=it["level"], row=1, col=1, text=text,
                               showarrow=True, arrowhead=2, arrowsize=0.8, arrowwidth=1, arrowcolor=soft(color),
                               ax={"right": -4, "left": 4, "center": 0}[anc], ay=max(18.0, (it["level"] - text_y) * ppu),
                               xanchor=anc, font=dict(color=color, size=12))
            fig.add_hline(y=it["level"], row=1, col=1, line=dict(color=color, width=1, dash="dot"))

    fig.update_layout(
        xaxis_rangeslider_visible=False, margin=dict(l=5, r=5, t=35, b=5), height=440,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=10)),
        template="plotly_dark", dragmode=False, hovermode=False,
    )
    fig.update_xaxes(fixedrange=True)
    fig.update_yaxes(fixedrange=True)
    fig.update_xaxes(type="category", tickangle=-45, showgrid=False, nticks=10, range=[-0.5, len(dates) - 0.5])
    fig.update_yaxes(side="right", gridcolor="rgba(255,255,255,0.08)")
    fig.update_yaxes(title_text=vol_label, title_font_size=10, row=2, col=1)
    return fig


# ---------- 資料（快取，避免每次操作都重抓） ----------
@st.cache_data(ttl=600, show_spinner=False)
def load_market():
    return data.fetch_market()


@st.cache_data(ttl=600, show_spinner=False)
def load_twse_stock_vol(code):
    """證交所個股每日成交股數（上市）：跟券商看盤同一份數字"""
    return data.fetch_twse_stock_volume(code, months=4)


@st.cache_data(ttl=600, show_spinner=False)
def load_stock(code, suffix):
    # auto_adjust=False：價格不還原股利，跟看盤軟體、夜間掃描一致
    return yf.Ticker(code + suffix).history(period="1y", auto_adjust=False)


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def load_stock_long(code, suffix):
    """回測用：抓 8 年（涵蓋 2020 疫情崩盤，前幾個月給均線暖身）"""
    return data._clean(yf.Ticker(code + suffix).history(period="8y", auto_adjust=False))


def _twse_month(y, m):
    """證交所某個月的每日成交金額（元）"""
    import requests
    url = f"https://www.twse.com.tw/exchangeReport/FMTQIK?response=json&date={y}{m:02d}01"
    out = {}
    try:
        j = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).json()
        if j.get("stat") == "OK" and j.get("data"):
            col = j["fields"].index("成交金額")
            for row in j["data"]:
                yy, mm, dd = (int(x) for x in row[0].strip().split("/"))
                out[pd.Timestamp(yy + 1911, mm, dd)] = float(str(row[col]).replace(",", ""))
    except Exception:
        pass
    return out


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False)
def load_market_hist():
    """回測用的大盤：8 年加權指數 + 每天的層級與啟用策略。
    成交金額只在空頭層級時才需要（止跌確認），所以只抓那些月份，省時間"""
    import backtest as BT
    mkt = data._clean(yf.download("^TWII", period="8y", interval="1d", progress=False, auto_adjust=False))
    layers = S.layer_series(mkt)
    months = set()
    for d in mkt.index[layers.isin([4, 5]).to_numpy()]:
        for back in (0, 1, 2, 3):                    # 止跌確認要往回看約 41 個交易日（爆量日 30 天＋低點日最多 10 天）
            p_ = (d - pd.DateOffset(months=back))
            months.add((p_.year, p_.month))
    vol = {}
    for y, m in sorted(months):
        vol.update(_twse_month(y, m))
        time.sleep(0.3)
    mkt["Volume"] = pd.Series(vol).reindex(mkt.index) if vol else float("nan")
    return mkt, BT.market_states(mkt)


@st.cache_data(ttl=60 * 60 * 12, show_spinner=False, max_entries=50)
def stock_backtest(code, suffix, ratios, mode, only_active, entry_mode="chase"):
    import backtest as BT
    raw = load_stock_long(code, suffix)
    mkt, mstate = load_market_hist()
    if raw is None or len(raw) < 120:
        return None
    return BT.run_stock(raw, mkt, mstate, ratios, mode=mode, only_active=only_active, entry_mode=entry_mode)


@st.cache_data(ttl=60 * 60 * 24, show_spinner="載入證交所官方股票清單...")
def load_directory():
    return lookup.load_directory()


def px(x):
    """依台股跳動單位決定小數位數：1000 元以上不顯示小數、500～1000 元整數、100～500 元一位…"""
    t = S._tick_size(x)
    d = 0 if t >= 1 else (1 if t in (0.5, 0.1) else 2)
    t_ = f"{x:,.{d}f}"
    return t_.rstrip("0").rstrip(".") if "." in t_ else t_      # 119.0 → 119、114.50 → 114.5


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
        if i in wrap and "\n" in str(text):                 # 一個句子一個 span：手機一句一行，電腦排在同一行
            inner = "".join(f'<span class="p">{esc(x)}</span>' for x in str(text).split("\n"))
        else:
            inner = esc(text).replace(chr(10), "<br>")
        align = "left" if i == 0 or i in wrap or i in left else "right"
        ws = "normal" if i in wrap else "nowrap"
        bg = "background:rgba(128,128,128,0.14);font-weight:600;" if tag == "th" else ""
        bt = f"border-top:{border};" if top else ""
        return (f'<{tag} style="padding:6px 7px;text-align:{align};white-space:{ws};{bg}{bt}'
                f'font-size:0.84rem;line-height:1.35;">{inner}</{tag}>')

    out = ['<div style="overflow-x:auto;margin:4px 0 10px 0;"><table class="ht" style="width:100%;'
           f'border-collapse:separate;border-spacing:0;border:{border};border-radius:8px;overflow:hidden;">']
    out.append("<tr>" + "".join(cell("th", i, h, False) for i, h in enumerate(head)) + "</tr>")
    for r in rows:
        out.append("<tr>" + "".join(cell("td", i, c, True) for i, c in enumerate(r)) + "</tr>")
    out.append("</table></div>")
    return "".join(out)


def show_table(head, rows, **kw):
    st.markdown(html_table(head, rows, **kw), unsafe_allow_html=True)


def split_sents(text):
    """用 ；，、 切成一個一個句子；括號（）裡面的不切，才不會從括號中間斷開"""
    out, buf, depth = [], "", 0
    for ch in str(text):
        if ch in "（(":
            depth += 1
        elif ch in "）)":
            depth = max(0, depth - 1)
        if ch in "；，、" and depth == 0:
            out.append(buf)
            buf = ""
        else:
            buf += ch
    out.append(buf)
    return [x.strip() for x in out if x.strip()]


def md_cond(rows):
    """條件表（結果 | 條件 | 現況）。現況欄可以換行，其他欄不換行"""
    rows = [(a, b, "\n".join(split_sents(c))) for a, b, c in rows]      # 一個句子一行，不會從句子中間斷開
    show_table(["", "條件", "現況"], rows, wrap=(2,), left=(1,))


def md_fail(rows):
    """沒通過的條件：一條一組，名稱一行、現況一個句子一行，用整個畫面寬度，不會從句子中間斷開"""
    import re as _re
    for name, detail in rows:
        parts = split_sents(detail)
        inner = "".join(f'<span class="p">{html.escape(x)}</span>' for x in parts)
        st.markdown(f'<div class="cf"><div class="cn">❌ <b>{html.escape(str(name))}</b></div>'
                    f'<div class="cd">{inner}</div></div>', unsafe_allow_html=True)


def plan_view(p, capital, close):
    """分批計畫：價位、距現價、均價、投入、股數、各階段停損與損益"""
    r = p["ratios"]
    rows = p.get("tranches") or [("① 進場", p["e"], r[0]), ("② 突破", p["b"], r[1]), ("③ 加碼", p["b2"], r[2])]
    n_tr = len(rows)
    gap = lambda price: f"{(price / close - 1) * 100:+.1f}%"
    st.markdown(f"**預期第 1 筆進場 {px(p['e'])}**　（現價 {px(close)}，差 {gap(p['e'])}）")

    shares, costs = [], []
    for _, price, ratio in rows:
        sh = int(capital * ratio // price) if price > 0 else 0
        shares.append(sh)
        costs.append(sh * price)
    avgs = []
    for k in range(n_tr):
        sh, cost = sum(shares[:k + 1]), sum(costs[:k + 1])
        avgs.append(S.tick(cost / sh) if sh > 0 else p["e"])

    # 一張表看完：每一筆的進場價、股數、累計均價、出場價、出場損益
    # 出場價：只買第 1 筆時是原本的出場價；突破加碼後，拉到累計均價（進位到可下單價），損益只剩手續費與證交稅
    out = []
    for k, (lab, price, _) in enumerate(rows):
        sh_k, cost_k = sum(shares[:k + 1]), sum(costs[:k + 1])
        if shares[k] <= 0:
            continue
        if k == 0:
            exit_p = p["stop1"]
        elif p.get("stop_after2"):
            exit_p = p["stop_after2"]                     # 攻擊日策略：突破日買進後，出場價上移到前波高點下 5%
        else:
            exit_p = S.tick(cost_k / sh_k, "ceil")
        pnl = sh_k * exit_p - cost_k - cost_k * cfg.FEE_RT
        out.append((lab, px(price), f"{shares[k]:,}", px(avgs[k]), px(exit_p), f"{pnl:+,.0f}"))
    show_table(["階段", "進場價", "股數", "均價", "出場價", "損益"], out)
    head_note = (f"① 攻擊日＝漲 {cfg.CHASE_GAIN * 100:.0f}% 且突破盤整高點時追進　② 突破日＝站上前波高點" if p.get("stop_after2")
                 else f"② 突破＝突破價　③ 加碼＝突破價 +{(cfg.EXT_RATIO - 1) * 100:.0f}%")
    st.caption(L(head_note,
                 "均價、出場價、損益＝買到該階段後的累計狀況",
                 f"合計投入 {sum(costs):,.0f} 元、{sum(shares):,} 股",
                 f"投入資金 {capital:,.0f} 元，剩餘 {capital - sum(costs):,.0f} 元"))
    if p.get("stop_after2"):
        st.caption(L(f"突破日買進後，出場價上移到前波高點下 {cfg.ATTACK_STOP2_PCT * 100:.0f}%",
                     "若跳空跌破出場價，實際虧損會比表上多"))
    else:
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
    if mk["active"]:                                   # 大盤結論放在標題正下方，一進來就先看到
        (st.warning if mk["risk"] else st.success)(
            "大盤目前允許：" + cfg.names_join(mk["active"]))
    else:
        st.error("🔴 尚未止跌，不建議建立多頭部位")
    try:                                               # 資料日比預期的舊：提醒一聲（休市日除外，所以只是提醒）
        _now = datetime.now(ZoneInfo("Asia/Taipei"))
        _exp = _now.date() - (timedelta(days=1) if _now.hour < 14 else timedelta(0))      # 14:00 前今天還沒有收盤資料
        while _exp.weekday() >= 5:
            _exp -= timedelta(days=1)
        _last = mk["date"].date()
        if _last < _exp:
            st.warning(L(f"大盤資料只到 {_last:%m/%d}，應該要有 {_exp:%m/%d}",
                         "如果那天不是休市，代表資料來源還沒更新",
                         *(str(mkt_df.attrs.get("twse_note", "")).split("\n") if mkt_df.attrs.get("twse_note")
                           else ["（沒有補資料的紀錄，data.py 可能還是舊版）"])))
    except Exception:
        pass
    mL, mR = st.columns([2, 3])          # 電腦：左邊數字和止跌條件、右邊圖；手機會自動變成上下排
    with mL:
        _pv = float(mk["df"]["Close"].iloc[-2]) if len(mk["df"]) >= 2 else None
        _d = mk["close"] - _pv if _pv else 0.0                    # 跟圖左上角同一種寫法：現價　▼ 漲跌點（幅度），紅漲綠跌
        _col = "#e04a4a" if _d > 0 else "#2e9e4f" if _d < 0 else "#555"
        _ar = "▲" if _d > 0 else "▼" if _d < 0 else "─"
        _tail = f"　{_ar} {abs(_d):,.2f}（{_d / _pv * 100:+.2f}%）" if _pv else ""
        st.markdown(f"<div style='font-size:0.875rem;margin-bottom:0.2rem'>加權指數</div>"
                    f"<div style='color:{_col};font-weight:700;font-size:1.45rem'>"
                    f"{mk['close']:,.2f}{_tail}</div>", unsafe_allow_html=True)
        st.metric(f"大盤層級（第 {mk['layer']} 層）", f"{LAYER_ICON[mk['layer']]} {mk['layer_name']}")

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
                da, nd = s_["days_after"], s_["need_days"]
                state = "✅ 通過" if s_["ok"] else ("⏳ 觀察中" if s_.get("watching") else "🔴 尚未止跌")
                st.markdown(L(f"🧱 **止跌確認**　{state}", "量能、K 棒、不破低　都要通過"))
                low_txt = ("低點日就是今天，還要再觀察" if da == 0 else
                           f"低點日（{s_['signal_date']:%m/%d}）最低 {s_['prior_low']:,.0f}，"
                           f"之後 {da} 天最低 {s_['recent_low']:,.0f}，要保持不破")
                md_cond([
                    (ok(s_["vol_ok"]), "量能",
                     f"爆量下跌日 {s_['dump_date']:%m/%d}；低點日成交額約為它的 {ratio:.0f}%，需 100% 以上"),
                    (ok(s_["shape_ok"]), "K 棒",
                     f"下影線占當天區間 {s_['shadow'] * 100:.0f}%（需 {cfg.STAB_SHADOW * 100:.0f}% 以上），"
                     f"或收紅且實體占 {s_['body'] * 100:.0f}%（需 {cfg.STAB_RED_BODY * 100:.0f}% 以上）"),
                    ("⏳" if (da == 0) else ok(s_["no_break"]), "不破低", low_txt),
                ])
                if s_["ok"]:
                    st.success("📌 日線止跌走穩，先以 2～3 成資金佈局，不猜底、不貪心")

    with mR:
        try:                                           # 跟 Telegram 用同一張圖（chart_png.py），兩邊永遠一樣
            import chart_png
            st.image(chart_png.market_chart(mk), use_container_width=True)
        except Exception as e:                         # 萬一畫圖失敗，退回互動圖（並寫出原因，方便排查）
            st.caption(f"大盤圖產生失敗：{e!r}")
            st.plotly_chart(plot_k_vol(mk["df"], is_market=True, stab=show_stab),
                            use_container_width=True, config={"displayModeBar": False, "staticPlot": True})

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


def _sub(text):
    """按鈕下方的小字（現價、距離等）"""
    st.markdown(f'<div style="text-align:center;opacity:.65;font-size:.85rem;margin:-4px 0 6px;">{text}</div>',
                unsafe_allow_html=True)


def rec_buttons(prefix, strategy, items, limit=None):
    """按鈕上直接寫追價與出場價（手機一行看完）；現價與壓撐放在下方小字"""
    for it in items[:limit]:
        arrow = "↑" if it.get("broke") else ""
        vs = {"壓": "🔴壓", "撐": "🟢撐"}.get(it.get("vl_side"), "")
        if st.button(f"{it['code']} {it['name']}{arrow}　追價 {px(it['key'])}｜出場 {px(it['stop1'])}",
                     key=f"{prefix}_{strategy}_{it['code']}", use_container_width=True):
            pick(it)
            st.rerun()      # 清單在可收合區塊（fragment）裡，要整頁重跑才會帶入下方診斷並捲動
        _sub(f"現價 {px(it['close'])}" + (f"｜{vs}" if vs else ""))
    st.caption(L("追價＝盤中漲到這個價就是追價點　🔴壓＝近 65 交易日第 1 大量當天最高點在現價之上（短線壓力）",
                 "🟢撐＝最低點在現價之下（支撐、出場參考）"))


def absorb_buttons(items, limit=None):
    for it in items[:limit]:
        if st.button(f"{it['code']} {it['name']}", key=f"absorb_{it['code']}", use_container_width=True):
            pick(it)
            st.rerun()
        st.markdown(f'<div style="text-align:center;opacity:.65;font-size:.85rem;margin:-4px 0 6px;">'
                    f'現價 {px(it["close"])}｜今日最低 {px(it["low"])}｜{it["kind"]}｜量能 {it["vol_mult"]:.1f} 倍{"｜13:00 已預判" if it.get("pre") else ""}</div>',
                    unsafe_allow_html=True)
    st.markdown('<div style="height:14px"></div>', unsafe_allow_html=True)      # 跟上面的股票空一行
    st.caption(L("量能＝今天量 ÷ 20 日均量", "隔天不破發現日最低，才比較可信"))


def watch_buttons(name, items, limit=None):
    """明日追價名單：按鈕上寫追價與出場價，下方小字寫現價與距離（同一套格式，跟推薦清單一致）"""
    for it in items[:limit]:
        up = (it["chase"] / it["close"] - 1) * 100 if it["close"] else 0
        if st.button(f"{it['code']} {it['name']}　追價 {px(it['chase'])}｜出場 {px(it['stop1'])}",
                     key=f"watch_{name}_{it['code']}", use_container_width=True):
            pick(it)
            st.rerun()
        _sub(f"現價 {px(it['close'])}｜距追價 {up:+.1f}%")


st.divider()
st.subheader("🔔 重大事件")
if events is None:
    st.caption("需要上傳 events.py 才會顯示")
else:
    import html as _html
    st.caption(L("未來 5 個交易日｜台灣時間", "‼️＝非常重要、❗️＝重要"))
    _rows = events.event_rows()
    if not _rows:
        st.markdown("沒有列入的重大事件")
    else:
        _tr = "".join(
            f'<tr><td class="ev-d">{_html.escape(r["date"])}</td>'
            f'<td class="ev-t">{_html.escape(r["time"])}</td>'
            f'<td class="ev-m">{r["mark"]}</td>'
            f'<td class="ev-n">{_html.escape(r["name"])}</td></tr>'
            for r in _rows)
        st.markdown(
            '<style>'
            '.ev{width:100%;border-collapse:collapse;font-size:.95rem;line-height:1.35}'
            '.ev th{font-weight:500;opacity:.55;text-align:left;padding:.35rem .5rem;'
            'border-bottom:1px solid rgba(128,128,128,.35);white-space:nowrap}'
            '.ev td{padding:.5rem .5rem;border-bottom:1px solid rgba(128,128,128,.15);vertical-align:middle}'
            '.ev .ev-d{white-space:nowrap;font-weight:600;width:6.5em}'
            '.ev .ev-t{white-space:nowrap;font-variant-numeric:tabular-nums;opacity:.8;width:4.2em}'
            '.ev .ev-m{width:1.6em;text-align:center}'
            '</style>'
            '<table class="ev"><thead><tr><th>日期</th><th>時間</th><th></th><th>事件</th></tr></thead>'
            f'<tbody>{_tr}</tbody></table>',
            unsafe_allow_html=True)
    if events.stale_note():
        st.warning(events.stale_note())

st.divider()
st.subheader("🌟 今日推薦")
scan = get_scan()
if not scan:
    st.info(L("還沒有夜間掃描資料", "每個交易日晚上 22:00 左右掃描完成後，這裡就會出現推薦"))
else:
    hits, active = scan["hits"], scan["active"]
    st.caption(L(f"資料日 {scan['data_date']}｜夜間掃描 {scan['generated_at']}",
                 "越符合規則越前面｜點股票會帶入下方診斷"))
    if active:
        names_on = cfg.names_join(active)
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
        st.error(L("🔴 尚未止跌，不建議建立多頭部位", f"已讀取 {scan['data_date']} 夜間掃描：今日無推薦"))

    wl = scan.get("watch") or {}
    act_wl = [n for n in ("pullback", "flag") if n in active and wl.get(n)]       # 大盤適用的策略才叫「追價名單」
    obs_wl = [n for n in ("pullback", "flag") if n not in active and wl.get(n)]   # 不適用的改放進觀察名單
    if act_wl:
        st.markdown(L("**🎯 明日追價名單**", "盤整條件已成立，明天盤中觀察要不要追"))
        for name in act_wl:
            items = wl[name]
            foldable(f"wlfold_{name}", f"{cfg.STRATEGY_NAMES[name]}｜{len(items)} 檔",
                     lambda name=name, items=items: watch_buttons(name, items), default=True)
    watch = [(n, hits.get(n, [])) for n in ORDER if n not in active and hits.get(n)]
    n_active = sum(len(hits.get(a_, [])) for a_ in active)
    if active and n_active == 0:
        st.info(L(f"✅ 已讀取 {scan['data_date']} 夜間掃描", "今日無推薦：啟用的策略都沒有符合的股票"))
    if not watch and not obs_wl:
        st.caption("👀 觀察名單：無。" if not active else "")
    if watch or obs_wl:
        st.markdown(L("**👀 觀察名單**", "型態符合，但大盤目前不適用", "僅供追蹤，不建議進場"))
        for name, items in watch:
            foldable(f"obsfold_{name}", f"{cfg.STRATEGY_NAMES[name]}｜{len(items)} 檔",
                     lambda name=name, items=items: rec_buttons("obs", name, items))
        for name in obs_wl:                                        # 盤整條件成立、但大盤不適用的「明天可能的追價點」
            items = wl[name]
            foldable(f"obswl_{name}", f"{cfg.STRATEGY_NAMES[name]}｜明天可能的追價點｜{len(items)} 檔",
                     lambda name=name, items=items: watch_buttons(name, items))

    ab_list = scan.get("absorb") or []
    if ab_list:                                                    # 恐慌承接：只供追蹤，不給追價點
        st.markdown(L(f"**🧺 {cfg.ABSORB_NAME}**", "今天殺低後被買回來，而且有量", "前段有大漲，之後從高點急跌", "收盤守住大量支撐，只供追蹤，沒有追價點"))
        foldable("absorbfold", f"先殺後拉（長下影）帶量｜{len(ab_list)} 檔",
                 lambda: absorb_buttons(ab_list), default=False)

# ==================== 資金比例 ====================
def set_w(a, b, c):
    st.session_state.w1, st.session_state.w2, st.session_state.w3 = a, b, c


def set_fund_open(v):
    st.session_state["fund_open"] = v


_ss = st.session_state
_DEF = {"capital": 1_000_000, "w1": int(round(cfg.TRANCHES[0] * 100)), "w2": int(round(cfg.TRANCHES[1] * 100)),
        "a1": int(round(cfg.ATTACK_SPLIT[0] * 100)), "a2": int(round(cfg.ATTACK_SPLIT[1] * 100))}
_DEF["w3"] = 100 - _DEF["w1"] - _DEF["w2"]
for _k, _v in _DEF.items():
    if _k not in _ss:
        _ss[_k] = _v
    _ss[_k] = _ss[_k]          # 面板收起來時，輸入框沒畫出來，這樣寫設定才不會被 Streamlit 清掉
# 前面的比例改了，後面的上限跟著收緊，合計不會超過 100%
_ss.w1 = min(max(int(_ss.w1), 5), 90)
_ss.w2 = min(int(_ss.w2), 100 - _ss.w1)
_ss.w3 = min(int(_ss.w3), 100 - _ss.w1 - _ss.w2)
_ss.a1 = min(max(int(_ss.a1), 5), 100)
_ss.a2 = min(int(_ss.a2), 100 - _ss.a1)

st.divider()
st.markdown("**⚙️ 資金與投入比例**")
fund_open = _ss.get("fund_open", False)
if fund_open:
    st.number_input("可用資金（元）", min_value=10000, max_value=100_000_000, step=50000, key="capital",
                    help="可以直接輸入金額，或按 + / − 調整，每次 5 萬")
    st.caption("可直接輸入金額，或按 ＋／－ 調整（每次 5 萬）")
    b1, b2, b3 = st.columns(3)
    b1.button("平衡策略 30/40/30", use_container_width=True, on_click=set_w, args=(30, 40, 30))
    b2.button("突破策略 30/50/20", use_container_width=True, on_click=set_w, args=(30, 50, 20))
    b3.button("順勢策略 20/50/30", use_container_width=True, on_click=set_w, args=(20, 50, 30))
    cw1, cw2, cw3 = st.columns(3)
    cw1.number_input("第 1 筆 (%)", 5, 90, step=5, key="w1")
    cw2.number_input("第 2 筆 (%)", 0, max(0, 100 - _ss.w1), step=5, key="w2")
    cw3.number_input("第 3 筆 (%)", 0, max(0, 100 - _ss.w1 - _ss.w2), step=5, key="w3")

w1, w2, w3, a1, a2 = (int(_ss[k]) for k in ("w1", "w2", "w3", "a1", "a2"))
capital_all = float(_ss.capital)
tot = w1 + w2 + w3                                                  # 合計可以小於 100%，沒用到的留現金
capital = capital_all * tot / 100                                   # 後面所有試算都用「實際要投入的資金」
ratios = S.norm_ratios((w1, w2, w3))
atk_tot = a1 + a2
capital_atk = capital_all * atk_tot / 100
atk_split = (a1 / 100, a2 / 100)

if fund_open:
    st.markdown(L("**低位起漲、量縮抗跌　適用**",
                  f"第 1 筆　{capital * ratios[0]:,.0f} 元（{w1}%）",
                  f"第 2 筆　{capital * ratios[1]:,.0f} 元（{w2}%）",
                  f"第 3 筆　{capital * ratios[2]:,.0f} 元（{w3}%）",
                  f"合計投入 {capital:,.0f} 元（{tot}%），保留現金 {capital_all - capital:,.0f} 元"))
    # 強勢續攻（創高拉回、噴出後整理）：只有兩筆（攻擊日、突破日），比例另外設定
    st.markdown(L("**強勢續攻　適用**", "（創高拉回、噴出後整理）"))
    ca1, ca2 = st.columns(2)
    ca1.number_input("攻擊日 (%)", 5, 100, step=5, key="a1")
    ca2.number_input("突破日 (%)", 0, max(0, 100 - _ss.a1), step=5, key="a2")
    st.markdown(L(f"攻擊日　{capital_atk * a1 / atk_tot:,.0f} 元（{a1}%）",
                  f"突破日　{capital_atk * a2 / atk_tot:,.0f} 元（{a2}%）",
                  f"合計投入 {capital_atk:,.0f} 元（{atk_tot}%），保留現金 {capital_all - capital_atk:,.0f} 元"))
    st.button("✅ 設定好了，收起來", key="fund_close", use_container_width=True, on_click=set_fund_open, args=(False,))
else:
    st.caption(L(f"可用資金 {capital_all:,.0f} 元",
                 f"低位起漲、量縮抗跌　{w1}/{w2}/{w3}（投入 {tot}%）",
                 f"強勢續攻　攻擊日 {a1}／突破日 {a2}（投入 {atk_tot}%）"))
    st.button("調整資金比例", key="fund_open_btn", use_container_width=True, on_click=set_fund_open, args=(True,))

# ==================== 單檔回測 ====================
BT_MODE = {"ladder_avg": "分 3 次買｜買到第 2 筆後，出場價上移到平均成本（保本）",
           "ladder_stop2": "分 3 次買｜買到第 2 筆後，出場價上移到突破價下 5%",
           "ladder_stop1": "分 3 次買｜出場價一直不動",
           "single": "一次全部買進｜出場價不動"}
BT_SHORT = {"pullback": "續攻・創高拉回", "flag": "續攻・噴出後整理", "breakout": "低位起漲", "dry": "量縮抗跌"}
BT_REASON = {"target": "🎯", "stop": "🛑", "time": "⏰"}
CRASH_NOTE = {
    "2020 疫情崩盤": "新冠肺炎全球爆發，各國封城，全球股市恐慌性崩跌",
    "2022 全年空頭": "美國通膨失控、聯準會快速升息，加上俄烏戰爭，股市全年走空",
    "2024/08 急殺": "日本升息引發日圓套利交易平倉，加上美國就業數據轉弱，8/5 台股單日重挫",
    "2025/04 關稅急跌": "美國宣布對各國課徵對等關稅，全球股市連續重挫",
}
CRASHES = {                      # 崩盤期（訊號出現在這段期間的交易）
    "2020 疫情崩盤": ("2020-02-15", "2020-04-30"),
    "2022 全年空頭": ("2022-01-01", "2022-12-31"),
    "2024/08 急殺": ("2024-07-15", "2024-09-15"),
    "2025/04 關稅急跌": ("2025-03-25", "2025-05-31"),
}


def _stat_row(label, s_):
    if not s_.get("n"):
        return (label, "0", "—", "—", "—", "—")
    return (label, f"{s_['n']}", f"{s_['win'] * 100:.0f}%", f"{s_['mean'] * 100:+.1f}%",
            f"{s_['avg_win'] * 100:+.1f}／{s_['avg_loss'] * 100:.1f}%", f"{s_['worst'] * 100:+.1f}%")


def stock_bt_body(code, suffix, capital):
    import backtest as BT
    mode = "ladder_avg"            # 照你設定的進場邏輯：三段比例，買到第 2 筆後出場價拉到平均成本
    only_active = st.toggle("只在大盤啟用該策略時進場（照目前規則）", value=True, key="sbt_active")
    st.caption(L(f"強勢續攻：盤中漲 {cfg.CHASE_GAIN * 100:.0f}% 且突破盤整高點就追（你的習慣）",
                 f"攻擊日買 {cfg.ATTACK_SPLIT[0] * 100:.0f}%、突破日買 {cfg.ATTACK_SPLIT[1] * 100:.0f}%",
                 f"跌破盤整低點出場（最多賠 {cfg.ATTACK_MAX_LOSS * 100:.0f}%），突破後出場價移到前高下 {cfg.ATTACK_STOP2_PCT * 100:.0f}%",
                 f"低位起漲、量縮抗跌：分 3 次買（{w1}/{w2}/{w3}），買到第 2 筆後出場價拉到平均成本"))
    periods = {"近 5 年": None, **{k: v for k, v in CRASHES.items()}}
    period = st.selectbox("期間", list(periods), key="sbt_period")
    with st.spinner("回測中…第一次要載入 8 年的大盤資料，約需 30 秒"):
        T_all = stock_backtest(code, suffix, tuple(ratios), mode, only_active)
    if T_all is None:
        st.info("這檔的歷史資料不足，無法回測")
        return
    raw = load_stock_long(code, suffix)
    end = raw.index[-1]
    if periods[period] is None:
        a_, b_ = end - pd.DateOffset(years=5), end
    else:
        a_, b_ = (pd.Timestamp(x) for x in periods[period])
    T = T_all[(T_all["date"] >= a_) & (T_all["date"] <= b_)] if not T_all.empty else T_all
    if period in CRASH_NOTE:
        st.caption(f"📌 {CRASH_NOTE[period]}")

    if raw.index[0] > a_ + pd.Timedelta(days=100):
        st.info(L(f"這檔的資料從 {raw.index[0]:%Y/%m} 才開始", "這段期間無法回測"))
    elif T.empty:
        st.info(L(f"{period}這檔沒有任何交易",
                  "大盤規則在這段期間沒有啟用策略" if only_active else "這段期間沒有出現符合條件的訊號"))
    else:
        rows = [_stat_row(BT_SHORT[n], BT.trade_stats(T[T["strat"] == n])) for n in BT.STRATS
                if (T["strat"] == n).any()]
        rows.append(_stat_row("合計", BT.trade_stats(T)))
        show_table(["策略", "筆數", "勝率", "期望值", "平均賺／賠", "最差"], rows)
        pnl = T["ret_plan"] * capital
        st.markdown(L(f"**每次都投入 {capital:,.0f} 元**", f"累計損益約 {pnl.sum():+,.0f} 元（共 {len(T)} 筆）"))

    # 追價 vs 收盤確認（只比較強勢續攻）
    T_close = stock_backtest(code, suffix, tuple(ratios), mode, only_active, "close")
    rows_c = []
    for label, X in (("盤中漲 4% 就追", T_all), ("收盤確認才買", T_close)):
        if X is None or X.empty:
            rows_c.append((label, "0", "—", "—", "—"))
            continue
        X = X[(X["date"] >= a_) & (X["date"] <= b_) & X["strat"].isin(["pullback", "flag"])]
        if X.empty:
            rows_c.append((label, "0", "—", "—", "—"))
            continue
        s_ = BT.trade_stats(X)
        rows_c.append((label, f"{s_['n']}", f"{s_['win'] * 100:.0f}%", f"{s_['mean'] * 100:+.1f}%",
                       f"{(X['ret_plan'] * capital).sum():+,.0f}"))
    st.markdown("**追價 vs 收盤確認**（強勢續攻）")
    show_table(["買法", "筆數", "勝率", "期望值", "損益（元）"], rows_c)
    st.caption(L("盤中追：漲 4% 且突破盤整高點就買，收盤不一定是攻擊日",
                 "收盤確認：收盤符合全部攻擊日條件，用收盤價買"))

    # 崩盤期總覽：這檔股票本身跌多少 vs 照策略操作的結果
    st.markdown("**崩盤期總覽**")
    rows = []
    short = {"2020 疫情崩盤": "2020 疫情", "2022 全年空頭": "2022 空頭", "2024/08 急殺": "2024/08", "2025/04 關稅急跌": "2025/04"}
    for label_full, (ca, cb) in CRASHES.items():
        label = short.get(label_full, label_full)
        seg = raw.loc[ca:cb, "Close"]
        if len(seg) < 5 or raw.index[0] > pd.Timestamp(ca) - pd.Timedelta(days=100):
            rows.append((label, "—", "—", "—", "—"))
            continue
        hold = seg.iloc[-1] / seg.iloc[0] - 1
        x = T_all[(T_all["date"] >= ca) & (T_all["date"] <= cb)] if not T_all.empty else T_all
        if x.empty:
            rows.append((label, f"{hold * 100:+.1f}%", "0", "—", "未進場"))
        else:
            s_ = BT.trade_stats(x)
            rows.append((label, f"{hold * 100:+.1f}%", f"{s_['n']}", f"{s_['win'] * 100:.0f}%",
                         f"{(x['ret_plan'] * capital).sum():+,.0f}"))
    show_table(["期間", "股價漲跌", "筆數", "勝率", "損益（元）"], rows)
    st.caption(L(*[f"{short[k]}：{v}" for k, v in CRASH_NOTE.items()]))
    st.caption(L("股價漲跌＝這段期間一直抱著這檔的漲跌", "筆數、損益＝照策略操作（訊號出現在這段期間的交易）",
                 "未進場＝策略沒有出手，也就沒有虧損"))
    if T.empty:
        return
    st.markdown("**每一筆交易**（新的在上面）")
    lst = T.sort_values("date", ascending=False).head(20)
    show_table(["進場日", "策略", "進場", "出場", "結果", "損益"], [
        (f"{r.entry_date:%y/%m/%d}", BT_SHORT[r.strat], px(r.entry_px), px(r.exit_px), BT_REASON[r.reason],
         f"{r.ret_plan * capital:+,.0f}") for r in lst.itertuples()])
    st.caption("損益單位：元　🎯 碰到目標價　🛑 碰到出場價　⏰ 滿 60 天賣出" + (f"　｜只列最近 20 筆，共 {len(T)} 筆" if len(T) > 20 else ""))
    st.caption(L("規則：訊號隔天開盤進場；同一天碰到出場價和目標價先算出場",
                 f"滿 {BT.MAX_HOLD} 個交易日還沒碰到出場價或目標價，用當天收盤價賣出",
                 f"每筆都已扣買賣成本 {cfg.FEE_RT * 100:.1f}%（手續費 0.1425%×2＋證交稅 0.3%）",
                 "期望值＝平均每筆報酬（以實際投入的錢計）",
                 "回測是過去的表現，不代表未來"))


# ==================== 個股診斷 ====================
st.divider()
st.subheader("🔍 個股診斷")
if st.session_state.get("target"):
    if st.button("回到今日推薦", key="back_to_list", use_container_width=True):
        st.session_state["_scroll_back"] = st.session_state["target"].get("code", "")
        st.rerun()
_back = st.session_state.pop("_scroll_back", None)
if _back is not None:
    # 回到剛剛點的那一檔按鈕；找不到（例如清單收起來了）就回到「今日推薦」標題
    components.html(f"""<script>
    // {time.time()}
    setTimeout(function () {{
      const doc = window.parent.document;
      for (const b of doc.querySelectorAll('button')) {{
        if ("{_back}" && b.innerText.trim().indexOf("{_back} ") === 0) {{ b.scrollIntoView({{block: 'center'}}); return; }}
      }}
      for (const h of doc.querySelectorAll('h1,h2,h3')) {{
        if (h.innerText.indexOf('今日推薦') >= 0) {{ h.scrollIntoView({{block: 'start'}}); break; }}
      }}
    }}, 150);
    </script>""", height=0)
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
            st.session_state["_vol_note"] = ""
            if sfx == ".TW":                                      # 上市股票：成交量改用證交所官方（跟券商一致）
                try:
                    tw_vol = load_twse_stock_vol(code)
                    df = df.copy()
                    df["Volume"] = tw_vol.reindex(df.index).fillna(df["Volume"])
                except Exception as e:                            # 證交所抓不到，沿用 Yahoo 的量，並在畫面上說明
                    print("個股成交量改用證交所失敗，沿用 Yahoo：", repr(e)[:120])
                    st.session_state["_vol_note"] = "⚠️ 證交所個股量暫時抓不到，成交量先用 Yahoo 的數字"
                else:
                    st.session_state["_vol_note"] = ""

    if df is None:
        st.error(L(f"查無代號【{code}】的歷史資料", "請確認是否為有效掛牌標的"))
    else:
        if st.session_state.get("_vol_note"):
            st.warning(st.session_state["_vol_note"])
        ev = S.evaluate(df, mkt_df, ratios)
        name = target["name"] or code
        close = float(df["Close"].iloc[-1])
        chg = (close / float(df["Close"].iloc[-2]) - 1) * 100
        pv = float(df["Close"].iloc[-2])
        clr = "red" if chg > 0 else "green" if chg < 0 else "gray"        # 紅漲綠跌
        ar = "▲" if chg > 0 else "▼" if chg < 0 else "─"
        st.markdown(f"### {code} {name}")
        st.markdown(f"#### :{clr}[{px(close)}　{ar} {abs(close - pv):.2f}（{chg:+.2f}%）]")
        if target["industry"]:
            st.caption(f"產業別：{target['industry']}")
        ex = "TPEX" if target.get("suffix") == ".TWO" else "TWSE"
        st.markdown(f"[📊 在 TradingView 開啟線圖](https://www.tradingview.com/chart/?symbol={ex}%3A{code})")

        if "error" in ev:
            st.error(ev["error"])
        else:
            level, text = S.verdict(ev)
            {"go": st.success, "watch": st.warning, "stop": st.error, "wait": st.info}[level](brk(text))
            cl, cr = st.columns([3, 2])          # 電腦：左邊大圖、右邊文字；手機會自動變成上下排
            with cl:
                try:                                       # 跟大盤同一種風格的圖（chart_png.py）
                    import chart_png
                    st.image(chart_png.stock_chart(df, title=f"{code} {name}"), use_container_width=True)
                except Exception:                          # 萬一畫圖失敗，退回原本的互動圖
                    st.plotly_chart(plot_k_vol(df, is_market=False), use_container_width=True,
                                    config={"displayModeBar": False, "staticPlot": True})

            with cr:
                try:                                       # 壓力／支撐：跟圖上畫的虛線同一套規則（chart_png.vol_levels）
                    lv = chart_png.vol_levels(df)
                except Exception:
                    lv = {"press": None, "supp": None}
                pr, sp = lv["press"], lv["supp"]
                if any(x and "vday" not in x for x in (pr, sp)):          # chart_png.py 還是舊版（沒有起點那根 K 棒的資料）
                    st.warning(L("chart_png.py 是舊版", "請把最新的 chart_png.py 上傳到 GitHub"))
                    pr = sp = None
                if pr:
                    if pr.get("fallback"):
                        st.markdown(L(f"🐻 **壓力　{px(pr['level'])}（{pr['day']:%m/%d} 前高）**",
                                      f"高於現價 {px(close)}（{(pr['level'] / close - 1) * 100:+.1f}%）",
                                      "上方沒有帶量的 K 棒，前高就是壓力"))
                    else:
                        st.markdown(L(f"🐻 **壓力　{px(pr['level'])}（{pr['day']:%m/%d} 最高）**",
                                      f"高於現價 {px(close)}（{(pr['level'] / close - 1) * 100:+.1f}%）",
                                      f"起點：{pr['vday']:%m/%d} 帶量 K",
                                      "上方套牢區，漲上去容易有賣壓"))
                if sp:
                    if sp.get("fallback"):
                        st.markdown(L(f"🐂 **支撐　{px(sp['level'])}（{sp['day']:%m/%d} 前低）**",
                                      f"低於現價 {px(close)}（{(sp['level'] / close - 1) * 100:+.1f}%）",
                                      "下方沒有帶量的 K 棒，前低就是支撐"))
                    else:
                        st.markdown(L(f"🐂 **支撐　{px(sp['level'])}（{sp['day']:%m/%d} 最低）**",
                                      f"低於現價 {px(close)}（{(sp['level'] / close - 1) * 100:+.1f}%）",
                                      f"起點：{sp['vday']:%m/%d} 帶量 K",
                                      "可作短線支撐，或出場價的參考"))
                else:
                    st.markdown(L("🐂 **支撐　沒有**", "現價已經是近 65 日最低點", "下方沒有支撐，不建議進場"))
                if not pr:
                    st.caption("現價已經是近 65 日最高點，上方沒有壓力")
                st.caption(L("支撐＝最近一根帶量 K 到今天的最低價", "壓力＝最近一根帶量 K 到今天的最高價",
                             "帶量＝當天量 ≥ 20 日均量 1.5 倍，不分紅黑",
                             "下方沒有帶量 K 時，前低就是支撐", "上方沒有帶量 K 時，前高就是壓力",
                             "起點太近時，至少回看 3 個交易日"))
            st.write("")                         # 跟上面的說明空一行
            if st.button("回到今日推薦", key="back_to_list_bottom", use_container_width=True):
                st.session_state["_scroll_back"] = st.session_state["target"].get("code", "")
                st.rerun()
            st.divider()                         # 策略診斷、回測放在兩欄下面，整行寬度各自一區
            st.markdown("**策略診斷**")
            st.caption(L(f"以下依可用資金 {capital_all:,.0f} 元試算", f"低位起漲、量縮抗跌　{w1}/{w2}/{w3}", f"強勢續攻　攻擊日 {a1}／突破日 {a2}"))
            for sname in ("pullback", "flag", "breakout", "dry"):
                r = ev["results"][sname]
                label = cfg.STRATEGY_NAMES[sname]
                if r is None:
                    st.caption(f"{label}：資料不足，無法判斷")
                    continue
                applicable = "大盤適用" if r["active"] else "大盤不適用"

                def body(r=r, sname=sname):
                    st.markdown(f"**現價 {px(close)}**")
                    fails = [(cname, detail) for cname, cok, detail in r["checks"] if not cok]     # 只列沒過的，過的不重複列
                    total = len(r["checks"])
                    if fails:
                        st.markdown(L(f"**符合 {total - len(fails)} / {total} 條**", f"卡住的 {len(fails)} 條："))
                        md_fail(fails)
                    else:
                        st.markdown(f"✅ **{total} 條全部符合**")
                    if sname in ("pullback", "flag") and "cons_hi" in r:
                        st.markdown(L(f"**盤整區　{px(r['cons_lo'])} ～ {px(r['cons_hi'])}**",
                                      f"前波高點 {px(r['key'])}（{r['peak_date']:%m/%d}）"))
                        if not r.get("attack"):
                            st.caption(L("今天還不是攻擊日",
                                         f"明天漲到 {px(r['entry'])}（漲 {cfg.CHASE_GAIN * 100:.0f}% 且突破盤整高點）就是追價點",
                                         "以下用這個價格試算"))
                    if r["ok"] and not r["active"]:
                        st.warning(L("型態符合", "但目前大盤層級不適用這套策略"))
                    if "key" not in r or "stop1" not in r:
                        st.info("目前沒有符合的型態，無法試算。")
                    else:
                        plan = S.plan_for(sname, r, ratios, split=atk_split)
                        if plan["stop1"] >= plan["e"]:
                            st.info("現價已低於出場價，無法試算。")
                        else:
                            if not r["ok"]:
                                st.warning(L("尚未符合條件", "以下試算僅供參考"))
                            show_plan(plan, capital_atk if sname in ("pullback", "flag") else capital, close)

                foldable(f"{code}_{sname}", label, body, default=r["ok"],
                         status=f"{'✅ 符合' if r['ok'] else '❌ 尚未符合'}｜{applicable}")

            st.divider()
            sfx_bt = sfx                         # 上面實際抓到資料的市場別（.TW 或 .TWO）
            foldable(f"{code}_bt", "🧪 這檔的回測（近 5 年）",
                     lambda: stock_bt_body(code, sfx_bt, capital), default=False,
                     status="用你的資金比例，模擬這檔過去每一次訊號")

# 整頁跑完：拿掉載入卡片，之後切換功能不再出現
_splash.empty()
st.session_state["_booted"] = True

