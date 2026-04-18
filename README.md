# 🤖 Bybit Auto Screening Bot v8

Bot trading otomatis untuk Bybit dengan fitur **Paper Trade** dan **Real Trade**.
Menggunakan **Multi-Timeframe Analysis** — sinyal entry di **15m**, dikonfirmasi oleh tren **1h**.

> **v8 Update** — Paper Trade kini berjalan otomatis di background **hanya dengan 1 terminal**.
> Tidak perlu lagi menjalankan `auto_trades.py` secara terpisah.

---

## 📐 Sistem Multi-Timeframe (MTF)

Bot menggunakan dua timeframe secara bersamaan:

| Timeframe          | Peran           | Keterangan                                                 |
| ------------------ | --------------- | ---------------------------------------------------------- |
| **1h** (Trend TF)  | Konfirmasi tren | EMA 13/21 menentukan arah pasar yang dominan               |
| **15m** (Entry TF) | Sinyal entry    | Pattern teknikal, SMC, Quant, dan Deriv dianalisis di sini |

### Aturan MTF:

- **Long hanya** diambil jika tren 1h **Bullish** (atau Sideways) **dan** pattern 15m adalah Long.
- **Short hanya** diambil jika tren 1h **Bearish** (atau Sideways) **dan** pattern 15m adalah Short.
- Jika tren 1h berlawanan dengan sinyal 15m → sinyal **diabaikan otomatis**.

### Alur Analisis per Symbol:

```
Symbol → Cek Duplikat
       → Konfirmasi Tren 1h (EMA 13/21)
       → Analisis Pattern 15m (Technicals, SMC, Quant, Deriv)
       → Filter MTF Alignment
       → Filter BTC Global Bias
       → Hitung Setup (Fib Entry, SL, TP1/2/3, R:R)
       → Simpan ke DB → Paper Runner ingest → Simulasi trade
```

---

## ⚡ Toggle Mode

Ubah satu baris di `config.json`:

```json
"auto_trade": false   ← Paper Trade (simulasi, aman) — 1 terminal cukup
"auto_trade": true    ← Real Trade (order nyata ke Bybit)
```

---

## 🚀 Cara Menjalankan

Pastikan venv sudah aktif `(venv)` sebelum menjalankan bot.

### Paper Trade & Signal — cukup 1 terminal:

```bash
python main.py
```

Ketika `auto_trade: false`, Paper Trade Runner otomatis berjalan di background thread.
Tidak perlu terminal kedua. Output startup akan menampilkan:

```
📋 Paper Trade Runner — AUTO START
   (auto_trade=false → paper trader berjalan di background thread)

🟢 PaperRunner thread started
   Balance awal  : $1000.00
   Risk/trade    : 1.0%
   Max positions : 40
   Leverage      : MAX per coin (dari Bybit) 🔝
```

### Real Trade — juga cukup 1 terminal:

```bash
python main.py
```

Untuk real trade, `auto_trades.py` tetap tersedia sebagai runner terpisah jika dibutuhkan.

---

## 🛠️ Instalasi

### 1. Clone repo

```bash
git clone https://github.com/arihazamie/Bot-Auto-Screening-Bybit
cd Bot-Auto-Screening-Bybit
```

### 2. Buat Virtual Environment (venv)

**Linux / macOS:**

```bash
python3 -m venv venv
source venv/bin/activate
```

**Windows (PowerShell):**

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

**Windows (Command Prompt):**

```cmd
python -m venv venv
venv\Scripts\activate.bat
```

> Kalau berhasil, terminal akan menampilkan `(venv)` di awal baris.

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Buat config.json

```bash
cp config.example.json config.json
```

Edit `config.json` dan isi:

- `bybit_key` & `bybit_secret` — dari Bybit API Management
- `auto_trade` — `false` untuk paper trade, `true` untuk real trade
- `use_max_leverage` — `true` untuk leverage maksimal per coin, `false` untuk fixed

---

## ⚙️ Konfigurasi Lengkap

