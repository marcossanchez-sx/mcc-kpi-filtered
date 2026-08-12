"""
Carga de datos de referencia: plantas, visibilidad, portfolios y alias.

Estas tablas son las que definen el scope. Se pueden recargar cuando cambie el
informe N3C o el mapeo de portfolios; después basta llamar a rebuild_scope() para
que el histórico completo se recalcule con las reglas nuevas.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import ContractorAlias, Plant, ScopeRule
from .scope import (
    CAUSE_FAILURE_ONLY_DEFAULT,
    COUNTRIES_OUT,
    EQUIPMENT_IN,
    EQUIPMENT_OUT,
    normalize,
)

log = logging.getLogger(__name__)


def _date(value: str | None) -> dt.date | None:
    if not value:
        return None
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _float(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip().replace(",", ".")
    if not text or text.lower() in {"nan", "none", "na"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def load_onboarding(session: Session, content: bytes) -> int:
    """
    N3C_Onboarding_Completed.csv (separado por ';'): nombre, país y fecha de alta.

    La fecha de alta importa mucho: sin ella contaríamos incidencias de antes de que
    el MCC tuviera visibilidad de la planta.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=";")
    count = 0
    for row in reader:
        name = (row.get("Plant Name") or "").strip()
        if not name:
            continue
        norm = normalize(name)
        plant = session.scalar(select(Plant).where(Plant.name_norm == norm))
        if plant is None:
            plant = Plant(name=name, name_norm=norm)
            session.add(plant)
        plant.name = name
        plant.country = (row.get("Country") or "").strip() or plant.country
        plant.completed_since = _date(row.get("Completed Since")) or plant.completed_since
        count += 1
    session.flush()
    log.info("onboarding cargado: %d plantas", count)
    return count


def load_visibility(session: Session, content: bytes) -> int:
    """
    Matriz de visibilidad por dispositivo (export del informe N3C de Smartsheet).

    Columnas: plant, asset_id, country, sst, poi, ppc, wst, pst, inv_pct,
    onboarding_status.
    """
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for row in reader:
        name = (row.get("plant") or "").strip()
        if not name:
            continue
        norm = normalize(name)
        plant = session.scalar(select(Plant).where(Plant.name_norm == norm))
        if plant is None:
            plant = Plant(name=name, name_norm=norm)
            session.add(plant)
        plant.asset_id = (row.get("asset_id") or "").strip() or plant.asset_id
        plant.country = (row.get("country") or "").strip() or plant.country
        plant.vis_sst = (row.get("sst") or "").strip() or None
        plant.vis_poi = (row.get("poi") or "").strip() or None
        plant.vis_ppc = (row.get("ppc") or "").strip() or None
        plant.vis_wst = (row.get("wst") or "").strip() or None
        plant.vis_pst = (row.get("pst") or "").strip() or None
        plant.vis_inv_pct = _float(row.get("inv_pct"))
        plant.onboarding_status = (row.get("onboarding_status") or "").strip() or None
        count += 1
    session.flush()
    log.info("visibilidad cargada: %d plantas", count)
    return count


def load_portfolios(session: Session, content: bytes) -> int:
    """plant, country, portfolio. El portfolio cruza países, no es subnivel del país."""
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for row in reader:
        name = (row.get("plant") or "").strip()
        if not name:
            continue
        norm = normalize(name)
        plant = session.scalar(select(Plant).where(Plant.name_norm == norm))
        if plant is None:
            plant = Plant(name=name, name_norm=norm)
            session.add(plant)
        plant.portfolio = (row.get("portfolio") or "").strip() or "Sin asignar"
        if not plant.country:
            plant.country = (row.get("country") or "").strip() or None
        count += 1
    session.flush()
    log.info("portfolios cargados: %d plantas", count)
    return count


def load_contractor_aliases(session: Session, content: bytes) -> int:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    count = 0
    for row in reader:
        pattern = (row.get("pattern") or "").strip()
        canonical = (row.get("canonical") or "").strip()
        if not pattern or not canonical:
            continue
        existing = session.scalar(select(ContractorAlias).where(ContractorAlias.pattern == pattern))
        if existing is None:
            session.add(ContractorAlias(pattern=pattern, canonical=canonical))
        else:
            existing.canonical = canonical
        count += 1
    session.flush()
    return count


def seed_scope_rules(session: Session) -> int:
    """Deja las reglas de scope visibles en base de datos, para poder auditarlas."""
    rules: list[tuple[str, str, str]] = []
    rules += [("equipment_in", e, "equipo con telemetría en SCADA") for e in sorted(EQUIPMENT_IN)]
    rules += [("equipment_out", e, "sin telemetría: no hay señal que vigilar") for e in sorted(EQUIPMENT_OUT)]
    rules += [("incident_type_in", t, "tipo de incidencia en scope") for t in ("Production Loss", "Communication Loss")]
    rules += [("country_out", c, "shadowing, no es operación del MCC") for c in sorted(COUNTRIES_OUT)]
    rules += [
        (
            "cause_failure_only",
            c,
            "sólo Cause=Failure; mantenimiento, revamping y causas externas no son "
            "incidencias que el MCC pueda detectar ('*' = todos los países)",
        )
        for c in sorted(CAUSE_FAILURE_ONLY_DEFAULT)
    ]

    count = 0
    for kind, value, note in rules:
        existing = session.scalar(
            select(ScopeRule).where(ScopeRule.kind == kind, ScopeRule.value == value)
        )
        if existing is None:
            session.add(ScopeRule(kind=kind, value=value, note=note))
            count += 1
    session.flush()
    return count


def apply_plant_exclusion(
    session: Session, *, plant_name: str, from_date: dt.date, reason: str
) -> bool:
    """
    Excluye una planta a partir de una fecha (caso Castelnau: pierde SCADA el
    2026-07-03). Sin telemetría no hay detección posible; contar esas WOs sería
    penalizar al MCC por algo que no puede ver.
    """
    plant = session.scalar(select(Plant).where(Plant.name_norm == normalize(plant_name)))
    if plant is None:
        return False
    plant.excluded_from = from_date
    plant.excluded_reason = reason
    session.flush()
    return True


def load_all_from_directory(session: Session, directory: Path) -> dict:
    """Carga todos los ficheros de referencia que encuentre en la carpeta."""
    results: dict[str, int | bool] = {}
    files = {
        "onboarding": ("N3C_Onboarding_Completed.csv", load_onboarding),
        "visibility": ("n3c_visibility.csv", load_visibility),
        "portfolios": ("plant_portfolio.csv", load_portfolios),
        "aliases": ("contractor_alias.csv", load_contractor_aliases),
    }
    for label, (filename, loader) in files.items():
        path = directory / filename
        if path.exists():
            results[label] = loader(session, path.read_bytes())
        else:
            results[label] = 0
            log.warning("no encontrado: %s", path)
    results["scope_rules"] = seed_scope_rules(session)
    return results
