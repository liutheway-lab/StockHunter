import pandas as pd
import requests
import io

def to_tw_tick(price: float) -> float:
    """台股法定升降單位 (Tick) 換算"""
    if price <= 0:
        return 0.01
    if price < 10.0:
        tick = 0.01
    elif price < 50.0:
        tick = 0.05
    elif price < 100.0:
        tick = 0.10
    elif price < 500.0:
        tick = 0.50
    elif price < 1000.0:
        tick = 1.00
    else:
        tick = 5.00
    return round(round(price / tick) * tick, 2)

def check_tdcc_increasing(stock_id: str):
    """查詢集保戶股權分散表：400張大戶近3週是否連續增持"""
    clean_id = stock_id.replace(".TWO", "").replace(".TW", "")
    url = f"https://norway.twsthr.info/StockHolders.aspx?stock={clean_id}"
    try:
        r = requests.get(url, timeout=5, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            tables = pd.read_html(io.StringIO(r.text))
            for t in tables:
                if len(t.columns) >= 7 and len(t) >= 4:
                    col_idx = 7 if len(t.columns) > 7 else -2
                    w0 = float(str(t.iloc[1, col_idx]).replace("%", ""))
                    w1 = float(str(t.iloc[2, col_idx]).replace("%", ""))
                    w2 = float(str(t.iloc[3, col_idx]).replace("%", ""))
                    if w0 > w1 > w2:
                        diff = round(w0 - w2, 2)
                        return True, w0, diff
    except Exception:
        pass
    return False, None, None
