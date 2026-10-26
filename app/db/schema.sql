-- Point-in-time store para datos historicos de derivados (Fase 1).
--
-- Tres timestamps por fila, y NO son lo mismo (invariante 6):
--
--   event_time    Cuando ocurrio el hecho segun el propio exchange: el
--                 cierre de la vela, el timestamp del bucket de metrics, el
--                 momento de liquidacion del funding.
--
--   available_at  Cuando ese dato pudo haberse SABIDO en el mercado real.
--                 NO es un dato medido: es una estimacion conservadora del
--                 lag de publicacion tipico de cada endpoint (el porque de
--                 cada numero esta comentado junto a su tabla, en
--                 app/db/load_historical.py). Un backtest que pregunta "que
--                 se sabia a las 14:00" filtra SIEMPRE por
--                 available_at <= as_of, nunca por event_time -- filtrar por
--                 event_time deja usar informacion que en ese instante
--                 todavia no existia (look-ahead bias), exactamente lo que
--                 el spec original prohibe en sus secciones 27/30/34/56/61/62.
--
--   ingested_at   Cuando ESTE proceso metio la fila aqui. Es auditoria de la
--                 propia tuberia, sin significado de mercado.
--
-- IMPORTANTE: estos ficheros se descargan en bloque, mucho despues de los
-- hechos (todo 2020-2026 en una tarde). Por eso ingested_at es una fecha de
-- descarga (hoy) y NUNCA se debe confundir con available_at: si se usara
-- ingested_at como corte de disponibilidad, ningun dato historico parecería
-- disponible antes de hoy y cualquier backtest quedaria vacio.
--
-- Liquidaciones reales (grabador, Fase 0) y liquidaciones proyectadas
-- (heatmap, Fase 4) no tienen tabla aqui todavia y, cuando la tengan, seran
-- DOS tablas separadas (invariante 5) -- nunca una con una columna
-- "es_modelado".

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS candles (
    exchange                TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    interval                TEXT NOT NULL,
    event_time              TIMESTAMPTZ NOT NULL,   -- close_time de la vela
    available_at            TIMESTAMPTZ NOT NULL,
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    open                    DOUBLE PRECISION NOT NULL,
    high                    DOUBLE PRECISION NOT NULL,
    low                     DOUBLE PRECISION NOT NULL,
    close                   DOUBLE PRECISION NOT NULL,
    volume                  DOUBLE PRECISION NOT NULL,
    quote_volume            DOUBLE PRECISION,
    trades                  INTEGER,
    taker_buy_volume        DOUBLE PRECISION,
    taker_buy_quote_volume  DOUBLE PRECISION,
    source_file             TEXT NOT NULL,   -- trazabilidad: de que CSV vino
    PRIMARY KEY (exchange, symbol, interval, event_time)
);
SELECT create_hypertable('candles', 'event_time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS funding_rate (
    exchange                TEXT NOT NULL,
    symbol                  TEXT NOT NULL,
    event_time              TIMESTAMPTZ NOT NULL,   -- calc_time
    available_at            TIMESTAMPTZ NOT NULL,
    ingested_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    funding_interval_hours  SMALLINT,
    funding_rate            DOUBLE PRECISION NOT NULL,
    source_file             TEXT NOT NULL,
    PRIMARY KEY (exchange, symbol, event_time)
);
SELECT create_hypertable('funding_rate', 'event_time', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS open_interest_metrics (
    exchange                            TEXT NOT NULL,
    symbol                              TEXT NOT NULL,
    event_time                          TIMESTAMPTZ NOT NULL,   -- create_time del bucket de 5 min
    available_at                        TIMESTAMPTZ NOT NULL,
    ingested_at                         TIMESTAMPTZ NOT NULL DEFAULT now(),
    sum_open_interest                   DOUBLE PRECISION,
    sum_open_interest_value             DOUBLE PRECISION,
    count_toptrader_long_short_ratio    DOUBLE PRECISION,
    sum_toptrader_long_short_ratio      DOUBLE PRECISION,
    count_long_short_ratio              DOUBLE PRECISION,
    sum_taker_long_short_vol_ratio      DOUBLE PRECISION,
    source_file                         TEXT NOT NULL,
    PRIMARY KEY (exchange, symbol, event_time)
);
SELECT create_hypertable('open_interest_metrics', 'event_time', if_not_exists => TRUE);

-- Tramos de notional y maintenance margin rate (MMR) por simbolo. NO es una
-- serie temporal de mercado: es un snapshot de las reglas de margen vigentes
-- en el momento de la consulta (Binance las revisa de vez en cuando). Viene
-- de un endpoint publico y sin autenticacion que usa la propia pagina de
-- Binance (bapi/futures/v1/friendly/future/common/brackets) -- verificado
-- con curl plano, no requiere API key. Es la unica fuente real de MMR: antes
-- de esto, el modelo de heatmap habria tenido que inventar el numero.
--
-- fetched_at: cuando SE LEYO este snapshot, no cuando entro en vigor -- ver
-- la distincion event_time/available_at/ingested_at en la cabecera de este
-- fichero. Como esto no varia con el tiempo de mercado, no tiene event_time
-- propio: se usa fetched_at como el punto de referencia para "que tabla
-- estaba vigente cuando se calculo este heatmap".
CREATE TABLE IF NOT EXISTS margin_brackets (
    exchange                      TEXT NOT NULL,
    symbol                        TEXT NOT NULL,
    fetched_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    bracket_seq                   SMALLINT NOT NULL,
    notional_floor                DOUBLE PRECISION NOT NULL,
    notional_cap                  DOUBLE PRECISION NOT NULL,
    maintenance_margin_rate       DOUBLE PRECISION NOT NULL,
    cum_maintenance_amount        DOUBLE PRECISION NOT NULL,
    min_leverage                  SMALLINT,
    max_leverage                  SMALLINT NOT NULL,
    PRIMARY KEY (exchange, symbol, fetched_at, bracket_seq)
);

-- Trades ejecutados (aggTrades) por encima del umbral de "big trade" (ver
-- api/bigtrades.BIG_TRADE_THRESHOLD_USD). ESTO NO ES LIQUIDACIONES: es el
-- tape de mercado que ya usa el heatmap en vivo como entrada de
-- `heatmap_model.compute_levels_from_trades` para modelar niveles -- ver la
-- nota de la cabecera de este fichero sobre por que liquidaciones reales y
-- proyectadas van en tablas aparte cuando existan. Esta tabla extiende esa
-- MISMA entrada hacia atras en el tiempo con volcados historicos de
-- data.binance.vision (ver ingest/binance_historical.py), para que el
-- heatmap en vivo tenga mas historia que las 12h que cubre el REST en vivo
-- de bigtrades.py.
--
-- available_at = event_time: un trade impreso es publico al instante, sin
-- el lag de publicacion que sí tienen candles/funding/metrics.
CREATE TABLE IF NOT EXISTS big_trades (
    exchange                      TEXT NOT NULL,
    symbol                        TEXT NOT NULL,
    agg_trade_id                  BIGINT NOT NULL,
    event_time                    TIMESTAMPTZ NOT NULL,   -- transact_time
    available_at                  TIMESTAMPTZ NOT NULL,
    ingested_at                   TIMESTAMPTZ NOT NULL DEFAULT now(),
    price                         DOUBLE PRECISION NOT NULL,
    notional_usd                  DOUBLE PRECISION NOT NULL,
    -- Campo "m" de Binance ("Was the buyer the maker?") -- True implica que
    -- el VENDEDOR fue el agresor (taker sell), False que lo fue el
    -- comprador (taker buy). Agregada 2026-09-15 (pedido del usuario:
    -- burbujas de "Big trades" coloreadas rojo/verde por lado, ver
    -- BigTradesOverlay.tsx) -- NULLABLE porque las filas ya cargadas antes
    -- de esta columna (backfill historico previo) no la tienen y no hay
    -- forma barata de recuperarla sin re-descargar esos CSV; el frontend
    -- pinta esas burbujas en un color neutro en vez de inventar un lado.
    is_buyer_maker                BOOLEAN,
    source_file                   TEXT NOT NULL,
    -- event_time va en la PK (ademas de agg_trade_id, que ya es unico de
    -- por si) porque TimescaleDB exige que la columna de particion forme
    -- parte de cualquier indice unico de un hypertable.
    PRIMARY KEY (exchange, symbol, event_time, agg_trade_id)
);
SELECT create_hypertable('big_trades', 'event_time', if_not_exists => TRUE);

-- Trade journal auditable del backtester (Fase 2, ver app/backtest/).
-- NO son hypertables: son metadata de corridas de backtest, no series de
-- mercado -- no tiene sentido particionarlas por tiempo de evento de
-- mercado. `run_id`/generado en Python con uuid.uuid4().hex (no
-- gen_random_uuid(): evita asumir la extension pgcrypto).
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id         TEXT PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol         TEXT NOT NULL,
    interval       TEXT NOT NULL,
    strategy       TEXT NOT NULL,
    params         JSONB NOT NULL,
    data_start     TIMESTAMPTZ NOT NULL,
    data_end       TIMESTAMPTZ NOT NULL,
    commission_bps DOUBLE PRECISION NOT NULL,
    slippage_bps   DOUBLE PRECISION NOT NULL
);

