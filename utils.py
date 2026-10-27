import os
import copy
import uuid
import requests
import time
from dotenv import load_dotenv

load_dotenv()

UA =   {"User-Agent": f"trip-planner/0.1 ({os.environ.get('NOMINATIM_CONTACT', 'no-contact')})"} #Imports from env contact use for call
_last_call = 0.0  # time of the last Nominatim request


# ---------- temporary in-memory storage (replace with MongoDB later) ----------
# Data is lost every time the server restarts.
_items = {}   # id -> item dict
_trips = {}   # id -> trip dict


def _new_id():
    return uuid.uuid4().hex


# ---------- items ----------
def load_items(user_id):
    return [copy.deepcopy(i) for i in _items.values() if i["user_id"] == user_id]


def load_item(item_id, user_id):
    item = _items.get(item_id)
    return copy.deepcopy(item) if item and item["user_id"] == user_id else None


def save_item(data):
    data = copy.deepcopy(data)
    data["_id"] = _new_id()
    _items[data["_id"]] = data
    return data["_id"]


def update_item(item_id, user_id, fields):
    item = _items.get(item_id)
    if not item or item["user_id"] != user_id:
        return False
    item.update(fields)
    return True


def remove_item(item_id, user_id):
    item = _items.get(item_id)
    if not item or item["user_id"] != user_id:
        return False
    del _items[item_id]
    return True


# ---------- trips ----------
def load_trips(user_id):
    return [copy.deepcopy(t) for t in _trips.values() if t["user_id"] == user_id]


def load_trip(trip_id, user_id):
    trip = _trips.get(trip_id)
    return copy.deepcopy(trip) if trip and trip["user_id"] == user_id else None


def save_trip(data):
    data = copy.deepcopy(data)
    data["_id"] = _new_id()
    _trips[data["_id"]] = data
    return data["_id"]


def update_trip(trip_id, user_id, fields):
    trip = _trips.get(trip_id)
    if not trip or trip["user_id"] != user_id:
        return False
    trip.update(fields)
    return True


def remove_trip(trip_id, user_id):
    trip = _trips.get(trip_id)
    if not trip or trip["user_id"] != user_id:
        return False
    del _trips[trip_id]
    return True


# ---------- external APIs ----------
def geocode(address):
    """Nominatim: address -> list of {label, lat, lon} candidates."""
    r = requests.get(
        "https://nominatim.openstreetmap.org/search",
        params={"q": address, "format": "jsonv2", "limit": 5, "countrycodes": "br"},
        headers=UA,
        timeout=10,
    )
    r.raise_for_status()
    return [
        {"label": p["display_name"], "lat": float(p["lat"]), "lon": float(p["lon"])}
        for p in r.json()
    ]


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