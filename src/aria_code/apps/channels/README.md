# Channel Apps

The chat channels moved to [`services/aria-bots`](../../../../services/aria-bots/README.md):
the conversation layer, approvals and digests are `aria_bots.channels`, the bots
are `aria_bots.feishu_bot` / `aria_bots.telegram_bot`. The modules here with the
old names are shims.

What is still here: `intake.py` (task intake for the CLI) and `tradingview.py`
(the daemon's TradingView webhook), which go with the Python worker and daemon.
