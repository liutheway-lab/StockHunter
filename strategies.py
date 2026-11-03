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


def _fp(x):
    """價位寫法：依台股跳動單位決定小數位數，並去掉多餘的 0（2300.00 → 2300、114.50 → 114.5）"""
    t = _tick_size(x)
    d = 0 if t >= 1 else (1 if t in (0.5, 0.1) else 2)
    out = f"{x:,.{d}f}".replace(",", "")
    return out.rstrip("0").rstrip(".") if "." in out else out


def _gauge(pos, n=20):
    """橫線＋圓點：圓點＝今天收盤在當天最低（左）到最高（右）的哪裡，圓點旁邊標出幾 %"""
    k = min(n - 1, max(0, int(round(pos * (n - 1)))))
    return "".join(f"●{pos * 100:.0f}%" if j == k else "─" for j in range(n))


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
    df["MA20"] = c.rolling(20).mean()                        # 月線
    df["MA60"] = c.rolling(60).mean()                        # 季線
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
    """加權指數每天的層級：1 強多頭 / 2 多頭 / 3 盤整轉折 / 4 空頭 / 5 強空頭 / 0 資料不足
    只用兩條線：月線（20 日平均價）和季線（60 日平均價），再看這兩條線是往上還是往下。
      1 強多頭：價在月線上、月線在季線上，兩條線都往上
      2 多頭　：價在兩條線上，但線的方向還沒全部往上
      3 盤整　：價夾在兩條線中間
      4 空頭　：價在兩條線下，但線的方向還沒全部往下
      5 強空頭：價在月線下、月線在季線下，兩條線都往下
    再看 5 日、10 日線（短線）：多頭（1、2 層）但價跌破兩條短線 → 降一層；
    空頭（4、5 層）但價站上兩條短線 → 升一層。這樣崩盤或反彈時，層級反應比較快
    換層要連續幾天確認才算（LAYER_CONFIRM_DAYS）"""
    c = df["Close"]
    ma5, ma10 = c.rolling(5).mean(), c.rolling(10).mean()
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    s20 = ma20 / ma20.shift(cfg.SLOPE_DAYS) - 1                  # 月線這幾天的變化
    s60 = ma60 / ma60.shift(cfg.SLOPE_DAYS) - 1
    layer = pd.Series(3, index=df.index)
    layer[(c > ma20) & (c > ma60)] = 2
    layer[(c < ma20) & (c < ma60)] = 4
    layer[(c > ma20) & (ma20 > ma60) & (s20 > 0) & (s60 > 0)] = 1
    layer[(c < ma20) & (ma20 < ma60) & (s20 < 0) & (s60 < 0)] = 5
    layer[ma60.isna() | s60.isna()] = 0
    short_dn, short_up = (c < ma5) & (c < ma10), (c > ma5) & (c > ma10)
    bull, bear = layer.isin([1, 2]), layer.isin([4, 5])
    layer[bull & short_dn] += 1                                  # 多頭但價在 5、10 日線下：降一層（1→2、2→3）
    layer[bear & short_up] -= 1                                  # 空頭但價在 5、10 日線上：升一層（5→4、4→3）
    return _confirm_layer(layer)


def layer_why(df):
    """用白話講「為什麼是這一層」（每行都短）"""
    c = df["Close"]
    ma20, ma60 = c.rolling(20).mean(), c.rolling(60).mean()
    s20 = float(ma20.iloc[-1] / ma20.iloc[-1 - cfg.SLOPE_DAYS] - 1)
    s60 = float(ma60.iloc[-1] / ma60.iloc[-1 - cfg.SLOPE_DAYS] - 1)
    x = float(c.iloc[-1])
    d = lambda v: "往上" if v > 0 else "往下"
    m5, m10 = float(c.rolling(5).mean().iloc[-1]), float(c.rolling(10).mean().iloc[-1])
    st_ = "上" if x > m5 and x > m10 else "下" if x < m5 and x < m10 else "中間"
    return [f"價在月線{'上' if x > ma20.iloc[-1] else '下'}、季線{'上' if x > ma60.iloc[-1] else '下'}",
            f"月線{d(s20)}",
            f"季線{d(s60)}",
            f"價在 5日、10日線{'之' + st_ if st_ != '中間' else '之間'}"]


def short_tag(df, layer):
    """第 1、5 層再看 5 日、10 日線：短線全部同方向時加標籤（只提醒，不改層級、不改策略）
    第 1 層：價 > 5日 > 10日 > 月線，且 5日、10日都往上 → 過熱
    第 5 層：價 < 5日 < 10日 < 月線，且 5日、10日都往下 → 恐慌"""
    if layer not in (1, 5) or len(df) < 12:
        return None
    c = df["Close"]
    m5, m10, m20 = (c.rolling(n).mean() for n in (5, 10, 20))
    x, a, b, m = float(c.iloc[-1]), float(m5.iloc[-1]), float(m10.iloc[-1]), float(m20.iloc[-1])
    u5, u10 = a > float(m5.iloc[-2]), b > float(m10.iloc[-2])
    if layer == 1 and x > a > b > m and u5 and u10:
        return "過熱"
    if layer == 5 and x < a < b < m and not u5 and not u10:
        return "恐慌"
    return None