-- signal_snapshot guarda los valores de los indicadores en el momento de
-- ENTRADA de cada trade -- lo que hace este journal auditable de verdad
-- (se puede reconstruir el "por que" de cualquier trade) y no solo un P&L
-- agregado, cumpliendo el requisito anti-caja-negra del spec original.
CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id            TEXT NOT NULL REFERENCES backtest_runs(run_id),
    trade_seq         INTEGER NOT NULL,
    side              TEXT NOT NULL,
    entry_time        TIMESTAMPTZ NOT NULL,
    entry_price       DOUBLE PRECISION NOT NULL,
    exit_time         TIMESTAMPTZ,
    exit_price        DOUBLE PRECISION,
    size_usd          DOUBLE PRECISION NOT NULL,
    commission_usd    DOUBLE PRECISION NOT NULL,
    funding_paid_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    pnl_usd           DOUBLE PRECISION,
    signal_snapshot   JSONB,
    -- Trade journal ampliado (2026-09-03, a peticion del usuario) --
    -- slippage_usd YA esta implicito en pnl_usd (entry/exit_price ya vienen
    -- con slip aplicado); es el desglose informativo de cuanto de ese costo
    -- es slippage vs comision, no una resta adicional. mfe_usd/mae_usd usan
    -- el high/low de cada barra mientras la posicion estuvo abierta, no solo
    -- el close. exit_signal_snapshot/exit_reason documentan POR QUE salio
    -- cada trade (señal de la estrategia vs cierre forzoso de fin de
    -- ventana) -- ver docstring de engine.Trade.
    slippage_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
    mfe_usd               DOUBLE PRECISION NOT NULL DEFAULT 0,
    mae_usd               DOUBLE PRECISION NOT NULL DEFAULT 0,
    exit_signal_snapshot  JSONB,
    exit_reason           TEXT,
    -- SL/TP real (Fase 3, 2026-09-03) -- NULL para las estrategias de
    -- referencia de Fase 2 (funding_squeeze_*), que no los definen. Sin
    -- estos no hay forma honesta de mostrar Riesgo %/R:R en el trade
    -- journal: se dejaron en N/A a proposito hasta que existiera una
    -- estrategia que de verdad usara un stop -- ver engine.Trade.
    stop_loss_price    DOUBLE PRECISION,
    take_profit_price  DOUBLE PRECISION,
    risk_usd           DOUBLE PRECISION,
    r_multiple         DOUBLE PRECISION,
    PRIMARY KEY (run_id, trade_seq)
);

