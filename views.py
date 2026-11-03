import logging
import math
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from pymongo.errors import PyMongoError
from apscheduler.schedulers.background import BackgroundScheduler
from utils import (address_to_coords, get_forecast, save_forecast, load_forecast,
                   check_db, ensure_indexes, delete_past_forecasts,
                   save_plan, load_plan, delete_plan,
                   load_geocode, save_geocode, NOMINATIM_CONTACT)


_scheduler = None

# Every box the frontend shows. Quantities are 0 to MAX_PER_PIECE.
PIECES = ("t-shirt", "long-sleeve", "pants", "shorts",
          "second-skin", "coat", "heavy-coat", "gloves")
MAX_PER_PIECE = 16
MAX_DAYS = 31
MAX_ADDRESS_LENGTH = 200
TEMP_RANGE = (-50, 60)

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII)
CODE_RE = re.compile(r"[A-Za-z0-9_-]{22}")   # what secrets.token_urlsafe(16) produces


class StorageError(Exception):
    """The database is unavailable (or could not store/read a plan)."""


# ---------- input validation ----------
def validate_plan_input(body):
    """Checks the request body. Returns (itinerary, clothes, average_temp), where
    clothes always has every piece in PIECES (missing ones as 0).
    Raises ValueError with a message that is safe to show to the client."""
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")

    # itinerary: {"YYYY-MM-DD": "address"}
    itinerary = body.get("itinerary")
    if (not isinstance(itinerary, dict) or not itinerary
            or not all(isinstance(a, str) for a in itinerary.values())):
        raise ValueError("itinerary must be a non-empty object of date -> address")
    if len(itinerary) > MAX_DAYS:
        raise ValueError(f"itinerary can have at most {MAX_DAYS} days")
    for day, address in itinerary.items():
        try:
            if not DATE_RE.fullmatch(day):
                raise ValueError
            date_cls.fromisoformat(day)
        except ValueError:
            raise ValueError(f"invalid date '{day}', use YYYY-MM-DD") from None
        if not address.strip() or len(address) > MAX_ADDRESS_LENGTH:
            raise ValueError(f"address for {day} must have 1 to {MAX_ADDRESS_LENGTH} characters")

    # clothes: {"piece": quantity}
    clothes = body.get("clothes")
    if not isinstance(clothes, dict):
        raise ValueError("clothes must be an object of piece -> quantity")
    unknown = sorted(set(clothes) - set(PIECES))
    if unknown:
        raise ValueError(f"unknown pieces: {unknown}")
    stock = {}
    for piece in PIECES:
        quantity = clothes.get(piece, 0)
        if isinstance(quantity, bool) or not isinstance(quantity, int) \
                or not 0 <= quantity <= MAX_PER_PIECE:
            raise ValueError(f"{piece}: quantity must be an integer from 0 to {MAX_PER_PIECE}")
        stock[piece] = quantity

    # average_temp: number from the slider
    average_temp = body.get("average_temp")
    if isinstance(average_temp, bool) or not isinstance(average_temp, (int, float)):
        raise ValueError("average_temp must be a number")
    if not math.isfinite(average_temp) or not TEMP_RANGE[0] <= average_temp <= TEMP_RANGE[1]:
        raise ValueError(f"average_temp must be between {TEMP_RANGE[0]} and {TEMP_RANGE[1]}")

    return itinerary, stock, average_temp


# ---------- saved plans (database access always goes through here) ----------
def is_valid_code(code):
    return isinstance(code, str) and CODE_RE.fullmatch(code) is not None


def save_new_plan(input_data, plan):
    """Stores a plan and returns its random code. Raises StorageError if the database fails."""
    try:
        return save_plan(input_data, plan)
    except PyMongoError as e:
        logging.error("Could not save plan: %s", e)   # never log the code itself
        raise StorageError() from e


