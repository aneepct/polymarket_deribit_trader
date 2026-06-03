# Polymarket Deribit Trader

An autonomous trading bot that cross-references **Deribit options market** implied probability with **Polymarket binary prediction markets** to identify and trade mispricings on BTC and ETH daily resolution markets.

## How it works

1. **Signal** — Every 60 seconds, the bot fetches the current BTC/ETH implied probability from Deribit's options market (via mark price).
2. **Scan** — If the Deribit signal is outside a configurable neutral band (default 49 – 51%), the bot finds the matching Polymarket daily "YES/NO" market for that asset.
3. **Buy** — A limit buy order is placed on the Polymarket CLOB at the current best price.
4. **Monitor** — Once in a position, the bot tracks P&L every cycle:
   - **Profit target hit** → place a SELL limit order to close.
   - **Stop-loss hit** → place a SELL to cut the loss.
   - **Market about to expire** → place a SELL to exit before settlement.
   - **SELL stale / unfilled** → cancel it and re-place at the fresh price.
   - **BUY unfilled** → cancel and go back to scanning.
5. **Loop** — BTC and ETH run as independent async tasks in the same process, each with their own Redis-backed state.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│  Django Admin  (browser)                            │
│  • Edit all trading parameters live                 │
│  • Manage Polymarket credentials (encrypted in DB)  │
└───────────────┬─────────────────────────────────────┘
                │ reads config each cycle
┌───────────────▼─────────────────────────────────────┐
│  auto_trader.py  (asyncio loop)                     │
│  BTC task  │  ETH task                              │
│  SCANNING ──► MONITORING ──► SCANNING               │
└──────┬──────────────┬───────────────────────────────┘
       │              │
       ▼              ▼
  Redis (state)   PostgreSQL (config + credentials)
```

| Component | Technology |
|---|---|
| Web / admin | Django 5.1 + DRF |
| Trading loop | Python asyncio management command (`run_trader`) |
| Per-asset state | Redis hash (`trader:state:BTC`, `trader:state:ETH`) |
| Config / credentials | PostgreSQL — all editable in Django admin |
| Secret encryption | Fernet symmetric encryption (`cryptography` library) |
| HTTP client | `httpx` async |
| Polymarket CLOB | `py_clob_client_v2` |

## Configuration (Django admin)

All parameters are editable at `/admin/` without restarting the bot — changes are picked up on the next cycle.

| Setting | Default | Description |
|---|---|---|
| `order_usd` | 5.0 | USDC to spend per trade |
| `profit_target_pct` | 5.0 | Close when P&L reaches this % |
| `stop_loss_pct` | -20.0 | Cut loss when P&L drops to this % |
| `scan_interval_s` | 60 | Seconds between cycles |
| `deribit_neutral_low/high` | 0.49 / 0.51 | Skip signal if Deribit prob is in this band |
| `today_lookahead_hours` | 6.0 | Hours added to now() when checking if market resolves "today" |
| `assets` | BTC,ETH | Comma-separated assets to trade |

Polymarket `private_key` is stored encrypted in PostgreSQL (Fernet). The encryption key lives only in `.env`.

## Quick start

### 1. Clone and configure

```bash
git clone <repo>
cd polymarket_deribit_trader
cp .env.example .env
```

Edit `.env` and fill in:

```
DJANGO_SECRET_KEY=<generate with python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
FIELD_ENCRYPTION_KEY=<generate with python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
POSTGRES_PASSWORD=<choose a strong password>
```

### 2. Start with Docker

```bash
docker compose up -d
```

This starts PostgreSQL, Redis, the Django web server, and the trading loop.

### 3. Create admin user and set credentials

```bash
docker compose exec web python manage.py createsuperuser
```

Open `http://localhost:8000/admin/` and:
- Set your **Polymarket private key** and funder address under *Polymarket Credentials*.
- Adjust **Trading Config** parameters as needed.

### 4. Monitor

```bash
docker compose logs -f trader   # trading loop output
docker compose logs -f web      # Django / gunicorn
```

### REST API

| Endpoint | Description |
|---|---|
| `GET /api/positions/` | Current open Polymarket positions |
| `GET /api/trader/state/` | Redis state for each asset (BTC, ETH) |

## Development (without Docker)

```bash
python -m venv .venv && source .venv/bin/activate
Or source .venv/bin/activate
pip install -r requirements.txt

# Requires a local PostgreSQL and Redis, or set DATABASE_URL to sqlite:///db.sqlite3
export DATABASE_URL=sqlite:///db.sqlite3
export REDIS_URL=redis://localhost:6379/0
export DJANGO_SECRET_KEY=dev-secret
export FIELD_ENCRYPTION_KEY=$(python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")

python manage.py migrate
python manage.py createsuperuser
python manage.py runserver        # admin + API
python manage.py run_trader       # trading loop
```

## Project structure

```
polymarket_deribit_trader/
├── trader/                  # Django project settings, URLs, WSGI
├── trading/
│   ├── models.py            # TradingConfig, PolymarketCredentials (singleton models)
│   ├── encryption.py        # Fernet EncryptedCharField
│   ├── state.py             # AssetState — Redis-backed state machine
│   ├── auto_trader.py       # Core trading logic (asyncio)
│   ├── polymarket_client.py # Polymarket CLOB API wrapper
│   ├── deribit_client.py    # Deribit implied probability fetcher
│   ├── admin.py             # Django admin registration
│   ├── views.py             # REST API views
│   └── management/commands/run_trader.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example
```
