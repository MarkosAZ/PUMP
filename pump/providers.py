"""Read-only public sources. No wallet, signing code, or transaction RPCs."""
import json
import time
import urllib.error
import urllib.request

from .config import valid_mint

TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"
READ_METHODS = {"getAccountInfo", "getTokenLargestAccounts", "getMultipleAccounts"}


class SourceError(RuntimeError):
    pass


class PublicSources:
    def __init__(self, rpc_url, delay=1.0):
        self.rpc_url = rpc_url
        self.delay = delay
        self.last_request = 0.0

    def request(self, url, body=None):
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={
            "User-Agent": "PUMP-research/0.1", "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read(4_000_001)
                if len(raw) > 4_000_000:
                    raise SourceError("Respuesta demasiado grande")
                result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            raise SourceError(f"HTTP {exc.code}; revisar permisos o límites de la fuente") from None
        except urllib.error.URLError:
            raise SourceError("No se pudo conectar; revisar red, proxy y dominios permitidos") from None
        except (TimeoutError, OSError, ValueError):
            raise SourceError("Respuesta no disponible o JSON inválido") from None
        finally:
            self.last_request = time.monotonic()
        return result

    def discover(self, limit):
        if not limit:
            return []
        rows = self.request("https://api.dexscreener.com/token-profiles/latest/v1")
        if not isinstance(rows, list):
            raise SourceError("Formato inesperado en perfiles DexScreener")
        # Profiles are not a launch feed and may include promoted tokens.
        return list(dict.fromkeys(
            row.get("tokenAddress") for row in rows if isinstance(row, dict)
            and row.get("chainId") == "solana" and valid_mint(row.get("tokenAddress"))
        ))[:limit]

    def pairs(self, mint):
        if not valid_mint(mint):
            raise ValueError("Mint inválido")
        rows = self.request(f"https://api.dexscreener.com/tokens/v1/solana/{mint}")
        if not isinstance(rows, list):
            raise SourceError("Formato inesperado en mercados DexScreener")
        return rows

    def rpc(self, method, params):
        if method not in READ_METHODS:
            raise ValueError("Solo se permiten métodos RPC de lectura")
        response = self.request(self.rpc_url, {"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        if not isinstance(response, dict) or "error" in response or "result" not in response:
            raise SourceError(f"RPC {method} no disponible o limitado por el proveedor")
        return response["result"]

    def chain_snapshot(self, mint):
        try:
            account = self.rpc("getAccountInfo", [mint, {"encoding": "jsonParsed", "commitment": "confirmed"}])
            value = account.get("value") if isinstance(account, dict) else None
            if not value:
                raise SourceError("Mint no encontrado")
            program = value.get("owner")
            if program not in (TOKEN_PROGRAM, TOKEN_2022_PROGRAM):
                return {"status": "unsupported", "reason": "Programa de token no compatible"}
            parsed = value["data"]["parsed"]
            if parsed["type"] != "mint":
                raise SourceError("La dirección no es un mint SPL")
            info = parsed["info"]
            if not {"mintAuthority", "freezeAuthority", "supply"} <= info.keys():
                raise SourceError("Datos de permisos incompletos")
            result = {"status": "partial", "mint_authority": info["mintAuthority"],
                      "freeze_authority": info["freezeAuthority"], "supply": str(info["supply"]),
                      "program": program, "holders": []}
            if program == TOKEN_2022_PROGRAM:
                extensions = info.get("extensions")
                result["extensions"] = [str(e.get("extension", "unknown")) for e in extensions
                                         if isinstance(e, dict)] if isinstance(extensions, list) else None
            try:
                largest = self.rpc("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
                rows = largest["value"]
                if not isinstance(rows, list) or not rows or len(rows) > 20:
                    raise SourceError("Muestra de cuentas incompleta")
                accounts = self.rpc("getMultipleAccounts", [[r["address"] for r in rows],
                                    {"encoding": "jsonParsed", "commitment": "confirmed"}])["value"]
                if len(accounts) != len(rows):
                    raise SourceError("Muestra de propietarios incompleta")
                for row, token_account in zip(rows, accounts):
                    holder = token_account["data"]["parsed"]["info"]
                    if holder["mint"] != mint or token_account["owner"] != program:
                        raise SourceError("Cuenta de tenedor inconsistente")
                    # Amount and owner come from the same RPC snapshot; no float conversion.
                    result["holders"].append({"account": row["address"], "owner": holder["owner"],
                                              "amount": str(holder["tokenAmount"]["amount"])})
                result["status"] = "observed"
            except SourceError as exc:
                result["holders"] = []
                result["holder_error"] = f"Distribución no disponible: {exc}; permisos conservados"
            except (KeyError, TypeError, ValueError):
                result["holders"] = []
                result["holder_error"] = "Distribución no disponible: esquema de cuentas incompatible; permisos conservados"
            return result
        except SourceError as exc:
            return {"status": "unavailable", "reason": str(exc)}
        except (KeyError, TypeError, ValueError):
            return {"status": "unavailable", "reason": "Datos on-chain incompletos o incompatibles"}