def get_saved_plan(code):
    """Returns the plan, or None if the code is malformed, unknown or expired.
    Raises StorageError if the database fails."""
    if not is_valid_code(code):
        return None                                   # no database query for obvious junk
    try:
        return load_plan(code)
    except PyMongoError as e:
        logging.error("Could not read plan: %s", e)
        raise StorageError() from e


def delete_saved_plan(code):
    """Returns True if the plan existed and was deleted. Raises StorageError if the database fails."""
    if not is_valid_code(code):
        return False
    try:
        return delete_plan(code)
    except PyMongoError as e:
        logging.error("Could not delete plan: %s", e)
        raise StorageError() from e


# ---------- forecast ----------
def _normalize(address):
    """'  Copacabana,  RIO ' -> 'copacabana, rio' so trivial differences count as copies."""
    return " ".join(address.lower().split())



def _coords_for(addr):
    """Coordinates of a normalized address, read from the cache when possible."""
    try:
        cached = load_geocode(addr)
    except PyMongoError as e:
        logging.warning("Could not read geocode cache: %s", e)
        cached = None
    if cached:
        return cached
    lat, lon = address_to_coords(addr)
    try:
        save_geocode(addr, lat, lon)
    except PyMongoError as e:
        logging.warning("Could not save geocode: %s", e)
    return lat, lon


def forecast_for_itinerary(itinerary):
    """{date: address} -> {date: {"address", "max", "min", "rain"}}.
    max, min and rain are None when the date is outside Open-Meteo's 16-day range.
    Raises ValueError (address not found) or requests.RequestException (API down)."""

    # 1) Remove copies: one entry per distinct address, with the dates needed there
    dates_by_addr = {}
    for date, addr in itinerary.items():
        dates_by_addr.setdefault(_normalize(addr), set()).add(date)

    # 2) Per distinct address: coordinates first (cached), then the forecast
    # Only dates Open-Meteo can return need to be in the cache (1 day of margin for time zones)
    today = datetime.now(timezone.utc).date()
    window = {(today + timedelta(days=i)).isoformat() for i in range(-1, 17)}
    forecasts = {}
    for addr, dates in dates_by_addr.items():
        lat, lon = _coords_for(addr)

        try:
            cached = load_forecast(lat, lon, dates)
        except PyMongoError as e:
            logging.warning("Could not read forecast cache: %s", e)
            cached = {}

        if (dates & window) <= set(cached):
            forecasts[addr] = cached
        else:
            forecasts[addr] = get_forecast(lat, lon)
            try:
                save_forecast(lat, lon, forecasts[addr])
            except PyMongoError as e:
                logging.warning("Could not save forecast: %s", e)

    # 3) Link each day to its address and that address's max, min and rain
    result = {}
    for date, addr in itinerary.items():
        day = forecasts[_normalize(addr)].get(date)  # None if out of range
        result[date] = {
            "address": addr,
            "max": day["max"] if day else None,
            "min": day["min"] if day else None,
            "rain": day["rain"] if day else None,
        }
    return result


# ---------- outfit planner ----------
def _take(stock, piece, outfit):
    """Uses one unit of a piece if there is any left. Returns True on success."""
    if stock.get(piece, 0) > 0:
        stock[piece] -= 1
        outfit[piece] = outfit.get(piece, 0) + 1
        return True
    return False


def _take_or_substitute(stock, ideal, fallback, outfit, notes):
    if _take(stock, ideal, outfit):
        return
    if _take(stock, fallback, outfit):
        notes.append(f"Wanted {ideal}, used {fallback} (no {ideal} left)")
        return
    notes.append(f"No {ideal} or {fallback} left")


