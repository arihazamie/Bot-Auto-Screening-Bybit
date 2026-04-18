# 🤖 Bybit Auto Screening Bot

Bot trading otomatis untuk Bybit dengan fitur **Paper Trade** dan **Real Trade**.
Menggunakan **Multi-Timeframe Analysis** — sinyal entry di **15m**, dikonfirmasi oleh tren **1h**.

---

## 📐 Sistem Multi-Timeframe (MTF)

Bot menggunakan dua timeframe secara bersamaan:

| Timeframe | Peran | Keterangan |
|-----------|-------|------------|
| **1h** (Trend TF) | Konfirmasi tren | EMA 13/21 menentukan arah pasar yang dominan |
| **15m** (Entry TF) | Sinyal entry | Pattern teknikal, SMC, Quant, dan Deriv dianalisis di sini |

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
       → Kirim Sinyal jika lolos semua filter
```

---

## ⚡ Toggle Real / Paper Trade

Ubah satu baris di `config.json`:

```json
"auto_trade": false   ← Paper Trade (simulasi, aman)
"auto_trade": true    ← Real Trade (order nyata ke Bybit)
```

---

## 📂 Struktur File

```
Bot-Auto-Screening-Bybit/
├── auto_trades.py          ← Engine utama (real + paper)
├── main.py                 ← Screener pasar (cari sinyal, MTF logic)
├── config.json             ← Konfigurasi kamu
├── config.example.json     ← Template konfigurasi
├── requirements.txt
├── deploy/
│   ├── bot.service         ← Systemd service file
│   └── restart_bot.sh      ← Script restart
└── modules/
    ├── __init__.py         ← File kosong (wajib ada!)
    ├── config_loader.py    ← Load config.json
    ├── database.py         ← JSON file storage
    ├── technicals.py       ← EMA, StochRSI, MACD, divergence
    ├── smc.py              ← Smart Money Concepts (OB, Structure)
    ├── quant.py            ← Quant metrics (Z-Score, Zeta, OBI)
    ├── derivatives.py      ← Funding rate, Basis, CVD divergence
    ├── patterns.py         ← Pattern detection (Double Top/Bottom, Flags, dll)
    ├── paper_trader.py     ← Simulasi trade
    ├── notifier.py         ← Notifikasi Telegram
    ├── discord_bot.py      ← Notifikasi Discord
    └── telegram_bot.py     ← Telegram bot handler
```

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

---

## 🚀 Cara Menjalankan

Pastikan venv sudah aktif `(venv)` sebelum menjalankan bot.

**Terminal 1 — Screener (pencari sinyal):**

```bash
python main.py
```

**Terminal 2 — Auto Trader:**

```bash
python auto_trades.py
```

> Folder `data/` akan dibuat otomatis saat pertama kali bot dijalankan.

### Nonaktifkan venv

```bash
deactivate
```

---

## ⚙️ Konfigurasi Penting

| Key                         | Default   | Keterangan                               |
| --------------------------- | --------- | ---------------------------------------- |
| `auto_trade`                | `false`   | Toggle real/paper                        |
| `system.entry_timeframe`    | `"15m"`   | Timeframe untuk sinyal entry             |
| `system.trend_timeframe`    | `"1h"`    | Timeframe untuk konfirmasi tren          |
| `system.min_candles_analysis` | `150`   | Minimum candle untuk analisis            |
| `risk.risk_percent`         | `0.01`    | Risiko per trade (1%)                    |
| `risk.target_leverage`      | `25`      | Leverage target                          |
| `risk.max_positions`        | `40`      | Maks posisi bersamaan                    |
| `risk.paper_balance`        | `1000.0`  | Modal awal paper trade                   |
| `strategy.risk_reward_min`  | `3.0`     | Minimum R:R untuk membuka posisi         |

### Contoh `config.json` (bagian system):

```json
"system": {
    "timezone": "Asia/Jakarta",
    "max_threads": 20,
    "check_interval_hours": 1,
    "entry_timeframe": "15m",
    "trend_timeframe": "1h",
    "min_candles_analysis": 150
}
```

---

## 📋 Mode Paper Trade

- Tidak ada order nyata yang dikirim ke Bybit
- Simulasi fill order berdasarkan harga live
- SL & TP dihitung secara virtual
- Breakeven otomatis setelah TP1 tercapai
- PnL & balance tersimpan di `data/paper_state.json`
- Tetap butuh Bybit API key yang valid (untuk ambil harga live)

---

## 💰 Mode Real Trade

- Order dikirim langsung ke Bybit via CCXT
- WebSocket memantau fill & perubahan posisi
- Split TP otomatis (30% / 30% / 40%)
- SL dipindah ke entry setelah TP1 tercapai
- Safety net untuk deteksi missed TP

---

## 🚀 Deployment di Linux (Systemd)

```bash
sudo cp deploy/bot.service /etc/systemd/system/bybit_bot.service
sudo systemctl daemon-reload
sudo systemctl enable bybit_bot
sudo systemctl start bybit_bot
sudo systemctl status bybit_bot
```

---

## ⚠️ Disclaimer

Software ini hanya untuk tujuan edukasi. Trading kripto mengandung risiko tinggi. Gunakan dengan risiko sendiri.