-- Curva de equity de la corrida, para poder graficarla en la vista "Bots"
-- del frontend sin tener que re-simular el backtest solo para dibujarla.
CREATE TABLE IF NOT EXISTS backtest_equity_curve (
    run_id  TEXT NOT NULL REFERENCES backtest_runs(run_id),
    seq     INTEGER NOT NULL,
    time    TIMESTAMPTZ NOT NULL,
    equity  DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (run_id, seq)
);

-- Reportes de validacion walk-forward (Fase 3, backtest/validation.py) --
-- tabla separada de backtest_runs/backtest_trades porque un
-- WalkForwardReport no es un solo BacktestResult: son N tramos, cada uno
-- con su propio barrido in-sample y su propia corrida out-of-sample (ver
-- docstring de validation.walk_forward_validate). Antes `--validate` solo
-- imprimia JSON a la terminal y no quedaba rastro -- el usuario pidio
-- verlo en la vista "Bots" (2026-09-05: "coloca los datos de la
-- estrategia aunque haya fallado"), asi que ahora se guarda SIEMPRE, sin
-- importar el veredicto -- un NO PASA es un resultado honesto, no un
-- error que se descarta (mismo criterio que ya rige todo Fase 3).
-- `by_fold` guarda el detalle completo (parametros elegidos, trials,
-- sobrevive BH, trades OOS por tramo) en vez de una tabla propia por
-- fold: se lee entero de una vez para la vista de detalle, nunca se
-- filtra por columna individual de un fold.
CREATE TABLE IF NOT EXISTS backtest_validations (
    validation_id        TEXT PRIMARY KEY,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    symbol               TEXT NOT NULL,
    interval             TEXT NOT NULL,
    strategy             TEXT NOT NULL,
    data_start           TIMESTAMPTZ NOT NULL,
    data_end             TIMESTAMPTZ NOT NULL,
    min_train_days       INTEGER NOT NULL,
    test_days            INTEGER NOT NULL,
    folds                INTEGER NOT NULL,
    total_trials_count   INTEGER NOT NULL,
    oos_trades           INTEGER NOT NULL,
    win_rate             DOUBLE PRECISION NOT NULL,
    win_rate_ci_low      DOUBLE PRECISION NOT NULL,
    win_rate_ci_high     DOUBLE PRECISION NOT NULL,
    expectancy_r         DOUBLE PRECISION,
    expectancy_r_ci_low  DOUBLE PRECISION,
    expectancy_r_ci_high DOUBLE PRECISION,
    -- Goal 2026-09-05: PF/expectancy en USD sobre TODOS los trades OOS
    -- concatenados -- faltaban aqui aunque ya existian
    -- para una corrida unica (`statistics.summarize`), ver
    -- `validation.WalkForwardReport`. NULL cuando no hay perdidas cerradas
    -- (profit_factor) o cuando no hay ningun trade OOS cerrado (ambas).
    profit_factor        DOUBLE PRECISION,
    expectancy_usd       DOUBLE PRECISION,
    sample_size          TEXT NOT NULL,
    verdict              TEXT NOT NULL,
    -- Corregido 2026-09-19: la meta real nunca fue 50% DIARIO (malentendido
    -- de una sesion anterior) -- es crecer un % chico y consistente
    -- ($100 base, `validation.STARTING_CAPITAL_USD`) sin caer
    -- en drawdown grave. `max_drawdown_pct` = caida maxima real (0.10=10%)
    -- de esa cuenta corriendo TODOS los trades OOS en orden cronologico;
    -- `monthly_usd_per_day` = $/dia neto de cada mes calendario con trades
    -- (JSONB, {"YYYY-MM": float}) -- de ahi se deriva
    -- `consecutive_months_meeting_floor` para CUALQUIER umbral sin
    -- recorrer la DB de nuevo (ver `validation.report_to_dict`). NULL/'{}'
    -- sin trades cerrados, mismo criterio que `profit_factor`.
    max_drawdown_pct      DOUBLE PRECISION,
    monthly_usd_per_day   JSONB,
    -- $/semana ISO (2026-09-19, segunda correccion del usuario: la meta
    -- real es SEMANAL, no diaria -- "ajusta las reglas a que la ganancia
    -- porcentual sea semanal", pensado para una estrategia de pocos
    -- trades de alta conviccion). `statistics.weekly_usd_per_week`.
    weekly_usd_per_week   JSONB,
    by_fold              JSONB NOT NULL
);

-- Trades individuales out-of-sample de una validacion walk-forward, en el
-- MISMO formato que backtest_trades -- pedido explicito del usuario
-- (2026-09-05, viendo la vista de detalle de una validacion): "lo que no
-- veo es nuestro formato de tabla para los trades". `by_fold.oos_trades`
-- en backtest_validations solo guarda un CONTEO por tramo; esta tabla
-- guarda el trade completo (precio, SL/TP, P&L, R:R, MFE/MAE, disparador)
-- para que la vista "Bots" pueda reusar exactamente el mismo journal de
-- trades que ya usa RunDetail, no una version resumida aparte.
-- `fold_index` (no un timestamp de tramo) porque es lo unico que hace
-- falta para saber de que tramo salio un trade si algun dia se necesita
-- filtrar por tramo -- hoy la vista muestra todos los trades OOS juntos,
-- igual que backtest_trades muestra todos los de una corrida.
CREATE TABLE IF NOT EXISTS backtest_validation_trades (
    validation_id     TEXT NOT NULL REFERENCES backtest_validations(validation_id),
    fold_index        INTEGER NOT NULL,
    trade_seq         INTEGER NOT NULL,
    side              TEXT NOT NULL,
    entry_time        TIMESTAMPTZ NOT NULL,
    entry_price       DOUBLE PRECISION NOT NULL,
    exit_time         TIMESTAMPTZ,
    exit_price        DOUBLE PRECISION,
    size_usd          DOUBLE PRECISION NOT NULL,
    commission_usd    DOUBLE PRECISION NOT NULL,
    funding_paid_usd  DOUBLE PRECISION NOT NULL DEFAULT 0,
    pnl_usd           DOUBLE PRECISION,
    signal_snapshot   JSONB,
    slippage_usd          DOUBLE PRECISION NOT NULL DEFAULT 0,
    mfe_usd               DOUBLE PRECISION NOT NULL DEFAULT 0,
    mae_usd               DOUBLE PRECISION NOT NULL DEFAULT 0,
    exit_signal_snapshot  JSONB,
    exit_reason           TEXT,
    stop_loss_price    DOUBLE PRECISION,
    take_profit_price  DOUBLE PRECISION,
    risk_usd           DOUBLE PRECISION,
    r_multiple         DOUBLE PRECISION,
    PRIMARY KEY (validation_id, fold_index, trade_seq)
);

-- Paper trading "DAY TRADING" (2026-10-20, pedido del usuario): bitacora de
-- operaciones simuladas, lanzadas desde los indicadores Long/Short del chart.
-- Las tablas son de la APP (las escribe la API), a diferencia del resto del
-- esquema que solo carga el ingest/el backtester. Ver api/paper_model.py.
--
-- Tiempos en BIGINT milisegundos (no TIMESTAMPTZ): la simulacion compara
-- contra el `open_time` de velas de Binance, que ya viene en ms, y asi no hay
-- conversiones de zona horaria en el camino.
CREATE TABLE IF NOT EXISTS paper_settings (
    id                  INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    initial_balance     DOUBLE PRECISION NOT NULL DEFAULT 100,
    fee_pct             DOUBLE PRECISION NOT NULL DEFAULT 0.04,
    default_risk_pct    DOUBLE PRECISION NOT NULL DEFAULT 5,
    max_leverage        DOUBLE PRECISION NOT NULL DEFAULT 20,
    analyst_min_trades  INTEGER NOT NULL DEFAULT 20,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO paper_settings (id) VALUES (1) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id            BIGSERIAL PRIMARY KEY,
    created_ms          BIGINT NOT NULL,
    symbol              TEXT NOT NULL,
    interval            TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('long', 'short')),
    -- pending: orden limite esperando que el precio toque la entrada;
    -- open: llena, con SL/TP vigentes; closed: cerrada; cancelled: pendiente
    -- que el usuario cancelo antes de llenarse.
    status              TEXT NOT NULL CHECK (status IN ('pending', 'open', 'closed', 'cancelled')),
    order_type          TEXT NOT NULL CHECK (order_type IN ('market', 'limit')),
    entry_price         DOUBLE PRECISION NOT NULL,
    sl_price            DOUBLE PRECISION NOT NULL,
    tp_price            DOUBLE PRECISION NOT NULL,
    qty                 DOUBLE PRECISION NOT NULL,
    notional_usd        DOUBLE PRECISION NOT NULL,
    risk_usd            DOUBLE PRECISION NOT NULL,
    risk_pct            DOUBLE PRECISION NOT NULL,
    capped              BOOLEAN NOT NULL DEFAULT FALSE,
    balance_at_open     DOUBLE PRECISION NOT NULL,
    -- Lo que el usuario escribe: por que tomo la operacion. Es la materia
    -- prima del bot analista (api/paper_analyst.py).
    note                TEXT NOT NULL,
    tags                TEXT[] NOT NULL DEFAULT '{}',
    confidence          SMALLINT CHECK (confidence BETWEEN 1 AND 5),
    review_note         TEXT,
    followed_plan       BOOLEAN,
    source_position_id  TEXT,
    filled_at_ms        BIGINT,
    fill_bar_ms         BIGINT,
    closed_at_ms        BIGINT,
    exit_price          DOUBLE PRECISION,
    exit_reason         TEXT CHECK (exit_reason IN ('tp', 'sl', 'manual', 'cancelled')),
    ambiguous_exit      BOOLEAN NOT NULL DEFAULT FALSE,
    high_since_fill     DOUBLE PRECISION,
    low_since_fill      DOUBLE PRECISION,
    last_checked_ms     BIGINT NOT NULL,
    gross_pnl           DOUBLE PRECISION,
    fees                DOUBLE PRECISION,
    net_pnl             DOUBLE PRECISION,
    r_multiple          DOUBLE PRECISION,
    context             JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS paper_trades_status_idx ON paper_trades (status);
