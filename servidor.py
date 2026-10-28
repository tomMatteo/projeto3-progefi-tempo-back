from flask import Flask, request, jsonify
from flask_cors import CORS
import requests

from views import forecast_for_itinerary, plan_outfits,  startup_check

app = Flask(__name__)
CORS(app, origins=["http://localhost:5173"])  # Vite's default dev port


@app.post("/plan")
def make_plan():
    body = request.get_json(silent=True) or {}
    itinerary = body.get("itinerary")
    clothes = body.get("clothes")
    average_temp = body.get("average_temp")

    # Minimal presence/type checks (full validation is still to be written)
    if (not isinstance(itinerary, dict) or not itinerary
            or not all(isinstance(a, str) for a in itinerary.values())):
        return jsonify({"error": "itinerary must be a non-empty object of date -> address"}), 400
    if not isinstance(clothes, dict):
        return jsonify({"error": "clothes must be an object of piece -> quantity"}), 400
    if isinstance(average_temp, bool) or not isinstance(average_temp, (int, float)):
        return jsonify({"error": "average_temp must be a number"}), 400

    try:
        forecast = forecast_for_itinerary(itinerary)           # Nominatim + Open-Meteo
        plan = plan_outfits(clothes, forecast, average_temp)   # no external calls
    except ValueError as e:                                    # e.g. address not found
        return jsonify({"error": str(e)}), 400
    except requests.RequestException:                          # Nominatim/Open-Meteo down or timed out
        return jsonify({"error": "external service unavailable"}), 502

    return jsonify(plan), 200


if __name__ == "__main__":
    startup_check()
    app.run(debug=True)