import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from pump.__main__ import main
from pump.analysis import analyze
from pump.config import load_config
from pump.demo import MINT_A, make_pair, run_demo
from pump.storage import Store
from pump.telegram import NoRedirect, ReportNotifier, TelegramClient, TelegramError, format_report

NOW = 1800000000
FAKE_TOKEN = "123456:TEST_ONLY_NOT_A_REAL_TELEGRAM_CREDENTIAL"


def make_report():
    row = analyze(MINT_A, make_pair(MINT_A, NOW, 4), {"status": "unavailable"}, load_config(), NOW)
    row["alerts"] = []
    return {"origin": "live", "scan_id": 1, "observed_at": NOW, "results": [row],
            "errors": [{"source": "solana", "message": "HTTP 429"}]}


class TelegramClientTests(unittest.TestCase):
    def test_missing_credentials_fail_locally(self):
        with patch.dict(os.environ, {}, clear=True), patch("urllib.request.OpenerDirector.open") as request:
            with self.assertRaises(TelegramError):
                TelegramClient.from_env()
        request.assert_not_called()

    def test_request_is_post_json_with_one_explicit_recipient(self):
        client = TelegramClient(FAKE_TOKEN, "123")
        with patch.object(client._opener, "open", return_value=io.BytesIO(
            b'{"ok":true,"result":{"message_id":42}}'
        )) as request:
            client.send("Resumen de prueba")
        req = request.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertTrue(req.full_url.startswith("https://api.telegram.org/bot"))
        payload = json.loads(req.data)
        self.assertEqual(payload["chat_id"], "123")
        self.assertEqual(payload["text"], "Resumen de prueba")
        self.assertNotIn("parse_mode", payload)
        self.assertTrue(payload["link_preview_options"]["is_disabled"])

    def test_http_errors_never_reveal_token_or_server_response(self):
        for code in (301, 401, 403, 409, 500):
            client = TelegramClient(FAKE_TOKEN, "123")
            exc = urllib.error.HTTPError("https://api.telegram.org/bot" + FAKE_TOKEN, code,
                                         FAKE_TOKEN, {}, io.BytesIO(FAKE_TOKEN.encode()))
            with self.subTest(code=code), patch.object(client._opener, "open", side_effect=exc):
                with self.assertRaises(TelegramError) as caught:
                    client.send("test")
                self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_network_failure_redacts_secret(self):
        client = TelegramClient(FAKE_TOKEN, "123")
        with patch.object(client._opener, "open", side_effect=urllib.error.URLError(FAKE_TOKEN)):
            with self.assertRaises(TelegramError) as caught:
                client.send("test")
        self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_retry_after_is_preserved(self):
        client = TelegramClient(FAKE_TOKEN, "123")
        exc = urllib.error.HTTPError("https://api.telegram.org/bot" + FAKE_TOKEN, 429, "rate limit", {},
                                     io.BytesIO(b'{"parameters":{"retry_after":7200}}'))
        with patch.object(client._opener, "open", side_effect=exc):
            with self.assertRaises(TelegramError) as caught:
                client.send("test")
        self.assertEqual(caught.exception.retry_after, 7200)

    def test_unconfirmed_delivery_is_failure(self):
        for response in ({"ok": False, "description": FAKE_TOKEN}, {"ok": True, "result": {}}):
            client = TelegramClient(FAKE_TOKEN, "123")
            with self.subTest(response=response), patch.object(client._opener, "open", return_value=io.BytesIO(json.dumps(response).encode())):
                with self.assertRaises(TelegramError) as caught:
                    client.send("test")
                self.assertNotIn(FAKE_TOKEN, str(caught.exception))

    def test_chat_lookup_lists_only_private_ids_and_does_not_acknowledge(self):
        client = TelegramClient(FAKE_TOKEN)
        updates = [{"message": {"chat": {"id": 123, "type": "private"}, "text": "private text"}},
                   {"message": {"chat": {"id": -99, "type": "group"}}},
                   {"message": {"chat": {"id": 123, "type": "private"}}}]
        with patch.object(client, "call", return_value=updates) as request:
            self.assertEqual(client.private_chat_ids(), [123])
        self.assertNotIn("offset", request.call_args.args[1])

    def test_redirects_are_refused(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))


