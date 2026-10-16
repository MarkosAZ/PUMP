"""Deterministic fabricated fixtures; never fetched prices or real recommendations."""
from .analysis import analyze
from .scanner import changes

MINT_A = "11111111111111111111111111111112"
MINT_B = "11111111111111111111111111111113"


def make_pair(mint, now, age_hours, price=0.01, liquidity=200000):
    return {"chainId": "solana", "baseToken": {"address": mint, "symbol": "DEMO"},
            "pairAddress": "synthetic-pair-" + mint[-1], "dexId": "synthetic",
            "pairCreatedAt": (now - age_hours * 3600) * 1000, "priceUsd": str(price),
            "liquidity": {"usd": liquidity}, "volume": {"h1": 25000, "h24": 200000},
            "txns": {"h1": {"buys": 120, "sells": 80}}}


def run_demo(store, config, now):
    if store.report()["scan_id"] is not None:
        return store.report()  # Re-running does not duplicate fabricated performance.
    snapshot = {"status": "observed", "mint_authority": None, "freeze_authority": None,
                "supply": "1000000", "holders": [
                    {"account": "account-1", "owner": "owner-a", "amount": "80000"},
                    {"account": "account-2", "owner": "owner-b", "amount": "50000"}]}
    first = analyze(MINT_A, make_pair(MINT_A, now - 60, 4), snapshot, config, now - 60)
    first["alerts"] = []
    store.record_scan([first], [], now - 60)
    store.paper_open(MINT_A, min(50, config["paper_max_position_usd"]), config, now - 60)
    second = analyze(MINT_A, make_pair(MINT_A, now, 4, price=0.008, liquidity=120000), snapshot, config, now)
    second["alerts"] = changes(first, second)
    risky_snapshot = {**snapshot, "freeze_authority": "synthetic-authority"}
    risky = analyze(MINT_B, make_pair(MINT_B, now, 24 * 30), risky_snapshot, config, now)
    risky["alerts"] = []
    store.record_scan([second, risky], [], now)
    store.paper_close(1, config, now)
    return store.report()
