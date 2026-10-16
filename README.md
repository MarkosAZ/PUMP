# PUMP — screener de investigación, presupuesto cero

Prototipo local para observar memecoins de Solana y registrar operaciones **ficticias**. Python 3.11 o superior, biblioteca estándar y SQLite: sin suscripciones, paquetes externos, cartera ni claves privadas. No firma ni envía transacciones.

## Probar ahora, sin Internet

Desde el checkout existente; no hace falta crear un worktree ni instalar dependencias:

```bash
cd /workspace/PUMP
python -m pump demo --output .local/demo-report.json
python -m pump --synthetic positions
python -m unittest discover -s tests -v
```

La demostración usa dos tokens inventados, una caída de liquidez, una autoridad de congelación activa y una operación simulada con pérdida. **No muestra precios reales ni resultados de una estrategia.** Repetir `demo` conserva la demostración existente. Para empezar otra, indica una ruta nueva mediante `--db` antes del subcomando.

## Consultar datos públicos

```bash
cd /workspace/PUMP
cp -n config.example.json config.json
# Edita watchlist en config.json con las direcciones mint que quieras seguir.
python -m pump --config config.json scan --output .local/report.json
python -m pump --config config.json watch --interval 300
```

`watch` imprime los resultados y alertas en la terminal y actualiza `.local/report.json`. Detén el proceso con Ctrl+C. Un ordenador encendido puede ejecutarlo sin contratar alojamiento; este proyecto no garantiza un servidor gratuito activo las 24 horas. No hay envío a Telegram en esta versión.

Opciones globales `--config`, `--db` y `--synthetic` van **antes** del comando. Ayuda: `python -m pump --help`.

Fuentes consultadas:

| Fuente | Uso y límites |
| --- | --- |
| `api.dexscreener.com/token-profiles/latest/v1` | Descubrimiento limitado a perfiles recientes de Solana. Un perfil nuevo no es un token recién lanzado; puede existir sesgo promocional. |
| `api.dexscreener.com/tokens/v1/solana/{mint}` | Precio USD, liquidez y actividad del mercado con mayor liquidez donde el mint es el token base. No se suman pools ni se confunde el precio del token de cotización. |
| `api.mainnet-beta.solana.com` | RPC público de lectura para mint, permisos y hasta 20 cuentas de tokens con sus propietarios. Puede limitar o rechazar métodos; sin garantía de disponibilidad. |
| X | Sin acceso automatizado en esta versión. Solo admite enlaces manuales, sin descargarlos, verificarlos o convertirlos en una métrica social. |

Las peticiones tienen tiempo límite, verificación TLS y separación entre solicitudes. El intervalo del bucle se cuenta después de terminar una consulta, no es una garantía de detección en segundos. A más tokens, mayor latencia. Ajusta la lista y el intervalo si el proveedor devuelve límites.

**Validación realizada en este entorno:** después de un bloqueo inicial del proxy, una consulta en vivo obtuvo diez perfiles/mercados y pudo leer sus cuentas mint en Solana. Otra consulta validó la clasificación de un mercado temprano Token-2022 y uno antiguo SPL. El RPC público devolvió `429` al pedir las mayores cuentas, así que la distribución real sigue sin validar y esas consultas se etiquetan como parciales (código `1`). No se comprobó la identidad oficial de los símbolos encontrados; no son recomendaciones.

Se intentó además `https://solana-rpc.publicnode.com`, sin conseguir conexión desde este entorno. No se configuró como alternativa automática ni se ha verificado que soporte los métodos necesarios. Se guardaron `api.dexscreener.com`, `api.mainnet-beta.solana.com` y ese dominio alternativo en el borrador de red, junto con instrucciones de inicio. Revisa y guarda los cambios en la configuración y publica el entorno para conservarlo. Guardar el borrador no demuestra aplicación de permisos ni publicación. Si cambias de RPC público en `rpc_url`, verifica su disponibilidad y sus límites: no necesitas contratar nada para probar la demostración.

El comando termina con código `0` si obtuvo observaciones sin errores de fuente, `1` si obtuvo datos parciales y `2` si no obtuvo observaciones o la configuración es inválida. Un `0` no certifica seguridad, cobertura de todos los lanzamientos, métricas de X ni rentabilidad. Los tokens incompatibles pueden aparecer descartados en una consulta técnicamente correcta.

## Dos grupos, sin plazo de salida fijo

- **Temprano:** mercado seleccionado de hasta 72 horas; liquidez mínima inicial de USD 10 000.
- **Mercado antiguo:** mercado seleccionado de al menos 7 días; liquidez mínima inicial de USD 100 000.
- Los mercados intermedios se etiquetan `developing`; edades ausentes, `unknown`.

Estos umbrales son hipótesis configurables para investigar, no parámetros rentables demostrados. La edad del pool no es la edad del token y no demuestra consolidación. El seguimiento puede durar minutos, horas o días; las entradas y salidas simuladas son manuales. No hay stop-loss ni venta automática.

**Pump.fun:** se pueden observar tokens originados allí cuando DexScreener tenga un mercado compatible, incluidos tokens incorporados por `watchlist`. No se ha integrado un feed directo de todos los lanzamientos, la bonding curve ni eventos anteriores a la graduación. No se infiere su procedencia por el sufijo de la dirección. Esta versión no cubre por completo ese objetivo.

