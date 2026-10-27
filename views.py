from utils import (
    load_items, save_item,
    load_trip, save_trip, update_trip,
    address_to_coords, get_forecast
)

def _normalize(address):
    """'  Copacabana,  RIO ' -> 'copacabana, rio' so trivial differences count as copies."""
    return " ".join(address.lower().split())


def forecast_for_itinerary(itinerary):
    """{date: address} -> {date: {"address", "max", "min"}}.
    max and min are None when the date is outside Open-Meteo's 16-day range.
    Raises ValueError (address not found) or requests.RequestException (API down)."""

    # 1) Remove copies: one entry per distinct address
    unique = {_normalize(addr) for addr in itinerary.values()}

    # 2) Per distinct address: coordinates first, then the forecast
    forecasts = {}
    for addr in unique:
        lat, lon = address_to_coords(addr)
        forecasts[addr] = get_forecast(lat, lon)

    # 3) Link each day to its address and that address's max and min
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

def _take(stock, piece, outfit):
    """Uses one unit of a piece if there is any left. Returns True on success."""
    if stock[piece] > 0:
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
    forecast: {date: {"address", "max", "min"}}  (max/min are None without a forecast)
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
            notes.append(f"High chance of rain ({rain}%): Prepare acoordingly")
        result[date] = {"outfit": outfit, "notes": notes}

    return dict(sorted(result.items()))  # back to date order