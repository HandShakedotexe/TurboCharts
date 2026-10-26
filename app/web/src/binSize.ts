/**
 * Ancho de bin de precio (USD) para el heatmap.
 *
 * Bug real reportado 2026-09-29 ("pools gigantescos" en ETHUSDT): $100 fijo
 * es ~0.12% del precio en BTCUSDT pero ~3.7% en ETHUSDT -- 30x mas grueso en
 * terminos relativos, por eso los pools se ven como bloques solidos en vez
 * de una textura fina. Esta funcion reemplaza el dolar fijo por un
 * porcentaje del precio de referencia (el mismo ~0.12% que BTCUSDT ya tenia
 * implicito), redondeado a la secuencia "linda" 1-2-5 (el mismo criterio
 * que usan los ejes de cualquier grafico) para no mostrar numeros feos como
 * "$3.24" en el eje, y para que el bin NO cambie en cada
 * tick -- solo salta al siguiente escalon cuando el precio se mueve lo
 * bastante (ETHUSDT necesitaria pasar de ~$2300 a ~$4700, un +~100%, para
 * saltar de $2 a $5).
 *
 * El target (0.12%) se eligio para que BTCUSDT quede EXACTAMENTE en $100,
 * sin ningun cambio visual en el simbolo mas mirado.
 *
 * Quedo DESACTIVADA un tiempo (2026-10-02, devolvia siempre el default
 * fijo) por dos dudas reales que aparecieron al verificarla en el
 * navegador -- las dos quedaron resueltas:
 * 1. Performance: una medicion inicial sugirio que bin_size chico
 *    disparaba el costo de `compute_liquidity_intervals` (>60s en
 *    ETHUSDT). Investigado a fondo (2026-10-02): FALSO POSITIVO -- esos
 *    timeouts eran rate-limit real de Binance (HTTP 429) acumulado por el
 *    volumen de pruebas de esa sesion, nada que ver con bin_size.
 *    Profiling aislado (sin red) con datos reales: BTCUSDT 0.96s (bin=100)
 *    vs 0.99s (bin=2); ETHUSDT 0.19s vs 0.18s -- sin diferencia real.
 * 2. Precedente: `bin_levels()` en heatmap_model.py documenta que un
 *    ancho RELATIVO al precio ya se habia probado y descartado el
 *    2026-08-28 porque las dos versiones anteriores recalculaban el bin
 *    en CADA request contra el precio del momento, sin redondear --
 *    el ancho "flotaba" todo el tiempo y la cuadricula no se veia regular
 *    como un heatmap publicado. Esta version es distinta a proposito: al
 *    redondear a la secuencia 1-2-5, el bin se queda FIJO durante largos
 *    tramos (solo salta en un movimiento de precio de ~2.2x) -- mismo
 *    espiritu que la cuadricula fija de antes, pero calibrada por simbolo
 *    en vez de un unico numero para los ~20.
 */
const BIN_SIZE_TARGET_RATIO = 0.0012;

const FALLBACK_BIN_SIZE_USD = 100;

export function niceBinSizeUsd(referencePrice: number | undefined | null): number {
  if (!referencePrice || !(referencePrice > 0)) return FALLBACK_BIN_SIZE_USD;
  const raw = referencePrice * BIN_SIZE_TARGET_RATIO;
  const exp = Math.floor(Math.log10(raw));
  const base = raw / 10 ** exp;
  const nice = base < 1.5 ? 1 : base < 3.5 ? 2 : base < 7.5 ? 5 : 10;
  return nice * 10 ** exp;
}
