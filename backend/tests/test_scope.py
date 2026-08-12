"""
Tests de las reglas de scope.

Cada test corresponde a una regla que descubrimos analizando los CSV reales. Si
alguno falla, el detection rate cambia — por eso están aquí y no como comentarios.
"""
from __future__ import annotations

import datetime as dt

import pytest

from app import scope as sc

ONBOARDED = dt.date(2026, 1, 1)


def plant(**overrides) -> sc.PlantScope:
    defaults = dict(
        name="Planta Test",
        country="Italy",
        portfolio="Toro 1",
        completed_since=ONBOARDED,
        vis_sst="FULL",
        vis_poi="OK",
        vis_ppc="OK",
        vis_wst="OK",
        vis_inv_pct=100.0,
    )
    defaults.update(overrides)
    return sc.PlantScope(**defaults)


def evaluate(**overrides) -> str | None:
    args = dict(
        plant=plant(),
        start_ts=dt.datetime(2026, 5, 10, 9, 0),
        equipment="Inverter",
        incident_type="Production Loss",
        cause="Failure",
        country="Italy",
        incident_lifecycle_hrs=5.0,
        # Por defecto se evalúa una WO del contratista: es la única a la que se le
        # aplican las reglas de detectabilidad. Los tests que van del MCC lo dicen.
        is_mcc=False,
    )
    args.update(overrides)
    return sc.evaluate(**args)


class TestNormalizacion:
    def test_quita_acentos_y_normaliza(self):
        assert sc.normalize("Alfamén") == "alfamen"
        assert sc.normalize("  Peñaflor  II ") == "penaflor ii"

    def test_none_es_cadena_vacia(self):
        assert sc.normalize(None) == ""


class TestParseoFechas:
    def test_meses_en_espanol(self):
        assert sc.parse_ts("27 jul 2026, 16:20") == dt.datetime(2026, 7, 27, 16, 20)
        assert sc.parse_ts("3 abr 2026, 8:05") == dt.datetime(2026, 4, 3, 8, 5)

    def test_valor_ilegible_devuelve_none_sin_lanzar(self):
        # Una fila con fecha corrupta se descarta; no debe romper la carga entera.
        for value in (None, "", "nan", "no es una fecha"):
            assert sc.parse_ts(value) is None


class TestClaveNatural:
    def test_no_incluye_quien_detecto(self):
        """
        La clave debe ser estable aunque cambie el CMMS User: es justo el campo que
        vimos cambiar entre exports y que queremos detectar como reatribución.
        """
        args = ("Ottobiano 1", dt.datetime(2026, 7, 27, 16, 20), "Inverter", "Production Loss")
        assert sc.natural_key(*args) == sc.natural_key(*args)

    def test_distingue_incidencias_distintas(self):
        base = ("Vada 1", dt.datetime(2026, 7, 27, 16, 20), "Inverter", "Production Loss")
        otra_hora = ("Vada 1", dt.datetime(2026, 7, 27, 16, 21), "Inverter", "Production Loss")
        otro_equipo = ("Vada 1", dt.datetime(2026, 7, 27, 16, 20), "Plant", "Production Loss")
        assert sc.natural_key(*base) != sc.natural_key(*otra_hora)
        assert sc.natural_key(*base) != sc.natural_key(*otro_equipo)

    def test_normaliza_la_planta(self):
        a = sc.natural_key("Alfamén", dt.datetime(2026, 5, 1, 0, 0), "Inverter", "Production Loss")
        b = sc.natural_key("ALFAMEN ", dt.datetime(2026, 5, 1, 0, 0), "inverter", "Production Loss")
        assert a == b


class TestVisibilidad:
    @pytest.mark.parametrize("value", ["NA", "", None, "PENDING", "NO COMMS", "no monitored"])
    def test_valores_que_significan_sin_visibilidad(self, value):
        assert sc.device_visible(value) is False

    @pytest.mark.parametrize("value", ["OK", "FULL", "PARTIALLY", "PARTIALLY - Only MTR", "true"])
    def test_valores_que_significan_visible(self, value):
        assert sc.device_visible(value) is True

    def test_inverter_al_cero_por_ciento_no_es_visible(self):
        # Caso real: Albaida y Alfamén tienen INV=0 y sus WOs de inverter no cuentan.
        assert sc.inverter_visible(0) is False
        assert sc.inverter_visible(None) is False
        assert sc.inverter_visible("NA") is False
        assert sc.inverter_visible(100) is True
        assert sc.inverter_visible(12.3) is True

    def test_equipo_usa_el_dispositivo_correcto(self):
        p = plant(vis_sst="NA", vis_inv_pct=100.0)
        # Plant depende de SST, que está NA -> fuera.
        assert p.visible_for("Plant") is False
        # Inverter depende de INV, que está al 100% -> dentro.
        assert p.visible_for("Inverter") is True

    def test_meter_y_transformador_dependen_de_poi(self):
        p = plant(vis_poi="NA")
        assert p.visible_for("Meter") is False
        assert p.visible_for("Power Transformer") is False
        assert p.visible_for("RMU") is False


