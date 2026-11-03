import os
import secrets
import time
import logging
import threading
import requests
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne
from pymongo.errors import ConfigurationError, DuplicateKeyError, PyMongoError
from datetime import datetime, timedelta, timezone

load_dotenv()

NOMINATIM_CONTACT = os.environ.get("NOMINATIM_CONTACT")
UA = {"User-Agent": f"trip-planner/0.1 ({NOMINATIM_CONTACT or 'no-contact'})"}
_last_call = 0.0  # monotonic time of the last Nominatim request
_client = None  # MongoClient, created on first use by get_db()
_nominatim_lock = threading.Lock()  # keeps the 1 request/second rule true across threads

PLAN_TTL_SECONDS = 3600                  # plans expire 1 hour after creation
FORECAST_TTL_SECONDS = 3 * 3600          # cached forecasts are refreshed after 3 hours
GEOCODE_TTL_SECONDS = 30 * 24 * 3600     # cached coordinates last 30 days


def get_db():
    """Returns the database, creating the client on first use (not at import time,
    so the app and the tests can be imported without MONGO_URI or network).
    Raises pymongo.errors.ConfigurationError (a PyMongoError) if MONGO_URI is not set."""
    global _client
    if _client is None:
        uri = os.environ.get("MONGO_URI")
        if not uri:
            raise ConfigurationError("MONGO_URI is not set")
        _client = MongoClient(uri, serverSelectionTimeoutMS=5000)
    return _client["trip_planner"]


def check_db():
    """Pings MongoDB. Returns True if reachable, False otherwise."""
    try:
        get_db().command("ping")
        logging.info("MongoDB connected")
        return True
    except PyMongoError as e:
        logging.error("MongoDB connection failed: %s", e)
        return False


def ensure_indexes():
    """Creates the TTL index that makes MongoDB delete expired plans by itself.
    Safe to call many times. Raises pymongo.errors.PyMongoError on failure."""
    db = get_db()
    db.plans.create_index("created_at", expireAfterSeconds=PLAN_TTL_SECONDS)
    db.forecasts.create_index("fetched_at", expireAfterSeconds=FORECAST_TTL_SECONDS)
    db.geocodes.create_index("fetched_at", expireAfterSeconds=GEOCODE_TTL_SECONDS)


# ---------- plans (temporary, identified by a random code) ----------
def new_code():
    """128 random bits as 22 URL-safe characters. Works like a password to the plan."""
    return secrets.token_urlsafe(16)


def save_plan(input_data, plan, tries=3):
    """Stores a plan under a new random code and returns the code.
    Raises pymongo.errors.PyMongoError if the database fails or no free code is found."""
    for _ in range(tries):
        code = new_code()
        try:
            get_db().plans.insert_one({
                "_id": code,
                "input": input_data,
                "plan": plan,
                "created_at": datetime.now(timezone.utc),
            })
            return code
        except DuplicateKeyError:
            continue
    raise PyMongoError("could not generate a unique plan code")


def load_plan(code):
    """Returns the stored plan, or None if it doesn't exist or has expired.
    Checks the age here too, because the TTL cleanup in MongoDB runs only about once a minute.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    doc = get_db().plans.find_one({"_id": code})
    if doc is None:
        return None
    created = doc["created_at"]
    if created.tzinfo is None:                      # MongoDB returns naive UTC datetimes
        created = created.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - created > timedelta(seconds=PLAN_TTL_SECONDS):
        return None
    return doc["plan"]


def delete_plan(code):
    """Deletes a plan. Returns True if it existed.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    return get_db().plans.delete_one({"_id": code}).deleted_count == 1


# ---------- forecast cache ----------
def save_forecast(lat, lon, forecast):
    """Saves {date: {"max", "min", "rain"}} for one location, one document per day.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    now = datetime.now(timezone.utc)
    ops = [
        UpdateOne(
            {"_id": f"{lat:.5f},{lon:.5f},{date}"},   # same _id format load_forecast reads
            {"$set": {"lat": lat, "lon": lon, "date": date,
                      "max": f["max"], "min": f["min"], "rain": f["rain"],
                      "fetched_at": now}},
            upsert=True,
        )
        for date, f in forecast.items()
    ]
    if ops:
        get_db().forecasts.bulk_write(ops)


def load_forecast(lat, lon, dates):
    """Returns {date: {"max", "min", "rain"}} for the dates already stored for
    these coordinates. Dates not in the database are simply missing from the result.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    ids = [f"{lat:.5f},{lon:.5f},{d}" for d in dates]   # same _id format as save_forecast
    docs = get_db().forecasts.find({"_id": {"$in": ids}})
    return {d["date"]: {"max": d["max"], "min": d["min"], "rain": d["rain"]} for d in docs}


def delete_past_forecasts(margin_days=1):
    """Deletes stored forecast days that have already passed.
    Returns how many documents were deleted.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=margin_days)).isoformat()
    result = get_db().forecasts.delete_many({"date": {"$lt": cutoff}})
    return result.deleted_count



def load_geocode(key):
    """Returns the (lat, lon) saved for a normalized address, or None.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    doc = get_db().geocodes.find_one({"_id": key})
    return (doc["lat"], doc["lon"]) if doc else None


def save_geocode(key, lat, lon):
    """Saves the coordinates of a normalized address.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    get_db().geocodes.update_one(
        {"_id": key},
        {"$set": {"lat": lat, "lon": lon, "fetched_at": datetime.now(timezone.utc)}},
        upsert=True,
    )

# ---------- external APIs ----------
def get_forecast(lat, lon):
    """Coordinates -> {"2026-10-28": {"max": 27.1, "min": 19.4, "rain": 40}, ...} (up to 16 days).
    max/min are apparent (feels-like) temperatures in °C; rain is the chance in %.
    Raises requests.RequestException if Open-Meteo is unreachable or returns an error."""
    r = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "daily": "apparent_temperature_max,apparent_temperature_min,precipitation_probability_max",
            "forecast_days": 16,
            "timezone": "auto",
        },
        timeout=10,
    )
    try:
        daily = r.json()["daily"]
        return {
            day: {"max": tmax, "min": tmin, "rain": rain}
            for day, tmax, tmin, rain in zip(
                daily["time"], daily["apparent_temperature_max"],
                daily["apparent_temperature_min"],
                daily["precipitation_probability_max"],
            )
        }
    except (ValueError, KeyError, TypeError) as e:
        raise requests.RequestException("unexpected Open-Meteo response") from e




def address_to_coords(text):
    """Address -> (lat, lon) using Nominatim.
    Raises ValueError if the address is not found, or requests.RequestException if Nominatim fails."""
    global _last_call
    # Nominatim's policy allows about 1 request per second (the lock keeps this true across threads)
    with _nominatim_lock:
        wait = 1.0 - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        try:
            r = requests.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": text, "format": "jsonv2", "limit": 1},
                headers=UA,
                timeout=10,
            )
        finally:
            _last_call = time.monotonic()   # also updated when the request fails or times out
    r.raise_for_status()

    try:
        results = r.json()
        found = bool(results)
        lat, lon = (float(results[0]["lat"]), float(results[0]["lon"])) if found else (None, None)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        raise requests.RequestException("unexpected Nominatim response") from e
    if not found:
        raise ValueError(f"could not find address: {text}")
    return lat, lon