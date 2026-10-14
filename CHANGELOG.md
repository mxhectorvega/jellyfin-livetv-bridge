# Changelog

Formato: [Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/).
Versionado: [SemVer](https://semver.org/lang/es/).

Todas las cifras de este archivo se midieron. Cuando una entrada dice "medido",
el comando y la salida estan en el commit que la introdujo.

## [Sin publicar]

## [0.4.3] — 2026-10-14

### Corregido

- **Los canales se quedaban PAUSADOS y no habia forma de reanimarlos.** Causa
  medida: el CDN de Pluto **vacia el playlist variante en la segunda lectura
  seguida** —devuelve `200` con cero segmentos, que no es una excepcion— y basta
  con **volver a pedir el master** para que lo rearme:

  ```
  1) master nuevo -> variante -> leer    segmentos=5
  2) la MISMA variante otra vez          segmentos=0   <- el CDN la vacia
  3) master nuevo otra vez -> leer       segmentos=5   <- se rearma
  4) y la nueva, repetida                segmentos=0
  ```

  El rele pedia el master UNA sola vez al resolver la variante y despues
  sondeaba esa misma URL cada `SONDEO_S` (0.5 s) para siempre. Bajaba los
  primeros segmentos, el playlist se vaciaba, y el canal se quedaba congelado
  **sin un solo error en el log**, porque un `200` vacio no dispara el camino de
  401/403/404. Medido en produccion: 5 reles con cliente enganchado y el frente
  clavado, reconectando en bucle cada ~60 s.

  Ahora hay un **vigilante de estancamiento**: si hay clientes esperando y el
  frente lleva mas de `ESTANCADO_S` (25 s) sin moverse, se vuelve a pedir el
  master. El reloj arranca cuando entra un cliente, para no juzgar de mas a un
  rele precalentado, y sin clientes no dispara. `/relay/status` expone
  `estancamientos`. Tres pruebas nuevas, incluida la de que NO dispara sin
  clientes.

## [0.4.2] — 2026-10-14

### Anadido

- **`tests/test_main.py`: 12 pruebas para el orquestador.** `src/main.py` no
  tenia ninguna, y es donde viven **dos invariantes que ya se rompieron una
  vez**: el ORDEN de las etapas (normalizar antes de asociar respaldos, que
  invertido deshabilitaba canales con respaldo vivo — medido con Canal 5) y el
  TOPE de precalentado (sin el se abre un lector por canal publicado y se
  satura el enlace de subida). Tambien cubre `persist_state` —incluido el modo
  600 del estado, que lleva URLs firmadas— y `load_state` con archivo ausente y
  con JSON corrupto. `src/main.py` paso de 0 a 12 pruebas.

### Corregido

- **Documentado por que la llave AES viaja en el argv de `openssl`**, que es el
  punto que una auditoria marco como critico. Verificado que **no hay
  alternativa**: `openssl enc` no acepta una llave CRUDA por archivo (`-kfile` y
  `-pass` son para contraseñas y la derivan con un KDF, o sea otra llave). La
  exposicion queda acotada a quien pueda leer `/proc/<pid>/cmdline`: dentro del
  contenedor el unico proceso con el mismo UID es el propio rele, que ya tiene
  esa llave en memoria, y root del host la leeria de ahi igual. El unico riesgo
  real —que el argv acabe en un log— no aplica: el argv nunca se registra. Queda
  anotado en `_descifrar` y en la seccion Seguridad del README.

### Descartado tras verificarlo

- **La carrera sobre `_vistos` NO existe.** El reporte decia que un hilo de
  cliente podia marcarlo entre el chequeo y la descarga. Verificado leyendo el
  codigo: `_vistos` tiene **un solo escritor** (el hilo lector), el `clear()` de
  `_contar_fallo` va bajo lock y el `len()` de `stats()` tambien. La lectura sin
  lock del lector es del mismo hilo. Se deja un comentario en el codigo
  explicandolo, para que el siguiente auditor no lo vuelva a marcar.

## [0.4.1] — 2026-10-14

### Anadido

- **`docs/ARQUITECTURA.md`**: el sistema descrito con el modelo **C4** — contexto,
  contenedores y los componentes del rele — mas el flujo de un canal de punta a
  punta en un diagrama de secuencia, y el porque de cada decision de forma. Los
  diagramas son **Mermaid**, asi que GitHub los renderiza sin build step; se
  comprobaron contra un renderizador real antes de publicarlos, no a ojo.
- **`docs/CONFIGURACION.md`**: la referencia de configuracion, con cada campo de
  `sources.yml` y cada variable de entorno con su valor por defecto.
- **Indice y vista de contexto en el README.** La portada lleva el diagrama C4 de
  nivel 1 y enlaza al resto.

### Corregido

- **El README se contradecia sobre el catalogo activo.** Una seccion decia que
  `POST /catalog` no persistia y otra que si. La verdad esta en
  `resolve_active_catalog()`: el POST escribe `/data/active_catalog.txt`, y ESE
  archivo gana sobre el `.env` y sobre `settings.active_catalog`. Verificado
  recreando el contenedor: siguio en `pluto`.
- **El README decia que el servicio no resuelve los cortes a comerciales.**
  Falso desde que existe el rele: `/relay/<id>` los absorbe con su colchon. La
  seccion «Que resuelve y que NO» se reescribio con lo que el servicio hace hoy.
- **`ts_mux.py` faltaba** en la lista de modulos de la seccion «Estructura».

## [0.4.0] — 2026-10-14

### Eliminado

- **Modulo `src/azteca.py` (139 lineas):** resolvia el token de TV Azteca para 2 canales nacionales.
- **`tests/test_azteca.py` (35 pruebas).**
- **Ajustes `azteca_enabled` y `azteca_local_base`** en `src/config.py`.
- **Endpoint `GET /azteca/<clave>`** en `src/api.py`.
- **Llamadas a `reescribir_url`** en `src/api.py` y `src/probe.py`.
- **Fuente «Nacionales» y guia «Nacionales (propia)»** en `config/sources.yml`.
- **Catalogo del host** `/home/server/catalogo_nacionales.m3u`.

### Cambiado

- **El proyecto queda solo con Pluto TV (198 canales).** Se conserva el mecanismo de `respaldo:` en `sources.yml` para fuentes alternas de cualquier canal, y el mecanismo de varios catalogos alimentados con uno publicado.

## [0.3.0] — 2026-10-13

### Cambiado

- **Catalogo activo: ahora SOLO Pluto TV (198 canales).** Antes se publicaba
  "todos" (10 nacionales + 198 Pluto = 208). En Jellyfin el conteo paso de **208
  a 197** con solo disparar `Actualizar canales` y `Refresh Guide`, sin dejar
  fantasmas. Medido: `GET /catalogs` responde
  `{"activo": "pluto", "catalogs": {"nacionales": 10, "pluto": 198}}`.
- **Documentado como se cambia el catalogo activo:** `POST /catalog/<nombre>`
  persiste en `/data/active_catalog.txt`, que gana sobre el `.env` y sobre
  `settings.active_catalog`. Ese archivo es la fuente de verdad; la clave de
  configuracion solo es el default de cuando aun no existe.
- **196 de 198 canales de Pluto accesibles** (medido). Los dos que no:
  "Paisajes por Stingray" (fallo de red) y "NFL Channel" (HTTP 500 del CDN de
  Pluto).
- **196 de 198 vienen cifrados en AES-128** (NO es DRM: 0 con DRM real), 198 con
  token firmado (caduca en 24 h), 0 piden auth.
- **Resolucion tipica 640x360** en 169 de los 198 canales.
- **0 canales de Pluto con respaldo declarado**.

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
