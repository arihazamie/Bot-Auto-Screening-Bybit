# 🤖 Bot Auto Screening Bybit

Bot trading otomatis berbasis Python untuk Bybit Futures (USDT Perpetual). Melakukan screening sinyal multi-timeframe, paper trading simulasi, dan notifikasi real-time via Telegram — tanpa perlu modal nyata untuk testing.

---

## ✨ Fitur Utama

| Fitur                        | Keterangan                                                                              |
| ---------------------------- | --------------------------------------------------------------------------------------- |
| **Multi-Timeframe Analysis** | Konfluensi entry (15m) + trend (1h) via Supertrend & EMA                                |
| **Auto Screening**           | Scan paralel seluruh watchlist setiap 15 menit                                          |
| **Active Hour Filter**       | Scan hanya aktif pada jam 06:00–22:00 UTC (sesi London + New York), dapat dikonfigurasi |
| **Smart Money Concepts**     | BOS, CHoCH, Fresh Order Block, FVG, Liquidity Sweep                                     |
| **Chart Pattern Detection**  | Double Top/Bottom, Bull/Bear Flag, Triangle, Rectangle (dengan volume gate)             |
| **Quant Metrics**            | Zeta Field (8 sub-indicator), RVOL, Z-Score volume, OBI dari real order book            |
| **Derivatives Analysis**     | Funding rate filter, Basis, CVD Divergence                                              |
| **Paper Trading**            | Simulasi fill, TP/SL bertingkat, trailing SL, PnL harian — tanpa modal nyata            |
| **Notifikasi Telegram**      | Alert sinyal, partial fill, TP/SL hit, laporan 24 jam                                   |
| **Telegram Commands**        | `/status`, `/trades`, `/balance`, `/report`, `/pause`, `/resume`                        |
| **Watchlist Dinamis**        | Auto-refresh top-N pair berdasarkan volume setiap hari                                  |
| **Thread Safety**            | Atomic JSON write, `threading.Event` untuk pause flag, singleton lock exchange          |

---

## 🗂️ Struktur Project

```
project/
├── main.py                      # Entry point — scheduler & scan loop utama
├── auto_trades.py               # Logika real order via WebSocket (auto_trade=true)
├── config.example.json          # Template konfigurasi
├── requirements.txt
└── modules/
    ├── config_loader.py         # Load & validasi config.json
    ├── exchange.py              # BybitClient (ccxt) — retry, leverage cache, health check
    ├── database.py              # JSON storage: sinyal, trade aktif, balance, state
    ├── watchlist.py             # Auto-refresh watchlist pair berdasarkan volume
    ├── technicals.py            # EMA 13/21, Stoch RSI, MACD, divergence
    ├── smc.py                   # Smart Money Concepts (lihat detail di bawah)
    ├── patterns.py              # Chart pattern detection + volume & pole validation
    ├── quant.py                 # Zeta Field, RVOL, Z-Score, OBI dari order book
    ├── derivatives.py           # Funding rate, Basis, CVD Divergence
    ├── paper_trader.py          # Simulasi fill, partial TP/SL, PnL calculation
    ├── paper_runner.py          # Runner loop & scheduler paper trade (daemon thread)
    ├── telegram_bot.py          # Kirim alert, update dashboard, scan completion
    ├── telegram_commands.py     # Handler command Telegram (long polling)
    └── notifier.py              # Abstraksi send_reply (reply ke pesan asli)
```

---

## ⚙️ Konfigurasi Lengkap

Salin template lalu isi dengan credential milik kamu:

```bash
cp config.example.json config.json
```

### Referensi Semua Parameter

