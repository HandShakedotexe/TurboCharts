/** Herramientas de dibujo del chart (linea horizontal/vertical/diagonal y
 * rectangulo, pedido del usuario 2026-09-20: "agrega las herramientas de
 * poder dibujar una linea horizontal/vertical/diagonal ... y poder
 * [dibujar] un cuadrado"). Tipo + persistencia separados de
 * `DrawingToolsOverlay.tsx` (que solo dibuja/interactua) para que
 * `App.tsx` -- que es quien posee el estado, igual que ya hace con
 * `botsViewedTrades` etc. -- pueda cargar/guardar sin importar el overlay
 * completo. Mismo patron de storage que `TradePositionsOverlay.tsx`
 * (`liq-intel:positions:${symbol}`): por simbolo, con manejo silencioso de
 * fallas (localStorage lleno/bloqueado degrada a solo-memoria, no rompe la
 * feature). */

export type DrawingKind = "hline" | "vline" | "trend" | "rect";

export interface LineStyle {
  color: string;
  /** Grosor en px -- 4 opciones fijas pedidas por el usuario, no un rango
   * libre (mas simple de elegir con un click, como el selector de grosor
   * de TradingView). */
  thickness: 1 | 2 | 3 | 4;
}

export interface RectStyle extends LineStyle {
  fillColor: string;
  /** 0-1, no 0-100 -- se aplica directo como alpha en `ctx.fillStyle`. */
  fillOpacity: number;
}

interface Point {
  timeMs: number;
  price: number;
}

export type Drawing =
  | { id: string; kind: "hline"; price: number; style: LineStyle }
  | { id: string; kind: "vline"; timeMs: number; style: LineStyle }
  | { id: string; kind: "trend"; p1: Point; p2: Point; style: LineStyle }
  | { id: string; kind: "rect"; p1: Point; p2: Point; style: RectStyle };

export const THICKNESS_OPTIONS = [1, 2, 3, 4] as const;
export const DEFAULT_DRAW_COLOR = "#eab308";
export const DEFAULT_DRAW_THICKNESS: LineStyle["thickness"] = 2;
export const DEFAULT_FILL_COLOR = "#eab308";
export const DEFAULT_FILL_OPACITY = 0.15;

function storageKey(symbol: string): string {
  return `liq-intel:drawings:${symbol}`;
}

export function loadDrawings(symbol: string): Drawing[] {
  try {
    const raw = window.localStorage.getItem(storageKey(symbol));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

export function saveDrawings(symbol: string, drawings: Drawing[]): void {
  try {
    window.localStorage.setItem(storageKey(symbol), JSON.stringify(drawings));
  } catch {
    // Igual que savePositions: sigue funcionando en memoria esta sesion.
  }
}

function newId(): string {
  return `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

export function makeHLine(price: number, style: LineStyle): Drawing {
  return { id: newId(), kind: "hline", price, style };
}

export function makeVLine(timeMs: number, style: LineStyle): Drawing {
  return { id: newId(), kind: "vline", timeMs, style };
}

export function makeTrend(p1: Point, p2: Point, style: LineStyle): Drawing {
  return { id: newId(), kind: "trend", p1, p2, style };
}

export function makeRect(p1: Point, p2: Point, style: RectStyle): Drawing {
  return { id: newId(), kind: "rect", p1, p2, style };
}

// --- Apariencia del chart (colores de vela) --------------------------
//
// Pedido del usuario (2026-09-20): "el usuario puede cambiar el color de
// las velas y se debe guardar el cambio". A diferencia de `drawings`
// (por simbolo -- lineas dibujadas sobre BTCUSDT no pintan en ETHUSDT),
// esto es una preferencia de TEMA, global -- primera clave de
// localStorage de todo el proyecto que NO es por simbolo.

export interface CandleColors {
  upColor: string;
  downColor: string;
}

// Mismos literales que ya estaban hardcodeados en PriceChart.tsx --
// default identico al look actual, nadie ve un cambio hasta que elige
// otro color a mano.
export const DEFAULT_CANDLE_COLORS: CandleColors = {
  upColor: "#26a69a",
  downColor: "#ef5350",
};

const CANDLE_COLORS_KEY = "liq-intel:settings:chart-appearance";

export function loadCandleColors(): CandleColors {
  try {
    const raw = window.localStorage.getItem(CANDLE_COLORS_KEY);
    if (!raw) return DEFAULT_CANDLE_COLORS;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.upColor === "string" && typeof parsed?.downColor === "string") {
      return { upColor: parsed.upColor, downColor: parsed.downColor };
    }
    return DEFAULT_CANDLE_COLORS;
  } catch {
    return DEFAULT_CANDLE_COLORS;
  }
}

export function saveCandleColors(colors: CandleColors): void {
  try {
    window.localStorage.setItem(CANDLE_COLORS_KEY, JSON.stringify(colors));
  } catch {
    // Degrada a solo-memoria, igual que el resto de los storage helpers.
  }
}
