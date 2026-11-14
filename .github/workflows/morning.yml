name: morning

on:
  schedule:
    - cron: "35 0 * * 1-5"   # 台灣時間 週一到週五 08:35（UTC 00:35）。08:40 還沒開始跑，Render 會補叫（bot_server.py）；GitHub 排程晚到、Render 已經補過，就跳過不重發
  workflow_dispatch:          # 也可以手動執行：Actions → morning → Run workflow

permissions:
  contents: read
  actions: read              # 排程要查「今天是不是已經發過」（Render 補叫過就不重發）

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - name: 今天已經發過就跳過（只有 GitHub 排程才檢查；Render 補叫、手動執行一定跑）
        id: gate
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          go=true
          if [ "${{ github.event_name }}" = "schedule" ]; then
            n=$(gh run list -R "$GITHUB_REPOSITORY" --workflow morning.yml --created ">=$(date -u +%F)" --limit 20 \
                 --json databaseId,status,conclusion \
                 --jq "[.[] | select(.databaseId != $GITHUB_RUN_ID) | select(.status != \"completed\" or .conclusion == \"success\")] | length" || echo 0)
            if [ "${n:-0}" -gt 0 ]; then go=false; echo "今天已經發過早報了（Render 補叫過），不再重發"; fi
          fi
          echo "go=$go" >> "$GITHUB_OUTPUT"
      - uses: actions/checkout@v4
        if: steps.gate.outputs.go == 'true'
      - uses: actions/setup-python@v5
        if: steps.gate.outputs.go == 'true'
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt
        if: steps.gate.outputs.go == 'true'
      - name: Run
        if: steps.gate.outputs.go == 'true'
        env:
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
        run: python morning.py
