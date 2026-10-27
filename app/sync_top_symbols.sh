#!/bin/bash
# DATA_DIR es relativo a app/ por defecto (mismo destino que db/load_historical.py
# espera, ver app/README.md) -- exportalo antes de llamar al script para usar otra ruta.
DATA_DIR="${DATA_DIR:-../../data/historical}"
SYMBOLS="ETHUSDT SOLUSDT XRPUSDT DOGEUSDT BNBUSDT ADAUSDT AVAXUSDT LINKUSDT SUIUSDT TONUSDT TRXUSDT DOTUSDT NEARUSDT LTCUSDT APTUSDT ARBUSDT OPUSDT INJUSDT TIAUSDT"
FAILED=""
for SYM in $SYMBOLS; do
  echo "=== $SYM ==="
  if ! python -m ingest.binance_historical --symbol "$SYM" --start 2020-09 --end 2026-08 \
      --klines-interval 1h --datasets klines,fundingRate,metrics \
      --data-dir "$DATA_DIR"; then
    echo "!!! $SYM tuvo fallos, sigue con el resto, se puede reintentar despues (idempotente) !!!"
    FAILED="$FAILED $SYM"
  fi
done
echo "=== fin ==="
if [ -n "$FAILED" ]; then
  echo "simbolos con algun fallo (reintentables):$FAILED"
fi
