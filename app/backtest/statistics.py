"""Modulo de estadistica del backtester -- Sharpe/Sortino/max drawdown/win
rate, IC (information coefficient), bootstrap y Monte Carlo. Solo stdlib
(`statistics`, `random`, `bisect`), determinista con semilla -- sin
dependencia nueva, coherente con la filosofia de dependencias minimas del
proyecto.
"""
from __future__ import annotations

import bisect
import calendar
import random
import statistics as pystats
from datetime import datetime, timezone
from typing import Optional, Sequence

from .engine import BacktestResult, Trade


def sharpe(returns: Sequence[float], periods_per_year: float) -> float:
    """`returns` es una serie de retornos POR PERIODO (no acumulados,
    p.ej. cambio de equity barra a barra). 0.0 si hay menos de 2 puntos o
    la serie no tiene varianza -- mejor que dividir por cero."""
    if len(returns) < 2:
        return 0.0
    stdev = pystats.pstdev(returns)
    if stdev == 0:
        return 0.0
    return (pystats.fmean(returns) / stdev) * (periods_per_year ** 0.5)


def sortino(returns: Sequence[float], periods_per_year: float) -> float:
    """Como `sharpe`, pero la desviacion solo sobre los retornos NEGATIVOS
    (downside deviation) -- no penaliza la volatilidad al alza."""
    if len(returns) < 2:
        return 0.0
    downside = [r for r in returns if r < 0]
    if not downside:
        return 0.0
    downside_dev = pystats.pstdev(downside) if len(downside) > 1 else abs(downside[0])
    if downside_dev == 0:
        return 0.0
    return (pystats.fmean(returns) / downside_dev) * (periods_per_year ** 0.5)


def max_drawdown(equity_values: Sequence[float]) -> float:
    """Fraccion (0.10 = 10%) desde el maximo previo. `peak` solo se
    considera si es positivo -- un `peak` en cero o negativo no tiene un
    "porcentaje de caida" con sentido economico."""
    peak = float("-inf")
    worst = 0.0
    for v in equity_values:
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > worst:
            worst = dd
    return worst


def win_rate(trades: Sequence[Trade]) -> float:
    closed = [t for t in trades if t.pnl_usd is not None]
    if not closed:
        return 0.0
    return sum(1 for t in closed if t.pnl_usd > 0) / len(closed)


def gross_pnl_usd(t: Trade) -> Optional[float]:
    """P&L de un trade ANTES de comisiones/funding -- `engine._close` ya
    calcula esto como `price_pnl` pero no lo persiste en `Trade` (solo el
    neto, `pnl_usd`); se reconstruye sumando los costos de vuelta en vez de
    tocar el motor o el schema por una metrica derivada."""
    if t.pnl_usd is None:
        return None
    return t.pnl_usd + t.commission_usd + t.funding_paid_usd


def gross_performance_usd(trades: Sequence[Trade]) -> float:
    return sum(gross_pnl_usd(t) or 0.0 for t in trades if t.pnl_usd is not None)


def net_performance_usd(trades: Sequence[Trade]) -> float:
    return sum(t.pnl_usd for t in trades if t.pnl_usd is not None)


def profit_factor(trades: Sequence[Trade]) -> Optional[float]:
    """Ganancia bruta de trades ganadores / perdida bruta (valor absoluto)
    de trades perdedores -- sobre P&L NETO, no el bruto (una estrategia
    debe evaluarse por su resultado neto real). `None` si no hay ninguna
    perdida cerrada -- un profit factor "infinito" no es un numero util
    para comparar ni para el score compuesto; se distingue de 0.0 (que
    significaria "todo perdida, cero ganancia")."""
    closed = [t.pnl_usd for t in trades if t.pnl_usd is not None]
    if not closed:
        return None
    gross_win = sum(p for p in closed if p > 0)
    gross_loss = abs(sum(p for p in closed if p < 0))
    if gross_loss == 0:
        return None
    return gross_win / gross_loss


def expectancy_usd(trades: Sequence[Trade]) -> Optional[float]:
    """Expectancy media en USD por trade cerrado -- complementa
    `expectancy_r` (que cubre R) sobre el MISMO universo de trades
    cerrados, sin filtrar por si tienen SL definido como si hace
    `expectancy_r`."""
    closed = [t.pnl_usd for t in trades if t.pnl_usd is not None]
    if not closed:
        return None
    return pystats.fmean(closed)


