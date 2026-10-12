# jellyfin-livetv-bridge

Servicio que convierte listas M3U/HLS en un tuner + guia para **Jellyfin Live
TV**, sin antena. Corre en un contenedor Docker (Debian + Python 3.12) dentro
de un LXC de Proxmox; ffmpeg/ffprobe se invocan por `subprocess`.

```
sources.yml --> INGESTA --> VALIDACION --> NORMALIZACION --> EPG
                                    |
                                    v
                         playlist.m3u + epg.xml  -->  Jellyfin
                                    |
                         API HTTP (FastAPI) /stream/<id>  (copy por defecto)
                                    |
                         health-check -> desactiva/reactiva canales
```

## Que resuelve y que NO

Resuelve: **catalogo + guia + validacion + salud** de tus propias listas, con
un unico endpoint que Jellyfin consume como tuner `m3u`.

NO resuelve por si solo: los **cortes a comerciales / discontinuidades HLS** de
proveedores que cambian de host a media emision (`EXT-X-DISCONTINUITY`). Ese
caso necesita un intermediario con colchon propio (ver "Limitaciones
conocidas"). Este servicio entrega el flujo por copia con `-fflags +genpts` e
intenta reconectar, que cubre el caso comun, no el splicing de anuncios que
cambia de host.

## Estructura

```
src/
  ingest.py     etapa 1  lee /config/sources.yml y listas M3U (catalogo por fuente)
  probe.py      etapa 2  ffprobe + liveness real (bytes, no metadatos)
  normalize.py  etapa 3  limpieza, dedupe, grupos, logos
  epg.py        etapa 4  XMLTV desde fuentes publicas + mapeo de tvg-id
  playlist.py   etapa 5  /data/playlist.m3u + catalogo visible
  transcode.py  etapa 6  copy por defecto; VAAPI/QSV con fallback a CPU
  health.py     etapa 7  health-check con backoff y activacion
  api.py        etapa 8  /health /status /catalogs /catalog/<x> /playlist.m3u /epg.xml /stream/<id>
  main.py       etapa 9  orquestador
config/sources.example.yml    plantilla (datos FICTICIOS)
tests/                        pytest: ingesta, validacion, normalizacion, M3U, XMLTV
```

## Arranque rapido (Docker)

```bash
# 1. Copia la configuracion y editala con TUS fuentes
mkdir -p /opt/jellyfin-livetv-bridge/{config,data}
cp config/sources.example.yml /opt/jellyfin-livetv-bridge/config/sources.yml
cp .env.example /opt/jellyfin-livetv-bridge/.env
$EDITOR /opt/jellyfin-livetv-bridge/config/sources.yml
$EDITOR /opt/jellyfin-livetv-bridge/.env      # LVB_PUBLIC_BASE = IP LAN del host

# 2. Levanta
docker compose up -d --build
docker compose logs -f livetv-bridge

# 3. Verifica
curl -s http://10.0.0.21:8581/health
curl -s http://10.0.0.21:8581/playlist.m3u | head
curl -s http://10.0.0.21:8581/status | python3 -m json.tool | head -40
```

Rutas absolutas en todo: `/config` (entrada) y `/data` (salida).

## Pruebas

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q
```

## Fuente de TV Azteca integrada

TV Azteca publica en su plataforma (mediastream / `mdstrm.com`) y exige un
`access_token` que caduca y rota en cada llamada. El bridge lo resuelve **por
si mismo**, sin servicio externo ni puerto extra:

```
GET /azteca/azteca7      -> 302 al master autenticado (equivale al viejo :8582)
GET /azteca/azteca-intl  -> idem para Azteca Internacional
LVB_AZTECA_ENABLED       true  (apagar = no tocar esas URLs)
LVB_AZTECA_BASE          http://127.0.0.1:8582  (prefijo que se reescribe)
```

Si el catalogo trae URLs que apuntan al proveedor local
(`http://127.0.0.1:8582/azteca7`), el bridge las reescribe al master con token
**al validar y al servir `/stream`**, nunca en el M3U publicado. El token se
cachea 240 s y, si no se puede obtener, se conserva la URL original y el canal
aparece como inaccesible en `/status` (fallo visible, no silencioso).

Con esto el `refresco-azteca.service` del host queda sin funcion: su trabajo
vive ahora en `src/azteca.py`, versionado y con tests.

## Catalogos: varios alimentados, uno visible

El servicio ingesta, valida y mantiene **todos** los catalogos, pero publica
solo uno: `playlist.m3u` y `epg.xml` salen del catalogo activo. Asi Jellyfin ve
un grupo a la vez (nacionales, o Pluto, o ViX) sin mantener tres tuners ni
cambiar la URL.

Cada fuente (o canal suelto) lleva su `catalog:` en `sources.yml`:

```yaml
sources:
  - name: "Nacionales"
    url: "/hostserver/catalogo_nacionales.m3u"
    catalog: "nacionales"
  - name: "Pluto"
    url: "https://.../pluto_mx.m3u"
    catalog: "pluto"
```

El catalogo visible se fija con `LVB_ACTIVE_CATALOG` (o
`settings.active_catalog`) y se puede cambiar **en caliente**, sin reiniciar ni
re-descargar la guia:

```
GET  /catalogs              -> {"activo": "...", "catalogs": {...}, ...}
POST /catalog/pluto         -> cambia, reescribe M3U + XMLTV, devuelve conteos
POST /catalog/todos         -> los publica juntos otra vez
```

La seleccion se persiste en `/data/active_catalog.txt` (gana sobre el `.env`,
asi el cambio por API siempre surte efecto). En Jellyfin, tras cambiar de
catalogo hay que disparar `Actualizar canales` y `Refresh Guide`.

## Conectar a Jellyfin Live TV (paso a paso)

**0. Requisito previo.** Jellyfin debe poder alcanzar
`LVB_PUBLIC_BASE` (por ejemplo `http://10.0.0.21:8581`) por la LAN.

**1. Crear el tuner (tipo `m3u`, NO `hdhomerun`).**

```
POST /LiveTv/TunerHosts          (header X-Emby-Token: <API_KEY>)
{
  "Type": "m3u",
  "Url": "http://10.0.0.21:8581/playlist.m3u",
  "FriendlyName": "LiveTV Bridge",
  "TunerCount": 4,
  "AllowHWTranscoding": false,
  "AllowStreamSharing": true,
  "EnableStreamLooping": true
}
```

Nota: en Jellyfin 12.x `GET /LiveTv/TunerHosts` responde **405** — el listado
vive dentro de `GET /System/Configuration/livetv` en `TunerHosts`.

**2. Crear la guia (listing provider `xmltv`).**

```
POST /LiveTv/ListingProviders
{
  "Type": "xmltv",
  "Path": "http://10.0.0.21:8581/epg.xml",
  "FriendlyName": "LiveTV Bridge EPG",
  "EnableAllTuners": true,
  "EnableAllUsers": true
}
```

**3. Refrescar.** No existe `POST /LiveTv/RefreshGuide` util en 12.x. Se
disparan las tareas por su `Id` **completo**, leido de `GET /ScheduledTasks`:

```
POST /ScheduledTasks/Running/<id>    -> 204
  "Actualizar canales"   relee el M3U
  "Refresh Guide"        recarga el XMLTV
```

Espera ~60 s y comprueba `GET /LiveTv/Channels?Limit=0` → `TotalRecordCount`.

**4. Verificar reproduccion sin GUI.**

```
GET /LiveTv/Channels?limit=5            -> Items[].Id
GET /Videos/<channelId>/stream.ts       -> el TS (mide bytes)
```

**5. Cambiar de fuentes.** Edita `sources.yml`, luego:

```bash
curl -s -X POST http://10.0.0.21:8581/reload
```

y re-dispara "Actualizar canales" + "Refresh Guide" en Jellyfin.

## Configuracion

Todo en `.env` (ver `.env.example`) y en el bloque `settings:` de
`sources.yml`. Lo mas importante:

```
LVB_PUBLIC_BASE            URL base que se escribe en el M3U (alcanzable por Jellyfin)
LVB_PROBE_MODE             liveness (recomendado) | ffprobe
LVB_INCLUDE_PROTECTED      incluir cifrados/DRM marcados (true por defecto)
LVB_HWACCEL                auto | vaapi | qsv | cpu | off
LVB_MAX_TRANSCODES         limite de recodificaciones simultaneas sin GPU
LVB_HEALTH_INTERVAL_S      intervalo del health-check
LVB_HEALTH_FAILS_TO_DISABLE  fallos consecutivos antes de desactivar
```

Un valor invalido **avisa y usa el de fabrica**; nunca impide el arranque.

## Decisiones de diseno (y por que)

- **La validacion no se cree de ffprobe.** Un master HLS de plantilla contesta
  200 con un ladder falso y ffprobe lo reporta como vivo (medido: 38 de 40
  falsos positivos sobre un CDN muerto). El modo `liveness` baja el master,
  resuelve la variante y **jala el primer segmento**, exigiendo firma
  contenedor y tamano minimo.
- **Los protegidos se incluyen marcados, no se descartan en silencio.** Un
  canal con `EXT-X-KEY`, DRM o auth aparece en `/status` con la bandera y el
  motivo; asi el operador sabe por que no reproduce.
- **El M3U apunta al proxy, no al origen.** Eso mantiene tokens/credenciales
  del lado del servidor y da un punto donde aplicar el perfil de codec.
- **Los `tvg-id` del M3U son los mismos que los `<channel id>` del XMLTV.** El
  servicio escribe ambos artefactos, asi que el empate queda garantizado por
  construccion; ademas acepta mapeos explicitos hacia guias externas.
- **Copy por defecto.** Solo se recodifica si el codec lo exige (HEVC->H264,
  audio no-AAC->AAC) o si el perfil del canal lo pide.

## Limitaciones conocidas

- **Splicing de anuncios / `EXT-X-DISCONTINUITY`:** si el origen cambia de
  host a media emision, ffmpeg termina el flujo y Jellyfin lo lee como "el
  canal se acabo" (log: `Cannot reuse HTTP connection for different host`).
  Este servicio reconecta en la peticion siguiente, pero **no cose el corte
  dentro de la misma sesion**; para eso hace falta un bucle de buffer propio.
- **DRM/CENC:** no se reproduce. Detectado y marcado.
- **Sin GPU:** VAAPI/QSV solo si el host expone `/dev/dri` con driver i965
  funcional. En Intel HD 4000 el encode VAAPI es limitado; el plan cae a CPU.
- **Guia:** cada fuente usa su propio espacio de nombres. El empate por nombre
  es conservador (si es ambiguo, no empata) y los canales locales/regionales
  rara vez tienen guia publica.
- **Salud:** un origen puede callar varios segundos de forma normal; el
  umbral por defecto es 3 fallos consecutivos con backoff.

## Licencia

MIT — ver `LICENSE`. Aviso legal — ver `DISCLAIMER.md`.
