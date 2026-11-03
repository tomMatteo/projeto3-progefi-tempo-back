import pytest
import json
import requests
from unittest.mock import patch
import servidor

ROUPAS = {
    "t-shirt": 2,
    "long-sleeve": 1,
    "pants": 1,
    "shorts": 2,
    "coat": 1,
    "heavy-coat": 0,
    "second-skin": 0,
    "gloves": 0
}

ROTEIRO = {
    "itinerary": {"2026-11-10": "Rio de Janeiro"},
    "clothes": ROUPAS,
    "average_temp": 22
}

FAKE_CODE = "A" * 22


@pytest.fixture(autouse=True)
def fake_storage(monkeypatch):
    # POST /plans saves the plan; here the database is replaced by a stub
    monkeypatch.setattr(servidor, "save_new_plan", lambda input_data, plan: FAKE_CODE)


@pytest.fixture
def client():
    servidor.app.config["TESTING"] = True
    with servidor.app.test_client() as client:
        yield client

@patch("servidor.forecast_for_itinerary")
def test_post_plan_dia_quente(mock_previsao, client):
    # POST /plans - dia quente: camiseta e short
    mock_previsao.return_value = {
        "2026-11-10": {"address": "Rio de Janeiro", "max": 32, "min": 24, "rain": 10}
    }
 
    response = client.post("/plans", json=ROTEIRO)
 
    assert response.status_code == 201
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["plan"]["2026-11-10"]["outfit"] == {"t-shirt": 1, "shorts": 1}

@patch("servidor.forecast_for_itinerary")
def test_post_plan_dia_frio(mock_previsao, client):
    # POST /plans - dia frio: manga longa, calça e casaco
    mock_previsao.return_value = {
        "2026-11-10": {"address": "Rio de Janeiro", "max": 15, "min": 13, "rain": 10}
    }
 
    response = client.post("/plans", json=ROTEIRO)
 
    assert response.status_code == 201
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["plan"]["2026-11-10"]["outfit"] == {"long-sleeve": 1, "pants": 1, "coat": 1}

@patch("servidor.forecast_for_itinerary")
def test_post_plan_sem_short(mock_previsao, client):
    # POST /plans - sem short, usa calça e avisa
    mock_previsao.return_value = {
        "2026-11-10": {"address": "Rio de Janeiro", "max": 32, "min": 24, "rain": 10}
    }
    roteiro = {
        "itinerary": {"2026-11-10": "Rio de Janeiro"},
        "clothes": {**ROUPAS, "shorts": 0},
        "average_temp": 22
    }
 
    response = client.post("/plans", json=roteiro)
 
    assert response.status_code == 201
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["plan"]["2026-11-10"]["outfit"] == {"t-shirt": 1, "pants": 1}
    assert response_data["plan"]["2026-11-10"]["notes"] == ["Wanted shorts, used pants (no shorts left)"]

@patch("servidor.forecast_for_itinerary")
def test_post_plan_sem_previsao(mock_previsao, client):
    # POST /plans - data sem previsão é tratada como dia normal
    mock_previsao.return_value = {
        "2026-12-25": {"address": "Rio de Janeiro", "max": None, "min": None, "rain": None}
    }
 
    response = client.post("/plans", json=ROTEIRO)
 
    assert response.status_code == 201
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["plan"]["2026-12-25"]["outfit"] == {"t-shirt": 1, "pants": 1}
    assert response_data["plan"]["2026-12-25"]["notes"] == ["No forecast available: assumed an average day"]

@patch("servidor.forecast_for_itinerary")
def test_post_plan_chuva(mock_previsao, client):
    # POST /plans - chuva alta gera aviso
    mock_previsao.return_value = {
        "2026-11-10": {"address": "Rio de Janeiro", "max": 25, "min": 20, "rain": 80}
    }
 
    response = client.post("/plans", json=ROTEIRO)
 
    assert response.status_code == 201
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["plan"]["2026-11-10"]["notes"] == ["High chance of rain (80%): Prepare accordingly"]

@patch("servidor.forecast_for_itinerary")
def test_post_plan_sem_roteiro(mock_previsao, client):
    # POST /plans - retorna erro se o roteiro estiver vazio
    roteiro = {"itinerary": {}, "clothes": ROUPAS, "average_temp": 22}
 
    response = client.post("/plans", json=roteiro)
 
    assert response.status_code == 400
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["error"] == "itinerary must be a non-empty object of date -> address"

@patch("servidor.forecast_for_itinerary")
def test_post_plan_roupas_invalidas(mock_previsao, client):
    # POST /plans - retorna erro se as roupas não forem um dicionário
    roteiro = {"itinerary": {"2026-11-10": "Rio de Janeiro"}, "clothes": "camisetas", "average_temp": 22}
 
    response = client.post("/plans", json=roteiro)
 
    assert response.status_code == 400
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["error"] == "clothes must be an object of piece -> quantity"

@patch("servidor.forecast_for_itinerary")
def test_post_plan_temperatura_invalida(mock_previsao, client):
    # POST /plans - retorna erro se a temperatura não for número
    roteiro = {"itinerary": {"2026-11-10": "Rio de Janeiro"}, "clothes": ROUPAS, "average_temp": "vinte"}
 
    response = client.post("/plans", json=roteiro)
 
    assert response.status_code == 400
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["error"] == "average_temp must be a number"

@patch("servidor.forecast_for_itinerary")
def test_post_plan_endereco_nao_encontrado(mock_previsao, client):
    # POST /plans - retorna erro se o endereço não existir
    mock_previsao.side_effect = ValueError("could not find address: Cidade Inventada")
 
    response = client.post("/plans", json=ROTEIRO)
 
    assert response.status_code == 400
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["error"] == "could not find address: Cidade Inventada"

@patch("servidor.forecast_for_itinerary")
def test_post_plan_api_fora_do_ar(mock_previsao, client):
    # POST /plans - retorna erro se a API de previsão estiver fora do ar
    mock_previsao.side_effect = requests.RequestException()
 
    response = client.post("/plans", json=ROTEIRO)
 
    assert response.status_code == 502
    response_data = json.loads(response.data.decode("utf-8"))
    assert response_data["error"] == "external service unavailable"

@patch("servidor.forecast_for_itinerary")
def test_post_plan_casaco_vai_para_o_dia_mais_frio(mock_previsao, client):
    # POST /plans - com um casaco só, ele vai para o dia mais frio
    mock_previsao.return_value = {
        "2026-11-10": {"address": "Rio de Janeiro", "max": 18, "min": 14, "rain": 10},
        "2026-11-11": {"address": "Gramado", "max": 15, "min": 10, "rain": 10}
    }
    roteiro = {
        "itinerary": {"2026-11-10": "Rio de Janeiro", "2026-11-11": "Gramado"},
        "clothes": ROUPAS,
        "average_temp": 22
    }

    response = client.post("/plans", json=roteiro)

    assert response.status_code == 201
    response_data = json.loads(response.data.decode("utf-8"))
    assert "coat" in response_data["plan"]["2026-11-11"]["outfit"]
    assert "coat" not in response_data["plan"]["2026-11-10"]["outfit"]