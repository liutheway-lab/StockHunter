"""guide.py — 策略說明書（只在你手動執行時，傳一則獨立的訊息到 Telegram）
數字全部讀 config.py，所以改了參數再執行一次，說明就會跟著更新。
建議收到後在 Telegram 長按訊息 →「釘選」，之後隨時回頭查。
"""
import config as cfg
from notify import send_telegram


def pct(x):
    return f"{x * 100:.0f}%"


def build():
    t = cfg.TRANCHES
    a = f"""📖 *台股選股系統說明書*
（夜間掃描、晨間速報都照這份規則）
━━━━━━━━━━━━━━
*一、大盤五層*
看加權指數收盤價相對 MA20、MA60：
🟢 第 1 層 強多頭：收盤 > MA5 > MA10 > MA20，且在 MA60 上
🟢 第 2 層 多頭：收盤在 MA20 與 MA60 之上
🟡 第 3 層 盤整／轉折：夾在兩條線中間
🔴 第 4 層 空頭：收盤在 MA20 與 MA60 之下
🔴 第 5 層 強空頭：收盤 < MA5 < MA10 < MA20，且在 MA60 下

*二、各層啟用哪些策略*
第 1、2 層：創高拉回、噴出後整理再攻
第 3 層：同上，但標示風險、部位縮小
第 4、5 層：預設不進場；通過「止跌確認」才啟用 盤整後噴出、量縮抗跌

*三、止跌確認（只在空頭時看，兩項都要過）*
1. 不破低：最近 {cfg.STAB_DAYS} 日最低點，高於前 {cfg.STAB_LOOKBACK} 日的最低點
2. 量承接：最近 {cfg.STAB_DAYS} 日的最大量，不小於前 {cfg.STAB_LOOKBACK} 日裡「下跌日最大量」（爆量下跌日）
訊息裡的百分比 = 最近 {cfg.STAB_DAYS} 日最大量 ÷ 爆量下跌日的量，要 100% 以上才算過。
成交額用證交所口徑（含零股、盤後、鉅額），跟看盤軟體會差約 7%，但兩邊都用同一口徑，比值不受影響。"""

    b = f"""*四、四套個股策略*
所有策略都先過流動性：5 日均量 ≥ {cfg.MIN_LOTS} 張，20 日均成交金額 ≥ {cfg.MIN_AVG_TURNOVER / 1e4:,.0f} 萬。

📈 *創高拉回*
・前高是近 {cfg.PB_LOOKBACK} 日的新高
・現價從前高回檔，但不超過 {pct(cfg.PB_PULLBACK_MAX)}（且要在能讓前兩筆平均成本 ≤ 前高 {pct(cfg.PB_MAX_AVG_RATIO)} 的範圍內）
・今日量 ≤ 前高日量的 {pct(cfg.PB_VOL_RATIO)}（量縮）
・EMA20 > EMA60，趨勢向上
・目標＝平均成本 × (1 + 停損% × {cfg.PB_RRR})，風報比 {cfg.PB_RRR}

🚀 *噴出後整理再攻*
・近 {cfg.FLAG_WINDOW} 日內噴出：3 日漲 ≥ {pct(cfg.FLAG_BURST_3D)} 或單日漲 ≥ {pct(cfg.FLAG_BURST_1D)}，且量 ≥ 均量 {cfg.FLAG_BURST_VOL} 倍
・之後整理至少 {cfg.FLAG_MIN_CONSOL} 天、回檔不超過噴出漲幅一半、守住月線、整理期量縮
・現價距前波高點 {pct(cfg.FLAG_NEAR)} 內（或已突破，標 ↑）

🌱 *盤整後噴出*
・離 60 日低點漲幅 ≤ {pct(cfg.BO_MAX_RISE)}（還在低檔）
・今日突破 {cfg.BO_PLATFORM} 日頸線，量 ≥ 20 日均量 {cfg.BO_VOL_MULT:.0f} 倍
・站上季線、月線翻揚
・停損：頸線下 2% 或買價下 {pct(cfg.BO_STOP_PCT)} 取低；目標 +{pct(cfg.BO_TARGET_PCT)}

🛡 *量縮抗跌*
・3 日均量 ≤ 20 日均量的 {pct(cfg.DRY_VOL_RATIO)}
・{cfg.STAB_DAYS} 日不破前低
・從大盤高點起算，個股跌幅 ≤ 大盤的 {pct(cfg.DRY_RS_RATIO)}
・停損＝跌破個股前低；目標風報比 {cfg.DRY_RRR}

*五、怎麼看清單的四個價格*
現價：今天收盤
突破：第 2 筆買進的關鍵價（突破才加碼）
停損：第 1 筆的停損價
目標：第 1 個停利目標
分批：{pct(t[0])}／{pct(t[1])}／{pct(t[2])}（第 1 筆進場、第 2 筆突破關鍵價、第 3 筆關鍵價 × {cfg.EXT_RATIO}），第 2 筆防守在關鍵價下 {pct(cfg.STOP_ADD_PCT)}
↑ 表示已經突破前波高點。

⚠️ 以上是規則篩選，不是買賣建議；進出場請自己再確認。
想調整任何數字，改 config.py 就好。"""
    return [a, b]


if __name__ == "__main__":
    send_telegram(build())
