import os
import requests

# 取得 Telegram 憑證
TG_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

df_results = pd.DataFrame(all_trades)

if df_results.empty:
    report = "⚠️ 回測期間內未觸發任何完整交易訊號。"
else:
    wins = df_results[df_results["pnl_pct"] > 0]
    losses = df_results[df_results["pnl_pct"] < 0]
    breakevens = df_results[df_results["pnl_pct"] == 0]

    win_rate = len(wins) / len(df_results) * 100
    avg_win = wins["pnl_pct"].mean() * 100 if not wins.empty else 0
    avg_loss = losses["pnl_pct"].mean() * 100 if not losses.empty else 0
    total_cum_ret = df_results["pnl_pct"].sum() * 100
    p_l_ratio = f"{abs(avg_win / avg_loss):.2f}:1" if avg_loss != 0 else "N/A"

    report = (
        f"📊 *【三段金字塔加碼波段策略 · 歷史回測報告】*\n"
        f"📅 期間：`{START_DATE}` ~ `{END_DATE}`\n"
        f"────────────────────\n"
        f"• 總交易次數：`{len(df_results)}` 次\n"
        f"• 獲利次數：`{len(wins)}` 次\n"
        f"• 保本平手：`{len(breakevens)}` 次（砍第2筆保本成功）\n"
        f"• 停損次數：`{len(losses)}` 次（第1筆 -10% 出場）\n"
        f"────────────────────\n"
        f"🏆 *勝率統計*：\n"
        f"  - 真實勝率 (排除打平)：`{len(wins) / (len(wins) + len(losses)) * 100:.1f}%`\n"
        f"  - 綜合勝率 (含打平)：`{win_rate:.1f}%`\n"
        f"📈 *盈虧比與報酬*：\n"
        f"  - 平均單筆獲利：`+{avg_win:.2f}%`\n"
        f"  - 平均單筆虧損：`{avg_loss:.2f}%`\n"
        f"  - 風報比 (盈虧比)：`{p_l_ratio}`\n"
        f"  - 策略累計報酬率：`{total_cum_ret:+.2f}%`\n"
        f"────────────────────"
    )

print(report)

# 推播到 Telegram
if TG_TOKEN and TG_CHAT:
    target_chats = [cid.strip() for cid in TG_CHAT.split(",") if cid.strip()]
    for cid in target_chats:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": cid, "text": report, "parse_mode": "Markdown"}
        )
