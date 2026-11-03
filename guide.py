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
    a1, a2 = cfg.ATTACK_SPLIT
    k, n = cfg.STAB_DAYS, cfg.STAB_LOOKBACK

    text = f"""📖 *台股選股系統說明書*
━━━━━━━━━━━━━━
*一、大盤五層*
只看兩條線：
月線＝最近 20 天平均價
季線＝最近 60 天平均價
再看這兩條線往上還是往下
🟢 第 1 層　強多頭
　價在月線上、月線在季線上
　兩條線都往上
🟢 第 2 層　多頭
　價在兩條線上
　但線還沒全部往上
🟡 第 3 層　盤整／轉折
　價夾在兩條線中間
　或月線走平（沒有方向）
🔴 第 4 層　空頭
　價在兩條線下
　但線還沒全部往下
🔴 第 5 層　強空頭
　價在月線下、月線在季線下
　兩條線都往下
第 1、5 層再看 5 日、10 日線
（最近 1 週、2 週的平均價）
🔥 過熱：第 1 層，而且
　價 > 5日 > 10日 > 月線
　5日、10日都往上
　→ 別追高，等拉回
😱 恐慌：第 5 層，而且
　價 < 5日 < 10日 < 月線
　5日、10日都往下
　→ 急殺中，留意止跌訊號
標籤只提醒，不改層級和策略
換層要連續 {cfg.LAYER_CONFIRM_DAYS} 天確認
避免一天的假動作
離近 60 日高點跌超過
{pct(cfg.DROP_WARN)} 會多一行警示
（只提醒，不影響層級）

*二、止跌確認（空頭時看）*
先找兩個日子：
爆量下跌日
　最近 {n + cfg.STAB_MAX_AGE} 個交易日裡，
　成交額最大的下跌日
低點日
　最近 {k} 天（含今天）
　股價最低點那一天
　（要在爆量日之後）
三項都要通過：
① 量能
　低點日成交額
　不小於爆量下跌日
　代表：不放的人終於賣了，
　便宜的又有人進場收走
② K 棒型態（二選一）
　長下影線（開低走高）
　　下影線 ≥ 當天高低差的
　　{pct(cfg.STAB_SHADOW)}
　長紅
　　收紅，實體占整根 K 棒
　　≥ {cfg.STAB_RED_BODY * 100:.0f}%
　代表：低檔有人用力買
③ 不破低
　低點日之後的最低點，
　都高於低點日的最低點
　低點日就是今天時，
　先顯示「觀察中」
成交額採用證交所數據，
看相對比例，100% 以上才算過。

⚠️ 這只是「日線」止跌走穩
週線未必止跌，可能再破低
所以通過後只先用
*2～3 成資金*佈局
不猜底、不摸頭、不貪心
趨勢更穩、帳戶賺錢後
再慢慢加碼

*三、四套個股策略*
先過流動性：
　5 日均量 ≥ {cfg.MIN_LOTS} 張
　20 日均成交金額 ≥ {cfg.MIN_AVG_TURNOVER / 1e4:,.0f} 萬

📈 *創高拉回*
① 前高是近 {cfg.PB_LOOKBACK} 日的新高
　EMA20 > EMA60
② 前高之後盤整至少 {cfg.ATTACK_MIN_CONSOL} 天
　盤整期量縮
③ 出現攻擊日就進場（見下方）

🚀 *噴出後整理再攻*
① 近 {cfg.FLAG_WINDOW} 日內有噴出
　3 日漲 ≥ {pct(cfg.FLAG_BURST_3D)}
　或單日漲 ≥ {pct(cfg.FLAG_BURST_1D)}
　且量 ≥ 均量 {cfg.FLAG_BURST_VOL} 倍
② 盤整至少 {cfg.ATTACK_MIN_CONSOL} 天，守住月線
　回檔不超過噴出漲幅的一半
　盤整期量縮
③ 出現攻擊日就進場（見下方）

⚔️ *攻擊日（上面兩套共用）*
盤整條件：
　前高之後盤整 {cfg.ATTACK_MIN_CONSOL}～{cfg.ATTACK_MAX_CONSOL} 天
　盤整最低點離前高 ≤ {pct(cfg.ATTACK_MAX_DEPTH)}
　（超過就算盤整失敗）
攻擊日：
① 收盤第一次站上盤整高點
② 漲幅 ≥ {pct(cfg.ATTACK_MIN_GAIN)}，收紅 K
③ 收在當天區間的上 1/3
④ 量 ≥ 前高那天的 {pct(cfg.ATTACK_VOL_VS_PEAK)}
買法：
　盤中漲 {pct(cfg.CHASE_GAIN)}
　且突破盤整高點就追 {pct(a1)}
　站上前波高點再買 {pct(a2)}
出場：跌破盤整最低點
　（最多賠 {pct(cfg.ATTACK_MAX_LOSS)}）
　突破後移到前高下 {pct(cfg.ATTACK_STOP2_PCT)}
目標：盤整最低點＋整段漲幅
　（整段＝前高之前 30 日低點
　　到前高的漲幅）
創高拉回和噴出後整理再攻
同時符合的股票只列一次
標 ◆＝也符合另一套

🌱 *盤整後噴出*
低檔盤整後，爆量突破
① 近 {cfg.BO_BOX_DAYS} 日低點
　≥ 高點的 {pct(cfg.BO_BOX_RATIO)}（區間夠窄）
② 離 60 日低點漲幅
　≤ {pct(cfg.BO_MAX_RISE)}（還在低檔）
③ 收盤突破 {cfg.BO_PLATFORM} 日高點（頸線）
④ 量 ≥ 20 日均量 {cfg.BO_VOL_MULT:g} 倍
⑤ 站上季線，月線翻揚
出場：頸線下 2%
　與買價下 {pct(cfg.BO_STOP_PCT)} 取較低
目標：買價 +{pct(cfg.BO_TARGET_PCT)}

🛡 *量縮抗跌*
大盤跌時，它不跌也沒量
① 3 日均量
　≤ 20 日均量的 {pct(cfg.DRY_VOL_RATIO)}
② {cfg.STAB_DAYS} 日不破前低
③ 比大盤抗跌：
　大盤自高點起算的跌幅，
　個股最大回檔 ≤ 它的 {pct(cfg.DRY_RS_RATIO)}
④ 離上方大量壓力
　≤ {pct(cfg.DRY_MAX_PRESSURE_DIST)}
　（上方沒有大量壓力就算過）
⑤ 出場價低於買價
出場：近 {cfg.DRY_STOP_DAYS} 日最低點
目標：近 {cfg.DRY_KEY_LOOKBACK} 日高點（突破價）
　第二目標看上方壓力

*四、各層啟用的策略*
第 1、2 層：
　創高拉回、噴出後整理再攻
　盤整後噴出
第 3 層：只開創高拉回
　部位減半
第 4、5 層：預設不進場
　止跌確認通過才啟用
　盤整後噴出、量縮抗跌

*五、清單怎麼看*
```
現價　　今天收盤
突破價　突破才加碼的價位
出場價　跌破就全部賣出
目標價　第 1 個停利點
```
創高拉回、噴後整理：
　攻擊日 {pct(a1)}／突破日 {pct(a2)}
盤整後噴出、量縮抗跌：
　{pct(t[0])}／{pct(t[1])}／{pct(t[2])}
　第三筆＝突破價 × {cfg.EXT_RATIO}
↑ 代表已突破前波高點

⚠️ 規則篩選，非買賣建議"""
    return [text]


if __name__ == "__main__":
    send_telegram(build())
