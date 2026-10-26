import { useEffect, useRef, useState } from "react";
import { THICKNESS_OPTIONS, type CandleColors, type DrawingKind, type LineStyle, type RectStyle } from "../drawings";

interface Props {
  placing: DrawingKind | null;
  onSetPlacing: (kind: DrawingKind | null) => void;
  lineStyle: LineStyle;
  onLineStyleChange: (style: LineStyle) => void;
  rectStyle: RectStyle;
  onRectStyleChange: (style: RectStyle) => void;
  hasSelection: boolean;
  /** Si el dibujo SELECCIONADO es un rectangulo -- distinto de
   * `hasSelection`, que es true para CUALQUIER tipo. Sin esto, la seccion
   * de relleno (que solo tiene sentido para un rectangulo) aparecia igual
   * al seleccionar una linea de tendencia (bug real, encontrado probando
   * esto en el navegador). */
  selectedIsRect: boolean;
  onDeleteSelected: () => void;
  /** Ocultar/mostrar TODOS los dibujos del chart (no los borra). */
  hideDrawings: boolean;
  onToggleHideDrawings: () => void;
  canUndo: boolean;
  canRedo: boolean;
  onUndo: () => void;
  onRedo: () => void;
  candleColors: CandleColors;
  onCandleColorsChange: (colors: CandleColors) => void;
  onClose: () => void;
}

const POS_KEY = "liq-intel:chart-tools-panel-pos";
const DEFAULT_POS = { x: 12, y: 12 };

function loadPos(): { x: number; y: number } {
  try {
    const raw = window.localStorage.getItem(POS_KEY);
    if (!raw) return DEFAULT_POS;
    const parsed = JSON.parse(raw);
    if (typeof parsed?.x === "number" && typeof parsed?.y === "number") return parsed;
    return DEFAULT_POS;
  } catch {
    return DEFAULT_POS;
  }
}

function savePos(pos: { x: number; y: number }): void {
  try {
    window.localStorage.setItem(POS_KEY, JSON.stringify(pos));
  } catch {
    // Solo memoria esta sesion -- no rompe nada, es una comodidad de UI.
  }
}

const TOOLS: Array<{ kind: DrawingKind; label: string; title: string }> = [
  { kind: "hline", label: "―", title: "Linea horizontal" },
  { kind: "vline", label: "❘", title: "Linea vertical" },
  { kind: "trend", label: "↗", title: "Linea diagonal (tendencia)" },
  { kind: "rect", label: "▭", title: "Rectangulo" },
];

/**
 * Panel flotante y arrastrable sobre el chart -- pedido del usuario
 * (2026-09-20): "las herramientas se me ocurre que puedes ponerlo en un
 * modal movible por encima del grafico para evitar seguir incluyendo cosas
 * al header, (Como el de trading view)". Aloja las 4 herramientas de
 * dibujo (`DrawingToolsOverlay.tsx` hace el render/interaccion real sobre
 * el chart -- este panel solo es el selector de herramienta/estilo) y la
 * seccion de apariencia (color de velas).
 *
 * Arrastre: mismo mecanismo manual de pixeles que ya usa la manija de
 * mover de `TradePositionsOverlay.tsx` (mousedown en la cabecera,
 * mousemove/mouseup en `window`), pero aca mueve el panel entero via
 * `left`/`top` en vez de una posicion de trade. Posicion persistida en
 * localStorage para que no "salte" de vuelta a la esquina en cada reload.
 */
