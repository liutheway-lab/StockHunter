import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

def plot_k_vol(df: pd.DataFrame, is_market: bool = False):
    """
    專業 K 線與成交量雙子圖
    - 嚴格濾除 Volume <= 0 之無效資料
    - 大盤：成交金額以數千億元級別精準換算
    - 個股：以台股常用「張數」換算
    - 手機防削頂：高度 400px，保留頂部邊距
    """
    p = df[df["Volume"] > 0].iloc[-65:].copy()
    
    if is_market:
        # 大盤成交金額估算 (億元) 對齊券商數值
        p["Plot_Vol"] = (p["Volume"] * p["Close"]) / 10000000.0
        if p["Plot_Vol"].mean() < 500:
            p["Plot_Vol"] = p["Volume"] / 10000.0
        vol_label = "成交金額 (億元)"
    else:
        # 個股成交量 (張)
        p["Plot_Vol"] = p["Volume"] / 1000.0
        vol_label = "成交量 (張)"

    # 技術均線
    p["5MA"] = p["Close"].rolling(5).mean()
    p["20MA"] = p["Close"].rolling(20).mean()
    p["60MA"] = p["Close"].rolling(60).mean()
    p["20VMA"] = p["Plot_Vol"].rolling(20).mean()
    
    dates = [d.strftime('%m/%d') for d in p.index]
    # 紅漲綠跌配色
    v_colors = ['#e04a4a' if c >= o else '#2e9e4f' for c, o in zip(p["Close"], p["Open"])]

    # 建立 75% 主圖 + 25% 量圖
    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.75, 0.25]
    )

    # 主 K 線
    fig.add_trace(go.Candlestick(
        x=dates,
        open=p["Open"],
        high=p["High"],
        low=p["Low"],
        close=p["Close"],
        name="日K",
        increasing_line_color='#e04a4a',
        increasing_fillcolor='#e04a4a',
        decreasing_line_color='#2e9e4f',
        decreasing_fillcolor='#2e9e4f'
    ), row=1, col=1)

    # 均線
    fig.add_trace(go.Scatter(x=dates, y=p["5MA"], line=dict(color='#f1c40f', width=1.2), name="5MA"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["20MA"], line=dict(color='#9b59b6', width=1.4), name="20MA"), row=1, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["60MA"], line=dict(color='#3498db', width=1.6), name="60MA"), row=1, col=1)

    # 副圖成交量與 20VMA
    fig.add_trace(go.Bar(x=dates, y=p["Plot_Vol"], marker_color=v_colors, name=vol_label), row=2, col=1)
    fig.add_trace(go.Scatter(x=dates, y=p["20VMA"], line=dict(color='#e67e22', width=1.1), name="20均量"), row=2, col=1)

    # 手機防擠壓與防削頂版面
    fig.update_layout(
        xaxis_rangeslider_visible=False,
        margin=dict(l=5, r=5, t=25, b=5),
        height=400,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(size=10)),
        template="plotly_dark",
        dragmode=False,
        hovermode="x unified"
    )
    fig.update_xaxes(type="category", tickangle=-45, showgrid=False)
    fig.update_yaxes(side="right", gridcolor='rgba(255,255,255,0.08)')
    return fig
