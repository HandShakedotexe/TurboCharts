import { useCallback, useEffect, useRef, useState } from "react";
import type { Drawing } from "./drawings";

const MAX_HISTORY = 50;
// Ventana en la que cambios seguidos del MISMO tipo (p.ej. arrastrar el
// selector de color, que dispara onChange en cada pixel) cuentan como un
// solo paso de historial.
const COALESCE_WINDOW_MS = 1000;

interface Counts {
  key: string;
  undo: number;
  redo: number;
}

/**
 * Historial deshacer/rehacer para los dibujos (pedido del usuario,
 * 2026-10-20: "a veces muevo un dibujo accidentalmente pero no puedo
 * regresar un paso antes"). Guarda copias completas del array -- los
 * dibujos son pocos y baratos de copiar, y evita tener que invertir cada
 * tipo de operacion por separado.
 *
 * Quien cambia `drawings` llama `snapshot()` ANTES del cambio (una vez por
 * gesto, no por mousemove -- ver `emit` en DrawingToolsOverlay). El historial
 * es por simbolo (`resetKey`): los dibujos de otro simbolo son otro array
 * distinto, deshacer no debe mezclarlos. Las pilas viven en refs; lo que se
 * renderiza (`canUndo`/`canRedo`) sale de un estado con su propia `key`, asi
 * que un cambio de simbolo invalida los botones sin tocar estado en un efecto.
 */
export function useDrawingsHistory(
  drawings: Drawing[],
  setDrawings: (next: Drawing[]) => void,
  resetKey: string,
) {
  const drawingsRef = useRef(drawings);
  const resetKeyRef = useRef(resetKey);
  useEffect(() => {
    drawingsRef.current = drawings;
    resetKeyRef.current = resetKey;
  });
  const stackKeyRef = useRef(resetKey);
  const undoRef = useRef<Drawing[][]>([]);
  const redoRef = useRef<Drawing[][]>([]);
  const coalesceRef = useRef<{ key: string; at: number } | null>(null);
  const [counts, setCounts] = useState<Counts>({ key: resetKey, undo: 0, redo: 0 });

  // Llamado al inicio de cada operacion: si el simbolo cambio desde la
  // ultima vez, las pilas viejas ya no aplican.
  const syncKey = useCallback(() => {
    if (stackKeyRef.current !== resetKeyRef.current) {
      stackKeyRef.current = resetKeyRef.current;
      undoRef.current = [];
      redoRef.current = [];
      coalesceRef.current = null;
    }
  }, []);

  const publish = useCallback(() => {
    setCounts({ key: stackKeyRef.current, undo: undoRef.current.length, redo: redoRef.current.length });
  }, []);

  const snapshot = useCallback((coalesceKey?: string) => {
    syncKey();
    const now = Date.now();
    const last = coalesceRef.current;
    if (coalesceKey && last && last.key === coalesceKey && now - last.at < COALESCE_WINDOW_MS) {
      last.at = now;
      return;
    }
    coalesceRef.current = coalesceKey ? { key: coalesceKey, at: now } : null;
    undoRef.current.push(drawingsRef.current);
    if (undoRef.current.length > MAX_HISTORY) undoRef.current.shift();
    redoRef.current = [];
    publish();
  }, [syncKey, publish]);

  const undo = useCallback((): boolean => {
    syncKey();
    const prev = undoRef.current.pop();
    if (!prev) return false;
    redoRef.current.push(drawingsRef.current);
    coalesceRef.current = null;
    setDrawings(prev);
    publish();
    return true;
  }, [syncKey, publish, setDrawings]);

  const redo = useCallback((): boolean => {
    syncKey();
    const next = redoRef.current.pop();
    if (!next) return false;
    undoRef.current.push(drawingsRef.current);
    coalesceRef.current = null;
    setDrawings(next);
    publish();
    return true;
  }, [syncKey, publish, setDrawings]);

  const current = counts.key === resetKey;
  return {
    snapshot,
    undo,
    redo,
    canUndo: current && counts.undo > 0,
    canRedo: current && counts.redo > 0,
  };
}
