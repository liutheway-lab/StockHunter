"""crash_study.py — 一次性研究：大盤大回檔（從 60 日高點跌 ≥15%）前有沒有可用的預警、跌下來後怎麼更快確認
只在 GitHub Actions 手動執行（crash_study.yml）。結果印在日誌，並私訊管理者（不發群組）。
資料：加權指數 ^TWII 日線（Yahoo，抓得到多久用多久）。
"""
import os
import sys

import numpy as np
import pandas as pd

EVENT_DD = 0.15      # 大回檔：離近 60 日高點跌幅
LOOK = 60            # 高點回看天數
EARLY_DD = 0.05      # 「還沒跌太多」的界線（預警必須在跌 5% 之前出現才算預警）
HORIZON = 40         # 預警後多少交易日內要發生才算命中
GAP = 20             # 同一波訊號合併的間隔（交易日）


# ───────────── 資料 ─────────────
def load():
    path = os.getenv("CRASH_CSV")           # 測試用：本機 CSV（Date,Close,Volume）
    if path:
        df = pd.read_csv(path, index_col=0, parse_dates=True)
    else:
        import yfinance as yf
        df = yf.Ticker("^TWII").history(period="max", interval="1d", auto_adjust=False)
        df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[["Close", "Volume"]].dropna(subset=["Close"]).copy()
    df = df[df["Close"] > 0]
    return df


# ───────────── 指標 ─────────────
def features(df):
    c, v = df["Close"], df["Volume"].replace(0, np.nan)
    f = pd.DataFrame(index=df.index)
    f["c"] = c
    f["hi60"] = c.rolling(LOOK, min_periods=LOOK).max()
    f["dd"] = c / f["hi60"] - 1
    f["ma5"], f["ma10"] = c.rolling(5).mean(), c.rolling(10).mean()
    f["ma20"], f["ma60"] = c.rolling(20).mean(), c.rolling(60).mean()
    f["bias20"] = c / f["ma20"] - 1
    f["bias60"] = c / f["ma60"] - 1
    f["ret1"] = c.pct_change()
    f["ret10"] = c.pct_change(10)
    f["volx"] = v / v.rolling(20).mean().shift(1)
    f["vol20"] = f["ret1"].rolling(20).std()
    f["vol60"] = f["ret1"].rolling(60).std()
    f["ma20_dn"] = f["ma20"] < f["ma20"].shift(5)
    f["near_hi"] = f["dd"] >= -0.03
    return f


def find_events(f):
    """事件：dd 首次 ≤ -15%；起點＝當時 60 日高點那天；dd 回到 > -5% 才能再算下一個事件"""
    ev, armed = [], True
    idx = f.index
    for i in range(LOOK, len(f)):
        dd = f["dd"].iloc[i]
        if armed and dd <= -EVENT_DD:
            win = f["c"].iloc[i - LOOK + 1:i + 1]
            peak_i = i - LOOK + 1 + int(np.argmax(win.values))
            ev.append(dict(peak=peak_i, hit=i))
            armed = False
        elif not armed and dd > -EARLY_DD:
            armed = True
    return ev


# ───────────── 預警訊號 ─────────────
def warn_signals(f):
    s = {}
    for th in (0.08, 0.10, 0.12):
        s[f"季線乖離≥{th:.0%}"] = f["bias60"] >= th
    for th in (0.05, 0.07):
        s[f"月線乖離≥{th:.0%}"] = f["bias20"] >= th
    s["10日急漲≥8%"] = f["ret10"] >= 0.08
    s["高檔爆量長黑"] = (f["near_hi"].rolling(5).max() > 0) & (f["ret1"] <= -0.02) & (f["volx"] >= 1.5)
    s["高檔單日跌≥3%"] = (f["near_hi"].rolling(5).max() > 0) & (f["ret1"] <= -0.03)
    s["高檔震盪放大(20日波動≥1.5倍60日)"] = f["near_hi"] & (f["vol20"] >= 1.5 * f["vol60"])
    s["高檔跌破5日10日線"] = f["near_hi"].shift(1, fill_value=False) & (f["c"] < f["ma5"]) & (f["c"] < f["ma10"])
    return s


