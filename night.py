"""night.py — 夜間個股掃描（台灣時間 22:30）
流程：加權指數 → 判斷大盤五層 → 依層級決定啟用哪些策略 → 掃描全上市櫃 → Telegram 通知。
沒有符合的股票也會發訊息（確認系統有在跑）。
所有策略數字都在 config.py。
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import config as cfg
import strategies as st
from data import fetch_market, fetch_prices, get_directory
from notify import send_telegram

MAX_LIST = 12   # 每套策略最多列出幾檔（依 20 日均成交金額由大到小）
WEEKDAY = "一二三四五六日"
LINE = "━━━━━━━━━━━━━━"
FW = "　"
LAYER_ICON = {0: "⚪", 1: "🟢", 2: "🟢", 3: "🟡", 4: "🔴", 5: "🔴"}
ORDER = ["pullback", "flag", "breakout", "dry"]
TITLES = {"pullback": "📈 創高拉回", "flag": "🚀 噴出後整理再攻",
          "breakout": "🌱 盤整後噴出", "dry": "🛡 量縮抗跌"}


def pad(name, n=4):
    name = name[:n]
    return name + FW * (n - len(name))


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
    tbl = [
        f"加權指數 {mk['close']:>9,.0f}",
        f"MA5 {ma[5]:>8,.0f}  MA10 {ma[10]:>8,.0f}",
        f"MA20{ma[20]:>8,.0f}  MA60 {ma[60]:>8,.0f}",
    ]
    out = [head, code_block(tbl)]
    if mk["layer"] in (4, 5):
        s = mk["stab"]
        if s is None:
            out.append("🧱 止跌確認：資料不足")
        elif s["vol_missing"]:
            out.append("🧱 止跌確認：❌ 大盤成交量取不到，無法確認")
        else:
            out.append(
                f"🧱 止跌確認：{'✅ 通過' if s['ok'] else '❌ 未通過'}\n"
                f"{'✅' if s['no_break'] else '❌'} 3 日不破前低：近 3 日最低 {s['recent_low']:,.0f}／前低 {s['prior_low']:,.0f}\n"
                f"{'✅' if s['vol_ok'] else '❌'} 量能回來：近 3 日最大量 {s['recent_vol'] / 1e8:.2f} 億股／爆量下跌日 {s['dump_vol'] / 1e8:.2f} 億股"
            )
    if mk["active"]:
        names = "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"])
        out.append(f"✅ 啟用策略：{names}")
        if mk["risk"]:
            out.append("⚠️ 第 3 層方向未明，請標示風險、縮小部位")
    else:
        out.append("🛑 沒有啟用的策略：空頭尚未止跌確認，不新進場")
    return "\n".join(out)


# ---------- 掃描 ----------
def scan(prices, mk, ratios):
    """回傳 (命中 {策略: [項目]}, 流動性合格檔數, 因資料過舊略過檔數)"""
    hits = {k: [] for k in ORDER}
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
                if r is not None and r["ok"]:
                    plan = st.plan_for(name, r, ratios)
                    hits[name].append({"sym": sym, "r": r, "plan": plan, "turnover": r["turnover"]})
        except Exception as e:   # 單一股票出錯不影響整體
            print(f"{sym} 分析失敗：{e!r}")
    for k in hits:
        hits[k].sort(key=lambda x: -x["turnover"])
    return hits, liquid, stale


def strategy_block(name, items, names, risk):
    title = f"*{TITLES[name]}*　{len(items)} 檔" + ("　⚠️標示風險" if risk else "")
    lines = []
    for h in items[:MAX_LIST]:
        code = h["sym"].split(".")[0]
        mark = "↑" if (name == "flag" and h["r"].get("status", "").startswith("已突破")) else " "
        p = h["plan"]
        lines.append(f"{mark}{code:<4} {pad(names.get(h['sym'], code))} "
                     f"{fp(h['r']['close']):>5} {fp(p['key']):>5} {fp(p['stop1']):>5} {fp(p['targets'][0][1]):>5}")
    more = f"\n（還有 {len(items) - MAX_LIST} 檔未列出）" if len(items) > MAX_LIST else ""
    return title + "\n" + code_block(lines) + more


def run():
    t0 = time.time()
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
    hits, liquid, stale = scan(prices, mk, ratios)

    data_date = mk["date"]
    head = [
        "🌙 *台股夜間掃描*",
        f"{now:%m/%d}（{WEEKDAY[now.weekday()]}）{now:%H:%M}　資料日 {data_date:%m/%d}",
    ]
    if data_date.date() != now.date():
        head.append("📌 資料日不是今天（可能休市或資料尚未更新），以下為最近交易日")

    parts = ["\n".join(head) + "\n" + LINE, market_block(mk) + "\n" + LINE]

    active = mk["active"]
    shown = False
    if active:
        parts.append("欄位：代號 名稱 現價 突破價 停損 目標（↑＝已突破）")
        for name in ORDER:
            if name in active and hits[name]:
                parts.append(strategy_block(name, hits[name], names, mk["risk"]))
                shown = True
        if not shown:
            parts.append("今晚沒有符合的股票。")
    extra = [f"{cfg.STRATEGY_NAMES[n]} {len(hits[n])} 檔" for n in ORDER if n not in active and hits[n]]
    if extra:
        parts.append("型態符合但大盤層級不適用（不列出）：" + "、".join(extra))

    mins = (time.time() - t0) / 60
    foot = f"掃描 {len(prices)} 檔｜流動性合格 {liquid} 檔｜耗時 {mins:.1f} 分"
    if directory.attrs.get("fallback"):
        foot += "\n⚠️ 官方股票清單抓取失敗，只掃描備用的幾檔"
    elif failed and len(failed) > 0.15 * len(directory):
        foot += f"\n⚠️ 有 {len(failed)} 檔沒抓到資料，結果可能不完整"
    if stale:
        foot += f"\n略過 {stale} 檔（停牌或資料未更新）"
    parts.append(LINE + "\n" + foot)

    send_telegram(parts)


if __name__ == "__main__":
    run()