class TestReglasDeScope:
    def test_caso_valido_entra(self):
        assert evaluate() is None

    def test_planta_desconocida(self):
        assert evaluate(plant=None) == sc.R_PLANT_UNKNOWN

    def test_anterior_al_onboarding(self):
        """Sin visibilidad previa, contar esas WOs penaliza al MCC injustamente."""
        assert evaluate(start_ts=dt.datetime(2025, 12, 31, 12, 0)) == sc.R_BEFORE_ONBOARDING

    def test_planta_excluida_desde_una_fecha(self):
        # Castelnau: pierde SCADA el 3-jul-2026.
        p = plant(excluded_from=dt.date(2026, 7, 3), excluded_reason="sin SCADA")
        assert evaluate(plant=p, start_ts=dt.datetime(2026, 7, 4, 9, 0)) == sc.R_PLANT_EXCLUDED
        # Antes de esa fecha sí cuenta.
        assert evaluate(plant=p, start_ts=dt.datetime(2026, 7, 2, 9, 0)) is None

    def test_japon_fuera(self):
        assert evaluate(country="Japan") == sc.R_COUNTRY_OUT

    @pytest.mark.parametrize("equipment", ["Tracker", "PV String", "DC Combiner Box", "SCADA"])
    def test_equipos_sin_telemetria(self, equipment):
        assert evaluate(equipment=equipment) == sc.R_EQUIPMENT_OUT

    def test_equipo_no_catalogado(self):
        assert evaluate(equipment="Cosa Rara") == sc.R_EQUIPMENT_UNKNOWN

    def test_tipo_de_incidencia_fuera(self):
        assert evaluate(incident_type="Preventive Maintenance") == sc.R_INCIDENT_TYPE

    def test_cause_failure_aplica_a_todos_los_paises(self):
        """
        Por defecto sólo cuentan las averías, en cualquier país. Mantenimiento y
        revamping son trabajo planificado: no son órdenes que apliquen al MCC.
        """
        for country in ("Spain", "Italy", "France", "Poland"):
            p = plant(country=country)
            assert evaluate(plant=p, country=country, cause="Failure") is None
            for cause in ("Curtailment", "Corrective Maintenance",
                          "Preventive Maintenance", "Revamping/Repowering", "Other"):
                assert evaluate(plant=p, country=country, cause=cause) == sc.R_CAUSE, (country, cause)

    def test_la_regla_se_puede_restringir_a_paises(self):
        """Con una lista explícita, sólo esos países filtran por causa."""
        italy = plant(country="Italy")
        only_es = {"Spain"}
        assert evaluate(plant=italy, country="Italy", cause="Corrective Maintenance",
                        cause_failure_only=only_es) is None
        spain = plant(country="Spain")
        assert evaluate(plant=spain, country="Spain", cause="Corrective Maintenance",
                        cause_failure_only=only_es) == sc.R_CAUSE

    def test_comodin_cubre_paises_no_listados(self):
        assert sc.cause_filter_applies("Narnia", {sc.ALL_COUNTRIES}) is True
        assert sc.cause_filter_applies("Narnia", {"Spain"}) is False

    def test_ciclo_de_vida_cero(self):
        assert evaluate(incident_lifecycle_hrs=0) == sc.R_NO_LIFECYCLE

    def test_sin_fecha_de_inicio(self):
        assert evaluate(start_ts=None) == sc.R_NO_START

    def test_dispositivo_sin_visibilidad(self):
        assert evaluate(plant=plant(vis_inv_pct=0)) == sc.R_NOT_VISIBLE

    def test_precedencia_lo_estructural_manda(self):
        """
        Con varias razones a la vez, gana la más informativa: si la planta no está
        onboardada da igual el equipo.
        """
        assert evaluate(plant=None, equipment="Tracker") == sc.R_PLANT_UNKNOWN


