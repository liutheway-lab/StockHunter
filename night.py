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
            # 兩列的數字欄位寬度一樣、標籤都是 3 個中文字，所以會對齊
            out.append(code_block([
                f"不破低　{s['recent_low']:>6,.0f} {'>' if s['no_break'] else '≤'} {s['prior_low']:>6,.0f}　{ok(s['no_break'])}",
                f"量承接　{ratio:>5.0f}% {'≥' if s['vol_ok'] else '<'} {100:>5}%　{ok(s['vol_ok'])}",
            ]))
            out.append(f"不破低＝近 3 日最低 vs 前低（{s['prior_low_date']:%m/%d}）\n"
                       f"量承接＝近 3 日最大量 vs 爆量下跌日（{s['dump_date']:%m/%d}）")
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


def strategy_block(name, items, names, risk, note=""):
    title = f"*{TITLES[name]}*　{len(items)} 檔" + ("　⚠️標示風險" if risk else "") + note
    shown = items[:MAX_LIST]
    labels = []
    for h in shown:
        code = h["sym"].split(".")[0]
        broke = name == "flag" and h["r"].get("status", "").startswith("已突破")
        labels.append(f"{code} {names.get(h['sym'], code)}" + ("↑" if broke else ""))
    W = max([_w("代號 名稱")] + [_w(x) for x in labels])
    cols = ["現價", "突破價", "停損價", "目標價"]
    lines = [_padr("代號 名稱", W) + "".join(" " * (6 - round(_w(c))) + c for c in cols)]
    for h, lab in zip(shown, labels):
        p = h["plan"]
        nums = [h["r"]["close"], p["key"], p["stop1"], p["targets"][0][1]]
        lines.append(_padr(lab, W) + "".join(f" {fp(v):>5}" for v in nums))
    links = "　".join(tv_link(h["sym"]) for h in shown)
    more = f"\n（還有 {len(items) - MAX_LIST} 檔未列出）" if len(items) > MAX_LIST else ""
    return title + "\n" + code_block(lines) + more + "\n📊 TradingView 線圖：" + links


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
