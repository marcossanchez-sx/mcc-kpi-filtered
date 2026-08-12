"""
API del MCC Detection Rate.

Sirve además el dashboard desde /, para que deje de haber un HTML de 700 KB por
versión: el frontend pide los datos a estos endpoints.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import logging
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import queries as q
from . import reference
from .db import get_session, init_db
from .ingest import IngestError, ingest_csv, rebuild_scope
from .models import AttributionChange, Plant, ScopeRule, SourceFile, WoScoped

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", "/frontend"))

@contextlib.asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """
    Verifica el esquema al arrancar, pero sin morir si la base de datos aún no está
    lista: el contenedor debe poder levantar y responder al healthcheck mientras
    Postgres termina de arrancar. El fallo se ve en /api/health, no en un crash loop.
    """
    try:
        init_db()
        log.info("esquema verificado")
    except Exception as exc:  # noqa: BLE001
        log.warning("no se pudo verificar el esquema al arrancar: %s", exc)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="MCC Detection Rate API",
    version="1.0.0",
    description=(
        "Detection rate del MCC frente a los contratistas O&M. "
        "Las WOs se guardan como snapshots por export; el scope se recalcula desde "
        "las tablas de referencia."
    ),
)

# El dashboard puede abrirse como fichero local durante el desarrollo.
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


def filters_from_query(
    date_from: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}$"),
    date_to: str | None = Query(None, alias="to", pattern=r"^\d{4}-\d{2}$"),
    country: str | None = None,
    portfolio: str | None = None,
    contractor: str | None = None,
    equipment: str | None = None,
    incident_type: str | None = Query(None, pattern="^[PC]$"),
    status: str | None = Query(None, pattern="^(open|closed)$"),
    scope: str = Query(
        "in",
        pattern="^(in|out|all)$",
        description="'in' = lo que aplica al MCC (por defecto), 'out' = lo descartado, "
        "'all' = el export en bruto",
    ),
) -> q.Filters:
    return q.Filters(
        date_from=date_from,
        date_to=date_to,
        country=country,
        portfolio=portfolio,
        contractor=contractor,
        equipment=equipment,
        incident_type=incident_type,
        status=status,
        scope=scope,
    )


@app.get("/api/health", tags=["meta"])
def health(session: Session = Depends(get_session)) -> dict:
    """Estado real: si la base de datos no responde, devuelve 503."""
    scoped = session.scalar(select(func.count()).select_from(WoScoped)) or 0
    in_scope = (
        session.scalar(
            select(func.count()).select_from(WoScoped).where(WoScoped.in_scope.is_(True))
        )
        or 0
    )
    files = session.scalar(select(func.count()).select_from(SourceFile)) or 0
    plants = session.scalar(select(func.count()).select_from(Plant)) or 0
    return {
        "status": "ok",
        "observations_scoped": scoped,
        "in_scope": in_scope,
        "source_files": files,
        "plants_reference": plants,
    }


@app.get("/api/meta", tags=["meta"])
def get_meta(session: Session = Depends(get_session)) -> dict:
    return q.meta(session)


@app.get("/api/kpis/summary", tags=["kpis"])
def kpi_summary(
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> dict:
    return q.summary(session, filters)


@app.get("/api/kpis/timeseries", tags=["kpis"])
def kpi_timeseries(
    granularity: str = Query("week", pattern="^(week|month)$"),
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> list[dict]:
    return q.timeseries(session, filters, granularity)


@app.get("/api/kpis/by/{dimension}", tags=["kpis"])
def kpi_by_dimension(
    dimension: str,
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> list[dict]:
    try:
        return q.by_dimension(session, filters, dimension)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/wos/missed", tags=["wos"])
def wos_missed(
    limit: int = Query(500, ge=1, le=5000),
    offset: int = Query(0, ge=0),
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> dict:
    return q.missed_wos(session, filters, limit=limit, offset=offset)


@app.get("/api/kpis/revenue-concentration", tags=["kpis"])
def kpi_revenue_concentration(
    top: int = Query(5, ge=1, le=50),
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> dict:
    """
    Concentración del importe en las incidencias más caras.

    Necesario para no leer una variación del rate económico como cambio de rendimiento
    cuando en realidad la mueve un único evento grande.
    """
    return q.revenue_concentration(session, filters, top=top)


@app.get("/api/kpis/status-split", tags=["kpis"])
def kpi_status_split(
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> dict:
    """Reparto abierto/cerrado, ignorando el filtro de estado, con sus métricas."""
    return q.status_split(session, filters)


@app.get("/api/scope/excluded", tags=["scope"])
def scope_excluded(
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> list[dict]:
    """Por qué queda fuera cada WO descartada. Para justificar el denominador."""
    return q.excluded_breakdown(session, filters)


@app.get("/api/scope/comparison", tags=["scope"])
def scope_comparison(
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
) -> dict:
    """
    Lo que aplica al MCC frente a lo que no, con los mismos filtros a los dos lados.

    Ignora el parámetro `scope` de la petición: devuelve los tres bloques siempre.
    """
    return q.scope_comparison(session, filters)


@app.get("/api/kpis/response-times", tags=["kpis"])
def response_times(
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
    dimension: str | None = Query(None, description="opcional: desglose por dimensión"),
) -> dict:
    """
    Cadena de tiempos del export: detección, actuación, resolución, cierre, validación
    y total, con mediana MCC / O&M, p90 y **cobertura**.

    La cobertura es imprescindible: Detección y Total vienen informados casi siempre,
    pero Resolución ronda el 2% de las filas. Una mediana sobre el 2% no es un KPI.
    """
    overall = q.summary(session, filters)
    payload: dict = {"overall": overall["times"], "wos": overall["wos"]}
    if dimension:
        payload["by"] = [
            {"key": row["key"], "wos": row["wos"], "times": row["times"]}
            for row in q.by_dimension(session, filters, dimension)
        ]
        payload["dimension"] = dimension
    return payload


@app.get("/api/audit/vanished", tags=["audit"])
def audit_vanished(
    filters: q.Filters = Depends(filters_from_query),
    session: Session = Depends(get_session),
    limit: int = Query(500, ge=1, le=5000),
) -> dict:
    """
    WOs conservadas tras desaparecer del origen.

    Siguen contando en el detection rate. Están aquí para revisarlas en eMaint, no
    para descontarlas: quitarlas movería porcentajes ya publicados.
    """
    return q.vanished_wos(session, filters, limit=limit)


@app.get("/api/scope/rules", tags=["scope"])
def scope_rules(session: Session = Depends(get_session)) -> dict:
    rules: dict[str, list[dict]] = {}
    for rule in session.scalars(select(ScopeRule).order_by(ScopeRule.kind, ScopeRule.value)):
        rules.setdefault(rule.kind, []).append({"value": rule.value, "note": rule.note})
    exclusions = [
        {
            "plant": p.name,
            "country": p.country,
            "from": p.excluded_from.isoformat() if p.excluded_from else None,
            "reason": p.excluded_reason,
        }
        for p in session.scalars(select(Plant).where(Plant.excluded_from.isnot(None)))
    ]
    return {"rules": rules, "plant_exclusions": exclusions}


@app.get("/api/audit/attribution-changes", tags=["audit"])
def attribution_changes(
    limit: int = Query(200, ge=1, le=2000),
    session: Session = Depends(get_session),
) -> dict:
    """
    Incidencias cuya atribución cambió entre exports.

    Importa porque significa que el detection rate de un periodo puede cambiar según
    cuándo se exporte, y una cifra ya reportada puede quedar desfasada.
    """
    rows = session.scalars(
        select(AttributionChange).order_by(AttributionChange.detected_at.desc()).limit(limit)
    ).all()
    return {
        "total": session.scalar(select(func.count()).select_from(AttributionChange)) or 0,
        "items": [
            {
                "plant": r.plant_raw,
                "start_ts": r.start_ts.isoformat(),
                "equipment": r.equipment,
                "field": r.field,
                "from": r.old_value,
                "to": r.new_value,
                "detected_at": r.detected_at.isoformat() if r.detected_at else None,
            }
            for r in rows
        ],
    }


@app.get("/api/audit/source-files", tags=["audit"])
def source_files(session: Session = Depends(get_session)) -> list[dict]:
    return [
        {
            "id": f.id,
            "filename": f.filename,
            "loaded_at": f.loaded_at.isoformat() if f.loaded_at else None,
            "rows_total": f.rows_total,
            "rows_inserted": f.rows_inserted,
            "attribution_changes": f.rows_superseded,
            "period_start": f.period_start.isoformat() if f.period_start else None,
            "period_end": f.period_end.isoformat() if f.period_end else None,
        }
        for f in session.scalars(select(SourceFile).order_by(SourceFile.loaded_at.desc()))
    ]


@app.post("/api/ingest/wo-export", tags=["ingest"])
async def ingest_export(
    file: UploadFile = File(...),
    rebuild: bool = Query(True, description="recalcular el scope tras la carga"),
    session: Session = Depends(get_session),
) -> dict:
    """Sube un export de Work Orders. Reprocesar el mismo fichero no duplica nada."""
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="fichero vacío")
    try:
        result = ingest_csv(session, content=content, filename=file.filename or "upload.csv")
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    payload = result.as_dict()
    if rebuild and not result.already_loaded:
        payload["scope"] = rebuild_scope(session)
    return payload


@app.post("/api/ingest/reference/{kind}", tags=["ingest"])
async def ingest_reference(
    kind: str,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
) -> dict:
    """Recarga un fichero de referencia: onboarding, visibility, portfolios, aliases."""
    loaders = {
        "onboarding": reference.load_onboarding,
        "visibility": reference.load_visibility,
        "portfolios": reference.load_portfolios,
        "aliases": reference.load_contractor_aliases,
    }
    if kind not in loaders:
        raise HTTPException(
            status_code=400, detail=f"tipo no válido; usa: {', '.join(sorted(loaders))}"
        )
    content = await file.read()
    rows = loaders[kind](session, content)
    return {"kind": kind, "rows": rows, "note": "llama a /api/scope/rebuild para aplicar"}


@app.post("/api/scope/rebuild", tags=["scope"])
def scope_rebuild(session: Session = Depends(get_session)) -> dict:
    """Recalcula el scope completo. Idempotente y rápido."""
    return rebuild_scope(session)


@app.post("/api/scope/exclude-plant", tags=["scope"])
def exclude_plant(
    plant: str,
    from_date: dt.date,
    reason: str,
    session: Session = Depends(get_session),
) -> dict:
    ok = reference.apply_plant_exclusion(
        session, plant_name=plant, from_date=from_date, reason=reason
    )
    if not ok:
        raise HTTPException(status_code=404, detail=f"planta no encontrada: {plant}")
    return {"plant": plant, "excluded_from": from_date.isoformat(), "reason": reason}


# El dashboard va al final para que /api/* tenga prioridad de rutas.
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    def dashboard() -> FileResponse:
        index = FRONTEND_DIR / "index.html"
        if not index.exists():
            raise HTTPException(status_code=404, detail="frontend no encontrado")
        return FileResponse(str(index))
