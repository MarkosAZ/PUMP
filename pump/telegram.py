"""Opt-in, outbound notifications through the official Telegram Bot API."""
import hashlib
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from urllib.parse import quote


class TelegramError(RuntimeError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramClient:
    def __init__(self, token, chat_id=None):
        if not isinstance(token, str) or not token.strip() or any(c.isspace() for c in token):
            raise TelegramError("Configura TELEGRAM_BOT_TOKEN como secreto del entorno")
        if chat_id is not None and not re.fullmatch(r"-?[1-9][0-9]*", str(chat_id)):
            raise TelegramError("TELEGRAM_CHAT_ID debe ser el identificador numérico del chat elegido")
        self._token = token
        self.chat_id = str(chat_id) if chat_id is not None else None
        self._opener = urllib.request.build_opener(NoRedirect())

    @classmethod
    def from_env(cls, require_chat=True):
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID") if require_chat else None
        if require_chat and not chat_id:
            raise TelegramError("Configura TELEGRAM_CHAT_ID con el chat que recibirá las alertas")
        return cls(token, chat_id)

    def call(self, method, payload):
        if method not in ("sendMessage", "getUpdates"):
            raise ValueError("Método de Telegram no permitido")
        # Credentials must never appear in logs, exceptions, reports or saved config.
        url = f"https://api.telegram.org/bot{quote(self._token, safe=':')}/{method}"
        request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "User-Agent": "PUMP-research/0.1",
        }, method="POST")
        try:
            with self._opener.open(request, timeout=15) as response:
                raw = response.read(1_000_001)
                if len(raw) > 1_000_000:
                    raise TelegramError("Respuesta de Telegram demasiado grande")
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry = 60
                try:
                    value = json.loads(exc.read(100000)).get("parameters", {}).get("retry_after")
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                        retry = value
                except (ValueError, OSError, AttributeError, http.client.HTTPException):
                    pass
                raise TelegramError("Telegram limitó los envíos; se respetará retry_after", retry) from None
            messages = {401: "Telegram rechazó el token; revisa el secreto configurado",
                        403: "Telegram rechazó el acceso al chat; abre el bot y pulsa Iniciar",
                        409: "Telegram detectó un webhook u otro consumidor de actualizaciones"}
            raise TelegramError(messages.get(exc.code, f"Telegram respondió HTTP {exc.code}")) from None
        except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
            raise TelegramError("Entrega a Telegram no confirmada; revisa conectividad y permisos de red") from None
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise TelegramError("Telegram no confirmó la operación; revisa configuración y acceso al chat")
        return result.get("result")

    def send(self, text):
        if not self.chat_id:
            raise TelegramError("Falta el chat destinatario")
        result = self.call("sendMessage", {"chat_id": self.chat_id, "text": text,
                           "link_preview_options": {"is_disabled": True}})
        if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
            raise TelegramError("Telegram no devolvió confirmación de entrega")

    def private_chat_ids(self):
        # No offset: do not acknowledge updates or change webhook configuration.
        rows = self.call("getUpdates", {"timeout": 0, "limit": 100, "allowed_updates": ["message"]})
        if not isinstance(rows, list):
            raise TelegramError("Telegram devolvió actualizaciones incompatibles")
        ids = set()
        for row in rows:
            message = row.get("message", {}) if isinstance(row, dict) else {}
            chat = message.get("chat", {})
            if chat.get("type") == "private" and isinstance(chat.get("id"), int) and chat["id"] > 0:
                ids.add(chat["id"])
        return sorted(ids)


def clean(value, limit=180):
    return " ".join("".join(c if c.isprintable() else " " for c in str(value)).split())[:limit]


