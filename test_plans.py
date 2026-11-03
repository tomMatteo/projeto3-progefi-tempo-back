import json
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import mongomock
import pytest
import requests

import servidor
import utils
import views

CODE = "A" * 22
PLAN = {"2026-11-10": {"outfit": {"t-shirt": 1, "pants": 1}, "notes": []}}

ROUPAS = {"t-shirt": 2, "long-sleeve": 1, "pants": 1, "shorts": 2,
          "coat": 1, "heavy-coat": 0, "second-skin": 0, "gloves": 0}
BODY = {"itinerary": {"2026-11-10": "Rio de Janeiro"}, "clothes": ROUPAS, "average_temp": 22}
FORECAST = {"2026-11-10": {"address": "Rio de Janeiro", "max": 25, "min": 20, "rain": 10}}


@pytest.fixture
def client():
    servidor.app.config["TESTING"] = True
    with servidor.app.test_client() as client:
        yield client


@pytest.fixture
def mongo(monkeypatch):
    """An in-memory MongoDB, so the storage functions run without Atlas."""
    db = mongomock.MongoClient()["trip_planner"]
    monkeypatch.setattr(utils, "get_db", lambda: db)
    return db


def data(response):
    return json.loads(response.data.decode("utf-8"))


# ---------- POST /plans ----------
@patch("servidor.save_new_plan", return_value=CODE)
@patch("servidor.forecast_for_itinerary", return_value=FORECAST)
def test_post_returns_201_code_and_location(mock_forecast, mock_save, client):
    response = client.post("/plans", json=BODY)

    assert response.status_code == 201
    assert data(response)["code"] == CODE
    assert response.headers["Location"] == f"/plans/{CODE}"
    saved_input, saved_plan = mock_save.call_args[0]
    assert saved_input["average_temp"] == 22
    assert saved_plan == data(response)["plan"]


@patch("servidor.save_new_plan", side_effect=views.StorageError())
@patch("servidor.forecast_for_itinerary", return_value=FORECAST)
def test_post_database_down_returns_503(mock_forecast, mock_save, client):
    response = client.post("/plans", json=BODY)

    assert response.status_code == 503
    assert data(response)["error"] == "database unavailable"


def test_post_without_json_body_returns_400(client):
    response = client.post("/plans", data="not json", content_type="text/plain")

    assert response.status_code == 400
    assert data(response)["error"] == "request body must be a JSON object"


@patch("servidor.forecast_for_itinerary")
def test_post_invalid_date_returns_400_before_calling_apis(mock_forecast, client):
    body = {**BODY, "itinerary": {"2026-11-5": "Rio de Janeiro"}}

    response = client.post("/plans", json=body)

    assert response.status_code == 400
    assert "invalid date" in data(response)["error"]
    mock_forecast.assert_not_called()


# ---------- GET /plans/<code> ----------
def test_get_plan_returns_200(client, monkeypatch):
    monkeypatch.setattr(servidor, "get_saved_plan", lambda code: PLAN)

    response = client.get(f"/plans/{CODE}")

    assert response.status_code == 200
    assert data(response) == {"code": CODE, "plan": PLAN}


def test_get_plan_unknown_returns_404(client, monkeypatch):
    monkeypatch.setattr(servidor, "get_saved_plan", lambda code: None)

    response = client.get(f"/plans/{CODE}")

    assert response.status_code == 404
    assert data(response)["error"] == "plan not found"


def test_get_plan_malformed_code_returns_404_without_touching_database(client):
    # No monkeypatch: if this reached the database it would fail, not return 404
    assert client.get("/plans/abc").status_code == 404
    assert client.get("/plans/" + "!" * 22).status_code == 404


def test_get_plan_database_down_returns_503(client, monkeypatch):
    def broken(code):
        raise views.StorageError()
    monkeypatch.setattr(servidor, "get_saved_plan", broken)

    assert client.get(f"/plans/{CODE}").status_code == 503


# ---------- DELETE /plans/<code> ----------
def test_delete_plan_returns_204(client, monkeypatch):
    monkeypatch.setattr(servidor, "delete_saved_plan", lambda code: True)

    response = client.delete(f"/plans/{CODE}")

    assert response.status_code == 204
    assert response.data == b""


def test_delete_plan_unknown_returns_404(client, monkeypatch):
    monkeypatch.setattr(servidor, "delete_saved_plan", lambda code: False)

    assert client.delete(f"/plans/{CODE}").status_code == 404


