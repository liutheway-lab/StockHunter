"""盤中放量急殺提醒（試跑中）：只私訊管理者，不發群組、不發給使用者。

判斷條件（三個都要成立）：
1. 時段：只在 09:05～09:15、10:00～10:30、11:00～11:30、13:00～13:25 檢查
2. 放量：最近 5 分鐘的成交量 ≥ 前 30 分鐘每 5 分鐘平均的 倍數（開盤 4 倍，其他 3 倍）
3. 急殺＋位置：最近 5 分鐘跌幅 ≥ 1.5%，而且現價貼近出場價或支撐（上下 1% 以內，或已經跌破）

每檔每天只提醒一次。沒有證交所累計量（只有富果或 Yahoo 來源）的報價不算。
"""
from collections import deque
from datetime import timedelta

WINDOWS = [                      # (開始, 結束不含, 倍數)
    ((9, 5), (9, 15), 4.0),      # 開盤震盪：門檻高一點，避免誤報
    ((10, 0), (10, 30), 3.0),
    ((11, 0), (11, 30), 3.0),
    ((13, 0), (13, 25), 3.0),    # 13:25 後不再掃（收盤前來不及下單）
]
MOVE_PCT = 1.5                   # 5 分鐘內跌幅門檻（%）
NEAR_PCT = 1.0                   # 離出場價、支撐在這個範圍內（%）
BLOCK = timedelta(minutes=5)
BASE_BLOCKS = 6                  # 基準：前 30 分鐘，每 5 分鐘一段
MAX_HIST = 90                    # 每檔最多留 90 筆（每分鐘一筆，約 1.5 小時）
TOL = timedelta(seconds=90)      # 取樣時間容許誤差（抓價偶爾會慢）


def threshold(hm):
    """這個時間要用幾倍量；不在掃描時段回傳 None"""
    for a, b, mult in WINDOWS:
        if a <= hm < b:
            return mult
    return None


class Tracker:
    def __init__(self):
        self.hist = {}           # {代號: deque[(時間, 累計成交股數, 成交價)]}
        self.day = None

    def update(self, code, t, cum, price):
        if self.day != t.date():                       # 換天：全部清掉
            self.hist.clear()
            self.day = t.date()
        d = self.hist.setdefault(code, deque(maxlen=MAX_HIST))
        if d and d[-1][0] >= t:
            return
        d.append((t, cum, price))

    def _at(self, code, t):
        """t 之前最近的一筆（容許 TOL 的誤差）；找不到就 None"""
        best = None
        for ts, cum, p in self.hist.get(code, ()):
            if ts <= t:
                best = (ts, cum, p)
            else:
                break
        if best and t - best[0] <= TOL:
            return best
        return None

    def measure(self, code, now):
        """回傳 {cur 這 5 分鐘量, base 前 30 分鐘每 5 分鐘平均量, mult 倍數, chg 5 分鐘漲跌%, price}；資料不夠回傳 None"""
        edges = [self._at(code, now - BLOCK * k) for k in range(BASE_BLOCKS + 2)]
        end, start = edges[0], edges[1]
        if not end or not start:
            return None
        blocks = []
        for earlier, later in zip(edges[2:], edges[1:]):   # 每一段：較早的一筆到較晚的一筆
            if earlier and later:
                blocks.append(later[1] - earlier[1])
        if len(blocks) < 2:                                # 基準至少要兩段
            return None
        base = sum(blocks) / len(blocks)
        if base <= 0:
            return None
        cur = end[1] - start[1]
        chg = (end[2] / start[2] - 1) * 100 if start[2] else 0.0
        return {"cur": cur, "base": base, "mult": cur / base, "chg": chg, "price": end[2]}


def judge(m, hm, stop=None, sup=None):
    """急殺且貼近出場價或支撐 → 回傳 [(名稱, 價位)]；不成立回傳 None"""
    mult = threshold(hm)
    if mult is None or m is None or m["mult"] < mult or m["chg"] > -MOVE_PCT:
        return None
    hit = [(n, v) for n, v in (("出場價", stop), ("支撐", sup))
           if v and m["price"] <= v * (1 + NEAR_PCT / 100)]
    return hit or None


def message(code, name, m, hit, mult_at):
    lines = [f"📉 放量急殺（試跑）　{code} {name}",
             f"5 分鐘量 {m['cur'] / 1000:,.0f} 張，是前 30 分鐘平均的 {m['mult']:.1f} 倍（門檻 {mult_at:g} 倍）",
             f"5 分鐘跌 {m['chg']:.1f}%，現價 {m['price']:g}"]
    for n, v in hit:
        lines.append(f"貼近{n} {v:g}（距 {(m['price'] / v - 1) * 100:+.1f}%）")
    lines.append("請自行判斷是否停損或停利被殺出")
    return "\n".join(lines)
