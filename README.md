# 🤖 Bot Auto Screening Bybit

Bot trading otomatis untuk Bybit yang melakukan screening sinyal, paper trading, dan notifikasi via Telegram.

---

## ✨ Fitur

- **Auto Screening** — scan pasar secara berkala berdasarkan watchlist
- **Paper Trading** — simulasi trading tanpa modal nyata (TP, SL, breakeven otomatis)
- **Notifikasi Telegram** — alert sinyal, fill entry, TP hit, SL hit, laporan harian
- **Teknical Analysis** — RSI, divergence, SMC, pattern detection, quant metrics
- **Derivatives Data** — analisis open interest & funding rate
- **Watchlist Dinamis** — auto-refresh daftar pair setiap hari

---

## 🗂️ Struktur Project

```
project/
├── main.py                        # Entry point utama
├── auto_trades.py                 # Logika auto trade (real order)
├── config.example.json            # Contoh konfigurasi
├── requirements.txt
└── modules/
    ├── paper_trader.py            # Simulasi fill, TP/SL, PnL calculation
    ├── paper_runner.py            # Runner loop & scheduler paper trade
    ├── exchange.py                # Client Bybit (ccxt)
    ├── database.py                # SQLite: sinyal, trade aktif, balance
    ├── telegram_bot.py            # Kirim alert & notifikasi
    ├── telegram_commands.py       # Handler command Telegram (/status, dll)
    ├── watchlist.py               # Kelola watchlist pair
    ├── technicals.py              # Indikator teknikal (RSI, divergence)
    ├── smc.py                     # Smart Money Concepts
    ├── patterns.py                # Deteksi pola candlestick
    ├── quant.py                   # Metrics kuantitatif & fakeout check
    ├── derivatives.py             # Open interest & funding rate
    ├── notifier.py                # Abstraksi pengiriman pesan
    └── config_loader.py           # Load & validasi config.json
```

---

## ⚙️ Konfigurasi

Salin `config.example.json` ke `config.json` lalu isi:

```bash
cp config.example.json config.json
```

| Key                           | Keterangan                                |
| ----------------------------- | ----------------------------------------- |
| `bybit.api_key`               | API key Bybit                             |
| `bybit.api_secret`            | API secret Bybit                          |
| `telegram.bot_token`          | Token bot Telegram                        |
| `telegram.chat_id`            | Chat ID tujuan notifikasi                 |
| `system.auto_trade`           | `true` = real order, `false` = paper mode |
| `system.check_interval_hours` | Interval scan (jam)                       |
| `paper.initial_balance`       | Modal awal paper trading (USD)            |
| `paper.risk_pct`              | Risiko per trade (%)                      |

---

## 🚀 Instalasi & Menjalankan

```bash
# 1. Buat & aktifkan virtual environment
python -m venv venv
venv\Scripts\activate          # Windows
source venv/bin/activate       # Linux/Mac

# 2. Install dependencies
pip install -r requirements.txt

# 3. Salin & isi konfigurasi
cp config.example.json config.json

# 4. Jalankan bot
python main.py
```

---

## 📋 Mode Paper Trading

Ketika `auto_trade = false`, bot berjalan dalam mode **paper trading**:

- Sinyal tetap di-generate dan disimpan ke database
- `paper_runner` berjalan sebagai background thread (daemon)
- Scheduler memanggil `run_paper_update()` setiap 1 menit untuk siklus: ingest sinyal → execute pending → monitor open trades
- PnL dihitung dengan formula exchange asli, fee taker 0.055% sudah diperhitungkan

### Formula PnL

```
margin         = balance × risk%
position_value = margin × leverage
quantity       = position_value / entry_price
pnl (Long)     = (exit - entry) × quantity - fee
pnl (Short)    = (entry - exit) × quantity - fee
```

---

## 🐛 Bug Fix

| #   | Deskripsi                                                                                                 |
| --- | --------------------------------------------------------------------------------------------------------- |
| 1   | **ImportError `run_paper_update`** — fungsi dipindah ke `paper_runner.py`, import di `main.py` diperbarui |
| 2   | **PnL calculation salah** — qty sudah mengandung leverage, tidak perlu dikali leverage lagi               |
| 3   | **Windows UTF-8 crash** — stdout di-wrap UTF-8 agar emoji tidak error di cp1252                           |

---

## 📄 Lisensi

MIT License — lihat file `LICENSE`.
