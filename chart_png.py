"""chart_png.py — 畫成圖片（PNG）傳到 Telegram 的圖
market_chart(mk)：加權指數近 45 日 K 線 + 四條均線 + 成交金額。
空頭層級會再標出「前低」「爆量下跌日」「近 3 日」，讓止跌確認的數字可以直接對照到是哪一天。
K 線顏色是台股習慣：紅 = 上漲、綠 = 下跌（跟看盤軟體一致）。通知裡的 🟢 利多／🔴 利空燈號是另一套規則，不受影響。
"""
import io

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

UP, DOWN = "#e04a4a", "#2e9e4f"
MA20_C, MA60_C = "#8e44ad", "#2980b9"
FONTS = ["Noto Sans CJK TC", "Noto Sans CJK JP", "Noto Sans CJK SC", "Microsoft JhengHei", "PingFang TC",
         "Arial Unicode MS", "DejaVu Sans"]


def _pos(index, d):
    """日期 d 在圖上的第幾根（不在範圍內回傳 None）"""
    if d is None or d not in index:
        return None
    return index.get_loc(d)


BADGE = "#1c7ed6"      # 現價標籤的顏色（藍底白字，跟紅漲綠跌、各種虛線都分得開）


def _price_header(fig, close, decimals=2, y=0.925):
    """標題下方一行：現價、漲跌點、幅度（紅漲綠跌）"""
    if len(close) < 2:
        return
    last, prev = float(close.iloc[-1]), float(close.iloc[-2])
    chg = last - prev
    pct = chg / prev * 100 if prev else 0.0
    col = UP if chg > 0 else DOWN if chg < 0 else "#555"
    arrow = "▲" if chg > 0 else "▼" if chg < 0 else "─"
    fig.text(0.02, y, f"{last:,.{decimals}f}　{arrow} {abs(chg):,.{decimals}f}（{pct:+.2f}%）", fontsize=13, color=col,
             fontweight="bold", ha="left", va="top")


def _price_line(ax, d, last, decimals=2, avoid=()):
    """現價：整張圖畫一條藍色虛線，字標在線上方，位置挑「沒有 K 線、沒有其他標籤」的地方，不會壓到字。
    avoid＝其他已經在圖上的文字（y 值），d＝畫出來的那些日線"""
    n = len(d)
    ylo, yhi = ax.get_ylim()
    rng = yhi - ylo
    ax.axhline(last, color=BADGE, linestyle=(0, (5, 3)), linewidth=1.3, zorder=4)
    h, w = rng * 0.07, 13                       # 字大約佔的高度（價格）和寬度（根數）
    y0, y1 = last + rng * 0.008, last + rng * 0.008 + h
    if y1 > yhi - rng * 0.02:                    # 太靠上緣就改標在線下方
        y1 = last - rng * 0.008
        y0 = y1 - h
    hi, lo = d["High"].to_numpy(float), d["Low"].to_numpy(float)
    best = None
    for x0 in sorted(set([1, 8, 18, 28, 38, 48, max(n - w - 1, 1)])):
        if x0 + w > n:
            continue
        pen = int(((lo[x0:x0 + w] <= y1) & (hi[x0:x0 + w] >= y0)).sum())
        pen += sum(6 for ay in avoid if y0 - rng * 0.03 <= ay <= y1 and x0 + w >= n - 17)   # 右側的壓力、支撐字
        if y1 > yhi - rng * 0.10 and x0 < 24:                                              # 左上角圖例
            pen += 6
        if best is None or pen < best[0]:
            best = (pen, x0)
    x0 = best[1] if best else 1
    ax.text(x0, y0 if y0 > last else y0, f"現價 {last:,.{decimals}f}", fontsize=9, color="white", fontweight="bold",
            ha="left", va="bottom" if y0 > last else "bottom", zorder=8,
            bbox=dict(boxstyle="round,pad=0.2", fc=BADGE, ec="none", alpha=0.95))


