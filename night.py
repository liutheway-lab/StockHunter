"""night.py — 收盤後個股掃描（台灣時間 15:30 開始，時間設定在 config.NIGHT_SCAN_HM；資料沒更新會每 5 分鐘再抓，最多等 40 分鐘）
流程：加權指數 → 判斷大盤五層 → 依層級決定啟用哪些策略 → 掃描全上市櫃 → Telegram 通知。
沒有符合的股票也會發訊息（確認系統有在跑）。
所有策略數字都在 config.py。

診斷模式：手動執行時把 diag 勾起來（或環境變數 DIAG=true），會不管大盤層級、
列出四套策略各自符合的股票，並顯示每個條件各有幾檔通過，用來判斷哪個條件太嚴格。
"""
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config as cfg
import strategies as st
import dry_groups as dg
import concept_lookup as cl
from data import apply_twse_volume, fetch_market, fetch_prices, get_directory
from notify import send_photo, send_telegram

MAX_LIST = 12   # 每套策略最多列出幾檔（依符合程度由高到低，同分才比 20 日均成交金額）
WEEKDAY = "一二三四五六日"
LINE = "━━━━━━━━━━━━━━"
LAYER_ICON = {0: "⚪", 1: "🟢", 2: "🟢", 3: "🟡", 4: "🔴", 5: "🔴"}
ORDER = ["pullback", "flag", "breakout", "dry"]
TITLES = {"pullback": "📈 強勢續攻（創高拉回）", "flag": "🚀 強勢續攻（噴出後整理）",
          "breakout": "🌱 低位起漲", "dry": "🛡 量縮抗跌"}
DIAG = os.getenv("DIAG", "").strip().lower() in ("1", "true", "yes")


def fp(x):
    """價格格式：依台股升降單位（tick）對齊，只留必要的小數，不補零。
    例：33.30→33.3、227.0→227、124.5→124.5、2820→2,820"""
    tick = 0.01 if x < 10 else 0.05 if x < 50 else 0.1 if x < 100 else 0.5 if x < 500 else 1 if x < 1000 else 5
    x = round(round(x / tick) * tick, 2)
    if abs(x - round(x)) < 1e-9:
        return f"{x:,.0f}"
    if abs(x * 10 - round(x * 10)) < 1e-9:
        return f"{x:.1f}"
    return f"{x:.2f}"


def code_block(lines, header=False, lang=""):
    # 行尾補「看不見的空格」，避免 Telegram 右上角的 </> 複製鈕蓋到最後一個字
    # header=True：第一行是表頭，不補空格（表頭本來就比資料行寬，再補會被切到下一行）
    return "```" + lang + "\n" + "\n".join(ln + ("" if header and k == 0 else "⠀" * 3) for k, ln in enumerate(lines)) + "\n```"


