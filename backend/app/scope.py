"""
Reglas de scope del detection rate, como funciones puras y testeables.

El detection rate sólo tiene sentido si el denominador es "incidencias que el MCC
podía haber detectado". Cada regla aquí quita del denominador algo que el MCC no
podía ver, o que no es comparable. Sin ellas el número castiga al MCC por cosas
fuera de su alcance.

Orden de evaluación pensado para que el motivo de exclusión sea el más informativo:
primero lo estructural (planta no onboardada), luego lo temporal, luego el tipo de
incidencia y por último la visibilidad del dispositivo concreto.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
from dataclasses import dataclass

# Equipos con telemetría en SCADA: el MCC puede verlos.
EQUIPMENT_IN = {
    "plant",
    "inverter",
    "meter",
    "power transformer",
    "ring main unit",
    "rmu",
    "switchgear",
    "generator",
    "weather station",
    "ppc",
}

# Equipos que el MCC no monitoriza estructuralmente. No es un fallo de vigilancia:
# no hay señal que vigilar.
EQUIPMENT_OUT = {
    "scada",
    "substation",
    "tracker",
    "pv string",
    "dc combiner box",
    "pv module",
    "electrical cabling",
    "yaw system",
    "converter",
}

INCIDENT_TYPES = {"Production Loss": "P", "Communication Loss": "C"}

# Países donde sólo cuentan las incidencias por fallo. Las causas externas
# (curtailment, red, meteorología) y el trabajo planificado (mantenimiento
# correctivo/preventivo, revamping) no son incidencias que el MCC pueda detectar.
#
# Es el valor por defecto: la lista real vive en la tabla `scope_rule`, así que se
# puede ampliar o reducir sin tocar código y recalcular el histórico con
# `rebuild_scope()`. Ver `cause_failure_only` en /api/scope/rules.
CAUSE_FAILURE_ONLY_DEFAULT = frozenset({"Spain", "Portugal", "Chile"})
CAUSE_FAILURE_ONLY = CAUSE_FAILURE_ONLY_DEFAULT

# Japón es shadowing, no operación del MCC.
COUNTRIES_OUT = {"Japan"}

# Valores de la matriz N3C que significan "sin visibilidad real".
NOT_VISIBLE = {
    "",
    "na",
    "n/a",
    "none",
    "null",
    "false",
    "no monitored",
    "no maps",
    "no map",
    "no monitc",
    "no comms",
    "pending",
}

# Qué columna de la matriz gobierna cada equipo.
EQUIPMENT_DEVICE = {
    "plant": "sst",
    "switchgear": "sst",
    "generator": "sst",
    "inverter": "inv",
    "meter": "poi",
    "power transformer": "poi",
    "ring main unit": "poi",
    "rmu": "poi",
    "weather station": "wst",
    "ppc": "ppc",
}

MONTHS_ES = {
    "ene": "Jan", "feb": "Feb", "mar": "Mar", "abr": "Apr",
    "may": "May", "jun": "Jun", "jul": "Jul", "ago": "Aug",
    "sep": "Sep", "oct": "Oct", "nov": "Nov", "dic": "Dec",
}


def normalize(value: object) -> str:
    """Minúsculas, sin acentos, espacios colapsados. Para cruzar nombres de planta."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFD", str(value))
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", text.lower().strip())


