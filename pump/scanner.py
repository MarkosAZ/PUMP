import time

from .analysis import analyze, select_pair
from .providers import SourceError


def changes(previous, current):
    alerts = []
    if not previous:
        return alerts
    before, after = previous.get("liquidity_usd"), current.get("liquidity_usd")
    if before and after is not None and after <= before * 0.7:
        alerts.append(f"Liquidez cayó {(1 - after / before) * 100:.1f}% desde la observación anterior")
    old, new = previous.get("distribution"), current.get("distribution")
    if old and new:
        delta = new["largest_observed_owner_pct_lower_bound"] - old["largest_observed_owner_pct_lower_bound"]
        if delta >= 5:
            alerts.append(f"Mayor propietario observado subió {delta:.1f} puntos porcentuales; revisar clasificación")
    if set(current["blockers"]) - set(previous["blockers"]):
        alerts.append("Nuevos motivos de descarte")
    return alerts


def scan(sources, store, config, now=None):
    errors, results = [], []
    try:
        discovered = sources.discover(config["discovery_limit"])
    except SourceError as exc:
        discovered = []
        errors.append({"source": "discovery", "message": str(exc)})
    mints = list(dict.fromkeys(config["watchlist"] + store.open_mints() + discovered))
    for mint in mints:
        try:
            pair = select_pair(sources.pairs(mint), mint)
            if pair is None:
                raise SourceError("No hay mercado con este mint como token base; puede faltar cobertura pregraduación")
            snapshot = sources.chain_snapshot(mint)
            observed_at = now if now is not None else time.time()
            result = analyze(mint, pair, snapshot, config, observed_at)
            result["alerts"] = changes(store.previous(mint), result)
            results.append(result)
            if snapshot.get("status") in ("unavailable", "partial"):
                errors.append({"source": "solana", "mint": mint,
                               "message": snapshot.get("reason") or snapshot.get("holder_error") or "Datos incompletos"})
        except SourceError as exc:
            errors.append({"source": "market", "mint": mint, "message": str(exc)})
        except (KeyError, TypeError, ValueError, AttributeError):
            errors.append({"source": "market", "mint": mint,
                           "message": "Datos del mercado incompletos o incompatibles; no se genera señal"})
    if not mints:
        errors.append({"source": "coverage", "message": "Sin tokens: agrega mints a watchlist o comprueba descubrimiento"})
    finished_at = now if now is not None else time.time()
    store.record_scan(results, errors, finished_at)
    return store.report()