def episodes(sig, gap=GAP):
    """把連續出現的訊號合併成『波』，回傳每波第一天的位置"""
    pos, last = [], -10 ** 9
    for i in np.flatnonzero(sig.fillna(False).values):
        if i - last > gap:
            pos.append(i)
        last = i
    return pos


def eval_warn(f, events, sigs):
    n = len(f)
    peaks = np.array([e["peak"] for e in events])
    # 基準：任一天之後 HORIZON 日內出現事件起點的機率
    base = np.mean([(peaks[(peaks > i) & (peaks <= i + HORIZON)].size > 0) for i in range(LOOK, n - HORIZON)])
    rows = []
    for name, sig in sigs.items():
        sig = sig.fillna(False) & (f["dd"] > -EARLY_DD)         # 預警：還沒跌 5% 之前
        eps = episodes(sig)
        if not eps:
            continue
        hit_ep = sum(1 for i in eps if ((peaks > i - 1) & (peaks <= i + HORIZON)).any())
        caught, leads = 0, []
        for p in peaks:
            w = np.flatnonzero(sig.values[max(0, p - 15):p + 3])
            if w.size:
                caught += 1
                leads.append(p - (max(0, p - 15) + w[0]))
        rows.append(dict(name=name, eps=len(eps), prec=hit_ep / len(eps), lift=hit_ep / len(eps) / max(base, 1e-9),
                         recall=caught / max(len(peaks), 1), lead=float(np.median(leads)) if leads else np.nan))
    return base, rows


# ───────────── 快速確認：跌破 5% 之後，會不會真的變 15%？ ─────────────
def confirm_study(f):
    """每一波回檔：首次跌到 -5%（距高點）的那天為觀察日；看當天特徵，統計之後是否跌到 -15%"""
    rows, armed = [], True
    for i in range(LOOK, len(f)):
        dd = f["dd"].iloc[i]
        if armed and dd <= -EARLY_DD:
            win = f["c"].iloc[i - LOOK + 1:i + 1]
            peak_i = i - LOOK + 1 + int(np.argmax(win.values))
            fut = f["dd"].iloc[i:i + 61]
            # 以「這波高點」為基準，往後 60 日內是否到 -15%
            base_hi = f["c"].iloc[peak_i]
            lo = f["c"].iloc[i:i + 61].min() / base_hi - 1
            r = f.iloc[i]
            rows.append(dict(i=i, days=i - peak_i, big=lo <= -EVENT_DD,
                             fast=(i - peak_i) <= 5,
                             volx3=f["volx"].iloc[max(0, i - 2):i + 1].max() >= 1.5,
                             below_ma20=r["c"] < r["ma20"], below_ma60=r["c"] < r["ma60"],
                             ma20_dn=bool(r["ma20_dn"]),
                             gap2=(f["ret1"].iloc[max(0, i - 2):i + 1].min() <= -0.025)))
            armed = False
        elif not armed and dd > -0.02:
            armed = True
    return pd.DataFrame(rows)


def eval_confirm(cdf):
    out = []
    tot = len(cdf)
    base = cdf["big"].mean() if tot else np.nan
    out.append(("全部（跌 5% 當天）", tot, base))
    cases = [
        ("5 天內快速跌 5%", cdf["fast"]),
        ("近 3 天有爆量(≥1.5倍)", cdf["volx3"]),
        ("近 3 天有單日跌≥2.5%", cdf["gap2"]),
        ("已跌破月線", cdf["below_ma20"]),
        ("已跌破季線", cdf["below_ma60"]),
        ("月線已下彎", cdf["ma20_dn"]),
        ("快速跌 + 爆量", cdf["fast"] & cdf["volx3"]),
        ("快速跌 + 單日大跌", cdf["fast"] & cdf["gap2"]),
        ("跌破月線 + 爆量", cdf["below_ma20"] & cdf["volx3"]),
    ]
    for name, m in cases:
        k = int(m.sum())
        out.append((name, k, cdf.loc[m, "big"].mean() if k else np.nan))
    return out


