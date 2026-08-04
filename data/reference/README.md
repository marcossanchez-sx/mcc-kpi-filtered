# Ficheros de referencia

Definen el scope del cálculo. **No se versionan** los ficheros reales: contienen
nombres de planta, la matriz de visibilidad SCADA y el mapeo de portfolios, que es
información operativa interna. Aquí sólo van las plantillas `*.example.csv`.

| Fichero | Origen | Contenido |
|---|---|---|
| `N3C_Onboarding_Completed.csv` | export interno (separador `;`) | `Plant Name;Country;Completed Since` |
| `n3c_visibility.csv` | informe Smartsheet *N3C onboarding completed for SX* | visibilidad por dispositivo: SST, POI, PPC, WST, PST, INV (%) |
| `plant_portfolio.csv` | mapeo de vehículo de financiación | `plant,country,portfolio` |
| `contractor_alias.csv` | mantenido a mano | regex → nombre canónico de contratista |

Tras reemplazar cualquiera de ellos:

```bash
docker compose exec api python cli.py load-reference
docker compose exec api python cli.py rebuild
```

El histórico completo se recalcula con las reglas nuevas.
