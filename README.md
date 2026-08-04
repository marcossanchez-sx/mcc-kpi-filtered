# MCC Detection Rate — backend

Backend y dashboard para el detection rate del MCC frente a los contratistas O&M.
Sustituye el flujo de "un HTML de 700 KB por versión": los datos viven en PostgreSQL
y el dashboard los pide a una API.

## Arrancar

Copia el fichero de entorno y cambia la contraseña:

```cmd
copy .env.example .env
```
```bash
cp .env.example .env          # Linux/macOS/Git Bash
```

Los puertos por defecto en `.env` son **8010** (API) y **55432** (Postgres), no 8000 ni
5432: en una máquina de desarrollo esos dos suelen estar ocupados y el contenedor falla
con `port is already allocated`. Si 8010 también choca, cámbialo ahí.

```bash
docker compose up -d --build
docker compose exec api python cli.py bootstrap
```

El `bootstrap` crea el esquema, carga la referencia, aplica las exclusiones de planta e
ingesta los CSV que haya en `data/incoming/`.

- Dashboard: http://localhost:8010
- API navegable: http://localhost:8010/docs
- Estado: http://localhost:8010/api/health

### `password authentication failed for user "mcc"`

Postgres fija la contraseña **sólo la primera vez** que inicializa su directorio de
datos. Si el contenedor arrancó antes de existir `.env`, el volumen quedó con la
contraseña por defecto y no coincide con la nueva.

Si aún no hay datos que conservar, se recrea el volumen:

```bash
docker compose down -v
docker compose up -d
docker compose exec api python cli.py bootstrap
```

Si ya hay datos cargados y no quieres perderlos, cambia la contraseña dentro de
Postgres en lugar de borrar el volumen:

```bash
docker compose exec db psql -U mcc -d mcc -c "ALTER USER mcc WITH PASSWORD 'la-de-tu-.env';"
docker compose restart api
```

### Si el puerto está ocupado

```cmd
netstat -ano | findstr :8010
tasklist /FI "PID eq <el PID que salga>"
```

Cambia `API_PORT` en `.env` y vuelve a lanzar `docker compose up -d`.

## Cargar datos nuevos

Dos vías, ambas idempotentes — **volver a cargar el mismo fichero no duplica nada**
(se compara el hash del contenido):

**Carpeta vigilada**

```bash
cp "Exportar (5).csv" data/incoming/
docker compose exec api python cli.py ingest --move
```

**Desde el dashboard**: pestaña *Cargas y auditoría* → subir el CSV. El scope se
recalcula solo.

## Por qué el modelo es así

### Los exports se guardan como snapshots, no se sobrescriben

Analizando los CSV apareció algo que obliga a este diseño: **la misma incidencia
cambia de atribución entre exports**. Nueve incidencias del 27 de julio figuraban
como `O&M Contractor` en el export del 28-jul y como `MCC` en el del 3-ago — misma
planta, mismo minuto de inicio, misma descripción palabra por palabra.

Si los exports se sumaran, esas nueve se contarían dos veces con valores
contradictorios. Si se sobrescribieran, el cambio se perdería sin rastro.

Aquí cada fichero deja un snapshot inmutable en `wo_observation`, y el scope se
calcula con la versión más reciente de cada work order. Consecuencias prácticas:

- no hay que decidir a mano si "sustituir o sumar" al recargar un periodo,
- los cambios quedan en `attribution_change` y se ven en `/api/audit/attribution-changes`,
- **el detection rate de un periodo puede cambiar según cuándo exportes**, y eso ahora
  es visible en vez de silencioso. Merece comentarse con quien mantiene el CMMS: una
  cifra ya reportada puede quedar desfasada.

### La clave natural incluye la descripción

`plant + start_ts + equipment + incident_type + hash(description)`.

La descripción no es opcional: sin ella la clave colapsa work orders distintas. En el
export real hay 380 grupos con dos WOs del mismo contratista, en la misma planta y el
mismo minuto, pero de inversores diferentes (`Inverter 8.3.2` frente a
`Inverter 12.2.3`). Sin la descripción se perderían más de 2.000 filas.

