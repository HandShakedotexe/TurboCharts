"""Registro de estrategias disponibles -- una sola fuente de verdad,
importada por `backtest/__main__.py` (CLI) y `api/routers/backtest.py`
(estado en vivo para la vista "Bots"). Antes vivia como un dict local
dentro de `__main__.py`; moverlo aqui evita que el router de la API tenga
que importar del modulo `__main__` (valido en Python, pero confuso) para
llegar a la misma lista."""
from __future__ import annotations

from .cvd_divergence import CvdDivergenceLong, CvdDivergenceShort
from .frvp_absorption import FrvpAbsorptionLong, FrvpAbsorptionShort
from .funding_oi_confluence import FundingOiConfluenceLong, FundingOiConfluenceShort
from .funding_squeeze import FundingSqueezeLong, FundingSqueezeShort
from .liquidation_bounce import LiquidationBounceLong, LiquidationBounceShort
from .liquidity_zone_4h import LiquidityZone4hLong, LiquidityZone4hShort
from .lvn_breakout import LvnBreakoutLong, LvnBreakoutShort
from .oi_trend_continuation import OiTrendContinuationLong, OiTrendContinuationShort
from .rsi_pullback import RsiPullbackLong, RsiPullbackShort
from .rsi_reversion import RsiReversionLong, RsiReversionShort
from .smc_confluence import SmcConfluenceLong, SmcConfluenceShort
from .smc_structure import (
    SmcStructureChochReversalLong, SmcStructureChochReversalShort,
    SmcStructureFreshMitigationLong, SmcStructureFreshMitigationShort,
    SmcStructureTrendZoneLong, SmcStructureTrendZoneShort,
)
from .volume_breakout import VolumeBreakoutLong, VolumeBreakoutShort

STRATEGIES = {
    "funding_squeeze_short": FundingSqueezeShort,
    "funding_squeeze_long": FundingSqueezeLong,
    "oi_trend_continuation_long": OiTrendContinuationLong,
    "oi_trend_continuation_short": OiTrendContinuationShort,
    "rsi_pullback_long": RsiPullbackLong,
    "rsi_pullback_short": RsiPullbackShort,
    "rsi_reversion_long": RsiReversionLong,
    "rsi_reversion_short": RsiReversionShort,
    "liquidation_bounce_long": LiquidationBounceLong,
    "liquidation_bounce_short": LiquidationBounceShort,
    "volume_breakout_long": VolumeBreakoutLong,
    "volume_breakout_short": VolumeBreakoutShort,
    "smc_confluence_long": SmcConfluenceLong,
    "smc_confluence_short": SmcConfluenceShort,
    "cvd_divergence_long": CvdDivergenceLong,
    "cvd_divergence_short": CvdDivergenceShort,
    "lvn_breakout_long": LvnBreakoutLong,
    "lvn_breakout_short": LvnBreakoutShort,
    "frvp_absorption_long": FrvpAbsorptionLong,
    "frvp_absorption_short": FrvpAbsorptionShort,
    # Mismas clases que cvd_divergence_long/short (misma señal de entrada),
    # pero registradas aparte con su propia grilla (ver __main__.py) para
    # barrer `breakeven_at_r` sin tocar el resultado ya documentado de la
    # version original.
    "cvd_divergence_breakeven_long": CvdDivergenceLong,
    "cvd_divergence_breakeven_short": CvdDivergenceShort,
    # Estructura SMC calculada y operada en SU PROPIA temporalidad de
    # trading (1h/4h, no 5m) -- ver docstring de smc_structure.py para la
    # distincion real con smc_confluence_* (que opera en 5m con 1h solo
    # de contexto).
    "smc_structure_trend_zone_long": SmcStructureTrendZoneLong,
    "smc_structure_trend_zone_short": SmcStructureTrendZoneShort,
    "smc_structure_choch_reversal_long": SmcStructureChochReversalLong,
    "smc_structure_choch_reversal_short": SmcStructureChochReversalShort,
    "smc_structure_fresh_mitigation_long": SmcStructureFreshMitigationLong,
    "smc_structure_fresh_mitigation_short": SmcStructureFreshMitigationShort,
    # Zona de liquidez real (cluster de liquidacion + perfil de volumen)
    # operada en 4h -- ver docstring de liquidity_zone_4h.py.
    "liquidity_zone_4h_long": LiquidityZone4hLong,
    "liquidity_zone_4h_short": LiquidityZone4hShort,
    # Confluencia funding+OI en 1h -- ninguna de las estrategias
    # anteriores combina estas dos senales en la misma regla de entrada.
    "funding_oi_confluence_long": FundingOiConfluenceLong,
    "funding_oi_confluence_short": FundingOiConfluenceShort,
}
