# BINANCE_KCSSTORE — Futures Signal Bot

A full-stack **Binance USDT-M Futures signal generator and (paper/live) auto-executor** with a Multi-Timeframe Confluence strategy.

> ⚠️ **DISCLAIMER**: This tool is for educational purposes. Crypto futures trading carries substantial risk. You can lose your entire capital. Use paper trading first. The author(s) are not responsible for any losses. **Never run live trading with money you cannot afford to lose.**

---

## Features

- **Multi-Timeframe Confluence strategy**
  - Bias filter (HTF, e.g. 4H): EMA 50 vs EMA 200 → LONG / SHORT / NEUTRAL bias
  - Setup zone (MTF, e.g. 1H): Support/Resistance + Order Block detection
  - Entry trigger (configurable LTF: **1m / 3m / 5m / 15m**): Break of Structure + RSI + EMA retest
- **Risk management**
  - Position sizing by % of equity
  - SL: last swing + 0.5× ATR buffer
  - Dual TP: RR 1:2 (close 50%) + RR 1:3 (close 50%)
- **Paper trading mode** (default) — trade with virtual balance using real market prices
- **Live trading mode** — executes real orders on Binance Futures
  - **Zero-latency SL/TP**: uses on-exchange `STOP_MARKET` + `TAKE_PROFIT_MARKET` orders with `reduceOnly` and `closePosition` flags, so exits are handled by Binance's matching engine even if the bot crashes.
- **Encrypted API key storage** — Fernet-encrypted at rest, never sent to frontend
- **Real-time UI**
  - Dashboard with live signals
  - Signal history + P&L tracking
  - Configurable pair watchlist + strategy parameters
- **24/7 scanner** — background async task loop, one worker per pair, uses WebSocket kline streams (no REST polling)

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Frontend (Next.js 14 + TypeScript + Tailwind)               │
│  - Dashboard, Signals, History, Settings                     │
│  - TradingView Lightweight Charts                            │
│  - Live signal push via WebSocket                            │
└─────────────────────────┬────────────────────────────────────┘
                          │ REST + WS
┌─────────────────────────▼────────────────────────────────────┐
│  Backend (FastAPI + AsyncIO + SQLModel)                      │
│                                                              │
│  ┌─────────────────┐    ┌────────────────────────────────┐   │
│  │  Signal Scanner │───▶│  Strategy Engine (MTF Conflu.) │   │
│  │  (async loop)   │    │  Indicators, Bias, Setup, Trig │   │
│  └────────┬────────┘    └───────────────┬────────────────┘   │
│           │                             │                    │
│           │                             ▼                    │
│           │                    ┌────────────────┐            │
│           │                    │  Risk Manager  │            │
│           │                    │  SL / TP / Size│            │
│           │                    └────────┬───────┘            │
│           │                             │                    │
│           ▼                             ▼                    │
│  ┌────────────────┐            ┌────────────────┐            │
│  │ Binance Client │            │  Executor      │            │
│  │ REST + WS      │◀───────────│ Paper | Live   │            │
│  └────────┬───────┘            └────────────────┘            │
└───────────┼──────────────────────────────────────────────────┘
            │
            ▼
    Binance Futures API
    (REST + WebSocket)
```

---

## Tech Stack

| Layer | Tech |
|---|---|
| Backend | Python 3.11, FastAPI, SQLModel, AsyncIO, `python-binance`, `pandas`, `pandas-ta` |
| Storage | PostgreSQL (prod) / SQLite (dev), Redis (pub/sub) |
| Frontend | Next.js 14 (App Router), TypeScript, Tailwind CSS, TradingView Lightweight Charts |
| Infra | Docker Compose |

---

## Quick Start (Development)

### 1. Prerequisites

- Docker + Docker Compose, **OR** Python 3.11+ and Node.js 20+
- Binance account with **Futures enabled** and an API key
  - ⚠️ **Recommended**: Start with [Binance Futures Testnet](https://testnet.binancefuture.com/) — get free testnet USDT
  - API key permissions: `Enable Futures` only. **DO NOT enable withdrawals.**

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — most importantly, set:
#   ENCRYPTION_KEY  (generate one: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
#   JWT_SECRET      (any long random string)
#   BINANCE_TESTNET (leave true for first run)
```

### 3. Run with Docker (recommended)

```bash
docker compose up --build
```

- Backend: http://localhost:8000
- Backend docs: http://localhost:8000/docs
- Frontend: http://localhost:3000

### 4. Run without Docker

**Backend:**
```bash
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

**Frontend (in another terminal):**
```bash
cd frontend
npm install
npm run dev
```

### 5. Setup in the UI

1. Open http://localhost:3000
2. Go to **Settings** → paste your Binance API Key + Secret → Save
   - The key is encrypted at rest before storing.
3. Adjust watchlist, timeframes, risk% if needed.
4. Toggle mode: **Paper** (default) or **Live**.
5. Enable scanner → signals will start appearing on **Dashboard**.

---

## Deploying to your VPS

Recommended: Ubuntu 22.04, min 2GB RAM, region close to Binance (Tokyo / Singapore).

```bash
# On VPS
sudo apt update && sudo apt install -y docker.io docker-compose-plugin git
git clone https://github.com/tdede914-creator/BINANCE_KCSSTORE.git
cd BINANCE_KCSSTORE
cp .env.example .env
nano .env                             # fill values
docker compose up -d --build

