"""
Streamlit Dashboard for Trading Bot
"""
import os
import sys
import time
import logging
import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import requests
import json

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Ensure shared models module is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

# API configuration
API_BASE_URL = os.getenv('API_BASE_URL', 'http://backend:8000')
logger.info(f"Frontend starting - API URL: {API_BASE_URL}")

st.set_page_config(
    page_title="Trading Bot Dashboard",
    page_icon="🤖",
    layout="wide"
)

st.title("🤖 Trading Bot Dashboard")

# Sidebar for controls
st.sidebar.header("Controls")

col1, col2 = st.sidebar.columns(2)

with col1:
    if st.button("🚀 Start Bot"):
        logger.info("User clicked Start Bot")
        try:
            response = requests.post(f"{API_BASE_URL}/start", timeout=5)
            logger.info(f"Start bot response: {response.status_code}")
            if response.status_code == 200:
                st.sidebar.success("Bot started!")
            else:
                error_msg = response.json().get('detail', 'Unknown error')
                logger.error(f"Start bot failed: {error_msg}")
                st.sidebar.error(f"Error: {error_msg}")
        except Exception as e:
            logger.error(f"Start bot connection error: {str(e)}")
            st.sidebar.error(f"Connection error: {str(e)}")

with col2:
    if st.button("⏹️ Stop Bot"):
        logger.info("User clicked Stop Bot")
        try:
            response = requests.post(f"{API_BASE_URL}/stop", timeout=5)
            logger.info(f"Stop bot response: {response.status_code}")
            if response.status_code == 200:
                st.sidebar.success("Bot stopped!")
            else:
                error_msg = response.json().get('detail', 'Unknown error')
                logger.error(f"Stop bot failed: {error_msg}")
                st.sidebar.error(f"Error: {error_msg}")
        except Exception as e:
            logger.error(f"Stop bot connection error: {str(e)}")
            st.sidebar.error(f"Connection error: {str(e)}")

if st.sidebar.button("💀 Kill Bot (Emergency)"):
    if st.sidebar.confirm("Are you sure? This will liquidate all positions!"):
        logger.warning("User clicked Emergency Kill Bot")
        try:
            response = requests.post(f"{API_BASE_URL}/kill", timeout=5)
            logger.info(f"Kill bot response: {response.status_code}")
            if response.status_code == 200:
                st.sidebar.success("Bot killed! All positions liquidated.")
            else:
                error_msg = response.json().get('detail', 'Unknown error')
                logger.error(f"Kill bot failed: {error_msg}")
                st.sidebar.error(f"Error: {error_msg}")
        except Exception as e:
            logger.error(f"Kill bot connection error: {str(e)}")
            st.sidebar.error(f"Connection error: {str(e)}")

st.sidebar.markdown("---")
st.sidebar.header("Manual Order")

with st.sidebar.form("order_form"):
    symbol = st.text_input("Symbol", "AAPL").upper()
    side = st.selectbox("Side", ["BUY", "SELL"])
    quantity = st.number_input("Quantity", min_value=0.01, value=1.0, step=0.1)
    submitted = st.form_submit_button("Place Order")
    
    if submitted:
        logger.info(f"User placed order: {side} {quantity} {symbol}")
        try:
            response = requests.post(f"{API_BASE_URL}/order", json={
                "symbol": symbol,
                "side": side.lower(),
                "quantity": quantity
            }, timeout=5)
            logger.info(f"Order response: {response.status_code}")
            if response.status_code == 200:
                st.sidebar.success(f"Order placed: {side} {quantity} {symbol}")
            else:
                error_msg = response.json().get('detail', 'Unknown error')
                logger.error(f"Order failed: {error_msg}")
                st.sidebar.error(f"Error: {error_msg}")
        except Exception as e:
            logger.error(f"Order connection error: {str(e)}")
            st.sidebar.error(f"Connection error: {str(e)}")

# Main content
st.header("Trading State")

try:
    logger.info("Fetching bot status")
    response = requests.get(f"{API_BASE_URL}/status", timeout=5)
    logger.info(f"Status response: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        
        # Status indicator
        status_color = "🟢" if data['status'] == "RUNNING" else "🔴"
        st.metric(label="Bot Status", value=f"{status_color} {data['status']}")
        
        # Metrics row
        col1, col2, col3 = st.columns(3)
        col1.metric(label="Balance", value=f"${data['balance']:,.2f}", delta=f"{data['daily_pnl']:.2f}")
        col2.metric(label="Open Positions", value=len(data['positions']))
        col3.metric(label="Recent Orders", value=len(data['orders']))
        
        # Positions table
        st.subheader("Current Positions")
        if data['positions']:
            positions_df = pd.DataFrame(data['positions'])
            if 'unrealized_pnl' in positions_df.columns:
                positions_df['unrealized_pnl'] = positions_df['unrealized_pnl'].apply(lambda x: f"${x:,.2f}")
            if 'market_value' in positions_df.columns:
                positions_df['market_value'] = positions_df['market_value'].apply(lambda x: f"${x:,.2f}")
            if 'change' in positions_df.columns:
                positions_df['change'] = positions_df['change'].apply(lambda x: f"{x:.2%}")
            st.dataframe(positions_df, use_container_width=True)
        else:
            st.info("No open positions")
        
        # Orders table
        st.subheader("Recent Orders")
        if data['orders']:
            orders_df = pd.DataFrame(data['orders'])
            if 'filled_avg_price' in orders_df.columns:
                orders_df['filled_avg_price'] = orders_df['filled_avg_price'].apply(lambda x: f"${x:,.2f}")
            st.dataframe(orders_df, use_container_width=True)
        else:
            st.info("No recent orders")
        
    else:
        error_msg = response.json().get('detail', 'Unknown error')
        logger.error(f"Status fetch failed: {error_msg}")
        st.error(f"Failed to get status: {error_msg}")
except requests.exceptions.ConnectionError:
    logger.error("Cannot connect to trading bot backend")
    st.error("Cannot connect to trading bot backend. Make sure the backend container is running.")
    st.info("API URL: " + API_BASE_URL)
except Exception as e:
    logger.error(f"Error fetching status: {str(e)}")
    st.error(f"Error: {str(e)}")

# Logs section
st.header("Recent Logs")
try:
    logger.info("Fetching logs")
    response = requests.get(f"{API_BASE_URL}/logs?limit=20", timeout=5)
    logger.info(f"Logs response: {response.status_code}")
    
    if response.status_code == 200:
        logs = response.json()
        logger.info(f"Received {len(logs)} log entries")
        logs_df = pd.DataFrame(logs)
        if not logs_df.empty:
            st.dataframe(logs_df, use_container_width=True)
        else:
            st.info("No logs available")
    else:
        logger.error("Failed to fetch logs")
        st.error("Failed to get logs")
except requests.exceptions.ConnectionError:
    logger.error("Cannot connect to backend for logs")
    st.error("Cannot connect to trading bot backend")
except Exception as e:
    logger.error(f"Error fetching logs: {str(e)}")
    st.error(f"Error: {str(e)}")

# Auto-refresh
st.caption("Auto-refreshes every 5 seconds")
time.sleep(5)
st.rerun()
