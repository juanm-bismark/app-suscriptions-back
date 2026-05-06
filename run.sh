#!/bin/bash
set -e

API_PORT=8000
DB_PORT=5432
DB_CONTAINER="back-db-1"

# ── 1. Docker ────────────────────────────────────────────────────────────────
echo ""
echo "=== Docker / PostgreSQL ==="

if ! docker info &>/dev/null; then
    echo "ERROR: Docker no esta corriendo. Inicia Docker Desktop y vuelve a intentarlo."
    exit 1
fi

CONTAINER_STATUS=$(docker inspect --format "{{.State.Status}}" "$DB_CONTAINER" 2>/dev/null || echo "missing")

if [ "$CONTAINER_STATUS" = "running" ]; then
    echo "OK  Contenedor '$DB_CONTAINER' ya esta corriendo."
else
    echo "    Contenedor '$DB_CONTAINER' no esta activo (estado: $CONTAINER_STATUS). Levantando..."
    docker compose up -d db
    echo -n "    Esperando que PostgreSQL este listo"
    for i in $(seq 1 20); do
        if docker compose exec db pg_isready -U postgres &>/dev/null; then
            echo " listo."
            break
        fi
        echo -n "."
        sleep 1
        if [ "$i" -eq 20 ]; then
            echo ""
            echo "ERROR: PostgreSQL no respondio en 20 segundos."
            exit 1
        fi
    done
fi

# ── 2. Puerto DB ─────────────────────────────────────────────────────────────
DB_PID=$(lsof -ti:$DB_PORT 2>/dev/null || true)
if [ -n "$DB_PID" ]; then
    echo "OK  Puerto $DB_PORT en uso (PID $DB_PID) — asumiendo que es Postgres."
else
    echo "WARN Puerto $DB_PORT libre — verifica que el contenedor expone el puerto correctamente."
fi

# ── 3. Puerto API ────────────────────────────────────────────────────────────
echo ""
echo "=== FastAPI / Puerto $API_PORT ==="

API_PID=$(lsof -ti:$API_PORT 2>/dev/null || true)
if [ -n "$API_PID" ]; then
    echo "    Puerto $API_PORT ocupado por PID $API_PID. Liberando..."
    kill -9 $API_PID
    sleep 1
    echo "OK  Puerto $API_PORT liberado."
else
    echo "OK  Puerto $API_PORT libre."
fi

# ── 4. FastAPI ───────────────────────────────────────────────────────────────
echo ""
echo "=== Iniciando FastAPI en http://localhost:$API_PORT ==="
echo ""
.venv/bin/python -m uvicorn app.main:app --reload --port $API_PORT