SHORT_TAG_NOTE = {"過熱": "短線全部往上，別追高\n等拉回再找買點",
                  "恐慌": "短線全部往下，正在急殺\n留意之後的止跌訊號"}


def drawdown(df, days=60):
    """離近 days 日最高收盤跌了幾 %（負數）"""
    c = df["Close"]
    return float(c.iloc[-1] / c.iloc[-days:].max() - 1)


def _confirm_layer(raw):
    """防來回跳動：新的層級要連續 LAYER_CONFIRM_DAYS 天都一樣，才正式換層（資料不足的 0 馬上生效）"""
    need = max(1, int(cfg.LAYER_CONFIRM_DAYS))
    v = raw.to_numpy()
    out = v.copy()
    cur = v[0] if len(v) else 0
    for i in range(len(v)):
        x = v[i]
        if x == 0 or x == cur:
            cur = x
        elif i + 1 >= need and all(v[i - j] == x for j in range(need)):
            cur = x
        out[i] = cur
    return pd.Series(out, index=raw.index)


def stabilization(df, i):
    """止跌確認（日線）。先找「爆量下跌日」＝最近 STAB_LOOKBACK＋STAB_MAX_AGE 個交易日裡，成交額最大的下跌日。
    再找「低點日」＝最近 STAB_DAYS 天股價最低點那天（要在爆量日之後），三項都過才算止跌走穩：
    ① 量能：低點日成交額 ≥ 爆量下跌日
    ② K 棒：長下影線（下影線 ≥ 當天高低差的 1/2）或長紅（收紅、漲幅 ≥ 1.5%），二擇一
    ③ 不破低：低點日之後的低點都高於低點日的最低點（低點日就是今天＝觀察中）
    字典欄位沿用舊名稱（prior_low＝低點日最低點、prior_low_date＝低點日、recent_*＝低點日之後）。"""
    n, k, age = cfg.STAB_LOOKBACK, cfg.STAB_DAYS, cfg.STAB_MAX_AGE
    if i < n + age + 1 or i >= len(df):
        return None
    O, H, L, C, V = (df[c].to_numpy(float) for c in ("Open", "High", "Low", "Close", "Volume"))
    idx = df.index
    down = np.r_[False, C[1:] < C[:-1]]
    w0 = i - n - age
    seg = slice(w0, i)                                           # 不含今天：爆量日一定在低點日之前
    m = down[seg] & ~np.isnan(V[seg]) & (V[seg] > 0)
    if not m.any():
        return {"ok": False, "no_break": False, "vol_ok": None, "vol_missing": True, "prior_low": float(L[i]),
                "recent_low": None, "dump_vol": None, "recent_vol": None, "prior_low_date": idx[i],
                "recent_low_date": None, "recent_start": idx[i], "recent_end": idx[i], "dump_date": None}
    P = w0 + int(np.where(m, V[seg], -1.0).argmax())             # 爆量下跌日
    dv, dd = float(V[P]), idx[P]

    # 低點日＝最近 STAB_DAYS 天（含今天）股價最低點那天；量能、K 棒、不破低都看這一天
    w = max(i - k + 1, P + 1)
    d = w + int(np.nan_to_num(L[w:i + 1], nan=1e18).argmin())
    if np.isnan(V[d]) or np.isnan(L[d]):
        return None
    rng = H[d] - L[d]
    shadow = float((min(O[d], C[d]) - L[d]) / rng) if rng > 0 else 0.0
    gain = float(C[d] / C[d - 1] - 1) if C[d - 1] > 0 else 0.0
    shadow_ok = bool(shadow >= cfg.STAB_SHADOW)                      # 長下影線（開低走高）
    body = float((C[d] - O[d]) / rng) if rng > 0 else 0.0              # 實體占整根 K 棒（收黑為負）
    red_ok = bool(C[d] > O[d] and body >= cfg.STAB_RED_BODY)         # 或長紅
    shape_ok = shadow_ok or red_ok
    vol_ok = bool(V[d] >= dv)
    after = L[d + 1:i + 1]
    days_after = len(after)
    no_break = bool(days_after > 0 and after.min() > L[d])           # 低點日之後的低點都沒跌破它
    full = vol_ok and shape_ok and no_break
    pending = vol_ok and shape_ok and days_after == 0                # 低點日就是今天：還要再觀察
    rl = float(after.min()) if days_after else None
    rld = idx[d + 1 + int(after.argmin())] if days_after else None
    return {"ok": bool(full), "watching": bool(pending), "signal_date": idx[d],
            "vol_ok": vol_ok, "shape_ok": shape_ok, "no_break": no_break, "vol_missing": False,
            "shadow": shadow, "gain": gain, "body": body, "shadow_ok": shadow_ok, "red_ok": red_ok,
            "days_after": days_after, "need_days": 1,
            "prior_low": float(L[d]), "prior_low_date": idx[d], "recent_low": rl, "recent_low_date": rld,
            "recent_start": idx[d], "recent_end": idx[i],
            "dump_vol": dv, "dump_date": dd, "recent_vol": float(V[d]), "vol_date": idx[d]}


