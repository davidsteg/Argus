"""
Trading Bot Engine - Async Alpaca Trading Bot
"""
import asyncio
import os
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List
from decimal import Decimal

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, AssetStatus
from alpaca.common.enums import BaseURL
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.models import Quote, Bar

import models

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class TradingBot:
    def __init__(self):
        self.api_key = os.getenv('ALPACA_API_KEY', '')
        self.api_secret = os.getenv('ALPACA_SECRET_KEY', '')
        self.base_url = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
        
        self.daily_stop_loss = float(os.getenv('DAILY_STOP_LOSS', '1000'))
        self.max_positions = int(os.getenv('MAX_POSITIONS', '5'))
        self.position_size = float(os.getenv('POSITION_SIZE_USD', '500'))
        self.trading_symbols = os.getenv('TRADING_SYMBOLS', 'AAPL,MSFT,GOOGL').split(',')
        
        self.db = models.Database()
        self.bot_status = "STOPPED"
        self.daily_pnl = 0.0
        self.daily_reset_date = datetime.now(timezone.utc).date()
        
        self.trading_client = TradingClient(
            self.api_key, 
            self.api_secret
        )
        
        self.data_client = StockHistoricalDataClient(self.api_key, self.api_secret)
        
        self.db.initialize()
        self._update_bot_status("STOPPED")
        
    def _update_bot_status(self, status: str):
        """Update bot status in database."""
        self.bot_status = status
        self.db.update_bot_status(status)
        logger.info(f"Bot status updated to {status}")
    
    def _check_daily_reset(self):
        """Reset daily PnL if it's a new day."""
        current_date = datetime.now(timezone.utc).date()
        if current_date != self.daily_reset_date:
            logger.info(
                f"Daily reset: {self.daily_reset_date} -> {current_date}"
            )
            logger.info(f"Closing PnL for old day: ${self.daily_pnl:.2f}")
            self.daily_pnl = 0.0
            self.daily_reset_date = current_date
            logger.info("Daily PnL reset for new trading day")
    
    def _get_account_balance(self) -> Decimal:
        """Get account buying power."""
        account = self.trading_client.get_account()
        return Decimal(str(account.buying_power))
    
    def _get_current_positions(self) -> List[Dict]:
        """Get all open positions."""
        positions = self.trading_client.get_all_positions()
        result = []
        for pos in positions:
            result.append({
                'symbol': pos.symbol,
                'qty': float(pos.qty),
                'side': pos.side,
                'market_value': float(pos.market_value),
                'avg_entry_price': float(pos.avg_entry_price),
                'unrealized_pnl': float(pos.unrealized_pl),
                'current_price': float(pos.current_price),
                'change': float(pos.change_today)
            })
        return result
    
    def _get_recent_orders(self) -> List[Dict]:
        """Get recent trade history."""
        orders = self.trading_client.get_orders(GetOrdersRequest(
            status=OrderStatus.FILLED,
            limit=50
        ))
        
        result = []
        for order in orders:
            result.append({
                'id': order.id,
                'symbol': order.symbol,
                'side': order.side.value,
                'qty': float(order.qty),
                'filled_avg_price': float(order.filled_avg_price or 0),
                'status': order.status.value,
                'filled_at': order.filled_at.isoformat() if order.filled_at else None,
                'order_type': order.order_type.value,
                'time_in_force': order.time_in_force.value
            })
        return result
    
    def _get_price_data(self, symbol: str, interval: str = '1Min', limit: int = 100) -> List[Dict]:
        """Get recent price data for a symbol."""
        request_params = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(1, TimeFrameUnit.Minute),
            start=datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0),
            end=datetime.now(timezone.utc),
            limit=limit
        )
        
        bars = self.data_client.get_stock_bars(request_params)
        
        result = []
        if bars:
            for bar in bars.get(symbol, []):
                result.append({
                    'timestamp': bar.timestamp.isoformat(),
                    'open': float(bar.open),
                    'high': float(bar.high),
                    'low': float(bar.low),
                    'close': float(bar.close),
                    'volume': bar.volume
                })
        
        return result
    
    def _execute_market_order(self, symbol: str, side: OrderSide, qty: float):
        """Execute a market order."""
        order_request = MarketOrderRequest(
            symbol=symbol,
            notional=None,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY
        )
        
        try:
            logger.info(
                f"Submitting order: {side.value} {qty:.4f} {symbol} "
                f"(qty=${self.position_size:.2f} / price)"
            )
            order = self.trading_client.submit_order(order_request)
            logger.info(
                f"Order submitted successfully: "
                f"ID={order.id}, "
                f"{side.value} {qty:.4f} {symbol}"
            )
            return order
        except Exception as e:
            logger.error(
                f"Failed to submit order: {side.value} {qty:.4f} {symbol} - {str(e)}",
                exc_info=True
            )
            raise
    
    def _execute_bracket_order(self, symbol: str, side: OrderSide, qty: float, 
                               limit_price: Optional[float] = None):
        """Execute a bracket order with stop loss and take profit."""
        if limit_price is None:
            # Get current price
            quotes = self.data_client.get_latest_stock_quote(symbol)
            limit_price = float(quotes[symbol].ask_price)
        
        # Calculate stop loss and take profit levels
        stop_loss_price = limit_price * 0.98  # 2% stop loss
        take_profit_price = limit_price * 1.02  # 2% take profit
        
        # Convert to integers for Alpaca (cents)
        limit_price_int = int(limit_price * 100)
        stop_loss_price_int = int(stop_loss_price * 100)
        take_profit_price_int = int(take_profit_price * 100)
        
        # Create bracket order
        order_request = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side.value,
            "type": "limit",
            "time_in_force": "day",
            "limit_price": str(limit_price),
            "take_profit": {
                "limit_price": str(take_profit_price)
            },
            "stop_loss": {
                "stop_price": str(stop_loss_price),
                "limit_price": str(stop_loss_price * 0.99)
            }
        }
        
        order = self.trading_client.submit_order(order_request)
        logger.info(f"Bracket order placed: {side.value} {qty} {symbol}")
        return order
    
    def _check_risk_management(self) -> bool:
        """Check if trading should be halted due to risk conditions."""
        self._check_daily_reset()
        
        logger.info("Running risk management check")
        
        # Check daily PnL
        if abs(self.daily_pnl) >= self.daily_stop_loss:
            logger.warning(
                f"Daily stop-loss hit: ${self.daily_pnl:.2f} "
                f"(limit: ${self.daily_stop_loss:.2f})"
            )
            logger.info("Executing emergency exit")
            self._execute_emergency_exit()
            self._update_bot_status("STOPPED")
            return False
        
        # Check number of open positions
        try:
            positions = self._get_current_positions()
            logger.info(f"Current positions: {len(positions)}/{self.max_positions}")
            if len(positions) >= self.max_positions:
                logger.warning(
                    f"Max positions reached: {len(positions)}/{self.max_positions}"
                )
                return False
        except Exception as e:
            logger.error(f"Failed to get current positions: {str(e)}", exc_info=True)
            return False
        
        return True
    
    def _execute_emergency_exit(self):
        """Liquidate all open positions."""
        logger.info("EMERGENCY EXIT: Liquidating all positions")
        positions = self._get_current_positions()
        
        if not positions:
            logger.info("No positions to liquidate")
            return
        
        logger.info(f"Found {len(positions)} positions to liquidate")
        
        for pos in positions:
            try:
                if pos['side'] == 'buy':
                    logger.info(f"Liquidating LONG: {pos['qty']} {pos['symbol']}")
                    self._execute_market_order(pos['symbol'], OrderSide.SELL, pos['qty'])
                elif pos['side'] == 'sell':
                    logger.info(f"Liquidating SHORT: {pos['qty']} {pos['symbol']}")
                    self._execute_market_order(pos['symbol'], OrderSide.BUY, pos['qty'])
                else:
                    logger.warning(f"Unknown position side: {pos['side']} for {pos['symbol']}")
            except Exception as e:
                logger.error(
                    f"Failed to liquidate {pos['side']} {pos['qty']} {pos['symbol']}: {str(e)}",
                    exc_info=True
                )
        
        logger.info("EMERGENCY EXIT COMPLETE: All positions liquidated")
    
    def _calculate_daily_pnl(self) -> float:
        """Calculate current day's PnL."""
        positions = self._get_current_positions()
        total_pnl = 0.0
        
        for pos in positions:
            total_pnl += pos['unrealized_pnl']
        
        # Add realized PnL from recent trades
        orders = self._get_recent_orders()
        today_orders = [o for o in orders if o.get('filled_at', '').startswith(
            datetime.now(timezone.utc).strftime('%Y-%m-%d'))
        ]
        
        for order in today_orders:
            if order['side'] == 'sell':
                pnl = (order['filled_avg_price'] - order.get('avg_entry_price', 0)) * order['qty']
                total_pnl += pnl
        
        return total_pnl
    
    def _update_trading_state(self):
        """Update trading state in database."""
        try:
            balance = self._get_account_balance()
            logger.info(f"Account balance: ${float(balance):,.2f}")
        except Exception as e:
            logger.error(f"Failed to get account balance: {str(e)}", exc_info=True)
            balance = 0
        
        try:
            positions = self._get_current_positions()
            logger.info(f"Current positions: {len(positions)}")
            for pos in positions:
                logger.info(
                    f"  Position: {pos['side']} {pos['qty']} {pos['symbol']} "
                    f"- PnL: ${pos['unrealized_pnl']:,.2f}"
                )
        except Exception as e:
            logger.error(f"Failed to get positions: {str(e)}", exc_info=True)
            positions = []
        
        try:
            orders = self._get_recent_orders()
            logger.info(f"Recent orders: {len(orders)}")
        except Exception as e:
            logger.error(f"Failed to get orders: {str(e)}", exc_info=True)
            orders = []
        
        try:
            daily_pnl = self._calculate_daily_pnl()
            logger.info(f"Daily PnL: ${daily_pnl:.2f}")
        except Exception as e:
            logger.error(f"Failed to calculate daily PnL: {str(e)}", exc_info=True)
            daily_pnl = 0.0
        
        try:
            self.db.update_trading_state(
                balance=float(balance),
                positions=positions,
                orders=orders,
                daily_pnl=daily_pnl,
                bot_status=self.bot_status
            )
            logger.info("Trading state updated in database")
        except Exception as e:
            logger.error(f"Failed to update trading state: {str(e)}", exc_info=True)
    
    async def run_trading_loop(self):
        """Main async trading loop."""
        logger.info("=" * 60)
        logger.info("TRADING LOOP STARTED")
        logger.info("=" * 60)
        self._update_bot_status("RUNNING")
        
        iteration = 0
        while self.bot_status == "RUNNING":
            iteration += 1
            logger.info(f"--- Trading loop iteration {iteration} ---")
            
            try:
                self._check_daily_reset()
                
                if self._check_risk_management():
                    # Execute trading logic
                    logger.info(f"Checking symbols: {self.trading_symbols}")
                    
                    for symbol in self.trading_symbols:
                        try:
                            logger.info(f"Processing symbol: {symbol}")
                            
                            # Simple momentum strategy: buy if price increasing
                            price_data = self._get_price_data(symbol, limit=10)
                            
                            if len(price_data) >= 5:
                                current_price = price_data[-1]['close']
                                prev_price = price_data[-2]['close']
                                
                                logger.info(
                                    f"Price data for {symbol}: "
                                    f"current=${current_price:.2f}, "
                                    f"previous=${prev_price:.2f}, "
                                    f"change={((current_price - prev_price) / prev_price) * 100:.2f}%"
                                )
                                
                                if current_price > prev_price:
                                    order_qty = self.position_size / current_price
                                    logger.info(
                                        f"SIGNAL: BUY {order_qty:.4f} {symbol} "
                                        f"(@ ${current_price:.2f}, using ${self.position_size:.2f})"
                                    )
                                    self._execute_market_order(symbol, OrderSide.BUY, order_qty)
                                else:
                                    logger.info(f"No buy signal for {symbol}: price decreasing")
                            else:
                                logger.warning(
                                    f"Not enough price data for {symbol}: "
                                    f"got {len(price_data)}, need 5"
                                )
                        except Exception as e:
                            logger.error(f"Error processing {symbol}: {str(e)}", exc_info=True)
                
                # Update state every 10 seconds
                logger.info("Updating trading state")
                self._update_trading_state()
                logger.info("Trading state updated")
                
                await asyncio.sleep(10)
                
            except Exception as e:
                logger.error(f"Error in trading loop iteration {iteration}: {str(e)}", exc_info=True)
                logger.info("Waiting 30 seconds before retry")
                await asyncio.sleep(30)
        
        logger.info("Trading loop ended - bot stopped")
    
    def kill_bot(self):
        """Kill the bot and liquidate positions."""
        logger.warning("Emergency kill switch activated")
        self._execute_emergency_exit()
        self._update_bot_status("STOPPED")
        return True


def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("TRADING BOT STARTING")
    logger.info("=" * 60)
    
    bot = TradingBot()
    
    logger.info(f"Bot initialized with symbols: {bot.trading_symbols}")
    logger.info(f"Daily stop-loss: ${bot.daily_stop_loss}")
    logger.info(f"Max positions: {bot.max_positions}")
    logger.info(f"Position size: ${bot.position_size}")
    logger.info(f"Trading symbols: {bot.trading_symbols}")
    
    try:
        logger.info("Starting trading loop...")
        asyncio.run(bot.run_trading_loop())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (KeyboardInterrupt)")
        bot.kill_bot()
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        bot.kill_bot()


if __name__ == "__main__":
    main()