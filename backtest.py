"""backtest.py — 回測：用跟夜間掃描完全相同的判斷程式，逐日往回推，模擬進出場

跑法（GitHub Actions 手動觸發 backtest.yml，或本機）：
    python backtest.py                 全部流動性合格的股票（要跑一陣子）
    python backtest.py --sample 300    隨機抽 300 檔（快速版，先確認程式沒問題）

交易模擬規則（可在下面「交易規則」區調整）：
  1. 訊號出現的「隔天開盤價」進第 1 筆（不用訊號當天收盤，避免高估）
  2. 出場價用盤中最低價觸價；若隔天直接跳空跌破，用開盤價成交（虧損會比預期大）
  3. 第 2、3 筆：價格漲到突破價、突破價+8% 才買；沒到就不買
  4. 同一天同時碰到出場價和目標價 → 一律先算出場（保守）
  5. 達到目標價全部賣出；持有超過 MAX_HOLD 個交易日還沒出場，用收盤價出場
  6. 手續費、證交稅：來回 cfg.FEE_RT
  7. 同一檔股票同一套策略，有部位時不重複進場
四種出場方式同時比較：
  ladder_avg  三段進場，第 2 筆買進後出場價拉到累計均價（網頁 app 現在的規則）
  ladder_stop2 三段進場，第 2 筆買進後出場價拉到「突破價下 5%」
  ladder_stop1 三段進場，出場價一直不動（第 1 筆的出場價）
  single      一次買進（全部資金第一天進場）
限制：yfinance 看不到已下市的股票，結果會偏樂觀（存活者偏差）。
"""
import argparse
import json
import math
import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

import config as cfg
import strategies as st

# ================= 交易規則 =================
MAX_HOLD = 60                       # 最久持有幾個交易日
MODES = ("ladder_avg", "ladder_stop2", "ladder_stop1", "single")
MAIN_MODE = "ladder_avg"
STRATS = ("pullback", "flag", "breakout", "dry")
FWD_DAYS = (5, 10, 20)              # 「壓／撐」預測力：看進場後 5、10、20 日報酬
SAMPLE_EVERY = 5                    # 預測力的一般樣本：每 5 個交易日抽一天
CRASHES = {                         # 崩盤期單獨看
    "2020 疫情崩盤": ("2020-02-15", "2020-04-30"),
    "2022 全年空頭": ("2022-01-01", "2022-12-31"),
    "2024/08 急殺": ("2024-07-15", "2024-09-15"),
    "2025/04 關稅急跌": ("2025-03-25", "2025-05-31"),
}
YEARS_MAIN = 5                      # 主要統計看近幾年


# ================= 大盤每日狀態 =================
def market_states(mkt):
    """每個交易日的 (層級, 啟用的策略, 是否標示風險)。跟夜間掃描同一套函式"""
    layers = st.layer_series(mkt)
    out = {}
    for i, d in enumerate(mkt.index):
        layer = int(layers.iloc[i])
        stab_ok = False
        if layer in (4, 5):
            s = st.stabilization(mkt, i)
            stab_ok = bool(s and s["ok"])
        active, risk = st.active_strategies(layer, stab_ok)
        out[d] = (layer, frozenset(active), bool(risk))
    return out


