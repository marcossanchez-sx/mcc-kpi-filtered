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

# Sólo cuentan las incidencias con Cause = Failure, en todos los países.
#
# El resto no son incidencias que el MCC pueda detectar: el mantenimiento correctivo y
# preventivo y el revamping son trabajo planificado, y las causas externas (curtailment,
# red, meteorología) no son averías. Contarlas metería en el denominador órdenes que no
# aplican al MCC.
#
# `*` significa "todos los países". La lista vive en la tabla `scope_rule`, así que se
# puede restringir a países concretos sin tocar código, recalculando con `rebuild_scope()`.
ALL_COUNTRIES = "*"
CAUSE_FAILURE_ONLY_DEFAULT = frozenset({ALL_COUNTRIES})
CAUSE_FAILURE_ONLY = CAUSE_FAILURE_ONLY_DEFAULT


def cause_filter_applies(country: str, failure_only: frozenset[str] | set[str]) -> bool:
    """La regla aplica si está el comodín o el país concreto."""
    return ALL_COUNTRIES in failure_only or country in failure_only

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
    is_mcc: bool = False,
) -> str | None:
    """
    Devuelve None si la incidencia entra en el cálculo, o el motivo de exclusión.

    Se devuelve el motivo en vez de un booleano para poder auditar cualquier cifra:
    "¿por qué no cuentan estas 13 WOs?" tiene respuesta sin volver al CSV.

    Las reglas están en dos familias, y la diferencia es deliberada:

    **Reglas de universo** — se aplican a todos. Definen qué entra en el KPI: país
    dentro de la operación del MCC, fecha de inicio legible, planta identificable.

    **Reglas de detectabilidad** — se aplican SÓLO a las WOs del contratista:
    onboarding N3C, visibilidad SCADA, equipo con telemetría, causa, tipo de
    incidencia, ciclo de vida. Existen para no penalizar al MCC por lo que no podía
    ver, así que no tienen ningún sentido aplicadas a una detección que el MCC **sí**
    hizo. Excluir una WO del MCC porque "la planta no está onboardada" sería negar una
    detección que ocurrió de verdad.

    Consecuencia a tener presente: el ratio deja de ser simétrico. No es "cuota sobre
    un universo comparable", es "detecciones del MCC frente a incidencias que abrió el
    contratista y que el MCC podría haber cazado". Es la definición correcta del KPI,
    pero hay que etiquetarla así para que nadie la lea como una proporción simple.
    """
    if start_ts is None:
        return R_NO_START

    if str(country).strip() in COUNTRIES_OUT:
        return R_COUNTRY_OUT

    if plant is None:
        return R_PLANT_UNKNOWN

    # Desde aquí, todo son reglas de detectabilidad: una detección del MCC no se
    # descarta por ellas.
    if is_mcc:
        return None

    try:
        if incident_lifecycle_hrs is not None and float(incident_lifecycle_hrs) == 0:
            return R_NO_LIFECYCLE
    except (TypeError, ValueError):
        pass

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
    if cause_filter_applies(effective_country, failure_only) and str(cause).strip() != "Failure":
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


WO_GUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


def wo_guid(url: object) -> str | None:
    """
    GUID de la WO a partir de la URL de eMaint.

    Se normaliza a minúsculas porque el mismo identificador aparece en mayúsculas en
    algunos campos (c_opsincidenturl) y en minúsculas en otros.
    """
    match = WO_GUID_RE.search(str(url or ""))
    return match.group(1).lower() if match else None


# ── Sospecha de causa mal clasificada en las WOs del contratista ──────────────────
#
# El sesgo va en el sentido incómodo: si una avería que encontró el contratista se
# etiqueta como Corrective Maintenance, sale del denominador y el detection rate del
# MCC **sube**. O sea que esta mala clasificación oculta fallos de detección nuestros.
# Por eso se mide y se publica como banda de sensibilidad, no se esconde.
#
# Dos niveles, y la diferencia de solidez es deliberada:
#
#   "contradiccion"  el propio registro se contradice: trae un Failure Cause concreto
#                    (AC BREAKER TRIP, IGBT OVERHEATING…) y a la vez una causa que no
#                    es Failure. No es una opinión, es un dato incoherente.
#   "rearme"         el texto dice que se rearmó, reseteó o reinició el equipo. No se
#                    rearma lo que se paró de forma planificada.
#   "texto"          la descripción habla de avería y no menciona trabajo planificado.
#                    Es una heurística mía, con falsos positivos, y NUNCA mueve la
#                    cifra publicada: sólo amplía la banda de sensibilidad.

FAILURE_CAUSE_VACIO = frozenset(
    {"", "n/a", "na", "none", "-", "nan", "sin dato", "no aplica"}
)

# Sin \b final a propósito: "THERMOGRAPHY" y "TERMOGRAFIAS" tienen que entrar por su
# raíz. Con el límite de palabra se colaban como sospechosas paradas por termografía
# anual, que son trabajo planificado legítimo.
TRABAJO_PLANIFICADO = re.compile(
    r"(preventiv|thermograph|termograf|cleaning|limpieza|megagem|semestral|semester|"
    r"annual|anual|scheduled|programad|revision|revisi[oó]n|inspecc|inspection|"
    r"manutenzione programmata|panel replacement|sustituci[oó]n de panel|"
    r"revamping|repowering|commissioning)",
    re.I,
)

