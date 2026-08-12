"""
Tests de ingesta y API, sobre SQLite en memoria.

El caso central es el de la reatribución: cargar un export más reciente debe
sustituir la versión anterior de esa incidencia en vez de duplicarla, y el cambio
debe quedar auditado. Es exactamente el problema que tuvimos que resolver a mano
comparando los CSV del 28-jul y del 3-ago.
"""
from __future__ import annotations

import csv
import datetime as dt
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app import queries as q
from app import reference
from app.db import make_engine
from app.ingest import IngestError, ingest_csv, rebuild_scope
from app.models import AttributionChange, Base, WoScoped

COLUMNS = [
    "Country", "Asset", "Om Contract", "Start Ts Local", "WO Created Ts Local",
    "Ongoing", "CMMS User", "Equipment", "Incident Type", "Cause",
    "Capacity Affected", "Revenue Loss", "Incident Lifecycle (hrs)",
    "Description English", "Detection (hrs)", "Act (hrs)", "Resolution (hrs)",
    "Completion (hrs)", "Validation (hrs)", "Total time (hrs)",
]


def row(
    *,
    plant: str = "Vada 2",
    country: str = "Italy",
    contractor: str = "Stern Energy",
    start: str = "1 may 2026, 8:00",
    created: str = "1 may 2026, 9:00",
    ongoing: str = "false",
    user: str = "MCC",
    equipment: str = "Inverter",
    incident_type: str = "Production Loss",
    cause: str = "Failure",
    capacity: str = "0.3",
    revenue: str = "100",
    lifecycle: str = "5",
    description: str = "x",
    detection_hrs: str = "1",
    act_hrs: str = "",
    resolution_hrs: str = "",
    completion_hrs: str = "",
    validation_hrs: str = "",
    total_hrs: str = "10",
) -> list[str]:
    return [
        country, plant, contractor, start, created, ongoing, user,
        equipment, incident_type, cause, capacity, revenue, lifecycle, description,
        detection_hrs, act_hrs, resolution_hrs, completion_hrs, validation_hrs, total_hrs,
    ]