export function ChartToolsPanel({
  placing, onSetPlacing, lineStyle, onLineStyleChange, rectStyle, onRectStyleChange,
  hasSelection, selectedIsRect, onDeleteSelected, hideDrawings, onToggleHideDrawings,
  canUndo, canRedo, onUndo, onRedo, candleColors, onCandleColorsChange, onClose,
}: Props) {
  const [pos, setPos] = useState(loadPos);
  const dragRef = useRef<{ startX: number; startY: number; origX: number; origY: number } | null>(null);

  useEffect(() => {
    const onMove = (evt: MouseEvent) => {
      const drag = dragRef.current;
      if (!drag) return;
      setPos({ x: drag.origX + (evt.clientX - drag.startX), y: drag.origY + (evt.clientY - drag.startY) });
    };
    const onUp = () => {
      if (dragRef.current) savePos(pos);
      dragRef.current = null;
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pos]);

  const onHeaderMouseDown = (evt: React.MouseEvent) => {
    dragRef.current = { startX: evt.clientX, startY: evt.clientY, origX: pos.x, origY: pos.y };
  };

  const isRectTool = placing === "rect" || selectedIsRect;

  return (
    <div className="chart-tools-panel" style={{ left: pos.x, top: pos.y }}>
      <div className="chart-tools-panel-header" onMouseDown={onHeaderMouseDown}>
        <span>Herramientas</span>
        <button type="button" className="chart-tools-panel-close" onClick={onClose} title="Cerrar">
          &times;
        </button>
      </div>
      <div className="chart-tools-panel-body">
        <div className="chart-tools-section">
          <span className="chart-tools-label">Dibujo</span>
          <div className="chart-tools-row">
            {TOOLS.map((t) => (
              <button
                key={t.kind}
                type="button"
                title={t.title}
                className={`chart-tools-tool${placing === t.kind ? " on" : ""}`}
                onClick={() => onSetPlacing(placing === t.kind ? null : t.kind)}
              >
                {t.label}
              </button>
            ))}
          </div>
        </div>

        <div className="chart-tools-section">
          <div className="chart-tools-row">
            <button
              type="button"
              className="chart-tools-tool"
              title="Deshacer el ultimo cambio en los dibujos (Ctrl+Z)"
              disabled={!canUndo}
              onClick={onUndo}
            >
              &#8630;
            </button>
            <button
              type="button"
              className="chart-tools-tool"
              title="Rehacer (Ctrl+Y)"
              disabled={!canRedo}
              onClick={onRedo}
            >
              &#8631;
            </button>
            <button
              type="button"
              className={`chart-tools-tool${hideDrawings ? " on" : ""}`}
              title={hideDrawings ? "Mostrar los dibujos otra vez" : "Ocultar todos los dibujos del grafico (no se borran)"}
              onClick={onToggleHideDrawings}
            >
              {hideDrawings ? "Mostrar dibujos" : "Ocultar dibujos"}
            </button>
          </div>
        </div>

        <div className="chart-tools-section">
          <span className="chart-tools-label">Grosor</span>
          <div className="chart-tools-row">
            {THICKNESS_OPTIONS.map((px) => (
              <button
                key={px}
                type="button"
                title={`${px}px`}
                className={`chart-tools-thickness${lineStyle.thickness === px ? " on" : ""}`}
                onClick={() => onLineStyleChange({ ...lineStyle, thickness: px })}
              >
                <span style={{ height: px }} />
              </button>
            ))}
            <input
              type="color"
              value={lineStyle.color}
              title="Color de linea"
              onChange={(e) => onLineStyleChange({ ...lineStyle, color: e.target.value })}
            />
          </div>
        </div>

        {isRectTool && (
          <div className="chart-tools-section">
            <span className="chart-tools-label">Rectangulo -- relleno</span>
            <div className="chart-tools-row">
              <input
                type="color"
                value={rectStyle.fillColor}
                title="Color de relleno"
                onChange={(e) => onRectStyleChange({ ...rectStyle, fillColor: e.target.value })}
              />
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={rectStyle.fillOpacity}
                title="Opacidad del relleno"
                onChange={(e) => onRectStyleChange({ ...rectStyle, fillOpacity: Number(e.target.value) })}
              />
              <span className="chart-tools-value">{Math.round(rectStyle.fillOpacity * 100)}%</span>
            </div>
          </div>
        )}

        {hasSelection && (
          <div className="chart-tools-section">
            <button type="button" className="chart-tools-delete" onClick={onDeleteSelected}>
              Eliminar seleccionado
            </button>
          </div>
        )}

        <div className="chart-tools-section">
          <span className="chart-tools-label">Apariencia de velas</span>
          <div className="chart-tools-row">
            <label className="chart-tools-swatch-label">
              Alcista
              <input
                type="color"
                value={candleColors.upColor}
                onChange={(e) => onCandleColorsChange({ ...candleColors, upColor: e.target.value })}
              />
            </label>
            <label className="chart-tools-swatch-label">
              Bajista
              <input
                type="color"
                value={candleColors.downColor}
                onChange={(e) => onCandleColorsChange({ ...candleColors, downColor: e.target.value })}
              />
            </label>
          </div>
        </div>
      </div>
    </div>
  );
}