# ---------- full flow with an in-memory database ----------
def test_create_get_delete_flow(client, mongo):
    with patch("servidor.forecast_for_itinerary", return_value=FORECAST):
        created = client.post("/plans", json=BODY)
    code = data(created)["code"]

    first = client.get(f"/plans/{code}")
    second = client.get(f"/plans/{code}")          # GET must not change anything
    assert first.status_code == second.status_code == 200
    assert data(first)["plan"] == data(created)["plan"]

    assert client.delete(f"/plans/{code}").status_code == 204
    assert client.get(f"/plans/{code}").status_code == 404
    assert client.delete(f"/plans/{code}").status_code == 404


# ---------- validate_plan_input ----------
def test_validate_fills_missing_pieces_with_zero():
    _, clothes, _ = views.validate_plan_input({**BODY, "clothes": {"t-shirt": 3, "pants": 2}})

    assert clothes["t-shirt"] == 3
    assert clothes["gloves"] == 0
    assert set(clothes) == set(views.PIECES)


@pytest.mark.parametrize("clothes, message", [
    ({"t-shirt": 17}, "t-shirt: quantity must be an integer from 0 to 16"),
    ({"t-shirt": -1}, "t-shirt: quantity must be an integer from 0 to 16"),
    ({"t-shirt": 1.5}, "t-shirt: quantity must be an integer from 0 to 16"),
    ({"t-shirt": True}, "t-shirt: quantity must be an integer from 0 to 16"),
    ({"t-shirt": "2"}, "t-shirt: quantity must be an integer from 0 to 16"),
    ({"cape": 1}, "unknown pieces: ['cape']"),
])
def test_validate_rejects_bad_clothes(clothes, message):
    with pytest.raises(ValueError, match=message.replace("[", r"\[").replace("]", r"\]")):
        views.validate_plan_input({**BODY, "clothes": clothes})


@pytest.mark.parametrize("day", ["2026-13-01", "2026-02-30", "20261110", "2026-11-5", "tomorrow"])
def test_validate_rejects_bad_dates(day):
    with pytest.raises(ValueError, match="invalid date"):
        views.validate_plan_input({**BODY, "itinerary": {day: "Rio"}})


@pytest.mark.parametrize("temp", [None, "22", True, float("nan"), float("inf"), 500, -80])
def test_validate_rejects_bad_average_temp(temp):
    with pytest.raises(ValueError):
        views.validate_plan_input({**BODY, "average_temp": temp})


def test_validate_rejects_blank_address_and_too_many_days():
    with pytest.raises(ValueError, match="address"):
        views.validate_plan_input({**BODY, "itinerary": {"2026-11-10": "   "}})
    many = {(datetime(2026, 1, 1) + timedelta(days=i)).strftime("%Y-%m-%d"): "Rio" for i in range(32)}
    with pytest.raises(ValueError, match="at most 31 days"):
        views.validate_plan_input({**BODY, "itinerary": many})


# ---------- plan_outfits on its own ----------
def test_plan_outfits_does_not_crash_when_a_piece_is_missing():
    # Used to raise KeyError: stock[piece] on a piece that was not in the dict
    plan = views.plan_outfits({"t-shirt": 1, "pants": 1}, FORECAST, 22)

    assert plan["2026-11-10"]["outfit"] == {"t-shirt": 1, "pants": 1}


def test_plan_outfits_notes_when_pieces_run_out():
    forecast = {"2026-11-10": {"address": "x", "max": 25, "min": 20, "rain": 10},
                "2026-11-11": {"address": "x", "max": 25, "min": 20, "rain": 10}}

    plan = views.plan_outfits({"t-shirt": 1, "pants": 2}, forecast, 22)

    assert "No t-shirt or long-sleeve left" in plan["2026-11-11"]["notes"]


# ---------- storage functions (mongomock) ----------
def test_code_is_random_and_has_the_expected_format():
    codes = {utils.new_code() for _ in range(200)}

    assert len(codes) == 200
    assert all(views.is_valid_code(c) for c in codes)


def test_save_and_load_plan(mongo):
    code = utils.save_plan({"average_temp": 22}, PLAN)

    assert views.is_valid_code(code)
    assert utils.load_plan(code) == PLAN
    assert mongo.plans.find_one({"_id": code})["input"] == {"average_temp": 22}