# ================= 單筆交易模擬 =================
def simulate(O, H, L, C, i, plan, mode, max_hold=MAX_HOLD, fee=None):
    """訊號在第 i 天收盤後出現，第 i+1 天開盤進場。回傳交易 dict，不能進場則回傳 None。
    O/H/L/C 是 numpy 陣列。plan 來自 strategies.plan_for。"""
    fee = cfg.FEE_RT if fee is None else fee
    n = len(C)
    j = i + 1
    if j >= n:
        return None
    stop1, target = float(plan["stop1"]), float(plan["targets"][0][1])
    p1 = float(O[j])
    if p1 <= stop1 or p1 >= target:       # 開盤就跳空到出場價下或目標價上，不進場
        return None
    if mode == "single":
        weights, levels = [1.0], [p1]
    else:
        weights = list(plan["ratios"])
        levels = [p1, float(plan["b"]), float(plan["b2"])]
    shares, cost = [], []                 # 各筆買進後的累計股數、累計金額（以總資金 = 1 計）
    sh = cs = 0.0
    stop = stop1
    reason, exit_px, d_exit = "time", None, None
    filled = 0

    def fill(k, price):
        nonlocal sh, cs, filled
        sh += weights[k] / price
        cs += weights[k]
        filled = k + 1

    fill(0, p1)
    last = min(j + max_hold - 1, n - 1)
    for d in range(j, last + 1):
        o, h, l, c = float(O[d]), float(H[d]), float(L[d]), float(C[d])
        if l <= stop:                                     # 先看出場（保守）
            exit_px = o if (d > j and o <= stop) else stop
            reason, d_exit = "stop", d
            break
        if h >= target:
            exit_px = max(o, target) if d > j else target
            reason, d_exit = "target", d
            break
        # 還沒出場：處理第 2、3 筆
        while filled < len(weights) and h >= levels[filled]:
            price = max(levels[filled], o) if d > j or levels[filled] <= o else levels[filled]
            fill(filled, price)
            if mode == "ladder_avg" and filled >= 2:
                stop = max(stop, st.tick(cs / sh, "ceil"))
            elif mode == "ladder_stop2" and filled >= 2:
                stop = max(stop, float(plan["stop2"]))
        if d == last:
            exit_px, reason, d_exit = c, "time", d
    if exit_px is None:
        exit_px, d_exit = float(C[last]), last
    pnl = sh * exit_px - cs - cs * fee
    return {"entry_idx": j, "exit_idx": d_exit, "hold": d_exit - j + 1, "tranches": filled,
            "invested": cs, "avg": cs / sh, "exit_px": exit_px, "reason": reason,
            "ret_inv": pnl / cs, "ret_plan": pnl}


# ================= 逐檔掃描 =================
def prescreen(df, ratios):
    """用向量運算先挑出「有機會符合」的日子，再交給完整判斷。
    這裡只放策略裡『一定要成立』的條件，所以不會漏掉任何訊號（verify=True 時會逐日驗證）。"""
    c, h, l, v = (df[x] for x in ("Close", "High", "Low", "Volume"))
    mav20, ema20, ema60 = df["MAV20"], df["EMA20"], df["EMA60"]
    n, k = cfg.STAB_LOOKBACK, cfg.STAB_DAYS
    t = st.norm_ratios(ratios)
    max_ratio = st.solve_max_entry_ratio(t[0], t[1])
    pre = {}
    roll_hi = h.rolling(cfg.PB_LOOKBACK + 3).max()
    pre["pullback"] = ((ema20 > ema60) & (c >= ema60 * 0.98) & (v <= mav20) & (c <= max_ratio * roll_hi))
    neck = h.shift(1).rolling(cfg.BO_PLATFORM).max()
    pre["breakout"] = ((c > neck) & (v >= cfg.BO_VOL_MULT * mav20) & (c >= ema60) & (ema20 > ema20.shift(1))
                       & (c <= (1 + cfg.BO_MAX_RISE) * l.rolling(60).min()))
    vol3 = v.rolling(k).mean()
    prior_low = l.rolling(n).min().shift(k)
    pre["dry"] = ((vol3 <= cfg.DRY_VOL_RATIO * mav20) & (l.rolling(k).min() > prior_low))
    burst = ((c / c.shift(3) - 1) >= cfg.FLAG_BURST_3D) | ((c / c.shift(1) - 1) >= cfg.FLAG_BURST_1D)
    span = cfg.FLAG_WINDOW - cfg.FLAG_MIN_CONSOL
    pre["flag"] = burst.astype(float).rolling(span).max().shift(cfg.FLAG_MIN_CONSOL + 1).fillna(0) > 0
    return {k_: v_.fillna(False).to_numpy(bool) for k_, v_ in pre.items()}