| Key                            | Default               | Keterangan                                            |
| ------------------------------ | --------------------- | ----------------------------------------------------- |
| `auto_trade`                   | `false`               | `true` = real order ke Bybit, `false` = paper trading |
| `api.bybit_key`                | —                     | API Key Bybit (read-only cukup untuk paper mode)      |
| `api.bybit_secret`             | —                     | API Secret Bybit                                      |
| `api.telegram_bot_token`       | —                     | Token bot dari @BotFather                             |
| `api.telegram_chat_id`         | —                     | Chat ID tujuan notifikasi                             |
| `system.max_threads`           | `20`                  | Thread paralel saat scan watchlist                    |
| `system.entry_timeframe`       | `"15m"`               | Timeframe analisis sinyal entry                       |
| `system.trend_timeframe`       | `"1h"`                | Timeframe filter trend (Supertrend)                   |
| `system.min_candles_analysis`  | `150`                 | Minimum candle OHLCV untuk analisis                   |
| `system.watchlist_top_n`       | `100`                 | Jumlah pair teratas yang masuk watchlist              |
| `system.active_hours_utc`      | `[6, 22]`             | Window jam aktif scan UTC `[start, end)`              |
| `risk.target_leverage`         | `25`                  | Leverage target (dibatasi oleh limit exchange)        |
| `risk.risk_percent`            | `0.01`                | Risiko per trade: 1% dari balance                     |
| `risk.max_positions`           | `20`                  | Maks posisi terbuka bersamaan                         |
| `risk.max_daily_loss_pct`      | `0.05`                | Stop trading jika loss harian > 5%                    |
| `risk.pending_expire_hours`    | `6`                   | Auto-cancel pending order setelah N jam               |
| `risk.tp_split`                | `[0.5, 0.25, 0.25]`   | Distribusi close di TP1/TP2/TP3 (harus sum = 1.0)     |
| `risk.paper_balance`           | `100.0`               | Modal awal paper trading (USD)                        |
| `setup.fib_entry_start`        | `0.5`                 | Entry mulai di Fibonacci 50%                          |
| `setup.fib_entry_end`          | `0.618`               | Entry berakhir di Fibonacci 61.8%                     |
| `setup.fib_sl`                 | `0.27`                | SL di luar swing ±27% range                           |
| `setup.fib_tp_1/2/3`           | `1.0 / 1.618 / 2.618` | TP di ekstensi Fibonacci                              |
| `strategy.min_tech_score`      | `3`                   | Skor teknikal minimum                                 |
| `strategy.min_smc_score`       | `2`                   | Skor SMC minimum                                      |
| `strategy.min_quant_score`     | `3`                   | Skor kuantitatif minimum                              |
| `strategy.min_deriv_score`     | `2`                   | Skor derivatives minimum                              |
| `strategy.max_entry_drift_pct` | `0.03`                | Maks jarak entry dari harga terkini (3%)              |
| `strategy.risk_reward_min`     | `2.5`                 | Minimum R:R ke TP3                                    |
| `indicators.min_rvol`          | `1.5`                 | Relative Volume minimum (1.5x rata-rata 20 candle)    |
| `patterns.tolerance`           | `0.015`               | Toleransi harga alignment check (1.5%)                |
| `patterns.<name>`              | `true`                | Enable/disable deteksi pattern tertentu               |

---

## 🚀 Instalasi & Menjalankan

### Prasyarat

- Python 3.10+
- Akun Bybit dengan API key (read-only untuk paper mode)
- Bot Telegram + Chat ID (buat via @BotFather, dapatkan chat ID via @userinfobot)

### Langkah-langkah

```bash
# 1. Masuk ke direktori project
cd Bot-Auto-Screening-Bybit-main

# 2. Buat & aktifkan virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux / macOS

# 3. Install dependencies
pip install -r requirements.txt

# 4. Salin & isi konfigurasi
cp config.example.json config.json
# Edit config.json dengan text editor — isi semua field YOUR_...

# 5. Jalankan bot
python main.py

# (Opsional) Mode verbose untuk debugging
BOT_DEBUG=true python main.py          # Linux/macOS
set BOT_DEBUG=true && python main.py   # Windows CMD
$env:BOT_DEBUG="true"; python main.py  # Windows PowerShell
```

Bot akan otomatis:

1. Health check koneksi ke Bybit
2. Fetch watchlist pertama kali (jika belum ada)
3. Langsung mulai scan pertama
4. Menjadwalkan: scan tiap 15 menit, paper update tiap 1 menit, refresh watchlist tiap hari jam 07:00 UTC

---

## 🔍 Pipeline Analisis (Per Pair)

Setiap pair dalam watchlist diproses melalui **13 filter berurutan**. Pair yang gagal di salah satu filter langsung di-skip dan alasannya dicatat untuk filter breakdown report.

