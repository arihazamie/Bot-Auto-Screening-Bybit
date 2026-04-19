# 🤖 Bot Auto Screening Bybit

Bot trading otomatis untuk Bybit yang melakukan screening sinyal, paper trading, dan notifikasi via Telegram.

---

## ✨ Fitur

- **Auto Screening** — scan pasar secara berkala berdasarkan watchlist
- **Paper Trading** — simulasi trading tanpa modal nyata (TP, SL, breakeven otomatis)
- **Notifikasi Telegram** — alert sinyal, fill entry, TP hit, SL hit, laporan harian
- **Teknikal Analysis** — RSI, divergence, SMC, pattern detection, quant metrics
- **Derivatives Data** — analisis open interest & funding rate
- **Watchlist Dinamis** — auto-refresh daftar pair setiap hari berdasarkan volume

---

## 🗂️ Struktur Project

```
project/
├── main.py                        # Entry point utama
├── auto_trades.py                 # Logika auto trade (real order via WebSocket)
├── config.example.json            # Contoh konfigurasi
├── requirements.txt
└── modules/
    ├── paper_trader.py            # Simulasi fill, TP/SL, PnL calculation
    ├── paper_runner.py            # Runner loop & scheduler paper trade
    ├── exchange.py                # Client Bybit (ccxt) + leverage cache TTL
    ├── database.py                # JSON storage: sinyal, trade aktif, balance
    ├── telegram_bot.py            # Kirim alert & notifikasi
    ├── telegram_commands.py       # Handler command Telegram (/status, dll)
    ├── watchlist.py               # Kelola watchlist pair by volume
    ├── technicals.py              # Indikator teknikal (RSI, divergence)
    ├── smc.py                     # Smart Money Concepts (OB, market structure)
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

| Key                      | Keterangan                                |
| ------------------------ | ----------------------------------------- |
| `api.bybit_key`          | API key Bybit                             |
| `api.bybit_secret`       | API secret Bybit                          |
| `api.telegram_bot_token` | Token bot Telegram                        |
| `api.telegram_chat_id`   | Chat ID tujuan notifikasi                 |
| `auto_trade`             | `true` = real order, `false` = paper mode |
| `system.entry_timeframe` | Timeframe entry (default: `15m`)          |
| `system.trend_timeframe` | Timeframe trend (default: `1h`)           |
| `system.max_threads`     | Jumlah thread paralel saat scan           |
| `system.watchlist_top_n` | Jumlah pair teratas di watchlist          |
| `risk.paper_balance`     | Modal awal paper trading (USD)            |
| `risk.risk_pct`          | Risiko per trade (%)                      |

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

# 5. (Opsional) Mode verbose
BOT_DEBUG=true python main.py
```

---

## 📋 Mode Paper Trading

Ketika `auto_trade = false`, bot berjalan dalam mode **paper trading**:

- Sinyal tetap di-generate dan disimpan ke storage JSON
- `paper_runner` berjalan sebagai background daemon thread
- Scheduler memanggil `run_paper_update()` setiap 1 menit: ingest sinyal → execute pending → monitor open trades
- PnL dihitung dengan formula exchange asli, fee taker 0.055% sudah diperhitungkan

### Formula PnL

```
margin         = balance × risk%
position_value = margin × leverage
quantity       = position_value / entry_price
pnl (Long)     = (exit - entry) × quantity - fee
pnl (Short)    = (entry - exit) × quantity - fee
```

### Partial TP & SL Trailing

| Event   | Aksi                                     | Balance |
| ------- | ---------------------------------------- | ------- |
| TP1 hit | Jual 30%, SL pindah → **Breakeven**      | +update |
| TP2 hit | Jual 30%, SL pindah → **harga TP1**      | +update |
| TP3 hit | Tutup sisa 40%                           | +update |
| SL hit  | Tutup sisa posisi di SL efektif saat itu | +update |

### Cascade TP (Skip Detection)

Jika harga loncat langsung ke TP2/TP3 tanpa menyentuh TP sebelumnya, bot memproses setiap level secara berurutan — TP1 partial dihitung di harga TP1, TP2 partial di harga TP2 — baru menutup posisi di level yang tersentuh.

### PENDING Auto-Expire

Order yang belum terisi dalam **24 jam** otomatis di-cancel dan notifikasi dikirim ke Telegram.

---

## 🛡️ Thread Safety

| Komponen        | Mekanisme                                | Keterangan                                                       |
| --------------- | ---------------------------------------- | ---------------------------------------------------------------- |
| JSON file write | `threading.Lock` + atomic `os.replace()` | Satu writer, tidak ada parsial write                             |
| Pause flag      | `threading.Event`                        | `.set()` / `.clear()` / `.is_set()` — atomic tanpa lock tambahan |
| Exchange client | `threading.Lock` (`_client_lock`)        | Singleton aman dari multi-thread                                 |
| Leverage cache  | `threading.Lock` + double-check          | Cegah thundering herd saat cache miss                            |

