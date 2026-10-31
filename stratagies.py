"""strategies.py — 大盤五層、止跌確認、四套個股策略、交易計畫
只做計算、不連網，所以 app、Telegram、回測都可以共用同一份規則。
所有數字都在 config.py。
"""
import math

import numpy as np
import pandas as pd

import config as cfg


# =====================================================
# 1. 小工具
# =====================================================
def _tick_size(p):
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.10
    if p < 500:
        return 0.50
    if p < 1000:
        return 1.00
    return 5.00


def tick(price, mode="round"):
    """轉成台股合法跳動單位。mode: round / floor / ceil"""
    if price is None or price != price or price <= 0:
        return 0.01
    t = _tick_size(price)
    n = price / t
    if mode == "floor":
        n = math.floor(n + 1e-9)
    elif mode == "ceil":
        n = math.ceil(n - 1e-9)
    else:
        n = round(n)
    return round(n * t, 2)


def avg_cost(prices, weights):
    """平均成本。COST_BY_MONEY=True：比例是資金比例（股數 = 資金 / 價格）；False：比例是股數比例"""
    if sum(weights) <= 0:
        return prices[0]
    if cfg.COST_BY_MONEY:
        return sum(weights) / sum(w / p for w, p in zip(weights, prices) if w > 0)
    return sum(w * p for w, p in zip(weights, prices)) / sum(weights)


def norm_ratios(ratios=None):
    """回傳 (r1, r2, r3)，加總為 1。傳入 None 用 config 預設。"""
    r = tuple(float(x) for x in (ratios or cfg.TRANCHES))
    if len(r) != 3 or any(x < 0 for x in r) or sum(r) <= 0:
        raise ValueError("三段比例必須是三個不小於 0 的數字")
    s = sum(r)
    return tuple(x / s for x in r)


def parse_ratios(text):
    """'30/40/30' 或 '30 40 30' → (0.3, 0.4, 0.3)；格式不對回傳 None"""
    parts = [p for p in str(text).replace("／", "/").replace(",", "/").replace(" ", "/").split("/") if p]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) != 3 or any(n < 0 for n in nums) or sum(nums) <= 0:
        return None
    return norm_ratios(nums)


def solve_max_entry_ratio(t1, t2, max_avg=None):
    """反推：第 1 筆最高可買在前高 B 的幾倍，才能讓前兩筆平均成本 <= max_avg x B"""
    max_avg = cfg.PB_MAX_AVG_RATIO if max_avg is None else max_avg
    if t1 <= 0:
        return 1.0
    lo, hi = 0.3, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if avg_cost([mid, 1.0], [t1, t2]) <= max_avg:
            lo = mid
        else:
            hi = mid
    return math.floor(lo * 10000) / 10000


def _ratio(a, b):
    return a / b if b else float("nan")


def _naive(df):
    if df is not None and getattr(df.index, "tz", None) is not None:
        df = df.copy()
        df.index = df.index.tz_localize(None)
    return df


def add_indicators(df):
    df = _naive(df).copy()
    c, v = df["Close"], df["Volume"]
    df["EMA20"] = c.ewm(span=20, adjust=False).mean()
    df["EMA60"] = c.ewm(span=60, adjust=False).mean()
    df["MA20"] = c.rolling(20).mean()
    df["MAV5"] = v.rolling(5).mean()
    df["MAV20"] = v.rolling(20).mean()
    df["TURN20"] = (c * v).rolling(20).mean()
    return df


def _liq(row):
    mav5, turn = float(row["MAV5"]), float(row["TURN20"])
    ok = mav5 >= cfg.MIN_LOTS * 1000 and turn >= cfg.MIN_AVG_TURNOVER
    return ("流動性", ok, f"5日均量 {mav5 / 1000:,.0f} 張、20日均額 {turn / 1e8:.2f} 億")


