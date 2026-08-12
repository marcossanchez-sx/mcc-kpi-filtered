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

from sqlalchemy import Select, and_, func, select, true
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


# Tramos de la cadena de tiempos tal como los da el export. El orden es el del
# ciclo real de la incidencia; la cobertura de cada uno varía muchísimo.
TIME_FIELDS: dict[str, str] = {
    "detection_hours": "Detección (inicio → WO creada)",
    "act_hrs": "Actuación",
    "resolution_hrs": "Resolución",
    "completion_hrs": "Cierre",
    "validation_hrs": "Validación",
    "total_time_hrs": "Total",
}


def _percentile(values: list[float], pct: float) -> float:
    """Percentil por interpolación lineal; con un solo valor devuelve ese valor."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * pct / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


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

    # Qué universo se mide:
    #   'in'  -> sólo lo que aplica al MCC (por defecto; es el detection rate oficial)
    #   'out' -> sólo lo descartado, para ver qué se queda fuera y por qué
    #   'all' -> todo el export en bruto, sin reglas de scope
    # Sirve para enseñar la diferencia entre lo que aplica y lo que no sin tener que
    # recargar nada: el descarte ya está persistido con su motivo.
    scope: str = "in"

    def clauses(self, *, with_scope: bool = True) -> list:
        conditions: list = []
        if with_scope:
            if self.scope == "in":
                conditions.append(WoScoped.in_scope.is_(True))
            elif self.scope == "out":
                conditions.append(WoScoped.in_scope.is_(False))
            elif self.scope != "all":
                raise ValueError("scope debe ser 'in', 'out' o 'all'")
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
    # true() como base: con scope='all' y sin filtros la lista queda vacía y and_()
    # sin argumentos está deprecado.
    return select(WoScoped).where(and_(true(), *filters.clauses()))


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
    # Cadena de tiempos del propio export, separada por actor. Se guarda la lista
    # completa para poder dar mediana y cobertura: Resolution viene informada en el
    # 2% de las filas y una mediana sin cobertura al lado engaña.
    times: dict = field(default_factory=dict)

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
        for name in TIME_FIELDS:
            value = getattr(row, name, None)
            if value is not None and value >= 0:
                slot = self.times.setdefault(name, {"mcc": [], "ext": []})
                slot["mcc" if row.is_mcc else "ext"].append(value)

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
            "times": self._times_as_dict(),
        }

    def _times_as_dict(self) -> dict:
        """Mediana MCC / O&M y cobertura de cada tramo de la cadena de tiempos."""
        out: dict[str, dict] = {}
        for name, label in TIME_FIELDS.items():
            slot = self.times.get(name) or {"mcc": [], "ext": []}
            mcc, ext = slot["mcc"], slot["ext"]
            both = mcc + ext
            out[name] = {
                "label": label,
                "median_mcc": round(statistics.median(mcc), 2) if mcc else None,
                "median_ext": round(statistics.median(ext), 2) if ext else None,
                "median_all": round(statistics.median(both), 2) if both else None,
                "p90_all": round(_percentile(both, 90), 2) if both else None,
                "n": len(both),
                "coverage": round(len(both) / self.total * 100, 1) if self.total else 0.0,
            }
        return out


def summary(session: Session, filters: Filters) -> dict:
    metrics = Metrics()
    plants: set[str] = set()
    for row in session.scalars(base_query(filters)):
        metrics.add(row)
        plants.add(row.plant)
    result = metrics.as_dict()
    result["plants"] = len(plants)
    # Aviso que viaja con la cifra: cuántas de las detecciones del MCC llevan una causa
    # que el contratista cambió después.
    # Banda de sensibilidad por la posible mala clasificación de causa en el
    # contratista. Va en el resumen para que la cifra nunca viaje sola.
    result["suspected_om_failures"] = session.scalar(
        select(func.count())
        .select_from(WoScoped)
        .where(
            WoScoped.in_scope.is_(False),
            WoScoped.excluded_reason == "causa distinta de Failure",
            WoScoped.misclass_signal.in_(("contradiccion", "rearme")),
            *filters.clauses(with_scope=False),
        )
    ) or 0
    result["suspected_om_planned"] = sum(
        1 for row in session.scalars(base_query(filters))
        if row.misclass_signal == "planificado"
    )
    # Las dos direcciones del sesgo, para que la cifra no viaje nunca sola.
    result["rate_wo_si_contaran"] = _rate(
        metrics.wo_mcc, metrics.wo_ext + result["suspected_om_failures"]
    )
    result["rate_wo_sin_planificado"] = _rate(
        metrics.wo_mcc, max(metrics.wo_ext - result["suspected_om_planned"], 0)
    )
    result["mcc_reclassified"] = sum(
        1 for row in session.scalars(base_query(filters))
        if row.is_mcc and (row.cause or "") != "Failure"
    )
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
    Por qué queda fuera cada WO descartada, con lo que la caracteriza.

    Permite responder "¿por qué el denominador es este?" sin volver a los CSV, que es
    la pregunta que siempre aparece al presentar el número. Cada motivo lleva sus
    causas y equipos más frecuentes, para ver de un golpe si el descarte tiene sentido
    (mantenimiento planificado) o si es una carencia nuestra (planta sin onboardar).
    """
    conditions = filters.clauses(with_scope=False)
    query = select(WoScoped).where(WoScoped.in_scope.is_(False), *conditions)

    groups: dict[str, list[WoScoped]] = {}
    for row in session.scalars(query):
        groups.setdefault(row.excluded_reason or "sin motivo", []).append(row)

    total = sum(len(rows) for rows in groups.values())
    out = []
    for reason, rows in groups.items():
        out.append(
            {
                "reason": reason,
                "wos": len(rows),
                "share": round(len(rows) / total * 100, 1) if total else 0.0,
                "top_causes": _top(rows, "cause"),
                "top_equipment": _top(rows, "equipment"),
                "top_countries": _top(rows, "country"),
                # Importe en juego. No es pérdida "recuperable": muchas de estas WOs
                # no son detectables por diseño. Sirve para dimensionar, no para exigir.
                "revenue": round(sum(r.revenue_loss or 0 for r in rows), 2),
            }
        )
    out.sort(key=lambda e: -e["wos"])
    return out


