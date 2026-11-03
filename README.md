# API DE PREVISÃO DE TEMPO E LOCAÇÃO DE ROUPAS

### BACKEND DO PROJETO 3 DE PROGRAMAÇÃO EFICAZ

**Link da API (AWS):** http://52.73.92.136

O objetivo dessa API é receber três coisas:
- Um roteiro: para cada dia da viagem, o endereço (ou cidade) onde a pessoa estará
- As quantidades de cada peça de roupa que ela vai levar (0 a 16 de cada)
- A temperatura que ela considera "média" (camiseta e calça)

E, a partir disso, gerar uma locação de roupas para cada dia, baseada na previsão de tempo do lugar (temperatura aparente máxima/mínima e chance de chuva). Cada peça é usada uma vez por unidade; quando acaba, a API substitui por outra parecida e avisa. Dias com alta chance de chuva recebem um aviso.

## Como funciona o recurso "plano"

Um plano é calculado, **salvo no MongoDB com um código aleatório** (128 bits, 22 caracteres) e devolvido junto com o código. Quem tem o código pode buscar o plano de novo, por exemplo para voltar depois ou abrir o link em outro aparelho. O código funciona como senha de acesso ao plano. Planos são temporários: expiram **1 hora** após a criação (índice TTL do MongoDB) e também podem ser apagados com `DELETE`.

## Rotas

| Método | Rota | Sucesso | Erros |
|---|---|---|---|
| POST | `/plans` | `201` com `{"code", "plan"}` e header `Location: /plans/<code>` | `400` entrada inválida ou endereço não encontrado; `429` limite de requisições por IP excedido; `502` Nominatim/Open-Meteo fora do ar; `503` banco indisponível |
| GET | `/plans/<code>` | `200` com `{"code", "plan"}` (somente leitura) | `404` código inexistente, expirado ou mal formado; `503` banco indisponível |
| DELETE | `/plans/<code>` | `204` sem corpo | `404` se não existe; `503` banco indisponível |

Corpo do `POST /plans`:

```json
{
  "itinerary": {
    "2026-11-10": "Copacabana, Rio de Janeiro, Brazil",
    "2026-11-11": "Gramado, Brazil"
  },
  "clothes": {
    "t-shirt": 3, "long-sleeve": 2, "pants": 2, "shorts": 2,
    "second-skin": 0, "coat": 1, "heavy-coat": 0, "gloves": 0
  },
  "average_temp": 22
}
```

- Datas no formato `YYYY-MM-DD`, no máximo 31 dias, um endereço por dia (texto de até 200 caracteres).
- Peças aceitas: `t-shirt`, `long-sleeve`, `pants`, `shorts`, `second-skin`, `coat`, `heavy-coat`, `gloves`. Cada quantidade é um inteiro de 0 a 16; peça ausente conta como 0. Peça desconhecida gera `400`.
- `average_temp` é um número entre -50 e 60 (°C).
- A previsão cobre até 16 dias a partir de hoje; dias fora disso são tratados como um dia médio, com um aviso.
- `POST /plans` tem limite de requisições por IP (padrão: 10 por minuto, configurável em `POST_RATE_LIMIT`). Acima disso a API responde `429`.

Resposta (`201`):

```json
{
  "code": "k3J9x_Qw7mV2tB8aZc1RpA",
  "plan": {
    "2026-11-10": {"outfit": {"t-shirt": 1, "shorts": 1}, "notes": []},
    "2026-11-11": {"outfit": {"long-sleeve": 1, "pants": 1, "coat": 1},
                   "notes": ["High chance of rain (70%): Prepare accordingly"]}
  }
}
```

## Uso do MongoDB (Atlas)

Banco `trip_planner`, três coleções:
- `plans`: os planos salvos (`_id` = código aleatório, `input`, `plan`, `created_at`), com índice TTL de 1 hora.
- `forecasts`: cache da previsão, um documento por local e dia (`_id` = `lat,lon,data`), com índice TTL de 3 horas (`fetched_at`), para a previsão ser renovada com frequência. A API só chama o Open-Meteo quando algum dia necessário (dentro da janela de 16 dias) não está salvo. Todo dia às 03:00 UTC (e ao iniciar o servidor) os dias que já passaram são apagados.
- `geocodes`: cache de endereço → coordenadas (`_id` = endereço normalizado), com índice TTL de 30 dias. Evita repetir consultas ao Nominatim.

## APIs e bibliotecas utilizadas
- ### Nominatim
API usada para transformar endereços em coordenadas (limite de 1 requisição por segundo; a API respeita isso, inclusive com várias threads).

nominatim.org

- ### Open Meteo
API usada para ter a previsão do tempo para cada dia em cada lugar.

open-meteo.com

Flask, Flask-CORS, Flask-Limiter, PyMongo, APScheduler, python-dotenv, requests. Testes: pytest e mongomock.

## Variáveis de ambiente

Copie `.env.example` para `.env` e preencha (o `.env` real não vai para o GitHub):

| Variável | Para que serve |
|---|---|
| `MONGO_URI` | string de conexão do MongoDB Atlas |
| `NOMINATIM_CONTACT` | e-mail ou URL enviado ao Nominatim, que exige identificar a aplicação (sem ele a API avisa no log ao iniciar) |
| `CORS_ORIGINS` | endereços do frontend autorizados, separados por vírgula (padrão: `http://localhost:5173`) |
| `FLASK_DEBUG` | `1` liga o modo debug ao rodar `python servidor.py` (só em desenvolvimento) |
| `DISABLE_STARTUP_TASKS` | `1` não checa o banco nem inicia a limpeza diária (usado nos testes) |
| `POST_RATE_LIMIT` | limite de `POST /plans` por IP (padrão: `10 per minute`) |
| `DISABLE_RATE_LIMIT` | `1` desliga o limite de requisições (usado nos testes) |

No Atlas, o IP de quem acessa o banco (sua máquina e o servidor EC2) precisa estar em **Network Access**.

## Como rodar localmente

```bash
python -m venv .venv
.venv\Scripts\activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # e preencha os valores
python servidor.py
```

## Testes

```bash
pip install -r requirements-dev.txt
pytest
```

Os testes não usam internet nem o Atlas: Nominatim e Open-Meteo são simulados e o banco é um MongoDB em memória (mongomock).

## Deploy (AWS EC2)

```bash
pip install -r requirements.txt
gunicorn -w 1 --threads 4 --timeout 120 -b 0.0.0.0:8000 servidor:app
```

- Use **um único worker** (`-w 1`): cada worker inicia a sua própria limpeza diária e guarda o seu próprio contador do limite de requisições. As threads deixam outras requisições passarem enquanto uma espera o Nominatim (1 s por endereço novo), e o `--timeout 120` cobre roteiros com muitos endereços.
- Libere a porta no Security Group da instância e o IP da instância no Network Access do Atlas.
- Crie o `.env` no servidor e deixe o gunicorn como serviço (systemd) para reiniciar sozinho.
- Defina `CORS_ORIGINS` com o endereço do frontend publicado.
- Se o servidor ficar atrás de um proxy (nginx, load balancer), configure o `ProxyFix` no Flask, senão o limite de requisições enxergará todos os usuários como o mesmo IP.
- Na primeira vez após atualizar para esta versão, apague a coleção `forecasts` no Atlas (`db.forecasts.drop()`), pois os documentos antigos não têm `fetched_at` e não expirariam pelo TTL.

## Membros:
- Eduardo Khaled
- Gabriel Rocha
- João Espioni
- Leonardo Meirelles
- Matteo Rosso