# =====================================================
# 2. 大盤五層 + 止跌確認
# =====================================================
def layer_series(df):
    """加權指數每天的層級：1 強多頭 / 2 多頭 / 3 盤整轉折 / 4 空頭 / 5 強空頭 / 0 資料不足"""
    c = df["Close"]
    ma5, ma10, ma20, ma60 = (c.rolling(n).mean() for n in (5, 10, 20, 60))
    layer = pd.Series(3, index=df.index)
    layer[(ma20 > ma60) & (c > ma20)] = 2
    layer[(ma20 < ma60) & (c < ma20)] = 4
    layer[(c > ma5) & (ma5 > ma10) & (ma10 > ma20) & (ma20 > ma60)] = 1
    layer[(c < ma5) & (ma5 < ma10) & (ma10 < ma20) & (ma20 < ma60)] = 5
    layer[ma60.isna()] = 0
    return layer


def stabilization(df, i):
    """止跌確認：最近 3 天最低點都沒破前低，且這 3 天任一天的量 >= 近 20 日（不含最近 3 天）量最大的下跌日"""
    n, k = cfg.STAB_LOOKBACK, cfg.STAB_DAYS
    if i < n + k + 1 or i >= len(df):
        return None
    lo, hi = i - k - n + 1, i - k + 1
    L = float(df["Low"].iloc[lo:hi].min())
    low_recent = float(df["Low"].iloc[hi:i + 1].min())
    no_break = low_recent > L

    close = df["Close"]
    down = (close < close.shift(1)).iloc[lo:hi].to_numpy()
    vols = df["Volume"].iloc[lo:hi].to_numpy(float)[down]
    vols = vols[~np.isnan(vols)]
    recent_v = float(df["Volume"].iloc[hi:i + 1].max())
    if len(vols) == 0 or vols.max() <= 0 or recent_v != recent_v:
        return {"ok": False, "no_break": no_break, "vol_ok": None, "vol_missing": True,
                "prior_low": L, "recent_low": low_recent, "dump_vol": None, "recent_vol": None}
    dump = float(vols.max())
    vol_ok = recent_v >= dump
    return {"ok": bool(no_break and vol_ok), "no_break": no_break, "vol_ok": vol_ok, "vol_missing": False,
            "prior_low": L, "recent_low": low_recent, "dump_vol": dump, "recent_vol": recent_v}


def active_strategies(layer, stab_ok):
    """依大盤層級決定哪些策略啟用。回傳 (策略清單, 是否要標示風險)"""
    if not cfg.USE_REGIME:
        return ["pullback", "breakout", "dry", "flag"], False
    if layer in (1, 2):
        return ["pullback", "flag"], False
    if layer == 3:
        return ["pullback", "flag"], True
    if layer in (4, 5):
        return (["breakout", "dry"] if stab_ok else []), False
    return [], False


def market_summary(df):
    """df：加權指數日線（要有 Open/High/Low/Close/Volume）"""
    df = _naive(df)
    i = len(df) - 1
    layer = int(layer_series(df).iloc[i])
    stab = stabilization(df, i)
    stab_ok = bool(stab and stab["ok"])
    active, risk = active_strategies(layer, stab_ok)
    c = df["Close"]
    return {
        "date": df.index[i], "close": float(c.iloc[i]),
        "ma": {n: float(c.rolling(n).mean().iloc[i]) for n in (5, 10, 20, 60)},
        "layer": layer, "layer_name": cfg.LAYER_NAMES[layer],
        "stab": stab, "stab_ok": stab_ok, "active": active, "risk": risk, "df": df,
    }


