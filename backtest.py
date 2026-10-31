"""backtest.py — 單一股票回測（網頁 app 個股診斷用）
用跟夜間掃描完全相同的判斷程式，在這檔股票的歷史上逐日往回推，模擬每一次訊號的進出場。

交易規則：
  1. 訊號出現的「隔天開盤價」進第 1 筆（不用訊號當天收盤，避免高估）
  2. 出場價用盤中最低價觸價；若隔天直接跳空跌破，用開盤價成交（虧損會比預期大）
  3. 第 2、3 筆：價格漲到突破價、突破價+8% 才買；沒到就不買
  4. 同一天同時碰到出場價和目標價 → 一律先算出場（保守）
  5. 達到目標價全部賣出；持有超過 MAX_HOLD 個交易日還沒出場，用收盤價出場
  6. 手續費、證交稅：來回 cfg.FEE_RT
  7. 同一套策略有部位時不重複進場
出場方式：
  ladder_avg   三段進場，第 2 筆買進後出場價拉到累計均價（網頁 app 現在的規則）
  ladder_stop2 三段進場，第 2 筆買進後出場價拉到「突破價下 5%」
  ladder_stop1 三段進場，出場價一直不動
  single       一次買進
"""
import numpy as np
import pandas as pd

import config as cfg
import strategies as st

MAX_HOLD = 60                       # 最久持有幾個交易日
MODES = ("ladder_avg", "ladder_stop2", "ladder_stop1", "single")
STRATS = ("pullback", "flag", "breakout", "dry")


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
    return {"entry_idx": j, "exit_idx": d_exit, "hold": d_exit - j + 1, "tranches": filled, "entry_px": p1,
            "invested": cs, "avg": cs / sh, "exit_px": exit_px, "reason": reason,
            "ret_inv": pnl / cs, "ret_plan": pnl}



# ================= 先用向量運算篩出可能的日子（加速，不會漏掉訊號） =================
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



# ================= 單一股票回測 =================
def run_stock(raw, mkt, mstate, ratios, mode="ladder_avg", start=None, only_active=False):
    """raw：個股日線（不還原）；mkt：加權指數日線；mstate：market_states(mkt)。
    回傳每一筆交易的 DataFrame（日期、策略、進出場價、報酬…）"""
    df = st.add_indicators(raw)
    n = len(df)
    if n < 80:
        return pd.DataFrame()
    O, H, L, C = (df[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    liq = ((df["MAV5"] >= cfg.MIN_LOTS * 1000) & (df["TURN20"] >= cfg.MIN_AVG_TURNOVER)).to_numpy()
    dates = df.index
    pre = prescreen(df, ratios)
    busy, rows = {}, []
    for i in range(69, n - 1):
        if not liq[i] or (start is not None and dates[i] < start):
            continue
        layer, active, _ = mstate.get(dates[i], (0, frozenset(), False))
        for name in STRATS:
            if not pre[name][i] or busy.get(name, -1) >= i:
                continue
            if only_active and name not in active:
                continue
            if name == "pullback":
                r = st.check_pullback(df, i, ratios)
            elif name == "flag":
                r = st.check_flag(df, i)
            elif name == "breakout":
                r = st.check_breakout(df, i)
            else:
                r = st.check_dry(df, i, mkt)
            if r is None or not r["ok"]:
                continue
            plan = st.plan_for(name, r, ratios)
            t = simulate(O, H, L, C, i, plan, mode)
            if t is None:
                continue
            busy[name] = t["exit_idx"]
            rows.append({"strat": name, "date": dates[i], "layer": layer, "active": name in active,
                         "entry_date": dates[t["entry_idx"]], "exit_date": dates[t["exit_idx"]],
                         "entry_px": t["entry_px"], "exit_px": t["exit_px"], "avg": t["avg"],
                         "tranches": t["tranches"], "hold": t["hold"], "reason": t["reason"],
                         "ret_inv": t["ret_inv"], "ret_plan": t["ret_plan"]})
    return pd.DataFrame(rows)


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
