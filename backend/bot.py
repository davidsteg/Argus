"""
Self-Improving Trading Bot Engine - AI-Powered with Learning Capabilities
"""
import asyncio
import os
import json
import logging
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, OrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.models import Quote, Bar
import yfinance as yf

import models

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class StrategyType(Enum):
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    ML_PREDICTIVE = "ml_predictive"
    HYBRID = "hybrid"


@dataclass
class TradeRecord:
    symbol: str
    side: str
    price: float
    quantity: float
    timestamp: str
    pnl: float = 0.0
    success: bool = True
    strategy: str = ""


@dataclass
class StrategyParams:
    # Strategy type
    strategy_type: str = "hybrid"
    
    # Risk management
    daily_stop_loss: float = 1000.0
    max_positions: int = 5
    position_size_usd: float = 500.0
    max_drawdown: float = 0.05  # 5% max drawdown
    
    # Momentum strategy
    momentum_threshold: float = 0.01  # 1% price change threshold
    lookback_period: int = 10  # bars to look back
    
    # Mean reversion
    rsi_overbought: int = 70
    rsi_oversold: int = 30
    bollinger_band_width: float = 2.0
    
    # ML parameters
    ml_training_data_points: int = 1000
    ml_confidence_threshold: float = 0.4  # Lower threshold for more trades in learning phase
    ml_retrain_frequency: int = 50  # More frequent retraining
    
    # Learning parameters - adjusted for initial learning phase
    learning_rate: float = 0.05  # Higher learning rate for faster initial learning
    exploration_rate: float = 0.9  # High exploration rate for initial learning
    reward_decay: float = 0.99
    
    # Performance tracking - adjusted for initial phase
    target_win_rate: float = 0.5  # Lower target for initial learning
    target_profit_factor: float = 1.2  # Lower target for initial learning
    max_consecutive_losses: int = 5  # More tolerance for learning


