"""
Ingesta de exports CSV y reconstrucción del scope.

Dos operaciones independientes:

  ingest_csv()    -> guarda un snapshot inmutable del fichero y detecta reatribuciones
  rebuild_scope() -> recalcula qué entra en el cálculo, a partir de los snapshots
                     vigentes y las tablas de referencia

Están separadas a propósito: si mañana cambia la matriz de visibilidad o el mapeo de
portfolios, se recarga la referencia y se llama a rebuild_scope() — sin volver a
tocar ningún CSV y con el histórico recalculado de forma coherente.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
from dataclasses import dataclass, field

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import scope as sc
from .models import (
    AttributionChange,
    ContractorAlias,
    Plant,
    SourceFile,
    WoObservation,
    WoScoped,
)

log = logging.getLogger(__name__)

# El export ha cambiado de nombres de columna al menos una vez: en agosto de 2026
# `Om Contract` pasó a `O&M Contractor` y `Revenue Loss` a `Revenue Loss (€)`. Como
# ambos son campos opcionales, la carga habría funcionado perdiendo el contratista y
# el importe **en silencio** — el peor fallo posible en un pipeline de datos.
#
# Por eso cada campo lógico declara todos sus alias conocidos, y los que alimentan
# una dimensión del análisis se comprueban explícitamente: si ninguno de sus alias
# está presente, se avisa en lugar de continuar.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "plant": ("Asset",),
    "start_ts": ("Start Ts Local",),
    "equipment": ("Equipment",),
    "incident_type": ("Incident Type",),
    "cmms_user": ("CMMS User",),
    "country": ("Country",),
    "wo_created_ts": ("WO Created Ts Local",),
    "cause": ("Cause",),
    "ongoing": ("Ongoing",),
    "contractor": ("O&M Contractor", "Om Contract"),
    "description": ("Description English",),
    "capacity": ("Capacity Affected",),
    "revenue": ("Revenue Loss (€)", "Revenue Loss"),
    "lifecycle": ("Incident Lifecycle (hrs)",),
    "wo_url": ("Url Emaint",),
    "failure_cause": ("Failure Cause",),
    "supervisor": ("O&M Supervisor",),
}

# Sin estos no se puede identificar ni atribuir una WO: la carga se aborta.
REQUIRED_FIELDS = ("plant", "start_ts", "equipment", "incident_type", "cmms_user")

# Estos alimentan una dimensión o una métrica del dashboard. Si faltan, la carga
# sigue pero se registra un aviso que llega hasta la respuesta de la API.
EXPECTED_FIELDS = ("contractor", "revenue", "ongoing", "wo_created_ts", "cause", "country")


def resolve_columns(columns: set[str]) -> tuple[dict[str, str], list[str]]:
    """
    Empareja cada campo lógico con el nombre real que trae el fichero.

    Devuelve el mapeo y la lista de campos esperados que no se han encontrado bajo
    ningún alias, para poder avisar en vez de perderlos sin más.
    """
    mapping: dict[str, str] = {}
    for logical, aliases in FIELD_ALIASES.items():
        for alias in aliases:
            if alias in columns:
                mapping[logical] = alias
                break
    missing_expected = [f for f in EXPECTED_FIELDS if f not in mapping]
    return mapping, missing_expected


class IngestError(ValueError):
    """Problema con el fichero que impide cargarlo."""


@dataclass
class IngestResult:
    source_file_id: int | None
    filename: str
    rows_total: int
    rows_inserted: int
    rows_skipped: int
    attribution_changes: int
    already_loaded: bool
    period_start: dt.date | None
    period_end: dt.date | None
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "source_file_id": self.source_file_id,
            "filename": self.filename,
            "rows_total": self.rows_total,
            "rows_inserted": self.rows_inserted,
            "rows_skipped": self.rows_skipped,
            "attribution_changes": self.attribution_changes,
            "already_loaded": self.already_loaded,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "warnings": self.warnings,
        }


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        result = float(text)
    except ValueError:
        return None
    # NaN/inf no son válidos en JSON y envenenan cualquier agregación posterior.
    return result if result == result and abs(result) != float("inf") else None


def _to_bool(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes", "si", "sí"}


def _latest_observation_subquery():
    """Última observación por natural_key, ordenando por el id del fichero."""
    return (
        select(
            WoObservation.natural_key.label("nk"),
            func.max(WoObservation.source_file_id).label("max_file"),
        )
        .group_by(WoObservation.natural_key)
        .subquery()
    )


def ingest_csv(
    session: Session,
    *,
    content: bytes,
    filename: str,
    notes: str | None = None,
) -> IngestResult:
    """
    Carga un export. Idempotente: el mismo contenido no se procesa dos veces.

    No decide nada sobre el scope — sólo persiste lo que dice el fichero y anota si
    alguna incidencia ya conocida ha cambiado de atribución.
    """
    digest = hashlib.sha256(content).hexdigest()

    existing = session.scalar(select(SourceFile).where(SourceFile.content_sha256 == digest))
    if existing is not None:
        log.info("fichero ya cargado (%s), no se reprocesa", filename)
        return IngestResult(
            source_file_id=existing.id,
            filename=existing.filename,
            rows_total=existing.rows_total,
            rows_inserted=0,
            rows_skipped=existing.rows_total,
            attribution_changes=0,
            already_loaded=True,
            period_start=existing.period_start,
            period_end=existing.period_end,
        )

    # utf-8-sig quita el BOM que suele traer Excel.
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = content.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise IngestError("no se pudo decodificar el fichero (probé utf-8 y latin-1)")

    reader = csv.DictReader(io.StringIO(text))
    columns = set(reader.fieldnames or [])
    col, missing_expected = resolve_columns(columns)

    missing_required = [f for f in REQUIRED_FIELDS if f not in col]
    if missing_required:
        expected = ", ".join(
            f"{f} (uno de: {' / '.join(FIELD_ALIASES[f])})" for f in missing_required
        )
        raise IngestError(f"faltan columnas obligatorias: {expected}")

    warnings: list[str] = []
    for fieldname in missing_expected:
        aliases = " / ".join(FIELD_ALIASES[fieldname])
        warnings.append(
            f"no se encontró ninguna columna para '{fieldname}' (esperaba {aliases}); "
            f"esa dimensión quedará vacía en este fichero"
        )
        log.warning("columna ausente para %s en %s", fieldname, filename)

    def value(row: dict, fieldname: str) -> str | None:
        """Lee un campo por su nombre lógico, con el alias que traiga este fichero."""
        name = col.get(fieldname)
        return row.get(name) if name else None

    source = SourceFile(filename=filename, content_sha256=digest, notes=notes)
    session.add(source)
    session.flush()

    # Estado vigente antes de esta carga, para detectar reatribuciones.
    latest = _latest_observation_subquery()
    previous: dict[str, tuple[str | None, int]] = {
        row.natural_key: (row.cmms_user, row.source_file_id)
        for row in session.execute(
            select(WoObservation.natural_key, WoObservation.cmms_user, WoObservation.source_file_id)
            .join(
                latest,
                (WoObservation.natural_key == latest.c.nk)
                & (WoObservation.source_file_id == latest.c.max_file),
            )
        )
    }

    seen_keys: dict[str, int] = {}
    inserted = skipped = changes = 0
    total = 0
    min_day: dt.date | None = None
    max_day: dt.date | None = None

    for row in reader:
        total += 1
        start_ts = sc.parse_ts(value(row, "start_ts"))
        if start_ts is None:
            skipped += 1
            continue

        plant_raw = (value(row, "plant") or "").strip()
        equipment = (value(row, "equipment") or "").strip()
        incident_type = (value(row, "incident_type") or "").strip()
        description = (value(row, "description") or "").strip()
        key = sc.natural_key(plant_raw, start_ts, equipment, incident_type, description)

        # Quedan ~0,7% de filas idénticas en todos los campos que tenemos. No las
        # descartamos —serían WOs reales perdidas— sino que les damos un sufijo
        # estable por orden de aparición. Así el total cuadra con el CSV y el
        # emparejamiento entre exports sigue funcionando (#1 con #1, #2 con #2).
        occurrence = seen_keys.get(key, 0) + 1
        seen_keys[key] = occurrence
        if occurrence > 1:
            key = f"{key}#{occurrence}"

        cmms_user = (value(row, "cmms_user") or "").strip()
        is_mcc = cmms_user.upper() == "MCC"

        session.add(
            WoObservation(
                source_file_id=source.id,
                natural_key=key,
                plant_raw=plant_raw,
                plant_norm=sc.normalize(plant_raw),
                country=(value(row, "country") or "").strip() or None,
                start_ts=start_ts,
                wo_created_ts=sc.parse_ts(value(row, "wo_created_ts")),
                equipment=equipment or None,
                incident_type=incident_type or None,
                cause=(value(row, "cause") or "").strip() or None,
                cmms_user=cmms_user or None,
                is_mcc=is_mcc,
                ongoing=_to_bool(value(row, "ongoing")),
                om_contract_raw=(value(row, "contractor") or "").strip() or None,
                description=description or None,
                capacity_affected=_to_float(value(row, "capacity")),
                revenue_loss=_to_float(value(row, "revenue")),
                incident_lifecycle_hrs=_to_float(value(row, "lifecycle")),
                wo_url=(value(row, "wo_url") or "").strip() or None,
                failure_cause=(value(row, "failure_cause") or "").strip() or None,
            )
        )
        inserted += 1

        day = start_ts.date()
        min_day = day if min_day is None or day < min_day else min_day
        max_day = day if max_day is None or day > max_day else max_day

        prior = previous.get(key)
        if prior is not None and prior[0] != cmms_user:
            session.add(
                AttributionChange(
                    natural_key=key,
                    plant_raw=plant_raw,
                    start_ts=start_ts,
                    equipment=equipment or None,
                    field="cmms_user",
                    old_value=prior[0],
                    new_value=cmms_user,
                    from_file_id=prior[1],
                    to_file_id=source.id,
                )
            )
            changes += 1

    source.rows_total = total
    source.rows_inserted = inserted
    source.rows_superseded = changes
    source.period_start = min_day
    source.period_end = max_day
    session.flush()

    log.info(
        "cargado %s: %d filas, %d insertadas, %d reatribuciones",
        filename, total, inserted, changes,
    )
    return IngestResult(
        source_file_id=source.id,
        filename=filename,
        rows_total=total,
        rows_inserted=inserted,
        rows_skipped=skipped,
        attribution_changes=changes,
        already_loaded=False,
        period_start=min_day,
        period_end=max_day,
        warnings=warnings,
    )


def rebuild_scope(session: Session) -> dict:
    """
    Recalcula wo_scoped desde cero con las observaciones vigentes.

    Se borra y se reconstruye en lugar de actualizar incrementalmente: son decenas
    de miles de filas, tarda poco, y elimina la posibilidad de que queden restos
    incoherentes tras cambiar una regla.
    """
    plants = {p.name_norm: p for p in session.scalars(select(Plant))}
    aliases = [
        (a.pattern, a.canonical)
        for a in session.scalars(select(ContractorAlias).order_by(ContractorAlias.id))
    ]

    session.execute(delete(WoScoped))

    latest = _latest_observation_subquery()
    observations = session.scalars(
        select(WoObservation).join(
            latest,
            (WoObservation.natural_key == latest.c.nk)
            & (WoObservation.source_file_id == latest.c.max_file),
        )
    )

    kept = 0
    reasons: dict[str, int] = {}

    for obs in observations:
        record = plants.get(obs.plant_norm)
        plant_scope = None
        if record is not None:
            plant_scope = sc.PlantScope(
                name=record.name,
                country=record.country,
                portfolio=record.portfolio,
                completed_since=record.completed_since,
                vis_sst=record.vis_sst,
                vis_poi=record.vis_poi,
                vis_ppc=record.vis_ppc,
                vis_wst=record.vis_wst,
                vis_inv_pct=record.vis_inv_pct,
                excluded_from=record.excluded_from,
                excluded_reason=record.excluded_reason,
            )

        reason = sc.evaluate(
            plant=plant_scope,
            start_ts=obs.start_ts,
            equipment=obs.equipment,
            incident_type=obs.incident_type,
            cause=obs.cause,
            country=obs.country,
            incident_lifecycle_hrs=obs.incident_lifecycle_hrs,
        )

        day = obs.start_ts.date()
        session.add(
            WoScoped(
                observation_id=obs.id,
                natural_key=obs.natural_key,
                plant=record.name if record else obs.plant_raw,
                country=(record.country if record else obs.country) or "Desconocido",
                portfolio=(record.portfolio if record else None) or "Sin asignar",
                contractor=sc.canonical_contractor(obs.om_contract_raw, aliases),
                contractor_raw=obs.om_contract_raw,
                start_date=day,
                month=day.strftime("%Y-%m"),
                iso_week=sc.iso_week_start(day).isoformat(),
                is_mcc=obs.is_mcc,
                equipment=obs.equipment or "Desconocido",
                incident_type=sc.INCIDENT_TYPES.get((obs.incident_type or "").strip(), "?"),
                ongoing=obs.ongoing,
                description=obs.description,
                capacity_affected=obs.capacity_affected,
                revenue_loss=obs.revenue_loss,
                detection_hours=sc.detection_hours(obs.start_ts, obs.wo_created_ts),
                wo_url=obs.wo_url,
                failure_cause=obs.failure_cause,
                in_scope=reason is None,
                excluded_reason=reason,
            )
        )
        if reason is None:
            kept += 1
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    session.flush()
    log.info("scope reconstruido: %d en scope, %d fuera", kept, sum(reasons.values()))
    return {
        "in_scope": kept,
        "excluded": sum(reasons.values()),
        "excluded_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
    }
