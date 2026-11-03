import logging
import os

import requests
from flask import Flask, request, jsonify, url_for
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from views import (validate_plan_input, forecast_for_itinerary, plan_outfits,
                   save_new_plan, get_saved_plan, delete_saved_plan,
                   StorageError, startup_check, start_cleanup_scheduler)

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

# Comma-separated list in the environment, e.g. CORS_ORIGINS=http://localhost:5173,https://meusite.com
_origins = os.environ.get("CORS_ORIGINS", "http://localhost:5173")
CORS(app, origins=[o.strip() for o in _origins.split(",") if o.strip()])

# Limits how often one IP can create plans (each one calls Nominatim/Open-Meteo and writes to the database).
# In-memory counters are enough because the app runs with a single gunicorn worker.
# If the server sits behind a proxy (nginx, load balancer), configure ProxyFix so the real IP is used.
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://",
                  enabled=os.environ.get("DISABLE_RATE_LIMIT") != "1")

# Database check, indexes and the daily cleanup. Done here (not under __main__) so it
# also runs under gunicorn. The tests set DISABLE_STARTUP_TASKS=1 to skip it.
# With gunicorn, use a single worker (-w 1), otherwise each worker starts its own scheduler.
if os.environ.get("DISABLE_STARTUP_TASKS") != "1":
    startup_check()
    start_cleanup_scheduler()



@app.errorhandler(429)
def too_many_requests(e):
    return jsonify({"error": "too many requests, try again later"}), 429


@limiter.limit(os.environ.get("POST_RATE_LIMIT", "10 per minute"))
@app.post("/plans")
def create_plan():
    """Calculates a plan, saves it under a random code and returns both."""
    try:
        itinerary, clothes, average_temp = validate_plan_input(request.get_json(silent=True))
        forecast = forecast_for_itinerary(itinerary)           # Nominatim + Open-Meteo + cache
        plan = plan_outfits(clothes, forecast, average_temp)   # no external calls
        code = save_new_plan({"itinerary": itinerary, "clothes": clothes,
                              "average_temp": average_temp}, plan)
    except requests.RequestException:
        return jsonify({"error": "external service unavailable"}), 502
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except StorageError:                                       # database down
        return jsonify({"error": "database unavailable"}), 503

    response = jsonify({"code": code, "plan": plan})
    response.status_code = 201
    response.headers["Location"] = url_for("get_plan", code=code)
    return response


@app.get("/plans/<code>")
def get_plan(code):
    """Read-only: returns the plan, or 404 if unknown, malformed or expired."""
    try:
        plan = get_saved_plan(code)
    except StorageError:
        return jsonify({"error": "database unavailable"}), 503
    if plan is None:
        return jsonify({"error": "plan not found"}), 404
    return jsonify({"code": code, "plan": plan}), 200


@app.delete("/plans/<code>")
def remove_plan(code):
    """Deletes the plan (the frontend calls this once the user has the plan)."""
    try:
        deleted = delete_saved_plan(code)
    except StorageError:
        return jsonify({"error": "database unavailable"}), 503
    if not deleted:
        return jsonify({"error": "plan not found"}), 404
    return "", 204


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")