"""
Modelo de datos del MCC Detection Rate.

Decisión central: NO sobrescribimos work orders. Cada export deja un snapshot y una
vista se queda con la versión más reciente de cada incidencia. Esto resuelve el
problema real que encontramos analizando los CSV: la misma incidencia (misma planta,
misma hora de inicio, mismo equipo) aparecía como `O&M Contractor` en el export del
28-jul y como `MCC` en el del 3-ago. Con este modelo:

  * no hay que decidir a mano si "sustituir o sumar" al recargar un periodo,
  * la reatribución queda auditada en `attribution_change`,
  * recargar el mismo fichero dos veces no duplica nada (hash de contenido).

Las reglas de scope viven en tablas de referencia, no en el código, para que al
actualizar la matriz de visibilidad o el mapeo de portfolios se pueda recalcular
todo el histórico sin tocar Python.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ──────────────────────────── ingesta / auditoría ────────────────────────────


class SourceFile(Base):
    """Un fichero cargado. El hash hace la ingesta idempotente."""

    __tablename__ = "source_file"

    id: Mapped[int] = mapped_column(primary_key=True)
    filename: Mapped[str] = mapped_column(String(500))
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    loaded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    rows_total: Mapped[int] = mapped_column(Integer, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, default=0)
    rows_superseded: Mapped[int] = mapped_column(Integer, default=0)
    period_start: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    observations: Mapped[list["WoObservation"]] = relationship(back_populates="source")


class WoObservation(Base):
    """
    Una fila de un export, tal cual venía. Inmutable.

    `natural_key` identifica la incidencia física: planta + inicio + equipo + tipo.
    Varias observaciones pueden compartir natural_key si vienen de exports distintos;
    la más reciente (por source_file.loaded_at) es la vigente.
    """

    __tablename__ = "wo_observation"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"), index=True)

    natural_key: Mapped[str] = mapped_column(String(400), index=True)

    plant_raw: Mapped[str] = mapped_column(String(200))
    plant_norm: Mapped[str] = mapped_column(String(200), index=True)
    country: Mapped[str | None] = mapped_column(String(80), index=True)
    start_ts: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    wo_created_ts: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    equipment: Mapped[str | None] = mapped_column(String(120), index=True)
    incident_type: Mapped[str | None] = mapped_column(String(80))
    cause: Mapped[str | None] = mapped_column(String(120))
    cmms_user: Mapped[str | None] = mapped_column(String(120))
    is_mcc: Mapped[bool] = mapped_column(Boolean, index=True)
    ongoing: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    om_contract_raw: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    capacity_affected: Mapped[float | None] = mapped_column(Float)
    revenue_loss: Mapped[float | None] = mapped_column(Float)
    incident_lifecycle_hrs: Mapped[float | None] = mapped_column(Float)

    source: Mapped[SourceFile] = relationship(back_populates="observations")

    __table_args__ = (
        UniqueConstraint("source_file_id", "natural_key", name="uq_obs_file_key"),
        Index("ix_obs_key_loaded", "natural_key", "source_file_id"),
    )


class AttributionChange(Base):
    """
    Registro de cuando un export cambia quién detectó una incidencia ya conocida.

    Esto no es un detalle técnico: significa que el detection rate de un periodo
    cambia según cuándo exportes, y cualquier cifra ya reportada puede quedar
    desfasada. Merece quedar visible en vez de resolverse en silencio.
    """

    __tablename__ = "attribution_change"

    id: Mapped[int] = mapped_column(primary_key=True)
    natural_key: Mapped[str] = mapped_column(String(400), index=True)
    plant_raw: Mapped[str] = mapped_column(String(200))
    start_ts: Mapped[dt.datetime] = mapped_column(DateTime)
    equipment: Mapped[str | None] = mapped_column(String(120))
    field: Mapped[str] = mapped_column(String(60))  # p.ej. "cmms_user", "ongoing"
    old_value: Mapped[str | None] = mapped_column(String(200))
    new_value: Mapped[str | None] = mapped_column(String(200))
    from_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"))
    to_file_id: Mapped[int] = mapped_column(ForeignKey("source_file.id"))
    detected_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc)
    )


# ──────────────────────── datos de referencia (scope) ────────────────────────


class Plant(Base):
    """Plantas con onboarding N3C y su matriz de visibilidad por dispositivo."""

    __tablename__ = "plant"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    name_norm: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    country: Mapped[str | None] = mapped_column(String(80), index=True)
    asset_id: Mapped[str | None] = mapped_column(String(60))
    portfolio: Mapped[str | None] = mapped_column(String(160), index=True)

    # Fecha desde la que el MCC tiene visibilidad. WOs anteriores quedan fuera.
    completed_since: Mapped[dt.date | None] = mapped_column(Date)

    # Visibilidad por dispositivo, tal cual viene del informe N3C.
    vis_sst: Mapped[str | None] = mapped_column(String(60))
    vis_poi: Mapped[str | None] = mapped_column(String(60))
    vis_ppc: Mapped[str | None] = mapped_column(String(60))
    vis_wst: Mapped[str | None] = mapped_column(String(60))
    vis_pst: Mapped[str | None] = mapped_column(String(60))
    vis_inv_pct: Mapped[float | None] = mapped_column(Float)
    onboarding_status: Mapped[str | None] = mapped_column(String(80))

    # Exclusión puntual con motivo y fecha (caso Castelnau: pierde SCADA el 3-jul).
    excluded_from: Mapped[dt.date | None] = mapped_column(Date)
    excluded_reason: Mapped[str | None] = mapped_column(Text)


class ContractorAlias(Base):
    """Regex -> nombre canónico. El CSV trae variantes del mismo grupo."""

    __tablename__ = "contractor_alias"

    id: Mapped[int] = mapped_column(primary_key=True)
    pattern: Mapped[str] = mapped_column(String(200), unique=True)
    canonical: Mapped[str] = mapped_column(String(160), index=True)


class ScopeRule(Base):
    """
    Reglas de scope configurables, para no tenerlas incrustadas en el código.

    kind:
      equipment_in / equipment_out  -> value = tipo de equipo (minúsculas)
      incident_type_in             -> value = "Production Loss" | "Communication Loss"
      country_out                  -> value = país excluido (Japón, por shadowing)
      cause_failure_only           -> value = país donde solo cuenta Cause=Failure
    """

    __tablename__ = "scope_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), index=True)
    value: Mapped[str] = mapped_column(String(160))
    note: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_rule"),)


# ──────────────────────────── resultado del scope ────────────────────────────


class WoScoped(Base):
    """
    Vista materializada del scope: una fila por incidencia vigente que entra en el
    cálculo. Se reconstruye entera con `rebuild_scope()`, que es rápido y evita
    incoherencias cuando cambian las reglas o la matriz de visibilidad.

    `excluded_reason` guarda por qué queda fuera una WO descartada, para poder
    justificar cualquier cifra sin volver a los CSV.
    """

    __tablename__ = "wo_scoped"

    id: Mapped[int] = mapped_column(primary_key=True)
    observation_id: Mapped[int] = mapped_column(
        ForeignKey("wo_observation.id"), unique=True, index=True
    )
    natural_key: Mapped[str] = mapped_column(String(400), index=True)

    plant: Mapped[str] = mapped_column(String(200), index=True)
    country: Mapped[str] = mapped_column(String(80), index=True)
    portfolio: Mapped[str] = mapped_column(String(160), index=True)
    contractor: Mapped[str] = mapped_column(String(160), index=True)
    contractor_raw: Mapped[str | None] = mapped_column(String(300))

    start_date: Mapped[dt.date] = mapped_column(Date, index=True)
    month: Mapped[str] = mapped_column(String(7), index=True)
    iso_week: Mapped[str] = mapped_column(String(10), index=True)

    is_mcc: Mapped[bool] = mapped_column(Boolean, index=True)
    equipment: Mapped[str] = mapped_column(String(120), index=True)
    incident_type: Mapped[str] = mapped_column(String(1))  # P | C
    ongoing: Mapped[bool] = mapped_column(Boolean, index=True)

    description: Mapped[str | None] = mapped_column(Text)
    capacity_affected: Mapped[float | None] = mapped_column(Float)
    revenue_loss: Mapped[float | None] = mapped_column(Float)
    detection_hours: Mapped[float | None] = mapped_column(Float)

    in_scope: Mapped[bool] = mapped_column(Boolean, index=True, default=True)
    excluded_reason: Mapped[str | None] = mapped_column(String(200), index=True)

    __table_args__ = (
        Index("ix_scoped_filters", "in_scope", "country", "month"),
        Index("ix_scoped_dims", "in_scope", "portfolio", "contractor"),
    )