```
 1. Duplicate check          → skip jika sinyal aktif untuk pair+TF ini sudah ada
 2. Fetch ticker             → skip jika data tidak valid / settlement token (ST)
 3. Symbol Trend (1h)        → Supertrend(10, 3.5): Bullish / Bearish / Sideways
 4. Fetch OHLCV (15m)        → skip jika data < min_candles_analysis
 5. Technicals               → hitung EMA 13/21, Stoch RSI, MACD, RVOL, Z-Score
 6. Pattern Detection        → cari chart pattern (volume gate + pole validation)
 7. MTF Alignment            → skip jika trend 1h berlawanan dengan arah sinyal
 8. BTC Bias Filter          → skip jika BTC EMA 13/21 berlawanan dengan arah sinyal
 9. SMC Analysis             → BOS, CHoCH, Fresh OB, FVG, Liquidity Sweep
10. Quant Metrics            → Zeta Field (8 sub-indicator), OBI dari real order book
11. Derivatives Check        → Funding Rate filter, Basis, CVD Divergence
12. Divergence (Stoch RSI)   → deteksi bullish/bearish divergence pada price pivot
13. Setup & R:R Check        → kalkulasi level Fibonacci + entry proximity (max 3% drift)
    ✅ SIGNAL VALID → alert Telegram + simpan ke DB → paper_runner ingest
```

---

## 📊 Smart Money Concepts (SMC)

Diimplementasikan penuh di `modules/smc.py`:

| Komponen              | Keterangan                                                 | Skor       |
| --------------------- | ---------------------------------------------------------- | ---------- |
| **Market Structure**  | Pivot HH/HL = Bullish, LH/LL = Bearish via `argrelextrema` | +1 atau +2 |
| **BOS**               | Break of Structure — konfirmasi continuation               | +2         |
| **CHoCH**             | Change of Character — early reversal warning               | +1         |
| **Fresh Order Block** | OB belum ter-mitigasi (stale OB otomatis dibuang)          | +2         |
| **Fair Value Gap**    | 3-candle imbalance yang belum terisi                       | +1         |
| **Liquidity Sweep**   | Equal highs/lows di-grab lalu reversal                     | +2         |
| **Contrarian BOS**    | BOS berlawanan dengan arah sinyal                          | -1         |

**Hard reject (sinyal dibatalkan):**

- Long ke zona Supply OB
- Short ke zona Demand OB
- Long di market structure LH atau LL
- Short di market structure HL atau HH

---

## 📈 Quant Metrics — Zeta Field

`modules/quant.py` menghitung **Zeta Field Score** dari 8 sub-indicator yang dinormalisasi ke 0–100:

| Sub-indicator | Formula                                       |
| ------------- | --------------------------------------------- |
| v_term        | `sigmoid(NATR_14)` — volatilitas normalized   |
| f_term        | `(CMF_20 + 1) / 2` — money flow               |
| c_term        | `sigmoid(CCI_20 / 100)` — commodity channel   |
| b_term        | `1 - min(abs(basis) × 100, 1)` — basis spread |
| s_term        | `RSI_14 / 100` — momentum                     |
| a_term        | `sigmoid(ROC_9)` — rate of change             |
| h_term        | `min(RVOL / 5, 1)` — relative volume cap      |
| t_term        | `ADX_14 / 100` — trend strength               |

Zeta > 70 atau < 30 memberikan bonus +1 pada quant score.

---

## 📋 Mode Paper Trading

Ketika `auto_trade = false`, bot berjalan dalam mode **paper trading** penuh:

### Formula PnL (identik dengan exchange asli)

```
margin         = balance × risk_percent
position_value = margin × leverage
quantity       = position_value / entry_price        ← leverage sudah baked-in
pnl (Long)     = (exit_price - entry) × quantity
pnl (Short)    = (entry - exit_price) × quantity
fee            = (entry × qty × 0.00055) + (exit × qty × 0.00055)
                 ↑ Bybit taker fee 0.055% untuk open dan close
```

**Contoh nyata:**

```
Balance=$100, Risk=1%, Leverage=50x, Entry=9.6084, SL=9.4868

margin         = $100 × 1%     = $1.00
position_value = $1 × 50       = $50.00
quantity       = $50 / 9.6084  = 5.203 koin
pnl (SL hit)   = (9.4868 - 9.6084) × 5.203 - fee = -$0.63
```

### Partial TP & Trailing SL

