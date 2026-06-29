"""
Mystic-Bot configuration.

Everything you might want to tweak lives here. The two files you must provide
yourself are:

    private/key.txt      -> your Alpaca API key id
    private/secret.txt   -> your Alpaca API secret key

Generate them from the Alpaca dashboard. Use PAPER keys while testing.
"""

from pathlib import Path

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
PRIVATE_DIR = BASE_DIR / "private"
LOG_DIR = BASE_DIR / "logs"
ACTIVITY_LOG = LOG_DIR / "activity.csv"   # one row per order submitted
RUN_LOG = LOG_DIR / "runs.log"            # free-text run history

# --------------------------------------------------------------------------- #
# Trading mode
# --------------------------------------------------------------------------- #
# PAPER = True  -> simulated money, no risk. Leave this on until you trust it.
# PAPER = False -> REAL money. You will also be asked to confirm at runtime.
PAPER = True

# --------------------------------------------------------------------------- #
# Target portfolio: 10 names, 10% each.
# Map of ticker -> (display name, target weight). Weights must sum to 1.0.
# --------------------------------------------------------------------------- #
PORTFOLIO = {
    "KO":   ("Coca-Cola Co",          0.10),
    "MCD":  ("McDonald's Corp",       0.10),
    "AAPL": ("Apple Inc",             0.10),
    "TSLA": ("Tesla Inc",             0.10),
    "CL":   ("Colgate-Palmolive Co",  0.10),
    "PG":   ("Procter & Gamble Co",   0.10),
    "WM":   ("Waste Management Inc",  0.10),
    "WMT":  ("Walmart Inc",           0.10),
    "NVDA": ("NVIDIA Corp",           0.10),
    "JNJ":  ("Johnson & Johnson",     0.10),
}

TARGET_WEIGHTS = {sym: w for sym, (_, w) in PORTFOLIO.items()}
DISPLAY_NAMES = {sym: name for sym, (name, _) in PORTFOLIO.items()}

# --------------------------------------------------------------------------- #
# Deployment rules
# --------------------------------------------------------------------------- #
# Keep this much cash untouched (e.g. for fees / buffer). 0 = invest everything.
CASH_RESERVE_USD = 0.0

# Don't bother running a rebalance unless at least this much investable cash
# has accumulated. Stops the bot churning over loose change / pending dividends.
MIN_DEPLOY_USD = 5.0

# Alpaca's minimum notional (dollar) order is $1. Buys smaller than this for a
# single name are skipped and the money rolls into the next deposit.
MIN_ORDER_USD = 1.0

# In --watch mode, how often to check for new cash (seconds).
WATCH_INTERVAL_SECONDS = 60

# If True, the bot will still place orders when the market is closed. Fractional
# / notional orders are accepted in extended + overnight sessions on Alpaca, but
# fills can be partial or delayed. False = wait for the regular session.
ALLOW_OUTSIDE_REGULAR_HOURS = True

# --------------------------------------------------------------------------- #
# Dashboard
# --------------------------------------------------------------------------- #
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 5000
DASHBOARD_REFRESH_SECONDS = 20


def load_credentials():
    """Read API key + secret from the private/ folder. Raises a clear error
    if either file is missing or empty."""
    key_file = PRIVATE_DIR / "key.txt"
    secret_file = PRIVATE_DIR / "secret.txt"

    missing = [str(f) for f in (key_file, secret_file) if not f.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing credential file(s):\n  "
            + "\n  ".join(missing)
            + "\n\nPut your Alpaca key in private/key.txt and secret in "
              "private/secret.txt."
        )

    key = key_file.read_text(encoding="utf-8").strip()
    secret = secret_file.read_text(encoding="utf-8").strip()

    if not key or not secret:
        raise ValueError("key.txt and/or secret.txt is empty.")

    return key, secret
