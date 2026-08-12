"""
Agregaciones de KPIs.

Replican exactamente la semántica del dashboard HTML, con dos cuidados que no son
obvios y que dan números distintos si se hacen mal:

  * El rate por importe se calcula sólo sobre las WOs que tienen Revenue Loss
    informado. Las que no lo tienen NO cuentan como cero: quedan fuera del
    numerador y del denominador. Tratarlas como cero hunde el rate artificialmente.

  * El tiempo de detección usa MEDIANA, no media. La distribución tiene colas muy
    largas (incidencias registradas semanas después) y la media no representa el
    comportamiento habitual.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from .models import WoScoped

# Dimensiones agrupables, mapeadas a columna. Lista blanca: evita inyección por
# nombre de columna y da un error claro si se pide una dimensión inexistente.
DIMENSIONS = {
    "country": WoScoped.country,
    "portfolio": WoScoped.portfolio,
    "contractor": WoScoped.contractor,
    "equipment": WoScoped.equipment,
    "plant": WoScoped.plant,
    "month": WoScoped.month,
    "week": WoScoped.iso_week,
}


@dataclass
class Filters:
    """Filtros del dashboard. Todos opcionales y combinables."""

    date_from: str | None = None          # 'YYYY-MM'
    date_to: str | None = None            # 'YYYY-MM'
    country: str | None = None
    portfolio: str | None = None
    contractor: str | None = None
    equipment: str | None = None
    incident_type: str | None = None      # 'P' | 'C'
    status: str | None = None             # 'open' | 'closed'

    def clauses(self) -> list:
        conditions = [WoScoped.in_scope.is_(True)]
        if self.date_from:
            conditions.append(WoScoped.month >= self.date_from)
        if self.date_to:
            conditions.append(WoScoped.month <= self.date_to)
        if self.country:
            conditions.append(WoScoped.country == self.country)
        if self.portfolio:
            conditions.append(WoScoped.portfolio == self.portfolio)
        if self.contractor:
            conditions.append(WoScoped.contractor == self.contractor)
        if self.equipment:
            conditions.append(WoScoped.equipment == self.equipment)
        if self.incident_type:
            conditions.append(WoScoped.incident_type == self.incident_type)
        if self.status == "open":
            conditions.append(WoScoped.ongoing.is_(True))
        elif self.status == "closed":
            conditions.append(WoScoped.ongoing.is_(False))
        return conditions


def base_query(filters: Filters) -> Select:
    return select(WoScoped).where(and_(*filters.clauses()))


def _rate(mcc: float, ext: float) -> float | None:
    total = mcc + ext
    return round(mcc / total * 100, 1) if total else None


@dataclass
class Metrics:
    """Los tres ejes de medida sobre el mismo conjunto de WOs."""

    wo_mcc: int = 0
    wo_ext: int = 0
    rev_mcc: float = 0.0
    rev_ext: float = 0.0
    rev_n: int = 0
    det_mcc: list[float] = field(default_factory=list)
    det_ext: list[float] = field(default_factory=list)

    def add(self, row: WoScoped) -> None:
        if row.is_mcc:
            self.wo_mcc += 1
        else:
            self.wo_ext += 1
        if row.revenue_loss is not None:
            self.rev_n += 1
            if row.is_mcc:
                self.rev_mcc += row.revenue_loss
            else:
                self.rev_ext += row.revenue_loss
        if row.detection_hours is not None:
            (self.det_mcc if row.is_mcc else self.det_ext).append(row.detection_hours)

    @property
    def total(self) -> int:
        return self.wo_mcc + self.wo_ext

    def as_dict(self) -> dict:
        det_all = self.det_mcc + self.det_ext
        return {
            "wos": self.total,
            "wo_mcc": self.wo_mcc,
            "wo_ext": self.wo_ext,
            "rate_wo": _rate(self.wo_mcc, self.wo_ext),
            "revenue_total": round(self.rev_mcc + self.rev_ext, 2),
            "revenue_mcc": round(self.rev_mcc, 2),
            "revenue_ext": round(self.rev_ext, 2),
            "rate_revenue": _rate(self.rev_mcc, self.rev_ext),
            # Cobertura del dato económico: por debajo del 80% el rate por importe
            # es indicativo, no concluyente.
            "revenue_coverage": round(self.rev_n / self.total * 100, 1) if self.total else 0.0,
            "detection_median_mcc": round(statistics.median(self.det_mcc), 2) if self.det_mcc else None,
            "detection_median_ext": round(statistics.median(self.det_ext), 2) if self.det_ext else None,
            "detection_median_all": round(statistics.median(det_all), 2) if det_all else None,
            "detection_coverage": round(len(det_all) / self.total * 100, 1) if self.total else 0.0,
        }


def summary(session: Session, filters: Filters) -> dict:
    metrics = Metrics()
    plants: set[str] = set()
    for row in session.scalars(base_query(filters)):
        metrics.add(row)
        plants.add(row.plant)
    result = metrics.as_dict()
    result["plants"] = len(plants)
    return result


def by_dimension(session: Session, filters: Filters, dimension: str) -> list[dict]:
    """Agrupa por una dimensión. Ordenado por volumen, con 'Sin asignar' al final."""
    if dimension not in DIMENSIONS:
        raise ValueError(
            f"dimensión '{dimension}' no válida; disponibles: {', '.join(sorted(DIMENSIONS))}"
        )
    column = DIMENSIONS[dimension]
    buckets: dict[str, Metrics] = {}
    plants: dict[str, set[str]] = {}
    for row in session.scalars(base_query(filters)):
        key = getattr(row, column.key)
        buckets.setdefault(key, Metrics()).add(row)
        plants.setdefault(key, set()).add(row.plant)

    out = []
    for key, metrics in buckets.items():
        entry = {"key": key, **metrics.as_dict(), "plants": len(plants[key])}
        out.append(entry)
    out.sort(key=lambda e: (e["key"] == "Sin asignar", -e["wos"]))
    return out


def timeseries(session: Session, filters: Filters, granularity: str = "week") -> list[dict]:
    """Serie temporal por semana (lunes ISO) o por mes."""
    if granularity not in {"week", "month"}:
        raise ValueError("granularity debe ser 'week' o 'month'")
    dimension = "week" if granularity == "week" else "month"
    rows = by_dimension(session, filters, dimension)
    rows.sort(key=lambda e: e["key"])
    return [{"period": r.pop("key"), **r} for r in rows]


def missed_wos(session: Session, filters: Filters, limit: int = 500, offset: int = 0) -> dict:
    """
    WOs abiertas por el contratista sin detección previa del MCC.

    Es la lista accionable del análisis: qué se escapó, dónde y con qué descripción.
    """
    query = base_query(filters).where(WoScoped.is_mcc.is_(False)).order_by(
        WoScoped.start_date.desc(), WoScoped.plant
    )
    total = len(session.scalars(query).all())
    rows = session.scalars(query.limit(limit).offset(offset)).all()
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "plant": r.plant,
                "country": r.country,
                "portfolio": r.portfolio,
                "contractor": r.contractor,
                "date": r.start_date.isoformat(),
                "equipment": r.equipment,
                "incident_type": r.incident_type,
                "ongoing": r.ongoing,
                "description": r.description,
                "revenue_loss": r.revenue_loss,
                "capacity_affected": r.capacity_affected,
                "detection_hours": r.detection_hours,
                "failure_cause": r.failure_cause,
                # Enlace directo a la WO en eMaint: hace la lista accionable.
                "wo_url": r.wo_url,
            }
            for r in rows
        ],
    }


def revenue_concentration(session: Session, filters: Filters, top: int = 5) -> dict:
    """
    Cuánto del importe total se concentra en las incidencias más caras.

    El rate por euros es muy sensible a los extremos: en el periodo analizado 20
    incidencias concentran el 47% del importe. Un único evento grande cayendo de un
    lado u otro mueve el rate económico más que meses de trabajo, así que conviene
    avisar antes de que alguien lea una variación como cambio de rendimiento.
    """
    rows = [
        r for r in session.scalars(base_query(filters))
        if r.revenue_loss is not None and r.revenue_loss > 0
    ]
    total = sum(r.revenue_loss for r in rows)
    if not total:
        return {"total": 0.0, "n": 0, "top_share": 0.0, "top1_share": 0.0, "items": []}
    rows.sort(key=lambda r: r.revenue_loss, reverse=True)
    head = rows[:top]
    return {
        "total": round(total, 2),
        "n": len(rows),
        "top_share": round(sum(r.revenue_loss for r in head) / total * 100, 1),
        "top1_share": round(rows[0].revenue_loss / total * 100, 1),
        "items": [
            {
                "plant": r.plant,
                "country": r.country,
                "date": r.start_date.isoformat(),
                "equipment": r.equipment,
                "is_mcc": r.is_mcc,
                "ongoing": r.ongoing,
                "revenue_loss": round(r.revenue_loss, 2),
                "share": round(r.revenue_loss / total * 100, 1),
            }
            for r in head
        ],
    }


def status_split(session: Session, filters: Filters) -> dict:
    """
    Reparto abierto/cerrado ignorando el filtro de estado.

    Las WOs abiertas son el 17% del recuento pero cerca del 60% del importe, porque su
    pérdida sigue acumulando. Mezclarlas compara importes provisionales con definitivos,
    así que el frontend necesita este reparto para avisar.
    """
    unfiltered = Filters(**{**filters.__dict__, "status": None})
    open_rows: list[WoScoped] = []
    closed_rows: list[WoScoped] = []
    for row in session.scalars(base_query(unfiltered)):
        (open_rows if row.ongoing else closed_rows).append(row)

    def revenue(rows: list[WoScoped]) -> float:
        return sum(r.revenue_loss or 0 for r in rows)

    total_rev = revenue(open_rows) + revenue(closed_rows)
    total_n = len(open_rows) + len(closed_rows)
    return {
        "n_open": len(open_rows),
        "n_closed": len(closed_rows),
        "revenue_open": round(revenue(open_rows), 2),
        "revenue_closed": round(revenue(closed_rows), 2),
        "share_open_count": round(len(open_rows) / total_n * 100, 1) if total_n else 0.0,
        "share_open_revenue": round(revenue(open_rows) / total_rev * 100, 1) if total_rev else 0.0,
        "open": Metrics_from(open_rows),
        "closed": Metrics_from(closed_rows),
    }


def Metrics_from(rows: list[WoScoped]) -> dict:
    metrics = Metrics()
    for row in rows:
        metrics.add(row)
    return metrics.as_dict()


def excluded_breakdown(session: Session, filters: Filters) -> list[dict]:
    """
    Por qué queda fuera cada WO descartada.

    Permite responder "¿por qué el denominador es este?" sin volver a los CSV, que
    es la pregunta que siempre aparece al presentar el número.
    """
    conditions = [c for c in filters.clauses() if c is not WoScoped.in_scope.is_(True)]
    query = select(WoScoped).where(WoScoped.in_scope.is_(False))
    if filters.date_from:
        query = query.where(WoScoped.month >= filters.date_from)
    if filters.date_to:
        query = query.where(WoScoped.month <= filters.date_to)
    if filters.country:
        query = query.where(WoScoped.country == filters.country)

    counts: dict[str, int] = {}
    for row in session.scalars(query):
        counts[row.excluded_reason or "sin motivo"] = (
            counts.get(row.excluded_reason or "sin motivo", 0) + 1
        )
    return [
        {"reason": reason, "wos": count}
        for reason, count in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def meta(session: Session) -> dict:
    """Catálogos para poblar los filtros del frontend."""
    rows = session.scalars(select(WoScoped).where(WoScoped.in_scope.is_(True))).all()
    countries, portfolios, contractors, equipment, months = set(), set(), set(), set(), set()
    pf_countries: dict[str, set[str]] = {}
    ctr_countries: dict[str, set[str]] = {}
    ctr_variants: dict[str, set[str]] = {}
    for r in rows:
        countries.add(r.country)
        portfolios.add(r.portfolio)
        contractors.add(r.contractor)
        equipment.add(r.equipment)
        months.add(r.month)
        pf_countries.setdefault(r.portfolio, set()).add(r.country)
        ctr_countries.setdefault(r.contractor, set()).add(r.country)
        if r.contractor_raw:
            ctr_variants.setdefault(r.contractor, set()).add(r.contractor_raw)

    def ordered(values: set[str]) -> list[str]:
        return sorted(values, key=lambda v: (v == "Sin asignar", v))

    return {
        "countries": sorted(countries),
        "months": sorted(months),
        "portfolios": ordered(portfolios),
        "contractors": ordered(contractors),
        "equipment": sorted(equipment),
        "pf_countries": {k: sorted(v) for k, v in pf_countries.items()},
        "ctr_countries": {k: sorted(v) for k, v in ctr_countries.items()},
        # Sólo los grupos que agrupan más de un nombre original: es lo que hay que
        # poder justificar de la normalización.
        "ctr_variants": {k: sorted(v) for k, v in ctr_variants.items() if len(v) > 1},
        "total_in_scope": len(rows),
    }
