import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import load_config, valid_mint
from .demo import run_demo
from .providers import PublicSources
from .scanner import scan
from .storage import Store


def display(report):
    synthetic = report["origin"] == "synthetic"
    print("DATOS SINTÉTICOS — DEMOSTRACIÓN" if synthetic else "DATOS PÚBLICOS — OBSERVACIÓN, SIN COMPRAS REALES")
    if report["observed_at"]:
        print("Consulta:", datetime.fromtimestamp(report["observed_at"], timezone.utc).isoformat())
    labels = {"early": "temprano", "developing": "en desarrollo", "established": "mercado antiguo", "unknown": "edad desconocida"}
    for row in report["results"]:
        price = f"${row['price_usd']:.8g}" if row["price_usd"] is not None else "sin precio"
        print(f"\n{row['symbol']} | {labels[row['cohort']]} | {row['status']} | prioridad de observación {row['market_attention_score']}/100 | {price}")
        print("  Mint:", row["mint"])
        for message in row.get("alerts", []) + row["blockers"]:
            print("  ALERTA:", message)
        print("  Revisión:", "; ".join(row["review_required"]))
        print("  X:", row["x"]["status"], "|", row["x"]["note"])
    for error in report["errors"]:
        print("FUENTE NO DISPONIBLE:", error["source"] if isinstance(error, dict) else "local",
              error["message"] if isinstance(error, dict) else error)
    print("\nNingún resultado certifica ausencia de rug pull. La puntuación no predice rentabilidad.")


def save_report(report, path):
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    temporary.replace(output)


def parser():
    result = argparse.ArgumentParser(description="PUMP: screener gratuito de investigación; no opera dinero real")
    result.add_argument("--config", help="JSON local; por defecto usa valores gratuitos integrados")
    result.add_argument("--db", help="SQLite local; live y demo deben usar archivos diferentes")
    result.add_argument("--synthetic", action="store_true", help="Leer/operar únicamente una base de demostración")
    commands = result.add_subparsers(dest="command", required=True)
    for name in ("scan", "demo", "report"):
        cmd = commands.add_parser(name)
        cmd.add_argument("--output", help="Guardar informe JSON con procedencia y errores")
    watch = commands.add_parser("watch")
    watch.add_argument("--interval", type=int, default=300, help="Segundos entre consultas; mínimo 60")
    watch.add_argument("--cycles", type=int, default=0, help="0: hasta Ctrl+C; otro entero: número de ciclos")
    watch.add_argument("--output", default=".local/report.json")
    buy = commands.add_parser("paper-open", help="Registrar compra FICTICIA manual, incluso de tokens de riesgo")
    buy.add_argument("mint")
    buy.add_argument("--usd", type=float, required=True)
    sell = commands.add_parser("paper-close", help="Registrar venta FICTICIA; no comprueba si una venta real funcionaría")
    sell.add_argument("position_id", type=int)
    commands.add_parser("positions")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    store = None
    try:
        config = load_config(args.config)
        synthetic = args.command == "demo" or args.synthetic
        if synthetic and args.command in ("scan", "watch"):
            raise ValueError("scan/watch requieren una base de datos real, sin --synthetic")
        db_path = args.db or (".local/demo.sqlite3" if synthetic else ".local/live.sqlite3")
        if args.command in ("report", "positions", "paper-open", "paper-close") and not Path(db_path).is_file():
            raise ValueError("Base no encontrada; ejecuta demo o scan primero")
        if args.command == "watch" and (args.interval < 60 or args.cycles < 0):
            raise ValueError("watch requiere intervalo >=60 segundos y cycles >=0")
        store = Store(db_path, "synthetic" if synthetic else "live")
        if args.command == "demo":
            report = run_demo(store, config, time.time())
        elif args.command in ("scan", "watch"):
            sources = PublicSources(config["rpc_url"], config["request_delay_seconds"])
            cycles = 0
            while True:
                report = scan(sources, store, config)
                display(report)
                if args.output:
                    save_report(report, args.output)
                if args.command == "scan":
                    return 2 if not report["results"] else (1 if report["errors"] else 0)
                cycles += 1
                if args.cycles and cycles >= args.cycles:
                    return 2 if not report["results"] else (1 if report["errors"] else 0)
                time.sleep(args.interval)
        elif args.command == "report":
            report = store.report()
        elif args.command == "positions":
            print(json.dumps({"origin": store.origin, "mode": "PAPER_ONLY", "positions": store.positions()}, indent=2, ensure_ascii=False))
            return 0
        elif args.command == "paper-open":
            if not valid_mint(args.mint):
                raise ValueError("Mint inválido")
            print(json.dumps(store.paper_open(args.mint, args.usd, config), indent=2, ensure_ascii=False))
            return 0
        else:
            print(json.dumps(store.paper_close(args.position_id, config), indent=2, ensure_ascii=False))
            return 0
        display(report)
        if args.output:
            save_report(report, args.output)
        return 0 if report["scan_id"] else 2
    except (ValueError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nVigilancia detenida; historial conservado.")
        return 130
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