# Check logs
docker compose logs -f backend
```

Then either:
- Reverse-proxy via Nginx + Let's Encrypt to expose the frontend publicly with HTTPS, or
- Keep it bound to `localhost` and access via SSH tunnel:
  `ssh -L 3000:localhost:3000 -L 8000:localhost:8000 user@your-vps`

---

## Strategy Detail

### Multi-Timeframe Confluence

Signal fires only when all three timeframes agree.

**1. Bias (HTF, default 4H)**
- `EMA(50) > EMA(200)` → **LONG bias only**
- `EMA(50) < EMA(200)` → **SHORT bias only**
- Otherwise → skip pair this scan

**2. Setup (MTF, default 1H)**
- Find latest swing highs/lows → S/R zones
- Detect last valid bullish/bearish order block near price
- Price must be **within N × ATR** of the setup zone

**3. Trigger (LTF, configurable: 1m / 3m / 5m / 15m)**
- Break of Structure (BOS) in bias direction
- RSI(14) not in extreme (not >75 for LONG, not <25 for SHORT)
- Close-back retest of key EMA(20)
- Volume confirmation (last candle volume > MA(20) volume)

**4. Risk**
- Position size = `equity × risk% / (entry − SL)`
- SL = last swing low/high ± `0.5 × ATR(14)`
- TP1 at `RR 1:2` (close 50% + move SL to break-even)
- TP2 at `RR 1:3` (close remaining 50%)

**5. Live-mode execution (zero-latency SL/TP)**
1. Submit MARKET or LIMIT entry order.
2. On fill confirmation → immediately submit:
   - `STOP_MARKET` with `closePosition=true` (SL)
   - `TAKE_PROFIT_MARKET` with `reduceOnly=true, quantity=50%` (TP1)
   - `TAKE_PROFIT_MARKET` with `closePosition=true` (TP2)
3. After TP1 fills → move SL to entry price (break-even) via order edit.

This means **once the entry is filled, the bot no longer needs to be online** for SL/TP to execute — they live on Binance's matching engine.

---

## Project Structure

```
BINANCE_KCSSTORE/
├── backend/
│   ├── app/
│   │   ├── main.py                # FastAPI entry
│   │   ├── core/
│   │   │   ├── config.py          # Settings (pydantic)
│   │   │   ├── security.py        # Fernet encryption, JWT
│   │   │   └── logging.py
│   │   ├── db/
│   │   │   ├── database.py        # Async engine
│   │   │   └── models.py          # ORM models
│   │   ├── binance/
│   │   │   ├── rest.py            # REST wrapper
│   │   │   └── ws.py              # WebSocket kline stream
│   │   ├── strategy/
│   │   │   ├── indicators.py      # EMA, RSI, ATR, swings
│   │   │   ├── mtf_confluence.py  # Main strategy
│   │   │   └── types.py           # Signal, Bias enums
│   │   ├── risk/
│   │   │   └── manager.py         # Sizing + SL/TP
│   │   ├── executor/
│   │   │   ├── base.py            # BaseExecutor
│   │   │   ├── paper.py           # PaperExecutor
│   │   │   └── live.py            # LiveExecutor (on-exchange OCO)
│   │   ├── scanner/
│   │   │   └── engine.py          # Async 24/7 scan loop
│   │   └── api/
│   │       ├── deps.py
│   │       ├── auth.py
│   │       ├── signals.py
│   │       ├── trades.py
│   │       ├── config.py
│   │       └── ws.py              # Frontend live push
│   ├── requirements.txt
│   ├── Dockerfile
│   └── pyproject.toml
├── frontend/
│   ├── app/
│   │   ├── layout.tsx
│   │   ├── page.tsx               # Dashboard
│   │   ├── signals/page.tsx
│   │   ├── history/page.tsx
│   │   └── settings/page.tsx
│   ├── components/
│   │   ├── SignalCard.tsx
│   │   ├── PriceChart.tsx
│   │   ├── StatCard.tsx
│   │   └── ...
│   ├── lib/
│   │   ├── api.ts
│   │   └── ws.ts
│   ├── package.json
│   ├── tsconfig.json
│   ├── tailwind.config.ts
│   └── Dockerfile
├── docker-compose.yml
├── .env.example
├── .gitignore
└── README.md
```

---

## Roadmap

- [x] Multi-Timeframe Confluence strategy
- [x] Paper + Live executor
- [x] Encrypted API key storage
- [ ] Backtesting engine
- [ ] Telegram / Discord notifications
- [ ] Trailing stop mode
- [ ] Multi-user support with auth
- [ ] Strategy plugins (add your own)

---

## License

MIT — see [LICENSE](LICENSE)
