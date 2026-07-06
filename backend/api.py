"""
FastAPI Backend for Trading Bot
"""
import asyncio
import os
import logging
import traceback
from datetime import datetime, timezone
from typing import Optional, List, Dict
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel
from alpaca.trading.enums import OrderSide

from bot import TradingBot

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Add request logging middleware
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """Log all HTTP requests and responses."""
    logger.info(f"HTTP REQUEST: {request.method} {request.url.path}")
    
    start_time = datetime.now(timezone.utc)
    response = await call_next(request)
    process_time = (datetime.now(timezone.utc) - start_time).total_seconds()
    
    logger.info(
        f"HTTP RESPONSE: {request.method} {request.url.path} "
        f"- Status: {response.status_code} - {process_time:.3f}s"
    )
    
    return response

app = FastAPI(title="Trading Bot API", version="1.0.0")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global bot instance
bot_instance: Optional[TradingBot] = None

# Pydantic models
class OrderRequest(BaseModel):
    symbol: str
    side: str  # "buy" or "sell"
    quantity: float

class BotStatusResponse(BaseModel):
    status: str
    balance: float
    daily_pnl: float
    positions: List[Dict]
    orders: List[Dict]
    timestamp: str

class LogEntry(BaseModel):
    timestamp: str
    level: str
    message: str
    source: Optional[str] = None


@app.on_event("startup")
async def startup_event():
    """Initialize bot on startup."""
    global bot_instance
    logger.info("=" * 50)
    logger.info("STARTUP: Initializing Trading Bot API")
    logger.info(f"Environment: ALPACA_API_KEY={'set' if os.getenv('ALPACA_API_KEY') else 'NOT SET'}")
    logger.info(f"Environment: ALPACA_API_SECRET={'set' if os.getenv('ALPACA_API_SECRET') else 'NOT SET'}")
    logger.info("=" * 50)
    try:
        logger.info("Creating TradingBot instance...")
        bot_instance = TradingBot()
        logger.info("Trading Bot initialized successfully")
        logger.info(f"Bot status: {bot_instance.bot_status}")
    except Exception as e:
        logger.error(f"Failed to initialize bot: {e}", exc_info=True)
        raise


@app.get("/")
async def root():
    """Root endpoint."""
    logger.info("Health check: /")
    return {"message": "Trading Bot API", "version": "1.0.0"}


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    logger.info("Health check: /health")
    bot_status = bot_instance.bot_status if bot_instance else "unknown"
    logger.info(f"Bot status: {bot_status}")
    return {"status": "healthy", "bot_status": bot_status}


@app.get("/status", response_model=BotStatusResponse)
async def get_bot_status():
    """Get current bot status."""
    if not bot_instance:
        logger.warning("Bot not initialized")
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    logger.info("Fetching bot status from database")
    try:
        state = bot_instance.db.get_latest_state()
        logger.info(
            f"Bot status: {state['bot_status']}, "
            f"Balance: ${state['balance']:,.2f}, "
            f"Positions: {len(state['positions'])}, "
            f"Orders: {len(state['orders'])}"
        )
        return BotStatusResponse(
            status=state['bot_status'],
            balance=state['balance'],
            daily_pnl=state['daily_pnl'],
            positions=state['positions'],
            orders=state['orders'],
            timestamp=state['timestamp']
        )
    except Exception as e:
        logger.error(f"Error fetching bot status: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error fetching status: {str(e)}")


@app.post("/start")
async def start_bot():
    """Start the trading bot."""
    logger.info("=" * 60)
    logger.info("POST /start - Starting bot")
    logger.info("=" * 60)
    
    if not bot_instance:
        logger.error("Bot not initialized")
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    if bot_instance.bot_status == "RUNNING":
        logger.warning("Bot already running - ignoring start request")
        return {"message": "Bot is already running"}
    
    logger.info("Updating bot status to RUNNING")
    bot_instance.bot_status = "RUNNING"
    try:
        bot_instance.db.update_bot_status("RUNNING")
        bot_instance.db.log_entry("INFO", "Bot started manually", "API")
        logger.info("Status updated in database")
    except Exception as e:
        logger.error(f"Failed to update status in database: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update status: {str(e)}")
    
    # Start trading loop in background
    logger.info("Creating background task for trading loop")
    try:
        asyncio.create_task(bot_instance.run_trading_loop())
        logger.info("Trading loop task created successfully")
    except Exception as e:
        logger.error(f"Failed to create trading loop task: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start bot: {str(e)}")
    
    logger.info("Bot started successfully")
    return {"message": "Bot started", "status": "RUNNING"}