| Key                           | Default  | Keterangan                                            |
| ----------------------------- | -------- | ----------------------------------------------------- |
| `auto_trade`                  | `false`  | Toggle real/paper                                     |
| `system.entry_timeframe`      | `"15m"`  | Timeframe untuk sinyal entry                          |
| `system.trend_timeframe`      | `"1h"`   | Timeframe untuk konfirmasi tren                       |
| `system.min_candles_analysis` | `150`    | Minimum candle untuk analisis                         |
| `system.check_interval_hours` | `1`      | Interval scan dalam jam                               |
| `risk.use_max_leverage`       | `false`  | `true` = max lev per coin dari Bybit, `false` = fixed |
| `risk.target_leverage`        | `25`     | Leverage fixed / fallback jika use_max_leverage gagal |
| `risk.risk_percent`           | `0.01`   | Risiko per trade (1% dari balance)                    |
| `risk.max_positions`          | `40`     | Maks posisi bersamaan                                 |
| `risk.paper_balance`          | `1000.0` | Modal awal paper trade (USD)                          |
| `strategy.risk_reward_min`    | `3.0`    | Minimum R:R untuk membuka posisi                      |
| `indicators.min_rvol`         | `1.5`    | Minimum Relative Volume                               |

### Contoh `config.json` (bagian risk):

```json
"risk": {
    "use_max_leverage": true,
    "target_leverage": 25,
    "risk_percent": 0.01,
    "max_positions": 40,
    "tp_split": [0.3, 0.3, 0.4],
    "paper_balance": 1000.0
}
```

---

## 📋 Mode Paper Trade

Paper Trade Runner berjalan sebagai **daemon thread** di dalam `main.py`.
Semua state tersimpan di file JSON — aman jika bot dimatikan dan dinyalakan kembali.

### Jadwal internal Paper Runner:

| Interval              | Fungsi                                          |
| --------------------- | ----------------------------------------------- |
| Setiap 5 detik        | Cek pending trade → simulasi fill entry         |
| Setiap 10 detik       | Monitor trade OPEN → cek apakah TP atau SL kena |
| Setiap 60 detik       | Ingest sinyal baru dari database                |
| Setiap hari **07:00** | Kirim daily report PnL ke Telegram              |

### Fitur Paper Trade:

- Tidak ada order nyata yang dikirim ke Bybit
- Simulasi fill order berdasarkan harga live dari Bybit
- **Breakeven otomatis** setelah TP1 tercapai (SL dipindah ke entry)
- SL & TP1 / TP2 / TP3 dipantau secara real-time
- PnL & balance tersimpan di `data/paper_state.json`
- Jika bot di-restart, posisi yang sedang open **dilanjutkan otomatis**
- Membutuhkan Bybit API key yang valid (untuk mengambil harga live)

### Leverage di Paper Mode:

```json
"use_max_leverage": true    → tiap coin pakai leverage maksimal dari Bybit
                              (BTC=100x, GALA=50x, DOGE=75x, dll)

"use_max_leverage": false   → semua coin pakai target_leverage (misal 25x)
```

> Market info dari Bybit di-cache saat startup, tidak ada overhead fetch berulang.

---

## 💰 Mode Real Trade

- Order dikirim langsung ke Bybit via CCXT
- WebSocket memantau fill & perubahan posisi secara real-time
- Split TP otomatis (30% / 30% / 40%)
- SL dipindah ke entry (breakeven) setelah TP1 tercapai
- Safety net untuk mendeteksi missed TP placement
- Butuh API key dengan permission **Derivatives Read + Trade**

---

## 📡 Notifikasi Telegram

Bot mengirim notifikasi otomatis ke Telegram untuk:

| Event              | Keterangan                                              |
| ------------------ | ------------------------------------------------------- |
| **Signal baru**    | Entry, SL, TP1/2/3, Score, Funding, OBI, R:R            |
| **Scan selesai**   | Jumlah sinyal, durasi scan, BTC bias                    |
| **Trade closed**   | Symbol, alasan (TP/SL), PnL                             |
| **Daily report**   | Setiap jam **07:00** — win rate, total PnL, saldo paper |
| **Live dashboard** | Di-update setiap 1 menit, mengedit 1 pesan yang sama    |

