from pathlib import Path

BASE = Path(".")
DATA_DIR = BASE / "data"
DICT_PATH = DATA_DIR / "torn_item_dictionary.csv"

BASE_URL = "https://api.torn.com/v2"
MAX_WORKERS = 5
RATE_LIMIT_PER_MIN = 90
RETRIES = 3
TIMEOUT = 15
MARKET_FEE = 0.05
FUZZY_THRESHOLD = 80