Lo correcto sería un identificador de WO, pero `SX WO Number` viene vacío en el 100%
de los casos — es la deuda de trazabilidad ya documentada del proyecto. Cuando se
empiece a rellenar, conviene migrar la clave a ese campo.

Quedan ~0,7% de filas idénticas en todos los campos disponibles. No se descartan (serían
WOs reales perdidas): se les añade un sufijo de orden estable.

### Las reglas de scope son datos, no código

El denominador debe ser "incidencias que el MCC podía haber detectado". Las reglas
viven en tablas (`plant`, `scope_rule`, `contractor_alias`), así que cuando cambie el
informe N3C o el mapeo de portfolios se recarga la referencia y se llama a
`rebuild_scope()` — y **el histórico completo se recalcula** de forma coherente, sin
volver a tocar un solo CSV.

Reglas activas:

| Regla | Motivo |
|---|---|
| Sólo plantas con onboarding N3C completado, desde su `Completed Since` | Antes de esa fecha el MCC no tenía visibilidad |
| 9 tipos de equipo en scope, 8 fuera | Sin telemetría no hay señal que vigilar |
| Sólo Production Loss y Communication Loss | El resto no es detección |
| ES / PT / CL: sólo `Cause = Failure` | Curtailment, red y meteorología no son averías detectables |
| Japón fuera | Shadowing, no es operación del MCC |
| Visibilidad por dispositivo (SST / INV / POI / WST / PPC) | Un equipo puede existir y no estar monitorizado |
| Castelnau excluida desde 2026-07-03 | Perdió comunicación SCADA |

Nada se descarta en silencio: cada WA fuera de scope guarda su motivo, consultable en
`/api/scope/excluded`. Sirve para responder "¿por qué el denominador es este?" sin
volver a los CSV, que es la pregunta que siempre aparece al presentar el número.

## Dos trampas de cálculo

**El rate por importe se calcula sólo sobre las WOs con `Revenue Loss` informado**
(84,6% del total). Las que no lo tienen no cuentan como cero: quedan fuera del
numerador y del denominador. Tratarlas como cero hundiría el rate artificialmente.
La cobertura se devuelve en cada respuesta; por debajo del 80% el número es indicativo.

**El tiempo de detección usa mediana, no media.** La distribución tiene colas muy
largas (incidencias registradas semanas después) y la media no representa el caso
habitual: MCC mediana 2,15 h frente a media 21,7 h. Las WOs con creación anterior al
inicio (67 casos, inconsistencia del origen) se descartan en lugar de contarse como
tiempo negativo.

## API

| Endpoint | Para qué |
|---|---|
| `GET /api/kpis/summary` | KPIs del periodo con los tres ejes: WO, importe y tiempo |
| `GET /api/kpis/timeseries?granularity=week\|month` | Serie temporal |
| `GET /api/kpis/by/{country\|portfolio\|contractor\|equipment\|plant\|month\|week}` | Agrupación |
| `GET /api/kpis/revenue-concentration` | Cuánto del importe está en las incidencias más caras |
| `GET /api/kpis/status-split` | Reparto abierto/cerrado con sus métricas, ignorando el filtro de estado |
| `GET /api/wos/missed` | WOs abiertas por O&M sin detección previa — la lista accionable |
| `GET /api/scope/excluded` | Motivos de exclusión, para justificar el denominador |
| `GET /api/scope/rules` | Reglas activas y exclusiones de planta |
| `GET /api/audit/attribution-changes` | Incidencias reatribuidas entre exports |
| `GET /api/audit/source-files` | Ficheros cargados |
| `POST /api/ingest/wo-export` | Subir un export |
| `POST /api/ingest/reference/{onboarding\|visibility\|portfolios\|aliases}` | Recargar referencia |
| `POST /api/scope/rebuild` | Recalcular el scope |

Filtros comunes, combinables entre sí: `from`, `to` (`YYYY-MM`), `country`,
`portfolio`, `contractor`, `equipment`, `incident_type` (`P`/`C`), `status`
(`open`/`closed`).

