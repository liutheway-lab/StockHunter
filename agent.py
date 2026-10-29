import io
import os
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# =====================================================
# 1. 策略核心參數設定
# =====================================================
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# 流動性與活躍度濾網（已移除價格上下限，僅把關量能）
MIN_VOL_SHARES = 1000     # 5 日均量至少 1000 張 (張 = 股數 / 1000)
MIN_AVG_TURNOVER = 5e7    # 20 日均成交金額 >= 5000 萬台幣

# 籌碼面門檻（神秘金字塔 / 集保股權分散表）
MIN_LARGE_RATIO = 45.0    # 400 張以上大戶持股比例至少 >= 45%

# 三筆加碼部位權重配置 (30% + 40% + 30% = 100%)
TRANCHE_1 = 0.30          # 第一筆：左側回檔量縮接刀 (30%)
TRANCHE_2 = 0.40          # 第二筆：突破前高點 B 確認加碼 (40%)
TRANCHE_3 = 0.30          # 第三筆：主升段延伸突破加碼 (30%)
EXT_RATIO = 1.08          # 第三筆觸