def market_chart(mk, bars=45):
    """回傳 PNG 的 bytes"""
    plt.rcParams["font.sans-serif"] = FONTS
    plt.rcParams["axes.unicode_minus"] = False
    full = mk["df"]
    c = full["Close"]
    ma5, ma10, ma20, ma60 = (c.rolling(n).mean().iloc[-bars:] for n in (5, 10, 20, 60))  # 均線先用完整資料算，再截取
    d = full.iloc[-bars:]
    idx = d.index
    x = list(range(len(d)))
    stab_mode = mk.get("layer") in (4, 5) and bool(mk.get("stab"))   # 空頭層級才畫止跌確認的標註
    s = (mk.get("stab") or {}) if stab_mode else {}
    has_vol = bool(d["Volume"].notna().any())
    nm = "低點日"

    fig, axes = plt.subplots(2 if has_vol else 1, 1, figsize=(8, 6.2 if has_vol else 4.2), dpi=150,
                             sharex=True, gridspec_kw={"height_ratios": [3, 1.3]} if has_vol else None)
    ax = axes[0] if has_vol else axes
    axv = axes[1] if has_vol else None

    # --- K 線 ---
    for xi, (o, h, l, cl) in enumerate(zip(d["Open"], d["High"], d["Low"], d["Close"])):
        col = UP if cl >= o else DOWN
        ax.vlines(xi, l, h, color=col, linewidth=1)
        ax.bar(xi, max(abs(cl - o), (h - l) * 0.02), bottom=min(o, cl), width=0.65, color=col)
    ax.plot(x, ma5.values, color="#e6a700", linewidth=0.8, label="5日")
    ax.plot(x, ma10.values, color="#16a085", linewidth=0.8, label="10日")
    ax.plot(x, ma20.values, color=MA20_C, linewidth=1.2, label="月線")
    ax.plot(x, ma60.values, color=MA60_C, linewidth=1.2, label="季線")

    # --- 近 3 日（灰色區） ---
    r0, r1 = _pos(idx, s.get("recent_start")), _pos(idx, s.get("recent_end"))
    if r0 is not None and r1 is not None:
        for a in (ax, axv):
            if a is not None:
                a.axvspan(r0 - 0.5, r1 + 0.5, color="#f1c40f", alpha=0.18, lw=0)

    # --- 前低 / 近 3 日最低 ---
    top_v, bot_v = float(d["High"].max()), float(d["Low"].min())
    rng_v = top_v - bot_v
    # 讓 MA60 盡量出現在圖裡（最多把範圍放大 35%，避免 K 線被壓扁）
    ext = rng_v * 0.35
    if ma60.notna().any():
        bot_v = min(bot_v, max(float(ma60.min()), bot_v - ext))
        top_v = max(top_v, min(float(ma60.max()), top_v + ext))
        rng_v = top_v - bot_v
    pl, pld = s.get("prior_low"), s.get("prior_low_date")
    rl, rld = s.get("recent_low"), s.get("recent_low_date")
    lowest = min([v for v in (bot_v, pl, rl) if v is not None])
    ax.set_ylim(lowest - rng_v * (0.20 if stab_mode else 0.04), top_v + rng_v * 0.04)    # 空頭時下方留白放標註
    if pl is not None:
        ax.axhline(pl, color="#00a8d6", linestyle="--", linewidth=1)
        p = _pos(idx, pld)
        if p is not None:
            ax.scatter([p], [pl], marker="^", color="#00a8d6", zorder=5, s=34)
            ax.annotate(f"{nm}最低 {pl:,.0f}（{pld:%m/%d}）", xy=(p, pl),
                        xytext=(max(p - 20, 0.5), lowest - rng_v * 0.05), fontsize=9, color="#0086ad",
                        arrowprops=dict(arrowstyle="->", color="#00a8d6", linewidth=0.9))
        else:
            ax.text(0.5, pl + rng_v * 0.02, f"{nm}最低 {pl:,.0f}", fontsize=9, color="#333")
    if rl is not None and _pos(idx, rld) is not None:
        rp = _pos(idx, rld)
        ax.scatter([rp], [rl], color="#8e6bd8", zorder=5, s=28)
        ax.annotate(f"之後最低 {rl:,.0f}（{rld:%m/%d}）", xy=(rp, rl),
                    xytext=(max(rp - 15, 0), lowest - rng_v * 0.13), fontsize=9, color="#8e6bd8",
                    arrowprops=dict(arrowstyle="->", color="#8e6bd8"))
    dd0 = s.get("dump_date")
    dpp = _pos(idx, dd0) if dd0 is not None else None
    if dpp is not None:                                   # 爆量下跌日那根 K 線也用橘色粗框框起來
        ax.add_patch(plt.Rectangle((dpp - 0.7, float(d["Low"].iloc[dpp]) - rng_v * 0.015), 1.4,
                                   float(d["High"].iloc[dpp] - d["Low"].iloc[dpp]) + rng_v * 0.03, fill=False,
                                   edgecolor="#ff8c00", linewidth=1.1, zorder=6))
    ax.set_ylabel("指數")
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=4)
    ax.grid(alpha=0.15)

    # --- 成交金額 ---
    if has_vol:
        v = d["Volume"] / 1e8
        cols = [UP if cl >= o else DOWN for o, cl in zip(d["Open"], d["Close"])]
        axv.bar(x, v.fillna(0).values, color=cols, width=0.65)
        dd, dv = s.get("dump_date"), s.get("dump_vol")
        if dd is not None and dv:
            dp = _pos(idx, dd)
            axv.axhline(dv / 1e8, color="#ff8c00", linestyle="--", linewidth=1.2)
            if dp is not None:
                axv.add_patch(plt.Rectangle((dp - 0.5, 0), 1.0, dv / 1e8 * 1.03, fill=False,
                                            edgecolor="#ff8c00", linewidth=1.1, zorder=6, clip_on=False))
                axv.annotate(f"爆量下跌日（{dd:%m/%d}）", xy=(dp, dv / 1e8),
                             xytext=(dp - 1, dv / 1e8 * 1.03), fontsize=9, color="#e67300", ha="right", fontweight="bold")
        rv = s.get("recent_vol")
        sp = _pos(idx, s.get("vol_date", pld))
        if rv and dv and sp is not None:
            axv.annotate(f"低點日量 = 爆量日的 {rv / dv * 100:.0f}%", xy=(sp, rv / 1e8),
                         xytext=(sp, max(float(v.max()), dv / 1e8) * 1.14), fontsize=9, color="#0086ad", ha="right",
                         arrowprops=dict(arrowstyle="->", color="#00a8d6"))
        axv.set_ylabel("成交金額（證交所，億）")
        axv.grid(alpha=0.15)
        axv.set_ylim(0, max(float(v.max()), (dv or 0) / 1e8) * 1.3)

    # --- X 軸日期 ---
    last_ax = axv if has_vol else ax
    step = max(1, len(d) // 9)
    ticks = list(range(0, len(d), step))
    last_ax.set_xticks(ticks)
    last_ax.set_xticklabels([idx[t].strftime("%m/%d") for t in ticks], fontsize=8)
    last_ax.set_xlim(-1, len(d))
    _price_line(ax, d, float(d["Close"].iloc[-1]), 0)

    if stab_mode:
        verdict = "通過" if mk["stab"].get("ok") else ("無法確認" if mk["stab"].get("vol_missing") else "未通過")
        title = f"加權指數 {idx[0]:%m/%d}–{idx[-1]:%m/%d}　第{mk['layer']}層・{mk['layer_name']}　止跌確認：{verdict}（黃色區 = {nm}起）"
    else:
        title = f"加權指數 {idx[0]:%m/%d}–{idx[-1]:%m/%d}　第{mk['layer']}層・{mk['layer_name']}"
    fig.suptitle(title, fontsize=11, x=0.02, ha="left")
    if stab_mode:
        for k, (txt, col) in enumerate([("■ 黃色區＝低點日起（最近 3 天內股價最低的那天）", "#b8860b"),
                                        ("┄ 水藍虛線＝低點日的最低點（上圖）", "#0086ad"),
                                        ("┄ 橘色虛線＝爆量下跌日的成交額（下圖）", "#e67300")][::-1]):
            fig.text(0.02, 0.008 + k * 0.026, txt, fontsize=8.5, color=col, ha="left", va="bottom")
    _price_header(fig, full["Close"])
    fig.tight_layout(rect=(0, 0.09 if stab_mode else 0, 1, 0.915))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


# =====================================================
# 個股線圖（跟大盤同一種風格：K 線 + 均線 + 成交量，右邊標價位）
# =====================================================
VOL_MULT = 1.5     # 大量＝當天量至少是當時 20 日均量的 1.5 倍，普通的量不標


def vol_levels(df, bars=65):
    """壓力／支撐（近 bars 日）：收盤在現價之上的天裡，量最大那天的最高點＝壓力；
    收盤在現價之下的天裡，量最大那天的最低點＝支撐。量要夠大（>= 20 日均量 1.5 倍）才算。
    回傳 {"press": {...} 或 None, "supp": {...} 或 None}，每項有 day、level、vol（張）、top（是不是整段最大量）"""
    full = df[df["Volume"] > 0]
    d = full.iloc[-bars:]
    if len(d) < 2:
        return {"press": None, "supp": None}
    vma = full["Volume"].rolling(20, min_periods=5).mean().reindex(d.index)
    last = float(d["Close"].iloc[-1])
    top_day = d["Volume"].idxmax()
    out = {}
    for key, mask, col in (("press", d["Close"] > last, "High"), ("supp", d["Close"] < last, "Low")):
        cand = d[mask & (d["Volume"] >= vma * VOL_MULT)]
        if cand.empty:
            out[key] = None
            continue
        day = cand["Volume"].idxmax()
        out[key] = {"day": day, "level": float(d.loc[day, col]), "vol": float(d.loc[day, "Volume"]) / 1000.0,
                    "top": day == top_day}
    return out


def stock_chart(df, title="", bars=65):
    """個股日線圖，回傳 PNG 的 bytes。df 需有 Open/High/Low/Close/Volume（Volume 是股數）"""
    plt.rcParams["font.sans-serif"] = FONTS
    plt.rcParams["axes.unicode_minus"] = False
    full = df[df["Volume"] > 0]
    c = full["Close"]
    ma5, ma10, ma20, ma60 = (c.rolling(n).mean().iloc[-bars:] for n in (5, 10, 20, 60))
    vma20 = (full["Volume"] / 1000.0).rolling(20).mean().iloc[-bars:]
    d = full.iloc[-bars:]
    idx = d.index
    x = list(range(len(d)))
    n = len(d)
    last = float(d["Close"].iloc[-1])

    fig, (ax, axv) = plt.subplots(2, 1, figsize=(8, 6.2), dpi=150, sharex=True, gridspec_kw={"height_ratios": [3, 1.3]})
    for xi, (o, h, l, cl) in enumerate(zip(d["Open"], d["High"], d["Low"], d["Close"])):
        col = UP if cl >= o else DOWN
        ax.vlines(xi, l, h, color=col, linewidth=1)
        ax.bar(xi, max(abs(cl - o), (h - l) * 0.02), bottom=min(o, cl), width=0.65, color=col)
    ax.plot(x, ma5.values, color="#e6a700", linewidth=0.8, label="5日")
    ax.plot(x, ma10.values, color="#16a085", linewidth=0.8, label="10日")
    ax.plot(x, ma20.values, color=MA20_C, linewidth=1.2, label="月線")
    ax.plot(x, ma60.values, color=MA60_C, linewidth=1.2, label="季線")

    # --- 右側標價位：前高、大量日的最高／最低點 ---
    hi_i = int(d["High"].to_numpy().argmax())
    hi_v = float(d["High"].iloc[hi_i])
    marks = [(hi_v, f"前高 {hi_v:,.1f}", "#e64980", ":")]
    vol_marks = []
    lv = vol_levels(df, bars)
    for key, nm in (("press", "壓力"), ("supp", "支撐")):
        it = lv[key]
        if not it:
            continue
        col = "#ff8c00" if it["top"] else ("#e04a4a" if key == "press" else "#2e9e4f")      # 整段最大量固定橘色
        marks.append((it["level"], f"{nm} {it['level']:,.1f}（{it['day']:%m/%d}）", col, "--"))
        vol_marks.append((_pos(idx, it["day"]), "第1大量" if it["top"] else nm, col))
    top_v, bot_v = float(d["High"].max()), float(d["Low"].min())
    allv = [m[0] for m in marks] + [top_v, bot_v]
    lo_y, hi_y = min(allv), max(allv)
    rng_v = max(hi_y - lo_y, 1e-9)
    if ma60.notna().any():                                      # 季線盡量留在圖裡（最多把範圍放大 35%，K 線才不會被壓扁）
        lo_y = min(lo_y, max(float(ma60.min()), lo_y - rng_v * 0.35))
        hi_y = max(hi_y, min(float(ma60.max()), hi_y + rng_v * 0.35))
        rng_v = max(hi_y - lo_y, 1e-9)
    ax.set_ylim(lo_y - rng_v * 0.06, hi_y + rng_v * 0.08)
    gap = rng_v * 0.075                                         # 標籤放在圖裡面右上角、線的上方，圖才能撐滿
    bb = dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.8)
    ys = {}
    for k, (val, _, _, _) in sorted(enumerate(marks), key=lambda t: -t[1][0]):
        ys[k] = val if not ys else min(val, min(ys.values()) - gap)
    for k, (val, txt, col, ls) in enumerate(marks):
        ax.axhline(val, color=col, linestyle=ls, linewidth=0.9, alpha=0.75)
        ax.text(n - 1.2, ys[k] + rng_v * 0.012, txt, fontsize=9, color=col, va="bottom", ha="right", bbox=bb, zorder=7)
    ax.set_xlim(-1, n)
    _price_line(ax, d, last, 2, avoid=[m[0] for m in marks])
    ax.set_ylabel("價格")
    ax.legend(loc="upper left", fontsize=8, frameon=False, ncol=4)
    ax.grid(alpha=0.15)

    # --- 成交量（張）---
    v = d["Volume"] / 1000.0
    cols = [UP if cl >= o else DOWN for o, cl in zip(d["Open"], d["Close"])]
    axv.bar(x, v.values, color=cols, width=0.65)
    axv.plot(x, vma20.values, color="#e67e22", linewidth=1.1, label="20日均量")
    seen = []
    vmax = float(v.max())
    for p_, rank, col in sorted([m for m in vol_marks if m[0] is not None], key=lambda m: m[0]):
        axv.add_patch(plt.Rectangle((p_ - 0.5, 0), 1.0, float(v.iloc[p_]) * 1.03, fill=False,
                                    edgecolor=col, linewidth=1.1, zorder=6, clip_on=False))
        lvl = sum(1 for q in seen if abs(q - p_) <= 8)                 # 兩個標記靠太近，後面的往上疊一層，不會壓在一起
        seen.append(p_)
        axv.text(p_ + (0.6 if p_ < 6 else 0), float(v.iloc[p_]) * 1.05 + vmax * 0.14 * lvl, rank, fontsize=8, color=col,
                 ha="left" if p_ < 6 else "center", va="bottom")
    axv.set_ylim(0, vmax * (1.5 if len(seen) > 1 else 1.3))
    axv.set_ylabel("成交量（張）")
    axv.legend(loc="upper right", fontsize=8, frameon=False)
    axv.grid(alpha=0.15)

    step = max(1, n // 9)
    ticks = list(range(0, n, step))
    axv.set_xticks(ticks)
    axv.set_xticklabels([idx[t].strftime("%m/%d") for t in ticks], fontsize=8)
    axv.set_xlim(-1, n)
    fig.suptitle(f"{title}　{idx[0]:%m/%d}–{idx[-1]:%m/%d}".strip(), fontsize=11, x=0.02, ha="left")
    _price_header(fig, d["Close"])
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