class TradingBot:
    def __init__(self):
        self.api_key = os.getenv('ALPACA_API_KEY', '')
        self.api_secret = os.getenv('ALPACA_SECRET_KEY', '')
        self.base_url = os.getenv('ALPACA_BASE_URL', 'https://paper-api.alpaca.markets')
        
        # Initialize with default parameters
        self.params = StrategyParams()
        self.db = models.Database()
        self.bot_status = "STOPPED"
        self.daily_pnl = 0.0
        self.daily_reset_date = datetime.now(timezone.utc).date()
        
        # Trading clients
        self.trading_client = TradingClient(
            self.api_key, 
            self.api_secret
        )
        self.data_client = StockHistoricalDataClient(
            self.api_key, 
            self.api_secret
        )
        self.yf_client = yf.Ticker  # Yahoo Finance client for fallback
        
        # Trading state
        self.trading_symbols = os.getenv('TRADING_SYMBOLS', 'AAPL,MSFT,GOOGL').split(',')
        self.positions = {}
        self.trade_history = []
        self.performance_metrics = {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'total_pnl': 0.0,
            'win_rate': 0.0,
            'profit_factor': 0.0,
            'max_drawdown': 0.0,
            'sharpe_ratio': 0.0
        }
        
        # ML Model (simplified Q-learning)
        self.q_table = {}
        self.state_space = {}
        self.action_space = ['BUY', 'SELL', 'HOLD']
        
        # Learning state
        self.trades_since_last_retrain = 0
        self.is_learning = False
        self.learning_log = []
        
        logger.info("Self-Improving Trading Bot initialized")
        logger.info(f"Strategy: {self.params.strategy_type}")
        logger.info(f"Symbols: {self.trading_symbols}")
    
    def _update_bot_status(self, status: str):
        """Update bot status in database."""
        self.bot_status = status
        self.db.update_bot_status(status)
    
    def _check_daily_reset(self):
        """Reset daily PnL if new day."""
        today = datetime.now(timezone.utc).date()
        if today > self.daily_reset_date:
            self.daily_pnl = 0.0
            self.daily_reset_date = today
            logger.info("Daily PnL reset")
    
    def _get_account_balance(self) -> Decimal:
        """Get current account balance."""
        try:
            account = self.trading_client.get_account()
            return Decimal(account.equity)
        except Exception as e:
            logger.error(f"Error getting account balance: {e}")
            return Decimal(0)
    
    def _get_current_positions(self) -> List[Dict]:
        """Get current positions."""
        try:
            positions = self.trading_client.get_all_positions()
            result = []
            for pos in positions:
                result.append({
                    'symbol': pos.symbol,
                    'qty': float(pos.qty),
                    'side': pos.side,
                    'market_value': float(pos.market_value),
                    'avg_entry_price': float(pos.avg_entry_price),
                    'unrealized_pl': float(pos.unrealized_pl)
                })
            return result
        except Exception as e:
            logger.error(f"Error getting positions: {e}")
            return []
    
    def _get_recent_orders(self) -> List[Dict]:
        """Get recent orders."""
        try:
            orders = self.trading_client.get_orders(
                GetOrdersRequest(status=OrderStatus.FILLED)
            )
            result = []
            for order in orders[-10:]:  # Last 10 orders
                result.append({
                    'symbol': order.symbol,
                    'side': order.side,
                    'qty': float(order.qty),
                    'filled_avg_price': float(order.filled_avg_price),
                    'time': order.submitted_at.isoformat() if order.submitted_at else None
                })
            return result
        except Exception as e:
            logger.error(f"Error getting orders: {e}")
            return []
    
    def _get_price_data(self, symbol: str, interval: str = '1Min', limit: int = 100) -> List[Dict]:
        """Get historical price data with IEX feed and Yahoo Finance fallback."""
        # Try Alpaca with IEX feed first
        try:
            end = datetime.now(timezone.utc)
            bars = self.data_client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=[symbol],
                    timeframe=TimeFrame.Minute,
                    start=end - timedelta(days=1),
                    end=end,
                    limit=limit,
                    feed='iex'  # Use IEX feed instead of SIP
                )
            )
            
            result = []
            for bar in bars:
                result.append({
                    'close': float(bar.c),
                    'volume': int(bar.v),
                    'timestamp': bar.t.isoformat() if bar.t else None
                })
            logger.info(f"Successfully fetched {len(result)} bars for {symbol} from Alpaca IEX")
            return result
        except Exception as e:
            logger.warning(f"Alpaca IEX data failed for {symbol}: {e}. Trying Yahoo Finance...")
        
        # Fallback to Yahoo Finance
        try:
            ticker = self.yf_client(symbol)
            hist = ticker.history(period="1d", interval="1m")
            
            if hist.empty:
                logger.warning(f"No data from Yahoo Finance for {symbol}")
                return []
            
            result = []
            for idx, row in hist.iterrows():
                result.append({
                    'close': float(row['Close']),
                    'volume': int(row['Volume']),
                    'timestamp': idx.isoformat()
                })
            
            logger.info(f"Successfully fetched {len(result)} bars for {symbol} from Yahoo Finance")
            return result
        except Exception as e:
            logger.error(f"Error getting price data for {symbol} from Yahoo Finance: {e}")
            return []
    
    def _calculate_rsi(self, prices: List[float], period: int = 14) -> float:
        """Calculate RSI indicator."""
        if len(prices) < period + 1:
            return 50.0  # Neutral if not enough data
        
        gains = []
        losses = []
        
        for i in range(1, len(prices)):
            change = prices[i] - prices[i-1]
            if change > 0:
                gains.append(change)
                losses.append(0)
            else:
                gains.append(0)
                losses.append(abs(change))
        
        avg_gain = sum(gains[-period:]) / period
        avg_loss = sum(losses[-period:]) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return rsi
    
    def _calculate_bollinger_bands(self, prices: List[float], period: int = 20, num_std: float = 2.0) -> Tuple[float, float, float]:
        """Calculate Bollinger Bands."""
        if len(prices) < period:
            return prices[-1], prices[-1], prices[-1]  # S, Lower, Upper
        
        recent_prices = prices[-period:]
        middle = sum(recent_prices) / period
        std = (sum((p - middle) ** 2 for p in recent_prices) / period) ** 0.5
        
        upper = middle + (num_std * std)
        lower = middle - (num_std * std)
        
        return upper, middle, lower
    
    def _get_state_features(self, symbol: str) -> Dict[str, float]:
        """Get features for ML state."""
        price_data = self._get_price_data(symbol, limit=50)
        
        if len(price_data) < 20:
            return {'rsi': 50.0, 'momentum': 0.0, 'bb_position': 0.5, 'volume_ratio': 1.0}
        
        closes = [p['close'] for p in price_data]
        
        # RSI
        rsi = self._calculate_rsi(closes)
        
        # Momentum (price change over lookback period)
        momentum = (closes[-1] - closes[-self.params.lookback_period]) / closes[-self.params.lookback_period]
        
        # Bollinger Band position
        bb_upper, bb_middle, bb_lower = self._calculate_bollinger_bands(closes)
        bb_position = (closes[-1] - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5
        
        # Volume ratio (current vs average)
        volumes = [p['volume'] for p in price_data]
        volume_ratio = volumes[-1] / (sum(volumes[-20:]) / 20) if volumes[-20:] else 1.0
        
        return {
            'rsi': rsi / 100.0,  # Normalize to 0-1
            'momentum': min(max(momentum, -0.1), 0.1),  # Normalize to -0.1 to 0.1
            'bb_position': min(max(bb_position, 0), 1),  # Normalize to 0-1
            'volume_ratio': min(max(volume_ratio, 0.5), 2.0)  # Normalize to 0.5-2.0
        }
    
    def _get_q_value(self, state: Dict[str, float], action: str) -> float:
        """Get Q-value for state-action pair."""
        state_key = str(sorted(state.items()))
        if state_key not in self.q_table:
            self.q_table[state_key] = {a: 0.0 for a in self.action_space}
        
        return self.q_table[state_key].get(action, 0.0)
    
    def _update_q_value(self, state: Dict[str, float], action: str, reward: float, next_state: Dict[str, float]):
        """Update Q-value using Q-learning update rule."""
        state_key = str(sorted(state.items()))
        next_state_key = str(sorted(next_state.items()))
        
        if state_key not in self.q_table:
            self.q_table[state_key] = {a: 0.0 for a in self.action_space}
        if next_state_key not in self.q_table:
            self.q_table[next_state_key] = {a: 0.0 for a in self.action_space}
        
        current_q = self.q_table[state_key][action]
        max_next_q = max(self.q_table[next_state_key].values())
        
        new_q = current_q + self.params.learning_rate * (reward + self.params.reward_decay * max_next_q - current_q)
        self.q_table[state_key][action] = new_q
    
    def _choose_action(self, state: Dict[str, float]) -> str:
        """Choose action based on Q-table with exploration."""
        if np.random.random() < self.params.exploration_rate:
            return np.random.choice(self.action_space)
        
        state_key = str(sorted(state.items()))
        if state_key not in self.q_table:
            return 'HOLD'
        
        q_values = self.q_table[state_key]
        return max(q_values, key=q_values.get)
    
    def _calculate_reward(self, trade_result: TradeRecord, prev_state: Dict[str, float], new_state: Dict[str, float]) -> float:
        """Calculate reward for trade."""
        reward = 0.0
        
        # Base reward from PnL
        if trade_result.success:
            reward += trade_result.pnl / 100.0  # Normalize PnL
        
        # Bonus for winning trades
        if trade_result.pnl > 0:
            reward += 1.0
        
        # Penalty for losing trades
        elif trade_result.pnl < 0:
            reward -= 1.0
        
        # Reward for good risk management
        if abs(trade_result.pnl) < self.params.position_size_usd * 0.02:  # Small loss
            reward += 0.5
        
        # Penalty for large losses
        if abs(trade_result.pnl) > self.params.position_size_usd * 0.05:  # Large loss
            reward -= 2.0
        
        return reward
    
    def _execute_market_order(self, symbol: str, side: OrderSide, qty: float):
        """Execute market order."""
        try:
            order_request = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY
            )
            
            order = self.trading_client.submit_order(order_request)
            logger.info(f"Order submitted: {side.value} {qty} {symbol} @ market")
            return order
        except Exception as e:
            logger.error(f"Error executing order: {e}")
            return None
    
    def _check_risk_management(self) -> bool:
        """Check risk management rules."""
        # Check daily stop loss
        if self.daily_pnl < -self.params.daily_stop_loss:
            logger.warning(f"Daily stop loss reached: ${self.daily_pnl:.2f}")
            return False
        
        # Check max drawdown
        account_balance = float(self._get_account_balance())
        if account_balance > 0:
            drawdown = (self.params.position_size_usd * self.params.max_positions - account_balance) / account_balance
            if drawdown > self.params.max_drawdown:
                logger.warning(f"Max drawdown reached: {drawdown:.2%}")
                return False
        
        # Check max positions
        current_positions = self._get_current_positions()
        if len(current_positions) >= self.params.max_positions:
            logger.info(f"Max positions reached: {len(current_positions)}")
            return False
        
        return True
    
    def _execute_emergency_exit(self):
        """Emergency exit all positions."""
        logger.warning("Executing emergency exit...")
        positions = self._get_current_positions()
        
        for pos in positions:
            if pos['side'] == 'long':
                self._execute_market_order(pos['symbol'], OrderSide.SELL, pos['qty'])
                logger.info(f"Emergency exit: SELL {pos['qty']} {pos['symbol']}")
    
    def _calculate_daily_pnl(self) -> float:
        """Calculate daily PnL."""
        positions = self._get_current_positions()
        daily_pnl = 0.0
        
        for pos in positions:
            daily_pnl += float(pos['unrealized_pl'])
        
        return daily_pnl
    
    def _update_trading_state(self):
        """Update trading state and performance metrics."""
        self.daily_pnl = self._calculate_daily_pnl()
        
        # Update performance metrics
        if self.trade_history:
            wins = [t for t in self.trade_history if t.pnl > 0]
            losses = [t for t in self.trade_history if t.pnl <= 0]
            
            self.performance_metrics['total_trades'] = len(self.trade_history)
            self.performance_metrics['winning_trades'] = len(wins)
            self.performance_metrics['losing_trades'] = len(losses)
            self.performance_metrics['win_rate'] = len(wins) / len(self.trade_history) if self.trade_history else 0
            
            total_wins = sum(t.pnl for t in wins)
            total_losses = abs(sum(t.pnl for t in losses))
            self.performance_metrics['profit_factor'] = total_wins / total_losses if total_losses > 0 else float('inf')
    
    def _retrain_model(self):
        """Retrain ML model based on recent performance."""
        logger.info("Retraining ML model...")
        
        # Adjust learning rate based on performance
        if self.performance_metrics['win_rate'] < self.params.target_win_rate:
            self.params.learning_rate *= 1.1  # Increase learning rate
        else:
            self.params.learning_rate *= 0.9  # Decrease learning rate
        
        # Adjust exploration rate
        if self.performance_metrics['profit_factor'] < self.params.target_profit_factor:
            self.params.exploration_rate = min(self.params.exploration_rate * 1.1, 0.3)
        else:
            self.params.exploration_rate = max(self.params.exploration_rate * 0.9, 0.05)
        
        # Log learning progress
        self.learning_log.append({
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'win_rate': self.performance_metrics['win_rate'],
            'profit_factor': self.performance_metrics['profit_factor'],
            'learning_rate': self.params.learning_rate,
            'exploration_rate': self.params.exploration_rate
        })
        
        logger.info(f"Model retrained. Win Rate: {self.performance_metrics['win_rate']:.2%}, "
                   f"Profit Factor: {self.performance_metrics['profit_factor']:.2f}")
    
    def _make_trading_decision(self, symbol: str) -> str:
        """Make trading decision using ML model."""
        state = self._get_state_features(symbol)
        
        # Get current positions for this symbol
        positions = self._get_current_positions()
        current_position = next((p for p in positions if p['symbol'] == symbol), None)
        
        # If we have a position, consider selling
        if current_position:
            # Calculate potential reward for selling
            sell_reward = self._get_q_value(state, 'SELL')
            hold_reward = self._get_q_value(state, 'HOLD')
            
            if sell_reward > hold_reward:
                return 'SELL'
            else:
                return 'HOLD'
        
        # If no position, consider buying
        buy_reward = self._get_q_value(state, 'BUY')
        hold_reward = self._get_q_value(state, 'HOLD')
        
        if buy_reward > hold_reward:
            return 'BUY'
        else:
            return 'HOLD'
    
    async def run_trading_loop(self):
        """Main async trading loop with learning capabilities."""
        logger.info("=" * 60)
        logger.info("SELF-IMPROVING TRADING LOOP STARTED")
        logger.info("=" * 60)
        self._update_bot_status("RUNNING")
        
        iteration = 0
        while self.bot_status == "RUNNING":
            iteration += 1
            logger.info(f"--- Trading loop iteration {iteration} ---")
            
            try:
                self._check_daily_reset()
                
                if self._check_risk_management():
                    for symbol in self.trading_symbols:
                        try:
                            logger.info(f"Processing symbol: {symbol}")
                            
                            # Get current state
                            prev_state = self._get_state_features(symbol)
                            
                            # Make trading decision
                            action = self._make_trading_decision(symbol)
                            logger.info(f"Decision for {symbol}: {action}")
                            
                            # Execute action
                            if action == 'BUY':
                                price_data = self._get_price_data(symbol, limit=10)
                                if price_data:
                                    current_price = price_data[-1]['close']
                                    order_qty = self.params.position_size_usd / current_price
                                    
                                    order = self._execute_market_order(symbol, OrderSide.BUY, order_qty)
                                    if order:
                                        trade = TradeRecord(
                                            symbol=symbol,
                                            side='BUY',
                                            price=current_price,
                                            quantity=order_qty,
                                            timestamp=datetime.now(timezone.utc).isoformat(),
                                            strategy=self.params.strategy_type
                                        )
                                        self.trade_history.append(trade)
                                        
                                        # Update Q-table with reward
                                        new_state = self._get_state_features(symbol)
                                        reward = self._calculate_reward(trade, prev_state, new_state)
                                        self._update_q_value(prev_state, 'BUY', reward, new_state)
                                        
                                        logger.info(f"BUY executed: {order_qty:.4f} {symbol} @ ${current_price:.2f}")
                            
                            elif action == 'SELL':
                                positions = self._get_current_positions()
                                position = next((p for p in positions if p['symbol'] == symbol), None)
                                
                                if position:
                                    order = self._execute_market_order(symbol, OrderSide.SELL, position['qty'])
                                    if order:
                                        trade = TradeRecord(
                                            symbol=symbol,
                                            side='SELL',
                                            price=position['avg_entry_price'],
                                            quantity=position['qty'],
                                            timestamp=datetime.now(timezone.utc).isoformat(),
                                            pnl=position['unrealized_pl'],
                                            strategy=self.params.strategy_type
                                        )
                                        self.trade_history.append(trade)
                                        
                                        # Update Q-table with reward
                                        new_state = self._get_state_features(symbol)
                                        reward = self._calculate_reward(trade, prev_state, new_state)
                                        self._update_q_value(prev_state, 'SELL', reward, new_state)
                                        
                                        logger.info(f"SELL executed: {position['qty']} {symbol} @ ${position['avg_entry_price']:.2f}")
                                        
                            # Update trading state
                            self._update_trading_state()
                            
                            # Check if it's time to retrain model
                            self.trades_since_last_retrain += 1
                            if self.trades_since_last_retrain >= self.params.ml_retrain_frequency:
                                self._retrain_model()
                                self.trades_since_last_retrain = 0
                        
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
    
    def update_params(self, new_params: dict):
        """Update bot parameters dynamically."""
        try:
            for key, value in new_params.items():
                if hasattr(self.params, key):
                    setattr(self.params, key, value)
                    logger.info(f"Parameter updated: {key} = {value}")
            
            self._update_trading_state()
            return True
        except Exception as e:
            logger.error(f"Error updating parameters: {e}")
            return False
    
    def get_performance_report(self) -> dict:
        """Get comprehensive performance report."""
        return {
            'performance_metrics': self.performance_metrics,
            'current_params': asdict(self.params),
            'trade_history_count': len(self.trade_history),
            'learning_log': self.learning_log[-10:],  # Last 10 learning entries
            'q_table_size': len(self.q_table),
            'bot_status': self.bot_status,
            'daily_pnl': self.daily_pnl,
            'timestamp': datetime.now(timezone.utc).isoformat()
        }


def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("SELF-IMPROVING TRADING BOT STARTING")
    logger.info("=" * 60)
    
    bot = TradingBot()
    
    logger.info(f"Bot initialized with strategy: {bot.params.strategy_type}")
    logger.info(f"Symbols: {bot.trading_symbols}")
    logger.info(f"Learning rate: {bot.params.learning_rate}")
    logger.info(f"Exploration rate: {bot.params.exploration_rate}")
    
    try:
        logger.info("Starting self-improving trading loop...")
        asyncio.run(bot.run_trading_loop())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user (KeyboardInterrupt)")
        bot.kill_bot()
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}", exc_info=True)
        bot.kill_bot()


if __name__ == "__main__":
    main()
