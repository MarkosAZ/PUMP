import json
import math
import re
from pathlib import Path
from urllib.parse import urlparse

DEFAULTS = {
    "watchlist": [], "discovery_limit": 10,
    "rpc_url": "https://api.mainnet-beta.solana.com",
    "request_delay_seconds": 1.0, "new_pair_max_hours": 72,
    "established_pair_min_days": 7, "new_min_liquidity_usd": 10000,
    "established_min_liquidity_usd": 100000, "max_observed_owner_pct": 10,
    "quote_max_age_seconds": 900, "paper_slippage_bps": 200,
    "paper_fee_usd_per_side": 0.02, "paper_max_position_usd": 100,
    "paper_max_exposure_usd": 500, "x_evidence": {},
}
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def valid_mint(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 44:
        return False
    try:
        number = 0
        for char in value:
            number = number * 58 + BASE58.index(char)
        leading = len(value) - len(value.lstrip("1"))
        return leading + (number.bit_length() + 7) // 8 == 32
    except ValueError:
        return False


def load_config(path=None):
    config = DEFAULTS.copy()
    if path:
        supplied = json.loads(Path(path).read_text())
        if not isinstance(supplied, dict):
            raise ValueError("La configuración debe ser un objeto JSON")
        unknown = set(supplied) - set(DEFAULTS)
        if unknown:
            raise ValueError("Opciones desconocidas: " + ", ".join(sorted(unknown)))
        config.update(supplied)
    if not isinstance(config["watchlist"], list) or not all(valid_mint(m) for m in config["watchlist"]):
        raise ValueError("watchlist debe contener direcciones mint válidas de Solana")
    if len(config["watchlist"]) > 30:
        raise ValueError("Máximo 30 direcciones en la lista gratuita")
    for key, default in DEFAULTS.items():
        if isinstance(default, (float, int)):
            value = config[key]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{key} debe ser un número finito no negativo")
    if not isinstance(config["discovery_limit"], int) or config["discovery_limit"] > 30:
        raise ValueError("discovery_limit debe ser entero entre 0 y 30")
    if config["paper_slippage_bps"] >= 10000:
        raise ValueError("paper_slippage_bps debe ser menor que 10000")
    if config["max_observed_owner_pct"] > 100:
        raise ValueError("max_observed_owner_pct debe estar entre 0 y 100")
    if config["quote_max_age_seconds"] <= 0 or config["paper_max_position_usd"] <= 0 or config["paper_max_exposure_usd"] <= 0:
        raise ValueError("Los límites de cotización y posición deben ser positivos")
    if config["established_pair_min_days"] * 24 <= config["new_pair_max_hours"]:
        raise ValueError("La edad del grupo establecido debe superar la del grupo temprano")
    if not isinstance(config["rpc_url"], str):
        raise ValueError("rpc_url debe ser una URL HTTPS")
    url = urlparse(config["rpc_url"])
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("rpc_url requiere HTTPS y no debe contener credenciales, consulta ni fragmento")
    evidence = config["x_evidence"]
    if not isinstance(evidence, dict):
        raise ValueError("x_evidence debe ser un objeto por mint")
    for mint, links in evidence.items():
        if not valid_mint(mint) or not isinstance(links, list) or not all(
            isinstance(link, str) and re.fullmatch(r"https://(?:x\.com|twitter\.com)/[A-Za-z0-9_]+/status/[0-9]+", link)
            for link in links
        ):
            raise ValueError("x_evidence requiere mints válidos y enlaces públicos /usuario/status/id sin parámetros")
    return config
