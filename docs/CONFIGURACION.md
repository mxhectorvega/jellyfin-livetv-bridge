# Configuracion

La configuracion del servicio vive en dos lugares. El archivo principal es `config/sources.yml` (no se versiona; la plantilla es `config/sources.example.yml`). Ahi se declaran las fuentes de canales, los canales sueltos, la guia EPG y un bloque `settings:` con ajustes del pipeline. Las variables de entorno del archivo `.env` (copia de `.env.example`) son los valores por defecto globales: el bloque `settings:` de `sources.yml` puede pisarlos. Los nombres de variable coinciden con el prefijo `LVB_` que aparece en el codigo (`src/config.py`).

## Orden de precedencia

El catalogo visible lo resuelve `Settings.resolve_active_catalog()`. El orden es:

1. **`/data/active_catalog.txt`** — lo escribe el endpoint `POST /catalog/<nombre>`. Si existe y tiene contenido valido, **gana siempre** y sobrevive a reinicios.
2. **`settings.active_catalog`** — viene del bloque `settings:` de `sources.yml` (si existe y no es nulo).
3. **Variable de entorno `LVB_ACTIVE_CATALOG`** — el valor por defecto del `.env`.

Si el archivo en `/data` dice `todos`, `all` o `*`, se publica todo junto (catalogo vacio). Cualquier otro nombre filtra la salida M3U/EPG a ese catalogo aunque el pipeline alimente y valide todos.

## sources.yml

El archivo YAML tiene cinco secciones de primer nivel. Cada campo se lista con su tipo y proposito.

### `sources:` — listas M3U externas o propias

Una lista de objetos. Cada entrada descarga una playlist M3U y la normaliza.

```
name: str            # identificador humano; aparece en logs y /status
type: "m3u"          # unico valor soportado hoy
url: str             # HTTP(S) o ruta absoluta de archivo (/config/...)
enabled: bool        # true | false (default: true)
country: str         # codigo ISO (ej. "MX"), solo informativo
language: str        # codigo ISO (ej. "es"), solo informativo
default_group: str   # grupo para entradas que no traen group-title
catalog: str         # nombre del catalogo al que pertenece (ej. "pluto", "mi-lista")
headers: dict        # cabeceras HTTP extra para la descarga (opcional)
timeout_s: int       # timeout de descarga en segundos (opcional, default 20)
```

### `channels:` — canales sueltos con metadatos explicitos

Tienen prioridad sobre lo que venga en las listas `sources:`. Un canal aqui anula uno del mismo `tvg_id` traido por M3U.

```
name: str            # nombre visible en el M3U final
url: str             # master HLS (.m3u8) o DASH (.mpd); HTTP(S)
tvg_id: str          # id estable para emparejar con la guia EPG (requerido)
tvg_name: str        # nombre en la guia (opcional; default = name)
group: str           # grupo del canal (ej. "Deportes", "Noticias")
catalog: str         # OBLIGATORIO: catalogo al que pertenece (coincide con sources[i].catalog)
logo: str            # URL de logo (opcional)
country: str         # codigo ISO (opcional)
language: str        # codigo ISO (opcional)
profile: str         # "copy" | "h264" | "aac" | "auto" (opcional; default "auto")
```

### `respaldo:` — fuentes alternas por id de canal

Diccionario `tvg_id -> lista de URLs`. Cuando el rele detecta fallo en la principal, prueba estas en orden. No aparece en la plantilla pero lo soporta el codigo.

```
"tvg_id_del_canal":
  - "https://backup1.example/canal/master.m3u8"
  - "https://backup2.example/canal/index.m3u8"
```

### `epg:` — guia de programacion

```
refresh_hours: int   # cada cuantas horas volver a bajar las fuentes EPG (default 12)
sources:
  - name: str        # identificador de la fuente EPG
    url: str         # HTTP(S) a .xml o .xml.gz
    enabled: bool    # true | false
    country: str     # pais cubierto (informativo)
    mappings: dict   # tvg_id_nuestro -> id_en_la_guia_externa
                     # si vacio, el servicio empata por nombre normalizado
```

### `settings:` — ajustes del pipeline (pisan al .env)

Todos los campos son opcionales. Los nombres coinciden con los atributos de `Settings` (sin prefijo `LVB_`).

```
active_catalog: str           # catalogo visible al arranque (ver Orden de precedencia)
probe_timeout_s: float        # segundos de timeout por sondeo (1.0 - 600.0)
probe_workers: int            # hilos concurrentes de validacion (1 - 64)
probe_mode: str               # "liveness" | "ffprobe"
include_protected: bool       # incluir canales DRM/cifrados/auth (marcados)
health_interval_s: float      # intervalo de health-check por canal (5.0 - 86400.0)
health_fails_to_disable: int  # fallos consecutivos para desactivar (1 - 100)
health_oks_to_enable: int     # exitos consecutivos para reactivar (1 - 100)
health_backoff_base_s: float  # base de backoff exponencial (0.1 - 3600.0)
health_backoff_max_s: float   # tope de backoff (0.1 - 86400.0)
hwaccel: str                  # "auto" | "vaapi" | "qsv" | "cpu" | "off"
max_transcodes: int           # transcodificaciones simultaneas (0 - 64)
strip_patterns: list[str]     # regex para limpiar nombres de canal
group_exclude: list[str]      # regex ^...$ para descartar grupos enteros
```

