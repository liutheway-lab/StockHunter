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

    fig, axes = plt.subplots(2 if has_vol else 1, 1, figsize=(8, 6.2 if has_vol else 4.2), dpi=150,
                             sharex=True, gridspec_kw={"height_ratios": [3, 1.3]} if has_vol else None)
    ax = axes[0] if has_vol else axes
    axv = axes[1] if has_vol else None

    # --- K 線 ---
    for xi, (o, h, l, cl) in enumerate(zip(d["Open"], d["High"], d["Low"], d["Close"])):
        col = UP if cl >= o else DOWN
        ax.vlines(xi, l, h, color=col, linewidth=1)
        ax.bar(xi, max(abs(cl - o), (h - l) * 0.02), bottom=min(o, cl), width=0.65, color=col)
    ax.plot(x, ma5.values, color="#e6a700", linewidth=0.9, label="MA5")
    ax.plot(x, ma10.values, color="#16a085", linewidth=0.9, label="MA10")
    ax.plot(x, ma20.values, color=MA20_C, linewidth=1.2, label="MA20")
    ax.plot(x, ma60.values, color=MA60_C, linewidth=1.2, label="MA60")

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
        ax.axhline(pl, color="#555", linestyle="--", linewidth=1)
        p = _pos(idx, pld)
        if p is not None:
            ax.scatter([p], [pl], marker="^", color="#333", zorder=5, s=30)
        ax.text(0.5, pl + rng_v * 0.02, f"前低 {pl:,.0f}（{pld:%m/%d}）", fontsize=9, color="#333")
    if rl is not None and _pos(idx, rld) is not None:
        rp = _pos(idx, rld)
        ax.scatter([rp], [rl], color="#d35400", zorder=5, s=28)
        ax.annotate(f"近3日最低 {rl:,.0f}（{rld:%m/%d}）", xy=(rp, rl),
                    xytext=(max(rp - 15, 0), lowest - rng_v * 0.13), fontsize=9, color="#d35400",
                    arrowprops=dict(arrowstyle="->", color="#d35400"))
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
            axv.axhline(dv / 1e8, color="#555", linestyle="--", linewidth=1)
            if dp is not None:
                axv.bar([dp], [dv / 1e8], color="none", edgecolor="#d35400", linewidth=1.8, width=0.65)
                axv.annotate(f"爆量下跌日 {dv / 1e8:,.0f}億（{dd:%m/%d}）", xy=(dp, dv / 1e8),
                             xytext=(max(dp - 12, 0), dv / 1e8 * 1.02), fontsize=9, color="#d35400")
        rv = s.get("recent_vol")
        if rv and r1 is not None:
            axv.annotate(f"近3日最大 {rv / 1e8:,.0f}億", xy=(r1, rv / 1e8), xytext=(max(r1 - 9, 0), rv / 1e8 * 1.25),
                         fontsize=9, color="#333", arrowprops=dict(arrowstyle="->", color="#555"))
        axv.set_ylabel("成交金額(億)")
        axv.grid(alpha=0.15)
        axv.set_ylim(0, max(float(v.max()), (dv or 0) / 1e8) * 1.25)

    # --- X 軸日期 ---
    last_ax = axv if has_vol else ax
    step = max(1, len(d) // 9)
    ticks = list(range(0, len(d), step))
    last_ax.set_xticks(ticks)
    last_ax.set_xticklabels([idx[t].strftime("%m/%d") for t in ticks], fontsize=8)
    last_ax.set_xlim(-1, len(d))

    if stab_mode:
        verdict = "通過" if mk["stab"].get("ok") else ("無法確認" if mk["stab"].get("vol_missing") else "未通過")
        title = f"加權指數 {idx[0]:%m/%d}–{idx[-1]:%m/%d}　第{mk['layer']}層・{mk['layer_name']}　止跌確認：{verdict}（黃色區 = 近 3 日）"
    else:
        title = f"加權指數 {idx[0]:%m/%d}–{idx[-1]:%m/%d}　第{mk['layer']}層・{mk['layer_name']}"
    fig.suptitle(title, fontsize=11, x=0.02, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.96))
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()
