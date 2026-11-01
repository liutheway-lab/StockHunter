"""night.py — 夜間個股掃描（台灣時間 22:30）
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
from data import fetch_market, fetch_prices, get_directory
from notify import send_photo, send_telegram

MAX_LIST = 12   # 每套策略最多列出幾檔（依 20 日均成交金額由大到小）
WEEKDAY = "一二三四五六日"
LINE = "━━━━━━━━━━━━━━"
LAYER_ICON = {0: "⚪", 1: "🟢", 2: "🟢", 3: "🟡", 4: "🔴", 5: "🔴"}
ORDER = ["pullback", "flag", "breakout", "dry"]
TITLES = {"pullback": "📈 創高拉回", "flag": "🚀 噴出後整理再攻",
          "breakout": "🌱 盤整後噴出", "dry": "🛡 量縮抗跌"}
DIAG = os.getenv("DIAG", "").strip().lower() in ("1", "true", "yes")


def fp(x):
    """價格格式：>=1000 不顯示小數，>=100 一位，其餘兩位"""
    if x >= 1000:
        return f"{x:,.0f}"
    if x >= 100:
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

    def two(l1, v1, l2="", v2=None):
        s = f"{l1:<5}{v1:>8,.0f}"
        return s + (f"   {l2:<5}{v2:>8,.0f}" if v2 is not None else "")

    # 全部是半形字，所以一定對齊
    tbl = [two("MA5", ma[5], "MA10", ma[10]),
           two("MA20", ma[20], "MA60", ma[60])]
    cc = mk["df"]["Close"]
    chg = float(cc.iloc[-1] - cc.iloc[-2]) if len(cc) > 1 else 0.0
    pct = chg / float(cc.iloc[-2]) * 100 if len(cc) > 1 else 0.0
    arrow = "🔴 ▲" if chg > 0 else "🟢 ▼" if chg < 0 else "⚪ ─"          # 台股習慣：紅漲、綠跌
    out = [head, f"收盤　{mk['close']:,.0f}\n{arrow} {abs(chg):,.0f} 點（{pct:+.2f}%）", code_block(tbl)]

    if mk["layer"] in (4, 5):
        s = mk["stab"]
        if s is None:
            out.append("🧱 止跌確認：資料不足")
        elif s["vol_missing"]:
            out.append("🧱 止跌確認：❌ 無法確認（大盤成交金額抓不到）")
        else:
            ok = lambda b: "✅" if b else "❌"
            ratio = s["recent_vol"] / s["dump_vol"] * 100 if s["dump_vol"] else 0
            da, nd = s["days_after"], s["need_days"]
            state = "✅ 通過" if s["ok"] else ("⏳ 觀察中" if s.get("watching") else "❌ 未通過")
            out.append(f"🧱 *止跌確認*　{state}\n下面 ① ② 都過才算止跌")
            cmp_ = lambda b, ge="≥", lt="<": ge if b else lt
            low_ok = "✅" if s["no_break"] else "⏳" if da == 0 else "❌"
            sig = s["vol_ok"] and s["shape_ok"]
            blk1 = [f"量能　{ratio:>4.0f}%　要 ≥ 100%　{ok(s['vol_ok'])}"]
            if da == 0:
                blk1.append("不破低　　　待觀察　　⏳")
            else:
                blk1.append(f"不破低 {s['recent_low']:,.0f} 要 > {s['prior_low']:,.0f}　{low_ok}")
            blk2 = [f"下影線 {s['shadow'] * 100:>4.0f}%　要 ≥ {cfg.STAB_SHADOW * 100:.0f}%　{ok(s['shadow_ok'])}",
                    "",
                    f"長　紅 {s['body'] * 100:>4.0f}%　要 ≥ {cfg.STAB_RED_BODY * 100:.0f}%　{ok(s['red_ok'])}"]
            out.append(f"爆量日：{s['dump_date']:%m/%d}\n（這波成交額最大的下跌日）\n"
                       f"低點日：{s['signal_date']:%m/%d}\n（最近 3 天股價最低的那天）")
            out.append("*① 量能・不破低*　兩項都要 ✅\n量能＝低點日成交額\n　　　÷爆量日成交額\n不破低＝低點日之後的最低點\n　　　　要高於低點日最低點")
            out.append(code_block(blk1, lang="text"))
            if da == 0:
                out.append("⏳ 今天為近 3 日最低點\n待後續觀察")
            out.append("*② K棒*　二擇一，有一項 ✅ 就過")
            out.append(code_block(blk2, lang="text"))
            out.append("下影線＝下影線占當天高低差的比例\n長紅＝紅K實體占當天整根K棒\n　　　的比例（收黑為負數）")
            if s["ok"]:
                out.append("📌 日線止跌走穩\n先以 2～3 成資金佈局\n不猜底、不貪心")
    if mk["active"]:
        names = "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"])
        out.append(f"✅ 啟用策略：{names}" + ("\n⚠️ 第 3 層方向未明，請縮小部位" if mk["risk"] else ""))
    else:
        out.append("🔴 尚未止跌，不建議建立多頭部位" if mk["layer"] in (4, 5) else "🔴 沒有啟用的策略")
    return "\n".join(out)


# ---------- 掃描 ----------
def scan(prices, mk, ratios):
    """回傳 (命中 {策略: [項目]}, 流動性合格檔數, 資料過舊略過檔數, 各條件通過檔數)"""
    hits = {k: [] for k in ORDER}
    watch = {"pullback": [], "flag": []}       # 明日追價名單
    funnel = {k: {"checks": {}, "ok": 0} for k in ORDER}
    liquid = stale = 0
    last_date = mk["date"]
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
            results = {
                "pullback": st.check_pullback(df, i, ratios),
                "flag": st.check_flag(df, i),
                "breakout": st.check_breakout(df, i),
                "dry": st.check_dry(df, i, mk["df"]),
            }
            for name, r in results.items():
                if r is None:
                    continue
                for cname, cok, _ in r["checks"]:
                    funnel[name]["checks"][cname] = funnel[name]["checks"].get(cname, 0) + (1 if cok else 0)
                if r["ok"]:
                    funnel[name]["ok"] += 1
                    plan = st.plan_for(name, r, ratios)
                    hits[name].append({"sym": sym, "r": r, "plan": plan, "turnover": r["turnover"], "vl": st.vol_low(df)})
            for name in watch:                    # 今天還沒攻擊、但盤整條件已成立 → 明天的追價點
                if results[name] is not None and results[name]["ok"]:
                    continue
                w = st.watch_tomorrow(raw, name)
                if w:
                    plan = st.attack_plan(w, entry=w["chase_px"])
                    watch[name].append({"sym": sym, "r": w, "plan": plan, "turnover": w["turnover"]})
        except Exception as e:   # 單一股票出錯不影響整體
            print(f"{sym} 分析失敗：{e!r}")
    for k in hits:
        hits[k].sort(key=lambda x: -x["turnover"])
    for k in watch:
        watch[k].sort(key=lambda x: -x["turnover"])
    return hits, liquid, stale, funnel, watch


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
        labels.append(f"{code} {names.get(h['sym'], code)}" + ("↑" if broke else "") + (f" {vl['side']}" if vl else ""))
    W = max([_w("代號 名稱")] + [_w(x) for x in labels])
    cols = [("現價", 3), ("突破價", 1), ("出場價", 1), ("目標", 1)]          # 表頭縮短，整行才不會被切成兩行
    lines = [_padr("代號 名稱", W) + "".join(" " * pad + c for c, pad in cols)]
    for h, lab in zip(shown, labels):
        p = h["plan"]
        nums = [h["r"]["close"], p["key"], p["stop1"], p["targets"][0][1]]
        lines.append(_padr(lab, W) + "".join(f" {fp(v):>5}" for v in nums))
    links = "　".join(tv_link(h["sym"]) for h in shown)
    more = f"\n（還有 {len(items) - MAX_LIST} 檔未列出）" if len(items) > MAX_LIST else ""
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
    legend = ("\n壓＝第1大量日最高點在現價上"
              "\n撐＝第1大量日最低點在現價下"
              "\n（65 日內成交量最大的那天）")
    return title + "\n" + code_block(lines, header=True) + rr_txt + legend + more + "\n📊 TradingView 線圖：" + links


def watch_block(name, items, names, note=""):
    """明日追價名單：明天盤中漲 4% 且突破盤整高點，就是追價點"""
    title = f"*👀 明日追價名單・{cfg.STRATEGY_NAMES[name]}*　{len(items)} 檔" + note
    shown = items[:MAX_LIST]
    labels = [f"{h['sym'].split('.')[0]} {names.get(h['sym'], h['sym'].split('.')[0])}" for h in shown]
    W = max([_w("代號 名稱")] + [_w(x) for x in labels])
    cols = [("現價", 3), ("追價點", 1), ("出場價", 1), ("目標", 1)]
    lines = [_padr("代號 名稱", W) + "".join(" " * pad + c for c, pad in cols)]
    for h, lab in zip(shown, labels):
        p = h["plan"]
        nums = [h["r"]["close"], p["e"], p["stop1"], p["targets"][0][1]]
        lines.append(_padr(lab, W) + "".join(f" {fp(v):>5}" for v in nums))
    more = f"\n（還有 {len(items) - MAX_LIST} 檔未列出）" if len(items) > MAX_LIST else ""
    legend = (f"\n追價點＝明天盤中要漲到的價格"
              f"\n（漲 {cfg.CHASE_GAIN * 100:.0f}% 且突破盤整高點）"
              f"\n盤整 {cfg.ATTACK_MIN_CONSOL}～{cfg.WATCH_MAX_CONSOL} 天、離前高 ≤ {cfg.ATTACK_MAX_DEPTH * 100:.0f}%"
              f"\n追價點離現價最多 {cfg.CHASE_MAX_DIST * 100:.0f}%"
              f"\n盤中看強弱再決定追不追")
    links = "　".join(tv_link(h["sym"]) for h in shown)
    return title + "\n" + code_block(lines, header=True) + legend + more + "\n📊 TradingView 線圖：" + links


RESULT_FILE = "scan_results.json"


def save_results(mk, hits, names, directory, now, watch=None):
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
                "target": h["plan"]["targets"][0][1], "turnover": round(h["turnover"]),
                "broke": bool(name == "flag" and h["r"].get("status", "").startswith("已突破")),
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
                         "key": h["plan"]["key"], "days": int(h["r"].get("n_cons", 0)), "turnover": round(h["turnover"])})
        out["watch"][name] = rows
    with open(RESULT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    ratios = cfg.TRANCHES

    if now.weekday() == 0:       # 每週一：掃描前先附上說明書（新加入的人才看得到）
        try:
            from guide import build
            send_telegram(build())
        except Exception as e:
            print("說明書傳送失敗：", repr(e))

    try:
        mkt = fetch_market()
        mk = st.market_summary(mkt)
    except Exception as e:
        send_telegram([f"🌙 *台股夜間掃描*\n⚠️ 大盤資料抓取失敗，今晚無法掃描：{e}"])
        raise SystemExit(str(e))

    directory = get_directory()
    directory = directory.assign(sym=directory["code"] + directory["suffix"])
    names = dict(zip(directory["sym"], directory["name"]))
    prices, failed = fetch_prices(list(directory["sym"]))
    hits, liquid, stale, funnel, watch = scan(prices, mk, ratios)

    print("\n".join(funnel_lines(funnel, liquid)))   # 永遠寫進 Actions 紀錄，方便檢查
    try:
        save_results(mk, hits, names, directory, now, watch)
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
            parts.append(watch_block(name, watch[name], names, "" if name in active else inactive_note(mk)))
    # 只有出問題時才顯示提醒
    warn = []
    if directory.attrs.get("fallback"):
        warn.append("⚠️ 官方股票清單抓取失敗，只掃描備用的幾檔")
    elif failed and len(failed) > 0.15 * len(directory):
        warn.append(f"⚠️ 有 {len(failed)} 檔沒抓到資料，結果可能不完整")
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
