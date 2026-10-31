"""guide.py — 策略說明書（手動執行時，傳一則獨立訊息到 Telegram）
數字全部讀 config.py，改了參數再執行一次，說明就會跟著更新。
每行都刻意寫短，手機上才不會被切成兩段。
"""
import config as cfg
import strategies as st
from notify import send_telegram


def pct(x):
    return f"{x * 100:.0f}%"


def build():
    t = cfg.TRANCHES
    max_ratio = st.solve_max_entry_ratio(*st.norm_ratios(t)[:2])
    pb_lo = (1 - max_ratio) * 100
    k, n = cfg.STAB_DAYS, cfg.STAB_LOOKBACK

    text = f"""📖 *台股選股系統說明書*
━━━━━━━━━━━━━━
*一、大盤五層*
看加權指數收盤與均線的位置
🟢 第 1 層　強多頭
　收盤 > MA5 > MA10 > MA20
　且收盤在 MA60 之上
🟢 第 2 層　多頭
　收盤在 MA20、MA60 之上
🟡 第 3 層　盤整／轉折
　收盤站上 MA20、MA60 其中一條
　同時跌破另一條
🔴 第 4 層　空頭
　收盤在 MA20、MA60 之下
🔴 第 5 層　強空頭
　收盤 < MA5 < MA10 < MA20
　且收盤在 MA60 之下

*二、各層啟用的策略*
第 1、2 層：創高拉回、噴出後整理再攻
第 3 層：同上，標示風險、縮小部位
第 4、5 層：預設不進場
　止跌確認通過才啟用
　盤整後噴出、量縮抗跌

*三、止跌確認（空頭時看）*
兩項都要通過：
① 不破低
　近 {k} 日最低點
　高於前 {n} 日的最低點
② 量承接
　近 {k} 日最大量
　不小於前 {n} 日下跌日的最大量
　（即「爆量下跌日」）
成交額採用證交所數據，
看相對比例，100% 以上才算過。

*四、四套個股策略*
先過流動性：
　5 日均量 ≥ {cfg.MIN_LOTS} 張
　20 日均成交金額 ≥ {cfg.MIN_AVG_TURNOVER / 1e4:,.0f} 萬

📈 *創高拉回*
① 前高是近 {cfg.PB_LOOKBACK} 日的新高
② 回檔 {pb_lo:.1f}%～{cfg.PB_PULLBACK_MAX * 100:.0f}%
③ 今日量 ≤ 前高日量的 {pct(cfg.PB_VOL_RATIO)}
④ EMA20 > EMA60
停損：進場價下 {pct(cfg.PB_STOP_PCT)}
目標：風報比 {cfg.PB_RRR}

🚀 *噴出後整理再攻*
① 近 {cfg.FLAG_WINDOW} 日內有噴出
　3 日漲 ≥ {pct(cfg.FLAG_BURST_3D)}，或單日漲 ≥ {pct(cfg.FLAG_BURST_1D)}
　且量 ≥ 均量 {cfg.FLAG_BURST_VOL} 倍
② 整理至少 {cfg.FLAG_MIN_CONSOL} 天
③ 回檔不超過噴出漲幅的一半
④ 守住月線，整理期量縮
⑤ 現價距前波高點 {pct(cfg.FLAG_NEAR)} 內
　（已突破者標 ↑）

🌱 *盤整後噴出*
① 離 60 日低點漲幅 ≤ {pct(cfg.BO_MAX_RISE)}
② 突破 {cfg.BO_PLATFORM} 日頸線
③ 量 ≥ 20 日均量 {cfg.BO_VOL_MULT:.0f} 倍
④ 站上季線、月線翻揚
停損：頸線下 2% 或買價下 {pct(cfg.BO_STOP_PCT)}，取低
目標：+{pct(cfg.BO_TARGET_PCT)}

🛡 *量縮抗跌*
① 3 日均量 ≤ 20 日均量的 {pct(cfg.DRY_VOL_RATIO)}
② {k} 日不破前低
③ 個股跌幅 ≤ 大盤的 {pct(cfg.DRY_RS_RATIO)}
　（從大盤 20 日高點起算）
停損：跌破個股前低
目標：風報比 {cfg.DRY_RRR}

*五、清單怎麼看*
```
現價　　今天收盤
突破價　突破才加碼的價位
停損價　第 1 筆的停損
目標價　第 1 個停利點
分批　　{pct(t[0])}／{pct(t[1])}／{pct(t[2])}
第三筆　突破價 × {cfg.EXT_RATIO}
```
↑ 代表已突破前波高點

⚠️ 規則篩選，非買賣建議"""
    return [text]


if __name__ == "__main__":
    send_telegram(build())