def describe_market(mk):
    """大盤狀態的文字說明（list of str，Markdown 格式）"""
    if not mk:
        return ["🌐 大盤資料取得失敗，無法判斷"]
    ma = mk["ma"]
    lines = [
        f"🌐 加權 `{mk['close']:,.0f}`｜MA5 `{ma[5]:,.0f}`／MA10 `{ma[10]:,.0f}`／MA20 `{ma[20]:,.0f}`／MA60 `{ma[60]:,.0f}`",
        f"📊 大盤層級：**第 {mk['layer']} 層・{mk['layer_name']}**",
    ]
    if mk["layer"] in (4, 5):
        s = mk["stab"]
        if s is None:
            lines.append("🧱 止跌確認：資料不足")
        elif s["vol_missing"]:
            lines.append("🧱 止跌確認：❌ 大盤成交量資料取不到，無法確認")
        else:
            lines.append(
                f"🧱 止跌確認：{'✅ 通過' if s['ok'] else '❌ 未通過'}\n"
                f"　{'✅' if s['no_break'] else '❌'} 近3日最低 {s['recent_low']:,.0f}，前低 {s['prior_low']:,.0f}\n"
                f"　{'✅' if s['vol_ok'] else '❌'} 近3日最大量 {s['recent_vol'] / 1e8:.2f} 億股，爆量下跌日 {s['dump_vol'] / 1e8:.2f} 億股"
            )
    if mk["active"]:
        names = "、".join(cfg.STRATEGY_NAMES[a] for a in mk["active"])
        lines.append(f"✅ 目前啟用的策略：{names}" + ("\n⚠️ 第 3 層方向未明，請標示風險、縮小部位" if mk["risk"] else ""))
    else:
        lines.append("🛑 目前沒有啟用的策略：空頭尚未止跌確認，不新進場")
    return lines


# =====================================================
# 3. 四套個股策略
# =====================================================
def _result(name, checks, **kw):
    return {"name": name, "ok": all(c[1] for c in checks), "checks": checks, **kw}


def check_pullback(df, i, ratios=None):
    """策略 1：創高拉回。df 需先 add_indicators"""
    if i < 69 or i >= len(df):
        return None
    t = norm_ratios(ratios)
    max_ratio = solve_max_entry_ratio(t[0], t[1])
    min_ratio = 1.0 - cfg.PB_PULLBACK_MAX
    row = df.iloc[i]
    close, vol = float(row["Close"]), float(row["Volume"])
    lo = i - cfg.PB_LOOKBACK + 1
    win_high = df["High"].iloc[lo:i - 1]
    if win_high.empty:
        return None
    b_pos = lo + int(win_high.to_numpy().argmax())
    B = float(df["High"].iloc[b_pos])
    vol_B = float(df["Volume"].iloc[b_pos])
    prior = df["High"].iloc[:lo]
    prior_high = float(prior.max()) if len(prior) else float("nan")
    ratio = close / B
    mav20 = float(row["MAV20"])
    ema20, ema60 = float(row["EMA20"]), float(row["EMA60"])

    checks = [
        _liq(row),
        ("前高為近期新高", bool(pd.isna(prior_high) or B >= prior_high * 0.98),
         f"前高 {B:.2f}，更早的高點 {prior_high:.2f}"),
        (f"回檔 {(1 - max_ratio) * 100:.1f}~{cfg.PB_PULLBACK_MAX * 100:.0f}%", min_ratio <= ratio <= max_ratio,
         f"目前回檔 {(1 - ratio) * 100:.1f}%"),
        ("量縮", bool(vol_B > 0 and mav20 > 0 and vol <= cfg.PB_VOL_RATIO * vol_B and vol <= mav20),
         f"今日量 = 高點日的 {_ratio(vol, vol_B) * 100:.0f}%、20日均量的 {_ratio(vol, mav20) * 100:.0f}%"
         f"（需 ≤ {cfg.PB_VOL_RATIO * 100:.0f}% 且 ≤ 100%）"),
        ("趨勢向上", bool(ema20 > ema60 and close >= ema60 * 0.98),
         f"EMA20 {ema20:.2f}、EMA60 {ema60:.2f}、收盤 {close:.2f}"),
    ]
    zone_hi = tick(B * max_ratio, "floor")
    zone_lo = tick(B * min_ratio, "ceil")
    return _result("pullback", checks, close=close, key=B, B_date=df.index[b_pos], ratio=ratio,
                   zone_hi=zone_hi, zone_lo=zone_lo, in_zone=zone_lo <= close <= zone_hi,
                   date=df.index[i], turnover=float(row["TURN20"]))


