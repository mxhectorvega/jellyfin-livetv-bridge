"""Configuracion central del servicio.

- Discussion: toda la configuracion cabe en :class:`Settings`. Las rutas son
  ABSOLUTAS siempre (nunca relativas al CWD): ``/config`` para la entrada y
  ``/data`` para la salida. Los valores se leen del entorno (``.env``) y
  pueden ser pisados por el bloque ``settings:`` de ``sources.yml``.
- Important: un valor invalido NUNCA tumba el arranque. Se avisa por el log y
  se usa el valor de fabrica. Un servicio que no levanta a las 3 de la manana
  porque una variable venia mal escrita es peor que un servicio con el valor
  por omision.
- Note: :meth:`Settings.redacted` devuelve el mapa de configuracion SIN
  valores de credenciales, para poder loguearlo sin filtrar secretos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

# --------------------------------------------------------------------------
# Valores de fabrica. Medidos en produccion cuando aplica; si no, el candidato
# razonable mas conservador.
# --------------------------------------------------------------------------
DEFAULT_CONFIG_DIR = "/config"
DEFAULT_DATA_DIR = "/data"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8581
DEFAULT_PUBLIC_BASE = ""          # vacio => se deduce de la peticion
DEFAULT_PROBE_TIMEOUT_S = 10.0
# 2, no 6: medido contra el CDN de Pluto, la concurrencia alta hace que el CDN
# devuelva manifiestos VACIOS (1/12 con 6 hilos contra 22/24 con 2). Un barrido
# con hilos de mas no es un barrido mas rapido, es un barrido que miente.
DEFAULT_PROBE_WORKERS = 2
DEFAULT_PROBE_MODE = "liveness"   # liveness | ffprobe
DEFAULT_INCLUDE_PROTECTED = True
DEFAULT_PLAYLIST_NAME = "playlist.m3u"
DEFAULT_EPG_NAME = "epg.xml"
DEFAULT_HWACCEL = "auto"          # auto | vaapi | qsv | cpu | off
DEFAULT_MAX_TRANSCODES = 2
DEFAULT_RELAY_ENABLED = False     # el rele convive con /stream; se activa a proposito
# Sin User-Agent explicito por omision: mdstrm (TV Azteca) responde 403 a los
# UA de navegador en sus segmentos (medido 2026-10-11) y los otros 9 nacionales
# funcionan igual con o sin UA. Cuando entre Pluto habra que darle el suyo.
DEFAULT_RELAY_UA = ""
DEFAULT_HEALTH_INTERVAL_S = 300.0
DEFAULT_HEALTH_FAILS = 3
DEFAULT_HEALTH_OKS = 2
DEFAULT_BACKOFF_BASE_S = 5.0
DEFAULT_BACKOFF_MAX_S = 120.0
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_FORMAT = "json"       # json | text


def _warn(msg: str) -> None:
    """Aviso de configuracion sin depender del logger (corre antes de armarlo)."""
    import sys

    print(f"[config] {msg}", file=sys.stderr)


def _env_str(name: str, default: str) -> str:
    raw = os.environ.get(name)
    return default if raw is None or raw.strip() == "" else raw.strip()


def _parse(raw: Any, caster: Callable[[Any], Any], default: Any, name: str) -> Any:
    """Convierte un valor, avisando y usando el de fabrica si no es valido.

    - Discussion: la regla del proyecto es que la configuracion invalida avisa
      y NO tumba el arranque. Aqui vive ese comportamiento para todos los
      tipos (int, float, bool, str).
    """
    if raw is None:
        return default
    try:
        return caster(raw)
    except (TypeError, ValueError):
        _warn(f"{name}={raw!r} invalido; se usa el valor por omision {default!r}")
        return default


def _range(raw: Any, caster: Callable[[Any], Any], default: Any, name: str,
           minimo: float, maximo: float) -> Any:
    """Como :func:`_parse` pero ademas exige un RANGO utilizable.

    - Discussion: validar el tipo no basta. Medido: ``LVB_PORT=99999`` es un
      ``int`` valido y revienta al hacer bind con ``OverflowError: port must be
      0-65535``; el pipeline arranca pero la API no levanta, y el fallo es
      silencioso. Igual con negativos en ``relay_idle_s`` (apaga reles recien
      usados) o fuera de rango en ``probe_workers``.
    - Important: fuera de rango se AVISA y se usa el valor de fabrica. Nunca se
      aborta el arranque: es la misma regla que el resto de la configuracion.
    """
    valor = _parse(raw, caster, default, name)
    if valor < minimo or valor > maximo:
        _warn(f"{name}={valor!r} fuera de rango [{minimo}, {maximo}]; "
              f"se usa el valor por omision {default!r}")
        return default
    return valor


def _to_bool(raw: Any) -> bool:
    """Booleano estricto: solo acepta si/no/true/false/1/0.

    - Important: un valor que no es ni si ni no se RECHAZA; no se toma como
      verdadero en silencio. Un booleano mal interpretado deja un arreglo
      apagado sin que nadie lo note.
    """
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text in {"1", "true", "yes", "si", "sí", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"booleano invalido: {raw!r}")


@dataclass
class Settings:
    """Configuracion efectiva del servicio (rutas absolutas garantizadas)."""

    config_dir: str = DEFAULT_CONFIG_DIR
    data_dir: str = DEFAULT_DATA_DIR
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    public_base: str = DEFAULT_PUBLIC_BASE

    probe_timeout_s: float = DEFAULT_PROBE_TIMEOUT_S
    probe_workers: int = DEFAULT_PROBE_WORKERS
    probe_mode: str = DEFAULT_PROBE_MODE
    include_protected: bool = DEFAULT_INCLUDE_PROTECTED

    playlist_name: str = DEFAULT_PLAYLIST_NAME
    epg_name: str = DEFAULT_EPG_NAME
    # Catalogo visible: vacio = todos juntos. Con valor (nacionales|pluto|vix)
    # el M3U/EPG publicados salen SOLO de ese catalogo, aunque el pipeline
    # alimente y valide los tres.
    active_catalog: str = ""

    hwaccel: str = DEFAULT_HWACCEL
    max_transcodes: int = DEFAULT_MAX_TRANSCODES
    # Rele HLS con colchon propio: cuando esta activo, el M3U publicado
    # apunta a /relay/<id> en vez de /stream/<id>.
    relay_enabled: bool = DEFAULT_RELAY_ENABLED
    relay_user_agent: str = DEFAULT_RELAY_UA
    # Remiendo de la linea de tiempo al cambiar de fuente (src/ts_splice.py):
    # desplaza PCR/PTS/DTS de la fuente nueva para que continue a la vieja. Sin
    # cambio de fuente no escribe un byte (solo lee), pero se puede apagar.
    relay_remendar: bool = True
    # Duracion de un cuadro en ticks de 90 kHz: 3003 a 29.97 fps, 3600 a 25.
    relay_paso_cuadro: int = 3003

    # IP concreta donde se ESCUCHA (vacio = todas, 0.0.0.0). El .env.example la
    # documentaba como LVB_BIND_IP desde el principio, pero el codigo nunca la
    # leia: el servicio escuchaba en 0.0.0.0 aunque el archivo prometiera lo
    # contrario. Aqui se hace real.
    bind_ip: str = ""
    # Token opcional para los endpoints que CAMBIAN estado (POST /catalog,
    # POST /reload). Vacio = sin autenticacion (comportamiento historico). Los
    # GET se dejan abiertos a proposito: Jellyfin los consume sin cabeceras.
    api_token: str = ""
    # Precalentado del rele: deja el primer byte en ~0.02 s, pero sostiene
    # ~330 KB/s de bajada continua POR CANAL. Medido: 10 canales = 26 Mbps. Sin
    # tope, cambiar al catalogo de Pluto abre 198 lectores (~523 Mbps) y satura
    # el enlace. `relay_precalentar_max` corta la lista; el resto arranca
    # on-demand cuando alguien lo pide.
    relay_precalentar: bool = True
    relay_precalentar_max: int = 16
    # Un rele que YA tuvo clientes y lleva este tiempo sin ninguno se apaga. Los
    # que nunca se usaron (precalentado) no entran aqui: ese es el punto del
    # precalentado.
    relay_idle_s: float = 900.0

    health_interval_s: float = DEFAULT_HEALTH_INTERVAL_S
    health_fails_to_disable: int = DEFAULT_HEALTH_FAILS
    health_oks_to_enable: int = DEFAULT_HEALTH_OKS
    backoff_base_s: float = DEFAULT_BACKOFF_BASE_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S

    log_level: str = DEFAULT_LOG_LEVEL
    log_format: str = DEFAULT_LOG_FORMAT

    # Fuente de TV Azteca integrada: las URLs del catalogo que apuntan al
    # proveedor local de token se resuelven dentro del bridge.
    azteca_enabled: bool = True
    azteca_local_base: str = "http://127.0.0.1:8582"

    # Patrones de limpieza y filtros, configurables desde sources.yml.
    strip_patterns: list[str] = field(default_factory=list)
    group_exclude: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Rutas derivadas (absolutas)
    # ------------------------------------------------------------------
    @property
    def config_path(self) -> Path:
        return Path(self.config_dir).resolve()

    @property
    def data_path(self) -> Path:
        return Path(self.data_dir).resolve()

    @property
    def sources_file(self) -> Path:
        return self.config_path / "sources.yml"

    @property
    def playlist_file(self) -> Path:
        return self.data_path / self.playlist_name

    @property
    def epg_file(self) -> Path:
        return self.data_path / self.epg_name

    @property
    def state_file(self) -> Path:
        return self.data_path / "state.json"

    @property
    def active_catalog_file(self) -> Path:
        """Selector persistido del catalogo visible (sobrevive reinicios)."""
        return self.data_path / "active_catalog.txt"

    def resolve_active_catalog(self) -> str:
        """Catalogo visible efectivo.

        - Discussion: gana lo que haya elegido el usuario por API (archivo en
          /data); si no hay seleccion, se usa el valor del entorno. Asi el
          .env solo fija el valor inicial y ``POST /catalog/<nombre>`` siempre
          surte efecto.
        - Returns: nombre del catalogo, o "" cuando se publican todos juntos.
        """
        try:
            saved = self.active_catalog_file.read_text(encoding="utf-8").strip().lower()
        except OSError:
            saved = ""
        if saved:
            return "" if saved in ("todos", "all", "*") else saved
        return (self.active_catalog or "").strip().lower()

    # ------------------------------------------------------------------
    # Construccion
    # ------------------------------------------------------------------
    @classmethod
    def from_env(cls, overrides: dict[str, Any] | None = None) -> "Settings":
        """Lee el entorno y aplica ``overrides`` (bloque settings de sources.yml)."""
        env = os.environ
        overrides = {k: v for k, v in (overrides or {}).items() if v is not None}
        get = lambda name, default: overrides.get(name, env.get(name, default))  # noqa: E731

        return cls(
            config_dir=str(get("config_dir", _env_str("LVB_CONFIG_DIR", DEFAULT_CONFIG_DIR))),
            data_dir=str(get("data_dir", _env_str("LVB_DATA_DIR", DEFAULT_DATA_DIR))),
            host=str(get("host", _env_str("LVB_HOST", DEFAULT_HOST))),
            port=_range(get("port", _env_str("LVB_PORT", str(DEFAULT_PORT))), int, DEFAULT_PORT, "port", 1, 65535),
            public_base=str(get("public_base", _env_str("LVB_PUBLIC_BASE", DEFAULT_PUBLIC_BASE))).rstrip("/"),
            probe_timeout_s=_range(get("probe_timeout_s", _env_str("LVB_PROBE_TIMEOUT_S", str(DEFAULT_PROBE_TIMEOUT_S))), float, DEFAULT_PROBE_TIMEOUT_S, "probe_timeout_s", 1.0, 600.0),
            probe_workers=_range(get("probe_workers", _env_str("LVB_PROBE_WORKERS", str(DEFAULT_PROBE_WORKERS))), int, DEFAULT_PROBE_WORKERS, "probe_workers", 1, 64),
            probe_mode=str(get("probe_mode", _env_str("LVB_PROBE_MODE", DEFAULT_PROBE_MODE))).strip().lower(),
            include_protected=_parse(get("include_protected", _env_str("LVB_INCLUDE_PROTECTED", str(DEFAULT_INCLUDE_PROTECTED))), _to_bool, DEFAULT_INCLUDE_PROTECTED, "include_protected"),
            playlist_name=str(get("playlist_name", _env_str("LVB_PLAYLIST_NAME", DEFAULT_PLAYLIST_NAME))),
            epg_name=str(get("epg_name", _env_str("LVB_EPG_NAME", DEFAULT_EPG_NAME))),
            active_catalog=str(get("active_catalog", _env_str("LVB_ACTIVE_CATALOG", ""))).strip().lower(),
            hwaccel=str(get("hwaccel", _env_str("LVB_HWACCEL", DEFAULT_HWACCEL))).strip().lower(),
            max_transcodes=_range(get("max_transcodes", _env_str("LVB_MAX_TRANSCODES", str(DEFAULT_MAX_TRANSCODES))), int, DEFAULT_MAX_TRANSCODES, "max_transcodes", 0, 64),
            relay_enabled=_parse(get("relay_enabled", _env_str("LVB_RELAY_ENABLED", str(DEFAULT_RELAY_ENABLED))), _to_bool, DEFAULT_RELAY_ENABLED, "relay_enabled"),
            relay_user_agent=str(get("relay_user_agent", _env_str("LVB_RELAY_USER_AGENT", DEFAULT_RELAY_UA))),
            relay_remendar=_parse(get("relay_remendar", _env_str("LVB_RELAY_REMENDAR", str(True))), _to_bool, True, "relay_remendar"),
            relay_paso_cuadro=_range(get("relay_paso_cuadro", _env_str("LVB_RELAY_PASO_CUADRO", "3003")), int, 3003, "relay_paso_cuadro", 1, 90000),
            bind_ip=str(get("bind_ip", _env_str("LVB_BIND_IP", ""))).strip(),
            api_token=str(get("api_token", _env_str("LVB_API_TOKEN", ""))).strip(),
            relay_precalentar=_parse(get("relay_precalentar", _env_str("LVB_RELAY_PRECALENTAR", str(True))), _to_bool, True, "relay_precalentar"),
            relay_precalentar_max=_range(get("relay_precalentar_max", _env_str("LVB_RELAY_PRECALENTAR_MAX", "16")), int, 16, "relay_precalentar_max", 0, 4096),
            relay_idle_s=_range(get("relay_idle_s", _env_str("LVB_RELAY_IDLE_S", "900")), float, 900.0, "relay_idle_s", 0.0, 86400.0),
            health_interval_s=_range(get("health_interval_s", _env_str("LVB_HEALTH_INTERVAL_S", str(DEFAULT_HEALTH_INTERVAL_S))), float, DEFAULT_HEALTH_INTERVAL_S, "health_interval_s", 5.0, 86400.0),
            health_fails_to_disable=_range(get("health_fails_to_disable", _env_str("LVB_HEALTH_FAILS_TO_DISABLE", str(DEFAULT_HEALTH_FAILS))), int, DEFAULT_HEALTH_FAILS, "health_fails_to_disable", 1, 100),
            health_oks_to_enable=_range(get("health_oks_to_enable", _env_str("LVB_HEALTH_OKS_TO_ENABLE", str(DEFAULT_HEALTH_OKS))), int, DEFAULT_HEALTH_OKS, "health_oks_to_enable", 1, 100),
            backoff_base_s=_range(get("backoff_base_s", _env_str("LVB_HEALTH_BACKOFF_BASE_S", str(DEFAULT_BACKOFF_BASE_S))), float, DEFAULT_BACKOFF_BASE_S, "backoff_base_s", 0.1, 3600.0),
            backoff_max_s=_range(get("backoff_max_s", _env_str("LVB_HEALTH_BACKOFF_MAX_S", str(DEFAULT_BACKOFF_MAX_S))), float, DEFAULT_BACKOFF_MAX_S, "backoff_max_s", 0.1, 86400.0),
            log_level=str(get("log_level", _env_str("LVB_LOG_LEVEL", DEFAULT_LOG_LEVEL))).upper(),
            log_format=str(get("log_format", _env_str("LVB_LOG_FORMAT", DEFAULT_LOG_FORMAT))).strip().lower(),
            azteca_enabled=_parse(get("azteca_enabled", _env_str("LVB_AZTECA_ENABLED", str(True))), _to_bool, True, "azteca_enabled"),
            azteca_local_base=str(get("azteca_local_base", _env_str("LVB_AZTECA_BASE", "http://127.0.0.1:8582"))).rstrip("/"),
            strip_patterns=list(get("strip_patterns", []) or []),
            group_exclude=list(get("group_exclude", []) or []),
        )

    # ------------------------------------------------------------------
    def ensure_dirs(self) -> None:
        """Crea /config y /data si faltan (idempotente)."""
        self.config_path.mkdir(parents=True, exist_ok=True)
        self.data_path.mkdir(parents=True, exist_ok=True)

    def redacted(self) -> dict[str, Any]:
        """Configuracion sin secretos, apta para loguear o exponer en /status."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if not any(s in f.name for s in ("password", "token", "secret", "key"))
        }
