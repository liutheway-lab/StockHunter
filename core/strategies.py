# core/strategies.py
from dataclasses import dataclass
from typing import Dict, List, Optional
import pandas as pd
from .indicators import to_tw_tick


@dataclass
class PlanResult:
  ticker: str
  strategy_name: str
  current_price: float
  is_qualified: bool
  reason: str
  weights: List[float]  # [w1, w2, w3]
  entry_p: float
  add_p: float
  ext_p: float
  stop_loss: float
  breakeven: float
  target_p: float


def calculate_pyramid(
    entry: float,
    add_price: float,
    ext_price: float,
    stop_p: float,
    weights: List[float],
    target_p: float,
) -> Dict:
  """核心動態分批資金引擎 (歸一化權重)"""
  tot = sum(weights)
  w1, w2, w3 = [w / tot for w in weights]

  avg_1 = entry
  avg_2 = to_tw_tick((w1 * entry + w2 * add_price) / (w1 + w2))
  avg_3 = to_tw_tick(
      (w1 * entry + w2 * add_price + w3 * ext_price) / (w1 + w2 + w3)
  )

  # 第二筆加碼防守：跌破加碼點 5%
  stop_2 = to_tw_tick(add_price * 0.95)
  loss_2 = (stop_2 - add_price) * w2
  # 第一筆保本底線
  breakeven = to_tw_tick((entry * w1 - loss_2) / w1)

  return {
      "avg_1": avg_1,
      "avg_2": avg_2,
      "avg_3": avg_3,
      "stop_1": stop_p,
      "stop_2": stop_2,
      "breakeven": breakeven,
      "target": target_p,
  }