def _rank(values: Sequence[float]) -> list[float]:
    """Rango 1-indexado con promedio en empates (rango estandar de
    Spearman) -- sin esto, un empate desharia la simetria de la
    correlacion sobre datos con muchos valores repetidos (p.ej. un
    indicador con periodo de calentamiento en 0.0)."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    mean_x, mean_y = pystats.fmean(xs), pystats.fmean(ys)
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return 0.0
    return cov / (var_x ** 0.5 * var_y ** 0.5)


def information_coefficient(signal_values: Sequence[float], forward_returns: Sequence[float]) -> float:
    """Correlacion de Spearman entre el valor de un indicador en `t` y el
    retorno futuro sobre los siguientes N periodos -- la forma honesta de
    comprobar que un indicador predice algo en vez de solo mirar el P&L
    final de un backtest (que puede salir bien por una sola racha de
    suerte)."""
    if len(signal_values) != len(forward_returns) or len(signal_values) < 2:
        return 0.0
    return _pearson(_rank(signal_values), _rank(forward_returns))


def bootstrap_ci(
    trade_returns: Sequence[float], n_resamples: int = 1000, ci: float = 0.90, seed: Optional[int] = None,
) -> tuple[float, float]:
    """Intervalo de confianza del retorno medio por trade, remuestreando
    CON reemplazo -- responde "que tan seguro puedo estar de que el
    retorno medio no es cero" sin asumir una distribucion normal."""
    if not trade_returns:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(trade_returns)
    means = []
    for _ in range(n_resamples):
        sample = [trade_returns[rng.randrange(n)] for _ in range(n)]
        means.append(pystats.fmean(sample))
    means.sort()
    tail = (1 - ci) / 2
    lo = means[max(0, int(tail * n_resamples))]
    hi = means[min(n_resamples - 1, int((1 - tail) * n_resamples))]
    return (lo, hi)


def _cum_curve(pnls: Sequence[float], base: float = 0.0) -> list[float]:
    curve = [base]
    running = base
    for p in pnls:
        running += p
        curve.append(running)
    return curve


def monte_carlo_shuffle(
    trade_returns: Sequence[float], n_shuffles: int = 1000, seed: Optional[int] = None,
) -> dict:
    """Baraja el ORDEN de los trades (no sus valores) para ver cuanto de
    la forma de la curva de equity depende de la secuencia en la que
    ocurrieron -- una curva "bonita" que se desarma con solo reordenar los
    mismos trades es una señal de que la secuencia real fue mas suerte que
    señal."""
    if not trade_returns:
        return {"max_drawdown_p50": 0.0, "max_drawdown_p95": 0.0,
                "final_equity_p05": 0.0, "final_equity_p50": 0.0, "final_equity_p95": 0.0}
    rng = random.Random(seed)
    pnls = list(trade_returns)
    dds, finals = [], []
    for _ in range(n_shuffles):
        shuffled = pnls[:]
        rng.shuffle(shuffled)
        curve = _cum_curve(shuffled)
        dds.append(max_drawdown(curve))
        finals.append(curve[-1])
    dds.sort()
    finals.sort()

    def _pct(sorted_vals, p):
        return sorted_vals[min(len(sorted_vals) - 1, int(p * len(sorted_vals)))]

    return {
        "max_drawdown_p50": _pct(dds, 0.50),
        "max_drawdown_p95": _pct(dds, 0.95),
        "final_equity_p05": _pct(finals, 0.05),
        "final_equity_p50": _pct(finals, 0.50),
        "final_equity_p95": _pct(finals, 0.95),
    }


def stats_by_regime(trades: Sequence[Trade], bar_times_ms: Sequence[int], regime_labels: Sequence[str]) -> dict:
    """Segmenta el pnl de cada trade CERRADO por el regimen vigente en su
    barra de ENTRADA (no de salida -- el regimen que importa para juzgar
    la decision es el que existia cuando se tomo, ver regime.py)."""
    buckets: dict[str, list[float]] = {}
    for t in trades:
        if t.pnl_usd is None:
            continue
        if not regime_labels:
            label = "unknown"
        else:
            idx = min(bisect.bisect_left(bar_times_ms, t.entry_time_ms), len(regime_labels) - 1)
            label = regime_labels[max(0, idx)]
        buckets.setdefault(label, []).append(t.pnl_usd)
    return {
        label: {
            "count": len(pnls),
            "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0,
            "total_pnl_usd": sum(pnls),
            "avg_pnl_usd": pystats.fmean(pnls) if pnls else 0.0,
        }
        for label, pnls in buckets.items()
    }


