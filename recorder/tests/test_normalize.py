"""La convención de lado es lo más fácil de invertir sin enterarse.

Estos tests usan payloads copiados de la documentación oficial de cada
exchange, y afirman lo único que importa: los tres describen el mismo suceso
—se liquidó un largo— con palabras distintas, y nuestra normalización los hace
converger.
"""
import unittest

from liqrec import normalize as n

# Payload de ejemplo de la doc de Binance (Liquidation Order Streams)
BINANCE_SELL = {
    "e": "forceOrder", "E": 1568014460893,
    "o": {"s": "BTCUSDT", "S": "SELL", "o": "LIMIT", "f": "IOC", "q": "0.014",
          "p": "9910", "ap": "9910", "X": "FILLED", "l": "0.014", "z": "0.014",
          "T": 1568014460893},
}
BINANCE_BUY = {
    "e": "forceOrder", "E": 1568014460893,
    "o": {"s": "BTCUSDT", "S": "BUY", "q": "0.500", "p": "10100", "ap": "10100",
          "X": "FILLED", "l": "0.5", "z": "0.5", "T": 1568014460893},
}
# Payload de ejemplo de la doc de Bybit (allLiquidation)
BYBIT_BUY = {"T": 1739502302929, "s": "ROSEUSDT", "S": "Buy", "v": "20", "p": "0.04499"}
BYBIT_SELL = {"T": 1739502302929, "s": "ROSEUSDT", "S": "Sell", "v": "20", "p": "0.04499"}
# Payload real del canal liquidation-orders de OKX
OKX_LONG = {"bkLoss": "0", "bkPx": "1.057", "ccy": "", "posSide": "long",
            "side": "sell", "sz": "768", "ts": "1723892524781"}
OKX_SHORT = {"bkLoss": "0", "bkPx": "1.157", "ccy": "", "posSide": "short",
             "side": "buy", "sz": "768", "ts": "1723892524781"}


class TestSideConvention(unittest.TestCase):
    def test_binance_sell_order_means_long_liquidated(self):
        self.assertEqual(n.from_binance(BINANCE_SELL, 1).side, n.LONG)

    def test_binance_buy_order_means_short_liquidated(self):
        self.assertEqual(n.from_binance(BINANCE_BUY, 1).side, n.SHORT)

    def test_bybit_buy_means_long_liquidated(self):
        # Documentado textualmente por Bybit: "When you receive a Buy update,
        # this means that a long position has been liquidated".
        self.assertEqual(n.from_bybit(BYBIT_BUY, 1).side, n.LONG)

    def test_bybit_sell_means_short_liquidated(self):
        self.assertEqual(n.from_bybit(BYBIT_SELL, 1).side, n.SHORT)

    def test_okx_uses_posSide_directly(self):
        self.assertEqual(n.from_okx("DYDX-USDT-SWAP", OKX_LONG, 1, ct_val=1.0).side, n.LONG)
        self.assertEqual(n.from_okx("DYDX-USDT-SWAP", OKX_SHORT, 1, ct_val=1.0).side, n.SHORT)

    def test_okx_net_mode_falls_back_to_order_side(self):
        net = dict(OKX_LONG, posSide="net")
        self.assertEqual(n.from_okx("X-USDT-SWAP", net, 1, ct_val=1.0).side, n.LONG)

    def test_the_trap_binance_and_bybit_say_opposite_words(self):
        """Binance dice SELL y Bybit dice Buy para el MISMO suceso."""
        b = n.from_binance(BINANCE_SELL, 1)
        y = n.from_bybit(BYBIT_BUY, 1)
        self.assertEqual(b.raw_side, "SELL")
        self.assertEqual(y.raw_side, "Buy")
        self.assertEqual(b.side, y.side)      # ambos: long liquidado
        self.assertEqual(b.side, n.LONG)


class TestFields(unittest.TestCase):
    def test_binance_marked_throttled_bybit_not(self):
        self.assertTrue(n.from_binance(BINANCE_SELL, 1).is_throttled)
        self.assertFalse(n.from_bybit(BYBIT_BUY, 1).is_throttled)
        self.assertTrue(n.from_okx("X-USDT-SWAP", OKX_LONG, 1, ct_val=1.0).is_throttled)

    def test_price_kind_is_explicit(self):
        # Binance da precio medio de ejecución; Bybit y OKX, precio de bancarrota.
        self.assertEqual(n.from_binance(BINANCE_SELL, 1).price_kind, "avg_fill")
        self.assertEqual(n.from_bybit(BYBIT_BUY, 1).price_kind, "bankruptcy")
        self.assertEqual(n.from_okx("X-USDT-SWAP", OKX_LONG, 1, ct_val=1.0).price_kind, "bankruptcy")

    def test_okx_size_is_contracts_and_needs_ctval(self):
        sin = n.from_okx("BTC-USDT-SWAP", OKX_LONG, 1)              # sin ctVal
        con = n.from_okx("BTC-USDT-SWAP", OKX_LONG, 1, ct_val=0.01)  # con ctVal
        self.assertEqual(sin.qty_base, 0.0)      # no inventamos un factor
        self.assertAlmostEqual(con.qty_base, 7.68)
        self.assertAlmostEqual(con.quote_qty, 7.68 * 1.057)

    def test_binance_combined_stream_envelope_is_unwrapped(self):
        wrapped = {"stream": "!forceOrder@arr", "data": BINANCE_SELL}
        self.assertEqual(n.from_binance(wrapped, 1).symbol, "BTCUSDT")

    def test_binance_notional_uses_average_fill_price(self):
        liq = n.from_binance(BINANCE_SELL, 1)
        self.assertAlmostEqual(liq.quote_qty, 0.014 * 9910)

    def test_unknown_side_raises(self):
        with self.assertRaises(n.NormalizationError):
            n.from_binance({"o": dict(BINANCE_SELL["o"], S="WAT")}, 1)
        with self.assertRaises(n.NormalizationError):
            n.from_bybit(dict(BYBIT_BUY, S="Nope"), 1)

    def test_okx_ct_vals_extraction(self):
        m = n.okx_ct_vals([{"instId": "BTC-USDT-SWAP", "ctVal": "0.01"},
                           {"instId": "BAD"},
                           {"instId": "ETH-USDT-SWAP", "ctVal": "0.1"}])
        self.assertEqual(m, {"BTC-USDT-SWAP": 0.01, "ETH-USDT-SWAP": 0.1})


if __name__ == "__main__":
    unittest.main()