# ---------- 大盤區塊 ----------
def market_block(mk):
    ma = mk["ma"]
    head = f"🌐 *大盤｜第 {mk['layer']} 層・{mk['layer_name']}* {LAYER_ICON[mk['layer']]}"
    if mk.get("tag"):
        head += f"\n{'🔥' if mk['tag'] == '過熱' else '😱'} *{mk['tag']}*"

    def lab(t, w=5):                                          # 中文字占 2 格，用顯示寬度補空白才會對齊
        return t + " " * max(0, w - sum(2 if ord(ch) > 0x2E80 else 1 for ch in t))

    def two(l1, v1, l2="", v2=None):
        s = f"{lab(l1)}{v1:>8,.0f}"
        return s + (f"   {lab(l2)}{v2:>8,.0f}" if v2 is not None else "")

    tbl = [two("5日", ma[5], "10日", ma[10]),
           two("月線", ma[20], "季線", ma[60])]
    cc = mk["df"]["Close"]
    chg = float(cc.iloc[-1] - cc.iloc[-2]) if len(cc) > 1 else 0.0
    pct = chg / float(cc.iloc[-2]) * 100 if len(cc) > 1 else 0.0
    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "─"          # 只用箭頭，不用紅綠燈（紅綠燈留給利多／利空，避免搞混）
    out = [head, f"收盤　{mk['close']:,.0f}\n{arrow} {abs(chg):,.0f} 點（{pct:+.2f}%）", code_block(tbl)]
    if mk.get("why"):
        out.append("\n".join(mk["why"]))
    if mk.get("tag"):
        out.append(st.SHORT_TAG_NOTE[mk["tag"]])
    if mk.get("dd") is not None and mk["dd"] <= -cfg.DROP_WARN:
        out.append(f"⚠️ 離近 60 日高點\n已經跌了 {abs(mk['dd']) * 100:.1f}%")

    if mk.get("base_layer", mk["layer"]) in (4, 5):          # 空頭，或從空頭反彈升上來：都顯示止跌確認
        s = mk["stab"]
        if s is None:
            out.append("🧱 止跌確認：資料不足")
        elif s["vol_missing"]:
            out.append("🧱 止跌確認：❌ 無法確認\n大盤成交金額抓不到")
        else:
            ok = lambda b: "✅" if b else "❌"
            ratio = s["recent_vol"] / s["dump_vol"] * 100 if s["dump_vol"] else 0
            da, nd = s["days_after"], s["need_days"]
            state = "✅ 通過" if s["ok"] else ("⏳ 觀察中" if s.get("watching") else "❌ 未通過")
            out.append(f"🧱 *止跌確認*　{state}\n下面 ① ② 兩項都過才算止跌")
            low_ok = "✅" if s["no_break"] else "⏳" if s["held"] else "❌"
            out.append(f"低點日：{s['signal_date']:%m/%d}\n（爆量下跌日當天起股價最低的那天）")
            out.append("*① 反彈*\n低點日之後曾經有一天\n收盤離低點日最低點\n反彈 ≥ " f"{cfg.STAB_REBOUND * 100:.0f}%")
            rb = [f"最多反彈 {s['rebound_max'] * 100:>+5.1f}%　{ok(s['reb_ok'])}"]
            if s.get("reb_date") is not None:
                rb.append(f"達標日 {s['reb_date']:%m/%d}")
            rb.append(f"今天 {s['rebound'] * 100:>+5.1f}%　要 ≥ {cfg.STAB_REBOUND * 100:.0f}%")
            out.append(code_block(rb, lang="text"))
            out.append(f"*② 不破低*\n低點日之後至少 {s['need_days']} 天\n最低點都沒跌破低點日最低點")
            if da == 0:
                blk = [f"不破低　待觀察（要 ≥ {s['need_days']} 天）　{low_ok}"]
            else:
                blk = [f"天數　{da} 天　要 ≥ {s['need_days']} 天",
                       f"最低 {s['recent_low']:,.0f}　要 ≥ {s['prior_low']:,.0f}　{low_ok}"]
            out.append(code_block(blk, lang="text"))
            out.append(f"參考\n量能 {ratio:.0f}%（低點日÷爆量日 {s['dump_date']:%m/%d}）\n下影線 {s['shadow'] * 100:.0f}%")
            if s["ok"]:
                out.append("📌 日線止跌走穩\n先以 2～3 成資金佈局\n不猜底、不貪心")
    if mk["active"]:
        names = cfg.names_join(mk["active"])
        out.append(f"✅ 啟用策略：{names}" + ("\n⚠️ 第 3 層方向未明\n只開最保守的策略，部位減半" if mk["risk"] else ""))
        out.append(f"💰 部位：{('過熱，維持正常部位，不加碼' if mk.get('tag') == '過熱' else cfg.LAYER_SIZE.get(mk['layer'], ''))}")
    else:
        out.append("🔴 尚未止跌，不建議建立多頭部位" if mk["layer"] in (4, 5) else "🔴 沒有啟用的策略")
    return "\n".join(out)


# ---------- 掃描 ----------
def dedupe_similar(lists, active):
    """強勢續攻的兩種型態（創高拉回、噴出後整理）攻擊日進場規則一樣，同一檔常常兩邊都符合。
    兩邊都有的股票只留一份：預設留在「噴出後整理」，並標上也符合創高拉回；
    如果只有「創高拉回」是啟用的型態（例如大盤第 3 層），就留在創高拉回。"""
    a, b = lists.get("pullback"), lists.get("flag")
    if a is None or b is None:
        return
    keep_flag = "flag" in active or "pullback" not in active
    keep, drop, kname, dname = (b, a, "flag", "pullback") if keep_flag else (a, b, "pullback", "flag")
    dsyms = {h["sym"] for h in drop}
    for h in keep:
        if h["sym"] in dsyms:
            h["also"] = cfg.SUB_NAMES[dname]
    ksyms = {h["sym"] for h in keep}
    lists[dname][:] = [h for h in drop if h["sym"] not in ksyms]