def vol_low(df, days=65):
    """近 65 個交易日成交量最大的那天。最高點在現價之上＝短線壓力（壓，看最高點）；否則＝支撐參考（撐，看最低點）"""
    d = df[df["Volume"] > 0].iloc[-days:]
    if d.empty:
        return None
    day = d["Volume"].idxmax()
    low, high, close = float(d.loc[day, "Low"]), float(d.loc[day, "High"]), float(df["Close"].iloc[-1])
    pressure = high > close                      # 壓力看最高點，支撐看最低點
    return {"date": day, "low": low, "high": high, "level": high if pressure else low,
            "vol": float(d.loc[day, "Volume"]) / 1000.0, "side": "壓" if pressure else "撐"}


def check_absorb(df, i):
    """恐慌承接（只做觀察名單）：今天殺低後被買回來，而且有量。
    長下影＝下影線占當天高低差 ≥ 50%；長紅＝收紅且實體占 ≥ 80%（跟大盤止跌確認的 K 棒同一套）；兩者其一，量 ≥ 20 日均量 1.5 倍，當天振幅 ≥ 8%。
    不符合回傳 None"""
    r = df.iloc[i]
    o, h, l, c = (float(r[k]) for k in ("Open", "High", "Low", "Close"))
    mav20, vol = float(r["MAV20"]), float(r["Volume"])
    rng = h - l
    if rng <= 0 or not mav20 or mav20 != mav20 or vol <= 0:
        return None
    vm = vol / mav20
    if vm < cfg.ABSORB_VOL_MULT:
        return None
    prev = float(df["Close"].iloc[i - 1]) if i >= 1 else c
    if prev and rng / prev < cfg.ABSORB_MIN_RANGE:
        return None
    shadow, body = (min(o, c) - l) / rng, (c - o) / rng
    kinds = []
    if shadow >= cfg.STAB_SHADOW:
        kinds.append("長下影")
    if c > o and body >= cfg.STAB_RED_BODY:
        kinds.append("長紅")
    if not kinds:
        return None
    return {"kind": "＋".join(kinds), "close": c, "low": l, "high": h, "vol_mult": vm, "shadow": shadow,
            "body": body, "chg": (c / prev - 1) if prev else 0.0, "turnover": float(r["TURN20"])}


def active_strategies(layer, stab_ok):
    """依大盤層級決定哪些策略啟用。回傳 (策略清單, 是否要標示風險)"""
    if not cfg.USE_REGIME:
        return ["pullback", "breakout", "dry", "flag"], False
    if layer in (1, 2):
        return ["pullback", "flag", "breakout"], False       # 低位階起漲：底部起漲不限空頭，各種行情都可能出現
    if layer == 3:
        return list(cfg.LAYER3_ACTIVE), True                 # 方向未明：只開最保守的，部位減半
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
        "why": layer_why(df) if len(df) > 60 + cfg.SLOPE_DAYS else [], "dd": drawdown(df),
        "tag": short_tag(df, layer),
        "stab": stab, "stab_ok": stab_ok, "active": active, "risk": risk, "df": df,
    }


def describe_market(mk):
    """大盤狀態的文字說明（list of str，Markdown 格式）"""
    if not mk:
        return ["🌐 大盤資料取得失敗，無法判斷"]
    ma = mk["ma"]
    lines = [
        f"🌐 加權 `{mk['close']:,.0f}`｜5日 `{ma[5]:,.0f}`／10日 `{ma[10]:,.0f}`／月線 `{ma[20]:,.0f}`／季線 `{ma[60]:,.0f}`",
        f"📊 大盤層級：**第 {mk['layer']} 層・{mk['layer_name']}**",
    ]
    if mk.get("why"):
        lines.append("　" + "、".join(mk["why"]))
    if mk.get("tag"):
        lines.append(f"{'🔥' if mk['tag'] == '過熱' else '😱'} {mk['tag']}：" + SHORT_TAG_NOTE[mk["tag"]].replace("\n", "，"))
    if mk.get("dd") is not None and mk["dd"] <= -cfg.DROP_WARN:
        lines.append(f"⚠️ 離近 60 日高點已跌 {abs(mk['dd']) * 100:.1f}%")
    if mk["layer"] in (4, 5):
        s = mk["stab"]
        if s is None:
            lines.append("🧱 止跌確認：資料不足")
        elif s["vol_missing"]:
            lines.append("🧱 止跌確認：❌ 大盤成交量資料取不到，無法確認")
        else:
            ratio = s["recent_vol"] / s["dump_vol"] * 100 if s["dump_vol"] else 0
            ok = lambda b: "✅" if b else "❌"
            state = "✅ 通過" if s["ok"] else ("⏳ 觀察中" if s.get("watching") else "❌ 未通過")
            lines.append(
                f"🧱 止跌確認：{state}（三項都要通過）\n"
                f"　{ok(s['vol_ok'])} 量能：低點日成交額是爆量下跌日（{s['dump_date']:%m/%d}）的 {ratio:.0f}%，需 100% 以上\n"
                f"　{ok(s['shape_ok'])} K 棒：下影線占 {s['shadow'] * 100:.0f}%（需 ≥{cfg.STAB_SHADOW * 100:.0f}%），或收紅實體占 {s['body'] * 100:.0f}%（需 ≥{cfg.STAB_RED_BODY * 100:.0f}%）\n"
                f"　{ok(s['no_break'])} 不破低：低點日最低 {s['prior_low']:,.0f}，"
                f"之後 {s['days_after']} 天最低 {(s['recent_low'] if s['recent_low'] is not None else 0):,.0f}"
            )
            if s["ok"]:
                lines.append("📌 日線止跌走穩，先以 2～3 成資金佈局，不猜底、不貪心")
    if mk["active"]:
        names = cfg.names_join(mk["active"])
        lines.append(f"✅ 目前啟用的策略：{names}" + ("\n⚠️ 第 3 層方向未明，只開最保守的策略，部位減半" if mk["risk"] else ""))
        lines.append(f"💰 部位：{('過熱，維持正常部位，不加碼' if mk.get('tag') == '過熱' else cfg.LAYER_SIZE.get(mk['layer'], ''))}")
    else:
        lines.append("🔴 尚未止跌，不建議建立多頭部位")
    return lines