def check_breakout(df, i):
    """策略 2：盤整後噴出（低檔爆量起漲，突破 30 日頸線）"""
    if i < 69 or i >= len(df):
        return None
    row = df.iloc[i]
    close, vol = float(row["Close"]), float(row["Volume"])
    mav20 = float(row["MAV20"])
    ema20, ema60 = float(row["EMA20"]), float(row["EMA60"])
    ema20_prev = float(df["EMA20"].iloc[i - 1])
    low60 = float(df["Low"].iloc[i - 59:i + 1].min())
    neck = float(df["High"].iloc[i - cfg.BO_PLATFORM:i].max())
    rise = (close - low60) / low60 if low60 > 0 else float("nan")
    volx = _ratio(vol, mav20)

    checks = [
        _liq(row),
        (f"離60日低點漲幅 ≤ {cfg.BO_MAX_RISE * 100:.0f}%", bool(rise <= cfg.BO_MAX_RISE), f"目前 +{rise * 100:.1f}%"),
        (f"突破 {cfg.BO_PLATFORM} 日頸線", close > neck, f"頸線 {neck:.2f}、收盤 {close:.2f}"),
        (f"爆量 ≥ {cfg.BO_VOL_MULT:.1f} 倍", bool(volx >= cfg.BO_VOL_MULT), f"今日 {volx:.1f} 倍 20 日均量"),
        ("站上季線且月線翻揚", bool(close >= ema60 and ema20 > ema20_prev),
         f"EMA20 {ema20:.2f}（昨 {ema20_prev:.2f}）、EMA60 {ema60:.2f}"),
    ]
    E = tick(close)
    stop1 = tick(min(neck * 0.98, E * (1 - cfg.BO_STOP_PCT)))
    return _result("breakout", checks, close=close, key=float(row["High"]), neckline=neck,
                   stop1=stop1, target=tick(E * (1 + cfg.BO_TARGET_PCT)),
                   stop1_note=f"頸線下 2%（{tick(neck * 0.98)}）與買價下 {cfg.BO_STOP_PCT * 100:.0f}%（{tick(E * (1 - cfg.BO_STOP_PCT))}）取較低",
                   date=df.index[i], turnover=float(row["TURN20"]))


def check_dry(df, i, mkt=None):
    """策略 3：量縮抗跌。mkt：加權指數日線 DataFrame（用來比較抗跌）"""
    if i < 69 or i >= len(df):
        return None
    row = df.iloc[i]
    close = float(row["Close"])
    mav20 = float(row["MAV20"])
    n, k = cfg.STAB_LOOKBACK, cfg.STAB_DAYS
    lo, hi = i - k - n + 1, i - k + 1
    L = float(df["Low"].iloc[lo:hi].min())
    low3 = float(df["Low"].iloc[hi:i + 1].min())
    vol3 = float(df["Volume"].iloc[hi:i + 1].mean())

    checks = [
        _liq(row),
        ("量縮", bool(mav20 > 0 and vol3 <= cfg.DRY_VOL_RATIO * mav20),
         f"3日均量 = 20日均量的 {_ratio(vol3, mav20) * 100:.0f}%（需 ≤ {cfg.DRY_VOL_RATIO * 100:.0f}%）"),
        (f"{k}日不破前低", low3 > L, f"最近{k}日最低 {low3:.2f}，前低 {L:.2f}"),
    ]
    rs_ok, rs_detail = False, "大盤資料不足，無法比較"
    if mkt is not None and len(mkt):
        d = df.index[i]
        m = _naive(mkt)["Close"].loc[:d].iloc[-cfg.STAB_LOOKBACK:]
        if len(m) >= 5:
            m_high, m_date, m_now = float(m.max()), m.idxmax(), float(m.iloc[-1])
            m_drop = max(0.0, 1 - m_now / m_high)
            ref = df["Close"].loc[:m_date]
            if len(ref):
                s_drop = max(0.0, 1 - close / float(ref.iloc[-1]))
                rs_ok = bool(s_drop <= cfg.DRY_RS_RATIO * m_drop)
                rs_detail = (f"大盤自 {m_date:%m/%d} 高點跌 {m_drop * 100:.1f}%，個股跌 {s_drop * 100:.1f}%"
                             f"（需 ≤ 大盤的 {cfg.DRY_RS_RATIO * 100:.0f}%）")
    checks.append(("比大盤抗跌", rs_ok, rs_detail))

    E = tick(close)
    stop1 = tick(L)
    key = float(df["High"].iloc[i - cfg.DRY_KEY_LOOKBACK + 1:i + 1].max())
    target = tick(E + cfg.DRY_RRR * max(E - stop1, 0.0))
    return _result("dry", checks, close=close, key=key, prior_low=L, stop1=stop1, target=target,
                   stop1_note=f"跌破個股前低 {stop1}", date=df.index[i], turnover=float(row["TURN20"]))