ABSORB_BASE = {}      # {代號.TW: (20 日均量（股）, 收盤價, 日線三層數字)}：給盤中 13:00 恐慌承接預判用


def absorb_avg_poc(sym, day):
    """恐慌承接發現日的均價與 POC（用 Yahoo 5 分 K）。抓不到回傳 (None, None)，盤中就只看跌停與破低"""
    try:
        import yfinance as yf
        bars = yf.download(sym, period="60d", interval="5m", progress=False, auto_adjust=False)
        if bars is None or bars.empty:
            return None, None
        if hasattr(bars.columns, "levels"):                   # 多層欄位（yfinance 新版）→ 只留欄位名稱
            bars.columns = bars.columns.get_level_values(0)
        return st.day_avg_poc(bars, day)
    except Exception as e:
        print(f"{sym} 發現日均價／POC 抓不到：{repr(e)[:80]}")
        return None, None


def monthly_zone(sym):
    """十年月線的最高、最低（低位階用）。抓不到回傳 None，這檔就先不算低檔"""
    try:
        import yfinance as yf
        h = yf.Ticker(sym).history(period="10y", interval="1mo", auto_adjust=False)
        if h is None or h.empty:
            return None
        return {"lo": float(h["Low"].min()), "hi": float(h["High"].max())}
    except Exception as e:
        print(f"{sym} 十年月線抓不到：{repr(e)[:80]}")
        return None


def scan(prices, mk, ratios):
    """回傳 (命中 {策略: [項目]}, 流動性合格檔數, 資料過舊略過檔數, 各條件通過檔數, 明日追價名單, 恐慌承接名單)"""
    hits = {k: [] for k in ORDER}
    watch = {"pullback": [], "flag": []}       # 明日追價名單
    funnel = {k: {"checks": {}, "ok": 0} for k in ORDER}
    absorb = []                                # 恐慌承接觀察名單（今天先殺後拉：長下影且帶量）
    liquid = stale = 0
    ABSORB_BASE.clear()
    last_date = mk["date"]
    # 量縮抗跌的族群排名（大盤不好時，族群中位數跌幅前 5 名）：每次掃描算一次；抓失敗就沿用上一次並提醒
    rank, rank_stale = None, False
    try:
        members = dg.fetch_all_members()
        if members:
            rank = dg.rank_groups({s.split(".")[0]: r["Close"] for s, r in prices.items()}, mk["df"]["Close"], members)
            if rank:
                dg.save_rank(rank)
    except Exception as e:
        print("族群排名失敗：", repr(e)[:120])
    if not rank:
        rank, rank_stale = dg.load_rank(), True
        dg.notify_owner("⚠️ 族群排名這次沒算出來，用的是上一次的排名（" + str((rank or {}).get("date", "沒有紀錄")) +
                        "），請檢查 Yahoo 族群頁")
    for sym, raw in prices.items():
        try:
            if raw.index[-1] < last_date:        # 停牌或資料沒更新到最新一天
                stale += 1
                continue
            df = st.add_indicators(raw)
            i = len(df) - 1
            if i < 69:
                continue
            if not st._liq(df.iloc[i])[1]:
                continue
            liquid += 1
            mav = float(df["MAV20"].iloc[i])
            if mav == mav and mav > 0:
                ABSORB_BASE[sym] = (mav, float(df["Close"].iloc[i]), st.absorb_levels(df.iloc[:i]))
            results = {
                "pullback": st.check_pullback(df, i, ratios),
                "flag": st.check_flag(df, i),
                "breakout": st.check_breakout(df, i),
                "dry": st.check_dry(df, i, mk["df"]),
            }
            if st.dry_needs_group(results["dry"]):                 # 候選股：盤整＋量縮都過 → 查它的概念，看族群是不是前 5 名
                concepts = cl.concepts_of(sym.split(".")[0])
                results["dry"] = st.check_dry(df, i, mk["df"], resilient=dg.is_resilient(concepts, rank))
            if st.bo_needs_zone(results["breakout"]):          # 候選股：只差低檔一項 → 再抓十年月線算位置
                zone = monthly_zone(sym)
                if zone:
                    results["breakout"] = st.check_breakout(df, i, zone)
            ab = st.check_absorb(df, i)
            if ab:
                avg, poc = absorb_avg_poc(sym, df.index[i])          # 發現日的均價＋POC：盤中用來判斷警示與站回
                absorb.append({"sym": sym, "r": ab, "turnover": ab["turnover"], "score": st.rank_absorb(ab),
                               "avg": avg, "poc": poc})
            for name, r in results.items():
                if r is None:
                    continue
                for cname, cok, _ in r["checks"]:
                    funnel[name]["checks"][cname] = funnel[name]["checks"].get(cname, 0) + (1 if cok else 0)
                if r["ok"]:
                    funnel[name]["ok"] += 1
                    plan = st.plan_for(name, r, ratios)
                    hits[name].append({"sym": sym, "r": r, "plan": plan, "turnover": r["turnover"], "vl": st.vol_low(df),
                                        "score": st.rank_score(name, r, plan)})
            for name in watch:                    # 今天還沒攻擊、但盤整條件已成立 → 明天的追價點
                if results[name] is not None and results[name]["ok"]:
                    continue
                w = st.watch_tomorrow(raw, name)
                if w:
                    plan = st.attack_plan(w, entry=w["chase_px"])
                    watch[name].append({"sym": sym, "r": w, "plan": plan, "turnover": w["turnover"],
                                        "score": st.rank_score(name, w, plan, watch=True),
                                        "prev": float(raw["Close"].iloc[-2]) if len(raw) >= 2 else None})
        except Exception as e:   # 單一股票出錯不影響整體
            print(f"{sym} 分析失敗：{e!r}")
    by_score = lambda x: (-x["score"], -x["turnover"])        # 越符合規則越前面；同分才比成交金額
    for k in hits:
        hits[k].sort(key=by_score)
    for k in watch:
        watch[k].sort(key=by_score)
    absorb.sort(key=by_score)
    dedupe_similar(hits, mk["active"])
    dedupe_similar(watch, mk["active"])
    return hits, liquid, stale, funnel, watch, absorb


