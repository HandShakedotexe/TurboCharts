"""Bot analista del paper trading: lee lo que el usuario escribio de cada
operacion (por que la tomo, tags, revision posterior) MAS los datos
reales del trade, y agrupa las operaciones que se parecen para sacar de
ahi "la logica de una estrategia".

Es estadistica clasica sobre texto, NO un modelo de lenguaje: TF-IDF sobre
las notas + similitud de la estructura del trade + clustering jerarquico de
enlace promedio. Ventajas deliberadas: corre local sin API key, es
determinista (la misma bitacora da siempre los mismos grupos) y cada grupo
se puede explicar ("se agruparon porque comparten estas palabras y esta
estructura"), que es lo que se necesita para confiar en el resultado. Lo
que NO hace: entender sinonimos ("barrido" vs "sweep" son palabras distintas
para el) ni razonar sobre el grafico. Para eso hace falta un LLM, que seria
un cambio aparte (una API key y enviar las notas del usuario a un servicio
externo -- decision suya, no se asume aqui).

Reglas:
* No se activa hasta tener `min_trades` operaciones cerradas (default 20):
  con menos, agrupar es ruido.
* Debe identificar grupos ("el trade 2, 7, 10 y 15 son parecidos").
* Un trade ganador con una logica distinta al resto NO se descarta: sale en
  `unique_cases` marcado como notable.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from typing import Optional

from .paper_model import planned_rr, rr_bucket, session_for

# Umbral de similitud promedio para fusionar dos grupos. Subirlo da grupos
# mas chicos y estrictos; bajarlo, grupos grandes y vagos. 0.45 sale de
# razonar los pesos de `pair_similarity`: la estructura sola (mismo lado,
# temporalidad, sesion y R:R, sin ninguna palabra en comun) llega a ~0.4 y
# NO debe alcanzar para agrupar -- hace falta que ademas coincida el relato.
DEFAULT_THRESHOLD = 0.45
# Tamano minimo para opinar si un grupo es una estrategia o algo a evitar.
MIN_GROUP_FOR_VERDICT = 5

_STOPWORDS = frozenset("""
a al algo algun alguna algunos ante antes aqui asi aun cada como con contra cual cuando de del desde donde
dos el ella ellos en entre era eran es esa ese eso esta estaba estan este esto estoy fue fui ha han hay
hasta la las le les lo los mas me mi mis muy ni no nos nuestro o otra otro para pero poco por porque que
quien se sea ser si sin sobre solo son su sus tambien tan te tengo tiene toda todo tu un una uno unos ver
voy ya yo the and for with this that from was are
""".split())


def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def tokenize(text: str) -> list[str]:
    tokens = []
    for raw in re.findall(r"[a-z0-9]+", _strip_accents(text.lower())):
        if len(raw) < 3 or raw in _STOPWORDS or raw.isdigit():
            continue
        # Plural simple ("pools" -> "pool", "liquidaciones" -> "liquidacion")
        if len(raw) > 4 and raw.endswith("es"):
            raw = raw[:-2]
        elif len(raw) > 3 and raw.endswith("s"):
            raw = raw[:-1]
        tokens.append(raw)
    return tokens


def _doc_tokens(trade: dict) -> list[str]:
    tokens = tokenize(trade.get("note") or "")
    tokens += tokenize(trade.get("review_note") or "")
    # Los tags son lo que el usuario eligio decir explicitamente: pesan doble.
    for tag in trade.get("tags") or []:
        tokens += tokenize(tag) * 2
    return tokens


def _tfidf(docs: list[list[str]]) -> list[dict[str, float]]:
    n = len(docs)
    df: Counter = Counter()
    for doc in docs:
        df.update(set(doc))
    vectors = []
    for doc in docs:
        tf = Counter(doc)
        vec = {t: c * (math.log((1 + n) / (1 + df[t])) + 1.0) for t, c in tf.items()}
        norm = math.sqrt(sum(v * v for v in vec.values()))
        vectors.append({t: v / norm for t, v in vec.items()} if norm > 0 else {})
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(v * b.get(t, 0.0) for t, v in a.items())


def _features(trade: dict) -> dict:
    entry_ms = trade.get("filled_at_ms") or trade.get("created_ms") or 0
    rr = planned_rr(trade["side"], trade["entry_price"], trade["sl_price"], trade["tp_price"])
    return {
        "side": trade["side"],
        "symbol": trade["symbol"],
        "interval": trade["interval"],
        "session": session_for(int(entry_ms)),
        "rr_bucket": rr_bucket(rr),
    }


_STRUCT_WEIGHTS = {"side": 0.4, "rr_bucket": 0.2, "interval": 0.15, "session": 0.15, "symbol": 0.1}


def pair_similarity(fa: dict, fb: dict, va: dict[str, float], vb: dict[str, float]) -> float:
    struct = sum(w for k, w in _STRUCT_WEIGHTS.items() if fa[k] == fb[k])
    if va and vb:
        sim = 0.6 * _cosine(va, vb) + 0.4 * struct
    else:
        # Sin texto en alguno de los dos no hay relato que comparar: la
        # estructura sola no debe bastar para agrupar con confianza.
        sim = 0.6 * struct
    if fa["side"] != fb["side"]:
        sim *= 0.5
    return sim


def _cluster(sim: list[list[float]], threshold: float) -> list[list[int]]:
    clusters = [[i] for i in range(len(sim))]
    while len(clusters) > 1:
        best = -1.0
        best_pair = (0, 1)
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                total = sum(sim[a][b] for a in clusters[i] for b in clusters[j])
                avg = total / (len(clusters[i]) * len(clusters[j]))
                if avg > best:
                    best = avg
                    best_pair = (i, j)
        if best < threshold:
            break
        i, j = best_pair
        clusters[i] = clusters[i] + clusters[j]
        del clusters[j]
    return clusters


def _group_stats(members: list[dict]) -> dict:
    n = len(members)
    wins = [t for t in members if t["net_pnl"] > 0]
    pnl = sum(t["net_pnl"] for t in members)
    gross_win = sum(t["net_pnl"] for t in wins)
    gross_loss = -sum(t["net_pnl"] for t in members if t["net_pnl"] <= 0)
    r_values = [t["r_multiple"] for t in members if t.get("r_multiple") is not None]
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": len(wins) / n,
        "net_pnl": pnl,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "expectancy_r": sum(r_values) / len(r_values) if r_values else None,
    }


def _dominant(values: list[str], min_share: float = 0.6) -> Optional[str]:
    if not values:
        return None
    value, count = Counter(values).most_common(1)[0]
    return value if count / len(values) >= min_share else None


def _verdict(stats: dict) -> tuple[str, str]:
    n = stats["trades"]
    exp = stats["expectancy_r"]
    if n < MIN_GROUP_FOR_VERDICT:
        return "pocos_datos", f"Solo {n} operaciones: todavia no alcanza para opinar si esto es una estrategia."
    if exp is not None and exp > 0 and stats["win_rate"] >= 0.4:
        return "candidata", "Candidata a estrategia: expectancy positiva con una muestra que ya dice algo. Validar con mas trades."
    if exp is not None and exp < 0:
        return "evitar", "Esta forma de operar pierde dinero de forma consistente en tu bitacora: revisar o dejar de hacerla."
    return "neutral", "Resultado cercano a cero: ni confirma ni descarta la logica."


def _describe_group(members: list[dict], feats: list[dict], top_terms: list[str], stats: dict) -> str:
    side = _dominant([f["side"] for f in feats])
    interval = _dominant([f["interval"] for f in feats])
    session = _dominant([f["session"] for f in feats])
    rr = _dominant([f["rr_bucket"] for f in feats])
    parts = []
    parts.append({"long": "Operaciones LONG", "short": "Operaciones SHORT"}.get(side or "", "Operaciones de ambos lados"))
    if interval:
        parts[0] += f" en {interval}"
    traits = []
    if session:
        traits.append(f"sesion {session.replace('_', ' ')}")
    if rr and rr != "?":
        traits.append(f"R:R planeado {rr}")
    if traits:
        parts.append("tipicamente " + ", ".join(traits))
    text = ", ".join(parts) + "."
    if top_terms:
        text += " En tus notas se repite: " + ", ".join(top_terms) + "."
    exp = stats["expectancy_r"]
    text += f" Resultado: {stats['wins']}/{stats['trades']} ganadas ({stats['win_rate'] * 100:.0f}%)"
    if exp is not None:
        text += f", expectancy {exp:+.2f}R"
    return text + "."


def _insights(closed: list[dict], feats: list[dict]) -> list[str]:
    notes: list[str] = []

    def bucket(key_fn) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for t, f in zip(closed, feats):
            k = key_fn(t, f)
            if k is not None:
                out.setdefault(k, []).append(t)
        return out

    def avg_r(ts: list[dict]) -> Optional[float]:
        rs = [t["r_multiple"] for t in ts if t.get("r_multiple") is not None]
        return sum(rs) / len(rs) if rs else None

    followed = bucket(lambda t, f: None if t.get("followed_plan") is None else ("si" if t["followed_plan"] else "no"))
    if len(followed.get("si", [])) >= 3 and len(followed.get("no", [])) >= 3:
        a, b = avg_r(followed["si"]), avg_r(followed["no"])
        if a is not None and b is not None:
            notes.append(
                f"Disciplina: cuando seguiste tu plan promedias {a:+.2f}R ({len(followed['si'])} trades); "
                f"cuando no, {b:+.2f}R ({len(followed['no'])} trades)."
            )
    by_session = {k: v for k, v in bucket(lambda t, f: f["session"]).items() if len(v) >= 3}
    if len(by_session) >= 2:
        scored = [(k, avg_r(v), len(v)) for k, v in by_session.items() if avg_r(v) is not None]
        if len(scored) >= 2:
            best = max(scored, key=lambda x: x[1])
            worst = min(scored, key=lambda x: x[1])
            notes.append(
                f"Sesion: te va mejor en {best[0].replace('_', ' ')} ({best[1]:+.2f}R, {best[2]} trades) "
                f"que en {worst[0].replace('_', ' ')} ({worst[1]:+.2f}R, {worst[2]} trades)."
            )
    by_side = {k: v for k, v in bucket(lambda t, f: f["side"]).items() if len(v) >= 3}
    if len(by_side) == 2:
        a, b = avg_r(by_side["long"]), avg_r(by_side["short"])
        if a is not None and b is not None:
            notes.append(f"Lado: LONG {a:+.2f}R ({len(by_side['long'])}) vs SHORT {b:+.2f}R ({len(by_side['short'])}).")
    by_conf = bucket(lambda t, f: None if t.get("confidence") is None else ("alta" if t["confidence"] >= 4 else "baja"))
    if len(by_conf.get("alta", [])) >= 3 and len(by_conf.get("baja", [])) >= 3:
        a, b = avg_r(by_conf["alta"]), avg_r(by_conf["baja"])
        if a is not None and b is not None:
            notes.append(
                f"Confianza: tus trades de confianza alta (4-5) promedian {a:+.2f}R; los de confianza baja, {b:+.2f}R. "
                + ("Tu intuicion esta calibrada." if a > b else "Tu confianza no esta prediciendo el resultado.")
            )
    return notes


def analyze(trades: list[dict], *, min_trades: int, force: bool = False, threshold: float = DEFAULT_THRESHOLD) -> dict:
    """`trades`: filas de `paper_trades` como dict (con `note`, `tags`, ...).
    Solo cuentan las cerradas."""
    closed = sorted(
        (t for t in trades if t["status"] == "closed" and t.get("net_pnl") is not None),
        key=lambda t: t["trade_id"],
    )
    n = len(closed)
    if n < min_trades and not force:
        return {
            "status": "waiting",
            "closed_trades": n,
            "min_trades": min_trades,
            "message": (
                f"El analista se activa con {min_trades} operaciones cerradas (llevas {n}). "
                "Con menos datos los grupos serian ruido. Puedes forzar el analisis para verlo igual."
            ),
        }
    if n < 2:
        return {"status": "waiting", "closed_trades": n, "min_trades": min_trades,
                "message": "Hace falta al menos 2 operaciones cerradas para comparar."}

    feats = [_features(t) for t in closed]
    vectors = _tfidf([_doc_tokens(t) for t in closed])
    size = len(closed)
    sim = [[1.0] * size for _ in range(size)]
    for i in range(size):
        for j in range(i + 1, size):
            sim[i][j] = sim[j][i] = pair_similarity(feats[i], feats[j], vectors[i], vectors[j])

    groups = []
    unique_cases = []
    for idx_list in sorted(_cluster(sim, threshold), key=lambda c: (-len(c), min(c))):
        members = [closed[i] for i in idx_list]
        member_feats = [feats[i] for i in idx_list]
        agg: Counter = Counter()
        for i in idx_list:
            agg.update(vectors[i])
        top_terms = [t for t, _ in agg.most_common(6)]
        stats = _group_stats(members)
        if len(idx_list) == 1:
            t = members[0]
            winner = t["net_pnl"] > 0
            unique_cases.append({
                "trade_id": t["trade_id"],
                "symbol": t["symbol"],
                "side": t["side"],
                "net_pnl": t["net_pnl"],
                "r_multiple": t.get("r_multiple"),
                "winner": winner,
                "note": t.get("note") or "",
                "message": (
                    "Ganadora con una logica distinta al resto: tomarla en cuenta como posible estrategia aparte."
                    if winner else "Perdedora sin parecido con otras operaciones: por ahora un caso aislado."
                ),
            })
            continue
        verdict, verdict_text = _verdict(stats)
        groups.append({
            **stats,
            "trade_ids": sorted(t["trade_id"] for t in members),
            "top_terms": top_terms,
            "verdict": verdict,
            "verdict_text": verdict_text,
            "logic": _describe_group(members, member_feats, top_terms, stats),
        })
    # Ganadoras unicas primero: son lo que el usuario pidio no perder de vista.
    unique_cases.sort(key=lambda c: (not c["winner"], c["trade_id"]))
    return {
        "status": "ready",
        "forced": n < min_trades,
        "closed_trades": n,
        "min_trades": min_trades,
        "groups": groups,
        "unique_cases": unique_cases,
        "insights": _insights(closed, feats),
    }