def check_flag(df, i):
    """策略 4：噴出後整理再攻。以昨天(j)為基準判斷型態，今天(i)判斷是否接近/突破前波高點"""
    if i < 69 or i >= len(df):
        return None
    j = i - 1
    row = df.iloc[i]
    close = df["Close"].to_numpy(float)
    high = df["High"].to_numpy(float)
    vol = df["Volume"].to_numpy(float)
    mav20 = df["MAV20"].to_numpy(float)
    ma20 = df["MA20"].to_numpy(float)
    liq = _liq(row)

    found = None
    for bp in range(j - cfg.FLAG_MIN_CONSOL, i - cfg.FLAG_WINDOW - 1, -1):
        if bp < 24:
            break
        g3 = close[bp] / close[bp - 3] - 1
        g1 = close[bp] / close[bp - 1] - 1
        if g3 >= cfg.FLAG_BURST_3D:
            S, kind, g, vwin = close[bp - 3], "3日", g3, vol[bp - 2:bp + 1]
        elif g1 >= cfg.FLAG_BURST_1D:
            S, kind, g, vwin = close[bp - 1], "單日", g1, vol[bp:bp + 1]
        else:
            continue
        base = mav20[bp - 3]
        if not (base > 0 and vwin.max() >= cfg.FLAG_BURST_VOL * base):
            continue
        found = (bp, S, kind, g, float(vwin.max() / base))
        break
    if found is None:
        return _result("flag", [liq, ("噴出", False,
                       f"近 {cfg.FLAG_WINDOW} 日沒有符合的噴出（3日≥{cfg.FLAG_BURST_3D * 100:.0f}% 或單日≥{cfg.FLAG_BURST_1D * 100:.0f}%，"
                       f"且量 ≥ {cfg.FLAG_BURST_VOL} 倍均量）")],
                       date=df.index[i], turnover=float(row["TURN20"]))

    bp, S, kind, g, vmult = found
    seg = high[bp - 2:j + 1]
    H = float(seg.max())
    pk = bp - 2 + int(seg.argmax())
    G = H - S
    consol = j - pk
    cons_close, cons_ma = close[pk + 1:j + 1], ma20[pk + 1:j + 1]
    mid = S + cfg.FLAG_MAX_PULLBACK * G
    min_close = float(cons_close.min()) if len(cons_close) else float("nan")
    burst_vol = float(vol[bp - 2:pk + 1].mean())
    cons_vol = float(vol[pk + 1:j + 1].mean()) if len(cons_close) else float("nan")
    cl = float(close[i])
    status = f"已突破 +{(cl / H - 1) * 100:.1f}%" if cl >= H else f"還差 {(1 - cl / H) * 100:.1f}%"

    checks = [
        liq,
        ("噴出", True, f"{df.index[bp]:%m/%d} {kind}漲幅 {g * 100:.1f}%，量 {vmult:.1f} 倍均量"),
        (f"整理 ≥ {cfg.FLAG_MIN_CONSOL} 天", consol >= cfg.FLAG_MIN_CONSOL, f"高點後已整理 {consol} 天"),
        (f"回檔 ≤ 噴出漲幅的 {cfg.FLAG_MAX_PULLBACK * 100:.0f}%",
         bool(len(cons_close) and min_close >= mid and cl >= mid),
         f"噴出 {S:.2f}→{H:.2f}，要守住 {mid:.2f}；整理最低收盤 {min_close:.2f}"),
        ("守月線(MA20)", bool(len(cons_close) and (cons_close >= cons_ma).all() and cl >= ma20[i]),
         f"現價 {cl:.2f}、MA20 {ma20[i]:.2f}"),
        (f"整理期量縮（≤ 噴出期 {cfg.FLAG_VOL_RATIO * 100:.0f}%）",
         bool(len(cons_close) and cons_vol <= cfg.FLAG_VOL_RATIO * burst_vol),
         f"整理均量 = 噴出期均量的 {_ratio(cons_vol, burst_vol) * 100:.0f}%"),
        (f"距前波高點 {cfg.FLAG_NEAR * 100:.0f}% 內（含已突破）", cl >= (1 - cfg.FLAG_NEAR) * H,
         f"現價 {cl:.2f}、前波高點 {H:.2f}（{status}）"),
    ]
    E = tick(cl)
    stop_half = tick(mid)
    stop_pct = tick(E * (1 - cfg.FLAG_STOP_MIN_PCT))
    return _result("flag", checks, close=cl, key=H, burst_start=S, burst_gain=G,
                   stop1=min(stop_half, stop_pct), target=tick(H + G),
                   stop1_note=f"噴出高度一半 {stop_half} 與買價下 {cfg.FLAG_STOP_MIN_PCT * 100:.0f}% {stop_pct} 取較低",
                   status=status, date=df.index[i], turnover=float(row["TURN20"]))


