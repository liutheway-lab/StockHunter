# core/market_regime.py
from dataclasses import dataclass
from typing import Optional, Tuple
import pandas as pd
import yfinance as yf


@dataclass
class MarketStatus:
  level: int
  name: str
  allowed_strategies: list
  is_rebounded: bool
  note: str


def get_market_regime() -> MarketStatus:
  """分析加權指數 (^TWII) 的多空位階與下跌止跌狀態"""
  df = yf.download(
      "^TWII",
      period="6mo",
      interval="1d",
      progress=False,
      auto_adjust=True,
  )
  if isinstance(df.columns, pd.MultiIndex):
    df = df.xs("^TWII", axis=1, level=1)
  df = df.dropna(subset=["Close"])

  close = float(df["Close"].iloc[-1])
  ma5 = float(df["Close"].rolling(5).mean().iloc[-1])
  ma10 = float(df["Close"].rolling(10).mean().iloc[-1])
  ma20 = float(df["Close"].rolling(20).mean().iloc[-1])
  ma60 = float(df["Close"].rolling(60).mean().iloc[-1])

  # 1. 判斷大盤 5 層位階
  if close > ma5 > ma10 > ma20 > ma60:
    level = 1
    name = "強烈多頭 (均線多頭排列)"
  elif close > ma20 > ma60 and (close <= ma5 or close <= ma10):
    level = 2
    name = "中度多頭 (月季線之上震盪)"
  elif (close > ma20 and close <= ma60) or (close <= ma20 and close > ma60):
    level = 3
    name = "震盪整理 / 轉折區 (月季線糾結)"
  elif close < ma20 < ma60 and close > ma5:
    level = 4
    name = "中度空頭 (反彈遭遇均線反壓)"
  else:
    level = 5
    name = "強烈空頭 (均線空頭排列)"

  # 2. 下跌波段止跌檢驗 (近 3 日不破低 + 量能大於近 20 日爆量陰線量)
  is_rebounded = False
  recent_3_low = float(df["Low"].iloc[-3:].min())
  prev_20_low = float(df["Low"].iloc[-23:-3].min())
  not_breaking_low = recent_3_low >= prev_20_low

  # 尋找近 20 日下跌日中量最大的那天
  down_days = df.iloc[-20:][df.iloc[-20:]["Close"] < df.iloc[-20:]["Open"]]
  max_down_vol = (
      float(down_days["Volume"].max()) if not down_days.empty else 0.0
  )
  recent_3_max_vol = float(df["Volume"].iloc[-3:].max())
  vol_recovered = recent_3_max_vol >= max_down_vol

  if not_breaking_low and vol_recovered:
    is_rebounded = True

  # 3. 策略准入決策
  allowed = []
  if level in [1, 2]:
    allowed = ["pullback", "breakout_extend"]  # 創高拉回、噴出整理再攻
    note = "盤勢健康，允許攻擊型策略。"
  elif level == 3:
    allowed = [
        "pullback",
        "bottom_platform",
        "breakout_extend",
        "volume_dry_resilient",
    ]
    note = "盤整轉折，允許進場但標示高風險。"
  else:  # level 4, 5
    if is_rebounded:
      allowed = [
          "bottom_platform",
          "volume_dry_resilient",
      ]  # 盤整後噴出、量縮抗跌
      note = "大盤空頭，但已滿足「3日不破低+爆量止跌」，允許防守型策略。"
    else:
      allowed = []
      note = "大盤空頭且未見有效止跌量能，全面禁止進場，嚴格空手觀望。"

  return MarketStatus(level, name, allowed, is_rebounded, note)