El portfolio es **ortogonal al país**, no un subnivel: Toro 1 tiene plantas en Francia,
Italia y España, y Toro 2 en cinco países. Agruparlo bajo país partiría Toro 1 en tres
trozos y ocultaría la comparación que importa.

## El dashboard

Tres vistas, en el lateral izquierdo:

- **Detection Rate** — 5 KPIs (rate, volumen, detectadas, mediana de detección, no detectadas),
  evolución semanal conmutable entre WO y euros, desglose por equipo, panel de portfolio o
  contratista, tabla de plantas, **iniciativas y hallazgos**, y el detalle de por qué se
  descarta cada WO fuera de scope.
- **Tendencia** — veredicto de dirección con avisos de fiabilidad, las dos métricas juntas mes
  a mes, composición del periodo en anillo, tiempo de detección MCC frente a O&M en escala
  logarítmica, y un glosario de qué significa cada cosa y qué conclusión admite.
- **Cargas y auditoría** — subir exports, ver las reatribuciones detectadas y los ficheros
  cargados.

Los hallazgos se calculan sobre la selección activa y sólo aparecen si el dato lo justifica
(umbrales mínimos de volumen). Las iniciativas curadas llevan ámbito de país: Castelnau sólo
sale en Francia, y `Cause = Failure` sólo en España, Portugal y Chile.

## Tests

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

89 tests sobre SQLite en memoria, sin necesidad de Postgres. Cubren cada regla de
scope, la idempotencia de la ingesta, la detección de reatribuciones, la normalización
de contratistas y los endpoints. Cada test corresponde a un caso encontrado en los
datos reales: si uno falla, el detection rate cambia.

## Validación contra el análisis manual

Cargando los dos exports (abril–julio 2026), el backend reproduce el dashboard:

| País | Backend | Dashboard |
|---|---|---|
| España | 816 · 48,9% | 816 · 48,9% |
| Italia | 724 · 58,8% | 723 · 58,9% |
| Chile | 387 · 34,9% | 387 · 34,9% |
| Francia | 197 · 36,5% | 197 · 36,5% |
| Portugal | 36 · 44,4% | 36 · 44,4% |
| Polonia | 19 · 52,6% | 19 · 52,6% |
| **Total** | **2.180 · 48,6%** | **2.179 · 48,6%** |

Seis de siete países coinciden exactamente. La WO de diferencia en Italia es un
duplicado que el pipeline manual descartaba y el backend conserva.

**Una cifra queda corregida**: la mediana de detección de O&M es **41,8 h**, no las
26,5 h del dashboard. El backend la calcula de los timestamps de la propia fila; en el
dashboard se reconstruyó cruzando ficheros y ese cruce era menos fiable. La ventaja del
MCC es mayor de lo que se dijo: 2,15 h frente a 41,8 h, unas 19 veces más rápido.

## Estructura

```
backend/app/
  models.py      esquema (snapshots + referencia + resultado del scope)
  scope.py       reglas de scope como funciones puras
  ingest.py      ingesta idempotente y rebuild_scope()
  queries.py     agregaciones de KPIs
  reference.py   carga de plantas, visibilidad, portfolios y alias
  main.py        API y servido del dashboard
backend/cli.py   administración por línea de comandos
frontend/        dashboard que consume la API (Chart.js vendorizado, sin CDN)
data/reference/  ficheros de referencia
data/incoming/   deja aquí los exports nuevos
```

## Pendiente

- **Sin autenticación.** Pensado para local. Antes de exponerlo a la red hace falta al
  menos auth en los endpoints `POST`, y restringir `CORS_ORIGINS`.
- **Datos sensibles**: la base contiene nombres de planta y descripciones de todas las
  incidencias. Información operativa interna.
- **Métricas por operador individual**: no implementado a propósito. Accuracy y tiempo
  de detección por persona entra en propósito restringido del EU AI Act (evaluación de
  rendimiento y monitorización de trabajadores) y requiere visto bueno de Digital,
  Legal y People Team antes de diseñarlo.
- Migraciones: el esquema se crea con `create_all`. Para cambios en producción conviene
  añadir Alembic.
