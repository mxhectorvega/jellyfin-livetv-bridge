"""jellyfin-livetv-bridge — puente de TV en vivo para Jellyfin.

Lee fuentes M3U/HLS, valida cada stream con ffprobe y con una comprobacion
real de bytes, normaliza nombres y grupos, genera ``playlist.m3u`` + ``epg.xml``
y los sirve por HTTP junto con un endpoint de stream que copia el flujo (o lo
transcodifica solo si el perfil del canal lo exige).

Modulos:
    config      configuracion central (rutas absolutas, variables de entorno)
    models      estructuras de datos (Channel, ProbeResult, ...)
    util        utilidades (HTTP, subprocess, escritura atomica, logs JSON)
    ingest      etapa 1 — lectura de sources.yml y listas M3U
    probe       etapa 2 — validacion con ffprobe + liveness real
    normalize   etapa 3 — limpieza, deduplicacion, grupos y logos
    epg         etapa 4 — XMLTV desde fuentes publicas y mapeo de tvg-id
    playlist    etapa 5 — generacion de /data/playlist.m3u y /data/epg.xml
    transcode   etapa 6 — decision copy/transcode y comandos ffmpeg
    health      etapa 7 — health-check, backoff y activacion/desactivacion
    api         etapa 8 — endpoints HTTP (FastAPI)
    main        etapa 9 — orquestador del pipeline y arranque de uvicorn
"""

__version__ = "0.4.0"
