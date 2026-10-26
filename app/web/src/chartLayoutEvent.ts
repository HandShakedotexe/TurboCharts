/** Nombre de evento compartido: se dispara en `window` cada vez que
 * `IndicatorPanes.tsx` agrega o quita un pane (`chart.addPane()`/
 * `chart.removePane()`). Agregar/quitar un pane cambia la altura del pane
 * de precio (`chart.panes()[0].getHeight()`) Y el ancho de la escala de
 * precio compartida entre panes (los numeros de OI, con muchos mas
 * digitos que el precio, ensanchan esa columna) -- ninguno de los dos
 * cambia el tamaño de `.chart-main` en si (el alto total del chart es
 * fijo por CSS, `flex: 1`; agregar un pane REDISTRIBUYE ese alto entre
 * mas panes), asi que el `ResizeObserver` que ya usan `HeatmapOverlay.tsx`
 * y `VolumeProfile.tsx` sobre su contenedor NUNCA se dispara por esto --
 * reportado por el usuario (2026-09-01): "al activar mas de un indicador
 * el heatmap se va a la derecha... si se desactivan se descuadra".
 *
 * Un `Event` de DOM normal en vez de una prop nueva: `HeatmapOverlay`/
 * `VolumeProfile` no necesitan saber CUANTOS indicadores hay ni CUALES --
 * solo que su mapeo de pixeles quedo stale y hay que recalcularlo. Evita
 * acoplar tres componentes hermanos entre si a traves de App.tsx. */
export const CHART_PANES_CHANGED_EVENT = "liq-intel:panes-changed";