def funnel_lines(funnel, liquid):
    """各策略每個條件各有幾檔通過（診斷用）"""
    out = [f"今天流動性合格共 {liquid} 檔。",
           "下面每個條件是『單獨』計算有幾檔過關；全部條件同時過關才會入選。"]
    for name in ORDER:
        f = funnel[name]
        out.append("")
        out.append(f"{TITLES[name]}　最後入選 {f['ok']} 檔")
        for c, k in f["checks"].items():
            if c != "流動性":
                out.append(f"・{c}　{k}／{liquid}")
    return out


def _w(t):
    """估算顯示寬度：中文約 1.68 個半形字寬（iPhone 的等寬字型），其他 1"""
    return sum(1.68 if ord(c) > 0x2E80 else 1 for c in t)


def _padr(t, width):
    """靠左對齊，用全形空白＋半形空白把寬度補到 width"""
    d = width - _w(t)
    if d <= 0:
        return t
    n = int(d // 1.68)
    return t + "\u3000" * n + " " * round(d - n * 1.68)


def tv_link(sym):
    """TradingView 連結（台股：上市 TWSE、上櫃 TPEX）"""
    code = sym.split(".")[0]
    ex = "TPEX" if sym.endswith(".TWO") else "TWSE"
    return f"[{code}](https://www.tradingview.com/chart/?symbol={ex}%3A{code})"


def inactive_note(mk):
    """策略目前沒有被大盤走勢啟用時，標題下面加的說明（每行都短，手機上不會被切斷）"""
    return (f"\n⚠️ 大盤走勢篩選：未啟用"
            f"\n大盤現在是第 {mk['layer']} 層・{mk['layer_name']}"
            f"\n這套策略目前不建議進場"
            f"\n名單只供參考")


def strategy_block(name, items, names, risk, note=""):
    title = f"*{TITLES[name]}*　{len(items)} 檔" + ("　⚠️標示風險" if risk else "") + note
    shown = items[:MAX_LIST]
    labels = []
    for h in shown:
        code = h["sym"].split(".")[0]
        broke = name == "flag" and h["r"].get("status", "").startswith("已突破")
        vl = h.get("vl")
        labels.append(f"{code} {names.get(h['sym'], code)}" + ("↑" if broke else "") + (f" {vl['side']}" if vl else "")
                      + ("◆" if h.get("also") else ""))
    W = max([_w("代號 名稱")] + [_w(x) for x in labels])
    cols = [("現價", 3), ("突破價", 1), ("出場價", 1), ("目標", 1)]          # 表頭縮短，整行才不會被切成兩行
    lines = [_padr("代號 名稱", W) + "".join(" " * pad + c for c, pad in cols)]
    for h, lab in zip(shown, labels):
        p = h["plan"]
        nums = [h["r"]["close"], p["key"], p["stop1"], p["targets"][0][1]]
        lines.append(_padr(lab, W) + "".join(f" {fp(v):>5}" for v in nums))
    links = "　".join(tv_link(h["sym"]) for h in shown)
    more = f"\n（完整 {len(items)} 檔請到 App 查看）" if len(items) > MAX_LIST else ""
    low_rr = [(names.get(h["sym"], h["sym"].split(".")[0]), h["plan"]["rr"]) for h in shown
              if h["plan"].get("rr") is not None and h["plan"]["rr"] < cfg.RR_MIN]
    rr_txt = ""
    if low_rr:
        pairs = [f"{n} {r:.1f}" for n, r in low_rr]
        rr_txt = f"\n⚠️ 風報比低於 {cfg.RR_MIN}：\n" + "\n".join("　" + "　".join(pairs[k:k + 2]) for k in range(0, len(pairs), 2))
    t2 = [(names.get(h["sym"], h["sym"].split(".")[0]), h["plan"]["targets"][1][1]) for h in shown
          if len(h["plan"]["targets"]) > 1]
    if t2:
        pairs = [f"{n} {fp(v)}" for n, v in t2]
        rr_txt += "\n🎯 第二目標（參考）：\n" + "\n".join("　" + "　".join(pairs[k:k + 2]) for k in range(0, len(pairs), 2))
    legend = ("\n排序：越符合規則越前面"
              "\n壓＝第1大量日最高點在現價上"
              "\n撐＝第1大量日最低點在現價下"
              "\n（65 日內成交量最大的那天）")
    also = sorted({h["also"] for h in shown if h.get("also")})
    if also:
        legend += f"\n◆＝也符合{'、'.join(also)}"
    return title + "\n" + code_block(lines, header=True) + rr_txt + legend + more + "\n📊 TradingView 線圖：" + links


def watch_block(name, items, names, note="", icon="🎯"):
    """明日追價名單：明天盤中漲 4% 且突破盤整高點，就是追價點"""
    title = f"*{icon} 明日追價名單・{cfg.STRATEGY_NAMES[name]}*　{len(items)} 檔" + note
    shown = items[:MAX_LIST]
    labels = [f"{h['sym'].split('.')[0]} {names.get(h['sym'], h['sym'].split('.')[0])}" + ("◆" if h.get("also") else "")
              for h in shown]
    W = max([_w("代號 名稱")] + [_w(x) for x in labels])
    cols = [("現價", 3), ("追價點", 1), ("出場價", 1), ("目標", 1)]
    lines = [_padr("代號 名稱", W) + "".join(" " * pad + c for c, pad in cols)]
    for h, lab in zip(shown, labels):
        p = h["plan"]
        nums = [h["r"]["close"], p["e"], p["stop1"], p["targets"][0][1]]
        lines.append(_padr(lab, W) + "".join(f" {fp(v):>5}" for v in nums))
    more = f"\n（完整 {len(items)} 檔請到 App 查看）" if len(items) > MAX_LIST else ""
    legend = (f"\n排序：越符合規則越前面"
              f"\n追價點＝明天盤中要漲到的價格"
              f"\n（漲 {cfg.CHASE_GAIN * 100:.0f}% 且突破盤整高點）"
              f"\n盤整 {cfg.ATTACK_MIN_CONSOL}～{cfg.WATCH_MAX_CONSOL} 天、離前高 ≤ {cfg.ATTACK_MAX_DEPTH * 100:.0f}%"
              f"\n追價點離現價最多 {cfg.CHASE_MAX_DIST * 100:.0f}%"
              f"\n盤中看強弱再決定追不追")
    also = sorted({h["also"] for h in shown if h.get("also")})
    if also:
        legend += f"\n◆＝也符合{'、'.join(also)}"
    links = "　".join(tv_link(h["sym"]) for h in shown)
    return title + "\n" + code_block(lines, header=True) + legend + more + "\n📊 TradingView 線圖：" + links


RESULT_FILE = "scan_results.json"
BASE_FILE = "absorb_base.json"          # 全市場基準（盤中 13:00 預判要讀）
PRE_FILE = "absorb_1300.json"           # 盤中 13:00 通知過的名單（intraday.py 存，這裡對照收盤結果）


def load_pre1300(mk):
    """今天 13:00 預判通知過的代號集合；檔案不是今天的就當作沒有"""
    try:
        with open(PRE_FILE, encoding="utf-8") as f:
            j = json.load(f)
        if j.get("date") == f"{mk['date']:%Y-%m-%d}":
            return set(j.get("codes") or [])
    except Exception:
        pass
    return set()


def absorb_block(items, names, note="", pre=None):
    """恐慌承接觀察名單：今天殺低後被買回來、而且有量。只列股票，沒有追價點"""
    title = f"*🧺{cfg.ABSORB_NAME}　{len(items)}檔*\n說明（今日帶量先殺後拉）" + note
    shown = items[:MAX_LIST]
    pre = pre or set()
    # 表格只放「純半形」的數字欄，名稱放最後一欄：
    # 中文字在不同手機／電腦的寬度不一樣，放在最後就不會把數字欄推歪（朋友的畫面不會空很大）
    rows = [(h["sym"].split(".")[0], fp(h["r"]["close"]), fp(h["r"]["low"]), f"{h['r']['vol_mult']:.1f}",
             names.get(h["sym"], h["sym"].split(".")[0]) + ("◆" if h["sym"].split(".")[0] in pre else ""))
            for h in shown]
    cw = [max(len(r[k]) for r in rows) for k in (1, 2, 3)] if rows else [0, 0, 0]
    lines = [f"{code} {c:>{cw[0]}} {lo:>{cw[1]}} {v:>{cw[2]}}  {nm}" for code, c, lo, v, nm in rows]
    more = f"\n（完整 {len(items)} 檔請到 App 查看）" if len(items) > MAX_LIST else ""
    legend = (f"\n欄位：代號、現價、今日最低、量能（倍）、名稱"
              f"\n排序：量能越大、振幅越大越前面"
              f"\n先殺：當天最低價跌破昨收"
              f"\n長下影＝下影線占當天 {cfg.STAB_SHADOW * 100:.0f}% 以上"
              f"\n量能＝今天量 ÷ 20 日均量"
              f"\n要 {cfg.ABSORB_VOL_MULT} 倍以上"
              f"\n當天振幅要 {cfg.ABSORB_MIN_RANGE * 100:.0f}% 以上"
              f"\n距 20 日高點已跌 {cfg.ABSORB_DROP_MIN * 100:.0f}%～{cfg.ABSORB_DROP_MAX * 100:.0f}%"
              f"\n收盤守住 65 日大量日的低點"
              f"\n收盤要收回最低 {cfg.ABSORB_RECOVER * 100:.0f}% 以上"
              f"\n只供追蹤，沒有追價點"
              f"\n隔天不破發現日最低，才比較可信")
    if pre:
        legend += "\n◆＝13:00 已預判通知過"
        codes_now = {h["sym"].split(".")[0] for h in items}
        gone = sorted(c for c in pre if c not in codes_now)
        if gone:
            nm = {k.split(".")[0]: v for k, v in names.items()}
            legend += "\n13:00 預判、收盤沒成立：" + "、".join(f"{c} {nm.get(c, '')}".strip() for c in gone)
    links = "　".join(tv_link(h["sym"]) for h in shown)
    return title + "\n" + code_block(lines) + legend + more + "\n📊 TradingView 線圖：" + links


def save_results(mk, hits, names, directory, now, watch=None, absorb=None, pre=None):
    """把今晚的掃描結果存成 JSON，網頁 app 的『今日推薦』直接讀它"""
    info = {r.sym: (r.industry, r.market) for r in directory.itertuples()}
    out = {
        "generated_at": f"{now:%Y-%m-%d %H:%M}",
        "data_date": f"{mk['date']:%Y-%m-%d}",
        "layer": mk["layer"], "layer_name": mk["layer_name"],
        "active": list(mk["active"]), "risk": bool(mk["risk"]),
        "stab_ok": bool(mk["stab_ok"]),
        "hits": {},
    }
    for name in ORDER:
        rows = []
        for h in hits[name]:
            code = h["sym"].split(".")[0]
            ind, mkt = info.get(h["sym"], ("", ""))
            rows.append({
                "code": code, "name": names.get(h["sym"], code), "suffix": "." + h["sym"].split(".")[1],
                "industry": ind or "", "market": mkt or "",
                "close": round(h["r"]["close"], 2), "key": h["plan"]["key"], "stop1": h["plan"]["stop1"],
                "target": h["plan"]["targets"][0][1], "turnover": round(h["turnover"]), "score": h["score"],
                "broke": bool(name == "flag" and h["r"].get("status", "").startswith("已突破")),
                **({"also": h["also"]} if h.get("also") else {}),
                **({"vl_low": h["vl"]["level"], "vl_date": f"{h['vl']['date']:%m/%d}", "vl_vol": round(h["vl"]["vol"]),
                    "vl_side": h["vl"]["side"]} if h.get("vl") else {}),
            })
        out["hits"][name] = rows
    out["watch"] = {}
    for name, items in (watch or {}).items():
        rows = []
        for h in items:
            code = h["sym"].split(".")[0]
            ind, mkt = info.get(h["sym"], ("", ""))
            rows.append({"code": code, "name": names.get(h["sym"], code), "suffix": "." + h["sym"].split(".")[1],
                         "industry": ind or "", "market": mkt or "", "close": round(h["r"]["close"], 2),
                         "chase": h["plan"]["e"], "stop1": h["plan"]["stop1"], "target": h["plan"]["targets"][0][1],
                         "key": h["plan"]["key"], "days": int(h["r"].get("n_cons", 0)), "turnover": round(h["turnover"]), "score": h["score"],
                         **({"prev": round(h["prev"], 2)} if h.get("prev") else {})})
        out["watch"][name] = rows
    out["absorb"] = []
    for h in (absorb or []):
        code = h["sym"].split(".")[0]
        ind, mkt = info.get(h["sym"], ("", ""))
        r = h["r"]
        out["absorb"].append({"code": code, "name": names.get(h["sym"], code), "suffix": "." + h["sym"].split(".")[1],
                              "industry": ind or "", "market": mkt or "", "close": round(r["close"], 2),
                              "low": round(r["low"], 2), "high": round(r["high"], 2), "kind": r["kind"],
                              "vol_mult": round(r["vol_mult"], 1), "chg": round(r["chg"] * 100, 2),
                              "turnover": round(h["turnover"]), "score": h["score"],
                              "avg": h.get("avg"), "poc": h.get("poc"),
                              "sup": round(h["r"]["sup"], 2) if h["r"].get("sup") is not None else None,   # 大量支撐（盤中跌破＝承接失敗）
                              **({"pre": True} if code in (pre or set()) else {})})
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    base = {}                                          # 全市場基準：盤中 13:00 恐慌承接預判用（20 日均量、昨收）
    for sym, (mav, prev, lv) in ABSORB_BASE.items():
        code, sfx = sym.split(".")[0], "." + sym.split(".")[1]
        row = {"s": sfx, "n": names.get(sym, code), "m": round(mav), "p": round(prev, 2)}
        if lv:                                         # 日線三層數字：h=近19日最高、l=近19日最低、x=65日大量日最低（支撐）
            row.update({"h": round(lv["hi19"], 2), "l": round(lv["lo19"], 2),
                        "x": round(lv["sup"], 2) if lv["sup"] is not None else None})
        base[code] = row
    with open(BASE_FILE, "w", encoding="utf-8") as f:
        json.dump({"date": f"{mk['date']:%Y-%m-%d}", "stocks": base}, f, ensure_ascii=False, separators=(",", ":"))


def wait_market(now, max_wait_min=40, step_min=5):
    """收盤後資料不一定馬上更新：資料日不是今天，就每 5 分鐘再抓一次，最多等 40 分鐘。
    等不到就用最新的（報告會標示「資料為最近交易日」）。週末不等"""
    import time
    tries = max_wait_min // step_min
    for i in range(tries + 1):
        mk = st.market_summary(fetch_market())
        if mk["date"].date() == now.date() or now.weekday() >= 5 or i == tries:
            return mk
        print(f"大盤資料還是 {mk['date']:%m/%d}，{step_min} 分鐘後再抓（第 {i + 1} 次）")
        time.sleep(step_min * 60)
        now = datetime.now(ZoneInfo("Asia/Taipei"))


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    ratios = cfg.TRANCHES

    if now.weekday() < 5 and now.hour * 60 + now.minute < 15 * 60:
        # 收盤前手動執行：大盤資料還是前一個交易日，等也等不到，只會白等 40 分鐘再逾時。
        # 而且存下的結果會讓閘門以為「今天已掃描」，15:30 的正式排程就會被跳過。所以直接結束、不存結果。
        print("現在還沒收盤（15:00 前），大盤資料是前一個交易日，這次不掃描、不存結果。請 15:30 後再手動執行。", flush=True)
        return

    if now.weekday() == 0:       # 每週一：掃描前先附上說明書（新加入的人才看得到）
        try:
            from guide import build
            send_telegram(build())
        except Exception as e:
            print("說明書傳送失敗：", repr(e))

    try:
        mk = wait_market(now)
    except Exception as e:
        send_telegram([f"🌙 *台股夜間掃描*\n⚠️ 大盤資料抓取失敗\n今晚無法掃描：{e}"])
        raise SystemExit(str(e))

    directory = get_directory()
    directory = directory.assign(sym=directory["code"] + directory["suffix"])
    names = dict(zip(directory["sym"], directory["name"]))
    import time as _t
    t0 = _t.time()
    prices, failed = fetch_prices(list(directory["sym"]))
    print(f"個股下載完成，用時 {(_t.time() - t0) / 60:.1f} 分鐘", flush=True)
    t0 = _t.time()
    prices, _ = apply_twse_volume(prices, list(mk["df"].index[-70:]))   # 上市股票的成交量改用證交所官方（跟券商一致）
    print(f"證交所成交量處理用時 {(_t.time() - t0) / 60:.1f} 分鐘", flush=True)
    hits, liquid, stale, funnel, watch, absorb = scan(prices, mk, ratios)

    print("\n".join(funnel_lines(funnel, liquid)))   # 永遠寫進 Actions 紀錄，方便檢查
    pre = load_pre1300(mk)
    try:
        save_results(mk, hits, names, directory, now, watch, absorb, pre)
    except Exception as e:
        print("掃描結果存檔失敗：", repr(e))

    data_date = mk["date"]
    head = [
        "🌙 *台股夜間掃描*" + ("　🔬診斷模式" if DIAG else ""),
        f"{now:%m/%d}（{WEEKDAY[now.weekday()]}）{now:%H:%M}　資料日 {data_date:%m/%d}",
    ]
    if data_date.date() != now.date():
        head.append("📌 資料為最近交易日")

    parts = ["\n".join(head) + "\n" + LINE, market_block(mk) + "\n" + LINE]

    active = mk["active"]
    shown = False
    if active or DIAG:
        for name in ORDER:
            if not hits[name]:
                continue
            if name in active:
                parts.append(strategy_block(name, hits[name], names, mk["risk"]))
                shown = True
            elif DIAG:
                parts.append(strategy_block(name, hits[name], names, False, inactive_note(mk)))
                shown = True
        if not shown:
            parts.append("今晚沒有符合的股票。")
    for name in ("pullback", "flag"):              # 明日追價名單（大盤啟用這套策略時才推播）
        if watch[name] and (name in active or DIAG):
            parts.append(watch_block(name, watch[name], names, "" if name in active else inactive_note(mk),
                                     icon="🎯" if name in active else "👀"))
    if absorb:                                     # 恐慌承接觀察名單：不管大盤層級，只供追蹤
        parts.append(absorb_block(absorb, names, "" if active else inactive_note(mk), pre))
    # 只有出問題時才顯示提醒
    warn = []
    if directory.attrs.get("fallback"):
        warn.append("⚠️ 官方股票清單抓取失敗\n只掃描備用的幾檔")
    elif failed and len(failed) > 0.15 * len(directory):
        warn.append(f"⚠️ 有 {len(failed)} 檔沒抓到資料\n結果可能不完整")
    if warn:
        parts.append("\n".join(warn))

    if parts[-1].endswith(LINE):       # 後面沒有內容時，不要留一條多餘的分隔線
        parts[-1] = parts[-1][:-len(LINE)].rstrip()
    send_telegram(parts)

    # 每天都傳一張大盤線圖；空頭層級會多標出前低、爆量下跌日、近 3 日
    try:
        from chart_png import market_chart
        send_photo(market_chart(mk))
    except Exception as e:
        print("線圖產生失敗：", repr(e))


if __name__ == "__main__":
    run()