# =====================================================
# 3. 四套個股策略
# =====================================================
def _result(name, checks, **kw):
    return {"name": name, "ok": all(c[1] for c in checks), "checks": checks, **kw}


ATTACK_DAY_CHECKS = ("攻擊日：收盤突破盤整高點", "收紅 K", "攻擊日漲幅", "收盤位置", "攻擊日量能", "流動性")


def _attack(df, i, pk, S, name, base_checks, quality_checks, max_consol=None, **extra):
    """強勢續攻（創高拉回、噴出後整理）共用的「攻擊日」判斷。
    pk：前波高點那一天；S：策略自己的起漲價（噴出後整理用來判斷回檔，不再拿來算目標）。
    目標價＝盤整最低點 ＋ 整段漲幅（前波高點 − 前高之前 30 日內的最低點），兩套共用同一種算法。
    盤整區＝前波高點之後到昨天（4～10 天、離前高 ≤ 10%）；
    今天收盤第一次站上盤整高點、收紅 K、漲 ≥ 5%、收盤位置 ≥ 67%、量能 ≥ 前高那天的 9 成 → 攻擊日。
    回傳的 pre_ok＝「昨天為止的盤整條件都成立」，chase_px＝盤中追價點（漲 4% 且突破盤整高點）"""
    o, h, l, c, v = (df[x].to_numpy(float) for x in ("Open", "High", "Low", "Close", "Volume"))
    row = df.iloc[i]
    H = float(h[pk])
    n_cons = i - 1 - pk
    if n_cons >= 1:
        upper, lower = float(h[pk:i].max()), float(l[pk + 1:i].min())      # 盤整高點把前波高點也算進去
    else:
        upper = lower = float("nan")
    E = float(c[i])
    prev = float(c[i - 1])
    gain = E / prev - 1 if prev > 0 else float("nan")
    rng = h[i] - l[i]
    pos = (E - l[i]) / rng if rng > 0 else 1.0
    vol_pk = float(v[pk])
    vr = _ratio(float(v[i]), vol_pk)
    broke = n_cons >= 1 and E > upper
    mc = max_consol or cfg.ATTACK_MAX_CONSOL
    setup = [
        (f"盤整 {cfg.ATTACK_MIN_CONSOL}～{mc} 天",
         cfg.ATTACK_MIN_CONSOL <= n_cons <= mc,
         f"前波高點 {df.index[pk]:%m/%d} 之後已盤整 {max(n_cons, 0)} 天"),
        (f"盤整深度 ≤ {cfg.ATTACK_MAX_DEPTH * 100:.0f}%", bool(n_cons >= 1 and lower >= H * (1 - cfg.ATTACK_MAX_DEPTH)),
         f"盤整最低 {_fp(lower)}，離前高 {(1 - lower / H) * 100:.1f}%" if n_cons >= 1 else "還沒有盤整區"),
    ]
    attack_checks = [
        ("攻擊日：收盤突破盤整高點", bool(broke),
         f"盤整區 {_fp(lower)}～{_fp(upper)}，收盤 {_fp(E)}" if n_cons >= 1 else "還沒有盤整區"),
        ("收紅 K", bool(c[i] > o[i]), f"開盤 {_fp(o[i])}、收盤 {_fp(E)}"),
        ("攻擊日漲幅", bool(gain >= cfg.ATTACK_MIN_GAIN), f"今日漲 {gain * 100:+.1f}%（需 ≥ {cfg.ATTACK_MIN_GAIN * 100:.0f}%）"),
        ("收盤位置", bool(pos >= cfg.ATTACK_CLOSE_POS),
         f"最低 {_gauge(pos)} 最高；要 {cfg.ATTACK_CLOSE_POS * 100:.0f}% 以上"),
        ("攻擊日量能", bool(vol_pk > 0 and vr >= cfg.ATTACK_VOL_VS_PEAK),
         f"今日量 = 前高那天的 {vr * 100:.0f}%（需 ≥ {cfg.ATTACK_VOL_VS_PEAK * 100:.0f}%）"),
    ]
    checks = list(base_checks) + setup + list(quality_checks) + attack_checks
    pre_ok = all(ok for nm, ok, _ in checks if nm not in ATTACK_DAY_CHECKS)
    if n_cons < 1:
        return _result(name, checks, close=E, pre_ok=False, date=df.index[i], turnover=float(row["TURN20"]), **extra)
    chase_px = tick(max(prev * (1 + cfg.CHASE_GAIN), upper), "ceil")          # 今天的盤中追價點
    if broke:
        entry = E
    else:                                                    # 還不是攻擊日：明天的追價點（漲 4% 且突破盤整高點）
        entry = tick(max(E * (1 + cfg.CHASE_GAIN), upper, float(h[i])), "ceil")
    leg_lo = float(l[max(0, pk - cfg.PB_LEG_LOOKBACK):pk + 1].min())       # 整段漲幅的起點：前高之前 30 日內最低點
    target = tick(lower + (H - leg_lo))
    if target <= entry:
        checks.append(("目標價高於進場價", False, f"目標 {_fp(target)}、進場 {_fp(entry)}"))
    status = f"已突破 +{(E / H - 1) * 100:.1f}%" if E >= H else f"還差 {(1 - E / H) * 100:.1f}%"
    r = _result(name, checks, close=E, entry=entry, key=H, peak_date=df.index[pk], leg_start=leg_lo,
                cons_hi=upper, cons_lo=lower, attack=bool(broke), pre_ok=pre_ok, chase_px=chase_px, n_cons=n_cons,
                target=target, stop_after2=tick(H * (1 - cfg.ATTACK_STOP2_PCT)), status=status,
                date=df.index[i], turnover=float(row["TURN20"]), **extra)
    r["stop1"], r["stop1_note"] = attack_stop(r, entry)
    return r