class TestLasDeteccionesDelMccNoSeDescartan:
    """
    Una WO abierta por el MCC no sale del cálculo por una regla de detectabilidad.

    Las reglas de onboarding, visibilidad, telemetría y causa existen para no penalizar
    al MCC por lo que no podía ver. Aplicárselas a una detección que el MCC sí hizo
    sería negar un hecho: la detectó. Sólo las reglas de universo —país, planta, fecha
    legible— siguen valiendo para todos.
    """

    @pytest.mark.parametrize(
        "caso",
        [
            {"plant": None},                                   # planta desconocida -> universo
            {"start_ts": None},                                # sin fecha -> universo
            {"country": "Japan"},                              # país fuera -> universo
        ],
    )
    def test_las_reglas_de_universo_siguen_aplicando(self, caso):
        assert evaluate(is_mcc=True, **caso) is not None

    @pytest.mark.parametrize(
        "caso",
        [
            {"start_ts": dt.datetime(2025, 12, 31, 12, 0)},    # antes del onboarding
            {"plant": plant(completed_since=None)},            # sin fecha de onboarding
            {"equipment": "Tracker"},                          # equipo sin telemetría
            {"equipment": "Cosa Rara"},                        # equipo no catalogado
            {"incident_type": "Preventive Maintenance"},       # tipo fuera
            {"cause": "Corrective Maintenance"},               # causa distinta de Failure
            {"plant": plant(vis_inv_pct=0)},                   # sin visibilidad SCADA
            {"incident_lifecycle_hrs": 0},                     # ciclo de vida cero
        ],
    )
    def test_la_detectabilidad_no_descarta_al_mcc(self, caso):
        # La misma incidencia, abierta por el contratista, sí queda fuera.
        assert evaluate(is_mcc=False, **caso) is not None
        assert evaluate(is_mcc=True, **caso) is None

    def test_planta_excluida_tampoco_descarta_al_mcc(self):
        """Castelnau pierde SCADA, pero si el MCC abrió la WO es que la detectó."""
        p = plant(excluded_from=dt.date(2026, 7, 3), excluded_reason="sin SCADA")
        fecha = dt.datetime(2026, 7, 4, 9, 0)
        assert evaluate(plant=p, start_ts=fecha, is_mcc=False) == sc.R_PLANT_EXCLUDED
        assert evaluate(plant=p, start_ts=fecha, is_mcc=True) is None


class TestContratistas:
    ALIASES = [
        (r"^res\b", "RES"),
        (r"^stern energy", "Stern Energy"),
        (r"^belectric", "Belectric"),
        (r"^(sonnedix france operations|sfo)\b", "Sonnedix France Operations"),
    ]

    @pytest.mark.parametrize(
        "raw",
        ["RES", "RES Energy", "RES Group", "RES Energy Services S.A.S."],
    )
    def test_agrupa_variantes_de_res(self, raw):
        # Sin agrupar, el mayor contratista del scope queda partido en cuatro.
        assert sc.canonical_contractor(raw, self.ALIASES) == "RES"

    def test_agrupa_stern_con_sufijos_y_filiales(self):
        for raw in ("Stern Energy", "Stern Energy spa", "Stern Energy UK"):
            assert sc.canonical_contractor(raw, self.ALIASES) == "Stern Energy"

    def test_contratos_conjuntos_van_al_operador_principal(self):
        assert (
            sc.canonical_contractor("Sonnedix France Operations / EDF RE", self.ALIASES)
            == "Sonnedix France Operations"
        )
        assert sc.canonical_contractor("SFO ; Soleco ; S2I", self.ALIASES) == "Sonnedix France Operations"

    def test_no_toca_lo_que_no_coincide(self):
        assert sc.canonical_contractor("Eiffage", self.ALIASES) == "Eiffage"

    def test_vacio_es_sin_asignar(self):
        for raw in (None, "", "nan"):
            assert sc.canonical_contractor(raw, self.ALIASES) == "Sin asignar"

    def test_res_no_captura_palabras_que_empiezan_igual(self):
        # \b evita que "Resolux" se agrupe como RES.
        assert sc.canonical_contractor("Resolux", self.ALIASES) == "Resolux"


class TestTiempoDeDeteccion:
    def test_calcula_horas(self):
        start = dt.datetime(2026, 5, 1, 8, 0)
        created = dt.datetime(2026, 5, 1, 10, 30)
        assert sc.detection_hours(start, created) == 2.5

    def test_negativo_se_descarta(self):
        """
        WO creada antes del inicio: inconsistencia del origen (67 casos reales).
        Devolver None evita que contamine las medianas.
        """
        start = dt.datetime(2026, 5, 1, 10, 0)
        created = dt.datetime(2026, 5, 1, 8, 0)
        assert sc.detection_hours(start, created) is None

    def test_falta_alguna_fecha(self):
        assert sc.detection_hours(None, dt.datetime(2026, 5, 1)) is None
        assert sc.detection_hours(dt.datetime(2026, 5, 1), None) is None


class TestSemanaIso:
    def test_lunes_es_inicio_de_semana(self):
        # 2026-07-29 es miércoles; su semana empieza el lunes 27.
        assert sc.iso_week_start(dt.date(2026, 7, 29)) == dt.date(2026, 7, 27)

    def test_lunes_se_queda_igual(self):
        assert sc.iso_week_start(dt.date(2026, 7, 27)) == dt.date(2026, 7, 27)

    def test_domingo_va_a_la_semana_anterior(self):
        assert sc.iso_week_start(dt.date(2026, 8, 2)) == dt.date(2026, 7, 27)