def csv_bytes(*rows: list[str]) -> bytes:
    """
    Construye un CSV como el export real.

    Importa que se genere con el módulo csv y no concatenando cadenas: los campos de
    fecha valen '1 may 2026, 8:00' — llevan coma dentro — y el fichero real los
    entrecomilla. Así el test ejercita el mismo parseo que producción.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows(rows)
    return buffer.getvalue().encode()


ONBOARDING = (
    "Plant Name;Country;Completed Since\n"
    "Ottobiano 1;Italy;2026-01-01\n"
    "Vada 2;Italy;2026-01-01\n"
    "Albaida;Spain;2026-01-01\n"
    "Castelnau;France;2026-01-01\n"
).encode()

# Casos reales: Ottobiano 1 tiene SST=NA (Plant fuera) y Albaida INV=0 (Inverter fuera).
VISIBILITY = (
    "plant,asset_id,country,sst,poi,ppc,wst,pst,inv_pct,onboarding_status\n"
    "Ottobiano 1,SX01,Italy,NA,OK,NA,OK,OK,100,Completed\n"
    "Vada 2,SX02,Italy,FULL,OK,OK,OK,OK,100,Completed\n"
    "Albaida,SX03,Spain,FULL,OK,OK,OK,OK,0,Completed\n"
    "Castelnau,SX04,France,FULL,OK,OK,OK,OK,100,Completed\n"
).encode()

PORTFOLIOS = (
    "plant,country,portfolio\n"
    "Ottobiano 1,Italy,Toro 1\n"
    "Vada 2,Italy,Toro 1\n"
    "Albaida,Spain,Albero\n"
    "Castelnau,France,Toro 1\n"
).encode()

ALIASES = ("pattern,canonical\n" "^res\\b,RES\n" "^stern energy,Stern Energy\n").encode()


@pytest.fixture()
def session():
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        reference.load_onboarding(s, ONBOARDING)
        reference.load_visibility(s, VISIBILITY)
        reference.load_portfolios(s, PORTFOLIOS)
        reference.load_contractor_aliases(s, ALIASES)
        reference.seed_scope_rules(s)
        s.commit()
        yield s


@pytest.fixture()
def client(session, monkeypatch):
    # Antes de importar main: evita que el arranque intente crear el engine de Postgres.
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")

    from app import db as db_module
    from app import main as main_module

    monkeypatch.setattr(main_module, "FRONTEND_DIR", main_module.Path("/nonexistent"))
    app = main_module.app
    app.dependency_overrides[db_module.get_session] = lambda: session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


class TestIngestaIdempotente:
    def test_mismo_fichero_no_se_procesa_dos_veces(self, session):
        data = csv_bytes(row())
        first = ingest_csv(session, content=data, filename="a.csv")
        second = ingest_csv(session, content=data, filename="a-copia.csv")
        assert first.already_loaded is False
        assert first.rows_inserted == 1
        # Mismo contenido con otro nombre: sigue siendo el mismo fichero.
        assert second.already_loaded is True
        assert second.rows_inserted == 0

    def test_filas_identicas_se_conservan_ambas(self, session):
        """
        Dos filas idénticas en todos los campos disponibles son dos WOs que no
        podemos distinguir (falta SX WO Number). Descartar una perdería trabajo real,
        así que se conservan las dos con un sufijo de orden.
        """
        result = ingest_csv(session, content=csv_bytes(row(), row()), filename="dup.csv")
        assert result.rows_inserted == 2
        assert result.rows_skipped == 0

    def test_wos_distintas_con_mismo_inicio_no_se_colapsan(self, session):
        """
        Caso real: 380 grupos del export tienen dos WOs de O&M en la misma planta y
        minuto, pero de inversores distintos. La descripción es lo único que las
        distingue; sin ella se perderían más de 2.000 filas.
        """
        result = ingest_csv(
            session,
            content=csv_bytes(
                row(description="Inverter 8.3.2 without production"),
                row(description="Inverter 12.2.3 without production"),
            ),
            filename="distintas.csv",
        )
        assert result.rows_inserted == 2
        rebuild_scope(session)
        assert len(session.scalars(select(WoScoped)).all()) == 2

    def test_columnas_obligatorias_ausentes(self, session):
        with pytest.raises(IngestError, match="faltan columnas"):
            ingest_csv(session, content=b"Country,Asset\nItaly,Vada 2\n", filename="mal.csv")

    def test_fila_con_fecha_ilegible_se_salta(self, session):
        data = csv_bytes(row(start="fecha mala"))
        result = ingest_csv(session, content=data, filename="fecha.csv")
        assert result.rows_inserted == 0
        assert result.rows_skipped == 1

    def test_bom_de_excel_no_rompe_las_cabeceras(self, session):
        data = b"\xef\xbb\xbf" + csv_bytes(row())
        assert ingest_csv(session, content=data, filename="bom.csv").rows_inserted == 1


class TestReatribucion:
    """El caso que motivó el diseño de snapshots."""

    OM = row(start="27 jul 2026, 16:20", created="27 jul 2026, 18:00", user="O&M Contractor")
    MCC = row(start="27 jul 2026, 16:20", created="27 jul 2026, 18:00", user="MCC")

    def test_el_export_mas_reciente_gana_sin_duplicar(self, session):
        ingest_csv(session, content=csv_bytes(self.OM), filename="export-28jul.csv")
        ingest_csv(session, content=csv_bytes(self.MCC), filename="export-03ago.csv")
        rebuild_scope(session)

        rows = session.scalars(select(WoScoped).where(WoScoped.in_scope.is_(True))).all()
        assert len(rows) == 1, "la incidencia no debe contarse dos veces"
        assert rows[0].is_mcc is True, "debe quedar la atribución del export más reciente"

    def test_el_cambio_queda_auditado(self, session):
        ingest_csv(session, content=csv_bytes(self.OM), filename="export-28jul.csv")
        result = ingest_csv(session, content=csv_bytes(self.MCC), filename="export-03ago.csv")
        assert result.attribution_changes == 1

        change = session.scalars(select(AttributionChange)).one()
        assert change.field == "cmms_user"
        assert change.old_value == "O&M Contractor"
        assert change.new_value == "MCC"
        assert change.plant_raw == "Vada 2"

    def test_sin_cambio_no_se_registra_nada(self, session):
        ingest_csv(session, content=csv_bytes(self.MCC), filename="a.csv")
        # Mismo contenido lógico en otro fichero (cambia la descripción).
        again = row(
            start="27 jul 2026, 16:20", created="27 jul 2026, 18:00", user="MCC", description="otra"
        )
        result = ingest_csv(session, content=csv_bytes(again), filename="b.csv")
        assert result.attribution_changes == 0


class TestScopeSobreDatosReales:
    def test_visibilidad_excluye_plant_sin_sst(self, session):
        ingest_csv(
            session,
            content=csv_bytes(
                row(plant="Ottobiano 1", equipment="Plant", start="1 jun 2026, 8:00",
                    user="O&M Contractor"),
                row(plant="Ottobiano 1", equipment="Inverter", start="1 jun 2026, 10:00", user="O&M Contractor"),
            ),
            filename="vis.csv",
        )
        rebuild_scope(session)
        state = {r.equipment: r.in_scope for r in session.scalars(select(WoScoped))}
        # Ottobiano 1 tiene SST=NA: el MCC no ve la planta, así que una WO del
        # contratista sobre 'Plant' no entra. (Si la hubiera abierto el MCC, contaría.)
        assert state["Plant"] is False, "SST=NA -> el MCC no ve la planta"
        assert state["Inverter"] is True

    def test_inverter_al_cero_excluye(self, session):
        ingest_csv(
            session,
            content=csv_bytes(row(plant="Albaida", country="Spain", contractor="RES Energy", user="O&M Contractor")),
            filename="inv0.csv",
        )
        rebuild_scope(session)
        record = session.scalars(select(WoScoped)).one()
        assert record.in_scope is False
        assert "visibilidad" in (record.excluded_reason or "")

    def test_cause_no_failure_fuera_en_espana(self, session):
        ingest_csv(
            session,
            content=csv_bytes(
                row(plant="Albaida", country="Spain", equipment="Meter", cause="Curtailment",
                    user="O&M Contractor")
            ),
            filename="cause.csv",
        )
        rebuild_scope(session)
        assert session.scalars(select(WoScoped)).one().in_scope is False

    def test_cause_no_failure_tampoco_cuenta_en_italia(self, session):
        """
        La regla es global: sólo averías, en cualquier país. El mantenimiento y el
        revamping son trabajo planificado y no son órdenes que apliquen al MCC.
        """
        ingest_csv(
            session,
            content=csv_bytes(
                row(user="O&M Contractor", cause="Corrective Maintenance", description="mantenimiento"),
                row(cause="Failure", description="averia", start="2 may 2026, 8:00"),
            ),
            filename="it.csv",
        )
        rebuild_scope(session)
        state = {r.description: r.in_scope for r in session.scalars(select(WoScoped))}
        assert state["mantenimiento"] is False
        assert state["averia"] is True

    def test_exclusion_de_planta_por_fecha(self, session):
        reference.apply_plant_exclusion(
            session, plant_name="Castelnau", from_date=dt.date(2026, 7, 3), reason="sin SCADA"
        )
        ingest_csv(
            session,
            content=csv_bytes(
                row(plant="Castelnau", country="France", start="2 jul 2026, 8:00", user="O&M Contractor"),
                row(plant="Castelnau", country="France", start="4 jul 2026, 8:00", user="O&M Contractor"),
            ),
            filename="cast.csv",
        )
        rebuild_scope(session)
        by_date = {r.start_date.isoformat(): r.in_scope for r in session.scalars(select(WoScoped))}
        assert by_date["2026-07-02"] is True
        assert by_date["2026-07-04"] is False

    def test_alias_de_contratista_aplicado(self, session):
        ingest_csv(
            session,
            content=csv_bytes(row(contractor="RES Energy Services S.A.S.")),
            filename="alias.csv",
        )
        rebuild_scope(session)
        record = session.scalars(select(WoScoped)).one()
        assert record.contractor == "RES"
        # El valor original se conserva: la normalización debe poder justificarse.
        assert record.contractor_raw == "RES Energy Services S.A.S."

    def test_rebuild_es_idempotente(self, session):
        ingest_csv(session, content=csv_bytes(row(), row(start="2 may 2026, 8:00")), filename="i.csv")
        first = rebuild_scope(session)
        second = rebuild_scope(session)
        assert first == second
        assert len(session.scalars(select(WoScoped)).all()) == 2

    def test_cambiar_la_referencia_y_recalcular_cambia_el_scope(self, session):
        """
        Recargar la matriz de visibilidad y llamar a rebuild debe recalcular el
        histórico. Es la razón de separar ingesta y scope.
        """
        ingest_csv(
            session,
            content=csv_bytes(row(plant="Ottobiano 1", equipment="Plant", user="O&M Contractor")),
            filename="v.csv",
        )
        rebuild_scope(session)
        assert session.scalars(select(WoScoped)).one().in_scope is False

        reference.load_visibility(
            session,
            (
                "plant,asset_id,country,sst,poi,ppc,wst,pst,inv_pct,onboarding_status\n"
                "Ottobiano 1,SX01,Italy,FULL,OK,OK,OK,OK,100,Completed\n"
            ).encode(),
        )
        rebuild_scope(session)
        assert session.scalars(select(WoScoped)).one().in_scope is True


class TestMetricas:
    @pytest.fixture()
    def poblado(self, session):
        ingest_csv(
            session,
            content=csv_bytes(
                # MCC, 100 €, 1 h de detección
                row(start="1 may 2026, 8:00", created="1 may 2026, 9:00", user="MCC",
                    revenue="100", description="a"),
                # O&M, 900 €, 10 h
                row(start="2 may 2026, 8:00", created="2 may 2026, 18:00",
                    user="O&M Contractor", revenue="900", description="b"),
                # O&M sin importe y abierta: no debe contar como 0 € en el rate
                row(start="3 may 2026, 8:00", created="3 may 2026, 9:00",
                    user="O&M Contractor", revenue="", ongoing="true",
                    incident_type="Communication Loss", description="c"),
            ),
            filename="m.csv",
        )
        rebuild_scope(session)
        session.commit()
        return session

    def test_rate_por_wo(self, poblado):
        s = q.summary(poblado, q.Filters())
        assert s["wos"] == 3
        assert s["wo_mcc"] == 1
        assert s["rate_wo"] == pytest.approx(33.3, abs=0.1)

    def test_rate_por_revenue_ignora_los_no_informados(self, poblado):
        s = q.summary(poblado, q.Filters())
        assert s["rate_revenue"] == pytest.approx(10.0)  # 100 / (100+900)
        assert s["revenue_total"] == pytest.approx(1000.0)
        # La cobertura avisa de que sólo 2 de 3 tienen importe.
        assert s["revenue_coverage"] == pytest.approx(66.7, abs=0.1)

    def test_mediana_de_deteccion_por_actor(self, poblado):
        s = q.summary(poblado, q.Filters())
        assert s["detection_median_mcc"] == pytest.approx(1.0)
        assert s["detection_median_ext"] == pytest.approx(5.5)  # mediana de 10 y 1

    def test_filtro_por_estado_particiona(self, poblado):
        abiertas = q.summary(poblado, q.Filters(status="open"))["wos"]
        cerradas = q.summary(poblado, q.Filters(status="closed"))["wos"]
        assert abiertas == 1
        assert cerradas == 2
        assert abiertas + cerradas == q.summary(poblado, q.Filters())["wos"]

    def test_filtro_por_tipo_particiona(self, poblado):
        p = q.summary(poblado, q.Filters(incident_type="P"))["wos"]
        c = q.summary(poblado, q.Filters(incident_type="C"))["wos"]
        assert (p, c) == (2, 1)
        assert p + c == q.summary(poblado, q.Filters())["wos"]

    def test_agrupacion_suma_el_total(self, poblado):
        total = q.summary(poblado, q.Filters())["wos"]
        for dimension in ("country", "portfolio", "contractor", "equipment", "plant", "month"):
            grouped = q.by_dimension(poblado, q.Filters(), dimension)
            assert sum(g["wos"] for g in grouped) == total, dimension

    def test_dimension_invalida(self, poblado):
        with pytest.raises(ValueError, match="no válida"):
            q.by_dimension(poblado, q.Filters(), "inventada")

    def test_serie_semanal_suma_el_total(self, poblado):
        total = q.summary(poblado, q.Filters())["wos"]
        series = q.timeseries(poblado, q.Filters(), "week")
        assert sum(p["wos"] for p in series) == total
        assert series == sorted(series, key=lambda p: p["period"])

    def test_missed_solo_devuelve_las_no_detectadas(self, poblado):
        missed = q.missed_wos(poblado, q.Filters())
        assert missed["total"] == 2
        assert {item["description"] for item in missed["items"]} == {"b", "c"}

    def test_rango_de_meses(self, poblado):
        assert q.summary(poblado, q.Filters(date_from="2026-06"))["wos"] == 0
        assert q.summary(poblado, q.Filters(date_to="2026-05"))["wos"] == 3

    def test_meta_expone_los_catalogos(self, poblado):
        meta = q.meta(poblado)
        assert meta["countries"] == ["Italy"]
        assert meta["portfolios"] == ["Toro 1"]
        assert meta["total_in_scope"] == 3


class TestAPI:
    def test_health(self, client):
        assert client.get("/api/health").json()["status"] == "ok"

    def test_subida_y_consulta(self, client):
        response = client.post(
            "/api/ingest/wo-export",
            files={"file": ("e.csv", csv_bytes(row()), "text/csv")},
        )
        assert response.status_code == 200
        assert response.json()["rows_inserted"] == 1

        summary = client.get("/api/kpis/summary").json()
        assert summary["wos"] == 1
        assert summary["rate_wo"] == 100.0

    def test_fichero_vacio_da_400(self, client):
        r = client.post("/api/ingest/wo-export", files={"file": ("v.csv", b"", "text/csv")})
        assert r.status_code == 400

    def test_csv_sin_columnas_da_422(self, client):
        r = client.post(
            "/api/ingest/wo-export", files={"file": ("m.csv", b"a,b\n1,2\n", "text/csv")}
        )
        assert r.status_code == 422

    def test_dimension_invalida_da_400(self, client):
        assert client.get("/api/kpis/by/inventada").status_code == 400

    def test_rango_de_fecha_mal_formado_da_422(self, client):
        assert client.get("/api/kpis/summary?from=2026").status_code == 422

    def test_meta_y_reglas_responden(self, client):
        assert "countries" in client.get("/api/meta").json()
        rules = client.get("/api/scope/rules").json()
        assert "equipment_in" in rules["rules"]

    def test_auditoria_de_reatribuciones_no_duplica_el_total(self, client):
        om = row(start="27 jul 2026, 16:20", created="27 jul 2026, 18:00", user="O&M Contractor")
        mcc = row(start="27 jul 2026, 16:20", created="27 jul 2026, 18:00", user="MCC")
        client.post("/api/ingest/wo-export", files={"file": ("a.csv", csv_bytes(om), "text/csv")})
        client.post("/api/ingest/wo-export", files={"file": ("b.csv", csv_bytes(mcc), "text/csv")})

        audit = client.get("/api/audit/attribution-changes").json()
        assert audit["total"] == 1
        assert audit["items"][0]["from"] == "O&M Contractor"
        assert audit["items"][0]["to"] == "MCC"
        assert client.get("/api/kpis/summary").json()["wos"] == 1

    def test_excluidas_explican_el_denominador(self, client):
        client.post(
            "/api/ingest/wo-export",
            files={"file": ("x.csv", csv_bytes(
                row(equipment="Tracker", user="O&M Contractor")), "text/csv")},
        )
        excluded = client.get("/api/scope/excluded").json()
        assert any("telemetría" in item["reason"] for item in excluded)


class TestConcentracionYEstado:
    """
    Los dos endpoints que alimentan los avisos de la vista de tendencia. Existen porque
    sin ellos el rate económico se lee mal: lo mueven unos pocos eventos grandes y la
    mezcla de WOs abiertas con cerradas compara provisionales con definitivas.
    """

    @pytest.fixture()
    def poblado(self, session):
        ingest_csv(
            session,
            content=csv_bytes(
                row(start="1 may 2026, 8:00", user="MCC", revenue="100000", description="gorda"),
                row(start="2 may 2026, 8:00", user="O&M Contractor", revenue="100", description="a"),
                row(start="3 may 2026, 8:00", user="O&M Contractor", revenue="100",
                    ongoing="true", description="b"),
            ),
            filename="c.csv",
        )
        rebuild_scope(session)
        session.commit()
        return session

    def test_concentracion_detecta_el_evento_dominante(self, poblado):
        conc = q.revenue_concentration(poblado, q.Filters(), top=1)
        assert conc["n"] == 3
        assert conc["top1_share"] == pytest.approx(99.8, abs=0.2)
        assert conc["items"][0]["revenue_loss"] == pytest.approx(100000.0)

    def test_concentracion_sin_importes(self, session):
        ingest_csv(session, content=csv_bytes(row(revenue="")), filename="v.csv")
        rebuild_scope(session)
        conc = q.revenue_concentration(session, q.Filters())
        assert conc["total"] == 0
        assert conc["items"] == []

    def test_status_split_ignora_el_filtro_de_estado(self, poblado):
        # Aunque se filtre a cerradas, el reparto debe seguir viendo las dos partes:
        # es lo que permite avisar de que se están mezclando.
        split = q.status_split(poblado, q.Filters(status="closed"))
        assert split["n_open"] == 1
        assert split["n_closed"] == 2
        assert split["share_open_count"] == pytest.approx(33.3, abs=0.1)

    def test_status_split_expone_metricas_de_cada_parte(self, poblado):
        split = q.status_split(poblado, q.Filters())
        assert split["closed"]["wos"] == 2
        assert split["open"]["wos"] == 1
        assert split["closed"]["rate_wo"] == pytest.approx(50.0)

    def test_endpoints_responden(self, client):
        client.post(
            "/api/ingest/wo-export",
            files={"file": ("e.csv", csv_bytes(row(revenue="500")), "text/csv")},
        )
        conc = client.get("/api/kpis/revenue-concentration?top=3")
        assert conc.status_code == 200
        assert conc.json()["top1_share"] == pytest.approx(100.0)
        split = client.get("/api/kpis/status-split")
        assert split.status_code == 200
        assert split.json()["n_closed"] == 1


class TestAliasDeColumnas:
    """
    El export cambió de nombres en agosto de 2026: `Om Contract` pasó a
    `O&M Contractor` y `Revenue Loss` a `Revenue Loss (€)`. Como son campos
    opcionales, la carga habría funcionado perdiendo contratista e importe en
    silencio — el fallo más peligroso de un pipeline. Estos tests lo impiden.
    """

    NEW_COLUMNS = [
        "Url Emaint", "Country", "Asset", "O&M Supervisor", "O&M Contractor",
        "Description English", "Start Ts Local", "WO Created Ts Local", "Ongoing",
        "CMMS User", "Equipment", "Incident Type", "Cause", "Failure Cause",
        "Capacity Affected", "Incident Lifecycle (hrs)", "Revenue Loss (€)",
    ]

    def new_format_csv(self, *, contractor="RES Energy", revenue="2216.0") -> bytes:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(self.NEW_COLUMNS)
        writer.writerow([
            "https://sonnedix.eu.accelix.com/#/nuoro/WorkOrders/abc123", "Italy", "Vada 2",
            "Cristiano Onnis", contractor, "inverter parado", "1 ago 2026, 8:00",
            "1 ago 2026, 9:00", "false", "MCC", "Inverter", "Production Loss", "Failure",
            "PROTECTION TRIP – CAUSE UNKNOWN", "0.3", "5", revenue,
        ])
        return buffer.getvalue().encode()

    def test_resuelve_los_nombres_nuevos(self):
        from app.ingest import resolve_columns
        mapping, missing = resolve_columns(set(self.NEW_COLUMNS))
        assert mapping["contractor"] == "O&M Contractor"
        assert mapping["revenue"] == "Revenue Loss (€)"
        assert missing == [], f"no debería faltar nada: {missing}"

    def test_resuelve_tambien_los_antiguos(self):
        from app.ingest import resolve_columns
        old = set(COLUMNS)
        mapping, missing = resolve_columns(old)
        assert mapping["contractor"] == "Om Contract"
        assert mapping["revenue"] == "Revenue Loss"
        assert missing == []

    def test_formato_nuevo_conserva_contratista_e_importe(self, session):
        ingest_csv(session, content=self.new_format_csv(), filename="ago.csv")
        rebuild_scope(session)
        record = session.scalars(select(WoScoped)).one()
        assert record.contractor == "RES", "el alias debe aplicarse sobre el nombre nuevo"
        assert record.contractor_raw == "RES Energy"
        assert record.revenue_loss == pytest.approx(2216.0)

    def test_captura_los_campos_nuevos(self, session):
        ingest_csv(session, content=self.new_format_csv(), filename="ago.csv")
        rebuild_scope(session)
        record = session.scalars(select(WoScoped)).one()
        assert record.wo_url.startswith("https://sonnedix.eu.accelix.com")
        assert record.failure_cause == "PROTECTION TRIP – CAUSE UNKNOWN"

    def test_avisa_si_falta_una_dimension(self, session):
        """Sin columna de contratista la carga sigue, pero debe avisar."""
        columns = [c for c in self.NEW_COLUMNS if c != "O&M Contractor"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(columns)
        writer.writerow([
            "url", "Italy", "Vada 2", "sup", "desc", "1 ago 2026, 8:00", "1 ago 2026, 9:00",
            "false", "MCC", "Inverter", "Production Loss", "Failure", "fc", "0.3", "5", "100",
        ])
        result = ingest_csv(session, content=buffer.getvalue().encode(), filename="sin.csv")
        assert result.rows_inserted == 1
        assert any("contractor" in w for w in result.warnings)

    def test_los_avisos_llegan_a_la_api(self, client):
        columns = [c for c in self.NEW_COLUMNS if c != "Revenue Loss (€)"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(columns)
        writer.writerow([
            "url", "Italy", "Vada 2", "sup", "desc", "1 ago 2026, 8:00", "1 ago 2026, 9:00",
            "false", "MCC", "Inverter", "Production Loss", "Failure", "fc", "0.3", "5",
        ])
        response = client.post(
            "/api/ingest/wo-export",
            files={"file": ("x.csv", buffer.getvalue().encode(), "text/csv")},
        )
        assert response.status_code == 200
        assert any("revenue" in w for w in response.json()["warnings"])

    def test_mezclar_formatos_no_duplica(self, session):
        """
        La misma WO exportada en el formato viejo y en el nuevo debe reconocerse como
        una sola: la clave natural no depende de los nombres de columna.
        """
        ingest_csv(session, content=csv_bytes(
            row(start="1 ago 2026, 8:00", created="1 ago 2026, 9:00",
                description="inverter parado", contractor="RES Energy", revenue="2216.0")
        ), filename="viejo.csv")
        ingest_csv(session, content=self.new_format_csv(), filename="nuevo.csv")
        rebuild_scope(session)
        assert len(session.scalars(select(WoScoped)).all()) == 1

    def test_url_en_la_lista_de_no_detectadas(self, session):
        ingest_csv(session, content=self.new_format_csv().replace(b",MCC,", b",O&M Contractor,"),
                   filename="om.csv")
        rebuild_scope(session)
        session.commit()
        missed = q.missed_wos(session, q.Filters())
        assert missed["total"] == 1
        assert missed["items"][0]["wo_url"].startswith("https://")
        assert missed["items"][0]["failure_cause"] == "PROTECTION TRIP – CAUSE UNKNOWN"


class TestFiltroDeScope:
    """
    El scope se puede invertir para ver qué se descarta y por qué.

    Los descartes ya están persistidos con su motivo, así que enseñar la diferencia
    entre lo que aplica y lo que no es un filtro, no una recarga.
    """

    def _cargar(self, session):
        ingest_csv(
            session,
            content=csv_bytes(
                row(),                                              # entra (MCC)
                row(user="O&M Contractor", start="2 may 2026, 8:00"),  # entra
                # Fuera de scope: son del contratista y no cumplen detectabilidad.
                # Si fueran del MCC contarían, porque el MCC las detectó.
                row(user="O&M Contractor", cause="Preventive Maintenance",
                    start="3 may 2026, 8:00"),
                row(user="O&M Contractor", equipment="Tracker", start="4 may 2026, 8:00"),
            ),
            filename="mix.csv",
        )
        rebuild_scope(session)
        session.commit()

    def test_in_es_el_valor_por_defecto(self, session):
        self._cargar(session)
        assert q.Filters().scope == "in"
        assert q.summary(session, q.Filters())["wos"] == 2

    def test_out_devuelve_lo_descartado(self, session):
        self._cargar(session)
        assert q.summary(session, q.Filters(scope="out"))["wos"] == 2

    def test_all_es_la_suma(self, session):
        self._cargar(session)
        dentro = q.summary(session, q.Filters(scope="in"))["wos"]
        fuera = q.summary(session, q.Filters(scope="out"))["wos"]
        assert q.summary(session, q.Filters(scope="all"))["wos"] == dentro + fuera

    def test_scope_invalido_falla_pronto(self, session):
        with pytest.raises(ValueError):
            q.Filters(scope="lo-que-sea").clauses()

    def test_comparacion_trae_los_tres_bloques_y_los_motivos(self, session):
        self._cargar(session)
        data = q.scope_comparison(session, q.Filters())
        assert data["in_scope"]["wos"] == 2
        assert data["out_of_scope"]["wos"] == 2
        assert data["share_in_scope"] == 50.0
        motivos = {r["reason"] for r in data["reasons"]}
        assert motivos == {sc_reason for sc_reason in motivos if sc_reason}
        assert len(data["reasons"]) == 2
        # El aviso de que el rate del bloque descartado no mide desempeño va en la
        # respuesta, no sólo en la documentación: viaja con el dato.
        assert "no mide desempeño" in data["note"]

    def test_los_motivos_llevan_causa_y_equipo(self, session):
        self._cargar(session)
        por_motivo = {r["reason"]: r for r in q.excluded_breakdown(session, q.Filters())}
        mantenimiento = next(r for k, r in por_motivo.items() if "causa" in k.lower())
        assert mantenimiento["top_causes"][0]["key"] == "Preventive Maintenance"

    def test_los_filtros_se_aplican_a_los_dos_lados(self, session):
        self._cargar(session)
        vacio = q.Filters(country="Spain")
        assert q.summary(session, vacio)["wos"] == 0
        assert q.summary(session, q.Filters(country="Spain", scope="out"))["wos"] == 0

    def test_endpoint_expone_el_parametro(self, client, session):
        self._cargar(session)
        assert client.get("/api/kpis/summary").json()["wos"] == 2
        assert client.get("/api/kpis/summary?scope=out").json()["wos"] == 2
        assert client.get("/api/kpis/summary?scope=all").json()["wos"] == 4
        assert client.get("/api/kpis/summary?scope=nope").status_code == 422
        assert client.get("/api/scope/comparison").json()["share_in_scope"] == 50.0


class TestCadenaDeTiempos:
    """Los tiempos del export se guardan enteros, con su cobertura al lado."""

    def test_se_persisten_todos_los_tramos(self, session):
        ingest_csv(
            session,
            content=csv_bytes(row(act_hrs="2", resolution_hrs="8", total_hrs="20")),
            filename="t.csv",
        )
        rebuild_scope(session)
        session.commit()
        scoped = session.scalars(select(WoScoped).where(WoScoped.in_scope.is_(True))).one()
        assert (scoped.act_hrs, scoped.resolution_hrs, scoped.total_time_hrs) == (2.0, 8.0, 20.0)

    def test_la_cobertura_distingue_un_tramo_fiable_de_uno_anecdotico(self, session):
        # Total informado en las tres; Resolución sólo en una.
        ingest_csv(
            session,
            content=csv_bytes(
                row(total_hrs="10", resolution_hrs="5"),
                row(total_hrs="20", start="2 may 2026, 8:00"),
                row(total_hrs="30", start="3 may 2026, 8:00"),
            ),
            filename="cov.csv",
        )
        rebuild_scope(session)
        session.commit()
        times = q.summary(session, q.Filters())["times"]
        assert times["total_time_hrs"]["coverage"] == 100.0
        assert times["total_time_hrs"]["median_all"] == 20.0
        assert times["resolution_hrs"]["coverage"] == pytest.approx(33.3, abs=0.1)
        assert times["resolution_hrs"]["n"] == 1

    def test_tramo_sin_ningun_dato_no_inventa_mediana(self, session):
        ingest_csv(session, content=csv_bytes(row(validation_hrs="")), filename="v.csv")
        rebuild_scope(session)
        session.commit()
        validacion = q.summary(session, q.Filters())["times"]["validation_hrs"]
        assert validacion["median_all"] is None
        assert validacion["coverage"] == 0.0

    def test_separa_mcc_de_contratista(self, session):
        ingest_csv(
            session,
            content=csv_bytes(
                row(user="MCC", total_hrs="4"),
                row(user="O&M Contractor", total_hrs="40", start="2 may 2026, 8:00"),
            ),
            filename="actor.csv",
        )
        rebuild_scope(session)
        session.commit()
        total = q.summary(session, q.Filters())["times"]["total_time_hrs"]
        assert total["median_mcc"] == 4.0
        assert total["median_ext"] == 40.0

    def test_percentil_con_un_solo_valor(self):
        assert q._percentile([7.0], 90) == 7.0

    def test_percentil_interpola(self):
        assert q._percentile([0.0, 10.0], 50) == 5.0

    def test_endpoint_con_desglose(self, client, session):
        ingest_csv(session, content=csv_bytes(row(total_hrs="12")), filename="e.csv")
        rebuild_scope(session)
        session.commit()
        data = client.get("/api/kpis/response-times?dimension=country").json()
        assert data["overall"]["total_time_hrs"]["median_all"] == 12.0
        assert data["by"][0]["key"] == "Italy"
        # La etiqueta viaja con el dato para que el frontend no la duplique.
        assert "Detección" in data["overall"]["detection_hours"]["label"]


class TestPersistenciaHistorica:
    """
    Una WO vista una vez no se pierde nunca.

    Es la garantía que hace que un porcentaje publicado no se pueda mover por detrás:
    si un export deja de traer una incidencia —fallo de ingesta, borrado en eMaint,
    edición de un campo clave— la WO se conserva y se marca, no se descuenta. Caso
    real: 75 WOs de julio-Italia desaparecieron entre el export del 4-ago y el del
    12-ago, 43 de ellas del MCC.
    """

    def _dos_fotos(self, session):
        """
        Foto 1: A (1 may), B (20 may), C (25 may).
        Foto 2: A y C, pero B ya no viene. Su ventana [1 may, 25 may] contiene el 20 de
        mayo, así que la ausencia de B es concluyente.
        """
        antigua = csv_bytes(
            row(description="incidencia A", created="1 may 2026, 9:00"),
            row(description="incidencia B", start="20 may 2026, 8:00", created="20 may 2026, 9:00"),
            row(description="incidencia C", start="25 may 2026, 8:00", created="25 may 2026, 9:00"),
        )
        nueva = csv_bytes(
            row(description="incidencia A", created="1 jun 2026, 9:00"),
            row(description="incidencia C", start="25 may 2026, 8:00", created="1 jun 2026, 9:00"),
        )
        ingest_csv(session, content=antigua, filename="foto-1.csv")
        ingest_csv(session, content=nueva, filename="foto-2.csv")
        rebuild_scope(session)
        session.commit()

    def test_la_wo_ausente_se_conserva(self, session):
        self._dos_fotos(session)
        descripciones = {
            r.description
            for r in session.scalars(select(WoScoped).where(WoScoped.in_scope.is_(True)))
        }
        assert descripciones == {"incidencia A", "incidencia B", "incidencia C"}

    def test_y_queda_marcada_como_desaparecida(self, session):
        self._dos_fotos(session)
        b = session.scalars(
            select(WoScoped).where(WoScoped.description == "incidencia B")
        ).one()
        assert b.vanished is True
        assert b.in_scope is True          # sigue contando: eso es lo importante
        a = session.scalars(
            select(WoScoped).where(WoScoped.description == "incidencia A")
        ).one()
        assert a.vanished is False

    def test_el_rate_no_cambia_al_desaparecer_una_wo(self, session):
        """El invariante que se pedía: recargar exports no mueve un número publicado."""
        ingest_csv(
            session,
            content=csv_bytes(
                row(description="A", user="MCC"),
                row(description="B", user="O&M Contractor", start="20 may 2026, 8:00"),
            ),
            filename="foto-1.csv",
        )
        rebuild_scope(session)
        session.commit()
        antes = q.summary(session, q.Filters())["rate_wo"]

        ingest_csv(
            session,
            content=csv_bytes(row(description="A", user="MCC", created="1 jun 2026, 9:00")),
            filename="foto-2.csv",
        )
        rebuild_scope(session)
        session.commit()
        assert q.summary(session, q.Filters())["rate_wo"] == antes

    def test_el_orden_de_carga_no_altera_el_resultado(self, session, monkeypatch):
        """
        Cargar la foto antigua *después* de la nueva no debe resucitar el dato viejo.

        Antes se ordenaba por id de fichero, o sea por orden de carga: recargar un
        export antiguo lo ascendía a "el más reciente" y pisaba la atribución buena.
        """
        vieja = csv_bytes(row(description="A", user="O&M Contractor", created="1 may 2026, 9:00"))
        nueva = csv_bytes(row(description="A", user="MCC", created="1 jun 2026, 9:00"))
        ingest_csv(session, content=nueva, filename="nueva.csv")
        ingest_csv(session, content=vieja, filename="vieja.csv")   # al revés a propósito
        rebuild_scope(session)
        session.commit()
        vigente = session.scalars(select(WoScoped)).one()
        assert vigente.is_mcc is True

    def test_as_of_sale_del_maximo_wo_created(self, session):
        from app.models import SourceFile

        ingest_csv(
            session,
            content=csv_bytes(
                row(created="1 may 2026, 9:00"),
                row(created="15 may 2026, 18:30", start="15 may 2026, 8:00"),
            ),
            filename="f.csv",
        )
        session.commit()
        f = session.scalars(select(SourceFile)).one()
        assert f.as_of == dt.datetime(2026, 5, 15, 18, 30)

    def test_rebuild_informa_de_cuantas_conserva(self, session):
        self._dos_fotos(session)
        stats = rebuild_scope(session)
        assert stats["vanished"] == 1

    def test_fuera_de_la_ventana_se_conserva_pero_no_se_marca(self, session):
        """
        Límite conocido y asumido a propósito.

        La ventana de cobertura de un export se deduce de las filas que trae, así que
        una WO que falta *por encima* de su última fila no se puede distinguir de una
        que el export nunca pretendió cubrir. Se elige el fallo seguro: la WO se
        conserva igual (que es lo que protege el porcentaje) y simplemente no se marca.
        Preferimos un aviso de menos a descontar una WO buena.
        """
        ingest_csv(
            session,
            content=csv_bytes(
                row(description="A", created="1 may 2026, 9:00"),
                row(description="Z", start="28 may 2026, 8:00", created="28 may 2026, 9:00"),
            ),
            filename="foto-1.csv",
        )
        ingest_csv(
            session,
            content=csv_bytes(row(description="A", created="1 jun 2026, 9:00")),
            filename="foto-2.csv",
        )
        rebuild_scope(session)
        session.commit()
        z = session.scalars(select(WoScoped).where(WoScoped.description == "Z")).one()
        assert z.in_scope is True     # lo esencial: sigue contando
        assert z.vanished is False    # no se marca porque no hay certeza

    def test_endpoint_de_auditoria(self, client, session):
        self._dos_fotos(session)
        data = client.get("/api/audit/vanished").json()
        assert data["total"] == 1
        assert data["items"][0]["description"] == "incidencia B"
        assert data["items"][0]["last_seen"] is not None
