import json
import math
import sqlite3
import time
from pathlib import Path


class Store:
    def __init__(self, path, origin):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY, observed_at REAL NOT NULL, errors TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (
                scan_id INTEGER NOT NULL, mint TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(scan_id, mint));
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY, mint TEXT NOT NULL, symbol TEXT NOT NULL,
                opened_at REAL NOT NULL, closed_at REAL, cost_usd REAL NOT NULL,
                quantity REAL NOT NULL, entry_price REAL NOT NULL, slippage_bps REAL NOT NULL,
                fee_usd REAL NOT NULL, entry_scan_id INTEGER NOT NULL,
                exit_scan_id INTEGER, proceeds_usd REAL, pnl_usd REAL);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_position_per_mint
                ON positions(mint) WHERE closed_at IS NULL;
        """)
        current = self.db.execute("SELECT value FROM metadata WHERE key='origin'").fetchone()
        if current and current[0] != origin:
            self.db.close()
            raise ValueError("No mezclar datos sintéticos y reales: usa bases de datos diferentes")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('origin', ?)", (origin,))
        self.origin = origin

    def close(self):
        self.db.close()

    def record_scan(self, results, errors, now):
        with self.db:
            scan_id = self.db.execute("INSERT INTO scans(observed_at, errors) VALUES (?, ?)",
                                      (now, json.dumps(errors, ensure_ascii=False))).lastrowid
            self.db.executemany("INSERT INTO observations VALUES (?, ?, ?)", [
                (scan_id, result["mint"], json.dumps(result, ensure_ascii=False, allow_nan=False)) for result in results
            ])
        return scan_id

    def report(self):
        scan = self.db.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        if not scan:
            return {"origin": self.origin, "scan_id": None, "observed_at": None, "results": [], "errors": ["Sin consultas guardadas"]}
        rows = self.db.execute("SELECT data FROM observations WHERE scan_id=?", (scan["id"],)).fetchall()
        results = sorted((json.loads(row[0]) for row in rows),
                         key=lambda row: (row["status"] == "DISCARD", -row["market_attention_score"]))
        return {"origin": self.origin, "scan_id": scan["id"], "observed_at": scan["observed_at"],
                "results": results, "errors": json.loads(scan["errors"])}

    def previous(self, mint):
        row = self.db.execute("SELECT data FROM observations WHERE mint=? ORDER BY scan_id DESC LIMIT 1", (mint,)).fetchone()
        return json.loads(row[0]) if row else None

    def open_mints(self):
        return [row[0] for row in self.db.execute("SELECT mint FROM positions WHERE closed_at IS NULL")]

    def quote(self, mint, config, now):
        report = self.report()
        observation = next((r for r in report["results"] if r["mint"] == mint), None)
        if not observation:
            raise ValueError("El token no tiene cotización en la última consulta; ejecuta scan")
        age = now - observation["observed_at"]
        if age < 0 or age > config["quote_max_age_seconds"]:
            raise ValueError("Cotización caducada; ejecuta scan antes de simular")
        price = observation.get("price_usd")
        if price is None or not math.isfinite(price) or price <= 0:
            raise ValueError("Precio no válido para una simulación")
        return observation, report["scan_id"]

    def paper_open(self, mint, usd, config, now=None):
        now = time.time() if now is None else now
        if not math.isfinite(usd) or usd <= config["paper_fee_usd_per_side"] or usd > config["paper_max_position_usd"]:
            raise ValueError("Importe inválido: debe superar la comisión y respetar el límite por posición")
        with self.db:
            # Serialize quote/exposure checks with concurrent scans or another paper order.
            self.db.execute("BEGIN IMMEDIATE")
            observation, scan_id = self.quote(mint, config, now)
            liquidity = observation.get("liquidity_usd")
            if not liquidity or usd > liquidity * 0.01:
                raise ValueError("Importe mayor al 1% de la liquidez observada o liquidez desconocida")
            exposure = self.db.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM positions WHERE closed_at IS NULL").fetchone()[0]
            if exposure + usd > config["paper_max_exposure_usd"]:
                raise ValueError("Se supera el límite de exposición simulada")
            fill = observation["price_usd"] * (1 + config["paper_slippage_bps"] / 10000)
            quantity = (usd - config["paper_fee_usd_per_side"]) / fill
            try:
                position_id = self.db.execute("""INSERT INTO positions
                    (mint, symbol, opened_at, cost_usd, quantity, entry_price, slippage_bps, fee_usd, entry_scan_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (mint, observation["symbol"], now, usd, quantity, fill, config["paper_slippage_bps"],
                     config["paper_fee_usd_per_side"], scan_id)).lastrowid
            except sqlite3.IntegrityError:
                raise ValueError("Ya hay una posición simulada abierta para ese token") from None
        return {"position_id": position_id, "mode": "PAPER_ONLY", "cost_usd": usd,
                "warnings": observation["blockers"] + observation["review_required"],
                "note": "Compra ficticia solicitada manualmente; no demuestra que una operación real sea ejecutable"}

    def paper_close(self, position_id, config, now=None):
        now = time.time() if now is None else now
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            position = self.db.execute("SELECT * FROM positions WHERE id=? AND closed_at IS NULL", (position_id,)).fetchone()
            if not position:
                raise ValueError("Posición inexistente o ya cerrada")
            observation, scan_id = self.quote(position["mint"], config, now)
            # Each side pays the position's original assumptions, even after config edits.
            proceeds = max(0, position["quantity"] * observation["price_usd"]
                           * (1 - position["slippage_bps"] / 10000) - position["fee_usd"])
            pnl = proceeds - position["cost_usd"]
            self.db.execute("UPDATE positions SET closed_at=?, exit_scan_id=?, proceeds_usd=?, pnl_usd=? WHERE id=? AND closed_at IS NULL",
                            (now, scan_id, proceeds, pnl, position_id))
        return {"position_id": position_id, "mode": "PAPER_ONLY", "proceeds_usd": proceeds, "pnl_usd": pnl,
                "note": "Venta ficticia con deslizamiento fijo; no verifica ruta, impacto ni capacidad real de venta"}

    def positions(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM positions ORDER BY id")]
