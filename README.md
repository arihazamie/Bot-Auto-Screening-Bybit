# Bot Auto Screening Bybit

A Python **signal-only screener** that scans Bybit USDT-Perpetual futures every 15 minutes, runs a multi-layer pattern + SMC + derivatives strategy, and pushes opportunities to your Telegram chat. A virtual portfolio tracker (paper mode) follows each signal so you can see what win-rate and PnL the strategy would have produced — without ever placing a real order.

> **Heads up — read this before running anything.**
> - This bot **does not place real orders**. It is screening + paper tracker only. If you want to act on a signal, do it manually.
> - Signals can still lose money if you act on them. Always paper-test for 1–2 weeks before trusting any signal blindly.
> - This is not financial advice.

---

## Table of contents

1. [What you get](#what-you-get)
2. [Quick start (5 minutes)](#quick-start-5-minutes)
3. [Step-by-step setup](#step-by-step-setup)
4. [Running the bot](#running-the-bot)
5. [Telegram commands](#telegram-commands)
6. [How the bot decides to trade](#how-the-bot-decides-to-trade)
7. [Where the data lives](#where-the-data-lives)
8. [Config reference](#config-reference)
9. [Troubleshooting](#troubleshooting)
10. [Updating the bot](#updating-the-bot)
11. [Project layout](#project-layout)
12. [Disclaimer](#disclaimer)

---

## What you get

- **Auto screening every 15 minutes**, candle-aligned. The first scan runs immediately on startup, reading whatever candles are already closed. After that, every scan fires right after the next candle closes. Example: start the bot at 10:17 → first scan happens at 10:17 (uses the 15m candle that closed at 10:15 and the 1h candle that closed at 10:00), then 10:30:05, 10:45:05, 11:00:05 (this one also picks up the fresh 1h candle), and so on.
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

### 4. (Optional) Bybit API key

The bot is signal-only and pulls all candle data from public endpoints, so a Bybit API key is **not required**. Leave the placeholders in `config.json`.

> **Never share API keys or commit them to Git.** The bot reads everything from `config.json`, which is `.gitignore`'d.

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

Send these to your bot in Telegram.

| Command   | What it does                                          |
|-----------|-------------------------------------------------------|
| `/status` | Current uptime, watchlist size, daily counters        |
| `/balance`| Paper portfolio balance (USDT)                        |
| `/trades` | All currently open paper trades                       |
| `/report` | Force-send today's daily report                       |
| `/pause`  | Stop opening new trades (existing ones keep running)  |
| `/resume` | Resume opening new trades                             |

`/pause` is your panic button: it freezes new entries instantly without killing the bot.

---

## How the bot decides to trade

On startup the bot scans once immediately (using the most recent already-closed candles), then keeps scanning right after every 15m candle close. On each scan, the bot:

1. **Picks the watchlist**: top **300** USDT-perp pairs by 24h volume, refreshed daily at 07:00 local time.
2. **Filters by regime**: classifies each symbol as `TREND_BULL`, `TREND_BEAR`, `RANGE`, `SQUEEZE`, or `ANOMALY`. Anomalies are skipped entirely.
3. **Runs the pattern + strategy stack**:
   - Multi-timeframe trend (15m + 1h Supertrend / EMA)
   - Smart Money Concepts (BOS, CHoCH, order blocks, liquidity sweeps)
   - Chart patterns (double top/bottom, flags, triangles, rectangles) with volume gate
   - **Pattern registry**: 28+ pattern detectors (candlestick, harmonic XABCD, ICT/SMC extras, Wyckoff Spring/Upthrust, Volume Profile POC/VAH/VAL, RSI/MACD divergence single-TF + multi-TF, Elliott Wave ABC corrective)
   - Quant metrics (Zeta Field, RVOL, OBI, Z-score)
   - Derivatives (funding rate, basis, CVD divergence)
4. **Scores everything** and only generates a signal if min-scores in every layer pass.
5. **Confirms on 5m**: requires 1 closed 5m candle in the signal direction before executing.
6. **Structure-aware setup**:
   - **Entry**: limit order at the nearest swing low (Long) / swing high (Short) ± 0.1% buffer
   - **SL**: anchored to the pattern's invalidation level (e.g., bullish ABC → below L₃; Order Block → below OB low) with an ATR buffer
   - **TPs**: snapped to nearby swing highs/lows or Fibonacci extensions of the impulse leg, falling back to 1R/2R/3R when no structure is in window
   - **R:R floor**: regime-adaptive (default 2.0 across all regimes; can be tuned per regime)
   - **Runner trail**: after TP2 fills, the remaining 30% trails via a chandelier exit (highest_seen − 1.33R for Long), capping legacy TP1-trail behavior
7. **Sends the signal to Telegram** with pattern winrate (baseline / actual / sample count, HIGH/MED/LOW confidence label).
8. **Tracks paper fills** and updates rolling 30-day pattern winrate stats on close.

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

## Config reference

The bot reads everything from `config.json`. Almost every threshold, lookback, and tolerance is configurable so you can tune behavior without touching code. Sections below mirror the structure of `config.example.json` — copy it to `config.json` and adjust whatever you want.

> **Pro tip:** if a key isn't in your `config.json`, the bot falls back to the default shown below. You only need to override the ones you actually want to change.

### `system` — Scan timing & timeframes

| Key                            | Default          | What it does                                                                |
|--------------------------------|------------------|-----------------------------------------------------------------------------|
| `timezone`                     | `"Asia/Jakarta"` | Timezone for the daily report scheduler (07:00 local)                       |
| `max_threads`                  | `20`             | Parallel scan threads                                                       |
| `check_interval_minutes`       | `15`             | Scan cadence (matches `entry_timeframe` candle close)                       |
| `entry_timeframe`              | `"15m"`          | Primary signal TF                                                           |
| `trend_timeframe`              | `"1h"`           | Higher TF for trend bias                                                    |
| `confirm_timeframe`            | `"5m"`           | LTF for entry confirmation                                                  |
| `min_candles_analysis`         | `150`            | Skip symbols with fewer closed candles                                      |
| `watchlist_top_n`              | `300`            | How many top USDT-perp pairs to scan                                        |
| `active_hours_utc`             | `[6, 22]`        | Only scan within this UTC window                                            |
| `skip_weekends`                | `true`           | Skip Saturday and Sunday                                                    |
| `skip_hours_utc`               | `[]`             | Extra UTC hours to skip (e.g. major news windows)                           |
| `watchlist_max_age_hours`      | `36`             | Refuse to use a watchlist older than this                                   |
| `scan_post_close_buffer_sec`   | `5`              | Wait N seconds after candle close before scan (lets exchange settle)        |

### `risk` — Money & position management

| Key                            | Default | What it does                                                                  |
|--------------------------------|---------|-------------------------------------------------------------------------------|
| `paper_balance`                | `100.0` | Starting balance for paper portfolio (USDT)                                   |
| `risk_percent`                 | `0.01`  | Fraction of balance risked per trade (1%)                                     |
| `max_positions`                | `2`     | Concurrent open paper trades                                                  |
| `max_daily_trades`             | `2`     | Hard cap on new entries per day                                               |
| `max_daily_loss_pct`           | `0.008` | Stop opening trades after −0.8% on the day                                    |
| `daily_profit_target_pct`      | `0.012` | Stop opening trades after +1.2% on the day                                    |
| `pending_expire_hours`         | `3`     | Cancel a pending limit order if it hasn't filled in this many hours           |
| `tp_split`                     | `[0.4, 0.3, 0.3]` | TP1 / TP2 / TP3 partial-close fractions                                |
| `atr_sl_multiplier`            | `1.5`   | ATR multiplier for the SL distance (legacy SL path)                           |
| `atr_sl_length`                | `14`    | ATR period                                                                    |
| `target_leverage`              | `10`    | Default leverage for paper sizing                                             |
| `use_max_leverage`             | `true`  | If true, use Bybit's per-coin max (capped by `max_leverage_cap`)              |
| `max_leverage_cap`             | `100`   | Hard cap on leverage regardless of exchange max                               |
| `paper_slippage_bps`           | `5`     | Simulated slippage on paper fills (5 bps = 0.05%)                             |
| `paper_spread_bps`             | `2`     | Simulated bid/ask spread                                                      |
| `paper_max_spread_bps`         | `50`    | Reject fills if spread exceeds this (gapped market)                           |

### `strategy` — Filters, gates, structure

The biggest section. Knobs that change *what counts as a signal* and *what the resulting entry/SL/TP looks like*.

| Key                              | Default | What it does                                                              |
|----------------------------------|---------|---------------------------------------------------------------------------|
| `min_tech_score`                 | `3`     | Minimum technical confluence score                                        |
| `min_quant_score`                | `3`     | Minimum quant confluence score                                            |
| `min_smc_score`                  | `4`     | Minimum SMC confluence score                                              |
| `min_deriv_score`                | `2`     | Minimum derivatives score                                                 |
| `risk_reward_min`                | `2.0`   | Floor R:R when regime-adaptive is off / regime is `UNKNOWN`               |
| `min_adx`                        | `22`    | Minimum trend strength                                                    |
| `min_atr_pct` / `max_atr_pct`    | `0.003` / `0.015` | ATR% must be in this band                                       |
| `min_tp1_distance_pct`           | `0.004` | TP1 must be ≥ this fraction away from entry (filters chop)                |
| `require_sl_beyond_swing`        | `true`  | SL must sit beyond the recent swing extreme                               |
| `sl_swing_buffer_atr`            | `0.3`   | Extra ATR buffer past the swing for the SL                                |
| `btc_bias_adx_min`               | `25`    | BTC ADX gate for direction bias                                           |
| `candle_confirmation`            | `true`  | Require the candle that triggered the pattern to fully close              |
| `limit_order_offset_pct`         | `0.002` | Fallback offset for non-structure entries (0.2%)                          |
| `swing_entry_enabled`            | `true`  | Use structure-based limit at nearest swing low/high                       |
| `swing_entry_buffer_pct`         | `0.001` | Buffer past the swing (0.1%)                                              |
| `swing_entry_max_drift_pct`      | `0.015` | Reject swing entry if it sits >1.5% from current price                    |
| `swing_entry_pivot_lookback`     | `60`    | Bars to scan for swing pivots                                             |
| `swing_entry_pivot_order`        | `5`     | argrelextrema half-window                                                 |
| `min_sl_pct` / `max_sl_pct`      | `0.005` / `0.03` | Floor/cap on SL distance as fraction of entry                    |
| `pattern_aware_sl_enabled`       | `true`  | Anchor SL to pattern invalidation level instead of pure ATR               |
| `sl_invalidation_buffer_atr`     | `0.3`   | ATR buffer past the invalidation extreme                                  |
| `tp_structure_enabled`           | `true`  | Snap TPs to swing/Fib structure                                           |
| `tp_structure_tol_below_r`       | `0.3`   | Don't snap TP closer than `default − 0.3R`                                |
| `tp_structure_tol_above_r`       | `0.5`   | Allow TP to extend up to `default + 0.5R` past structure                  |
| `regime_adaptive_rr_enabled`     | `true`  | Use per-regime R:R thresholds                                             |
| `min_rr_trend`                   | `2.0`   | Min R:R for `TREND_BULL` / `TREND_BEAR`                                   |
| `min_rr_range`                   | `2.0`   | Min R:R for `RANGE` / `SQUEEZE`                                           |
| `chandelier_trail_enabled`       | `true`  | Trail runner SL post-TP2                                                  |
| `chandelier_trail_r_mult`        | `1.33`  | Trail distance in R (1.33 ≈ 2×ATR when SL = 1.5×ATR)                      |
| `mtf_confluence_enabled`         | `true`  | Require multi-TF agreement                                                |
| `correlation_filter_enabled`     | `true`  | Reject trades that just duplicate exposure to an open trade               |
| `correlation_threshold`          | `0.7`   | Pearson r above which two symbols count as correlated                     |
| `correlation_lookback`           | `50`    | Bars used for correlation calc                                            |
| `correlation_sector_max`         | `1`     | Max concurrent trades per sector                                          |
| `max_funding_long` / `min_funding_short` | `0.0008` / `−0.0008` | Skip Long/Short when funding is extreme       |
| `cvd_lookback`                   | `30`    | Bars for CVD divergence                                                   |
| `cvd_min_adx`                    | `20`    | Skip CVD signal in choppy regimes                                         |

### `setup` — Fibonacci levels for legacy entry/TP/SL math

| Key             | Default  | What it does                                              |
|-----------------|----------|-----------------------------------------------------------|
| `fib_entry_start` / `fib_entry_end` | `0.5` / `0.618` | Entry retracement zone of impulse leg |
| `fib_sl`        | `0.27`   | SL beyond fib retracement of impulse leg                  |
| `fib_tp_1` / `fib_tp_2` / `fib_tp_3` | `1.0` / `1.618` / `2.618` | Default TP extensions     |

### `strategy.regime` — Regime classifier

| Key                       | Default | What it does                                            |
|---------------------------|---------|---------------------------------------------------------|
| `trend_adx`               | `22`    | ADX threshold for `TREND_BULL` / `TREND_BEAR`           |
| `anomaly_atr_pct`         | `0.025` | ATR% threshold for `ANOMALY` (auto-skip)                |
| `squeeze_bbw_pct` / `range_bbw_pct` | `0.20` / `0.50` | BBWidth thresholds for SQUEEZE / RANGE   |
| `lookback`                | `120`   | Bars used for regime inputs                             |
| `skip_when_anomaly`       | `true`  | Hard-skip ANOMALY regime entirely                       |

### `strategy.range` — RANGE-regime mean-revert config

| Key                            | Default | What it does                                       |
|--------------------------------|---------|----------------------------------------------------|
| `enabled`                      | `true`  | Toggle range strategy                              |
| `bb_length` / `bb_std`         | `20` / `2.0` | Bollinger Band parameters                     |
| `rsi_oversold` / `rsi_overbought` | `30` / `70` | RSI bands                                  |
| `squeeze_breakout_volume_mult` | `1.5`   | RVOL required for squeeze breakout                 |

### `strategy.ltf_confirmation` — 5m confirmation gate

| Key                          | Default | What it does                                              |
|------------------------------|---------|-----------------------------------------------------------|
| `enabled`                    | `true`  | Toggle 5m confirmation                                    |
| `lookback_bars`              | `3`     | How many 5m bars must agree                               |
| `require_close_in_direction` | `true`  | Latest 5m close must align with signal direction          |

### `patterns` — Chart pattern detectors

| Key                       | Default | What it does                                                  |
|---------------------------|---------|---------------------------------------------------------------|
| `tolerance`               | `0.015` | ±1.5% tolerance for pattern boundary equality                 |
| `volume_multiplier`       | `1.8`   | Required RVOL on the breakout candle                          |
| `min_pattern_adx`         | `20`    | Min ADX to take a pattern                                     |
| `min_double_gap_bars`     | `5`     | Min bars between the two tops/bottoms                         |
| `min_double_reject_atr`   | `0.4`   | Required wick rejection in ATR units                          |
| `harmonic_tolerance`      | `0.08`  | ±8% Fibonacci tolerance for XABCD harmonic patterns           |
| `harmonic_pivot_order`    | `5`     | Pivot detection window for harmonic XABCD                     |
| `double_top` / `double_bottom` / `bull_flag` / `bear_flag` / `ascending_triangle` / `descending_triangle` / `bullish_rectangle` | `true` | Toggle individual chart patterns |

### `pattern_stats` — Rolling 30-day winrate & confidence labels

| Key                            | Default | What it does                                                                 |
|--------------------------------|---------|------------------------------------------------------------------------------|
| `min_samples_actual_override`  | `10`    | Sample count needed before "actual winrate" overrides the literature baseline |
| `rolling_window_days`          | `30`    | Rolling window for winrate aggregation                                       |
| `confidence_high`              | `0.65`  | Winrate ≥ this → HIGH confidence label                                       |
| `confidence_med`               | `0.50`  | Winrate ≥ this → MED label (else LOW)                                        |

### `divergence` — RSI/MACD divergence + multi-TF confluence

| Key                  | Default | What it does                                              |
|----------------------|---------|-----------------------------------------------------------|
| `pivot_order`        | `3`     | Bars each side for argrelextrema                          |
| `min_pivots`         | `2`     | Minimum pivots required for a divergence                  |
| `min_tf_confluence`  | `2`     | Number of TFs that must agree for a multi-TF hit          |

### `volume_profile` — POC / VAH / VAL detector

| Key              | Default | What it does                                       |
|------------------|---------|----------------------------------------------------|
| `lookback`       | `100`   | Bars in the profile window                         |
| `bins`           | `50`    | Histogram bin count                                |
| `value_area_pct` | `0.70`  | Value Area = X% of volume                          |
| `tolerance_bin`  | `1`     | Bin-widths of slack to count price as "near a level" |

### `wyckoff` — Spring / Upthrust detection

| Key              | Default | What it does                                       |
|------------------|---------|----------------------------------------------------|
| `range_lookback` | `30`    | How many bars defines the trading range            |
| `rvol_length`    | `20`    | Volume MA window                                   |
| `rvol_min`       | `1.3`   | Required RVOL on the spring/upthrust candle        |

### `ict_extras` — Breaker / Mitigation block detection

| Key                    | Default | What it does                                                 |
|------------------------|---------|--------------------------------------------------------------|
| `lookback`             | `50`    | How far back to scan for swings / OB candidates              |
| `swing_order`          | `3`     | argrelextrema half-window for swing detection                |
| `test_tolerance_atr`   | `0.5`   | How close to a zone (in ATR) counts as a "test"              |
| `displacement_atr_min` | `1.5`   | Min displacement-candle body in ATR units                    |

### `tp_resolver` — Structure-aligned TP snap

| Key               | Default | What it does                                       |
|-------------------|---------|----------------------------------------------------|
| `lookback_bars`   | `80`    | Bars scanned for swing-pivot candidates            |
| `pivot_order`     | `4`     | argrelextrema half-window                          |
| `impulse_lookback`| `30`    | Bars for the last impulse leg (Fib anchor)         |

### `invalidation` — Pattern-aware SL anchor

| Key                     | Default | What it does                                           |
|-------------------------|---------|--------------------------------------------------------|
| `default_lookback_bars` | `10`    | Fallback lookback for unknown / unmapped patterns      |
| `min_bars_required`     | `5`     | Minimum bars before attempting to compute invalidation |

### `telegram` — Telegram command rate limit

| Key                     | Default | What it does                                       |
|-------------------------|---------|----------------------------------------------------|
| `rate_limit_window_sec` | `60`    | Rolling window for command rate limit              |
| `rate_limit_max`        | `10`    | Max commands per chat_id per window                |

### `regime_alerts` — BTC global regime change notifications

When the BTC 1h regime classification changes between scans, the bot sends a Telegram alert (e.g. *"BTC: TREND_BULL → ANOMALY — new entries auto-paused"*). The previous regime is persisted in `state_store` so transitions are detected across restarts.

| Key                | Default            | What it does                                                                                       |
|--------------------|--------------------|----------------------------------------------------------------------------------------------------|
| `enabled`          | `true`             | Master switch for regime change alerts                                                             |
| `btc_symbol`       | `BTC/USDT:USDT`    | CCXT symbol used to fetch BTC OHLCV                                                                |
| `timeframe`        | `1h`               | Timeframe for regime classification                                                                |
| `candles`          | `200`              | Number of bars to fetch (≥ 120 enforced)                                                           |
| `notify_first_run` | `false`            | Send an informational alert on the very first classification (when no previous regime is stored)   |

### `scan_summary` — Scan-end console output

| Key                        | Default | What it does                                                                                  |
|----------------------------|---------|-----------------------------------------------------------------------------------------------|
| `show_regime_distribution` | `true`  | After each scan, print how many watchlist symbols fell into each regime (TREND/RANGE/etc.)    |

### `daily_report` — Daily PnL Telegram message

| Key                       | Default | What it does                                                                                                |
|---------------------------|---------|-------------------------------------------------------------------------------------------------------------|
| `show_regime_breakdown`   | `true`  | Append a *By Regime* section listing trade count + W/L + WR + PnL per regime, plus best/worst regime call-out |

### Patterns NOT exposed as config

Some pattern definitions are *part of the pattern itself* and would corrupt the detector if tweaked:

- **Elliott Wave ABC**: Fibonacci ratios for B retrace (38.2%–95%) and C extension (61.8%–200%) are pattern definitions, not knobs.
- **Harmonic XABCD**: Per-pattern Fibonacci ratios (Gartley/Bat/Butterfly/Crab/Shark) are Carney's definitions.
- **Pattern baseline winrates**: Sourced from literature (Bulkowski, Carney, Pruden, Steidlmayer/Dalton, ICT material). Override happens automatically once `pattern_stats.min_samples_actual_override` paper trades have closed.

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
├── main.py                       # entry point — scheduler, scan loop, signal pipeline
├── config.example.json           # config template (copy to config.json)
├── requirements.txt
└── modules/
    ├── config_loader.py          # validates config.json on startup
    ├── exchange.py               # Bybit client (ccxt) — retry, leverage cache
    ├── database.py               # SQLite — signals, active trades, paper_state, pattern_stats
    ├── leverage.py               # per-coin max leverage resolver
    ├── watchlist.py              # daily top-N USDT-perp by volume
    ├── indicators.py             # ATR, ADX, RSI, Stoch RSI, MACD primitives
    ├── technicals.py             # EMA, MACD, Stoch RSI, ADX, MTF confluence
    ├── smc.py                    # Smart Money Concepts (BOS/CHoCH/OB/sweep)
    ├── patterns.py               # chart patterns (DT/DB, flags, triangles)
    ├── candlestick_patterns.py   # 13 candlestick formations
    ├── harmonic_patterns.py      # Gartley/Bat/Butterfly/Crab/Shark XABCD
    ├── ict_extras.py             # Breaker / Mitigation block detectors
    ├── wyckoff_patterns.py       # Spring / Upthrust
    ├── volume_profile.py         # POC / VAH / VAL reaction & rejection
    ├── divergence.py             # RSI/MACD regular divergence (single + multi-TF)
    ├── elliott_wave.py           # Elliott Wave ABC corrective detector
    ├── pattern_registry.py       # central aggregator + literature baseline winrates
    ├── signal_formatter.py       # Telegram pattern-stats payload renderer
    ├── invalidation.py           # pattern-aware SL invalidation level resolver
    ├── tp_resolver.py            # structure-aligned TP snap (swing + Fib)
    ├── derivatives.py            # funding rate, basis, CVD divergence
    ├── regime.py                 # market regime classifier
    ├── range_strategy.py         # mean-revert + breakout for RANGE regime
    ├── paper_trader.py           # paper fill simulation, slippage, chandelier trail
    ├── paper_runner.py           # paper runner daemon (ingest / execute / monitor)
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
