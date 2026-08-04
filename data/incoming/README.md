# Exports pendientes de cargar

Deja aquí los CSV de Work Orders y lanza:

```bash
docker compose exec api python cli.py ingest --move
```

La ingesta es idempotente: el mismo fichero no se procesa dos veces (se compara el
hash del contenido). Los procesados se mueven a `../processed/`.

Los CSV no se versionan.