# ───────────── 現行系統對照：跌破兩條均線時，已經跌多少？ ─────────────
def layer_delay(f, events):
    rows = []
    for e in events:
        p, h = e["peak"], e["hit"]
        seg = f.iloc[p:h + 1]
        bear = (seg["c"] < seg["ma20"]) & (seg["c"] < seg["ma60"]) & seg["ma20_dn"]
        if bear.any():
            j = int(np.argmax(bear.values))
            rows.append((f.index[p], f["dd"].iloc[p + j], j))
        else:
            rows.append((f.index[p], np.nan, np.nan))
    return rows


# ───────────── 轉空確認規則比較（誰比現行更早、又不太誤報）─────────────
def days_since_high(c):
    arr = c.values
    out = np.full(len(arr), np.nan)
    for i in range(LOOK - 1, len(arr)):
        out[i] = LOOK - 1 - int(np.argmax(arr[i - LOOK + 1:i + 1]))
    return pd.Series(out, index=c.index)


def rule_signals(f):
    dh = days_since_high(f["c"])
    rmin = f["ret1"].rolling(5).min()
    return {
        "現行：破月季線且月線下彎": (f["c"] < f["ma20"]) & (f["c"] < f["ma60"]) & f["ma20_dn"],
        "A 5日內離高點跌5%": (f["dd"] <= -0.05) & (dh <= 5),
        "B 急跌5%＋單日跌2.5%": (f["dd"] <= -0.05) & (dh <= 7) & (rmin <= -0.025),
        "C 跌5%且破月線": (f["dd"] <= -0.05) & (f["c"] < f["ma20"]),
        "D 單純跌7%": f["dd"] <= -0.07,
        "E 跌4%破月線5<10日": (f["dd"] <= -0.04) & (f["c"] < f["ma20"]) & (f["ma5"] < f["ma10"]),
    }


def eval_rules(f, events, sigs):
    c, n = f["c"].values, len(f)
    idx = range(LOOK, n - 40)
    b_more = np.median([c[i + 1:i + 41].min() / c[i] - 1 for i in idx])
    b_up = np.mean([c[i + 20] > c[i] for i in idx])
    rows = []
    for name, sig in sigs.items():
        eps = [i for i in episodes(sig) if i + 40 < n]
        if not eps:
            continue
        dd0 = np.array([f["dd"].iloc[i] for i in eps])
        more = np.array([c[i + 1:i + 41].min() / c[i] - 1 for i in eps])
        up20 = np.array([c[i + 20] > c[i] for i in eps])
        caught = sum(1 for e in events if sig.values[e["peak"]:e["hit"] + 1].any())
        rows.append(dict(name=name, n=len(eps), at=np.median(dd0), more=np.median(more),
                         false=float(np.mean(more > -0.03)), up=float(up20.mean()),
                         caught=caught, tot=len(events)))
    return b_more, b_up, rows


# ───────────── 帳戶模擬：觸發就空手，止跌確認才回來（多市場）─────────────
INDICES = [("^TWII", "台灣加權"), ("^GSPC", "美國S&P500"), ("^N225", "日本日經"),
           ("^KS11", "韓國綜合"), ("^HSI", "香港恆生")]
START = "1997-01-01"     # 各市場用同一段期間比較
COST = 0.001             # 每次進出成本 0.1%
REB, HOLD = 0.05, 2      # 止跌確認：離低點反彈 5%、低點後 2 天不破低


def load_ticker(t):
    import yfinance as yf
    df = yf.Ticker(t).history(period="max", interval="1d", auto_adjust=False)
    df.index = pd.to_datetime(df.index).tz_localize(None)
    df = df[["Close", "Volume"]].dropna(subset=["Close"])
    df = df[(df["Close"] > 0) & (df.index >= START)]
    return df


