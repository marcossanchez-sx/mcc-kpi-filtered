#!/usr/bin/env python3
"""
CLI de administración.

  python -m cli init                    crea el esquema
  python -m cli load-reference          carga plantas, visibilidad, portfolios, alias
  python -m cli ingest [FICHERO...]     carga exports (por defecto data/incoming/*.csv)
  python -m cli rebuild                 recalcula el scope
  python -m cli status                  resumen del estado
  python -m cli bootstrap               init + referencia + exclusiones + ingest + rebuild

Los ficheros procesados se mueven a data/processed/ para que la carpeta vigilada
quede limpia. La ingesta es idempotente: si vuelves a soltar el mismo fichero no se
duplica nada.
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import shutil
import sys
from pathlib import Path

from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).parent))

from app import reference  # noqa: E402
from app.db import get_session_factory, init_db  # noqa: E402
from app.ingest import IngestError, ingest_csv, rebuild_scope  # noqa: E402
from app.models import AttributionChange, Plant, SourceFile, WoScoped  # noqa: E402

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(levelname)s %(message)s")
log = logging.getLogger("cli")

DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
INCOMING = DATA_DIR / "incoming"
PROCESSED = DATA_DIR / "processed"
REFERENCE = DATA_DIR / "reference"

# Exclusiones puntuales con motivo. Castelnau pierde comunicación SCADA el 3-jul-2026:
# sin telemetría el MCC no puede detectar nada, así que sus WOs desde esa fecha no
# entran en el cálculo.
PLANT_EXCLUSIONS = [
    ("Castelnau", dt.date(2026, 7, 3), "pérdida de comunicaciones SCADA"),
]


def cmd_init(_: argparse.Namespace) -> int:
    init_db()
    print("esquema creado")
    return 0


def cmd_load_reference(_: argparse.Namespace) -> int:
    with get_session_factory()() as session:
        results = reference.load_all_from_directory(session, REFERENCE)
        applied = []
        for name, date, why in PLANT_EXCLUSIONS:
            if reference.apply_plant_exclusion(
                session, plant_name=name, from_date=date, reason=why
            ):
                applied.append(f"{name} desde {date}")
        session.commit()
    for key, value in results.items():
        print(f"  {key}: {value}")
    for item in applied:
        print(f"  exclusión aplicada: {item}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.files] if args.files else sorted(INCOMING.glob("*.csv"))
    if not paths:
        print(f"no hay CSV en {INCOMING}")
        return 0

    loaded = 0
    with get_session_factory()() as session:
        for path in paths:
            if not path.exists():
                log.error("no existe: %s", path)
                continue
            try:
                result = ingest_csv(
                    session, content=path.read_bytes(), filename=path.name
                )
            except IngestError as exc:
                log.error("%s: %s", path.name, exc)
                continue

            if result.already_loaded:
                print(f"  {path.name}: ya cargado, se omite")
            else:
                print(
                    f"  {path.name}: {result.rows_inserted}/{result.rows_total} filas"
                    f"{f', {result.attribution_changes} reatribuciones' if result.attribution_changes else ''}"
                )
                loaded += 1
            session.commit()

            if args.move and not path.is_absolute():
                PROCESSED.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(PROCESSED / path.name))

        if loaded and not args.no_rebuild:
            stats = rebuild_scope(session)
            session.commit()
            print(f"\nscope: {stats['in_scope']} en scope, {stats['excluded']} fuera")
            for reason, count in stats["excluded_by_reason"].items():
                print(f"    {count:5d}  {reason}")
    return 0


def cmd_rebuild(_: argparse.Namespace) -> int:
    with get_session_factory()() as session:
        stats = rebuild_scope(session)
        session.commit()
    print(f"{stats['in_scope']} en scope, {stats['excluded']} fuera")
    for reason, count in stats["excluded_by_reason"].items():
        print(f"  {count:5d}  {reason}")
    return 0


def cmd_cause_rule(args: argparse.Namespace) -> int:
    """
    Cambia en qué países sólo cuentan las WOs con Cause = Failure.

    El export de agosto viene ya filtrado a Failure, así que no puede aportar
    mantenimiento ni revamping. Si esos países se dejan sin la regla, el histórico
    los cuenta y los meses nuevos no: un corte metodológico invisible. Con este
    comando la regla se cambia y el histórico se recalcula entero.
    """
    from app.models import ScopeRule

    countries = [] if args.countries == ["none"] else args.countries
    with get_session_factory()() as session:
        existing = list(
            session.scalars(select(ScopeRule).where(ScopeRule.kind == "cause_failure_only"))
        )
        for rule in existing:
            session.delete(rule)
        session.flush()

        if countries == ["all"]:
            countries = ["*"]  # comodín: aplica a todos los países, presentes y futuros
        for country in countries:
            session.add(
                ScopeRule(
                    kind="cause_failure_only",
                    value=country,
                    note="sólo Cause=Failure; mantenimiento y revamping no son detectables",
                )
            )
        session.commit()
        label = "todos los países" if countries == ["*"] else (", ".join(sorted(countries)) or "ningún país")
        print("Cause = Failure aplicado a:", label)
        stats = rebuild_scope(session)
        session.commit()
    print(f"\nscope: {stats['in_scope']} en scope, {stats['excluded']} fuera")
    for reason, count in stats["excluded_by_reason"].items():
        print(f"  {count:5d}  {reason}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    with get_session_factory()() as session:
        files = session.scalar(select(func.count()).select_from(SourceFile)) or 0
        plants = session.scalar(select(func.count()).select_from(Plant)) or 0
        in_scope = (
            session.scalar(
                select(func.count()).select_from(WoScoped).where(WoScoped.in_scope.is_(True))
            )
            or 0
        )
        out_scope = (
            session.scalar(
                select(func.count()).select_from(WoScoped).where(WoScoped.in_scope.is_(False))
            )
            or 0
        )
        changes = session.scalar(select(func.count()).select_from(AttributionChange)) or 0
        mcc = (
            session.scalar(
                select(func.count())
                .select_from(WoScoped)
                .where(WoScoped.in_scope.is_(True), WoScoped.is_mcc.is_(True))
            )
            or 0
        )
        months = session.scalars(
            select(WoScoped.month).where(WoScoped.in_scope.is_(True)).distinct()
        ).all()

    print(f"ficheros cargados      : {files}")
    print(f"plantas de referencia  : {plants}")
    print(f"WOs en scope           : {in_scope}")
    print(f"WOs fuera de scope     : {out_scope}")
    print(f"reatribuciones         : {changes}")
    if in_scope:
        print(f"detection rate         : {mcc / in_scope * 100:.1f}%  ({mcc}/{in_scope})")
    if months:
        print(f"periodo                : {min(months)} .. {max(months)}")
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    cmd_init(args)
    cmd_load_reference(args)
    args.files = []
    args.move = False
    args.no_rebuild = False
    cmd_ingest(args)
    return cmd_status(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Administración del MCC Detection Rate")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="crea el esquema").set_defaults(func=cmd_init)
    sub.add_parser("load-reference", help="carga los datos de referencia").set_defaults(
        func=cmd_load_reference
    )

    p_ingest = sub.add_parser("ingest", help="carga exports de Work Orders")
    p_ingest.add_argument("files", nargs="*", help="por defecto: data/incoming/*.csv")
    p_ingest.add_argument("--move", action="store_true", help="mover a processed/ al terminar")
    p_ingest.add_argument("--no-rebuild", action="store_true", help="no recalcular el scope")
    p_ingest.set_defaults(func=cmd_ingest)

    sub.add_parser("rebuild", help="recalcula el scope").set_defaults(func=cmd_rebuild)

    p_cause = sub.add_parser(
        "cause-rule",
        help="define en qué países sólo cuenta Cause=Failure, y recalcula",
    )
    p_cause.add_argument(
        "countries",
        nargs="+",
        help="lista de países, o 'all' para todos, o 'none' para desactivar la regla",
    )
    p_cause.set_defaults(func=cmd_cause_rule)
    sub.add_parser("status", help="resumen del estado").set_defaults(func=cmd_status)
    sub.add_parser("bootstrap", help="init + referencia + ingest + rebuild").set_defaults(
        func=cmd_bootstrap
    )

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
