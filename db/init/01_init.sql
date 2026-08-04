-- Se ejecuta solo la primera vez que se crea el volumen de Postgres.
-- El esquema lo crea SQLAlchemy; aquí sólo lo que la ORM no cubre.

-- Búsqueda por similitud sobre descripciones y nombres de planta.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Zona horaria fija: los timestamps del export son hora local de planta y se
-- guardan sin tz. Dejarlo explícito evita sorpresas al comparar fechas.
SET TIME ZONE 'UTC';