def simulate(f, sig):
    """sig＝None 代表一直持有。回傳 (年化, 最大回檔, 在場比例, 每年進出次數)"""
    c = f["c"].values
    n = len(c)
    ma20, ma60 = f["ma20"].values, f["ma60"].values
    up20 = (~f["ma20_dn"]).values
    s = np.zeros(n, bool) if sig is None else sig.fillna(False).values.astype(bool)
    pos = np.ones(n)
    held, low, low_i, guard, sw = True, None, None, None, 0
    for i in range(LOOK, n):
        if sig is None:
            break
        if held:
            edge = s[i] and not s[i - 1]
            broke = guard is not None and c[i] < guard
            if edge or broke:
                held, low, low_i, guard = False, c[i], i, None
                sw += 1
        else:
            if c[i] < low:
                low, low_i = c[i], i
            stab = c[i] >= low * (1 + REB) and i - low_i >= HOLD
            trend = c[i] > ma20[i] and c[i] > ma60[i] and up20[i]
            if stab or trend:
                held = True
                guard = low if (stab and not trend) else None   # 靠止跌回來的，跌破低點就再出場
                sw += 1
        pos[i] = 1.0 if held else 0.0
    ret = np.zeros(n)
    ret[1:] = c[1:] / c[:-1] - 1
    r = np.zeros(n)
    r[LOOK + 1:] = pos[LOOK:-1] * ret[LOOK + 1:] - COST * np.abs(np.diff(pos[LOOK - 1:-1]))
    eq = np.cumprod(1 + r[LOOK:])
    years = max((f.index[-1] - f.index[LOOK]).days / 365.25, 1e-9)
    cagr = eq[-1] ** (1 / years) - 1
    mdd = float(np.min(eq / np.maximum.accumulate(eq) - 1))
    return cagr, mdd, float(pos[LOOK:].mean()), sw / years


def equity_all():
    if os.getenv("CRASH_CSV"):
        sets = [("測試", load())]
    else:
        sets = []
        for t, name in INDICES:
            try:
                d = load_ticker(t)
                if len(d) > 500:
                    sets.append((name, d))
                else:
                    print(f"{name} 資料太少，跳過")
            except Exception as e:
                print(f"{name} 抓取失敗：{e!r}")
    res = []
    for name, d in sets:
        f = features(d)
        sigs = rule_signals(f)
        row = {"一直持有": simulate(f, None)}
        for k, sg in sigs.items():
            row[k] = simulate(f, sg)
        res.append((name, d, row))
    return res


# ───────────── 報告（字卡：每段一則訊息，<pre> 區塊不換行）─────────────
import html as _html


def card(lines):
    return "<pre>" + _html.escape("\n".join(lines).rstrip()) + "</pre>"


def _fmt(v, spec):
    return "-" if v is None or np.isnan(v) else format(v, spec)


def build(df):
    f = features(df)
    ev = find_events(f)
    msgs = []

    head = [f"大回檔研究 {df.index[0]:%Y}～{df.index[-1]:%Y/%m}",
            f"定義：離60日高點跌≥{EVENT_DD:.0%}",
            f"共 {len(ev)} 次", ""]
    for e in ev[-8:]:
        head += [f"{f.index[e['peak']]:%Y-%m-%d} 高點",
                 f"　{f.index[e['hit']]:%m-%d} 跌破15%（{e['hit'] - e['peak']}天）", ""]
    msgs.append(card(head))

    base, rows = eval_warn(f, ev, warn_signals(f))
    lines = ["一、崩跌前預警", "（跌5%之前出現）", "",
             f"隨機一天，{HORIZON}天內", f"開始大回檔的機率 {base:.0%}", ""]
    for r in sorted(rows, key=lambda r: -r["lift"]):
        lead = "沒抓到" if np.isnan(r["lead"]) else f"提前{r['lead']:.0f}天"
        lines += [r["name"],
                  f"　命中{r['prec']:.0%}｜{r['lift']:.1f}倍",
                  f"　抓到{r['recall']:.0%}｜{lead}", ""]
    lines.append("留下條件：倍數>1.5 且抓到夠多")
    msgs.append(card(lines))

    cdf = confirm_study(f)
    lines = ["二、跌5%之後快速確認", "之後真的跌到15%的比例", ""]
    for name, k, rate in eval_confirm(cdf):
        lines += [name, f"　次數{k}｜{_fmt(rate, '.0%')}", ""]
    msgs.append(card(lines))

    lines = ["三、現行確認要等多久", "（跌破月線、季線且月線下彎）", ""]
    for d, dd, j in layer_delay(f, ev)[-8:]:
        lines.append(f"{d:%Y-%m-%d} 高點")
        lines.append("　跌15%都沒確認" if np.isnan(dd) else f"　第{int(j)}天、跌{abs(dd):.1%}才確認")
        lines.append("")
    msgs.append(card(lines))
    return msgs