def attack_stop(r, entry):
    """出場價＝盤整最低點；離買價超過 10% 時改用買價下 10%"""
    lo = tick(r["cons_lo"], "floor")
    cap = tick(entry * (1 - cfg.ATTACK_MAX_LOSS), "ceil")
    if lo >= cap:
        return lo, f"跌破盤整最低點 {lo}"
    return cap, f"盤整最低點 {lo} 太遠，改用買價下 {cfg.ATTACK_MAX_LOSS * 100:.0f}%"


def check_pullback(df, i, ratios=None, max_consol=None):
    """強勢續攻之一：創高拉回。近 30 日創新高 → 盤整（量縮、離前高 ≤ 10%）→ 攻擊日進場。df 需先 add_indicators"""
    if i < 69 or i >= len(df):
        return None
    row = df.iloc[i]
    lo = i - cfg.PB_LOOKBACK
    h = df["High"].to_numpy(float)
    win = h[lo:i]                                          # 不含今天
    pk = lo + int(win.argmax())
    H = float(h[pk])
    prior = h[:lo]
    prior_high = float(prior.max()) if len(prior) else float("nan")
    leg = df["Low"].to_numpy(float)[max(0, pk - cfg.PB_LEG_LOOKBACK):pk + 1]
    S = float(leg.min())
    v = df["Volume"].to_numpy(float)
    cons_vol = float(v[pk + 1:i].mean()) if i - 1 - pk >= 1 else float("nan")
    ema20, ema60 = float(row["MA20"]), float(row["MA60"])
    base = [
        _liq(row),
        ("前高是近期新高", bool(pd.isna(prior_high) or H >= prior_high * 0.98),
         f"前高 {_fp(H)}（{df.index[pk]:%m/%d}），更早的高點 {_fp(prior_high)}"),
        ("趨勢向上", bool(ema20 > ema60), f"月線 {_fp(ema20)}、季線 {_fp(ema60)}"),
    ]
    quality = [
        ("盤整量縮", bool(cons_vol == cons_vol and v[pk] > 0 and cons_vol <= cfg.PB_CONS_VOL_RATIO * v[pk]),
         f"盤整均量 = 前高那天的 {_ratio(cons_vol, v[pk]) * 100:.0f}%（需 ≤ {cfg.PB_CONS_VOL_RATIO * 100:.0f}%）"
         if cons_vol == cons_vol else "還沒有盤整區"),
    ]
    return _attack(df, i, pk, S, "pullback", base, quality, max_consol=max_consol)