def test_load_plan_ignores_expired_plans_even_before_ttl_cleanup(mongo):
    code = utils.save_plan({}, PLAN)
    old = datetime.now(timezone.utc) - timedelta(seconds=utils.PLAN_TTL_SECONDS + 5)
    mongo.plans.update_one({"_id": code}, {"$set": {"created_at": old}})

    assert utils.load_plan(code) is None


def test_load_and_delete_unknown_plan(mongo):
    assert utils.load_plan(CODE) is None
    assert utils.delete_plan(CODE) is False


def test_ensure_indexes_creates_the_ttl_index(mongo):
    utils.ensure_indexes()

    info = mongo.plans.index_information()
    ttl = [i for i in info.values() if i.get("expireAfterSeconds") == utils.PLAN_TTL_SECONDS]
    assert ttl and ttl[0]["key"] == [("created_at", 1)]


def test_forecast_cache_read_and_cleanup(mongo):
    # Documents inserted by hand, in the same format save_forecast writes
    # (mongomock does not support pymongo's bulk_write yet, so save_forecast is not used here)
    mongo.forecasts.insert_many([
        {"_id": "-22.97000,-43.18000,2999-01-01", "lat": -22.97, "lon": -43.18,
         "date": "2999-01-01", "max": 30, "min": 20, "rain": 5},
        {"_id": "-22.97000,-43.18000,2000-01-01", "lat": -22.97, "lon": -43.18,
         "date": "2000-01-01", "max": 10, "min": 5, "rain": 0},
    ])

    found = utils.load_forecast(-22.97, -43.18, ["2999-01-01", "2000-01-01", "2999-01-02"])
    assert set(found) == {"2999-01-01", "2000-01-01"}
    assert found["2999-01-01"] == {"max": 30, "min": 20, "rain": 5}

    assert utils.delete_past_forecasts() == 1       # only the 2000 one
    assert set(utils.load_forecast(-22.97, -43.18, ["2999-01-01", "2000-01-01"])) == {"2999-01-01"}


def test_missing_mongo_uri_is_a_database_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(utils, "_client", None)
    monkeypatch.delenv("MONGO_URI", raising=False)

    with pytest.raises(views.PyMongoError):
        utils.get_db()
    assert utils.check_db() is False
    with pytest.raises(views.StorageError):
        views.get_saved_plan(CODE)


# ---------- fixes: regex, cache, API parsing ----------
def test_code_and_date_with_trailing_newline_are_rejected():
    assert not views.is_valid_code("A" * 22 + "\n")
    with pytest.raises(ValueError, match="invalid date"):
        views.validate_plan_input({**BODY, "itinerary": {"2026-11-10\n": "Rio"}})


def test_geocode_cache_avoids_second_nominatim_call(mongo):
    with patch("views.address_to_coords", return_value=(-22.9, -43.2)) as nominatim, \
         patch("views.get_forecast", return_value={}), \
         patch("views.save_forecast"):
        views.forecast_for_itinerary({"2026-11-10": "Rio"})
        views.forecast_for_itinerary({"2026-11-10": "  RIO "})   # same address, trivial differences

    assert nominatim.call_count == 1


def test_dates_outside_forecast_window_do_not_trigger_open_meteo(mongo):
    with patch("views.address_to_coords", return_value=(-22.9, -43.2)), \
         patch("views.get_forecast") as open_meteo:
        result = views.forecast_for_itinerary({"2999-01-01": "Rio"})

    open_meteo.assert_not_called()
    assert result["2999-01-01"]["max"] is None


@pytest.mark.parametrize("func, args", [
    (utils.get_forecast, (-22.9, -43.2)),
    (utils.address_to_coords, ("Rio",)),
])
def test_invalid_json_from_apis_becomes_request_exception(func, args):
    with patch("utils.requests.get") as get, patch("utils.time.sleep"):
        get.return_value.json.side_effect = ValueError("not json")
        with pytest.raises(requests.RequestException):
            func(*args)


def test_ensure_indexes_also_covers_forecasts_and_geocodes(mongo):
    utils.ensure_indexes()

    for collection, seconds in (("forecasts", utils.FORECAST_TTL_SECONDS),
                                ("geocodes", utils.GEOCODE_TTL_SECONDS)):
        info = mongo[collection].index_information()
        assert any(i.get("expireAfterSeconds") == seconds for i in info.values())