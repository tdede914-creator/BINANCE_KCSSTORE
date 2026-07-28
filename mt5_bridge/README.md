# KCS MT5 Bridge (Windows) — Setup Guide

Bridge ini jalanin auto-eksekusi signal forex dari bot KCS ke MetaTrader
5 (MT5) di Exness. Ini yang di-install di **Windows VPS**, terpisah dari
Linux VPS yang jalanin scanner.

## Yang kalian butuhkan

- **Windows VPS** dengan minimum 2GB RAM, koneksi stabil ke internet
- **Akun Exness** (demo dulu untuk testing, real setelah confirmed)
- **MetaTrader 5** — download versi Exness (bukan yang official MetaQuotes)
- **Python 3.11 atau 3.12** — download dari python.org
- **Linux VPS bot** kalian (yang sekarang jalan di `43.128.118.89:8000`) harus reachable dari Windows VPS

---

## Step 1 — Install MT5 Exness

1. Login ke [Exness](https://exness.com) di browser
2. Personal Area → Download MT5 → Windows
3. Install dengan default settings
4. Buka MT5, di layar login pilih server:
   - **Demo**: `Exness-MT5Trial5` (buat testing)
   - **Real**: `Exness-MT5Real14` atau server yang di-assign ke akun kalian (cek di email pas buka akun)
5. Login pakai kredensial akun MT5 (bukan Exness web login)
6. **PENTING**: klik **Ctrl+E** untuk enable **AutoTrading**
   - Icon "Algo Trading" atau "Perdagangan Otomatis" di toolbar harus **hijau** (bukan merah)
   - Kalau merah, klik → jadi hijau. Ini WAJIB kalau ga Python API ga bisa kirim order.

## Step 2 — Install Python + package

1. Download Python 3.11 dari python.org
2. **Centang "Add Python to PATH"** waktu install (WAJIB)
3. Buka Command Prompt (Windows key → cmd → Enter)
4. Test install:
   ```cmd
   python --version
   ```
   Harus print `Python 3.11.x`

## Step 3 — Setup bridge

1. Buka Command Prompt di Windows VPS
2. Buat folder dan pindah ke sana:
   ```cmd
   mkdir C:\kcs_bridge
   cd C:\kcs_bridge
   ```
3. Download file bridge (ganti dengan cara upload/download yang kalian punya):
   - `bridge.py`
   - `config.example.py`
   - `requirements.txt`
4. Install dependency:
   ```cmd
   pip install -r requirements.txt
   ```
   Butuh 30-60 detik. Kalau ada error `MetaTrader5` ga bisa install:
   - Pastikan Python 32-bit atau 64-bit **sama** dengan MT5 (biasanya 64-bit)
   - Coba: `pip install --index-url=https://pypi.org/simple/ MetaTrader5`

## Step 4 — Config

1. Copy config template:
   ```cmd
   copy config.example.py config.py
   ```
2. Buka `config.py` pake Notepad
3. Edit isi:

   ```python
   BACKEND_URL = "http://43.128.118.89:8000"

   # Ambil dari web dashboard: Settings → "MT5 Bridge" → Secret
   BRIDGE_SECRET = "abc123xyz..."

   MT5_LOGIN = 12345678         # nomor login MT5 kalian
   MT5_PASSWORD = "password_mt5"
   MT5_SERVER = "Exness-MT5Trial5"  # atau server real kalian

   RISK_PER_TRADE_PCT = 1.0     # 1% dari balance per trade
   MAX_LOT_PER_TRADE = 0.5      # safety cap
   SYMBOLS_ALLOWLIST = ["XAUUSD", "EURUSD", "GBPUSD"]  # atau [] untuk semua
   POLL_INTERVAL_SECONDS = 5

   DRY_RUN = True               # !!! MULAI DENGAN True buat testing !!!
   ```

4. **Ambil BRIDGE_SECRET dari web**:
   - Buka `43.128.118.89:3000/settings` di browser
   - Scroll ke section **"MT5 Bridge"** (akan saya tambahkan di UI)
   - Copy secret-nya, paste ke `config.py`

## Step 5 — First run (DRY_RUN mode)

```cmd
cd C:\kcs_bridge
python bridge.py
```

Kalian harusnya lihat:
```
KCS MT5 bridge starting
Backend: http://43.128.118.89:8000
DRY_RUN=True — no real orders will be sent
Logged in: login=12345678 server=Exness-MT5Trial5 balance=10000.00 USD
MT5 lists 500 symbols
Symbol map built: 15 entries
  EURUSD     -> EURUSDm
  XAUUSD     -> XAUUSDm
  ...
```

Kalau muncul semua ini, artinya bridge **konek ke Linux VPS + login MT5 sukses**. Biarkan jalan.

Waktu signal forex fire di Linux backend:
```
FILLED signal #123 SHORT XAUUSDm lot=0.05 @ 2035.20 ticket=DRY-123
```

DRY_RUN mode akan log seolah-olah order dikirim, tapi **tidak beneran kirim ke Exness**. Cek dashboard web kalian — signal harus muncul dengan `mt5_ticket` = "DRY-123".

## Step 6 — Go live

Setelah confirm bridge log baik + web dashboard menerima laporan:

1. Stop bridge (`Ctrl+C`)
2. Edit `config.py`: `DRY_RUN = False`
3. Restart bridge:
   ```cmd
   python bridge.py
   ```
4. Sekarang signal baru akan dieksekusi real di MT5. Kalian akan lihat position beneran di MT5 UI.

## Step 7 — Bikin bridge auto-start (opsional tapi recommended)

Biar bridge auto-jalan pas Windows VPS reboot, pakai **Task Scheduler**:

1. Windows key → search "Task Scheduler" → open
2. Action → Create Basic Task
3. Name: `KCS MT5 Bridge`
4. Trigger: **When the computer starts**
5. Action: **Start a program**
6. Program: `python`
7. Arguments: `C:\kcs_bridge\bridge.py`
8. Start in: `C:\kcs_bridge`
9. Finish
10. Right-click task → Properties → **Run whether user is logged on or not** → OK

Alternatif lebih robust: pakai **NSSM** (Non-Sucking Service Manager):

```cmd
choco install nssm
nssm install "KCS_Bridge" C:\Python311\python.exe C:\kcs_bridge\bridge.py
nssm set "KCS_Bridge" AppDirectory C:\kcs_bridge
nssm start "KCS_Bridge"
```

## Troubleshooting

### "MT5 initialize failed"
- MT5 terminal belum jalan → buka MT5, login manual dulu, biarkan running
- Salah login/password/server → cek email Exness untuk kredensial

### "no MT5 symbol found for XAUUSD"
- Bridge ga bisa cari suffix Exness → cek di MT5, ketik XAU di search Market Watch, lihat suffix yang muncul
- Tambahin ke `SymbolMap.INDEX_ALIASES` di `bridge.py`

### "retcode 10006 comment=Request rejected"
- AutoTrading disabled → Ctrl+E di MT5, pastikan icon hijau
- Symbol suspended (weekend) → tunggu Senin buka
- Not enough margin → tambah balance atau kurangi RISK_PER_TRADE_PCT

### Bridge log "backend HTTP error"
- Linux VPS ga reachable dari Windows VPS → cek firewall, pastikan port 8000 open
- BRIDGE_SECRET salah → copy ulang dari web Settings

### Position ga muncul di web dashboard
- Cek log bridge — ada baris `FILLED signal #X ticket=Y`?
- Kalau ada, cek Linux backend log: `docker compose logs backend | grep mt5`
- Kalau ada `mt5.report.filled` → sukses, refresh dashboard

---

## Design notes

- Bridge **poll setiap 5 detik** ke backend untuk signal baru. Latency total signal-to-fill: 5-10 detik.
- **1 signal → 1 posisi MT5**. TP1 native (di broker), TP2/TP3 display-only.
- **SL native** di broker (visible di MT5 UI). Kalau harga tembus SL, MT5 auto-close.
- Bridge polling MT5 positions setiap 5 detik untuk detect closure (SL/TP hit).
- **Bridge crash / VPS mati**: existing positions tetap dilindungi SL native di broker. Signal baru ga akan dieksekusi sampai bridge nyala lagi.

## Safety recommendations

- **DRY_RUN=True** minimal 1 hari sebelum go live
- **Demo account** minimal 1 minggu sebelum real account
- **Modal awal real**: max 50-100 USD, tingkatkan gradual
- **MAX_LOT_PER_TRADE**: set 0.05 untuk akun kecil ($100), 0.5 untuk akun besar ($1000+)
- **RISK_PER_TRADE_PCT**: 0.5-1.0 untuk mulai. Jangan 5%+ walau strategi terlihat bagus di backtest.
