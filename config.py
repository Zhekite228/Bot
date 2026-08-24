import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

_default_db_path = DATA_DIR / "race_results.db"
_legacy_db_path = BASE_DIR / "race_results.db"
if _legacy_db_path.exists() and not _default_db_path.exists():
    _legacy_db_path.rename(_default_db_path)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "")
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", _default_db_path))
MIN_LAP_TIME_SECONDS = 38.5
MAX_LAP_SPEED = 300
DEFAULT_CAR_RANK = "S"

TRACKS: dict[str, str] = {
    "obiezdnaya": "Обьездная",
    "proseka": "Просека",
    "ferma": "ферма",
    "gonochnaya": "Гоночная трасса",
}

CAR_CLASSES: dict[str, str] = {
    "all": "Общий",
    "F": "F",
    "E": "E",
    "D": "D",
    "C": "C",
    "B": "B",
    "A": "A",
    "S": "S",
}

_admin_ids_raw = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {
    int(value.strip())
    for value in _admin_ids_raw.split(",")
    if value.strip().isdigit()
}

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN не задан в .env")
if not OCR_SPACE_API_KEY:
    raise RuntimeError("OCR_SPACE_API_KEY не задан в .env")
