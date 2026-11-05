"""dry_groups.py — 量縮抗跌的族群排名（給夜間掃描用）

流程：
1. rank_groups：大盤收盤在 20 日均線下的日子（最近 20 個交易日內），算每檔股票這些天的累計跌幅；
   每個概念族群取成分股的「中位數」，排前 5 名（跌得最少的族群）。
2. is_resilient：候選股（已經是盤整＋量縮）所屬的任一概念，在前 5 名內，就是抗跌。
族群名單來源：Yahoo 概念分類頁（每頁抓得到的成分股，約 34 檔）。
排名結果存到 group_rank.json；抓失敗就沿用上一次的排名，並回傳 stale=True，讓呼叫的人提醒管理者。
"""
import json
import re
import statistics
import time
import urllib.parse
from datetime import date

import pandas as pd
import requests

RANK_FILE = "group_rank.json"
TOP_N = 5
BAD_WINDOW = 20          # 看最近 20 個交易日內大盤不好的日子
MIN_BAD_DAYS = 3         # 不好的日子少於 3 天，不排名（樣本太少）
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
      "Accept-Language": "zh-TW,zh;q=0.9"}
CODE_RE = re.compile(r"(\d{4,6})\.(?:TWO|TW)\b")
CONCEPTS = [
    "蘋果200大供應商", "MOSFET", "AI理財機器人", "Sharp", "Google Pixel", "米其林摘星", "夏日飲料", "無人商店",
    "跨國連鎖餐飲", "AirPods", "VR虛擬實境", "汽車電子", "台幣升值", "中國台商汽車組件", "智慧音箱", "Apple watch",
    "AI人工智慧", "空污", "Apple Pay", "紡織機能運動", "再生循環", "銀髮商機", "Tesla", "雙十一", "3D感測", "日圓貶值",
    "互聯網+", "三星", "第三方支付", "iPhone", "一帶一路", "車聯網", "3D列印", "補教/民辦教育", "新掛牌", "Apple iTV",
    "小米", "Nike", "雲伺服器", "Amazon Go應用技術", "iPad", "電競產業", "空污防治", "行動支付", "台資在中國高獲利",
    "折疊手機", "人民幣升值", "歐元貶值", "台積電", "無人機", "聯發科", "WiFi 6", "Mini LED", "FinTech", "網紅經濟",
    "水資源", "日圓升值", "矽智財(IP)", "OPPO", "都更", "ADAS", "國防自主", "Intel", "眼球商機", "風力發電/離岸風電",
    "醫療呼吸器", "宅經濟", "功率半導體", "大數據2.0", "HomePod", "無線充電", "任天堂Switch", "智慧家庭", "防疫",
    "博弈觀光", "5G", "Toyota", "穿戴裝置", "丹麥沃旭", "華為", "半導體設備", "體育/運動產業", "智慧汽車", "電子商務",
    "海外掛牌", "低軌衛星", "嬰幼兒相關", "比特幣挖礦", "機器人/智慧機械", "工業4.0", "電動車/油電車", "通膨",
    "航空/航太", "環保綠能材料", "智慧城市", "植物工廠", "雲端產業", "PlayStation 5", "物聯網", "智慧醫療", "人臉辨識",
]


def bad_market_days(mkt_close: pd.Series):
    """最近 20 個交易日內，大盤收盤在 20 日均線之下的日子"""
    ma20 = mkt_close.rolling(20).mean()
    recent = mkt_close.index[-BAD_WINDOW:]
    return [d for d in recent if pd.notna(ma20.get(d)) and mkt_close[d] < ma20[d]]


def cum_return(close: pd.Series, days):
    """只看指定的那些天，個股的累計漲跌幅（複利）"""
    r = close.pct_change().reindex(days).dropna()
    if r.empty:
        return None
    return float((1 + r).prod() - 1)


def fetch_members(name, timeout=30):
    """一個概念分類頁的成分股代號（頁面只給約 34 檔）。失敗回傳 None"""
    try:
        r = requests.get("https://tw.stock.yahoo.com/class-quote",
                         params={"category": name, "categoryLabel": "概念股"}, headers=UA, timeout=timeout)
        if r.status_code != 200:
            return None
        return list(dict.fromkeys(CODE_RE.findall(r.text)))
    except Exception:
        return None


def fetch_all_members(names=CONCEPTS):
    """回傳 {族群名: [代號...]}，抓不到的族群不放進來；全部都抓不到回傳 None"""
    out = {}
    for n in names:
        m = fetch_members(n)
        if m:
            out[n] = m
        time.sleep(0.4)
    return out or None


def rank_groups(prices, mkt_close, members):
    """prices：{代號: 收盤 Series}；members：{族群: [代號]}。回傳排名 dict，或 None（樣本不夠）"""
    days = bad_market_days(mkt_close)
    if len(days) < MIN_BAD_DAYS:
        return None
    scores = {}
    for g, codes in members.items():
        vals = [v for v in (cum_return(prices[c], days) for c in codes if c in prices) if v is not None]
        if vals:
            scores[g] = statistics.median(vals)
    top = sorted(scores, key=scores.get, reverse=True)[:TOP_N]
    return {"date": date.today().isoformat(), "bad_days": len(days), "top": top,
            "scores": {g: round(s * 100, 2) for g, s in scores.items()}}


def save_rank(rank, path=RANK_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rank, f, ensure_ascii=False, indent=2)


def load_rank(path=RANK_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def is_resilient(stock_concepts, rank):
    """候選股的任一概念在前 5 名內 → 抗跌。沒有概念資料或沒有排名 → False"""
    if not stock_concepts or not rank:
        return False
    return any(c in rank.get("top", []) for c in stock_concepts)
