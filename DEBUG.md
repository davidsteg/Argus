# Debug Checklist for Trading Bot

## 1. Backend Container Logs
```bash
docker-compose logs backend -f
```
Look for:
- ✅ "TRADING BOT STARTING" - Bot initialized
- ✅ "TRADING LOOP STARTED" - Loop running
- ✅ "HTTP REQUEST: GET /status" - API requests
- ❌ Any "ERROR" or "FAILED" messages

## 2. Frontend Container Logs
```bash
docker-compose logs frontend -f
```
Look for:
- ✅ "Frontend starting - API URL: http://backend:8000"
- ✅ "Fetching bot status" - API calls
- ❌ "Cannot connect to backend" - Connection issues

## 3. Check Bot Status
```bash
docker-compose exec backend curl http://localhost:8000/status
```

## 4. Test API Endpoints
```bash
# Health check
curl http://localhost:8000/health

# Get status
curl http://localhost:8000/status

# Get logs
curl http://localhost:8000/logs?limit=10

# Start bot (if stopped)
curl -X POST http://localhost:8000/start

# Stop bot
curl -X POST http://localhost:8000/stop
```

## 5. Frontend Dashboard
- Open http://localhost:8501
- Check bot status indicator (🟢 RUNNING / 🔴 STOPPED)
- Verify positions and orders tables show data
- Check "Recent Logs" section

## 6. Common Issues

### "Bot not initialized"
- Backend container may not have started properly
- Check backend logs for initialization errors
- Verify ALPACA_API_KEY and ALPACA_API_SECRET are set

### "Cannot connect to backend"
- Frontend container can't reach backend
- Check docker-compose network configuration
- Verify backend is listening on port 8000

### "Order failed" or "Failed to place order"
- Check Alpaca API credentials
- Verify paper trading account is active
- Check for API rate limits or authentication issues

### "Emergency kill switch activated"
- Bot hit daily stop-loss or manual kill was triggered
- Check logs for the reason
- Review positions after emergency exit

## 7. Manual Bot Control via API
```bash
# Start trading
curl -X POST http://localhost:8000/start

# Stop trading
curl -X POST http://localhost:8000/stop

# Emergency kill (liquidate all positions)
curl -X POST http://localhost:8000/kill

# Place manual order
curl -X POST http://localhost:8000/order \
  -H "Content-Type: application/json" \
  -d '{"symbol": "AAPL", "side": "buy", "quantity": 1.0}'
```

## 8. Database Location
- Path: `/data/trading_state.db` inside container
- To inspect: `docker-compose exec backend ls -lh /data/`
- To backup: `docker-compose cp backend:/data/trading_state.db .`

## 9. Environment Variables
Check .env file:
- ALPACA_API_KEY
- ALPACA_API_SECRET
- ALPACA_PAPER (should be "true")
- DAILY_STOP_LOSS
- MAX_POSITIONS
- POSITION_SIZE_USD

## 10. Trading Loop Behavior
- Runs every 10 seconds
- Checks risk management before each trade
- Buys if price increasing (simple momentum)
- Respects daily stop-loss limit
- Respects max positions limit

## 11. Logging Levels
- **INFO**: Normal operation, trading signals, status updates
- **WARNING**: Risk conditions, stop-loss triggers
- **ERROR**: Failed operations, API errors
- **CRITICAL**: Emergency kill switch

## 12. Test Scenarios

### Test 1: Bot Initialization
1. Start containers
2. Check backend logs for "TRADING BOT STARTING"
3. Verify bot_status in database

### Test 2: Manual Order
1. Place order via frontend or API
2. Check logs for "Order submitted successfully"
3. Verify order appears in "Recent Orders" table

### Test 3: Risk Management
1. Set low DAILY_STOP_LOSS in .env
2. Run bot
3. Check logs for "Daily stop-loss hit"

### Test 4: Emergency Kill
1. Start bot with positions
2. Click "Kill Bot" in frontend
3. Verify "Emergency EXIT COMPLETE" in logs
4. Check positions table is empty

## 13. Troubleshooting Commands
```bash
# Restart all containers
docker-compose down && docker-compose up -d

# View all logs
docker-compose logs -f

# Check container status
docker-compose ps

# Inspect backend logs in real-time
docker-compose logs backend -f --tail 100

# Test backend connectivity
docker-compose exec backend curl http://localhost:8000/health

# Check database
docker-compose exec backend sqlite3 /data/trading_state.db "SELECT * FROM log_entries ORDER BY id DESC LIMIT 10;"
```

## 14. Expected Log Output Example
```
2024-01-15 10:00:00,123 - __main__ - INFO - TRADING BOT STARTING
2024-01-15 10:00:00,124 - __main__ - INFO - Bot initialized with symbols: ['AAPL', 'MSFT']
2024-01-15 10:00:00,125 - __main__ - INFO - TRADING LOOP STARTED
2024-01-15 10:00:10,456 - __main__ - INFO - --- Trading loop iteration 1 ---
2024-01-15 10:00:10,457 - __main__ - INFO - Checking symbols: ['AAPL', 'MSFT']
2024-01-15 10:00:10,789 - __main__ - INFO - Processing symbol: AAPL
2024-01-15 10:00:10,890 - __main__ - INFO - Price data for AAPL: current=$150.25, previous=$149.80
2024-01-15 10:00:10,891 - __main__ - INFO - SIGNAL: BUY 0.6652 AAPL (@ $150.25, using $100.00)
2024-01-15 10:00:11,012 - __main__ - INFO - Order submitted successfully: ID=abc123, BUY 0.6652 AAPL
2024-01-15 10:00:12,345 - __main__ - INFO - HTTP RESPONSE: POST /order - Status: 200 - 0.890s
```

## 15. Performance Monitoring
- Trading loop iteration time should be ~10 seconds
- API response times should be < 1 second
- Database queries should be fast (< 10ms)
- No memory leaks or excessive logging
