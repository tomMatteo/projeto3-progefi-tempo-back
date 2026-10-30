import os
import copy
import uuid
import requests
import time
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne 
from pymongo.errors import PyMongoError
from datetime import datetime, timedelta, timezone 

load_dotenv()

UA =   {"User-Agent": f"trip-planner/0.1 ({os.environ.get('NOMINATIM_CONTACT', 'no-contact')})"} #Imports from env contact use for call
_last_call = 0.0  # time of the last Nominatim request      
_client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=5000)
db = _client["trip_planner"]

def check_db():
    """Pings MongoDB. Returns True if reachable, False otherwise."""
    try:
        _client.admin.command("ping")
        print("MongoDB connected")
        return True
    except PyMongoError as e:
        print(f"MongoDB connection failed: {e}")
        return False


def save_forecast(lat, lon, forecast):
    """Saves {date: {"max", "min", "rain"}} for one location, one document per day.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    ops = [
        UpdateOne(
            {"_id": f"{lat:.5f},{lon:.5f},{date}"},
            {"$set": {"lat": lat, "lon": lon, "date": date,
                      "max": f["max"], "min": f["min"], "rain": f["rain"]}},
            upsert=True,
        )
        for date, f in forecast.items()
    ]
    if ops:
        db.forecasts.bulk_write(ops)

def delete_past_forecasts(margin_days=1):
    """Deletes stored forecast days that have already passed.
    Returns how many documents were deleted.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    today = datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=margin_days)).isoformat()
    result = db.forecasts.delete_many({"date": {"$lt": cutoff}})
    return result.deleted_count

def load_forecast(lat, lon, dates):                                 # NEW
    """Returns {date: {"max", "min", "rain"}} for the dates already stored for
    these coordinates. Dates not in the database are simply missing from the result.
    Raises pymongo.errors.PyMongoError if the database can't be reached."""
    ids = [f"{lat:.5f},{lon:.5f},{d}" for d in dates]               # same _id format as save_forecast
    docs = db.forecasts.find({"_id": {"$in": ids}})
    return {d["date"]: {"max": d["max"], "min": d["min"], "rain": d["rain"]} for d in docs}

# ---------- external APIs ----------
def get_forecast(lat, lon):
    """Coordinates -> {"2026-10-28": {"max": 27.1, "min": 19.4}, ...} (up to 16 days).
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
    r.raise_for_status()
    daily = r.json()["daily"]
    return {
        day: {"max": tmax, "min": tmin, "rain": rain}
        for day, tmax, tmin, rain in zip(
            daily["time"], daily["apparent_temperature_max"],
            daily["apparent_temperature_min"],
            daily["precipitation_probability_max"],
        )
    }

def address_to_coords(address):
    """Address -> (lat, lon) as floats.
    Raises ValueError if the address is empty or not found.
    Raises requests.RequestException if Nominatim is unreachable or returns an error."""
    global _last_call

    text = address.strip()
    if not text:
        raise ValueError("empty address")

    # Nominatim's policy allows about 1 request per second
    wait = 1.0 - (time.time() - _last_call)
    if wait > 0:
        time.sleep(wait)

    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": text, "format": "jsonv2", "limit": 1},
        headers=UA,
        timeout=10,
    )
    _last_call = time.time()
    r.raise_for_status()

    results = r.json()
    if not results:
        raise ValueError(f"could not find address: {text}")
    return float(results[0]["lat"]), float(results[0]["lon"])