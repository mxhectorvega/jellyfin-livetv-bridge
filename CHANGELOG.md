# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado: [SemVer](https://semver.org/lang/es/).

Todas las cifras de este archivo se midieron. Cuando una entrada dice "medido",
el comando y la salida estan en el commit que la introdujo.

## [Sin publicar]

## [0.2.2] — 2026-10-13

### Corregido

- **El PID del audio cambiaba entre segmentos del MISMO canal.** Se elegia "el
  primer PID libre" contra los PIDs de cada segmento, y el video no trae siempre
  los mismos. Medido sobre el rele en vivo, en un solo jale: `0x100`, `0x101`,
  `0x102` y `0x103`. Cada segmento trae su propio PAT y PMT, asi que es MPEG-TS
  valido — pero un reproductor que se quede con el primer PMT y no relea la PSI
  pierde el audio al cambiar de segmento.
  Ahora el PID se elige desde `BASE_PID_AUDIO` (0x1F00, fuera de donde viven el
  video y la PSI) y el rele lo **recuerda por canal** y lo vuelve a pedir en cada
  segmento (`muxear(..., pid_audio=)`). Si ese PID llegara a chocar, se elige
  otro y el rele se entera por el reporte. `/relay/status` expone `pid_audio`.

### Anadido

- Tres pruebas nuevas: el PID por defecto sale estable aunque cambien los del
  video, se respeta el que da el rele, y se reasigna si choca con el video.
- Banco `tests/banco_pid_audio.py`: cuenta los PIDs de audio distintos a lo largo
  del flujo servido. No corre en CI: necesita el CDN real.

## [0.2.1] — 2026-10-13

### Corregido

- **El muxeo tiraba el PAT y no lo volvia a emitir.** Al juntar el audio con el
  video se descartaba la PSI original —PAT incluido— y la salida empezaba con el
  PMT, sin nada que dijera donde vive. El PAT es el punto de entrada de la PSI
  y es obligatorio (ISO/IEC 13818-1 §2.4.4.3, Tabla 2-30): sin el, el
  decodificador queda obligado a barrer el flujo buscando una tabla con
  `table_id` 0x02. ffprobe lo hace —y por eso los bancos daban verde— pero un
  cliente estricto no tiene por que. Ahora `pat_a_paquetes()` emite un PAT con
  el MISMO numero de programa que declara el PMT, y va primero en la salida.
  Medido antes del arreglo, sobre el rele en vivo: `PID 0x0000` ausente.
  Dos pruebas nuevas: el PAT existe y apunta al PMT, y el numero de programa
  empata entre PAT y PMT.

## [0.2.0] — 2026-10-13

### Anadido

- **Muxeo del audio cuando el canal lo publica aparte** (`src/ts_mux.py`).
  Medido: 66 de 197 canales del catalogo de Pluto traen el audio en su propia
  playlist (`#EXT-X-MEDIA:TYPE=AUDIO`) y la variante de video no lleva sonido,
  asi que se servian mudos mientras `/status` reportaba `audio: aac`. El rele
  ahora detecta esa rendicion, baja su playlist, empareja por POSICION (medido:
  los dos playlists difieren en 1 en `EXT-X-MEDIA-SEQUENCE`, asi que el numero
  de secuencia no sirve para emparejar) y muxea los dos flujos en un solo
  MPEG-TS antes de meterlo al colchon. `/status` expone `rendicion_audio`,
  `muxeados` y `sin_audio`.
- Dos bancos nuevos que NO corren en el CI porque necesitan red contra el CDN
  real: `tests/banco_mux.py` (el muxer contra segmentos reales) y
  `tests/banco_rele_audio.py` (el camino completo del rele). Ambos usan el
  parser de playlists DEL PROPIO RELE, para que banco y rele no puedan divergir.
  Medido: 12 OK / 0 fallo sobre 15 canales con audio aparte, y el rele sirviendo
  `['h264,video','aac,audio']` con `muxeados=2`.

### Corregido

- **Renumerado del PID del audio.** Elegia un PID libre mirando solo los
  paquetes presentes, y podia escoger uno que el PMT ya declaraba aunque no
  trajera paquetes en ese segmento; al agregarlo, `agregar_es` reventaba. Ahora
  el conjunto incluye paquetes presentes Y PIDs declarados.
- **El resolver borraba la rendicion de audio.** Al bajar del master a la
  variante, la segunda vuelta del bucle limpiaba `_audio_pl` y se perdia lo que
  el master acababa de declarar. Sintoma medido: `rendicion=False` en un canal
  que si la trae, y el rele sirviendo mudo sin un solo error en el log.

## [0.1.1] — 2026-10-13

### Corregido

- **La validacion solo miraba la URL principal.** Con el bloque `respaldo:`
  declarado y la principal caida, el canal se marcaba inaccesible, el
  health-check lo deshabilitaba a los 3 fallos y salia del M3U — asi que el
  respaldo **nunca llegaba a ejecutarse**, porque el rele no arranca para un
  canal que ya no se publica. Medido el 2026-10-12 con Canal 5.
  Ahora `probe.check_channel()` prueba la principal y luego cada respaldo en
  orden, y devuelve el primero que responde. Verificado en vivo: con la
  principal muerta a proposito, `check_stream` da inaccesible y `check_channel`
  encuentra el respaldo. `/status` expone `via_respaldo` y el pipeline reporta
  `por_respaldo`.
