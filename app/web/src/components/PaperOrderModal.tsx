import { useEffect, useMemo, useState } from "react";
import { createPaperTrade, fetchPaperTrades, type PaperSettings, type PaperTrade } from "../api";
import type { TradePosition } from "./TradePositionsOverlay";

interface Props {
  position: TradePosition;
  symbol: string;
  interval: string;
  onClose: () => void;
  onLaunched: (trade: PaperTrade) => void;
}

/**
 * Formulario para lanzar una operacion de paper trading desde un indicador
 * Long/Short del chart, que funciona como trigger. La entrada/SL/TP son las del
 * indicador tal como estan dibujados; aca se elige el tipo de orden, el
 * riesgo, y -- lo central -- se escribe POR QUE se toma la operacion, que es
 * la materia prima del bot analista.
 */
export function PaperOrderModal({ position, symbol, interval, onClose, onLaunched }: Props) {
  const [settings, setSettings] = useState<PaperSettings | null>(null);
  const [balance, setBalance] = useState<number | null>(null);
  const [orderType, setOrderType] = useState<"market" | "limit">("limit");
  const [riskPct, setRiskPct] = useState<string>("");
  const [note, setNote] = useState("");
  const [tags, setTags] = useState("");
  const [confidence, setConfidence] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // refresh=false: solo hace falta el balance y los ajustes, no resolver ordenes.
    fetchPaperTrades(false)
      .then((r) => {
        if (cancelled) return;
        setSettings(r.settings);
        setBalance(r.summary.balance);
        setRiskPct((prev) => prev || String(r.settings.default_risk_pct));
      })
      .catch((e) => !cancelled && setError(String(e)));
    return () => {
      cancelled = true;
    };
  }, []);

  const preview = useMemo(() => {
    const risk = Number(riskPct);
    if (!settings || balance === null || !(risk > 0)) return null;
    const distance = Math.abs(position.entryPrice - position.slPrice);
    if (distance <= 0) return null;
    let qty = (balance * risk) / 100 / distance;
    let capped = false;
    if (qty * position.entryPrice > balance * settings.max_leverage) {
      qty = (balance * settings.max_leverage) / position.entryPrice;
      capped = true;
    }
    const rr = Math.abs(position.tpPrice - position.entryPrice) / distance;
    // El TP mas cerca que las comisiones de ida y vuelta: aunque se cumpla, la
    // operacion pierde dinero.
    const tpPct = (Math.abs(position.tpPrice - position.entryPrice) / position.entryPrice) * 100;
    const feeRoundTripPct = settings.fee_pct * 2;
    return {
      qty, notional: qty * position.entryPrice, riskUsd: qty * distance, leverage: (qty * position.entryPrice) / balance,
      rr, capped, tpPct, feeRoundTripPct, tpBelowFees: tpPct <= feeRoundTripPct,
    };
  }, [settings, balance, riskPct, position]);

  const submit = async () => {
    if (!note.trim()) {
      setError("Escribe por que tomas esta operacion: sin esa razon el analista no tiene con que trabajar.");
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const trade = await createPaperTrade({
        symbol,
        interval,
        side: position.side,
        entry_price: position.entryPrice,
        sl_price: position.slPrice,
        tp_price: position.tpPrice,
        order_type: orderType,
        risk_pct: Number(riskPct) || undefined,
        note: note.trim(),
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
        confidence: confidence ?? undefined,
        source_position_id: position.id,
      });
      onLaunched(trade);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setSubmitting(false);
    }
  };

  const sideLabel = position.side === "long" ? "LONG" : "SHORT";

  return (
    <div className="paper-modal-backdrop" onMouseDown={onClose}>
      <div className="paper-modal" onMouseDown={(e) => e.stopPropagation()} role="dialog" aria-label="Lanzar a paper trading">
        <div className="paper-modal-header">
          <span>
            <span className={`strategy-direction ${position.side}`}>{sideLabel}</span> {symbol} &middot; {interval} &mdash; lanzar a paper trading
          </span>
          <button type="button" className="chart-tools-panel-close" onClick={onClose} title="Cerrar">
            &times;
          </button>
        </div>

        <div className="paper-modal-levels">
          <span>Entrada <b>{position.entryPrice.toLocaleString()}</b></span>
          <span className="trade-loss">SL <b>{position.slPrice.toLocaleString()}</b></span>
          <span className="trade-win">TP <b>{position.tpPrice.toLocaleString()}</b></span>
          <span>R:R <b>{preview ? preview.rr.toFixed(2) : "--"}</b></span>
        </div>

        <label className="paper-field">
          <span>Tipo de orden</span>
          <div className="paper-row">
            <button type="button" className={`chart-tools-tool${orderType === "limit" ? " on" : ""}`} onClick={() => setOrderType("limit")}
              title="Queda pendiente y entra cuando el precio toque la linea de entrada del indicador.">
              Pendiente (limit)
            </button>
            <button type="button" className={`chart-tools-tool${orderType === "market" ? " on" : ""}`} onClick={() => setOrderType("market")}
              title="Entra ya al precio actual; SL y TP se quedan donde los dibujaste.">
              Mercado (ya)
            </button>
          </div>
          {orderType === "limit" && (
            <small className="paper-hint">
              Entra solo si el precio toca {position.entryPrice.toLocaleString()} (hasta que lo toque, queda pendiente y puedes cancelarla).
            </small>
          )}
          {orderType === "market" && (
            <small className="paper-hint">Entra al precio actual de {symbol}; si ya cruzo tu SL o TP, la API lo rechaza.</small>
          )}
        </label>

        <label className="paper-field">
          <span>Riesgo por operacion (% del balance{balance !== null ? `: $${balance.toFixed(2)}` : ""})</span>
          <input type="number" min={0.1} max={100} step={0.5} value={riskPct} onChange={(e) => setRiskPct(e.target.value)} />
          {preview && (
            <small className="paper-hint">
              Arriesgas ${preview.riskUsd.toFixed(2)} &middot; tamano {preview.qty.toPrecision(3)} {symbol.replace("USDT", "")} &middot; nocional ${preview.notional.toFixed(2)} &middot; apalancamiento {preview.leverage.toFixed(1)}x
              {preview.capped && " (recortado al apalancamiento maximo: arriesgas menos de lo pedido)"}
            </small>
          )}
        </label>

        {preview?.tpBelowFees && (
          <p className="error">
            El TP esta a {preview.tpPct.toFixed(3)}% de la entrada y las comisiones de ida y vuelta suman {preview.feeRoundTripPct.toFixed(2)}%:
            aunque llegue al TP, la operacion pierde dinero.
          </p>
        )}

        <label className="paper-field">
          <span>Por que tomas esta operacion *</span>
          <textarea
            rows={4}
            value={note}
            placeholder="Ej.: barrido de liquidez del pool inferior + vela de rechazo en 5m, el precio perdio el nivel y vuelve al rango."
            onChange={(e) => setNote(e.target.value)}
          />
        </label>

        <div className="paper-row paper-row-split">
          <label className="paper-field">
            <span>Tags (separados por coma)</span>
            <input type="text" value={tags} placeholder="barrido, pool, ny" onChange={(e) => setTags(e.target.value)} />
          </label>
          <div className="paper-field">
            <span>Confianza</span>
            <div className="paper-row">
              {[1, 2, 3, 4, 5].map((n) => (
                <button key={n} type="button" className={`chart-tools-tool${confidence === n ? " on" : ""}`}
                  onClick={() => setConfidence(confidence === n ? null : n)}>
                  {n}
                </button>
              ))}
            </div>
          </div>
        </div>

        {error && <p className="error">{error}</p>}
        <div className="paper-row paper-modal-actions">
          <button type="button" className="toolbar-button" onClick={onClose}>
            Cancelar
          </button>
          <button type="button" className="toolbar-button on" onClick={submit} disabled={submitting}>
            {submitting ? "Enviando..." : orderType === "market" ? "Abrir operacion" : "Crear orden pendiente"}
          </button>
        </div>
        <p className="paper-hint">
          Simulacion honesta: si una vela toca SL y TP a la vez, se asume SL; comision incluida; sin slippage ni funding.
        </p>
      </div>
    </div>
  );
}