# Rearmar, resetear o reiniciar un equipo es la firma de una avería: no se rearma algo
# que se paró de forma planificada. Va aparte porque es la señal más específica del
# grupo, y aparece sobre todo en el campo Action Taken de eMaint — que hoy NO viene en
# el export de WOs. Pedir esa columna es la mejora de datos más rentable que queda.
REARME = re.compile(
    r"(rearm|re-?arm|resete|\breset\b|reinici|riavvi|red[eé]marr|restart|"
    r"reactiv|vuelta a servicio|back in service|returned to service|"
    r"alarm(a)? (cleared|borrada|reseteada)|borrado de alarma|puesta en marcha)",
    re.I,
)

LENGUAJE_DE_AVERIA = re.compile(
    r"(fault|failure|tripp?ed|\btrip\b|stopped|shutdown|\berror\b|alarm|"
    r"without production|not in production|no production|underperform|low production|"
    r"fallo|aver[ií]a|parada|parado|disparo|guasto|fermo|non in produzione|"
    r"sin producci[oó]n|blocco|\bko\b)",
    re.I,
)


# La mala clasificación de causa corta en los dos sentidos, y hay que medir los dos:
#
#   avería etiquetada como mantenimiento  -> sale del denominador -> SUBE nuestro rate
#   mantenimiento etiquetado como Failure -> entra en el denominador -> BAJA nuestro rate
#
# Publicar sólo el primero sería quedarse con la mitad que nos incomoda y la otra que
# nos favorece sin contar. La banda de sensibilidad va a dos lados.


def failure_cause_informado(failure_cause: object) -> bool:
    """True sólo si trae un motivo de fallo real. 'N/A' es relleno, no información."""
    return str(failure_cause or "").strip().lower() not in FAILURE_CAUSE_VACIO


def sospecha_de_averia(
    *,
    cause: object,
    failure_cause: object,
    description: object,
    is_mcc: bool,
    action_taken: object = None,
) -> str | None:
    """
    Nivel de sospecha de que una WO del contratista sea en realidad una avería.

    Sólo se evalúan las del contratista: son las únicas a las que la regla de causa les
    quita sitio en el denominador.
    """
    if is_mcc or str(cause).strip() == "Failure":
        return None
    if failure_cause_informado(failure_cause):
        return "contradiccion"
    text = " ".join(str(x or "") for x in (description, action_taken))
    if REARME.search(text) and not TRABAJO_PLANIFICADO.search(text):
        # Rearmar es tan específico que sube de nivel: se rearma lo que ha fallado.
        return "rearme"
    if LENGUAJE_DE_AVERIA.search(text) and not TRABAJO_PLANIFICADO.search(text):
        return "texto"
    return None


def sospecha_de_planificado(
    *,
    cause: object,
    failure_cause: object,
    description: object,
    is_mcc: bool,
    action_taken: object = None,
) -> str | None:
    """
    Señal inversa: WO del contratista etiquetada como Failure que parece planificada.

    Estas sí entran en el denominador, así que **bajan** el detection rate del MCC. Si
    sólo se midiera la sospecha en el otro sentido, se estaría publicando el sesgo
    incómodo y callando el que nos favorece.

    Se exige que el texto diga trabajo planificado y que NO haya lenguaje de avería ni
    un Failure Cause concreto: con cualquiera de los dos, la etiqueta Failure se
    sostiene y no hay nada que sospechar.
    """
    if is_mcc or str(cause).strip() != "Failure":
        return None
    if failure_cause_informado(failure_cause):
        return None
    text = " ".join(str(x or "") for x in (description, action_taken))
    if TRABAJO_PLANIFICADO.search(text) and not LENGUAJE_DE_AVERIA.search(text):
        return "planificado"
    return None


def sin_evidencia_de_causa(
    *,
    cause: object,
    failure_cause: object,
    description: object,
    is_mcc: bool,
    action_taken: object = None,
) -> str | None:
    """
    WO del contratista etiquetada como Failure sin nada que lo respalde ni lo contradiga.

    Ni Failure Cause, ni lenguaje de avería, ni de trabajo planificado: el texto no dice
    nada. **No se excluye ni se reclasifica** — la etiqueta del origen se respeta. Es
    sólo una lista de revisión, para decidir a mano si aplica.

    Se separa de "planificado" a propósito: allí hay indicio de que la etiqueta está
    mal; aquí no hay indicio de nada, y no tener evidencia no es evidencia en contra.
    """
    if is_mcc or str(cause).strip() != "Failure":
        return None
    if failure_cause_informado(failure_cause):
        return None  # su propio Failure Cause ya confirma la etiqueta
    text = " ".join(str(x or "") for x in (description, action_taken))
    if LENGUAJE_DE_AVERIA.search(text) or TRABAJO_PLANIFICADO.search(text):
        return None
    return "sin_evidencia"
