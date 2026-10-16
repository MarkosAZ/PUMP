import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from pump.__main__ import main
from pump.analysis import analyze, distribution, select_pair
from pump.config import DEFAULTS, load_config, valid_mint
from pump.demo import MINT_A, MINT_B, make_pair, run_demo
from pump.providers import PublicSources, SourceError, TOKEN_PROGRAM, TOKEN_2022_PROGRAM
from pump.scanner import changes, scan
from pump.storage import Store

NOW = 1800000000


def snapshot(**overrides):
    return {"status": "observed", "mint_authority": None, "freeze_authority": None,
            "supply": "10000000000000000000", "holders": [
                {"account": "a", "owner": "owner-1", "amount": "500000000000000000"},
                {"account": "b", "owner": "owner-1", "amount": "300000000000000000"},
                {"account": "c", "owner": "owner-2", "amount": "200000000000000000"},
            ], **overrides}


def observation(price=0.01, chain=None, age=4, liquidity=200000):
    return analyze(MINT_A, make_pair(MINT_A, NOW, age, price, liquidity),
                   snapshot() if chain is None else chain, load_config(), NOW)


class AnalysisTests(unittest.TestCase):
    def test_mint_validation_decodes_full_32_bytes(self):
        self.assertTrue(valid_mint(MINT_A))
        self.assertFalse(valid_mint("2" * 32))
        self.assertFalse(valid_mint("0" * 44))

    def test_quote_token_price_never_used(self):
        wrong = make_pair(MINT_B, NOW, 4, liquidity=9999999)
        wrong["quoteToken"] = {"address": MINT_A}
        correct = make_pair(MINT_A, NOW, 4)
        self.assertEqual(select_pair([wrong, correct], MINT_A), correct)
        self.assertIsNone(select_pair([wrong], MINT_A))

    def test_liquidity_selects_one_pool_not_sum(self):
        pools = [make_pair(MINT_A, NOW, 4, liquidity=n) for n in (1000, 2000)]
        self.assertEqual(select_pair(pools, MINT_A)["liquidity"]["usd"], 2000)

    def test_distribution_groups_token_accounts_by_owner(self):
        result = distribution(snapshot())
        self.assertEqual(result["sample_owners"], 2)
        self.assertAlmostEqual(result["largest_observed_owner_pct_lower_bound"], 8)
        self.assertAlmostEqual(result["sample_supply_pct"], 10)

    def test_invalid_distribution_is_unknown(self):
        for supply in ("0", "NaN", "1", "-5", "2.5"):
            with self.subTest(supply=supply):
                self.assertIsNone(distribution(snapshot(supply=supply)))
        duplicate = snapshot()
        duplicate["holders"].append(duplicate["holders"][0])
        self.assertIsNone(distribution(duplicate))

    def test_authorities_block_for_both_cohorts(self):
        for age in (1, 24 * 30):
            for authority in ("mint_authority", "freeze_authority"):
                with self.subTest(age=age, authority=authority):
                    row = observation(chain=snapshot(**{authority: "active"}), age=age)
                    self.assertEqual(row["status"], "DISCARD")
                    self.assertFalse(row["automatic_buy_eligible"])

    def test_unknowns_never_imply_safety(self):
        for chain in (snapshot(), {"status": "unavailable"}, {"status": "partial"}):
            row = observation(chain=chain)
            self.assertEqual(row["status"], "REVIEW")
            self.assertFalse(row["automatic_buy_eligible"])
            self.assertTrue(row["review_required"])

    def test_unsupported_token_program_blocked(self):
        row = observation(chain={"status": "unsupported", "reason": "Token-2022 sin analizar"})
        self.assertEqual(row["status"], "DISCARD")

    def test_cohort_is_pair_age_not_guarantee_of_consolidation(self):
        self.assertEqual(observation(age=1)["cohort"], "early")
        self.assertEqual(observation(age=100)["cohort"], "developing")
        self.assertEqual(observation(age=720)["cohort"], "established")
        self.assertEqual(observation(age=-1)["cohort"], "unknown")

    def test_missing_or_nonfinite_price_blocks(self):
        for price in ("NaN", "inf", 0, -1, None):
            with self.subTest(price=price):
                self.assertEqual(observation(price=price)["status"], "DISCARD")

    def test_established_requires_more_liquidity(self):
        self.assertEqual(observation(liquidity=20000)["status"], "REVIEW")
        self.assertEqual(observation(age=720, liquidity=20000)["status"], "DISCARD")

    def test_x_is_never_invented_or_scored(self):
        pair = make_pair(MINT_A, NOW, 4)
        config = load_config()
        original = analyze(MINT_A, pair, snapshot(), config, NOW)
        config["x_evidence"] = {MINT_A: ["https://x.com/test/status/123"]}
        manual = analyze(MINT_A, pair, snapshot(), config, NOW)
        self.assertEqual(original["x"]["status"], "unavailable")
        self.assertEqual(manual["x"]["status"], "manual_unverified")
        self.assertEqual(original["market_attention_score"], manual["market_attention_score"])

    def test_changes_detect_liquidity_drop_and_new_authority(self):
        before = observation()
        after = observation(liquidity=100000, chain=snapshot(freeze_authority="active"))
        alerts = changes(before, after)
        self.assertTrue(any("Liquidez cayó 50.0%" in msg for msg in alerts))
        self.assertIn("Nuevos motivos de descarte", alerts)


class ConfigTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            path.write_text(json.dumps(data))
            return load_config(path)

    def test_example_matches_defaults(self):
        example = Path(__file__).resolve().parents[1] / "config.example.json"
        self.assertEqual(load_config(example), DEFAULTS)

    def test_invalid_limits_and_credentials_rejected(self):
        for data in ({"paper_slippage_bps": 10000}, {"discovery_limit": 100},
                     {"request_delay_seconds": -1}, {"paper_max_position_usd": float("nan")},
                     {"rpc_url": "https://name:password@example.com"},
                     {"rpc_url": "http://example.com"}, {"rpc_url": None},
                     {"watchlist": ["fake"]}, {"paper_fee_usd_per_side": True},
                     {"x_evidence": {MINT_A: ["https://example.com/status/1"]}},
                     {"unknown_option": 1}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.load(data)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "db.sqlite3"
        self.store = Store(self.path, "synthetic")
        self.config = load_config()
        self.store.record_scan([observation()], [], NOW)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_costs_make_flat_price_roundtrip_negative(self):
        self.store.paper_open(MINT_A, 50, self.config, NOW)
        closed = self.store.paper_close(1, self.config, NOW)
        expected = (50 - 0.02) / 1.02 * 0.98 - 0.02 - 50
        self.assertAlmostEqual(closed["pnl_usd"], expected)
        self.assertLess(closed["pnl_usd"], 0)

    def test_original_costs_survive_config_change(self):
        self.store.paper_open(MINT_A, 50, self.config, NOW)
        changed = {**self.config, "paper_slippage_bps": 0, "paper_fee_usd_per_side": 0}
        self.assertLess(self.store.paper_close(1, changed, NOW)["pnl_usd"], -1)

    def test_stale_quote_blocks_entry_and_exit(self):
        with self.assertRaisesRegex(ValueError, "caducada"):
            self.store.paper_open(MINT_A, 50, self.config, NOW + 901)
        self.store.paper_open(MINT_A, 50, self.config, NOW)
        with self.assertRaisesRegex(ValueError, "caducada"):
            self.store.paper_close(1, self.config, NOW + 901)

    def test_latest_failed_scan_invalidates_old_quote(self):
        self.store.record_scan([], [{"source": "market", "message": "offline"}], NOW + 10)
        with self.assertRaisesRegex(ValueError, "última consulta"):
            self.store.paper_open(MINT_A, 50, self.config, NOW + 10)

    def test_nonfinite_and_oversized_entries_rejected(self):
        for amount in (float("nan"), float("inf"), -1, 101, 0):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                self.store.paper_open(MINT_A, amount, self.config, NOW)

    def test_total_exposure_cap(self):
        config = {**self.config, "paper_max_exposure_usd": 40}
        with self.assertRaisesRegex(ValueError, "exposición"):
            self.store.paper_open(MINT_A, 50, config, NOW)
        self.assertEqual(self.store.positions(), [])

    def test_duplicate_entry_and_exit_are_rejected(self):
        self.store.paper_open(MINT_A, 50, self.config, NOW)
        with self.assertRaisesRegex(ValueError, "Ya hay"):
            self.store.paper_open(MINT_A, 50, self.config, NOW)
        self.store.paper_close(1, self.config, NOW)
        with self.assertRaisesRegex(ValueError, "ya cerrada"):
            self.store.paper_close(1, self.config, NOW)

    def test_cannot_mix_demo_and_real_data(self):
        with self.assertRaisesRegex(ValueError, "No mezclar"):
            Store(self.path, "live")


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.source = PublicSources("https://api.mainnet-beta.solana.com", 0)

    def test_rpc_cannot_send_transactions(self):
        with self.assertRaises(ValueError):
            self.source.rpc("sendTransaction", ["nothing"])

    def test_rpc_error_does_not_leak_response_or_credentials(self):
        with patch.object(self.source, "request", return_value={"error": {"message": "SECRET"}}):
            result = self.source.chain_snapshot(MINT_A)
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("SECRET", str(result))

    def test_http_error_does_not_leak_url(self):
        error = urllib.error.HTTPError("https://host/SECRET", 429, "SECRET", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(SourceError, "HTTP 429") as caught:
                self.source.request("https://host/SECRET")
        self.assertNotIn("SECRET", str(caught.exception))

    def test_holder_failure_preserves_authorities(self):
        mint = {"value": {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": {
            "mintAuthority": "active", "freezeAuthority": None, "supply": "1000"}}}}}
        with patch.object(self.source, "rpc", side_effect=[mint, SourceError("limited")]):
            result = self.source.chain_snapshot(MINT_A)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["mint_authority"], "active")
        self.assertEqual(observation(chain=result)["status"], "DISCARD")

    def test_reads_holders_and_mint_with_parsed_schemas(self):
        mint = {"value": {"owner": TOKEN_PROGRAM, "data": {"parsed": {"type": "mint", "info": {
            "mintAuthority": None, "freezeAuthority": None, "supply": "1000"}}}}}
        largest = {"value": [{"address": "account-a", "amount": "99"}]}
        account = {"value": [{"owner": TOKEN_PROGRAM, "data": {"parsed": {"info": {
            "mint": MINT_A, "owner": "wallet-a", "tokenAmount": {"amount": "100"}}}}}]}
        with patch.object(self.source, "rpc", side_effect=[mint, largest, account]):
            result = self.source.chain_snapshot(MINT_A)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["holders"][0]["amount"], "100")
        self.assertEqual(distribution(result)["largest_observed_owner_pct_lower_bound"], 10)

    def test_token_2022_can_be_observed_but_extensions_are_not_cleared(self):
        mint = {"value": {"owner": TOKEN_2022_PROGRAM, "data": {"parsed": {"type": "mint", "info": {
            "mintAuthority": None, "freezeAuthority": None, "supply": "1000",
            "extensions": [{"extension": "transferHook", "state": {}}]}}}}}
        largest = {"value": [{"address": "account-a"}]}
        account = {"value": [{"owner": TOKEN_2022_PROGRAM, "data": {"parsed": {"info": {
            "mint": MINT_A, "owner": "wallet-a", "tokenAmount": {"amount": "100"}}}}}]}
        with patch.object(self.source, "rpc", side_effect=[mint, largest, account]):
            result = self.source.chain_snapshot(MINT_A)
        self.assertEqual(result["status"], "observed")
        self.assertEqual(result["extensions"], ["transferHook"])
        row = observation(chain=result)
        self.assertEqual(row["status"], "DISCARD")
        self.assertFalse(row["automatic_buy_eligible"])
        self.assertIsNotNone(row["distribution"])