## Variables de entorno

Todas las `LVB_*` que aparecen en `.env.example` y `src/config.py`. Agrupadas por tema. Cada bloque muestra: `NOMBRE=valor_por_defecto` — que hace.

### Servidor / Red

```
LVB_CONFIG_DIR=/config          # directorio de entrada (sources.yml, etc.); absoluto
LVB_DATA_DIR=/data              # directorio de salida (playlist.m3u, epg.xml, cache); absoluto
LVB_HOST=0.0.0.0                # interfaz HTTP donde escuchar (0.0.0.0 = todas)
LVB_PORT=8581                   # puerto HTTP (1 - 65535)
LVB_PUBLIC_BASE=                # base publica en URLs del M3U (ej. http://192.168.1.50:8581); vacio = se deduce de la peticion
LVB_BIND_IP=                    # IP concreta para bind; vacio = usa LVB_HOST (0.0.0.0)
LVB_API_TOKEN=                  # token para POST /catalog y POST /reload; vacio = sin auth
```

### Sondeo (validacion de canales)

```
LVB_PROBE_TIMEOUT_S=10          # timeout por sondeo individual en segundos (1.0 - 600.0)
LVB_PROBE_MODE=liveness         # "liveness" (baja bytes) | "ffprobe" (rapido, puede falso positivo)
LVB_PROBE_WORKERS=2             # hilos concurrentes (1 - 64); 2 medido contra CDN Pluto
LVB_INCLUDE_PROTECTED=true      # incluir canales DRM/cifrados/auth en la salida (marcados)
```

### Rele HLS (modo /relay/<id>)

Solo aplica si `LVB_RELAY_ENABLED=true`. El rele sirve cada canal con colchon propio, repara huecos y salta a fuente alterna.

```
LVB_RELAY_ENABLED=false         # activa el rele; el M3U publicara /relay/<id> en vez de /stream/<id>
LVB_RELAY_USER_AGENT=           # User-Agent para peticiones del rele; vacio = sin UA (evita 403 en mdstrm)
LVB_RELAY_REMENDAR=true         # coser PCR/PTS/DTS al cambiar de fuente (src/ts_splice.py)
LVB_RELAY_PASO_CUADRO=3003      # ticks 90 kHz entre fuentes (3003 @29.97fps, 3600 @25fps); 1 - 90000
LVB_RELAY_PRECALENTAR=true      # precalienta reles (primer byte ~0.02s); sostiene ~330 KB/s por canal
LVB_RELAY_PRECALENTAR_MAX=16    # maximo reles precalentados a la vez (0 - 4096); evita saturacion con Pluto (198 canales)
LVB_RELAY_IDLE_S=900            # segundos sin clientes para apagar rele YA USADO (0 - 86400); precalentados no cuentan
```

### Salud (resiliencia de canales)

Controlan el health-check periodico que desactiva/reactiva canales.

```
LVB_HEALTH_INTERVAL_S=300       # intervalo entre chequeos por canal en segundos (5.0 - 86400.0)
LVB_HEALTH_FAILS_TO_DISABLE=3   # fallos seguidos para marcar canal como caido (1 - 100)
LVB_HEALTH_OKS_TO_ENABLE=2      # exitos seguidos para reactivar (1 - 100)
LVB_HEALTH_BACKOFF_BASE_S=5     # base backoff exponencial tras fallo (0.1 - 3600.0)
LVB_HEALTH_BACKOFF_MAX_S=120    # tope de backoff (0.1 - 86400.0)
```

### Registro / Logs

```
LVB_LOG_LEVEL=INFO              # DEBUG | INFO | WARNING | ERROR
LVB_LOG_FORMAT=json             # "json" (estructurado) | "text" (humano)
```

### Transcodificacion

```
LVB_HWACCEL=auto                # "auto" | "vaapi" | "qsv" | "cpu" | "off" (off = siempre -c copy)
LVB_MAX_TRANSCODES=2            # transcodificaciones simultaneas (0 - 64); limite CPU sin GPU
```

## Como se valida

Toda conversion de tipo y rango pasa por funciones internas de `src/config.py`:

- `_parse(raw, caster, default, name)` — intenta castear; si falla **avisa por stderr** y usa el valor de fabrica.
- `_range(raw, caster, default, name, min, max)` — igual que `_parse` mas validacion de rango; fuera de rango **avisa y usa el de fabrica**.
- `_to_bool(raw)` — booleano estricto: solo acepta `1/true/yes/si/on` o `0/false/no/off`; cualquier otra cosa **lanza ValueError** que `_parse` atrapa y convierte en aviso + valor de fabrica.

**Regla invariable:** un valor invalido **nunca impide el arranque**. El servicio levanta con el valor de fabrica y el aviso queda en el log.

Existe una prueba automatizada que compara lo que declaran `pyproject.toml` y `requirements.txt` para detectar dependencias desincronizadas.