- **`config/sources.example.yml` publicaba CERO canales.** Declaraba
  `active_catalog: demo` pero sus canales sueltos no llevaban `catalog:`, asi
  que caian en `manual` y no se publicaba ninguno. Medido arrancando el servicio
  con la plantilla tal cual: `publicados: 0`. Ahora `publicados: 3`.
- Fuera la IP de LAN real de `docker-compose.yml`, `src/main.py` y las pruebas;
  la despersonalizacion anterior se habia quedado en el README y el
  `.env.example`.

### Cambiado

- **El orden del pipeline:** `ingest -> normalize -> respaldo -> probe` (antes
  la sonda corria antes de asociar los respaldos, lo que hacia imposible lo de
  arriba). Normalizar fija los ids estables; la sonda va despues.

### Anadido

- Seccion en el README: **«De donde salen los canales (el repo NO trae
  ninguno)»**, con la salida real de arrancar la plantilla, donde conseguir
  listas publicas (iptv-org, iptv-epg, epgshare01, i.mjh.nz) y el aviso de que
  los manifiestos mienten.

## [0.1.0] — 2026-10-12

Primera version publica. El servicio llevaba corriendo desde el 2026-10-10
sirviendo 10 canales nacionales; lo que cambia aqui es que quedo medido,
comprobado y con los estandares verificados por CI.

### Anadido

- **`src/ts_splice.py`**: cosido de la linea de tiempo de MPEG-TS. Al cambiar de
  fuente, desplaza PCR, PTS y DTS (modulo 2**33) para que la fuente nueva
  continue a la vieja, y marca `discontinuity_indicator` una vez por PID.
  Norma: ISO/IEC 13818-1 | ITU-T H.222.0 §2.4.3 y §2.4.3.6.
- **`tests/banco_ts.py`**: banco del cosido sobre un flujo real capturado del
  rele. Mide con `ffprobe`, no con el propio modulo. Sin el cosido el salto era
  de 1371.876 s; con el, 0.167 s y ya fuera del empalme.
- **`tests/banco_failover.py`**: banco del failover contra los streams reales
  del catalogo, con un proxy local que mata la fuente principal a media
  reproduccion.
- **`pyproject.toml`** (PEP 621) con la regla de estilo declarada: 100 columnas.
- **`.editorconfig`** y **`.pre-commit-config.yaml`**.
- **`tests/test_estandares.py`**: exige que `pyproject.toml` y
  `requirements.txt` declaren las mismas dependencias y que la version del
  modulo cuadre.
- **`epg.validar_xmltv()`**: revisa la guia contra lo que el DTD de XMLTV exige
  antes de escribirla.
- Job **"estilo"** en CI: `ruff check` y `ruff format --check`.
- `GET /channels` dejo de publicar `source_url`.

### Cambiado

- **El rele ya no se adelanta al tope del colchon cuando hay clientes.** El
  lector se bajaba la playlist nueva entera al cambiar de fuente y `_podar()`
  soltaba por la izquierda, que es donde esta el cliente. Medido: 24.1 s de
  contenido que nadie consumio, y que el espectador veia como tiron.
- **Tipo de medio de `/playlist.m3u`**: `audio/x-mpegurl` -> `audio/mpegurl`.
  RFC 8216 §4: el Content-Type "MUST be application/vnd.apple.mpegurl or
  audio/mpegurl". El que se servia no era ninguno de los dos.
- **Codigos HTTP**: canal desactivado 409 -> 403; DRM 451 -> 403 (451 es para
  restriccion LEGAL, no para algo irreproducible).
- `/stream` drena el `stderr` de ffmpeg.
- La imagen base del Dockerfile va fijada por digest.

### Corregido

- **`ingest.parse_m3u`**: el titulo del `#EXTINF` se cortaba en la PRIMERA coma.
  Con `tvg-name="Canal, Uno"` el nombre salia `'Uno" group-title="G",Canal
  Uno'`.
- **`transcode.plan_for`**: usaba lista NEGRA, asi que `vc1`, `theora` y `vp8`
  acababan en `-c copy` dentro de `-f mpegts`. Ahora es lista blanca.
- **`config`**: validaba el tipo pero no el rango. `LVB_PORT=99999` pasaba y el
  bind reventaba con `OverflowError`: pipeline arriba, API abajo y en silencio.
- **`epg`**: XMLTV con entidades internas (bomba XML). ~250 B expandian a 10,000
  caracteres.
- **`util.http_get`**: el tope de descarga se aplicaba al comprimido, no a la
  salida. Medido: 203,860 B de gzip -> 200 MB (1029x).
- HEAD en `/playlist.m3u` y `/epg.xml`: una ruta GET no lo atiende y devolvia
  405, que un cliente lee como caida.
- Cinco imports muertos que el linter encontro.

### Seguridad

- `config/sources.yml` y `data/state.json` en modo 600.
- `/channels` ya no expone las URLs de origen.
- `.dockerignore`: el contexto de build mandaba `.env`, `config/` y `data/` al
  daemon en cada build.

[Sin publicar]: https://github.com/mxhectorvega/jellyfin-livetv-bridge/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/mxhectorvega/jellyfin-livetv-bridge/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/mxhectorvega/jellyfin-livetv-bridge/releases/tag/v0.1.0