## Riesgo y distribución

`DISCARD` indica motivos de descarte como permisos activos, programa no compatible, precio inválido o liquidez insuficiente. `REVIEW` indica revisión pendiente; **no significa comprar ni token seguro**. Ningún resultado habilita compras automáticas.

La distribución agrupa las cuentas observadas que tienen el mismo propietario. Calcula qué porcentaje de la oferta corresponde a esa muestra y un límite inferior de la participación del mayor propietario observado, usando cantidades enteras originales. No interpreta cada token account como una persona distinta.

Limitaciones que se conservan como desconocidas:

- La muestra de las 20 mayores cuentas no reconstruye todos los tenedores. Un propietario puede tener otras cuentas fuera de ella.
- No se identifican carteras financiadas por un mismo actor ni clusters de insiders.
- No se clasifican pools o exchanges: una cuenta grande puede ser un pool legítimo. La concentración elevada requiere revisión, no una acusación automática de fraude.
- No se comprueba el control o retirada de liquidez, ni una ruta de venta ejecutable.
- Token-2022 permite leer permisos y distribución cuando el RPC responde; se listan sus extensiones, pero queda descartado hasta auditar sus efectos. Otros programas no compatibles también quedan descartados.

La prioridad de observación de 0 a 100 solo combina liquidez, volumen y número de operaciones mediante una heurística documentada en `pump/analysis.py`. No mide usuarios únicos, no detecta todo el wash trading, no usa X y no estima la probabilidad de ganar. Se muestran primero los casos sin motivos de descarte, todos pendientes de revisión.

Entre observaciones, la terminal alerta de una caída de liquidez de al menos 30%, un aumento observado de concentración de al menos 5 puntos o nuevos motivos de descarte. Una alerta puede llegar tarde para vender.

## Añadir evidencia manual de X

En `config.json`, reemplaza el mint y el enlace por valores reales:

```json
{
  "x_evidence": {
    "DIRECCION_MINT_SOLANA": ["https://x.com/usuario/status/123456789"]
  }
}
```

El ejemplo es ilustrativo; el mint debe ser válido para ejecutar la aplicación. Usa la dirección de contrato como referencia al revisar publicaciones, no solo el símbolo. Los enlaces se etiquetan `manual_unverified`: no se verifican autoría, antigüedad, contrato mencionado ni volumen de atención. **Todavía no hay un filtro automatizado de X.** Incorporarlo requiere una fuente autorizada cuya disponibilidad y coste estén verificados; el bot no recurre a scraping, credenciales de sesión ni cifras inventadas.

## Cartera simulada

Después de una consulta reciente que incluya el token:

```bash
python -m pump --config config.json paper-open DIRECCION_MINT_SOLANA --usd 50
# Ejecuta más consultas para observar la evolución.
python -m pump --config config.json scan
python -m pump --config config.json positions
python -m pump --config config.json paper-close 1
```

Estas órdenes registran decisiones ficticias manuales, incluso sobre tokens descartados, para estudiar qué habría mostrado el modelo. Imprimen los riesgos conocidos y pendientes; no son aprobaciones de compra.

Valores iniciales configurables: USD 100 como máximo por posición, USD 500 de exposición abierta, tamaño inferior o igual al 1% de la liquidez observada, deslizamiento supuesto de 200 puntos básicos por lado y USD 0,02 de coste fijo por lado. El importe de entrada incluye el coste fijo. Cada posición conserva sus supuestos de costes hasta el cierre. Se prohíben posiciones duplicadas, cotizaciones de más de 15 minutos y usar la última cotización histórica si el token falta en la consulta más reciente.

El precio consultado no es una cotización ejecutable. La simulación no verifica impacto real, orden de ejecución, MEV, prioridad, transacciones fallidas, capacidad de vender ni disponibilidad de una ruta. Puede mostrar una venta ficticia de un token imposible de vender realmente. No se debe interpretar su resultado como rentabilidad realizable.

Datos reales y sintéticos se guardan en bases distintas; el programa rechaza mezclarlos. `.local/`, `config.json`, bases SQLite y archivos `.env` están ignorados por Git. No guardes claves privadas ni semillas: esta aplicación no las necesita.

## Qué falta para el objetivo completo

1. Resolver los límites del RPC para validar distribución real y ampliar cobertura de las fuentes gratuitas. Los precios y permisos básicos ya se consultaron en vivo.
2. Integrar un feed autorizado de Pump.fun para observar lanzamientos pregraduación.
3. Conseguir una fuente autorizada para medir atención en X automáticamente.
4. Ampliar distribución, clasificación de pools, relación entre carteras y control de liquidez.
5. Registrar señales en tiempo real durante días y evaluar resultados con costes y sesgos de cobertura.

La ejecución con dinero real es un desarrollo posterior. Axiom puede servir para inspección manual; no hay integración con su interfaz ni API en este prototipo.

## Desarrollo

```bash
python -m unittest discover -s tests -v
```

Las pruebas cubren autoridades, datos desconocidos, distribución parcial, selección correcta del token base, costes ficticios, límites, cotizaciones caducadas, errores de fuente y separación demo/live. Los contratos externos se prueban con fixtures: esas pruebas no sustituyen una validación en vivo.