def equity_curve_returns(equity_curve: Sequence[tuple[int, float]]) -> list[float]:
    """Retornos periodo a periodo (fraccion, no USD) sobre la curva de
    equity de un `BacktestResult` -- entrada de `sharpe`/`sortino`."""
    values = [v for _, v in equity_curve]
    out = []
    for i in range(1, len(values)):
        prev = values[i - 1]
        out.append((values[i] - prev) / prev if prev else 0.0)
    return out


# --- Criterio de validacion ------------------------------------------
#
# Un umbral simple de "minimo de trades + % de aciertos" deja pasar
# candidatas mediocres por puro azar y excluye por diseño estrategias de
# tendencia legitimas. El criterio usado en su lugar:
#   1. Cota inferior del IC95% de la expectancy en R > 0.
#   2. trials_count obligatorio, con correccion por comparaciones multiples.
#   3. Out-of-sample independiente como condicion NECESARIA.
#   4. Win rate siempre con su intervalo, nunca suelto.
# Las funciones de abajo son las piezas que permiten aplicar ese criterio
# de verdad, no solo describirlo.

MIN_TRADES_FOR_REPORTING = 200


def win_rate_ci(
    trades: Sequence[Trade], n_resamples: int = 1000, ci: float = 0.95, seed: Optional[int] = None,
) -> tuple[float, float]:
    """Intervalo de confianza del win rate por bootstrap -- para que nunca
    se muestre suelto (criterio 4 de arriba). Bootstrap sobre la
    proporcion, no una aproximacion normal: con pocos trades (frecuente en
    este proyecto) la normal es mala aproximacion cerca de 0 o 1."""
    closed = [t for t in trades if t.pnl_usd is not None]
    if not closed:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(closed)
    wins = [1.0 if t.pnl_usd > 0 else 0.0 for t in closed]
    rates = []
    for _ in range(n_resamples):
        sample = [wins[rng.randrange(n)] for _ in range(n)]
        rates.append(sum(sample) / n)
    rates.sort()
    tail = (1 - ci) / 2
    lo = rates[max(0, int(tail * n_resamples))]
    hi = rates[min(n_resamples - 1, int((1 - tail) * n_resamples))]
    return (lo, hi)


def _r_multiples(trades: Sequence[Trade]) -> list[float]:
    """Solo trades CERRADOS que ademas tienen un SL definido (`r_multiple`
    no es None) -- mezclar trades con y sin SL inventaria una unidad de
    riesgo comun que no existe para los que no lo tienen."""
    return [t.r_multiple for t in trades if t.r_multiple is not None]


def expectancy_r(trades: Sequence[Trade]) -> Optional[float]:
    """Expectancy media en unidades de R (criterio 1). None -- no 0.0 --
    si ningun trade cerrado define SL: "sin dato" y "expectancy cero" son
    afirmaciones distintas, y la primera es la verdadera aqui."""
    r_values = _r_multiples(trades)
    if not r_values:
        return None
    return pystats.fmean(r_values)


def expectancy_r_ci(
    trades: Sequence[Trade], n_resamples: int = 1000, ci: float = 0.95, seed: Optional[int] = None,
) -> Optional[tuple[float, float]]:
    """IC de la expectancy en R por bootstrap -- la cota INFERIOR de este
    intervalo es el numero que de verdad importa para el criterio 1: no
    "el promedio salio positivo" sino "incluso en el escenario pesimista
    del remuestreo, sigue siendo positivo"."""
    r_values = _r_multiples(trades)
    if not r_values:
        return None
    rng = random.Random(seed)
    n = len(r_values)
    means = []
    for _ in range(n_resamples):
        sample = [r_values[rng.randrange(n)] for _ in range(n)]
        means.append(pystats.fmean(sample))
    means.sort()
    tail = (1 - ci) / 2
    lo = means[max(0, int(tail * n_resamples))]
    hi = means[min(n_resamples - 1, int((1 - tail) * n_resamples))]
    return (lo, hi)


def expectancy_r_p_value(
    trades: Sequence[Trade], n_resamples: int = 1000, seed: Optional[int] = None,
) -> Optional[float]:
    """p-valor de una cola (H0: expectancy en R <= 0) por bootstrap: la
    fraccion de remuestreos cuya media SIGUE siendo <= 0. Entrada de
    `benjamini_hochberg` para el criterio 2 -- sin un p-valor por
    configuracion no hay nada que corregir por comparaciones multiples."""
    r_values = _r_multiples(trades)
    if not r_values:
        return None
    rng = random.Random(seed)
    n = len(r_values)
    count_le_zero = 0
    for _ in range(n_resamples):
        sample = [r_values[rng.randrange(n)] for _ in range(n)]
        if pystats.fmean(sample) <= 0:
            count_le_zero += 1
    return count_le_zero / n_resamples


