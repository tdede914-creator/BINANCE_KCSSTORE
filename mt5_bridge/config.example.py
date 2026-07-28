"""Copy this file to `config.py` and fill in the blanks.

`config.py` is git-ignored so your credentials stay local to the VPS.
"""

# -----------------------------------------------------------------------------
# Backend connection
# -----------------------------------------------------------------------------
# The URL your Linux VPS is serving the KCS backend on. Include the port.
BACKEND_URL = "http://43.128.118.89:8000"

# Copy this from the web dashboard: Settings → "MT5 Bridge" card → Secret.
# Auto-generated on first backend read; treat it like a password.
BRIDGE_SECRET = "PASTE_ME_FROM_WEB_SETTINGS"

# -----------------------------------------------------------------------------
# MT5 / Exness credentials
# -----------------------------------------------------------------------------
# From your Exness account. Server name is on the MT5 login screen
# (e.g. "Exness-MT5Trial5" for demo, "Exness-MT5Real14" for real).
MT5_LOGIN = 12345678
MT5_PASSWORD = "your_mt5_password"
MT5_SERVER = "Exness-MT5Trial5"

# -----------------------------------------------------------------------------
# Risk / execution controls
# -----------------------------------------------------------------------------
# Percentage of account balance to risk per trade (loss when SL hits).
# 1.0 = 1%. Recommended: 0.5-1.0 while validating.
RISK_PER_TRADE_PCT = 1.0

# Hard cap on lot size — safety net in case sizing math goes wrong on
# an unusual symbol. Exness minimums are usually 0.01 (fx), 0.01 (gold).
MAX_LOT_PER_TRADE = 0.5

# Which symbols the bridge is allowed to execute. Leave the list empty
# to accept everything the strategy sends. Example:
#     SYMBOLS_ALLOWLIST = ["XAUUSD", "EURUSD", "US500"]
SYMBOLS_ALLOWLIST: list[str] = []

# -----------------------------------------------------------------------------
# Bridge behaviour
# -----------------------------------------------------------------------------
# How often (seconds) to poll the backend for new signals + check for
# closed positions. 5-10 is a good default.
POLL_INTERVAL_SECONDS = 5

# Set True to skip real order_send() — the bridge logs what WOULD happen
# but never risks real money. Perfect for a first smoke test.
DRY_RUN = True