def build_rules(df):
    f = features(df)
    ev = find_events(f)
    b_more, b_up, rows = eval_rules(f, ev, rule_signals(f))
    lines = ["轉空確認規則比較", f"資料 {df.index[0]:%Y}～{df.index[-1]:%Y/%m}", "",
             "隨便一天當基準：", f"　之後40天再跌 中位{abs(b_more):.1%}", f"　20天後上漲 {b_up:.0%}", ""]
    for r in rows:
        lines += [r["name"],
                  f"　觸發{r['n']}次｜誤報{r['false']:.0%}",
                  f"　觸發時已跌 中位{abs(r['at']):.1%}",
                  f"　之後再跌 中位{abs(r['more']):.1%}",
                  f"　20天後上漲 {r['up']:.0%}",
                  f"　抓到大回檔 {r['caught']}/{r['tot']}", ""]
    lines += ["誤報＝之後40天再跌不到3%", "好規則：觸發早、再跌多、誤報少"]
    return [card(lines)]


def build_equity():
    res = equity_all()
    msgs = []
    for name, d, row in res:
        lines = [f"{name} {d.index[0]:%Y}～{d.index[-1]:%Y/%m}", ""]
        for k, (cg, md, ex, sw) in row.items():
            lines += [k, f"　年化{cg:.1%}｜最大回檔{abs(md):.0%}"]
            if k != "一直持有":
                lines.append(f"　在場{ex:.0%}｜每年進出{sw:.1f}次")
            lines.append("")
        msgs.append(card(lines))
    if res:
        keys = list(res[0][2].keys())
        cur = keys[1]
        lines = [f"{len(res)} 個市場平均", "（年化｜最大回檔）", ""]
        for k in keys:
            cg = np.mean([r[2][k][0] for r in res])
            md = np.mean([r[2][k][1] for r in res])
            lines += [k, f"　{cg:.1%}｜{abs(md):.0%}"]
            if k not in ("一直持有", cur):
                win = sum(1 for r in res
                          if r[2][k][0] / max(abs(r[2][k][1]), 1e-9) > r[2][cur][0] / max(abs(r[2][cur][1]), 1e-9))
                lines.append(f"　比現行好 {win}/{len(res)} 個市場")
            lines.append("")
        lines += ["比現行好＝年化÷最大回檔較高", "出場：規則剛觸發", "回來：止跌確認或站回月季線",
                  f"每次進出扣 {COST:.1%} 成本"]
        msgs.insert(0, card(lines))
    return msgs


def push(msgs):
    import requests
    owner = os.getenv("TG_OWNER_ID", "").strip()
    tok = os.getenv("TG_BOT_TOKEN", "").strip()
    if not (owner and tok):
        print("沒有設定 TG_OWNER_ID，只印在日誌")
        return
    for m in msgs:
        requests.post(f"https://api.telegram.org/bot{tok}/sendMessage",
                      json={"chat_id": owner, "text": m, "parse_mode": "HTML"}, timeout=20)


if __name__ == "__main__":
    data = load()
    mode = sys.argv[1] if len(sys.argv) > 1 else "equity"
    msgs = {"rules": lambda: build_rules(data), "all": lambda: build(data)}.get(mode, build_equity)()
    for m in msgs:
        print(_html.unescape(m.replace("<pre>", "").replace("</pre>", "")))
        print("-" * 20)
    if not os.getenv("CRASH_CSV"):
        try:
            push(msgs)
        except Exception as e:
            print("傳到 Telegram 失敗：", repr(e))
    sys.exit(0)
