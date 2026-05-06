#!/bin/bash

# Script para detener todos los procesos de la app

echo "🛑 Deteniendo procesos..."

# Matar procesos uvicorn
pkill -f "uvicorn" && echo "✓ uvicorn detenido" || echo "✗ No hay uvicorn ejecutándose"

# Matar procesos Python de FastAPI
pkill -f "app.main" && echo "✓ app.main detenido" || echo "✗ No hay app.main ejecutándose"

# Liberar puerto 8000 si está en uso
if lsof -Pi :8000 -sTCP:LISTEN -t >/dev/null ; then
    kill -9 $(lsof -t -i:8000) && echo "✓ Puerto 8000 liberado" || echo "✗ No se pudo liberar puerto 8000"
fi

echo "✅ Hecho"
