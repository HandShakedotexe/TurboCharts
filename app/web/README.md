# web/ — chart + heatmap (Fase 1)

React + Vite + TypeScript. Precio real (Lightweight Charts, Apache 2.0) más
una capa canvas propia para el heatmap de liquidaciones, sincronizada por
escala de precios con la serie de velas — un iframe de TradingView no deja
superponer datos propios.

El heatmap que sirve la API está etiquetado siempre `MODELED · UNCALIBRATED`
(visible en la barra superior, con la cesta de apalancamientos y su versión
en el tooltip). No calibrar contra Bybit hasta Fase 4 — ver
`../api/heatmap_model.py`.

## Arranque

Con la API corriendo (`../api`, puerto 8000 por defecto):

```bash
cd app/web
npm install
npm run dev   # http://localhost:5173
```

`VITE_API_BASE` (opcional, `.env.local`) sobreescribe la URL de la API si no
está en `http://127.0.0.1:8000/api`.
