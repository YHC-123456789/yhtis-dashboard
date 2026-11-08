# V5 回測

- `backtest_v5.py`：原版研究分數的逐日事件回測（結果 `backtest_v5.json`）。
- `backtest_v5b.py`：四種事先固定規則的比較（結果 `backtest_v5b.json`）：
  A 原版、B 近 60 日除權息排除、C 除權息還原價（目前上線）、D C＋20 日均成交值 ≥ 5,000 萬。
- 需要完整歷史行情庫（2018–2026，約 650MB，不放在 GitHub）：
  `V5_STORE=/path/to/store_preview/days python3 v5/backtest/backtest_v5b.py`
- 2026-11-06 執行結果：C 的 11/06 TOP 10 與 `v5/v5_live.py` 輸出完全相同。