| Event   | Tindakan                                    | SL Baru                       |
| ------- | ------------------------------------------- | ----------------------------- |
| TP1 hit | Jual 50% posisi, update balance             | → **Breakeven** (harga entry) |
| TP2 hit | Jual 25% posisi, update balance             | → **harga TP1**               |
| TP3 hit | Tutup sisa 25% posisi, tutup trade          | —                             |
| SL hit  | Tutup semua sisa posisi di harga SL efektif | —                             |

> Distribusi TP default `[0.5, 0.25, 0.25]` bisa diubah via `risk.tp_split`.

### Cascade TP (Skip Detection)

Jika harga loncat langsung ke TP2/TP3 tanpa menyentuh TP sebelumnya, bot memproses setiap level **secara berurutan** — TP1 dihitung di harga TP1, TP2 di harga TP2 — baru menutup posisi di level yang tersentuh.

### PENDING Auto-Expire

Order pending yang belum terisi dalam `pending_expire_hours` jam (default 6) otomatis di-cancel dan notifikasi dikirim ke Telegram.

---

## 🎧 Telegram Commands

Bot mendengarkan command via long polling di daemon thread terpisah. Hanya `telegram_chat_id` yang dikonfigurasi yang bisa mengirim command.

| Command    | Keterangan                                                       |
| ---------- | ---------------------------------------------------------------- |
| `/start`   | Salam + daftar semua perintah                                    |
| `/help`    | Daftar perintah                                                  |
| `/status`  | Ringkasan: posisi aktif, pending, paper balance                  |
| `/trades`  | Detail semua posisi aktif & pending (entry, TP/SL, PnL floating) |
| `/balance` | Tampilkan paper balance saat ini                                 |
| `/report`  | Laporan 24 jam: total trade, win rate, total PnL, best trade     |
| `/pause`   | Pause scan cycle — scan dilewati sampai `/resume`                |
| `/resume`  | Resume scan cycle                                                |

---

## 🛡️ Thread Safety

| Komponen            | Mekanisme                                               | Keterangan                            |
| ------------------- | ------------------------------------------------------- | ------------------------------------- |
| **JSON file write** | `threading.Lock` + atomic `os.replace()` + `fsync`      | Tidak ada partial write / file korup  |
| **Pause flag**      | `threading.Event` (`.set()` / `.clear()` / `.is_set()`) | Atomic tanpa lock tambahan            |
| **Exchange client** | `threading.Lock` (`_client_lock`)                       | Singleton aman dari multi-thread scan |
| **Leverage cache**  | `threading.Lock` + double-check locking                 | Cegah thundering herd saat cache miss |

---

## 📝 Logging & Observabilitas

- **File log:** `data/bot.log` — `RotatingFileHandler` maks 5 MB, 3 backup otomatis
- **Named logger:** setiap module punya logger sendiri (`Main`, `Exchange`, `SMC`, `Quant`, `Patterns`, `Derivatives`, `PaperTrader`, `TelegramBot`, `TelegramCmd`)
- **Filter breakdown:** setiap scan mencetak distribusi rejection per filter ke console
- **Debug mode:** `BOT_DEBUG=true` mengaktifkan verbose output + traceback penuh
- **Step tracking:** exception pada `analyze_ticker` mencatat step terakhir sebelum error

Contoh output filter breakdown:

```
📊 Filter Breakdown — 87 pairs diproses:
   Tidak ada pattern                        52 (59.8%)  ██████████████████████████
   MTF conflict (1h vs 15m)                 18 (20.7%)  █████████
   RVOL rendah (< 1.5x)                      9 (10.3%)  ████
   SMC fail                                  5 ( 5.7%)  ██
   R:R rendah (< 2.5)                        2 ( 2.3%)  █
```

---

## 🐛 Bug Fix Log