def scan_single_stock(
    ticker: str,
    df: pd.DataFrame,
    twii_df: pd.DataFrame,
    weights: List[float] = [0.3, 0.4, 0.3],
) -> List[PlanResult]:
  """單一股票進行四大策略全面診斷"""
  results = []
  if len(df) < 60:
    return results

  close = float(df["Close"].iloc[-1])
  open_p = float(df["Open"].iloc[-1])
  high = float(df["High"].iloc[-1])
  vol = float(df["Volume"].iloc[-1])
  mav20 = float(df["Volume"].rolling(20).mean().iloc[-1])
  ema20 = float(df["Close"].ewm(span=20, adjust=False).mean().iloc[-1])
  ema60 = float(df["Close"].ewm(span=60, adjust=False).mean().iloc[-1])
  ma5 = float(df["Close"].rolling(5).mean().iloc[-1])

  # ---------------------------------------------------------
  # 策略 1：創高量縮拉回 (右側紅 K 止跌)
  # ---------------------------------------------------------
  s1_win = df.iloc[-30:-2]
  if not s1_win.empty:
    B1 = float(s1_win["High"].max())
    vol_B1 = float(df.loc[s1_win["High"].idxmax(), "Volume"])
    pb = (B1 - close) / B1
    c_pb = 0.08 <= pb <= 0.16
    c_vol = vol <= 0.65 * vol_B1 and vol <= mav20 * 1.1
    c_rev = (close > open_p) and (close >= ma5)
    c_ema = ema20 > ema60 and close >= ema60

    if c_pb and c_vol and c_rev and c_ema:
      p = calculate_pyramid(
          entry=close,
          add_price=B1,
          ext_price=to_tw_tick(B1 * 1.08),
          stop_p=to_tw_tick(close * 0.90),
          weights=weights,
          target_p=to_tw_tick(close * 1.25),
      )
      results.append(
          PlanResult(
              ticker,
              "創高量縮回測",
              close,
              True,
              "回測前高量縮且收紅站上5MA",
              weights,
              close,
              B1,
              to_tw_tick(B1 * 1.08),
              p["stop_1"],
              p["breakeven"],
              p["target"],
          )
      )

  # ---------------------------------------------------------
  # 策略 2 / A：低檔起漲爆量突破 (盤整後噴出)
  # ---------------------------------------------------------
  low_60 = float(df["Low"].iloc[-60:].min())
  if low_60 > 0 and (close - low_60) / low_60 <= 0.18:
    neckline = float(df.iloc[-31:-1]["High"].max())
    if close > neckline and (vol / mav20 >= 2.0) and close >= ema60:
      stop_p = to_tw_tick(min(neckline * 0.98, close * 0.93))
      p = calculate_pyramid(
          entry=close,
          add_price=to_tw_tick(close * 1.05),
          ext_price=to_tw_tick(close * 1.10),
          stop_p=stop_p,
          weights=weights,
          target_p=to_tw_tick(close * 1.20),
      )
      results.append(
          PlanResult(
              ticker,
              "低檔爆量起漲 (策略A)",
              close,
              True,
              "帶量2倍突破30日頸線",
              weights,
              close,
              to_tw_tick(close * 1.05),
              to_tw_tick(close * 1.10),
              stop_p,
              p["breakeven"],
              p["target"],
          )
      )

  # ---------------------------------------------------------
  # 策略 B：量縮抗跌 (大盤重挫時逆勢抗跌)
  # ---------------------------------------------------------
  if vol <= 0.50 * mav20:  # 量縮 <= 50% 20MAV
    # 比對近 3 日跌幅
    stock_drop = (close - float(df["Close"].iloc[-4])) / float(
        df["Close"].iloc[-4]
    )
    twii_drop = (
        float(twii_df["Close"].iloc[-1]) - float(twii_df["Close"].iloc[-4])
    ) / float(twii_df["Close"].iloc[-4])
    if twii_drop < -0.02:  # 大盤近 3 日顯著回檔
      if stock_drop <= 0.50 * twii_drop:  # 跌幅不到大盤一半
        recent_low = float(df["Low"].iloc[-10:].min())
        stop_p = to_tw_tick(recent_low * 0.99)
        risk = close - stop_p
        target = to_tw_tick(close + risk * 2.5)
        p = calculate_pyramid(
            entry=close,
            add_price=to_tw_tick(close * 1.05),
            ext_price=to_tw_tick(close * 1.10),
            stop_p=stop_p,
            weights=weights,
            target_p=target,
        )
        results.append(
            PlanResult(
                ticker,
                "量縮抗跌強勢股 (策略B)",
                close,
                True,
                "極致量縮且跌幅遠小於大盤",
                weights,
                close,
                to_tw_tick(close * 1.05),
                to_tw_tick(close * 1.10),
                stop_p,
                p["breakeven"],
                target,
            )
        )

  # ---------------------------------------------------------
  # 策略 C：噴出後整理再攻
  # ---------------------------------------------------------
  # 檢查近 15 天內是否有噴出日 (單日 >= 4% 或 3 日 >= 15%，且量 >= 1.5 倍)
  for lookback in range(3, 15):
    sub = df.iloc[-lookback:]
    prior_c = float(df["Close"].iloc[-lookback - 1])
    burst_h = float(sub["High"].max())
    burst_gain = (burst_h - prior_c) / prior_c

    # 滿足噴出標準
    if burst_gain >= 0.15 or any(
        (df["Close"].iloc[-i] - df["Close"].iloc[-i - 1])
        / df["Close"].iloc[-i - 1]
        >= 0.04
        for i in range(1, lookback)
    ):
      half_level = prior_c + (burst_h - prior_c) * 0.5
      # 整理回檔不破 1/2
      if float(sub["Low"].min()) >= half_level:
        # 距離前高 4% 以內或已突破
        if close >= burst_h * 0.96:
          # 停損取 噴出 1/2 與 買價 -5% 孰低
          stop_candidate = min(half_level, close * 0.95)
          target = to_tw_tick(burst_h + (burst_h - prior_c))  # 等幅測量
          p = calculate_pyramid(
              entry=close,
              add_price=to_tw_tick(burst_h),
              ext_price=to_tw_tick(burst_h * 1.05),
              stop_p=to_tw_tick(stop_candidate),
              weights=weights,
              target_p=target,
          )
          results.append(
              PlanResult(
                  ticker,
                  "噴出後整理再攻 (策略C)",
                  close,
                  True,
                  "噴出整理不破1/2，逼近或突破前高",
                  weights,
                  close,
                  to_tw_tick(burst_h),
                  to_tw_tick(burst_h * 1.05),
                  p["stop_1"],
                  p["breakeven"],
                  target,
              )
          )
          break

  return results
