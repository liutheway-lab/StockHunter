name: morning

on:
  schedule:
    - cron: "30 0 * * 1-5"   # 台灣時間 週一到週五 08:30（GitHub 用 UTC，所以是 00:30）
  workflow_dispatch:          # 也可以手動執行：Actions → morning → Run workflow

jobs:
  run:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
          cache: pip
      - run: pip install -r requirements.txt
      - name: Run
        env:
          TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
          TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
        run: python morning.py
