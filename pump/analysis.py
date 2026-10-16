import math
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from .providers import TOKEN_2022_PROGRAM


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def select_pair(pairs, mint):
    choices = [p for p in pairs if isinstance(p, dict) and p.get("chainId") == "solana"
               and (p.get("baseToken") or {}).get("address") == mint]
    # priceUsd is for baseToken, never use it as the quoteToken's price.
    return max(choices, key=lambda p: number((p.get("liquidity") or {}).get("usd")) or 0, default=None)


def distribution(snapshot):
    """Observed lower bound only: the largest 20 token accounts are not all holders."""
    rows = snapshot.get("holders", [])
    if not rows:
        return None
    try:
        supply = Decimal(snapshot["supply"])
        if not supply.is_finite() or supply <= 0 or supply != supply.to_integral_value():
            return None
        owners = defaultdict(int)
        seen = set()
        for row in rows:
            raw = Decimal(row["amount"])
            if not raw.is_finite() or raw < 0 or raw != raw.to_integral_value() or row["account"] in seen:
                return None
            seen.add(row["account"])
            owners[row["owner"]] += int(raw)
        amounts = sorted(owners.values(), reverse=True)
        if not amounts or sum(amounts) > supply:
            return None
        return {"sample_accounts": len(rows), "sample_owners": len(owners),
                "largest_observed_owner_pct_lower_bound": float(Decimal(amounts[0]) * 100 / supply),
                "sample_supply_pct": float(Decimal(sum(amounts)) * 100 / supply),
                "caveat": "Sin clasificar pools ni exchanges; propietarios relacionados desconocidos; muestra parcial"}
    except (InvalidOperation, KeyError, TypeError, ValueError):
        return None


def analyze(mint, pair, snapshot, config, now):
    blockers, reviews, reasons = [], [], []
    liquidity = number((pair.get("liquidity") or {}).get("usd"))
    price = number(pair.get("priceUsd"))
    created = number(pair.get("pairCreatedAt"))
    age_hours = (now - created / 1000) / 3600 if created and created / 1000 <= now else None
    if age_hours is None:
        cohort = "unknown"
        reviews.append("Edad del mercado desconocida")
    elif age_hours <= config["new_pair_max_hours"]:
        cohort = "early"
    elif age_hours >= config["established_pair_min_days"] * 24:
        cohort = "established"
    else:
        cohort = "developing"
    floor = config["new_min_liquidity_usd"] if cohort == "early" else config["established_min_liquidity_usd"]
    if price is None or price <= 0:
        blockers.append("Precio USD ausente o inválido")
    if liquidity is None or liquidity < floor:
        blockers.append(f"Liquidez desconocida o menor que ${floor:,.0f}")
    state = snapshot.get("status")
    if snapshot.get("program") == TOKEN_2022_PROGRAM:
        blockers.append("Token-2022: efectos de sus extensiones sin auditar; requiere revisión especializada")
    if state == "unsupported":
        blockers.append(snapshot.get("reason", "Programa de token sin analizar"))
    elif state not in ("observed", "partial"):
        reviews.append("Permisos y distribución on-chain no verificados")
    else:
        for field, label in (("mint_authority", "emisión"), ("freeze_authority", "congelación")):
            if field not in snapshot:
                reviews.append(f"Permiso de {label} desconocido")
            elif snapshot[field] is not None:
                blockers.append(f"Autoridad de {label} activa")
    holders = distribution(snapshot)
    if holders is None:
        reviews.append("Distribución de tenedores no disponible")
    elif holders["largest_observed_owner_pct_lower_bound"] > config["max_observed_owner_pct"]:
        reviews.append("Concentración elevada o pool sin clasificar; requiere identificar propietarios")
    # The free prototype cannot certify these checks. Unknown never means safe.
    reviews.extend(["Control y retirada de liquidez sin verificar", "Vínculos entre carteras sin verificar",
                    "Ruta de venta y restricciones de transferencia sin verificar"])
    volume = pair.get("volume") or {}
    h1, h24 = number(volume.get("h1")), number(volume.get("h24"))
    txns = (pair.get("txns") or {}).get("h1") or {}
    buys, sells = number(txns.get("buys")), number(txns.get("sells"))
    if None in (h1, h24, buys, sells):
        reviews.append("Actividad de mercado incompleta")
    acceleration = h1 / (h24 / 24) if h1 is not None and h24 and h24 > 0 else None
    # Explicit research heuristic, not a probability, return prediction or X score.
    score = 0.0
    if liquidity:
        score += min(30, max(0, math.log10(liquidity / 1000) * 10))
    if acceleration is not None:
        score += min(25, acceleration * 5)
        reasons.append(f"Volumen de última hora / media horaria 24h: {acceleration:.2f}x")
    if buys is not None and sells is not None and buys + sells > 0:
        score += min(25, math.log10(1 + buys + sells) * 8)
        reasons.append(f"Operaciones h1: {int(buys)} compras / {int(sells)} ventas; no son usuarios únicos")
    if liquidity and h1 is not None:
        score += min(20, h1 / liquidity * 20)
    x_links = list(dict.fromkeys(config["x_evidence"].get(mint, [])))
    return {
        "mint": mint, "symbol": str((pair.get("baseToken") or {}).get("symbol", "?"))[:32],
        "pair": pair.get("pairAddress"), "dex": pair.get("dexId"), "observed_at": now,
        "cohort": cohort, "pair_age_hours": age_hours, "price_usd": price, "liquidity_usd": liquidity,
        "volume_h1": h1, "volume_h24": h24, "market_attention_score": round(score, 1),
        "status": "DISCARD" if blockers else "REVIEW", "blockers": blockers, "review_required": reviews,
        "reasons": reasons, "distribution": holders, "chain_status": state,
        "token_program": snapshot.get("program"), "token_extensions": snapshot.get("extensions"),
        "x": {"status": "manual_unverified" if x_links else "unavailable", "links": x_links,
              "note": "Enlaces aportados por el usuario; no se consultó X ni se midió atención social"},
        "automatic_buy_eligible": False,
    }