def check_breakout(df, i):
    """低位階起漲（低檔爆量起漲，突破 30 日頸線）"""
    if i < 69 or i >= len(df):
        return None
    row = df.iloc[i]
    close, vol = float(row["Close"]), float(row["Volume"])
    mav20 = float(row["MAV20"])
    ema20, ema60 = float(row["MA20"]), float(row["MA60"])
    ema20_prev = float(df["MA20"].iloc[i - 1])
    low60 = float(df["Low"].iloc[i - 59:i + 1].min())
    neck = float(df["High"].iloc[i - cfg.BO_PLATFORM:i].max())
    rise = (close - low60) / low60 if low60 > 0 else float("nan")
    volx = _ratio(vol, mav20)
    bd = cfg.BO_BOX_DAYS
    box_hi = float(df["High"].iloc[i - bd:i].max())
    box_lo = float(df["Low"].iloc[i - bd:i].min())

    checks = [
        _liq(row),
        (f"盤整：近 {bd} 日低點 ≥ 高點 {cfg.BO_BOX_RATIO * 100:.0f}%", bool(box_lo >= cfg.BO_BOX_RATIO * box_hi),
         f"近 {bd} 日高 {_fp(box_hi)}、低 {_fp(box_lo)}（低點是高點的 {box_lo / box_hi * 100:.0f}%）"),
        (f"離60日低點漲幅 ≤ {cfg.BO_MAX_RISE * 100:.0f}%", bool(rise <= cfg.BO_MAX_RISE), f"目前 +{rise * 100:.1f}%"),
        (f"突破 {cfg.BO_PLATFORM} 日頸線", close > neck, f"頸線 {_fp(neck)}、收盤 {_fp(close)}"),
        (f"爆量 ≥ {cfg.BO_VOL_MULT:.1f} 倍", bool(volx >= cfg.BO_VOL_MULT), f"今日 {volx:.1f} 倍 20 日均量"),
        ("站上季線且月線翻揚", bool(close >= ema60 and ema20 > ema20_prev),
         f"月線 {_fp(ema20)}（昨 {_fp(ema20_prev)}）、季線 {_fp(ema60)}"),
    ]
    E = tick(close)
    stop1 = tick(min(neck * 0.98, E * (1 - cfg.BO_STOP_PCT)))
    return _result("breakout", checks, close=close, key=float(row["High"]), neckline=neck,
                   stop1=stop1, target=tick(E * (1 + cfg.BO_TARGET_PCT)),
                   stop1_note=f"頸線下 2%（{tick(neck * 0.98)}）與買價下 {cfg.BO_STOP_PCT * 100:.0f}%（{tick(E * (1 - cfg.BO_STOP_PCT))}）取較低",
                   date=df.index[i], turnover=float(row["TURN20"]))


def check_dry(df, i, mkt=None):
    """量縮抗跌。mkt：加權指數日線 DataFrame（用來比較抗跌）"""
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
        (f"{k}日不破前低", low3 > L, f"最近{k}日最低 {_fp(low3)}，前低 {_fp(L)}"),
    ]
    rs_ok, rs_detail = False, "大盤資料不足，無法比較"
    if mkt is not None and len(mkt):
        d = df.index[i]
        m = _naive(mkt)["Close"].loc[:d].iloc[-cfg.STAB_LOOKBACK:]
        if len(m) >= 5:
            m_high, m_date, m_now = float(m.max()), m.idxmax(), float(m.iloc[-1])
            m_drop = max(0.0, 1 - m_now / m_high)
            seg = df["Close"].loc[m_date:]                                   # 大盤高點那天起，個股的走勢
            if len(seg) >= 2:
                peak = seg.cummax()
                s_dd = float(((peak - seg) / peak).max())                    # 同一段期間的最大回檔（中間跌很深再反彈也算）
                rs_ok = bool(s_dd <= cfg.DRY_RS_RATIO * m_drop)
                rs_detail = (f"大盤自 {m_date:%m/%d} 高點跌 {m_drop * 100:.1f}%，個股同期最大回檔 {s_dd * 100:.1f}%"
                             f"（需 ≤ 大盤的 {cfg.DRY_RS_RATIO * 100:.0f}%）")
    checks.append(("比大盤抗跌", rs_ok, rs_detail))

    E = tick(close)
    w20 = df.iloc[i - cfg.DRY_KEY_LOOKBACK + 1:i + 1]
    key = float(w20["High"].max())
    low20 = float(w20["Low"].min())
    stop1 = tick(float(df["Low"].iloc[i - cfg.DRY_STOP_DAYS + 1:i + 1].min()))        # 出場價＝近 5 日最低點
    below = (key - close) / key if key > 0 else 1.0
    vl0 = vol_low(df.iloc[:i + 1])
    if vl0 and vl0["side"] == "壓":
        dist = (vl0["high"] - close) / vl0["high"]
        checks.append((f"離上方大量壓力 ≤ {cfg.DRY_MAX_PRESSURE_DIST * 100:.0f}%", bool(dist <= cfg.DRY_MAX_PRESSURE_DIST),
                       f"65日第1大量日（{vl0['date']:%m/%d}）最高點 {_fp(vl0['high'])}、現價 {_fp(close)}（差 {dist * 100:.1f}%）"))
    elif vl0:
        checks.append((f"離上方大量壓力 ≤ {cfg.DRY_MAX_PRESSURE_DIST * 100:.0f}%", True,
                       f"65日第1大量日（{vl0['date']:%m/%d}）最高點 {_fp(vl0['high'])} 已在現價之下，上方沒有這個壓力"))
    checks.append(("出場價低於買價", bool(stop1 < E), f"出場 {stop1}、買價 {E}"))
    target = tick(key)                                                                # 第一目標＝前高（突破價）
    # 第二目標（參考）：依序找上方最近的壓力 → 65 日第 1 大量日最高點（要在前高之上）→ 近 60 日高點 → 買價＋2.5 倍風險
    t2, t2_note = None, ""
    vl = vol_low(df.iloc[:i + 1])
    h60 = float(df["High"].iloc[max(0, i - cfg.DRY_T2_HIGH_DAYS + 1):i + 1].max())
    if vl and vl["side"] == "壓" and vl["high"] > key * 1.001:
        t2, t2_note = tick(vl["high"]), "65日第1大量日最高點"
    elif h60 > key * 1.001:
        t2, t2_note = tick(h60), f"近{cfg.DRY_T2_HIGH_DAYS}日高點"
    elif E > stop1:
        r2 = tick(E + cfg.DRY_T2_R * (E - stop1))
        if r2 > target:
            t2, t2_note = r2, f"買價＋{cfg.DRY_T2_R:g}倍風險"
    return _result("dry", checks, close=close, key=key, prior_low=L, stop1=stop1, target=target,
                   target2=t2, target2_note=t2_note,
                   stop1_note=f"跌破近 {cfg.DRY_STOP_DAYS} 日最低點 {stop1}", date=df.index[i], turnover=float(row["TURN20"]))