@app.post("/stop")
async def stop_bot():
    """Stop the trading bot."""
    logger.info("POST /stop - Stopping bot")
    
    if not bot_instance:
        logger.error("Bot not initialized")
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    if bot_instance.bot_status == "STOPPED":
        logger.warning("Bot already stopped - ignoring stop request")
        return {"message": "Bot is already stopped"}
    
    logger.info("Updating bot status to STOPPED")
    bot_instance.bot_status = "STOPPED"
    try:
        bot_instance.db.update_bot_status("STOPPED")
        bot_instance.db.log_entry("WARNING", "Bot stopped manually", "API")
        logger.info("Status updated in database")
    except Exception as e:
        logger.error(f"Failed to update status in database: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to update status: {str(e)}")
    
    logger.info("Bot stopped successfully")
    return {"message": "Bot stopped", "status": "STOPPED"}


@app.post("/kill")
async def kill_bot():
    """Emergency kill switch - liquidate all positions."""
    logger.critical("=" * 60)
    logger.critical("POST /kill - EMERGENCY KILL SWITCH")
    logger.critical("=" * 60)
    
    if not bot_instance:
        logger.error("Bot not initialized")
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    logger.info("Calling bot_instance.kill_bot()")
    try:
        bot_instance.kill_bot()
        bot_instance.db.log_entry("CRITICAL", "Emergency kill switch activated", "API")
        logger.warning("Emergency kill completed - all positions liquidated")
        return {"message": "Bot killed and all positions liquidated"}
    except Exception as e:
        logger.error(f"Emergency kill failed: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Emergency kill failed: {str(e)}")


@app.post("/order")
async def place_order(request: OrderRequest):
    """Place a manual order."""
    logger.info(f"POST /order - {request.side.upper()} {request.quantity} {request.symbol}")
    
    if not bot_instance:
        logger.error("Bot not initialized")
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    if request.side.lower() == "buy":
        side = OrderSide.BUY
    elif request.side.lower() == "sell":
        side = OrderSide.SELL
    else:
        raise HTTPException(status_code=400, detail="Invalid side. Use 'buy' or 'sell'")
    
    try:
        logger.info(f"Executing market order: {side.value} {request.quantity} {request.symbol}")
        order = bot_instance._execute_market_order(
            symbol=request.symbol,
            side=side,
            qty=request.quantity
        )
        bot_instance.db.log_entry("INFO", f"Manual order placed: {request.side} {request.quantity} {request.symbol}", "API")
        logger.info(f"Order placed successfully: {order.id}")
        return {"message": f"Order placed: {request.side} {request.quantity} {request.symbol}", "order_id": order.id}
    except Exception as e:
        logger.error(f"Failed to place order: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to place order: {str(e)}")


@app.get("/logs", response_model=List[LogEntry])
async def get_logs(limit: int = 50):
    """Get recent log entries."""
    if not bot_instance:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    try:
        logs = bot_instance.db.get_recent_logs(limit=limit)
        logger.info(f"Fetched {len(logs)} recent log entries")
        return logs
    except Exception as e:
        logger.error(f"Failed to fetch logs: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch logs: {str(e)}")


@app.get("/positions")
async def get_positions():
    """Get current positions."""
    if not bot_instance:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    try:
        positions = bot_instance._get_current_positions()
        logger.info(f"Fetching {len(positions)} current positions")
        return positions
    except Exception as e:
        logger.error(f"Failed to fetch positions: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch positions: {str(e)}")


@app.get("/orders")
async def get_orders():
    """Get recent orders."""
    if not bot_instance:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    try:
        orders = bot_instance._get_recent_orders()
        logger.info(f"Fetching {len(orders)} recent orders")
        return orders
    except Exception as e:
        logger.error(f"Failed to fetch orders: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch orders: {str(e)}")


@app.get("/price/{symbol}")
async def get_price_history(symbol: str, limit: int = 100):
    """Get price history for a symbol."""
    logger.info(f"POST /price/{symbol} - limit={limit}")
    
    if not bot_instance:
        raise HTTPException(status_code=503, detail="Bot not initialized")
    
    try:
        price_data = bot_instance._get_price_data(symbol, limit=limit)
        logger.info(f"Fetching {len(price_data)} price data points for {symbol}")
        return price_data
    except Exception as e:
        logger.error(f"Failed to fetch price data for {symbol}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to fetch price data: {str(e)}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