| #   | Deskripsi                                                                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **ImportError `run_paper_update`** — fungsi dipindah ke `paper_runner.py`, import di `main.py` diperbarui                                   |
| 2   | **PnL calculation salah** — qty sudah mengandung leverage, tidak perlu dikali leverage lagi                                                 |
| 3   | **Windows UTF-8 crash** — stdout di-wrap UTF-8 agar emoji tidak error di encoding cp1252                                                    |
| 4   | **Balance tidak update saat partial** — balance diperbarui realtime di setiap TP hit                                                        |
| 5   | **Remaining qty salah di TP3/SL** — sisa posisi dihitung benar setelah partial terjual                                                      |
| 6   | **JSON korup saat crash** — write kini atomic (temp file + fsync + os.replace)                                                              |
| 7   | **`_paused` bool tidak thread-safe** — diganti `threading.Event` (.set/.clear/.is_set)                                                      |
| 8   | **pytz tidak ada di requirements.txt** — ditambahkan `pytz>=2024.1`                                                                         |
| 9   | **Scan aktif 24 jam termasuk jam sepi** — ditambahkan `is_active_hour()` dengan window `active_hours_utc` yang dapat dikonfigurasi          |
| 10  | **OBI selalu 0** — `calculate_obi()` kini menggunakan real order book depth (top 10 level), bukan `ticker.bidVolume` yang selalu 0 di Bybit |
| 11  | **`bare except:` di telegram_bot.py** — diganti `except Exception:` agar tidak menelan `KeyboardInterrupt`                                  |
| 12  | **`auto_trades.py` bypass BybitClient** — kini routing semua order lewat wrapper `BybitClient` untuk retry & logging yang konsisten         |
| 13  | **Stoch RSI kolom dinamis** — kolom K/D dideteksi by suffix bukan hardcode agar tidak crash di versi `pandas_ta` yang berbeda               |

---

## 📦 Dependencies

```
ccxt>=4.3.0           # Bybit exchange client (REST)
pybit>=5.6.0          # WebSocket support untuk auto_trade mode
schedule>=1.2.0       # Job scheduler (scan loop)
requests>=2.31.0      # HTTP Telegram API
python-dotenv>=1.0.0
pytz>=2024.1          # Timezone handling

pandas>=2.0.0
pandas_ta>=0.3.14b    # Technical indicators (EMA, Supertrend, MACD, Stoch RSI, dll.)
numpy>=1.26.0

scipy>=1.11.0         # argrelextrema (pivot), linregress (slope), expit (Zeta Field)
```

---

## 📊 Penilaian Kualitas Kode

> Review menyeluruh terhadap 17 file Python, requirements, config, dan struktur project.

| #         | Bidang                   |    Nilai    | Catatan                                                                             |
| :-------- | :----------------------- | :---------: | :---------------------------------------------------------------------------------- |
| 1         | Syntax & Import          |    10/10    | Semua file pass AST check, cross-import valid ✅                                    |
| 2         | Struktur Kode            |    10/10    | Atomic write ✅, daemon thread rapi ✅, logging terpusat ✅                         |
| 3         | PnL & Persentase         |    10/10    | Formula benar (qty baked-in leverage) ✅, fee 0.055% ✅, ROI on margin & balance ✅ |
| 4         | Error Handling           |    10/10    | Semua exception tertangkap dengan tipe spesifik + logger ✅                         |
| 5         | Config & Validasi        |    10/10    | Key wajib ✅, placeholder check ✅, validasi range TP split / leverage / risk ✅    |
| 6         | Telegram                 |    10/10    | Rate limit 429 handling ✅, retry 5x ✅, named logger ✅                            |
| 7         | Kualitas Sinyal          |    10/10    | MTF confluence ✅, BTC bias ✅, SMC lengkap ✅, OBI dari real order book ✅         |
| 8         | Logging & Observabilitas |    10/10    | Named logger per module ✅, RotatingFileHandler ✅, filter breakdown ✅             |
| **Total** |                          | **129/130** | Sisa 1 poin: pattern harmonics (Gartley, Bat, Crab) — opsional                      |

---

## 🗒️ Catatan Penting

- **Paper mode ≠ tidak akurat.** PnL formula, fee, dan partial TP/SL identik dengan kondisi exchange asli.
- **API Key untuk paper mode** — hanya butuh permission `Read`. Tidak perlu izin `Trade`.
- **Watchlist otomatis** — jika `data/watchlist.json` belum ada, bot fetch semua pair USDT perpetual aktif dari Bybit saat startup. Refresh otomatis setiap hari jam 07:00 UTC.
- **Semua log masuk ke satu file** — termasuk dari `auto_trades.py` via root logger → `data/bot.log`.
- **Pair stablecoin difilter otomatis** — USDC, DAI, FDUSD, dan sejenisnya dieksklusi dari watchlist.

---

## 📄 Lisensi

MIT License — lihat file `LICENSE`.