---

## 🐛 Bug Fix Log

| #   | Deskripsi                                                                                                 |
| --- | --------------------------------------------------------------------------------------------------------- |
| 1   | **ImportError `run_paper_update`** — fungsi dipindah ke `paper_runner.py`, import di `main.py` diperbarui |
| 2   | **PnL calculation salah** — qty sudah mengandung leverage, tidak perlu dikali leverage lagi               |
| 3   | **Windows UTF-8 crash** — stdout di-wrap UTF-8 agar emoji tidak error di cp1252                           |
| 4   | **Balance tidak update saat partial** — balance diperbarui realtime di setiap TP hit                      |
| 5   | **Remaining qty salah di TP3/SL** — sisa posisi dihitung benar setelah partial terjual                    |
| 6   | **JSON korup saat crash** — write kini atomic (temp file + fsync + os.replace)                            |
| 7   | **`_paused` bool tidak thread-safe** — diganti `threading.Event` (.set/.clear/.is_set)                    |
| 8   | **pytz tidak ada di requirements.txt** — ditambahkan `pytz>=2024.1`                                       |

---

## 📄 Lisensi

MIT License — lihat file `LICENSE`.

---

## 📊 Penilaian Bot

> Review menyeluruh terhadap 17 file Python, requirements, config, dan struktur project.

| #         | Bidang                   |    Nilai    | Catatan                                                                                                                                                                                                                                 |
| :-------- | :----------------------- | :---------: | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1         | Syntax & Import          |    9/10     | Semua 17 file pass AST check; cross-import valid; minor: `auto_trades.py` bypass `BybitClient` wrapper                                                                                                                                  |
| 2         | Struktur Kode            |    9/10     | Atomic write ✅, daemon thread rapi ✅; dikurangi 1: `auto_trades.py` punya `basicConfig` sendiri, tidak terintegrasi ke `bot.log`                                                                                                      |
| 3         | Paper Trade Logic        |    9/10     | Cascade TP ✅, SL trailing bertingkat (BE→TP1) ✅, partial balance realtime ✅, auto-expire 24h ✅                                                                                                                                      |
| 4         | PnL & Persentase         |    9/10     | Formula benar: qty sudah bake-in leverage ✅, fee taker 0.055% open+close ✅, ROI/margin & ROI/balance ✅                                                                                                                               |
| 5         | Max Leverage per Coin    |  **10/10**  | `leverageFilter.maxLeverage` (field Bybit native) ✅, TTL 6 jam ✅, `prefetch_leverage()` warmup ✅, network error tidak di-cache ✅                                                                                                    |
| 6         | Error Handling           |    9/10     | Semua exception tertangkap dengan tipe + logger; satu `except:` tanpa tipe tersisa di `telegram_bot.py:304`                                                                                                                             |
| 7         | Thread Safety            |  **10/10**  | Atomic write (os.replace+fsync) ✅, `threading.Event` untuk pause flag ✅, double-check lock leverage ✅                                                                                                                                |
| 8         | Config & Validasi        |    9/10     | Cek file ada ✅, JSON valid ✅, key wajib & placeholder terdeteksi ✅, error dikirim ke Telegram ✅                                                                                                                                     |
| 9         | Requirements             |  **10/10**  | Semua 9 third-party import tercantum termasuk `pytz>=2024.1` ✅                                                                                                                                                                         |
| 10        | Telegram                 |    9/10     | Rate limit 429 ✅, baca `retry_after` dari response ✅, retry otomatis hingga 5x ✅                                                                                                                                                     |
| 11        | Kualitas Sinyal          |    8/10     | MTF confluence ✅, BTC bias filter ✅, SMC scoring ✅, Quant (Z-score/Zeta/OBI/RVOL) ✅, Derivatives ✅, R:R filter ✅; SMC hanya 2 check (OB + market structure), pattern detection masih basic                                        |
| 12        | Logging & Observabilitas |    7/10     | Named logger per module ✅, `BOT_DEBUG` env var ✅, filter breakdown per scan ✅; **tidak ada `RotatingFileHandler`** — `bot.log` tumbuh tak terbatas; `auto_trades.py` tidak menulis ke `bot.log`                                      |
| 13        | Keamanan API & Config    |    8/10     | API key di `config.json` (tidak hardcode) ✅, `.gitignore` cover `config.json` & `data/` ✅, placeholder validation ✅; `auto_trades.py` load API key tanpa cek `auto_trade` flag; tidak ada validasi `chat_id` untuk Telegram commands |
| **Total** |                          | **116/130** | Semua fix teraplikasi. Ruang perbaikan utama: log rotation, integrasi logging auto_trades, dan kedalaman SMC                                                                                                                            |