def plan_outfits(clothes, forecast, average_temp):
    """
    clothes:  {piece: quantity}, pieces the user left empty already as 0
    forecast: {date: {"address", "max", "min", "rain"}}  (values are None without a forecast)
    average_temp: the slider value
    Returns {date: {"outfit": {piece: count}, "notes": [..]}} sorted by date.
    """
    THRESHOLDS = {
        "long_sleeve": -4,   # d <= -4: long sleeve instead of t-shirt
        "shorts": 4,         # d >= +4: shorts instead of pants
        "coat": -7,          # d_min <= -7: add coat
        "second_skin": -10,  # d_min <= -10: add second skin
        "heavy_coat": -14,   # d_min <= -14: heavy coat instead of coat, and gloves
    }
    RAIN_WARNING_PROB = 60
    stock = dict(clothes)  # copy, so the caller's dict is not changed

    # Temperature of each day relative to the user's average
    days = {}
    for date, f in forecast.items():
        if f["max"] is None or f["min"] is None:
            days[date] = {"d_mean": 0, "d_min": 0,
                          "notes": ["No forecast available: assumed an average day"]}
        else:
            days[date] = {"d_mean": (f["max"] + f["min"]) / 2 - average_temp,
                          "d_min": f["min"] - average_temp,
                          "notes": []}

    result = {}
    # Coldest days first, so scarce warm pieces go to the days that need them most
    for date in sorted(days, key=lambda dt: days[dt]["d_min"]):
        d_mean, d_min, notes = days[date]["d_mean"], days[date]["d_min"], days[date]["notes"]
        outfit = {}

        # One top and one bottom per day, based on the day's mean temperature
        if d_mean <= THRESHOLDS["long_sleeve"]:
            _take_or_substitute(stock, "long-sleeve", "t-shirt", outfit, notes)
        else:
            _take_or_substitute(stock, "t-shirt", "long-sleeve", outfit, notes)

        if d_mean >= THRESHOLDS["shorts"]:
            _take_or_substitute(stock, "shorts", "pants", outfit, notes)
        else:
            _take_or_substitute(stock, "pants", "shorts", outfit, notes)

        # Cold extras, based on the day's minimum temperature
        if d_min <= THRESHOLDS["heavy_coat"]:
            _take_or_substitute(stock, "heavy-coat", "coat", outfit, notes)
        elif d_min <= THRESHOLDS["coat"]:
            _take_or_substitute(stock, "coat", "heavy-coat", outfit, notes)

        if d_min <= THRESHOLDS["second_skin"]:
            if not _take(stock, "second-skin", outfit):
                notes.append("Cold day but no second skin left")

        if d_min <= THRESHOLDS["heavy_coat"]:
            if not _take(stock, "gloves", outfit):
                notes.append("Very cold day but no gloves left")

        rain = forecast[date]["rain"]
        if rain is not None and rain >= RAIN_WARNING_PROB:
            notes.append(f"High chance of rain ({rain}%): Prepare accordingly")
        result[date] = {"outfit": outfit, "notes": notes}

    return dict(sorted(result.items()))  # back to date order


# ---------- startup and background cleanup ----------
def startup_check():
    """Checks the database connection and creates the indexes. Never raises."""
    if not NOMINATIM_CONTACT:
        logging.warning("NOMINATIM_CONTACT is not set: Nominatim requires identifying the application")
    ok = check_db()
    if ok:
        try:
            ensure_indexes()
        except PyMongoError as e:
            logging.warning("Could not create indexes: %s", e)
    return ok


def cleanup_past_forecasts():
    """Removes forecast days that have already passed. Never raises."""
    try:
        deleted = delete_past_forecasts()
        logging.info("Cleanup: %d past forecast documents deleted", deleted)
        return deleted
    except PyMongoError as e:
        logging.warning("Cleanup failed: %s", e)
        return 0


def start_cleanup_scheduler():
    """Runs the cleanup once now and then every day at 03:00 UTC. Safe to call twice."""
    global _scheduler
    if _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(timezone="UTC")
    _scheduler.add_job(cleanup_past_forecasts)
    _scheduler.add_job(cleanup_past_forecasts, "cron", hour=3, minute=0,
                       misfire_grace_time=3600, coalesce=True)         # daily
    _scheduler.start()