/**
 * Ancho REAL del area de ploteo del chart (velas), sin la columna de la
 * escala de precio a la derecha -- bug real reportado por el usuario
 * (2026-09-21, con captura): "todos nuestras herramientas/indicadores que
 * podemos poner en el grafico invaden la seccion de precios" -- CADA
 * overlay de este proyecto (heatmap, perfil de volumen, big trades,
 * posiciones, dibujos) media su canvas con
 * `container.clientWidth`, que es el ancho del DIV CONTENEDOR completo --
 * Lightweight Charts dibuja la escala de precio DENTRO de ese mismo div
 * (una tabla interna con velas+escala como celdas hermanas), asi que
 * "todo el contenedor" incluye esa columna. Como los canvas de los
 * overlays van encima (z-index explicito) de la tabla interna de la
 * libreria, cualquier contenido que llegue hasta el borde derecho del
 * canvas (celdas de heatmap, etiquetas, burbujas, lineas dibujadas)
 * termina pintado ENCIMA de los numeros del eje.
 *
 * `chart.priceScale('right').width()` seria la forma "correcta" de medir
 * esa columna -- pero esta CONFIRMADO (hallazgo del 2026-09-01 sobre
 * paneles de indicadores) que esa API devuelve SIEMPRE 0 en este
 * proyecto, no es un error de uso. Se mide el DOM real en su
 * lugar: la tabla que genera Lightweight Charts (`.tv-lightweight-charts`)
 * vive DENTRO del mismo contenedor que los canvas de los overlays
 * (hermanos, ver `.chart-main` en App.tsx) -- su ULTIMA celda de cada fila
 * es exactamente esa columna, con el mismo ancho en todos los panes
 * (comparten la escala). Verificado con datos reales del navegador:
 * `container.clientWidth` 1024px, celda de escala 70px, area de ploteo
 * real 954px -- exactamente lo que devuelve la tabla interna, nunca 1024.
 */
export function priceScaleGutterWidth(container: HTMLElement): number {
  const cell = container.querySelector(".tv-lightweight-charts table tr td:last-child");
  return cell ? cell.getBoundingClientRect().width : 0;
}

/** Ancho a usar para DIMENSIONAR el canvas de un overlay -- nunca
 * `container.clientWidth` directo, ver `priceScaleGutterWidth`. Se
 * recalcula en CADA `draw()` (no se cachea): el ancho de la columna
 * cambia si el usuario cambia de simbolo (mas o menos digitos de precio)
 * o activa/desactiva un pane de indicador (ver CHART_PANES_CHANGED_EVENT
 * en chartLayoutEvent.ts) -- los mismos disparadores de redibujo que ya
 * tiene cada overlay bastan, esto solo cambia QUE ancho usan al redibujar. */
export function plotWidth(container: HTMLElement): number {
  return Math.max(0, container.clientWidth - priceScaleGutterWidth(container));
}