> Bot Telegram saat ini hanya mengirim pesan (push only). Perintah interaktif seperti `/status` atau `/balance` belum tersedia.

---

## 📂 Struktur File

```
Bot-Auto-Screening-Bybit/
├── main.py                 ← Entry point utama (screener + paper runner)
├── auto_trades.py          ← Real trade engine (opsional, untuk real mode)
├── config.json             ← Konfigurasi kamu
├── config.example.json     ← Template konfigurasi
├── requirements.txt
└── modules/
    ├── __init__.py         ← File kosong (wajib ada)
    ├── config_loader.py    ← Load config.json
    ├── database.py         ← Penyimpanan data lokal (JSON file di /data)
    ├── exchange.py         ← BybitClient wrapper (CCXT)
    ├── technicals.py       ← EMA, StochRSI, MACD, divergence
    ├── smc.py              ← Smart Money Concepts (OB, Structure)
    ├── quant.py            ← Quant metrics (Z-Score, Zeta, OBI, RVOL)
    ├── derivatives.py      ← Funding rate, Basis, CVD divergence
    ├── patterns.py         ← Pattern detection (Double Top/Bottom, Flags, dll)
    ├── paper_trader.py     ← Logika simulasi fill, TP/SL, PnL
    ├── paper_runner.py     ← Background thread paper trade (auto dari main.py)
    ├── watchlist.py        ← Manajemen watchlist 100 pair teratas
    ├── notifier.py         ← Notifikasi Telegram (trade closed, alerts)
    └── telegram_bot.py     ← Telegram send functions (signal, dashboard, scan)
```

### Folder `data/` (dibuat otomatis):

```
data/
├── signals.json        ← Antrian sinyal dari screener
├── active_trades.json  ← Posisi paper trade (PENDING / OPEN / CLOSED)
├── paper_state.json    ← Saldo paper trade saat ini
├── sent_trades.json    ← Sinyal yang sudah dikirim ke Telegram
├── daily_reports.json  ← Histori laporan harian
├── state.json          ← State umum (dashboard message ID, dll)
└── bot.log             ← Log lengkap bot
```

> Semua data di folder `data/` **persisten** — aman jika bot dimatikan dan dinyalakan kembali.

---

## 🔄 Behaviour saat Bot Direstart

Karena semua state disimpan di file JSON, bot **melanjutkan dari posisi terakhir** setelah restart:

| Kondisi sebelum restart    | Setelah restart                                 |
| -------------------------- | ----------------------------------------------- |
| Trade PENDING (belum fill) | Tetap dipantau, fill saat harga menyentuh entry |
| Trade OPEN (sudah fill)    | Tetap dipantau, TP/SL tetap aktif               |
| Saldo paper $1250          | Tetap $1250, tidak reset                        |
| Sinyal belum diingest      | Tetap diproses saat runner jalan                |

---

## 🚀 Deployment di Linux (Systemd)

Buat file service:

```ini
# /etc/systemd/system/bybit_bot.service
[Unit]
Description=Bybit Screening Bot v8
After=network.target

[Service]
WorkingDirectory=/path/to/Bot-Auto-Screening-Bybit
ExecStart=/path/to/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Aktifkan:

```bash
sudo systemctl daemon-reload
sudo systemctl enable bybit_bot
sudo systemctl start bybit_bot
sudo systemctl status bybit_bot
```

Cek log:

```bash
tail -f data/bot.log
# atau
sudo journalctl -u bybit_bot -f
```

---

## 👤 Credit

Bot ini dikembangkan berdasarkan repo original milik [@Kurumichan987](https://x.com/Kurumichan987).
Terima kasih atas fondasi yang sudah dibuat — go follow & support beliau di X (Twitter)!

---
