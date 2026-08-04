FROM python:3.12-slim

# Sin .pyc y con salida sin buffer: los logs aparecen en docker logs al instante.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

# Las dependencias en su propia capa: cambiar el código no reinstala nada.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY cli.py .

# Usuario sin privilegios.
RUN useradd --create-home --uid 10001 mcc && chown -R mcc:mcc /srv
USER mcc

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health',timeout=4).status==200 else 1)"

CMD ["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]
