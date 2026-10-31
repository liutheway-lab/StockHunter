"""app.py — 台股戰情室（Streamlit 網頁）
功能：大盤位階（五層＋止跌確認）、個股診斷（四套策略＋分批計畫）。
資料口徑跟夜間掃描完全相同（data.py、strategies.py 共用）。
"""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import yfinance as yf

import webcharts_v5 as charts
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


def md_table(head, rows):
    """靠右對齊的 markdown 表格（第一欄靠左）"""
    out = ["| " + " | ".join(head) + " |", "| :--- " + "| ---: " * (len(head) - 1) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def md_cond(rows):
    """條件表：結果 | 條件 | 現況。放在表格裡，換行時文字也會對齊"""
    esc = lambda t: str(t).replace("|", "／")
    return "\n".join(["| | 條件 | 現況 |", "| :---: | :--- | :--- |"]
                      + [f"| {a} | **{esc(b)}** | {esc(c)} |" for a, b, c in rows])


def plan_view(p, capital):
    """分批計畫：價位、投入、可買股數、各階段停損與風險"""
    r = p["ratios"]
    rows = [("① 進場", p["e"], r[0]), ("② 突破", p["b"], r[1]), ("③ 加碼", p["b2"], r[2])]
    tbl, shares, costs = [], [], []
    for label, price, ratio in rows:
        amt = capital * ratio
        sh = int(amt // price) if price > 0 else 0
        shares.append(sh)
        costs.append(sh * price)
        tbl.append((label, f"{price:.2f}", f"{amt:,.0f}", f"{sh:,}"))
    st.markdown(md_table(["階段", "價位", "投入", "股數"], tbl))
    st.caption(f"② 突破＝突破價；③ 加碼＝突破價 +{(cfg.EXT_RATIO - 1) * 100:.0f}%。投入單位：元。")

    tot_sh, tot_cost = sum(shares), sum(costs)
    st.markdown(md_table(["合計股數", "實際投入", "剩餘資金"],
                         [(f"{tot_sh:,}", f"{tot_cost:,.0f}", f"{capital - tot_cost:,.0f}")]))

    # 各階段停損：只買第 1 筆時停損在停損價；突破加碼後，停損拉到實際平均成本（保本）
    loss1 = shares[0] * max(p["e"] - p["stop1"], 0)
    risk = [("① 第 1 筆", f"{p['stop1']:.2f}", f"虧 {loss1:,.0f}")]
    notes = [f"第 1 筆停損約佔資金 {loss1 / capital * 100:.1f}%"]
    for k, name in ((2, "② 前 2 筆"), (3, "③ 前 3 筆")):
        sh, cost = sum(shares[:k]), sum(costs[:k])
        if sh > 0:
            risk.append((name, f"{cost / sh:.2f}", "約打平"))
            notes.append(f"前 {k} 筆約損費用 {cost * cfg.FEE_RT:,.0f}")
    st.markdown("**各階段停損**")
    st.markdown(md_table(["持有", "停損價", "結果"], risk))
    st.caption("買到第 2 筆（突破）後，停損拉到實際平均成本，整體不虧本，只損手續費與證交稅"
               f"（{'、'.join(notes)} 元）。若跳空跌破停損價，實際虧損會比表上多。")

    st.markdown(f"🎯 **目標價　{p['targets'][0][1]:.2f}**")
    if p.get("stop1_note"):
        st.caption(f"第 1 筆停損依據：{p['stop1_note']}")
    if len(p["targets"]) > 1:
        st.caption("目標價：" + "；".join(f"{lbl} `{px:.2f}`" for lbl, px in p["targets"]))
    for n in p.get("notes", []):
        st.caption(n)


def show_plan(p, capital):
    plan_view(p, capital)


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
            st.markdown(md_cond([
                (ok(s_["no_break"]), "不破低",
                 f"近 3 日最低 {s_['recent_low']:,.0f}（{s_['recent_low_date']:%m/%d}），"
                 f"前低 {s_['prior_low']:,.0f}（{s_['prior_low_date']:%m/%d}）"),
                (ok(s_["vol_ok"]), "量承接",
                 f"近 3 日最大量為爆量下跌日（{s_['dump_date']:%m/%d}）的 {ratio:.0f}%，需 100% 以上"),
            ]))

    st.plotly_chart(charts.plot_k_vol(mk["df"], is_market=True, stab=show_stab),
                    use_container_width=True, config={"displayModeBar": False})
    if show_stab:
        st.markdown(f"🟠 前低 {show_stab['prior_low']:,.0f}（{show_stab['prior_low_date']:%m/%d}）  \n"
                    f"🔵 近 3 日最低 {show_stab['recent_low']:,.0f}（{show_stab['recent_low_date']:%m/%d}）  \n"
                    f"🟨 黃色底 ＝ 近 3 日")
        st.caption("成交金額採用證交所數據，看相對比例。")
    if mk["active"]:
        (st.warning if mk["risk"] else st.success)(
            "大盤目前允許：" + "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"]))
    else:
        st.error("🔴 尚未止跌，不建立多頭部位")

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
        st.markdown(f"### {'🔴' if chg > 0 else '🟢'} {code} {name}｜現價 `{close:.2f}`（{chg:+.2f}%）")
        if target["industry"]:
            st.caption(f"產業別：{target['industry']}")
        ex = "TPEX" if target.get("suffix") == ".TWO" else "TWSE"
        st.markdown(f"[📊 在 TradingView 開啟線圖](https://www.tradingview.com/chart/?symbol={ex}%3A{code})")

        if "error" in ev:
            st.error(ev["error"])
        else:
            level, text = S.verdict(ev)
            {"go": st.success, "watch": st.warning, "stop": st.error, "wait": st.info}[level](text)
            st.plotly_chart(charts.plot_k_vol(df, is_market=False), use_container_width=True,
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
                    st.markdown(md_cond([("✅" if cok else "❌", cname, detail) for cname, cok, detail in r["checks"]]))
                    if sname == "pullback":
                        st.markdown(f"🎯 **理想第 1 筆進場區　{r['zone_lo']} ~ {r['zone_hi']}**")
                        st.caption(f"依你的比例 {w1}/{w2}/{w3} 推算，前高 {r['key']:.2f}")
                        if r["in_zone"]:
                            st.caption("現價已在進場區內，以下以現價為第 1 筆試算。")
                        elif r["close"] > r["zone_hi"]:
                            st.caption(f"現價還沒回檔到進場區，以下假設第 1 筆掛在 {r['zone_hi']} 試算。")
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
                            show_plan(plan, capital)

                foldable(f"{code}_{sname}", label, body, default=r["ok"],
                         status=f"{'✅ 符合' if r['ok'] else '❌ 尚未符合'}｜{applicable}")

st.caption("最後更新：2026 年 10 月")
