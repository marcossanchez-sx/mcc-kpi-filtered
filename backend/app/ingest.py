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

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from . import scope as sc
from .models import (
    AttributionChange,
    ContractorAlias,
    Plant,
    ScopeRule,
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
    "detection_hrs": ("Detection (hrs)",),
    "act_hrs": ("Act (hrs)",),
    "resolution_hrs": ("Resolution (hrs)",),
    "completion_hrs": ("Completion (hrs)",),
    "validation_hrs": ("Validation (hrs)",),
    "total_time_hrs": ("Total time (hrs)",),
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
    """
    Observación vigente de cada natural_key: la de la foto más reciente.

    Se ordena por `SourceFile.as_of` y sólo se desempata por id. Ordenar por id sería
    ordenar por *orden de carga*, y entonces recargar un export antiguo lo ascendería a
    "el más reciente" y machacaría datos buenos con datos viejos. Con as_of el
    resultado no depende del orden en que se carguen los ficheros.
    """
    ranked = (
        select(
            WoObservation.identity.label("nk"),
            WoObservation.source_file_id.label("fid"),
            func.row_number()
            .over(
                partition_by=WoObservation.identity,
                order_by=(
                    SourceFile.as_of.desc().nullslast(),
                    WoObservation.source_file_id.desc(),
                ),
            )
            .label("rn"),
        )
        .join(SourceFile, SourceFile.id == WoObservation.source_file_id)
        .subquery()
    )
    return (
        select(ranked.c.nk.label("nk"), ranked.c.fid.label("max_file"))
        .where(ranked.c.rn == 1)
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
    previous: dict[str, tuple[str | None, int, str | None]] = {
        row.identity: (row.cmms_user, row.source_file_id, row.cause)
        for row in session.execute(
            select(
                WoObservation.identity,
                WoObservation.cmms_user,
                WoObservation.source_file_id,
                WoObservation.cause,
            )
            .join(
                latest,
                (WoObservation.identity == latest.c.nk)
                & (WoObservation.source_file_id == latest.c.max_file),
            )
        )
    }

    seen_keys: dict[str, int] = {}
    inserted = skipped = changes = 0
    total = 0
    min_day: dt.date | None = None
    max_day: dt.date | None = None
    max_created: dt.datetime | None = None

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
        url = (value(row, "wo_url") or "").strip() or None
        guid = sc.wo_guid(url)

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
                detection_hrs_src=_to_float(value(row, "detection_hrs")),
                act_hrs=_to_float(value(row, "act_hrs")),
                resolution_hrs=_to_float(value(row, "resolution_hrs")),
                completion_hrs=_to_float(value(row, "completion_hrs")),
                validation_hrs=_to_float(value(row, "validation_hrs")),
                total_time_hrs=_to_float(value(row, "total_time_hrs")),
                wo_url=url,
                wo_guid=guid,
                # El GUID manda; la clave compuesta es el respaldo para los exports
                # que no traen la columna Url Emaint.
                identity=guid or key,
                failure_cause=(value(row, "failure_cause") or "").strip() or None,
            )
        )
        inserted += 1

        created = sc.parse_ts(value(row, "wo_created_ts"))
        if created is not None and (max_created is None or created > max_created):
            max_created = created

        day = start_ts.date()
        min_day = day if min_day is None or day < min_day else min_day
        max_day = day if max_day is None or day > max_day else max_day

        prior = previous.get(guid or key)
        if prior is not None:
            cause_value = (value(row, "cause") or "").strip() or None
            # Se auditan los dos campos que reescriben el pasado: quién la abrió y por
            # qué. El segundo puede sacar una WO del scope retroactivamente, así que
            # dejarlo sin traza haría imposible explicar por qué cambió una cifra.
            for field_name, old, new in (
                ("cmms_user", prior[0], cmms_user),
                ("cause", prior[2], cause_value),
            ):
                if (old or "") == (new or ""):
                    continue
                session.add(
                    AttributionChange(
                        natural_key=key,
                        plant_raw=plant_raw,
                        start_ts=start_ts,
                        equipment=equipment or None,
                        field=field_name,
                        old_value=old,
                        new_value=new,
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
    # Fecha de la foto: un export no puede contener una WO creada después de generarlo,
    # así que el máximo 'WO Created Ts' del fichero la acota bien. Si el fichero no
    # trae esa columna, se cae a la fecha de carga, que al menos preserva el orden.
    source.as_of = max_created or dt.datetime.now()
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


def resolve_identities(session: Session) -> dict:
    """
    Da identidad de eMaint a las observaciones de exports que no traían Url Emaint.

    Se emparejan por planta + inicio + equipo + tipo de incidencia, deliberadamente
    **sin** la descripción: el texto es justo lo que cambia cuando alguien reescribe o
    reutiliza la WO, así que meterlo en el emparejamiento anularía el propósito.

    Sólo se acepta el enlace cuando ese grupo apunta a un único GUID. Si hay varios, el
    emparejamiento es ambiguo —varias WOs distintas en el mismo minuto y equipo— y se
    deja la clave compuesta: preferimos no enlazar a enlazar mal.
    """
    # Sólo columnas y un UPDATE por lotes: cargar decenas de miles de objetos ORM y
    # dejar que el flush los recorra uno a uno tardaba minutos.
    # Dos mapas, y el orden importa:
    #   1. clave compuesta exacta -> GUID. Es el enlace seguro: si la descripción no
    #      cambió, la fila vieja y la nueva son la misma WO sin discusión.
    #   2. clave laxa (sin descripción) -> GUID. Recupera los casos en que el texto se
    #      reescribió, que son justo los que rompían el pipeline.
    # Sin el primero, una WO con guid en un export y sin guid en otro se partía en dos
    # identidades y el total subía en vez de bajar.
    porclave: dict[str, set[str]] = {}
    grupos: dict[tuple, set[str]] = {}
    for natural, plant, start, equipment, incident, guid in session.execute(
        select(
            WoObservation.natural_key,
            WoObservation.plant_norm,
            WoObservation.start_ts,
            WoObservation.equipment,
            WoObservation.incident_type,
            WoObservation.wo_guid,
        ).where(WoObservation.wo_guid.isnot(None))
    ):
        porclave.setdefault(natural, set()).add(guid)
        grupos.setdefault((plant, start, equipment, incident), set()).add(guid)

    exactos = {k: next(iter(v)) for k, v in porclave.items() if len(v) == 1}
    unicos = {k: next(iter(v)) for k, v in grupos.items() if len(v) == 1}
    ambiguos = sum(1 for v in grupos.values() if len(v) > 1)

    porguid: dict[str, list[int]] = {}
    sinenlace: list[int] = []
    for obs_id, plant, start, equipment, incident, natural, identity in session.execute(
        select(
            WoObservation.id,
            WoObservation.plant_norm,
            WoObservation.start_ts,
            WoObservation.equipment,
            WoObservation.incident_type,
            WoObservation.natural_key,
            WoObservation.identity,
        ).where(WoObservation.wo_guid.is_(None))
    ):
        guid = exactos.get(natural) or unicos.get((plant, start, equipment, incident))
        if guid is not None:
            if identity != guid:
                porguid.setdefault(guid, []).append(obs_id)
        elif identity != natural:
            sinenlace.append(obs_id)

    enlazadas = sum(len(v) for v in porguid.values())
    for guid, ids in porguid.items():
        session.execute(
            update(WoObservation).where(WoObservation.id.in_(ids)).values(identity=guid)
        )
    for chunk in range(0, len(sinenlace), 500):
        ids = sinenlace[chunk : chunk + 500]
        session.execute(
            update(WoObservation)
            .where(WoObservation.id.in_(ids))
            .values(identity=WoObservation.natural_key)
        )
    session.flush()
    log.info(
        "identidades resueltas: %d enlazadas por GUID, %d grupos ambiguos sin enlazar",
        enlazadas, ambiguos,
    )
    return {"linked": enlazadas, "ambiguous": ambiguos}


def rebuild_changes(session: Session) -> int:
    """
    Recalcula el log de cambios desde el historial completo, por identidad.

    Se borra y se reconstruye igual que el scope, y por el mismo motivo: la detección
    que hace ingest_csv sólo ve las identidades tal como estaban en ese momento, y las
    de los exports sin Url Emaint no se resuelven hasta resolve_identities(). Hecho
    aquí, un cambio se detecta aunque la identidad se haya reconstruido después.
    """
    session.execute(delete(AttributionChange))

    historial: dict[str, list] = {}
    for obs_id, identity, plant, start, equipment, user, cause, file_id, as_of in session.execute(
        select(
            WoObservation.id,
            WoObservation.identity,
            WoObservation.plant_raw,
            WoObservation.start_ts,
            WoObservation.equipment,
            WoObservation.cmms_user,
            WoObservation.cause,
            WoObservation.source_file_id,
            SourceFile.as_of,
        )
        .join(SourceFile, SourceFile.id == WoObservation.source_file_id)
        .order_by(SourceFile.as_of.asc().nullsfirst(), WoObservation.source_file_id.asc())
    ):
        historial.setdefault(identity, []).append(
            (plant, start, equipment, user, cause, file_id)
        )

    total = 0
    for identity, versiones in historial.items():
        for antes, ahora in zip(versiones, versiones[1:]):
            for campo, viejo, nuevo in (
                ("cmms_user", antes[3], ahora[3]),
                ("cause", antes[4], ahora[4]),
            ):
                if (viejo or "") == (nuevo or ""):
                    continue
                session.add(
                    AttributionChange(
                        natural_key=identity,
                        plant_raw=ahora[0],
                        start_ts=ahora[1],
                        equipment=ahora[2],
                        field=campo,
                        old_value=viejo,
                        new_value=nuevo,
                        from_file_id=antes[5],
                        to_file_id=ahora[5],
                    )
                )
                total += 1
    session.flush()
    log.info("log de cambios reconstruido: %d cambios de atribución o causa", total)
    return total


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

    # La regla de causa se lee de la tabla, no del código: ampliarla a todos los
    # países es un cambio de datos y un rebuild, sin tocar Python ni redeployar.
    failure_only = {
        r.value
        for r in session.scalars(
            select(ScopeRule).where(ScopeRule.kind == "cause_failure_only")
        )
    }
    if not failure_only:
        failure_only = set(sc.CAUSE_FAILURE_ONLY_DEFAULT)
        log.warning("sin reglas cause_failure_only en la tabla; usando el valor por defecto")
    log.info("cause_failure_only aplicado a: %s", ", ".join(sorted(failure_only)) or "ninguno")

    identidades = resolve_identities(session)
    cambios = rebuild_changes(session)

    session.execute(delete(WoScoped))

    # Ventana, fecha y *causas presentes* de cada foto.
    #
    # Las causas son imprescindibles para no acusar de borrado lo que sólo es un
    # filtro. Algunos exports vienen ya filtrados a Cause=Failure: si uno de ellos no
    # trae una WO de mantenimiento, eso no prueba nada — nunca la habría traído. Sin
    # esta comprobación salían 2956 "desaparecidas" de las que 2680 eran simplemente
    # no-Failure ausentes de un export filtrado. Una foto sólo es testigo de la
    # ausencia de una WO si contiene al menos una fila con esa misma causa.
    # Primera causa observada de cada incidencia, en orden de foto. Sirve para avisar
    # de las reclasificaciones posteriores.
    first_cause: dict[str, str | None] = {}
    for nk, cause in session.execute(
        select(WoObservation.identity, WoObservation.cause)
        .join(SourceFile, SourceFile.id == WoObservation.source_file_id)
        .order_by(SourceFile.as_of.asc().nullsfirst(), WoObservation.source_file_id.asc())
    ):
        first_cause.setdefault(nk, cause)

    causes_by_file: dict[int, set[str | None]] = {}
    for file_id, cause in session.execute(
        select(WoObservation.source_file_id, WoObservation.cause).distinct()
    ):
        causes_by_file.setdefault(file_id, set()).add(cause)

    snapshots = [
        (f.id, f.as_of, f.period_start, f.period_end, causes_by_file.get(f.id, set()))
        for f in session.scalars(select(SourceFile).order_by(SourceFile.id))
        if f.as_of is not None
    ]

    latest = _latest_observation_subquery()

    kept = 0
    vanished_count = 0
    reasons: dict[str, int] = {}

    for obs, obs_as_of in session.execute(
        select(WoObservation, SourceFile.as_of)
        .join(
            latest,
            (WoObservation.identity == latest.c.nk)
            & (WoObservation.source_file_id == latest.c.max_file),
        )
        .join(SourceFile, SourceFile.id == WoObservation.source_file_id)
    ):
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
            cause_failure_only=failure_only,
            is_mcc=obs.is_mcc,
        )

        day = obs.start_ts.date()

        # ¿Desaparecida? Existe una foto posterior que (a) cubre su fecha y (b) sí
        # trae WOs de su misma causa —o sea, que la habría incluido— y aun así no la
        # tiene. La WO se conserva de todas formas: un problema de ingesta o un borrado
        # en eMaint no debe reescribir un porcentaje ya publicado. La marca sirve para
        # revisarla, no para descontarla.
        vanished = any(
            as_of is not None
            and obs_as_of is not None
            and as_of > obs_as_of
            and period_start is not None
            and period_end is not None
            and period_start <= day <= period_end
            and obs.cause in causes
            for _fid, as_of, period_start, period_end, causes in snapshots
        )
        if vanished:
            vanished_count += 1

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
                cause=obs.cause,
                identity=obs.identity,
                cause_first=first_cause.get(obs.identity, obs.cause),
                cause_reclassified=(
                    first_cause.get(obs.identity, obs.cause) or ""
                ) != (obs.cause or ""),
                act_hrs=obs.act_hrs,
                resolution_hrs=obs.resolution_hrs,
                completion_hrs=obs.completion_hrs,
                validation_hrs=obs.validation_hrs,
                total_time_hrs=obs.total_time_hrs,
                wo_url=obs.wo_url,
                failure_cause=obs.failure_cause,
                in_scope=reason is None,
                excluded_reason=reason,
                vanished=vanished,
                last_seen_as_of=obs_as_of,
            )
        )
        if reason is None:
            kept += 1
        else:
            reasons[reason] = reasons.get(reason, 0) + 1

    session.flush()
    log.info(
        "scope reconstruido: %d en scope, %d fuera, %d conservadas tras desaparecer del origen",
        kept, sum(reasons.values()), vanished_count,
    )
    return {
        "in_scope": kept,
        "excluded": sum(reasons.values()),
        "excluded_by_reason": dict(sorted(reasons.items(), key=lambda kv: -kv[1])),
        "vanished": vanished_count,
        "changes": cambios,
        "identities_linked": identidades["linked"],
        "identities_ambiguous": identidades["ambiguous"],
    }