def check_flag(df, i, max_consol=None):
    """強勢續攻之二：噴出後整理。以昨天(j)為基準判斷型態，今天(i)判斷是否接近/突破前波高點"""
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
    seg = high[bp - 2:i]                                    # 噴出到昨天的最高點＝前波高點
    pk = bp - 2 + int(seg.argmax())
    H = float(high[pk])
    G = H - S
    mid = S + cfg.FLAG_MAX_PULLBACK * G
    cons_close, cons_ma = close[pk + 1:i], ma20[pk + 1:i]
    min_close = float(cons_close.min()) if len(cons_close) else float("nan")
    burst_vol = float(vol[bp - 2:pk + 1].mean())
    cons_vol = float(vol[pk + 1:i].mean()) if len(cons_close) else float("nan")
    base = [liq, ("噴出", True, f"{df.index[bp]:%m/%d} {kind}漲幅 {g * 100:.1f}%，量 {vmult:.1f} 倍均量")]
    quality = [
        (f"回檔 ≤ 噴出漲幅的 {cfg.FLAG_MAX_PULLBACK * 100:.0f}%", bool(len(cons_close) and min_close >= mid),
         f"噴出 {_fp(S)}→{_fp(H)}，要守住 {_fp(mid)}；盤整最低收盤 {_fp(min_close)}"),
        ("守月線(MA20)", bool(len(cons_close) and (cons_close >= cons_ma).all()),
         f"盤整期間收盤都在 MA20 之上" if len(cons_close) and (cons_close >= cons_ma).all() else "盤整期間有跌破 MA20"),
        (f"盤整量縮（≤ 噴出期 {cfg.FLAG_VOL_RATIO * 100:.0f}%）",
         bool(len(cons_close) and cons_vol <= cfg.FLAG_VOL_RATIO * burst_vol),
         f"盤整均量 = 噴出期均量的 {_ratio(cons_vol, burst_vol) * 100:.0f}%"),
    ]
    return _attack(df, i, pk, S, "flag", base, quality, max_consol=max_consol, burst_start=S, burst_gain=G)


def watch_tomorrow(raw, name):
    """明日追價名單：今天收盤後，盤整條件已經成立（盤整放寬到 15 天）的股票，算出明天的追價點。
    做法：在最後面補一根「明天」的假 K 線（價格＝今天收盤），用同一套條件判斷『昨天為止』的盤整。
    回傳策略結果（含 chase_px＝明天漲 4% 且突破盤整高點的價格），不符合回傳 None"""
    d = _naive(raw)
    if len(d) < 80:
        return None
    last = d.iloc[-1]
    nxt = d.index[-1] + pd.tseries.offsets.BDay(1)
    fake = pd.DataFrame({"Open": [last["Close"]], "High": [last["Close"]], "Low": [last["Close"]],
                         "Close": [last["Close"]], "Volume": [last["Volume"]]}, index=[nxt])
    df = add_indicators(pd.concat([d[["Open", "High", "Low", "Close", "Volume"]], fake]))
    i = len(df) - 1
    fn = check_pullback if name == "pullback" else check_flag
    r = fn(df, i, max_consol=cfg.WATCH_MAX_CONSOL)
    if not r or not r.get("pre_ok") or "chase_px" not in r:
        return None
    if not _liq(df.iloc[i - 1])[1]:
        return None
    if r["chase_px"] > float(last["Close"]) * (1 + cfg.CHASE_MAX_DIST) + 1e-9:     # 追價點太遠（要大漲才過盤整高點）
        return None
    r["turnover"] = float(df["TURN20"].iloc[i - 1])
    r["date"] = d.index[-1]
    return r


# =====================================================
# 4. 交易計畫
# =====================================================
def _breakeven(e, p2, stop2, t):
    """第 2 筆在 stop2 被砍掉後，第 1 筆要漲到多少，整趟才打平"""
    if t[0] <= 0:
        return None
    s1, s2 = (t[0] / e, t[1] / p2) if cfg.COST_BY_MONEY else (t[0], t[1])
    return tick(e + max(0.0, p2 - stop2) * s2 / s1)


