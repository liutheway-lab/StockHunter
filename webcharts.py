"""webcharts.py — 網頁 app 用的互動 K 線圖（plotly）
均線先用完整歷史算，再截取最近 N 根來畫。K 線顏色：紅漲綠跌（跟看盤軟體一致）。
"""
import pandas as pd

VERSION = "圖表 v5"
UP, DOWN = "#e04a4a", "#2e9e4f"
MA_STYLE = (("MA5", "#f1c40f", 1.2), ("MA10", "#1abc9c", 1.2), ("MA20", "#9b59b6", 1.4), ("MA60", "#3498db", 1.6))


def prepare(df, is_market=False, bars=65):
    """回傳 (要畫的 DataFrame, 成交量欄位名稱)。個股量用「張」，大盤用證交所成交金額（億元）"""
    d = df.copy() if is_market else df[df["Volume"] > 0].copy()
    if is_market:
        d["Plot_Vol"] = d["Volume"] / 1e8
        label = "成交金額（證交所，億）"
    else:
        d["Plot_Vol"] = d["Volume"] / 1000.0
        label = "成交量（張）"
    for n in (5, 10, 20, 60):
        d[f"MA{n}"] = d["Close"].rolling(n).mean()
    d["VMA20"] = d["Plot_Vol"].rolling(20).mean()
    return d.iloc[-bars:], label


def plot_k_vol(df, is_market=False, bars=65, stab=None):
    """stab：大盤空頭時傳入 stabilization 結果，會畫出前低虛線與近 3 日色塊"""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    p, vol_label = prepare(df, is_market, bars)
    dates = [d.strftime("%m/%d") for d in p.index]
    v_colors = [UP if c >= o else DOWN for c, o in zip(p["Close"], p["Open"])]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.74, 0.26])
    fig.add_trace(go.Candlestick(x=dates, open=p["Open"], high=p["High"], low=p["Low"], close=p["Close"],
                                 name="日K", increasing_line_color=UP, decreasing_line_color=DOWN,
                                 showlegend=False), row=1, col=1)
    for col, color, width in MA_STYLE:
        fig.add_trace(go.Scatter(x=dates, y=p[col], line=dict(color=color, width=width), name=col),
                      row=1, col=1)
    fig.add_trace(go.Bar(x=dates, y=p["Plot_Vol"], marker_color=v_colors, name=vol_label, showlegend=False),
                  row=2, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["VMA20"], line=dict(color="#e67e22", width=1.1), name="20日均量"),
                  row=2, col=1)

    if stab and stab.get("prior_low") is not None:
        s0, s1 = f"{stab['recent_start']:%m/%d}", f"{stab['recent_end']:%m/%d}"
        if s0 in dates and s1 in dates:
            # 近 3 日：淡黃色底，只蓋住上面的 K 線圖（不蓋成交金額）
            fig.add_shape(type="rect", xref="x", yref="y domain", x0=dates.index(s0) - 0.5,
                          x1=dates.index(s1) + 0.5, y0=0, y1=1, fillcolor="rgba(255,221,0,0.18)",
                          line_width=0, layer="above")

        def mark(day, val, text, color, ax, ay):
            if day in dates:
                fig.add_trace(go.Scatter(x=[day], y=[val], mode="markers", showlegend=False, hoverinfo="skip",
                                         marker=dict(symbol="diamond", size=9, color=color,
                                                     line=dict(color="#ffffff", width=1))), row=1, col=1)
                # 用箭頭拉出來寫數字；兩個標籤的位置錯開，才不會疊在一起
                fig.add_annotation(x=day, y=val, xref="x", yref="y", text=text, showarrow=True,
                                   arrowhead=2, arrowsize=1, arrowwidth=1.5, arrowcolor=color,
                                   ax=ax, ay=ay, font=dict(color=color, size=12),
                                   bgcolor="rgba(17,17,17,0.85)", borderpad=2)

        mark(f"{stab['prior_low_date']:%m/%d}", stab["prior_low"], f"前低 {stab['prior_low']:,.0f}",
             "#ff9f1a", -70, 40)
        if stab.get("recent_low") is not None and stab.get("recent_low_date") is not None:
            mark(f"{stab['recent_low_date']:%m/%d}", stab["recent_low"],
                 f"近3日低 {stab['recent_low']:,.0f}", "#00e5ff", -90, 95)

    fig.update_layout(
        xaxis_rangeslider_visible=False, margin=dict(l=5, r=5, t=35, b=5), height=440,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=10)),
        template="plotly_dark", dragmode=False, hovermode=False,
    )
    fig.update_xaxes(type="category", tickangle=-45, showgrid=False, nticks=10, range=[-0.5, len(dates) - 0.5])
    fig.update_yaxes(side="right", gridcolor="rgba(255,255,255,0.08)")
    fig.update_yaxes(title_text=vol_label, title_font_size=10, row=2, col=1)
    return fig