# =====================================================
# 4. 交易計畫
# =====================================================
def _breakeven(e, p2, stop2, t):
    """第 2 筆在 stop2 被砍掉後，第 1 筆要漲到多少，整趟才打平"""
    if t[0] <= 0:
        return None
    s1, s2 = (t[0] / e, t[1] / p2) if cfg.COST_BY_MONEY else (t[0], t[1])
    return tick(e + max(0.0, p2 - stop2) * s2 / s1)


def build_plan(entry, B, ratios=None):
    """創高拉回的完整計畫（第 1 筆在 entry，第 2 筆突破前高 B，第 3 筆 B x 1.08）"""
    t = norm_ratios(ratios)
    e, b = tick(entry), tick(B)
    b2 = tick(B * cfg.EXT_RATIO)
    avg2, avg3 = tick(avg_cost([e, b], t[:2])), tick(avg_cost([e, b, b2], t))
    stop2 = tick(b * (1 - cfg.STOP_ADD_PCT))
    tp = cfg.PB_STOP_PCT * cfg.PB_RRR
    return {
        "ratios": t, "e": e, "b": b, "b2": b2, "key": b, "key_label": "突破前高",
        "avg2": avg2, "avg3": avg3,
        "stop1": tick(e * (1 - cfg.PB_STOP_PCT)), "stop1_note": f"進場價下 {cfg.PB_STOP_PCT * 100:.0f}%",
        "stop2": stop2, "breakeven": _breakeven(e, b, stop2, t),
        "targets": [("打滿三筆", tick(avg3 * (1 + tp))), ("只買兩筆", tick(avg2 * (1 + tp)))],
    }


