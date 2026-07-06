# Automated Trading Bot Prototype

A fully containerized, async trading bot with a responsive Streamlit dashboard for monitoring and control.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     Docker Compose                          │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─────────────────┐              ┌─────────────────┐      │
│  │   trading_bot   │              │   trading_ui    │      │
│  │   (Python/Alpaca)│              │  (Streamlit)    │      │
│  │                 │              │                 │      │
│  │ • Async loop    │              │ • Auto-refresh  │      │
│  │ • Risk engine   │   SQLite     │ • Metrics       │      │
│  │ • Order executor│◄────────────►│ • Kill switch   │      │
│  └─────────────────┘    db_data   └─────────────────┘      │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## Features

### Backend Bot (Engine)
- ✅ Modern Python with `alpaca-py` SDK (async)
- ✅ Async event loop for non-blocking data streaming
- ✅ SQLite database for state persistence
- ✅ Full risk management system
- ✅ Automated bracket orders (TakeProfit + StopLoss)
- ✅ Emergency kill switch

### Frontend Dashboard
- ✅ Pure Python Streamlit with auto-refresh (2.5s)
- ✅ Live metrics: Balance, PnL, Bot Status
- ✅ Active positions table
- ✅ Equity curve visualization
- ✅ Live log viewer (last 20 entries)
- ✅ Emergency kill switch button

### Dockerization
- ✅ Docker Compose with two services
- ✅ Shared named volume (`db_data`)
- ✅ Timezone set to `Europe/Zurich`
- ✅ Environment variable configuration

## Prerequisites

1. **Python 3.11+** (for local development)
2. **Docker** and **Docker Compose** installed
3. **Alpaca API Keys** (Paper trading recommended)

## Setup Instructions

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment Variables

Create a `.env` file based on `.env.template`:

```bash
cp .env.template .env
```

Edit `.env` with your Alpaca credentials:

```
ALPACA_API_KEY=your_api_key_here
ALPACA_SECRET_KEY=your_secret_key_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets
DAILY_STOP_LOSS=1000
MAX_POSITIONS=5
POSITION_SIZE_USD=500
TRADING_SYMBOLS=AAPL,MSFT,GOOGL
```

### 3. Run with Docker Compose

```bash
docker-compose up --build
```

Or run in detached mode:

```bash
docker-compose up -d --build
```

### 4. Access the Dashboard

Once containers are running, access the Streamlit dashboard at:

```
http://localhost:8501
```

## Services

### trading_bot
- **Port**: 8080 (internal API, if needed)
- **Volume**: `/data` → `db_data` (SQLite database)
- **Environment**: All Alpaca and trading parameters

### trading_ui
- **Port**: 8501 (Streamlit interface)
- **Volume**: `/data` → `db_data` (read access to SQLite)
- **Auto-refresh**: Every 2.5 seconds

## Configuration Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `ALPACA_API_KEY` | - | Your Alpaca API key |
| `ALPACA_SECRET_KEY` | - | Your Alpaca secret key |
| `ALPACA_BASE_URL` | `https://paper-api.alpaca.markets` | Alpaca API base URL |
| `DAILY_STOP_LOSS` | `1000` | Daily stop-loss threshold (USD) |
| `MAX_POSITIONS` | `5` | Maximum concurrent positions |
| `POSITION_SIZE_USD` | `500` | Position size (USD) |
| `TRADING_SYMBOLS` | `AAPL,MSFT,GOOGL` | Comma-separated list of symbols |

## Emergency Kill Switch

The dashboard includes a prominent "EMERGENCY KILL SWITCH" button that:

1. Immediately liquidates all open positions
2. Halts the bot (updates status to "KILLED")
3. Prevents new orders from being placed

Use this in case of unexpected market movements or system issues.

## Database Schema

The SQLite database (`trading_state.db`) stores:

- **trading_state**: Current bot state, positions, orders
- **log_entries**: System logs (level, message, timestamp)
- **price_history**: Historical price data for visualization

## Project Structure

```
.
├── backend/
│   ├── bot.py              # Main trading bot engine
│   ├── Dockerfile          # Backend container config
│   └── requirements.txt    # Backend dependencies
├── frontend/
│   ├── app.py              # Streamlit dashboard
│   ├── Dockerfile          # Frontend container config
│   └── requirements.txt    # Frontend dependencies
├── shared/
│   └── models.py           # SQLite database models
├── docker-compose.yml       # Container orchestration
├── requirements.txt         # Unified dependencies
├── .env.template            # Environment variable template
└── README.md               # This file
```

## Development Mode

To run locally without Docker:

```bash
# Terminal 1: Start the bot
python backend/bot.py

# Terminal 2: Start the UI
streamlit run frontend/app.py --server.port 8501
```

## Security Notes

⚠️ **Important Security Considerations**:

1. **Never commit API keys** to version control
2. Use **paper trading** for testing
3. Implement proper **authentication** for production
4. Use **HTTPS** in production deployments
5. Store API keys in **secure vaults** (e.g., AWS Secrets Manager, HashiCorp Vault)

## Production Deployment

For production, consider:

- **Authentication**: Add user authentication to the Streamlit app
- **HTTPS**: Use a reverse proxy (nginx, Caddy) with SSL
- **Database**: Replace SQLite with PostgreSQL for better concurrency
- **Monitoring**: Add Prometheus metrics and Grafana dashboards
- **Logging**: Implement structured logging with ELK stack
- **Backup**: Regular database backups

## Troubleshooting

### Bot not connecting to Alpaca
- Verify API keys in `.env`
- Check network connectivity
- Ensure you're using the correct base URL (paper vs live)

### Dashboard not loading
- Check Docker logs: `docker-compose logs trading_ui`
- Verify port 8501 is not in use
- Ensure database file is accessible

### Database locked errors
- SQLite has limited concurrency
- For production, use PostgreSQL
- Reduce auto-refresh interval if needed

## License

MIT License - See LICENSE file for details

## Disclaimer

This is a prototype for educational purposes. Trading involves risk. Past performance does not guarantee future results. Use at your own risk.
```