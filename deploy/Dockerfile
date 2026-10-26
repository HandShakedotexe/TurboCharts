# Sin dependencias que instalar: la imagen es Python y el código, nada más.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY liqrec/ /app/liqrec/
COPY config.example.toml /app/config.example.toml

# Usuario sin privilegios: el recorder no necesita ninguno.
RUN useradd --create-home --uid 10001 liqrec && mkdir -p /data && chown -R liqrec /data /app
USER liqrec

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health',timeout=4).status==200 else 1)"

ENTRYPOINT ["python", "-m", "liqrec"]
CMD ["--config", "/app/config.toml", "--data-dir", "/data", "--health-port", "8787"]