def benjamini_hochberg(p_values: Sequence[Optional[float]], alpha: float = 0.05) -> list[bool]:
    """Correccion FDR de Benjamini-Hochberg -- criterio 2. Sin esto,
    evaluar muchas configuraciones (`trials_count`) y quedarse con la de
    mejor p-valor es exactamente el p-hacking que motiva este criterio de
    validacion: con suficientes candidatas mediocres, varias pasan un
    filtro simple por puro azar. Un `None` (estrategia sin SL, sin
    p-valor que evaluar) nunca sobrevive."""
    indexed = [(i, p) for i, p in enumerate(p_values) if p is not None]
    m = len(indexed)
    survives = [False] * len(p_values)
    if m == 0:
        return survives
    indexed.sort(key=lambda x: x[1])
    threshold_rank = -1
    for k in range(m, 0, -1):
        _, p = indexed[k - 1]
        if p <= (k / m) * alpha:
            threshold_rank = k
            break
    if threshold_rank >= 0:
        for i, _ in indexed[:threshold_rank]:
            survives[i] = True
    return survives


def sample_size_flag(trades: Sequence[Trade]) -> str:
    """"INSUFFICIENT_SAMPLE" por debajo del umbral de REPORTE -- el minimo
    de 200 se usa como umbral de reporte, no de descarte automatico."""
    closed = sum(1 for t in trades if t.pnl_usd is not None)
    return "OK" if closed >= MIN_TRADES_FOR_REPORTING else "INSUFFICIENT_SAMPLE"


def monthly_usd_per_day(trades: Sequence[Trade]) -> dict[str, float]:
    """P&L neto total de cada mes calendario (UTC, clave "YYYY-MM" segun
    `entry_time_ms`) dividido entre los dias reales de ese mes -- la
    unidad necesaria para evaluar si una estrategia sostiene un piso de
    $X/dia en varios meses CONSECUTIVOS. Se agrupa por `entry_time_ms` (no
    `exit_time_ms`): un trade cuenta para el mes en que SE ABRIO, igual
    que el resto del proyecto mide "trades/dia" por apertura, no por
    cierre. Meses sin ningun trade cerrado simplemente no aparecen en el
    dict -- `consecutive_months_meeting_floor` los trata como huecos que
    rompen la racha, no como floor cumplido ni incumplido."""
    totals: dict[str, float] = {}
    for t in trades:
        if t.pnl_usd is None:
            continue
        dt = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc)
        key = f"{dt.year:04d}-{dt.month:02d}"
        totals[key] = totals.get(key, 0.0) + t.pnl_usd
    return {
        key: total / calendar.monthrange(int(key[:4]), int(key[5:7]))[1]
        for key, total in totals.items()
    }


def consecutive_months_meeting_floor(monthly: dict[str, float], floor_usd_per_day: float) -> int:
    """Racha MAS LARGA de meses CONSECUTIVOS EN EL CALENDARIO (no solo
    adyacentes en el dict) donde `monthly[mes] >= floor_usd_per_day`. Un
    mes que falta en `monthly` (sin trades ese mes) rompe la racha igual
    que uno por debajo del piso -- "sostener" exige datos reales cada
    mes, no una racha que salte huecos silenciosamente."""
    if not monthly:
        return 0
    best = current = 0
    prev_year, prev_month = None, None
    for key in sorted(monthly):
        year, month = int(key[:4]), int(key[5:7])
        contiguous = prev_year is not None and (year, month) == (
            (prev_year + 1, 1) if prev_month == 12 else (prev_year, prev_month + 1)
        )
        current = current + 1 if contiguous and monthly[key] >= floor_usd_per_day else int(
            monthly[key] >= floor_usd_per_day
        )
        best = max(best, current)
        prev_year, prev_month = year, month
    return best


def sustains_daily_floor(trades: Sequence[Trade], floor_usd_per_day: float, min_consecutive_months: int = 3) -> bool:
    """Envoltorio de conveniencia: ¿esta estrategia sostuvo
    `floor_usd_per_day` en al menos `min_consecutive_months` meses
    consecutivos del historico? NO reemplaza `verdict()` (IC95%/muestra/
    PF), es un chequeo adicional que solo importa una vez que una
    estrategia ya paso ese criterio estadistico."""
    monthly = monthly_usd_per_day(trades)
    return consecutive_months_meeting_floor(monthly, floor_usd_per_day) >= min_consecutive_months