def ladder_plan(E, K, stop1, target, ratios=None, key_label="突破價", stop1_note="", first_target=False,
                target2=None, target2_note=""):
    """其他三套策略的通用分批：第 1 筆 E、第 2 筆突破價 K、第 3 筆 K x 1.08"""
    t = norm_ratios(ratios)
    p1, k = tick(E), tick(K)
    p2 = max(k, p1)
    p3 = max(tick(K * cfg.EXT_RATIO), p2)
    stop2 = tick(k * (1 - cfg.STOP_ADD_PCT))
    notes = []
    tg = tick(target)
    if first_target:
        notes.append(f"ℹ️ 第一目標＝前高 {tg}，到了先獲利了結；突破後要不要續抱請自行判斷")
    elif p2 > p1 and tg <= p2:
        notes.append(f"⚠️ 目標價 {tg} 不高於第 2 筆加碼價 {p2}，加碼前就可能先到目標")
    elif tg <= p3:
        notes.append(f"⚠️ 目標價 {tg} 不高於第 3 筆加碼價 {p3}，第 3 筆實務上可能不會買進")
    st1 = tick(stop1)
    rr = (tg - p1) / (p1 - st1) if p1 > st1 else None                  # 風報比＝賺的空間 ÷ 賠的空間
    if rr is not None:
        notes.append(f"⚠️ 風報比 {rr:.1f}，低於 {cfg.RR_MIN}，賺的空間偏小" if rr < cfg.RR_MIN else f"ℹ️ 風報比 {rr:.1f}")
    if p1 >= k:
        notes.append("ℹ️ 現價已在突破價之上，第 2 筆可與第 1 筆一併買進")
    return {
        "ratios": t, "e": p1, "b": p2, "b2": p3, "key": k, "key_label": key_label,
        "avg2": tick(avg_cost([p1, p2], t[:2])), "avg3": tick(avg_cost([p1, p2, p3], t)),
        "stop1": tick(stop1), "stop1_note": stop1_note,
        "stop2": stop2, "breakeven": _breakeven(p1, p2, stop2, t),
        "targets": ([("第一目標（前高）", tg), (f"第二目標（{target2_note}）", tick(target2))] if target2 else [("", tg)]),
        "notes": notes, "rr": rr,
        "tranches": [("① 進場", p1, t[0]), ("② 突破", p2, t[1]), ("③ 加碼", p3, t[2])],
    }


def attack_plan(r, entry=None, split=None):
    """強勢續攻：攻擊日買 50%，站上前波高點（突破日）再買 50%。entry：實際買價（回測追價用）
    split：自訂兩筆比例（攻擊日, 突破日），不給就用 config 預設；會自動換算成合計 1"""
    t = tuple(float(x) for x in (split or cfg.ATTACK_SPLIT))
    if len(t) != 2 or any(x < 0 for x in t) or sum(t) <= 0:
        t = tuple(cfg.ATTACK_SPLIT)
    t = (t[0] / sum(t), t[1] / sum(t))
    e = tick(entry if entry is not None else r["entry"])
    stop1, note = attack_stop(r, e) if entry is not None else (r["stop1"], r.get("stop1_note", ""))
    k = tick(r["key"])
    b = max(k, e)
    notes = []
    if e >= k:
        notes.append("ℹ️ 攻擊日已經站上前波高點，兩筆同一天買進")
    after2 = max(r["stop_after2"], stop1)
    return {
        "ratios": t, "e": e, "b": b, "b2": b, "key": k, "key_label": "前波高點",
        "tranches": [("① 攻擊日", e, t[0]), ("② 突破日", b, t[1])],
        "avg2": tick(avg_cost([e, b], t)), "avg3": tick(avg_cost([e, b], t)),
        "stop1": stop1, "stop1_note": note, "stop_after2": after2, "stop2": after2,
        "breakeven": None, "targets": [("", r["target"])], "notes": notes,
    }


def plan_for(name, r, ratios=None, split=None):
    """依策略結果 r 產生交易計畫（split＝強勢續攻的兩筆比例）"""
    if r is None:
        return None
    if name in ("pullback", "flag"):
        return attack_plan(r, split=split)
    labels = {"breakout": "突破當天最高價", "dry": "近20日高點"}
    return ladder_plan(r["close"], r["key"], r["stop1"], r["target"], ratios,
                       key_label=f"突破{labels[name]}", stop1_note=r.get("stop1_note", ""),
                       first_target=(name == "dry"), target2=r.get("target2"), target2_note=r.get("target2_note", ""))


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
        names = cfg.names_join(go)
        risk = "（大盤第 3 層，部位減半）" if mk and mk["risk"] else ""
        return "go", f"✅ 符合【{names}】，且大盤目前適用{risk}"
    if ok_names:
        names = cfg.names_join(ok_names)
        return "watch", f"⚠️ 型態符合【{names}】，但目前大盤層級不適用這套策略，先觀察"
    if mk and not mk["active"]:
        return "stop", "🔴 大盤尚未止跌，不建議建立多頭部位；這檔也沒有符合的策略"
    return "wait", "⏳ 目前沒有符合的策略，可以先放進觀察名單，等條件成形"
