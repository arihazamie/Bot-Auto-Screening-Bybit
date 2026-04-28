# Bot Auto Screening Bybit

A Python trading bot that automatically scans Bybit USDT-Perpetual futures, generates signals from a multi-layer strategy, and either **simulates trades** (paper mode, default) or **places real orders** (auto-trade mode). Every signal, fill, take-profit hit, and daily summary is delivered to your Telegram chat.

> **Heads up — read this before running anything.**
> - Crypto trading is risky. This bot can lose money, especially in real-trade mode.
> - The default config runs in **paper mode** (no real orders). Run it for at least 1–2 weeks before even thinking about flipping `auto_trade: true`.
> - This is not financial advice. You are responsible for everything the bot does with your account.

---

## Table of contents

1. [What you get](#what-you-get)
2. [Quick start (5 minutes)](#quick-start-5-minutes)
3. [Step-by-step setup](#step-by-step-setup)
4. [Running the bot](#running-the-bot)
5. [Telegram commands](#telegram-commands)
6. [Paper mode vs auto-trade mode](#paper-mode-vs-auto-trade-mode)
7. [How the bot decides to trade](#how-the-bot-decides-to-trade)
8. [Where the data lives](#where-the-data-lives)
9. [Common config knobs](#common-config-knobs)
10. [Troubleshooting](#troubleshooting)
11. [Updating the bot](#updating-the-bot)
12. [Project layout](#project-layout)
13. [Disclaimer](#disclaimer)

---

## What you get

- **Auto screening every 15 minutes**, candle-aligned. If you start the bot at 10:17, the first scan runs at 10:30:05 (5 seconds after the candle closes), then 10:45:05, 11:00:05, and so on.
- **Multi-layer strategy**: trend filter, Smart Money Concepts, chart patterns, derivatives (funding/CVD), volume + volatility regime classifier, and a separate range-trading module.
- **Paper trading simulator** with realistic slippage and spread, so PnL numbers reflect what real trading would feel like.
- **Telegram alerts**: signal, partial fills, TP/SL hits, trade closed, daily report at 07:00 your local time.
- **Telegram remote control**: `/status`, `/pause`, `/resume`, `/balance`, `/trades`, `/report`.
- **Daily safety limits**: max trades per day, daily profit target, daily loss cap, weekend skip.

---

## Quick start (5 minutes)

Use this if you already have Python 3.10+ and a Telegram account. Otherwise jump to the [step-by-step setup](#step-by-step-setup).

```bash
# 1. Clone and install
git clone https://github.com/arihazamie/Bot-Auto-Screening-Bybit.git
cd Bot-Auto-Screening-Bybit
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
cp config.example.json config.json
# → open config.json and fill in:
#     api.telegram_bot_token
#     api.telegram_chat_id
#   (Bybit keys are optional for paper mode)

# 3. Run
python main.py
```

You should see something like:

```
🤖 Bybit Screening Bot v8
   Mode    : PAPER TRADE 📋 (signal + simulasi)
   Debug   : OFF
   Env     : PROD
📐 Timeframes — Entry: 15m | Trend: 1h | Confirm: 5m
📡 Bybit client ready
📋 Watchlist loaded: 100 pairs (age: 0.0h ago)
🚀 Bot Started.
🕖 Watchlist refresh otomatis setiap hari jam 07:00 Asia/Jakarta
📐 Scan candle-aligned: setiap candle 15m close + 5s buffer
📐 Next candle-aligned scan at 03:45:05 UTC
```

If you see that, you're done. Leave it running and watch your Telegram for signals.

---

## Step-by-step setup

### 1. Install Python and Git

You need **Python 3.10 or newer** and **Git**.

- macOS: `brew install python git`
- Ubuntu/Debian: `sudo apt update && sudo apt install python3 python3-venv python3-pip git`
- Windows: install from [python.org](https://www.python.org/downloads/) (check "Add Python to PATH") and [git-scm.com](https://git-scm.com/download/win)

Verify:

```bash
python3 --version    # should print 3.10.x or higher
git --version
```

### 2. Clone the repository and create a virtual environment

```bash
git clone https://github.com/arihazamie/Bot-Auto-Screening-Bybit.git
cd Bot-Auto-Screening-Bybit

python3 -m venv venv
source venv/bin/activate           # Windows: venv\Scripts\activate

pip install -r requirements.txt
```

A virtual environment keeps the bot's libraries isolated from your system Python.

### 3. Create a Telegram bot and get your chat ID

This is the only **mandatory** credential. Without it the bot will refuse to start.

**Create the bot:**

1. Open Telegram, search for `@BotFather`, send `/newbot`.
2. Pick any name and a unique username ending in `bot` (e.g. `mybybit_bot`).
3. BotFather replies with a token that looks like `123456789:ABCdefGHI...`. **Copy this** — it goes into `api.telegram_bot_token`.

**Get your chat ID:**

1. Open a chat with your new bot and send any message (e.g. `hi`).
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser (replace `<YOUR_TOKEN>`).
3. Look for `"chat":{"id":12345678,...}`. That number is your chat ID — copy it into `api.telegram_chat_id`.

> Want signals in a group? Add the bot to the group, send a message in the group, then re-check `getUpdates`. The group chat ID will be a negative number like `-1001234567890`.

### 4. (Optional) Get a Bybit API key

You only need this if you plan to enable real trading later (`auto_trade: true`). For paper mode, leave the placeholders.

1. Sign up / log in at [bybit.com](https://www.bybit.com).
2. Go to **API → Create New Key → System-generated**.
3. Permissions you need:
   - **Contract → Orders & Positions** (read + write)
   - **Wallet → Account Transfer** is **NOT** needed
4. **Important**: enable **IP whitelist** with your bot's static IP. If you don't have one, you can leave it open but it's much less safe.
5. Copy the API key and secret into `api.bybit_key` and `api.bybit_secret`.

> **Never share your API key, never commit it to Git.** The bot reads it from `config.json` which is `.gitignore`'d.

### 5. Fill in `config.json`

```bash
cp config.example.json config.json
```

Open `config.json` and edit the fields under `api`:

```json
"api": {
  "bybit_key":          "YOUR_BYBIT_API_KEY",
  "bybit_secret":       "YOUR_BYBIT_API_SECRET",
  "telegram_bot_token": "123456789:ABC...",
  "telegram_chat_id":   "12345678"
}
```

For paper mode you can leave `bybit_key` and `bybit_secret` as placeholders — the bot only uses public market data in paper mode.

The other sections (`system`, `risk`, `strategy`) come pre-tuned. See [common config knobs](#common-config-knobs) below if you want to customize.

### 6. (Optional) If you're in a region where Bybit is blocked

You'll see `403 Forbidden` or DNS errors when the bot tries to fetch prices. Use a VPN with an exit node in a country where Bybit is accessible (e.g. Singapore, Japan, Germany), then start the bot.

---

## Running the bot

Activate your virtual environment and run:

```bash
source venv/bin/activate
python main.py
```

**Stop it with `Ctrl+C`.** The bot exits cleanly and persists state in `data/bot.db`.

### Run it 24/7

Pick whichever you prefer:

| Method | Best for |
|---|---|
| `nohup python main.py > /dev/null 2>&1 &` | Quick test on a Linux box you already have. |
| `tmux` / `screen` | Easy to detach and reattach to view logs. |
| `systemd` service | Servers — restart on crash, start on boot. |
| Docker | Production-style deployment (no Dockerfile shipped yet, but easy to write). |

A minimal systemd unit:

```ini
# /etc/systemd/system/bybit-bot.service
[Unit]
Description=Bybit Screening Bot
After=network.target

[Service]
Type=simple
User=youruser
WorkingDirectory=/home/youruser/Bot-Auto-Screening-Bybit
ExecStart=/home/youruser/Bot-Auto-Screening-Bybit/venv/bin/python main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then `sudo systemctl enable --now bybit-bot`.

---

## Telegram commands

Send these to your bot in Telegram. They work whether the bot is in paper or real mode.

| Command   | What it does                                          |
|-----------|-------------------------------------------------------|
| `/status` | Current mode, uptime, watchlist size, daily counters  |
| `/balance`| Paper balance (paper mode) or Bybit USDT (real mode)  |
| `/trades` | All currently open trades                             |
| `/report` | Force-send today's daily report                       |
| `/pause`  | Stop opening new trades (existing ones keep running)  |
| `/resume` | Resume opening new trades                             |

`/pause` is your panic button: it freezes new entries instantly without killing the bot.

---

## Paper mode vs auto-trade mode

| Feature                          | Paper (`auto_trade: false`) | Auto-trade (`auto_trade: true`) |
|----------------------------------|-----------------------------|---------------------------------|
| Real orders on Bybit             | No                          | **Yes — real money**            |
| Bybit API key needed             | Optional                    | **Required**                    |
| Slippage / spread simulated      | Yes (5 bps + 2 bps default) | Real exchange fills             |
| TP/SL execution                  | Simulated against live price| Native Bybit limit orders       |
| Telegram alerts                  | Yes                         | Yes                             |
| Daily limits enforced            | Yes                         | Yes                             |
| Bot lock-step with Bybit API     | No (read-only public data)  | Yes — full account access       |

**Strong recommendation:** run paper mode for at least 7–14 calendar days (including a weekend) before flipping `auto_trade: true`. Compare the simulated PnL with what you'd expect — if paper mode loses money, real mode will lose more.

---

## How the bot decides to trade

Every 15 minutes (candle-aligned), the bot:

1. **Picks the watchlist**: top 100 USDT-perp pairs by 24h volume, refreshed daily.
2. **Filters by regime**: classifies each symbol as `TREND_BULL`, `TREND_BEAR`, `RANGE`, `SQUEEZE`, or `ANOMALY`. Anomalies are skipped entirely.
3. **Runs the strategy stack**:
   - Multi-timeframe trend (15m + 1h Supertrend / EMA)
   - Smart Money Concepts (BOS, CHoCH, order blocks, liquidity sweeps)
   - Chart patterns (double top/bottom, flags, triangles, rectangles) with volume gate
   - Quant metrics (Zeta Field, RVOL, OBI, Z-score)
   - Derivatives (funding rate, basis, CVD divergence)
4. **Scores everything** and only generates a signal if min-scores in every layer pass.
5. **Confirms on 5m**: requires 1 closed 5m candle in the signal direction before executing.
6. **Sizes the position** based on `risk_percent`, leverage, and a hard cap of `max_loss_per_trade_pct`.
7. **Sends the signal to Telegram**, inserts a pending paper/real order, and monitors fill + TP/SL.

Daily safety limits stop new entries when:

- `max_daily_trades` reached (default 2)
- Daily PnL ≥ `daily_profit_target_pct` (default +1.2%)
- Daily PnL ≤ `-max_daily_loss_pct` (default −0.8%)
- Weekend (`skip_weekends: true`)

---

## Where the data lives

Everything is in `data/` next to `main.py`:

| File                     | Contents                                                   |
|--------------------------|------------------------------------------------------------|
| `data/bot.db`            | SQLite — signals, active trades, paper balance             |
| `data/bot.log`           | Rotating log file (5 MB × 3 backups)                       |
| `data/watchlist.json`    | Cached top-N pairs                                         |
| `data/state.json`        | Pause flag, last scan timestamp, runtime state             |

To **wipe paper history** and start fresh:

```bash
rm data/bot.db
```

The bot will recreate it with the starting paper balance from `config.json` (`risk.paper_balance`, default `100.0`).

---

## Common config knobs

These are the ones beginners are most likely to want to change. Everything else has sensible defaults.

| Key                                 | Default          | Meaning                                        |
|-------------------------------------|------------------|------------------------------------------------|
| `auto_trade`                        | `false`          | `true` to place real orders. **Be careful.**   |
| `risk.paper_balance`                | `100.0`          | Starting balance for paper mode (USDT)         |
| `risk.risk_percent`                 | `0.01`           | 1% of balance risked per trade                 |
| `risk.max_positions`                | `2`              | Concurrent open trades                         |
| `risk.max_daily_trades`             | `2`              | Hard cap on new entries per day                |
| `risk.max_daily_loss_pct`           | `0.008`          | Stop trading after −0.8% day                   |
| `risk.daily_profit_target_pct`      | `0.012`          | Stop trading after +1.2% day                   |
| `risk.target_leverage`              | `10`             | Used when `use_max_leverage` is `false`        |
| `risk.use_max_leverage`             | `true`           | `true` = use Bybit's max leverage per coin     |
| `risk.max_leverage_cap`             | `100`            | Hard cap regardless of exchange max            |
| `system.skip_weekends`              | `true`           | Skip Saturday and Sunday                       |
| `system.timezone`                   | `"Asia/Jakarta"` | Timezone for the daily report scheduler        |
| `system.scan_post_close_buffer_sec` | `5`              | Wait N seconds after candle close before scan  |
| `strategy.min_adx`                  | `22`             | Minimum trend strength to take a trade         |
| `strategy.risk_reward_min`          | `3.0`            | Reject trades with R:R < 3.0                   |

For the full list of strategy parameters, open `config.example.json` — every key is grouped by purpose.

---

## Troubleshooting

### `❌ CONFIG ERROR — Key wajib berikut belum diisi`

Your `config.json` is missing `api.telegram_bot_token` or `api.telegram_chat_id`. Fill them in and try again.

### `403 Forbidden` or `Connection refused` when fetching Bybit data

You're in a region where Bybit is blocked. Use a VPN with an exit node in Singapore, Japan, or another country where Bybit operates.

### Telegram `getUpdates error: Not Found`

Your `telegram_bot_token` is wrong or the bot was deleted. Double-check the token in BotFather and update `config.json`. The bot will exit at startup if the token is invalid (since the ops-hardening update).

### `getUpdates 409 Conflict`

You're running two copies of the bot pointing at the same Telegram token. Stop one of them.

### Bot starts but no signals appear for hours

Normal during low-volatility periods or weekends. Check `/status` — if regime is mostly `RANGE` or `ANOMALY`, the strategy is correctly being conservative. To verify it works at all, temporarily set `system.skip_weekends: false` and `strategy.min_adx: 15` (don't trade real money on these settings).

### Paper balance shows weird numbers after a trade

Run `sqlite3 data/bot.db 'SELECT * FROM paper_state;'` to inspect. If you suspect corruption, stop the bot, `rm data/bot.db`, and restart — paper history will reset.

### Bot crashes with `ModuleNotFoundError: ...`

You forgot to activate the virtual environment, or `pip install -r requirements.txt` didn't complete. Re-run both.

### `Watchlist stale (Xh > 36h max)`

Watchlist refresh failed for >36 hours (Bybit rate-limit, network, or VPN issue). The bot will try again on the next scan cycle. If it persists, delete `data/watchlist.json` to force a fresh fetch.

---

## Updating the bot

```bash
cd Bot-Auto-Screening-Bybit
git pull
source venv/bin/activate
pip install -r requirements.txt --upgrade
```

Then restart `python main.py`. Your `config.json` and `data/` folder are not touched by `git pull`.

If a new release adds new config keys, the bot will tell you on startup. You can also `diff config.example.json config.json` to spot missing fields.

---

## Project layout

```
.
├── main.py                       # entry point — scheduler & scan loop
├── auto_trades.py                # real-order execution (auto_trade=true)
├── config.example.json           # config template
├── requirements.txt
└── modules/
    ├── config_loader.py          # validates config.json on startup
    ├── exchange.py               # Bybit client (ccxt) — retry, leverage cache
    ├── database.py               # SQLite — atomic balance, signal claim
    ├── leverage.py               # per-coin max leverage resolver
    ├── watchlist.py              # daily top-volume pair refresh
    ├── technicals.py             # EMA, MACD, Stoch RSI, ADX, divergence
    ├── smc.py                    # Smart Money Concepts (BOS/CHoCH/OB/sweep)
    ├── patterns.py               # chart patterns (DT/DB, flags, triangles)
    ├── quant.py                  # Zeta Field, RVOL, Z-score, OBI
    ├── derivatives.py            # funding rate, basis, CVD divergence
    ├── regime.py                 # market regime classifier
    ├── range_strategy.py         # mean-revert + breakout for RANGE regime
    ├── paper_trader.py           # paper fill simulation, slippage, PnL
    ├── paper_runner.py           # paper runner daemon (ingest/execute/monitor)
    ├── telegram_bot.py           # alerts, dashboard, scan completion
    ├── telegram_commands.py      # /status, /pause, /resume, /report ...
    └── notifier.py               # send/send_reply with retry-aware Telegram
```

---

## Disclaimer

This software is provided **as-is, with no warranty of any kind**, for educational and research purposes. Trading cryptocurrency futures involves substantial risk and is not suitable for every investor. The authors are not responsible for any financial losses, account suspensions, exchange ToS violations, or other damages arising from the use of this bot. **Use at your own risk.**

Always:

1. Test in paper mode for at least 1–2 weeks.
2. Start with a small balance you can afford to lose.
3. Use IP-whitelisted, withdrawal-disabled API keys.
4. Watch the bot for the first few days of real trading.
