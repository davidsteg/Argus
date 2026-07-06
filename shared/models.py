"""
Database Models and Helpers for Trading Bot
"""
import sqlite3
import json
import logging
from datetime import datetime, timezone
from typing import List, Dict, Optional
import os

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


class Database:
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = os.getenv('DB_PATH', '/data/trading_state.db')
        self.db_path = db_path
        self.conn = None
        self.cursor = None
        logger.info(f"Database initialized at {db_path}")
    
    def connect(self):
        """Establish database connection."""
        logger.debug(f"Connecting to database: {self.db_path}")
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.cursor = self.conn.cursor()
        return self
    
    def close(self):
        """Close database connection."""
        if self.conn:
            logger.debug("Closing database connection")
            self.conn.close()
    
    def initialize(self):
        """Create tables if they don't exist."""
        logger.info("Initializing database tables")
        self.connect()
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS trading_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                balance REAL NOT NULL,
                daily_pnl REAL NOT NULL DEFAULT 0,
                bot_status TEXT NOT NULL DEFAULT 'STOPPED',
                positions TEXT NOT NULL,
                orders TEXT NOT NULL
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS log_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                source TEXT
            )
        ''')
        
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume INTEGER NOT NULL
            )
        ''')
        
        self.conn.commit()
        logger.info("Database tables created successfully")
        self.close()
    
    def update_trading_state(self, balance: float, positions: List[Dict], 
                            orders: List[Dict], daily_pnl: float = 0, 
                            bot_status: str = 'STOPPED'):
        """Update trading state in database."""
        self.connect()
        
        # Serialize positions and orders
        positions_json = json.dumps(positions)
        orders_json = json.dumps(orders)
        
        self.cursor.execute('''
            INSERT INTO trading_state (timestamp, balance, daily_pnl, bot_status, positions, orders)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            balance,
            daily_pnl,
            bot_status,
            positions_json,
            orders_json
        ))
        
        self.conn.commit()
        self.close()
    
    def update_bot_status(self, status: str):
        """Update bot status."""
        self.connect()
        
        self.cursor.execute('''
            INSERT INTO trading_state (timestamp, balance, daily_pnl, bot_status, positions, orders)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now(timezone.utc).isoformat(),
            0.0,  # Balance will be updated separately
            0.0,  # PnL will be updated separately
            status,
            '[]',  # Empty positions
            '[]'   # Empty orders
        ))
        
        self.conn.commit()
        self.close()
    
    def log_entry(self, level: str, message: str, source: str = None):
        """Add a log entry to the database."""
        logger.debug(f"Adding log entry: [{level.upper()}] {message} (source: {source})")
        self.connect()
        
        try:
            self.cursor.execute('''
                INSERT INTO log_entries (timestamp, level, message, source)
                VALUES (?, ?, ?, ?)
            ''', (
                datetime.now(timezone.utc).isoformat(),
                level.upper(),
                message,
                source
            ))
            
            self.conn.commit()
            logger.debug("Log entry added successfully")
        except Exception as e:
            logger.error(f"Failed to add log entry: {str(e)}", exc_info=True)
            raise
        finally:
            self.close()
    
    def get_latest_state(self) -> Dict:
        """Get the latest trading state."""
        logger.debug("Fetching latest trading state")
        self.connect()
        
        try:
            self.cursor.execute('''
                SELECT * FROM trading_state 
                ORDER BY id DESC 
                LIMIT 1
            ''')
            
            row = self.cursor.fetchone()
            
            if row:
                state = {
                    'timestamp': row[1],
                    'balance': row[2],
                    'daily_pnl': row[3],
                    'bot_status': row[4],
                    'positions': json.loads(row[5]),
                    'orders': json.loads(row[6])
                }
                logger.debug(f"Latest state: balance=${state['balance']:.2f}, status={state['bot_status']}")
            else:
                state = {
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'balance': 0.0,
                    'daily_pnl': 0.0,
                    'bot_status': 'STOPPED',
                    'positions': [],
                    'orders': []
                }
                logger.info("No trading state found in database")
            
            return state
        except Exception as e:
            logger.error(f"Failed to fetch trading state: {str(e)}", exc_info=True)
            raise
        finally:
            self.close()
    
    def get_recent_logs(self, limit: int = 20) -> List[Dict]:
        """Get recent log entries."""
        logger.debug(f"Fetching {limit} recent log entries")
        self.connect()
        
        try:
            self.cursor.execute('''
                SELECT * FROM log_entries 
                ORDER BY id DESC 
                LIMIT ?
            ''', (limit,))
            
            rows = self.cursor.fetchall()
            
            logs = []
            for row in reversed(rows):  # Reverse to get chronological order
                logs.append({
                    'timestamp': row[1],
                    'level': row[2],
                    'message': row[3],
                    'source': row[4]
                })
            
            logger.debug(f"Retrieved {len(logs)} log entries")
            return logs
        except Exception as e:
            logger.error(f"Failed to fetch logs: {str(e)}", exc_info=True)
            raise
        finally:
            self.close()
    
    def save_price_data(self, symbol: str, data: List[Dict]):
        """Save price history data."""
        self.connect()
        
        for item in data:
            self.cursor.execute('''
                INSERT INTO price_history (symbol, timestamp, open, high, low, close, volume)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (
                symbol,
                item['timestamp'],
                item['open'],
                item['high'],
                item['low'],
                item['close'],
                item['volume']
            ))
        
        self.conn.commit()
        self.close()
    
    def get_price_history(self, symbol: str, limit: int = 100) -> List[Dict]:
        """Get price history for a symbol."""
        self.connect()
        
        self.cursor.execute('''
            SELECT * FROM price_history 
            WHERE symbol = ? 
            ORDER BY id DESC 
            LIMIT ?
        ''', (symbol, limit))
        
        rows = self.cursor.fetchall()
        
        prices = []
        for row in reversed(rows):  # Reverse to get chronological order
            prices.append({
                'timestamp': row[2],
                'open': row[3],
                'high': row[4],
                'low': row[5],
                'close': row[6],
                'volume': row[7]
            })
        
        self.close()
        return prices


def initialize_database():
    """Initialize database tables."""
    db = Database()
    db.initialize()


if __name__ == "__main__":
    initialize_database()
    print("Database initialized successfully")