def ladder_plan(E, K, stop1, target, ratios=None, key_label="突破關鍵價", stop1_note=""):
    """其他三套策略的通用分批：第 1 筆 E、第 2 筆突破關鍵價 K、第 3 筆 K x 1.08"""
    t = norm_ratios(ratios)
    p1, k = tick(E), tick(K)
    p2 = max(k, p1)
    p3 = max(tick(K * cfg.EXT_RATIO), p2)
    stop2 = tick(k * (1 - cfg.STOP_ADD_PCT))
    notes = []
    tg = tick(target)
    if p2 > p1 and tg <= p2:
        notes.append(f"⚠️ 目標價 {tg} 不高於第 2 筆加碼價 {p2}，加碼前就可能先到目標")
    elif tg <= p3:
        notes.append(f"⚠️ 目標價 {tg} 不高於第 3 筆加碼價 {p3}，第 3 筆實務上可能不會買進")
    if p1 >= k:
        notes.append("ℹ️ 現價已在關鍵價之上，第 2 筆可與第 1 筆一併買進")
    return {
        "ratios": t, "e": p1, "b": p2, "b2": p3, "key": k, "key_label": key_label,
        "avg2": tick(avg_cost([p1, p2], t[:2])), "avg3": tick(avg_cost([p1, p2, p3], t)),
        "stop1": tick(stop1), "stop1_note": stop1_note,
        "stop2": stop2, "breakeven": _breakeven(p1, p2, stop2, t),
        "targets": [("", tg)], "notes": notes,
    }


def plan_for(name, r, ratios=None):
    """依策略結果 r 產生交易計畫。創高拉回：現價在進場區就用現價，否則假設掛在進場區上緣"""
    if r is None:
        return None
    if name == "pullback":
        entry = r["close"] if r["in_zone"] else r["zone_hi"]
        return build_plan(entry, r["key"], ratios)
    labels = {"breakout": "突破當天最高價", "dry": "近20日高點", "flag": "前波高點"}
    return ladder_plan(r["close"], r["key"], r["stop1"], r["target"], ratios,
                       key_label=f"突破{labels[name]}", stop1_note=r.get("stop1_note", ""))


# =====================================================
# 5. 一次評估一檔股票
# =====================================================
def evaluate(df, mkt_df, ratios=None):
    """df：個股日線；mkt_df：加權指數日線。回傳大盤狀態與四套策略結果"""
    t = norm_ratios(ratios)
    df = add_indicators(df)
    i = len(df) - 1
    if i < 69:
        return {"error": f"資料只有 {len(df)} 個交易日，不足以分析（至少需要 70 天）"}
    mk = market_summary(mkt_df) if mkt_df is not None and len(mkt_df) >= 70 else None
    mdf = mk["df"] if mk else None
    results = {
        "pullback": check_pullback(df, i, t),
        "breakout": check_breakout(df, i),
        "dry": check_dry(df, i, mdf),
        "flag": check_flag(df, i),
    }
    for name, r in results.items():
        if r is not None:
            r["active"] = bool(mk and name in mk["active"])
    return {"market": mk, "results": results, "ratios": t, "date": df.index[i],
            "close": float(df["Close"].iloc[i])}


def verdict(ev):
    """一句話結論。回傳 (等級, 文字)；等級：go / watch / wait / stop"""
    mk = ev["market"]
    ok_names = [n for n, r in ev["results"].items() if r and r["ok"]]
    go = [n for n in ok_names if ev["results"][n]["active"]]
    if go:
        names = "、".join(cfg.STRATEGY_NAMES[n] for n in go)
        risk = "（大盤第 3 層，請縮小部位）" if mk and mk["risk"] else ""
        return "go", f"✅ 符合【{names}】，且大盤目前適用{risk}"
    if ok_names:
        names = "、".join(cfg.STRATEGY_NAMES[n] for n in ok_names)
        return "watch", f"⚠️ 型態符合【{names}】，但目前大盤層級不適用這套策略，先觀察"
    if mk and not mk["active"]:
        return "stop", "🛑 大盤尚未止跌確認，且這檔也沒有符合的策略，不進場"
    return "wait", "⏳ 目前沒有符合的策略，可以先放進觀察名單，等條件成形"
