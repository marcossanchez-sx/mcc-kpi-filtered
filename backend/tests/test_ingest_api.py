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
    "Description English",
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
) -> list[str]:
    return [
        country, plant, contractor, start, created, ongoing, user,
        equipment, incident_type, cause, capacity, revenue, lifecycle, description,
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
                row(plant="Ottobiano 1", equipment="Plant", start="1 jun 2026, 8:00"),
                row(plant="Ottobiano 1", equipment="Inverter", start="1 jun 2026, 10:00"),
            ),
            filename="vis.csv",
        )
        rebuild_scope(session)
        state = {r.equipment: r.in_scope for r in session.scalars(select(WoScoped))}
        assert state["Plant"] is False, "SST=NA -> el MCC no ve la planta"
        assert state["Inverter"] is True

    def test_inverter_al_cero_excluye(self, session):
        ingest_csv(
            session,
            content=csv_bytes(row(plant="Albaida", country="Spain", contractor="RES Energy")),
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
                row(plant="Albaida", country="Spain", equipment="Meter", cause="Curtailment")
            ),
            filename="cause.csv",
        )
        rebuild_scope(session)
        assert session.scalars(select(WoScoped)).one().in_scope is False

    def test_cause_no_failure_si_cuenta_en_italia(self, session):
        ingest_csv(session, content=csv_bytes(row(cause="Curtailment")), filename="it.csv")
        rebuild_scope(session)
        assert session.scalars(select(WoScoped)).one().in_scope is True

    def test_exclusion_de_planta_por_fecha(self, session):
        reference.apply_plant_exclusion(
            session, plant_name="Castelnau", from_date=dt.date(2026, 7, 3), reason="sin SCADA"
        )
        ingest_csv(
            session,
            content=csv_bytes(
                row(plant="Castelnau", country="France", start="2 jul 2026, 8:00"),
                row(plant="Castelnau", country="France", start="4 jul 2026, 8:00"),
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
            content=csv_bytes(row(plant="Ottobiano 1", equipment="Plant")),
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
            files={"file": ("x.csv", csv_bytes(row(equipment="Tracker")), "text/csv")},
        )
        excluded = client.get("/api/scope/excluded").json()
        assert any("telemetría" in item["reason"] for item in excluded)