def weekly_usd_per_week(trades: Sequence[Trade]) -> dict[str, float]:
    """P&L neto total de cada semana ISO (UTC, clave "YYYY-Www" segun
    `entry_time_ms`) -- vista semanal en vez de diaria, pensada para una
    estrategia de pocas señales de alta conviccion, donde una meta DIARIA
    no tiene sentido (la mayoria de los dias no hay ningun trade cerrado).
    A diferencia de `monthly_usd_per_day` (divide entre los dias del mes
    para dar un promedio DIARIO), aca NO se divide -- la suma de la
    semana YA es la unidad pedida, "$/semana". Semana ISO (no de
    calendario Gregoriano) para que "semana" siempre tenga 7 dias
    completos, sin semanas partidas en el cambio de mes."""
    totals: dict[str, float] = {}
    for t in trades:
        if t.pnl_usd is None:
            continue
        dt = datetime.fromtimestamp(t.entry_time_ms / 1000, tz=timezone.utc)
        iso_year, iso_week, _ = dt.isocalendar()
        key = f"{iso_year:04d}-W{iso_week:02d}"
        totals[key] = totals.get(key, 0.0) + t.pnl_usd
    return totals


def consecutive_weeks_meeting_floor(weekly: dict[str, float], floor_usd_per_week: float) -> int:
    """Igual que `consecutive_months_meeting_floor` pero por semana ISO
    CONSECUTIVA en el calendario (no solo adyacente en el dict) -- una
    semana faltante (sin trades) rompe la racha igual que una por debajo
    del piso. `datetime.fromisocalendar` (stdlib) resuelve el lunes real
    de cada semana ISO -- mas simple y sin casos especiales de fin de año
    que reimplementar a mano el aritmetica de "semana 52/53 -> semana 1
    del año siguiente" como si fuera manual."""
    if not weekly:
        return 0
    best = current = 0
    prev_monday = None
    for key in sorted(weekly):
        year, week = int(key[:4]), int(key[6:8])
        monday = datetime.fromisocalendar(year, week, 1)
        contiguous = prev_monday is not None and (monday - prev_monday).days == 7
        current = current + 1 if contiguous and weekly[key] >= floor_usd_per_week else int(
            weekly[key] >= floor_usd_per_week
        )
        best = max(best, current)
        prev_monday = monday
    return best


def sustains_weekly_floor(trades: Sequence[Trade], floor_usd_per_week: float, min_consecutive_weeks: int = 3) -> bool:
    """Analogo semanal de `sustains_daily_floor` -- ver docstring de
    `weekly_usd_per_week` sobre por que la unidad real es semanal, no
    diaria. `min_consecutive_weeks` default 3 solo por simetria con los
    "3 meses" del piso mensual -- ninguna cifra fija todavia para el piso
    semanal, se reportan varios umbrales (0.5%/1%/2%/3%) en
    `validation.report_to_dict` en vez de elegir uno solo aca."""
    weekly = weekly_usd_per_week(trades)
    return consecutive_weeks_meeting_floor(weekly, floor_usd_per_week) >= min_consecutive_weeks


def summarize(result: BacktestResult, periods_per_year: float) -> dict:
    """Resumen que imprime la CLI -- junta todo el modulo en un solo
    diccionario serializable, para no tener que llamar cada funcion suelta
    en `__main__.py`."""
    returns = equity_curve_returns(result.equity_curve)
    trade_pnls = [t.pnl_usd for t in result.trades if t.pnl_usd is not None]
    equity_values = [v for _, v in result.equity_curve]
    return {
        "trades": len(result.trades),
        "win_rate": win_rate(result.trades),
        "win_rate_ci_95": win_rate_ci(result.trades, seed=0),
        "sample_size": sample_size_flag(result.trades),
        "expectancy_r": expectancy_r(result.trades),
        "expectancy_r_ci_95": expectancy_r_ci(result.trades, seed=0),
        "expectancy_usd": expectancy_usd(result.trades),
        "profit_factor": profit_factor(result.trades),
        "gross_performance_usd": gross_performance_usd(result.trades),
        "net_performance_usd": net_performance_usd(result.trades),
        "sharpe": sharpe(returns, periods_per_year),
        "sortino": sortino(returns, periods_per_year),
        "max_drawdown": max_drawdown(equity_values),
        "total_pnl_usd": sum(trade_pnls),
        "final_equity": equity_values[-1] if equity_values else result.initial_equity,
        "bootstrap_ci_90": bootstrap_ci(trade_pnls, seed=0),
        "monte_carlo": monte_carlo_shuffle(trade_pnls, seed=0),
        "by_regime": stats_by_regime(result.trades, result.bar_times_ms, result.regime_labels),
    }