def _top(rows: list[WoScoped], attribute: str, limit: int = 3) -> list[dict]:
    counts: dict[str, int] = {}
    for row in rows:
        value = getattr(row, attribute, None) or "sin dato"
        counts[value] = counts.get(value, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    return [{"key": k, "wos": v} for k, v in ordered]


def suspected_failures(session: Session, filters: Filters, limit: int = 500) -> dict:
    """
    WOs del contratista excluidas por causa que probablemente sean averías.

    Importa el sentido del sesgo: excluir una avería que encontró el contratista quita
    un fallo de detección del denominador, así que **sube** el rate del MCC. Esta
    función existe para que ese sesgo se publique en lugar de beneficiarnos en silencio.

    Devuelve la banda de sensibilidad: qué pasaría con el rate si esas WOs contaran.
    La cifra oficial no cambia —reclasificar es decisión del equipo, no del pipeline—
    pero se presenta siempre con la banda al lado.
    """
    base = Filters(**{**filters.__dict__, "scope": "in"})
    dentro = list(session.scalars(base_query(base)))
    mcc = sum(1 for r in dentro if r.is_mcc)

    conditions = filters.clauses(with_scope=False)
    fuera = list(
        session.scalars(
            select(WoScoped).where(
                WoScoped.in_scope.is_(False),
                WoScoped.excluded_reason == "causa distinta de Failure",
                WoScoped.is_mcc.is_(False),
                *conditions,
            )
        )
    )
    contradiccion = [r for r in fuera if r.misclass_signal == "contradiccion"]
    rearme = [r for r in fuera if r.misclass_signal == "rearme"]
    texto = [r for r in fuera if r.misclass_signal == "texto"]

    # Señal inversa: dentro del scope, WOs del contratista etiquetadas como Failure que
    # parecen trabajo planificado. Estas hinchan el denominador y bajan nuestro rate.
    planificado = [r for r in dentro if r.misclass_signal == "planificado"]
    # Etiquetadas Failure sin nada que lo respalde ni lo contradiga. No se tocan: son
    # lista de revisión. No tener evidencia no es evidencia en contra.
    sin_evidencia = [r for r in dentro if r.misclass_signal == "sin_evidencia"]

    def rate_con(entran: int = 0, salen: int = 0) -> float | None:
        total = len(dentro) + entran - salen
        return round(mcc / total * 100, 1) if total else None

    return {
        "excluded_by_cause": len(fuera),
        "contradiccion": len(contradiccion),
        "rearme": len(rearme),
        "texto": len(texto),
        "planificado": len(planificado),
        "sin_evidencia": len(sin_evidencia),
        "by_cause": [
            {"key": k, "wos": v}
            for k, v in sorted(_count(contradiccion, "cause").items(), key=lambda kv: -kv[1])
        ],
        "by_contractor": [
            {"key": k, "wos": v}
            for k, v in sorted(_count(contradiccion, "contractor").items(), key=lambda kv: -kv[1])
        ][:10],
        "by_failure_cause": [
            {"key": k, "wos": v}
            for k, v in sorted(_count(contradiccion, "failure_cause").items(), key=lambda kv: -kv[1])
        ][:10],
        # Banda a dos lados. El extremo bajo entra averías que hoy no cuentan; el alto
        # saca trabajo planificado que hoy sí cuenta. La cifra oficial no se mueve: la
        # reclasificación la decide el equipo, no el pipeline.
        "sensitivity": {
            "rate_actual": rate_con(),
            "solo_contradiccion": rate_con(entran=len(contradiccion)),
            "contradiccion_y_rearme": rate_con(entran=len(contradiccion) + len(rearme)),
            "todas_las_senales": rate_con(
                entran=len(contradiccion) + len(rearme) + len(texto),
                salen=len(planificado),
            ),
            "extremo_alto_sin_planificado": rate_con(salen=len(planificado)),
            "extremo_bajo_todas_no_failure": rate_con(entran=len(fuera)),
        },
        "note": (
            "Excluir una avería que encontró el contratista quita un fallo de detección "
            "del denominador y sube el rate del MCC. La banda muestra hasta dónde: el "
            "nivel 'contradiccion' es dato incoherente del propio registro (Failure "
            "Cause concreto con causa que no es Failure); 'texto' es heurística sobre la "
            "descripción, con falsos positivos, y no debe usarse para mover la cifra. "
            "La señal 'planificado' va en sentido contrario: WOs etiquetadas como "
            "Failure que parecen trabajo programado, y que hoy nos bajan el rate. "
            "'sin_evidencia' no mueve nada: son Failure sin Failure Cause y con un "
            "texto que no dice nada, y se listan sólo para revisarlas a mano."
        ),
        "items": [
            {
                "plant": r.plant, "country": r.country, "date": r.start_date.isoformat(),
                "equipment": r.equipment, "cause": r.cause, "failure_cause": r.failure_cause,
                "contractor": r.contractor, "signal": r.misclass_signal,
                "description": r.description, "wo_url": r.wo_url,
            }
            for r in sorted(
                contradiccion + rearme + texto + planificado + sin_evidencia,
                key=lambda r: r.start_date,
                reverse=True,
            )[:limit]
        ],
    }


def reclassified_wos(session: Session, filters: Filters, limit: int = 500) -> dict:
    """
    Detecciones del MCC cuya causa ya no es Failure.

    El MCC abre la WO al detectar la incidencia y después el contratista reclasifica la
    causa. La detección es válida y la WO cuenta —para eso están las reglas separadas—
    pero sin la nota el dato se lee al revés: parece que el MCC abrió un mantenimiento.

    Se distinguen dos situaciones, porque la evidencia no es la misma:
      con_traza  -> hemos visto el cambio entre dos exports (Failure -> otra cosa)
      sin_traza  -> ya la conocimos con la causa nueva; el cambio es anterior a
                    nuestro primer export, así que se deduce, no se demuestra
    """
    rows = list(
        session.scalars(
            select(WoScoped)
            .where(WoScoped.is_mcc.is_(True), WoScoped.cause != "Failure", *filters.clauses())
            .order_by(WoScoped.start_date.desc())
        )
    )
    con_traza = [r for r in rows if r.cause_reclassified]
    total_mcc = session.scalar(
        select(func.count())
        .select_from(WoScoped)
        .where(WoScoped.is_mcc.is_(True), *filters.clauses())
    ) or 0
    return {
        "total": len(rows),
        "con_traza": len(con_traza),
        "sin_traza": len(rows) - len(con_traza),
        "mcc_total": total_mcc,
        "share_mcc": round(len(rows) / total_mcc * 100, 1) if total_mcc else 0.0,
        "by_cause": [
            {"key": k, "wos": v}
            for k, v in sorted(_count(rows, "cause").items(), key=lambda kv: -kv[1])
        ],
        "note": (
            "Son detecciones del MCC. Cuentan en el rate: la reclasificación posterior "
            "de la causa no deshace la detección. Se marcan para que no se lean como "
            "trabajo planificado abierto por el MCC."
        ),
        "items": [
            {
                "plant": r.plant, "country": r.country, "date": r.start_date.isoformat(),
                "equipment": r.equipment, "cause": r.cause, "cause_first": r.cause_first,
                "reclassified": r.cause_reclassified, "contractor": r.contractor,
                "description": r.description, "wo_url": r.wo_url,
            }
            for r in rows[:limit]
        ],
    }


def vanished_wos(session: Session, filters: Filters, limit: int = 500) -> dict:
    """
    WOs que aparecieron en un export y ya no vienen en los posteriores.

    Se conservan en el cálculo a propósito: si se descontaran, un fallo de ingesta o un
    borrado en eMaint reescribiría porcentajes ya publicados. Esta lista existe para
    revisarlas una a una y decidir, no para ajustar el número por detrás.
    """
    query = select(WoScoped).where(WoScoped.vanished.is_(True), *filters.clauses())
    rows = list(session.scalars(query.order_by(WoScoped.start_date.desc())))
    mcc = sum(1 for r in rows if r.is_mcc)
    return {
        "total": len(rows),
        "mcc": mcc,
        "ext": len(rows) - mcc,
        "still_open": sum(1 for r in rows if r.ongoing),
        "by_country": [{"key": k, "wos": v} for k, v in
                       sorted(_count(rows, "country").items(), key=lambda kv: -kv[1])],
        "items": [
            {
                "plant": r.plant, "country": r.country, "date": r.start_date.isoformat(),
                "equipment": r.equipment, "is_mcc": r.is_mcc, "ongoing": r.ongoing,
                "description": r.description, "wo_url": r.wo_url,
                "last_seen": r.last_seen_as_of.isoformat() if r.last_seen_as_of else None,
            }
            for r in rows[:limit]
        ],
    }


def _count(rows: list[WoScoped], attribute: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        key = getattr(row, attribute, None) or "sin dato"
        counts[key] = counts.get(key, 0) + 1
    return counts


def scope_comparison(session: Session, filters: Filters) -> dict:
    """
    Lo que aplica al MCC frente a lo que no, con los mismos filtros en ambos lados.

    Es la vista que contesta "¿y qué estáis dejando fuera?". Importante: el bloque
    `out` no tiene detection rate interpretable — el MCC no puede detectar un
    mantenimiento planificado ni una planta sin telemetría, así que ese porcentaje
    no mide desempeño y no se debe presentar como tal.
    """
    def measure(scope: str) -> dict:
        scoped = Filters(**{**filters.__dict__, "scope": scope})
        return summary(session, scoped)

    inside, outside = measure("in"), measure("out")
    total = inside["wos"] + outside["wos"]
    return {
        "in_scope": inside,
        "out_of_scope": outside,
        "all": measure("all"),
        "share_in_scope": round(inside["wos"] / total * 100, 1) if total else 0.0,
        "reasons": excluded_breakdown(session, filters),
        "note": (
            "El rate del bloque fuera de scope no mide desempeño: son WOs que el MCC "
            "no puede detectar (trabajo planificado, equipos sin telemetría, plantas "
            "sin onboardar). Está sólo para ver el tamaño y el motivo del descarte."
        ),
    }


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