def scan_stock(sym, raw, mstate, mkt, ratios, start, verify=False):
    df = st.add_indicators(raw)
    n = len(df)
    if n < 80:
        return [], [], []
    O, H, L, C = (df[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    liq = ((df["MAV5"] >= cfg.MIN_LOTS * 1000) & (df["TURN20"] >= cfg.MIN_AVG_TURNOVER)).to_numpy()
    dates = df.index
    pre = prescreen(df, ratios)
    signals, trades, samples = [], [], []
    busy = {}
    for i in range(69, n - 1):
        if not liq[i] or dates[i] < start:
            continue
        layer, active, risk = mstate.get(dates[i], (0, frozenset(), False))
        funcs = {
            "pullback": lambda: st.check_pullback(df, i, ratios),
            "flag": lambda: st.check_flag(df, i),
            "breakout": lambda: st.check_breakout(df, i),
            "dry": lambda: st.check_dry(df, i, mkt),
        }
        results = {}
        for name, fn in funcs.items():
            if pre[name][i] or verify:
                r = fn()
                if verify and r is not None and r["ok"] and not pre[name][i]:
                    raise AssertionError(f"預先篩選漏掉了訊號：{sym} {name} {dates[i]:%Y-%m-%d}")
                results[name] = r
            else:
                results[name] = None
        vl = None
        for name, r in results.items():
            if r is None or not r["ok"]:
                continue
            if vl is None:
                vl = st.vol_low(df.iloc[:i + 1])
            side = vl["side"] if vl else ""
            plan = st.plan_for(name, r, ratios)
            sig = {"sym": sym, "strat": name, "date": dates[i], "layer": layer,
                   "active": name in active, "risk": risk, "side": side}
            for h in FWD_DAYS:
                sig[f"f{h}"] = C[i + h] / O[i + 1] - 1 if i + h < n else np.nan
            signals.append(sig)
            for mode in MODES:
                if busy.get((name, mode), -1) >= i:
                    continue
                t = simulate(O, H, L, C, i, plan, mode)
                if t is None:
                    continue
                busy[(name, mode)] = t["exit_idx"]
                trades.append({**sig, "mode": mode, "entry_date": dates[t["entry_idx"]],
                               "exit_date": dates[t["exit_idx"]], **{k: v for k, v in t.items() if k not in ("entry_idx", "exit_idx")}})
        if i % SAMPLE_EVERY == 0:                      # 一般樣本：不管有沒有訊號，看「壓／撐」之後的報酬
            if vl is None:
                vl = st.vol_low(df.iloc[:i + 1])
            if vl:
                row = {"sym": sym, "date": dates[i], "layer": layer, "side": vl["side"]}
                for h in FWD_DAYS:
                    row[f"f{h}"] = C[i + h] / O[i + 1] - 1 if i + h < n else np.nan
                samples.append(row)
    return signals, trades, samples


# ================= 統計 =================
def trade_stats(df, col="ret_inv"):
    if df is None or len(df) == 0:
        return {"n": 0}
    df = df.sort_values("exit_date")
    r = df[col].to_numpy(float)
    w, lo = r[r > 0], r[r <= 0]
    cum = np.cumsum(r)
    dd = float(np.max(np.maximum.accumulate(cum) - cum)) if len(cum) else 0.0
    streak = best = 0
    for x in r:
        streak = streak + 1 if x <= 0 else 0
        best = max(best, streak)
    return {
        "n": int(len(r)), "win": float((r > 0).mean()),
        "avg_win": float(w.mean()) if len(w) else 0.0, "avg_loss": float(lo.mean()) if len(lo) else 0.0,
        "mean": float(r.mean()), "median": float(np.median(r)),
        "pf": float(w.sum() / -lo.sum()) if len(lo) and lo.sum() < 0 else float("inf") if len(w) else 0.0,
        "hold": float(df["hold"].mean()), "max_streak": int(best), "worst": float(r.min()),
        "cum": float(cum[-1]), "maxdd": dd,
    }


def pct(x, d=1):
    return "—" if x is None or x != x else f"{x * 100:.{d}f}%"


def fwd_table(df):
    """依「壓／撐」分組的後續報酬"""
    out = {}
    for side, g in df.groupby("side"):
        row = {"n": int(len(g))}
        for h in FWD_DAYS:
            s = g[f"f{h}"].dropna()
            row[f"f{h}"] = float(s.mean()) if len(s) else None
            row[f"w{h}"] = float((s > 0).mean()) if len(s) else None
        out[side] = row
    return out


def build_report(signals, trades, samples, end_date):
    S = pd.DataFrame(signals)
    T = pd.DataFrame(trades)
    P = pd.DataFrame(samples)
    t0 = end_date - pd.DateOffset(years=YEARS_MAIN)
    rep = {"end": f"{end_date:%Y-%m-%d}", "from": f"{t0:%Y-%m-%d}", "n_signals": int(len(S)), "n_trades": int(len(T))}
    if T.empty:
        return rep, T, S, P
    main = T[T["date"] >= t0]
    # 1. 四套策略各自績效（主要出場方式）
    rep["by_strategy"] = {}
    for name in STRATS:
        g = main[(main["mode"] == MAIN_MODE) & (main["strat"] == name)]
        rep["by_strategy"][name] = {"all": trade_stats(g), "active": trade_stats(g[g["active"]]),
                                    "inactive": trade_stats(g[~g["active"]])}
    # 2. 大盤五層
    rep["by_layer"] = {}
    for name in STRATS:
        g = main[(main["mode"] == MAIN_MODE) & (main["strat"] == name)]
        rep["by_layer"][name] = {int(l): trade_stats(x) for l, x in g.groupby("layer")}
    g = main[main["mode"] == MAIN_MODE]
    rep["by_layer"]["all"] = {int(l): trade_stats(x) for l, x in g.groupby("layer")}
    # 3. 壓／撐
    Pm = P[P["date"] >= t0] if not P.empty else P
    rep["side_samples"] = fwd_table(Pm) if not Pm.empty else {}
    Sm = S[S["date"] >= t0]
    rep["side_signals"] = fwd_table(Sm) if not Sm.empty else {}
    rep["side_trades"] = {s: trade_stats(x) for s, x in g.groupby("side")}
    # 4. 出場方式比較（只看大盤啟用的訊號）
    rep["by_mode"] = {}
    for name in STRATS:
        rep["by_mode"][name] = {}
        for mode in MODES:
            x = main[(main["strat"] == name) & (main["mode"] == mode) & (main["active"])]
            rep["by_mode"][name][mode] = trade_stats(x)
    # 5. 崩盤期
    rep["crash"] = {}
    for label, (a, b) in CRASHES.items():
        x = T[(T["mode"] == MAIN_MODE) & (T["date"] >= a) & (T["date"] <= b)]
        rep["crash"][label] = {"all": trade_stats(x), "active": trade_stats(x[x["active"]])}
    return rep, T, S, P


# ================= 報告文字 =================
SHORT = {"pullback": "創高拉回", "flag": "噴後整理", "breakout": "盤整噴出", "dry": "量縮抗跌"}   # 都是 4 個字，表格才會對齊
MODE_CODE = {"ladder_avg": "A", "ladder_stop2": "B", "ladder_stop1": "C", "single": "D"}


def _row(label, s, streak=True):
    if not s.get("n"):
        return f"{label} 沒有交易"
    out = f"{label}{s['n']:>5}{pct(s['win'], 0):>5}{pct(s['avg_win']):>7}{pct(s['avg_loss']):>7}{pct(s['mean']):>7}"
    return out + (f"{s['max_streak']:>4}" if streak else "")


def format_report(rep):
    if "by_strategy" not in rep:
        return ["📉 回測完成，但整段期間沒有產生任何交易（請檢查資料）"]
    msgs = []
    msgs.append(f"📊 *回測報告*　{rep['from']} ～ {rep['end']}\n"
                f"股票 {rep.get('universe', '?')} 檔　訊號 {rep['n_signals']:,}　模擬交易 {rep['n_trades']:,}\n"
                f"規則：訊號隔天開盤進場；同日先算出場；來回成本 {cfg.FEE_RT * 100:.1f}%\n"
                "⚠️ 看不到已下市的股票，結果會偏樂觀\n"
                "表內：賺＝平均賺的那幾筆、賠＝平均賠的那幾筆、期望＝每筆平均報酬（以實際投入計）")
    hdr = "策略　　筆數 勝率   賺    賠   期望 連虧"
    lines = [hdr] + [_row(SHORT[n], rep["by_strategy"][n]["active"]) for n in STRATS]
    msgs.append("*① 四套策略（大盤啟用該策略時才進場）*\n```\n" + "\n".join(lines) + "\n```")
    lines = [hdr] + [_row(SHORT[n], rep["by_strategy"][n]["inactive"]) for n in STRATS]
    msgs.append("*　對照：大盤沒有啟用時硬進場*\n```\n" + "\n".join(lines) + "\n```")
    lines = ["層級　筆數 勝率   賺    賠   期望 連虧"]
    for l in range(1, 6):
        lines.append(_row(f"第{l}層", rep["by_layer"]["all"].get(l, {"n": 0})))
    legend = "　".join(f"{l}={cfg.LAYER_NAMES[l]}" for l in range(1, 6))
    msgs.append("*② 大盤五層（四套策略合計，不看有沒有啟用）*\n```\n" + "\n".join(lines) + "\n```\n" + legend)
    lines = ["分組　 樣本數   5日   10日  20日"]
    for side in ("壓", "撐"):
        s = rep["side_samples"].get(side)
        if s:
            lines.append(f"{side}　{s['n']:>8,}{pct(s['f5']):>7}{pct(s['f10']):>7}{pct(s['f20']):>7}")
    lines.append("— 只看有策略訊號的 —")
    for side in ("壓", "撐"):
        s = rep["side_signals"].get(side)
        if s:
            lines.append(f"{side}　{s['n']:>8,}{pct(s['f5']):>7}{pct(s['f10']):>7}{pct(s['f20']):>7}")
    msgs.append("*③ 第 1 大量「壓／撐」之後的平均報酬*\n```\n" + "\n".join(lines) + "\n```")
    lines = ["策略　　方式 筆數 勝率  期望  最差"]
    for name in STRATS:
        for mode in MODES:
            s = rep["by_mode"][name][mode]
            if s.get("n"):
                lines.append(f"{SHORT[name]} {MODE_CODE[mode]}{s['n']:>5}{pct(s['win'], 0):>5}{pct(s['mean']):>7}{pct(s['worst'], 0):>6}")
    msgs.append("*④ 出場方式比較（大盤啟用時的訊號）*\n```\n" + "\n".join(lines) + "\n```\n"
                "A＝三段，買第 2 筆後出場價拉到均價\nB＝三段，拉到突破價下 5%\nC＝三段，出場價不動\nD＝一次買進")
    short_crash = {"2020 疫情崩盤": "2020疫情", "2022 全年空頭": "2022空頭", "2024/08 急殺": "2024/08", "2025/04 關稅急跌": "2025/04"}
    lines = ["期間　　 筆數 勝率  期望 連虧"]
    for label, v in rep["crash"].items():
        s = v["all"]
        lines.append(f"{short_crash.get(label, label)}" + (f"{s['n']:>5}{pct(s['win'], 0):>5}{pct(s['mean']):>7}{s['max_streak']:>4}" if s.get("n") else " 沒有交易"))
    msgs.append("*⑤ 崩盤期（四套策略合計，不看大盤是否啟用）*\n```\n" + "\n".join(lines) + "\n```")
    return msgs


# ================= 資料 =================
def fetch_market_long(years):
    import data
    mkt = data.fetch_market(period=f"{years}y")
    try:
        s = data.fetch_twse_turnover(months=years * 12 + 4)
        mkt["Volume"] = s.reindex(mkt.index)
    except Exception as e:
        print("成交金額抓取失敗，止跌確認會無法判斷：", repr(e))
    return mkt


def run(sample=None, years=8, send=True, out_prefix="bt"):
    import data
    t_start = time.time()
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    mkt = fetch_market_long(years)
    mstate = market_states(mkt)
    directory = data.get_directory()
    syms = list(directory["code"] + directory["suffix"])
    if sample:
        random.seed(7)
        syms = random.sample(syms, min(sample, len(syms)))
    prices, failed = data.fetch_prices(syms, period=f"{years}y")
    print(f"取得 {len(prices)} 檔，失敗 {len(failed)} 檔，耗時 {time.time() - t_start:.0f} 秒", flush=True)
    return analyze(prices, mkt, mstate, now, send, out_prefix, t_start, len(failed))


def analyze(prices, mkt, mstate, now, send=True, out_prefix="bt", t_start=None, n_failed=0):
    t_start = t_start or time.time()
    ratios = cfg.TRANCHES
    start = mkt.index[0] + pd.Timedelta(days=100)
    signals, trades, samples = [], [], []
    for k, (sym, raw) in enumerate(prices.items(), 1):
        try:
            s, t, p = scan_stock(sym, raw, mstate, mkt, ratios, start)
            signals += s; trades += t; samples += p
        except Exception as e:
            print(f"{sym} 回測失敗：{e!r}")
        if k % 100 == 0:
            print(f"回測進度 {k}/{len(prices)}　訊號 {len(signals)}　交易 {len(trades)}　{time.time() - t_start:.0f} 秒", flush=True)
    rep, T, S, P = build_report(signals, trades, samples, mkt.index[-1])
    rep["universe"] = len(prices); rep["failed"] = n_failed
    rep["generated_at"] = f"{now:%Y-%m-%d %H:%M}"
    with open(f"{out_prefix}_report.json", "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1, default=lambda x: None if x != x else x)
    if not T.empty:
        T.to_csv(f"{out_prefix}_trades.csv", index=False, encoding="utf-8-sig")
    msgs = format_report(rep)
    print("\n\n".join(msgs))
    if send:
        try:
            from notify import send_telegram
            send_telegram(msgs)
        except Exception as e:
            print("Telegram 傳送失敗：", repr(e))
    print(f"完成，共 {time.time() - t_start:.0f} 秒")
    return rep


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=0, help="只回測隨機抽出的 N 檔（快速版）")
    ap.add_argument("--years", type=int, default=8, help="下載幾年資料（主要統計看近 5 年，其餘留給崩盤期）")
    ap.add_argument("--no-telegram", action="store_true")
    a = ap.parse_args()
    run(sample=a.sample or None, years=a.years, send=not a.no_telegram)
