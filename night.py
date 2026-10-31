"""night.py — 夜間個股掃描（台灣時間 22:30）
流程：加權指數 → 判斷大盤五層 → 依層級決定啟用哪些策略 → 掃描全上市櫃 → Telegram 通知。
沒有符合的股票也會發訊息（確認系統有在跑）。
所有策略數字都在 config.py。

診斷模式：手動執行時把 diag 勾起來（或環境變數 DIAG=true），會不管大盤層級、
列出四套策略各自符合的股票，並顯示每個條件各有幾檔通過，用來判斷哪個條件太嚴格。
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import config as cfg
import strategies as st
from data import fetch_market, fetch_prices, get_directory, turnover_audit
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


def code_block(lines):
    # 行尾補「看不見的空格」，避免 Telegram 右上角的 </> 複製鈕蓋到最後一個字
    return "```\n" + "\n".join(ln + "⠀" * 3 for ln in lines) + "\n```"


# ---------- 大盤區塊 ----------
def market_block(mk):
    ma = mk["ma"]
    head = f"🌐 *大盤｜第 {mk['layer']} 層・{mk['layer_name']}* {LAYER_ICON[mk['layer']]}"

    def two(l1, v1, l2="", v2=None):
        s = f"{l1:<5}{v1:>8,.0f}"
        return s + (f"   {l2:<5}{v2:>8,.0f}" if v2 is not None else "")

    # 全部是半形字，所以一定對齊
    tbl = [two("Close", mk["close"], "MA5", ma[5]),
           two("MA10", ma[10], "MA20", ma[20]),
           two("MA60", ma[60])]
    out = [head, code_block(tbl)]

    if mk["layer"] in (4, 5):
        s = mk["stab"]
        if s is None:
            out.append("🧱 止跌確認：資料不足")
        elif s["vol_missing"]:
            out.append("🧱 止跌確認：❌ 無法確認（大盤成交金額抓不到）")
        else:
            ok = lambda b: "✅" if b else "❌"
            ratio = s["recent_vol"] / s["dump_vol"] * 100 if s["dump_vol"] else 0
            out.append(f"🧱 *止跌確認*　{'✅ 通過' if s['ok'] else '❌ 未通過'}（兩項都要過）")
            out.append(
                f"{ok(s['no_break'])} 1. 近 3 日沒有再創新低\n"
                f"　近 3 日最低 {s['recent_low']:,.0f}（{s['recent_low_date']:%m/%d}）"
                f"{'高於' if s['no_break'] else '低於或等於'}前低 {s['prior_low']:,.0f}（{s['prior_low_date']:%m/%d}）\n"
                f"{ok(s['vol_ok'])} 2. 近 3 日有大量承接\n"
                f"　近 3 日最大量 = 爆量下跌日（{s['dump_date']:%m/%d}）的 {ratio:.0f}%，需要 100% 以上"
            )
    if mk["active"]:
        names = "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"])
        out.append(f"✅ 啟用策略：{names}" + ("\n⚠️ 第 3 層方向未明，請縮小部位" if mk["risk"] else ""))
    else:
        out.append("🛑 沒有啟用的策略，不新進場")
    return "\n".join(out)


# ---------- 掃描 ----------
def scan(prices, mk, ratios):
    """回傳 (命中 {策略: [項目]}, 流動性合格檔數, 資料過舊略過檔數, 各條件通過檔數)"""
    hits = {k: [] for k in ORDER}
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
                    hits[name].append({"sym": sym, "r": r, "plan": plan, "turnover": r["turnover"]})
        except Exception as e:   # 單一股票出錯不影響整體
            print(f"{sym} 分析失敗：{e!r}")
    for k in hits:
        hits[k].sort(key=lambda x: -x["turnover"])
    return hits, liquid, stale, funnel


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


def _md(t):
    """名稱裡若有 Telegram Markdown 的特殊符號就跳脫"""
    for ch in "_*`[":
        t = t.replace(ch, "\\" + ch)
    return t


def strategy_block(name, items, names, risk, note=""):
    title = f"*{TITLES[name]}*　{len(items)} 檔" + ("　⚠️標示風險" if risk else "") + note
    cards = []
    for h in items[:MAX_LIST]:
        code = h["sym"].split(".")[0]
        p = h["plan"]
        broke = name == "flag" and h["r"].get("status", "").startswith("已突破")
        cards.append(
            f"*{code} {_md(names.get(h['sym'], code))}*　現價 {fp(h['r']['close'])}" + ("　↑已突破" if broke else "") + "\n"
            f"　突破 {fp(p['key'])}　停損 {fp(p['stop1'])}　目標 {fp(p['targets'][0][1])}"
        )
    more = f"\n（還有 {len(items) - MAX_LIST} 檔未列出）" if len(items) > MAX_LIST else ""
    return title + "\n\n" + "\n\n".join(cards) + more


def run():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    ratios = cfg.TRANCHES

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
    hits, liquid, stale, funnel = scan(prices, mk, ratios)

    print("\n".join(funnel_lines(funnel, liquid)))   # 永遠寫進 Actions 紀錄，方便檢查

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
                parts.append(strategy_block(name, hits[name], names, False, "　（大盤未啟用）"))
                shown = True
        if not shown:
            parts.append("今晚沒有符合的股票。")
    if DIAG:
        try:
            parts.append("🔬 *成交額核對（暫時，找看盤軟體的算法）*\n" + "\n".join(turnover_audit()))
        except Exception as e:
            print("成交額對照失敗：", repr(e))
        parts.append("🔬 *各條件通過檔數*\n" + "\n".join(funnel_lines(funnel, liquid)))

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
        cap = "加權指數 K 線、均線與成交金額"
        if mk["layer"] in (4, 5) and mk["stab"]:
            cap += "｜黃色區 = 近 3 日｜虛線 = 止跌確認的門檻"
        send_photo(market_chart(mk), cap)
    except Exception as e:
        print("線圖產生失敗：", repr(e))


if __name__ == "__main__":
    run()
