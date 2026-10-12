# syntax=docker/dockerfile:1
#
# Imagen del servicio jellyfin-livetv-bridge.
# Python 3.12 sobre Debian slim + ffmpeg/ffprobe del sistema.
#
# Sin GPU dedicada en el host (Intel HD 4000 / QSV limitado): la imagen NO
# asume VAAPI. Si el host expone /dev/dri y el driver i965 esta disponible,
# se pasa por docker-compose; si no, transcode.py cae a CPU por si solo.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LVB_CONFIG_DIR=/config \
    LVB_DATA_DIR=/data \
    PATH=/usr/local/bin:/usr/bin:/bin

# ffmpeg trae ffplay/ffprobe. tzdata para America/Mexico_City.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY src/ /app/src/
COPY README.md DISCLAIMER.md LICENSE /app/

# Volumenes declarados; el compose los monta desde el host.
VOLUME ["/config", "/data"]

# El servicio corre con el UID/GID del dueno de /config y /data en el host
# (server:server = 1000:1000). NO se crea usuario con useradd: no existe en las
# imagenes slim, y un UID numerico basta para Python. Sin esto el contenedor no
# puede escribir en los volumenes del host.
ARG LVB_UID=1000
ARG LVB_GID=1000
RUN mkdir -p /config /data \
 && chown -R ${LVB_UID}:${LVB_GID} /config /data
USER ${LVB_UID}:${LVB_GID}

EXPOSE 8581

HEALTHCHECK --interval=60s --timeout=10s --start-period=40s --retries=3 \
  CMD python -c "import os,sys,urllib.request; h=os.environ.get('LVB_BIND_IP') or '127.0.0.1'; sys.exit(0 if urllib.request.urlopen('http://%s:%s/health'%(h,os.environ.get('LVB_PORT','8581')), timeout=8).status==200 else 1)"

ENTRYPOINT ["python", "-m", "src.main"]