def parse_ts(value: object) -> dt.datetime | None:
    """
    Parsea el formato del export ('27 jul 2026, 16:20'), con meses en español.
    Devuelve None en lugar de lanzar: una fila con fecha ilegible se descarta,
    no rompe la carga entera.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return None
    for es, en in MONTHS_ES.items():
        text = text.replace(es, en)
    for fmt in ("%d %b %Y, %H:%M", "%d %b %Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def natural_key(
    plant: str,
    start_ts: dt.datetime,
    equipment: str,
    incident_type: str,
    description: object = "",
) -> str:
    """
    Identidad de una work order, estable entre exports.

    Incluye la descripción y esto NO es opcional: sin ella la clave colapsa WOs que
    son distintas. En el export real hay 380 grupos con dos WOs de O&M en la misma
    planta, mismo minuto de inicio y mismo tipo de equipo, pero de inversores
    distintos ("Inverter 8.3.2" frente a "Inverter 12.2.3"). Sin la descripción se
    perderían más de 2.000 filas.

    Deliberadamente NO incluye cmms_user: es justo el campo que cambia entre exports
    y que queremos detectar como reatribución. Se verificó que las 9 reatribuciones
    del 27-jul tienen descripción idéntica, así que son la misma WO reasignada.

    El ideal sería un identificador de WO, pero `SX WO Number` viene vacío en el
    100% de los casos — es la deuda de trazabilidad documentada del proyecto.
    """
    code = INCIDENT_TYPES.get(str(incident_type).strip(), "?")
    desc = normalize(description)
    # La descripción se hashea para acotar la longitud de la clave sin perder
    # capacidad de distinguir.
    digest = hashlib.sha1(desc.encode("utf-8")).hexdigest()[:12] if desc else "nodesc"
    return "|".join(
        [normalize(plant), start_ts.strftime("%Y-%m-%dT%H:%M"), normalize(equipment), code, digest]
    )


def device_visible(value: object) -> bool:
    return str(value if value is not None else "").strip().lower() not in NOT_VISIBLE


def inverter_visible(pct: object) -> bool:
    """INV viene como porcentaje monitorizado. 0 o vacío = sin visibilidad."""
    try:
        return float(pct) > 0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class PlantScope:
    """Lo que necesitamos saber de una planta para decidir el scope."""

    name: str
    country: str | None = None
    portfolio: str | None = None
    completed_since: dt.date | None = None
    vis_sst: str | None = None
    vis_poi: str | None = None
    vis_ppc: str | None = None
    vis_wst: str | None = None
    vis_inv_pct: float | None = None
    excluded_from: dt.date | None = None
    excluded_reason: str | None = None

    def visible_for(self, equipment: str) -> bool | None:
        """True/False si hay regla; None si el equipo no tiene dispositivo asociado."""
        device = EQUIPMENT_DEVICE.get(normalize(equipment))
        if device is None:
            return None
        if device == "inv":
            return inverter_visible(self.vis_inv_pct)
        return device_visible(getattr(self, f"vis_{device}"))


# Motivos de exclusión. Cadenas estables: la API las expone y el frontend las agrupa.
R_PLANT_UNKNOWN = "planta sin onboarding N3C"
R_NO_ONBOARDING_DATE = "planta sin fecha de onboarding completado"
R_BEFORE_ONBOARDING = "anterior a la fecha de onboarding"
R_PLANT_EXCLUDED = "planta excluida (sin telemetría)"
R_COUNTRY_OUT = "país fuera de scope"
R_EQUIPMENT_OUT = "equipo sin telemetría"
R_EQUIPMENT_UNKNOWN = "equipo no catalogado"
R_INCIDENT_TYPE = "tipo de incidencia fuera de scope"
R_CAUSE = "causa distinta de Failure"
R_NOT_VISIBLE = "dispositivo sin visibilidad en SCADA"
R_NO_LIFECYCLE = "incidencia con ciclo de vida 0"
R_NO_START = "sin fecha de inicio válida"


def evaluate(
    *,
    plant: PlantScope | None,
    start_ts: dt.datetime | None,
    equipment: object,
    incident_type: object,
    cause: object,
    country: object,
    incident_lifecycle_hrs: object = None,
    cause_failure_only: frozenset[str] | set[str] | None = None,
) -> str | None:
    """
    Devuelve None si la incidencia entra en el cálculo, o el motivo de exclusión.

    Se devuelve el motivo en vez de un booleano para poder auditar cualquier cifra:
    "¿por qué no cuentan estas 13 WOs?" tiene respuesta sin volver al CSV.
    """
    if start_ts is None:
        return R_NO_START

    try:
        if incident_lifecycle_hrs is not None and float(incident_lifecycle_hrs) == 0:
            return R_NO_LIFECYCLE
    except (TypeError, ValueError):
        pass

    if str(country).strip() in COUNTRIES_OUT:
        return R_COUNTRY_OUT

    if plant is None:
        return R_PLANT_UNKNOWN

    # Estar en la matriz de visibilidad no basta: el scope exige onboarding N3C
    # completado. Sin fecha de alta no sabemos desde cuándo el MCC veía la planta,
    # así que no puede entrar en el denominador.
    if plant.completed_since is None:
        return R_NO_ONBOARDING_DATE

    if start_ts.date() < plant.completed_since:
        return R_BEFORE_ONBOARDING

    if plant.excluded_from and start_ts.date() >= plant.excluded_from:
        return R_PLANT_EXCLUDED

    equipment_norm = normalize(equipment)
    if equipment_norm in EQUIPMENT_OUT:
        return R_EQUIPMENT_OUT
    if equipment_norm not in EQUIPMENT_IN:
        return R_EQUIPMENT_UNKNOWN

    if str(incident_type).strip() not in INCIDENT_TYPES:
        return R_INCIDENT_TYPE

    effective_country = plant.country or str(country).strip()
    failure_only = CAUSE_FAILURE_ONLY if cause_failure_only is None else cause_failure_only
    if effective_country in failure_only and str(cause).strip() != "Failure":
        return R_CAUSE

    if plant.visible_for(equipment) is False:
        return R_NOT_VISIBLE

    return None


def canonical_contractor(raw: object, aliases: list[tuple[str, str]]) -> str:
    """
    Aplica los alias regex al valor de Om Contract.

    El CSV trae el mismo grupo escrito de varias formas (RES / RES Energy /
    RES Group / RES Energy Services S.A.S.). Sin agrupar, el mayor contratista
    del scope queda partido en cuatro y no aparece en ningún ranking.
    """
    text = "" if raw is None else str(raw).strip()
    if not text or text.lower() in {"nan", "none"}:
        return "Sin asignar"
    lowered = text.lower()
    for pattern, canonical in aliases:
        if re.match(pattern, lowered):
            return canonical
    return text


def detection_hours(start_ts: dt.datetime | None, created_ts: dt.datetime | None) -> float | None:
    """
    Horas desde el inicio de la incidencia hasta la creación de la WO.

    Un valor negativo significa WO creada antes del inicio: inconsistencia del
    sistema origen, no un tiempo de detección. Se devuelve None para que no
    contamine las medianas.
    """
    if start_ts is None or created_ts is None:
        return None
    hours = (created_ts - start_ts).total_seconds() / 3600.0
    return None if hours < 0 else round(hours, 2)


def iso_week_start(day: dt.date) -> dt.date:
    """Lunes de la semana ISO. El dashboard agrupa por semana empezando en lunes."""
    return day - dt.timedelta(days=day.weekday())