def format_report(report):
    synthetic = report["origin"] == "synthetic"
    lines = ["PUMP | DEMOSTRACIÓN: DATOS INVENTADOS" if synthetic else "PUMP | Observación de mercado"]
    stamp = report.get("observed_at")
    if stamp is not None:
        local = datetime.fromtimestamp(stamp, timezone(timedelta(hours=-5)))
        lines.append("Consulta: " + local.strftime("%Y-%m-%d %H:%M UTC-05"))
    rows = report["results"]
    discarded = sum(row["status"] == "DISCARD" for row in rows)
    lines.append(f"Tokens: {len(rows)} | Descartados: {discarded} | Pendientes de revisión: {len(rows) - discarded}")
    lines.append("Hasta 5 tokens: primero alertas de riesgo, después prioridad de observación. No es una señal de compra.")
    cohorts = {"early": "temprano", "developing": "en desarrollo", "established": "mercado antiguo", "unknown": "edad desconocida"}
    selected = sorted(rows, key=lambda row: not bool(row.get("alerts")))[:5]
    for row in selected:
        status = "DESCARTADO" if row["status"] == "DISCARD" else "REVISIÓN PENDIENTE"
        lines.extend(["", f"{clean(row['symbol'], 32)} | {cohorts[row['cohort']]} | {status}",
                      "Mint: " + clean(row["mint"], 44),
                      f"Prioridad de observación: {row['market_attention_score']}/100"])
        if row.get("liquidity_usd") is not None:
            lines.append(f"Liquidez observada: USD {row['liquidity_usd']:,.0f}")
        for alert in row.get("alerts", [])[:2]:
            lines.append("Alerta: " + clean(alert))
        for reason in row["blockers"][:2]:
            lines.append("Descarte: " + clean(reason))
        if row.get("distribution") is None:
            lines.append("Distribución: no verificada")
        else:
            lines.append("Distribución: muestra parcial; pools y carteras relacionadas sin clasificar")
    errors = report.get("errors", [])
    if errors:
        lines.extend(["", f"Consultas de fuente con errores: {len(errors)}; datos incompletos."])
    # Telegram counts UTF-16 units. Reserve room for the limitations after truncation.
    body = "\n".join(lines).encode("utf-16-le", errors="replace")
    if len(body) > 6400:
        body = body[:6300]
        lines_text = body.decode("utf-16-le", errors="ignore") + "\n[Resumen recortado]"
    else:
        lines_text = body.decode("utf-16-le")
    return lines_text + "\n\nX automático no disponible. Riesgos pendientes: revisar informe local. Sin garantía contra rug pulls. Sin compras reales."


class ReportNotifier:
    def __init__(self, client, store, summary_seconds=3600):
        self.client = client
        self.store = store
        self.summary_seconds = summary_seconds
        self.key = "telegram:last:" + hashlib.sha256(client.chat_id.encode()).hexdigest()
        self.retry_at = 0

    def notify(self, report, now=None):
        now = time.time() if now is None else now
        if report.get("scan_id") is None or now < self.retry_at:
            return False
        raw = self.store.get_metadata(self.key)
        previous = json.loads(raw) if raw else {}
        if previous.get("scan_id") == report["scan_id"]:
            return False
        # Collapse repeated per-mint RPC failures so they do not cause a notification storm.
        source_state = sorted(set((e.get("source", "local"), e.get("message", ""))
                                  if isinstance(e, dict) else ("local", str(e)) for e in report["errors"]))
        fingerprint = hashlib.sha256(json.dumps(source_state).encode()).hexdigest()
        alerts = any(row.get("alerts") for row in report["results"])
        due = now - previous.get("sent_at", 0) >= self.summary_seconds
        if previous and not (alerts or due or fingerprint != previous.get("source_state")):
            return False
        try:
            self.client.send(format_report(report))
        except TelegramError as exc:
            self.retry_at = now + max(60, exc.retry_after or 300)
            raise
        self.store.set_metadata(self.key, json.dumps({"scan_id": report["scan_id"], "sent_at": now,
                                                       "source_state": fingerprint}))
        return True