class ScannerIntegrationTests(unittest.TestCase):
    def test_scan_report_paper_roundtrip_and_offline_failure(self):
        class FakeSources:
            price = 0.01
            offline = False

            def discover(self, limit):
                if self.offline:
                    raise SourceError("offline")
                return [MINT_A]

            def pairs(self, mint):
                if self.offline:
                    raise SourceError("offline")
                return [make_pair(mint, NOW, 5, self.price)]

            def chain_snapshot(self, mint):
                return snapshot()

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "test.sqlite3", "synthetic")
            try:
                source, config = FakeSources(), load_config()
                first = scan(source, store, config, NOW)
                self.assertEqual(len(first["results"]), 1)
                self.assertEqual(first["errors"], [])
                store.paper_open(MINT_A, 50, config, NOW)
                source.price = 0.02
                scan(source, store, config, NOW + 60)
                self.assertGreater(store.paper_close(1, config, NOW + 60)["pnl_usd"], 40)
                source.offline = True
                failed = scan(source, store, {**config, "watchlist": [MINT_A]}, NOW + 120)
                self.assertEqual(failed["results"], [])
                self.assertTrue(failed["errors"])
                with self.assertRaises(ValueError):
                    store.paper_open(MINT_A, 50, config, NOW + 120)
            finally:
                store.close()

    def test_partial_rpc_scan_is_explicitly_degraded(self):
        class Partial:
            def discover(self, limit): return [MINT_A]
            def pairs(self, mint): return [make_pair(mint, NOW, 4)]
            def chain_snapshot(self, mint): return {"status": "unavailable", "reason": "403"}
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "partial.sqlite3", "synthetic")
            try:
                result = scan(Partial(), store, load_config(), NOW)
                self.assertEqual(result["results"][0]["status"], "REVIEW")
                self.assertEqual(result["errors"][0]["source"], "solana")
                self.assertFalse(result["results"][0]["automatic_buy_eligible"])
            finally:
                store.close()

    def test_demo_is_repeatable_and_cli_separates_databases(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "demo.sqlite3")
            output = str(Path(folder) / "report.json")
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(["--db", path, "demo", "--output", output]), 0)
                self.assertEqual(main(["--db", path, "demo"]), 0)
                self.assertEqual(main(["--db", path, "--synthetic", "positions"]), 0)
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", path, "report"]), 2)
            report = json.loads(Path(output).read_text())
            self.assertEqual(report["origin"], "synthetic")
            self.assertEqual(len(report["results"]), 2)
            store = Store(path, "synthetic")
            try:
                self.assertEqual(len(store.positions()), 1)
                self.assertLess(store.positions()[0]["pnl_usd"], 0)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