class FormattingTests(unittest.TestCase):
    def test_unknown_risk_is_explicit_and_not_a_buy_signal(self):
        text = format_report(make_report())
        self.assertIn("Distribución: no verificada", text)
        self.assertIn("REVISIÓN PENDIENTE", text)
        self.assertIn("X automático no disponible", text)
        self.assertIn("Sin compras reales", text)
        self.assertIn("UTC-05", text)

    def test_synthetic_content_is_labeled(self):
        report = make_report()
        report["origin"] = "synthetic"
        self.assertIn("DATOS INVENTADOS", format_report(report))

    def test_risk_alert_outside_top_five_is_included(self):
        report = make_report()
        rows = []
        for index in range(6):
            row = json.loads(json.dumps(report["results"][0]))
            row["symbol"] = f"TOKEN-{index}"
            row["alerts"] = ["Liquidez cayó 80%"] if index == 5 else []
            rows.append(row)
        report["results"] = rows
        text = format_report(report)
        self.assertIn("TOKEN-5", text)
        self.assertIn("Liquidez cayó 80%", text)
        self.assertNotIn("TOKEN-4", text)

    def test_utf16_limit_preserves_risk_footer(self):
        report = make_report()
        row = report["results"][0]
        row["symbol"] = "x\nFALSA COMPRA\x1b[0m"
        row["alerts"] = ["🧪" * 300, "🧪" * 300]
        row["blockers"] = ["🧪" * 300, "🧪" * 300]
        report["results"] = [row] * 10
        text = format_report(report)
        self.assertLessEqual(len(text.encode("utf-16-le")) // 2, 4096)
        self.assertNotIn("\x1b", text)
        self.assertIn("Sin garantía contra rug pulls", text)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "test.sqlite3", "synthetic")
        self.client = Mock(chat_id="123")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_restart_does_not_duplicate_sent_scan(self):
        report = make_report()
        self.assertTrue(ReportNotifier(self.client, self.store).notify(report, NOW))
        self.assertFalse(ReportNotifier(self.client, self.store).notify(report, NOW + 60))
        self.assertEqual(self.client.send.call_count, 1)

    def test_quiet_scans_wait_for_hourly_summary(self):
        report = make_report()
        notifier = ReportNotifier(self.client, self.store)
        self.assertTrue(notifier.notify(report, NOW))
        report["scan_id"] = 2
        self.assertFalse(notifier.notify(report, NOW + 300))
        self.assertTrue(notifier.notify(report, NOW + 3600))
        self.assertEqual(self.client.send.call_count, 2)

    def test_risk_alert_does_not_wait_for_hourly_summary(self):
        report = make_report()
        notifier = ReportNotifier(self.client, self.store)
        notifier.notify(report, NOW)
        report["scan_id"] = 2
        report["results"][0]["alerts"] = ["Liquidez cayó 50%"]
        self.assertTrue(notifier.notify(report, NOW + 300))

    def test_source_state_change_triggers_notification(self):
        report = make_report()
        notifier = ReportNotifier(self.client, self.store)
        notifier.notify(report, NOW)
        report["scan_id"] = 2
        report["errors"] = []
        self.assertTrue(notifier.notify(report, NOW + 300))

    def test_failure_does_not_mark_delivered_and_respects_backoff(self):
        notifier = ReportNotifier(self.client, self.store)
        self.client.send.side_effect = TelegramError("rate limit", retry_after=7200)
        report = make_report()
        with self.assertRaises(TelegramError):
            notifier.notify(report, NOW)
        self.assertIsNone(self.store.get_metadata(notifier.key))
        self.assertFalse(notifier.notify(report, NOW + 3600))
        self.client.send.side_effect = None
        self.assertTrue(notifier.notify(report, NOW + 7200))


class TelegramCLITests(unittest.TestCase):
    def test_preview_needs_neither_token_nor_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder) / "demo.sqlite3")
            store = Store(path, "synthetic")
            run_demo(store, load_config(), NOW)
            store.close()
            output = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), patch("urllib.request.OpenerDirector.open") as request, contextlib.redirect_stdout(output):
                self.assertEqual(main(["--db", path, "--synthetic", "telegram-preview"]), 0)
            request.assert_not_called()
            self.assertIn("DATOS INVENTADOS", output.getvalue())

    def test_telegram_is_opt_in_and_missing_token_does_not_start_scanner(self):
        with patch.dict(os.environ, {}, clear=True), patch("pump.__main__.scan") as scanner, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["scan", "--telegram"]), 2)
        scanner.assert_not_called()

    def test_send_failure_preserves_scan_and_returns_degraded_status(self):
        report = make_report()
        report["errors"] = []

        def fake_scan(sources, store, config):
            store.record_scan(report["results"], [], NOW)
            return store.report()

        with tempfile.TemporaryDirectory() as folder:
            db_path = str(Path(folder) / "live.sqlite3")
            output = str(Path(folder) / "report.json")
            with patch("pump.__main__.TelegramClient.from_env", return_value=Mock(chat_id="123")), \
                 patch("pump.__main__.scan", side_effect=fake_scan), \
                 patch("pump.__main__.ReportNotifier.notify", side_effect=TelegramError("offline")), \
                 contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(["--db", db_path, "scan", "--telegram", "--output", output]), 1)
            self.assertEqual(len(json.loads(Path(output).read_text())["results"]), 1)
            store = Store(db_path, "live")
            try:
                self.assertEqual(len(store.report()["results"]), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
