"""Relé HLS con colchón propio (etapa que faltaba).

El problema que resuelve
------------------------
`/stream/<id>` lanza `ffmpeg -c copy` contra el origen y entrega lo que salga.
Eso es un proxy sin memoria:

- **No hay colchón**: el hueco del origen (medido: 6.5 s) llega tal cual a la
  pantalla, y el silencio normal entre segmentos también.
- **No hay re-resolución**: ffmpeg fija la variante del master UNA vez. Si el
  proveedor rota el playlist variante (medido en Canal 5: nombre nuevo en cada
  lectura del master, la vieja da 404), ffmpeg muere y Jellyfin lo lee como fin
  de canal.

Diseño
------
Un hilo lector descarga el HLS por su cuenta y lo guarda en un colchón en
memoria, indexado por un contador PROPIO (no por el `EXT-X-MEDIA-SEQUENCE` del
origen, que retrocede al cambiar de variante). El cliente se sirve de ese
colchón, siempre a `COLCHON_SEGMENTOS` de distancia del frente de escritura:

- **no puede adelantarse** — no hay datos más allá del frente;
- **no puede atrasarse** — se le deja de servir cuando el colchón baja.

No se estima ningún ritmo, así que no puede frenar al cliente por perseguir un
objetivo inalcanzable (el fallo que costó la pausa al minuto en el despachador
viejo). Y **no se usa ffmpeg**: los segmentos ya son MPEG-TS y se concatenan
tal cual, así que no hay re-timestamping, ni reinicio de PTS en cero, ni
`-output_ts_offset` que hunda el caudal.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass
from typing import Iterator

from .util import get_logger

log = get_logger("relay")

# Reintentos del proveedor en cada descarga (los CDN FAST fallan a rachas).
DESCARGA_INTENTOS = 4
DESCARGA_ESPERA_S = 0.8
MANIFIESTO_MAX_BYTES = 2 * 1024 * 1024
SEGMENTO_MAX_BYTES = 16 * 1024 * 1024
SONDEO_S = 0.5                 # cada cuanto se relee el playlist
COLCHON_SEGMENTOS = 3          # distancia del cliente al frente de escritura
MAX_SEGMENTOS = 15             # ventana que se conserva en memoria
MAX_BYTES = 32 * 1024 * 1024   # tope de la ventana por canal
ARRANQUE_MAX_S = 3.0           # el primer byte tiene que salir antes que el sondeo de Jellyfin
CLIENTE_OLVIDO_S = 60.0        # un cliente que no avanza tanto tiempo se da por muerto
FALLOS_PARA_CAMBIAR = 3        # fallos consecutivos antes de saltar a la fuente alterna


@dataclass
class Segmento:
    """Un segmento descargado, listo para servir."""

    idx: int
    url: str
    datos: bytes
    dur: float


@dataclass
class RefSegmento:
    """Referencia a un segmento del playlist, antes de descargarlo."""

    url: str
    dur: float
    key_uri: str = ""     # URI de la llave AES-128 ("" = sin cifrar)
    iv: str = ""          # IV en hex; vacio = se usa el numero de secuencia
    seq: int = 0          # numero de secuencia (para el IV por omision)


def _hex_a_bytes(texto: str) -> str:
    return texto.strip().removeprefix("0x").removeprefix("0X").lower()


def _http(url: str, user_agent: str = "", timeout: float = 12.0, max_bytes: int = SEGMENTO_MAX_BYTES) -> bytes:
    """GET crudo con User-Agent opcional (urllib no lo pone por defecto)."""
    headers = {"Accept": "*/*"}
    if user_agent:
        headers["User-Agent"] = user_agent
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


class RelayHLS:
    """Lee un origen HLS y mantiene un colchón de segmentos reemisible."""

    def __init__(self, fuentes, user_agent: str = "", resolver=None) -> None:
        """``fuentes`` es la principal seguida de sus respaldos (en orden).

        - Discussion: si la principal deja de responder ``FALLOS_PARA_CAMBIAR``
          veces seguidas, el relé salta a la siguiente fuente. La ventana del
          colchón NO se toca: el índice del cliente es propio y monotónico, así
          que el flujo sigue aunque la fuente nueva traiga otra línea de tiempo
          (el reproductor puede dar un tirón en el salto; eso es el punto 3a).
        - ``resolver(idx)`` devuelve la URL viva de la fuente ``idx`` (para
          refrescar tokens como el de TV Azteca). Sin resolver, se usa la URL
          tal cual.
        """
        self.fuentes = [u for u in ([fuentes] if isinstance(fuentes, str) else fuentes) if u]
        if not self.fuentes:
            raise ValueError("el rele necesita al menos una fuente")
        self.resolver = resolver
        self.user_agent = user_agent
        self._indice = 0
        self._fallos = 0
        self._cambios = 0

        self._lock = threading.RLock()   # _podar() se llama con el lock tomado
        self._buf: "OrderedDict[int, Segmento]" = OrderedDict()
        self._vistos: set[str] = set()
        self._clientes: dict[int, tuple[int, float]] = {}   # id -> (idx, ultimo avance)

        self._variante = ""          # "" = aun hay que resolver el master
        self._usar_master = True     # el source_url es un master
        self._claves: dict[str, str] = {}   # URI de llave AES -> hex
        self._cifrados = 0           # segmentos descifrados (AES-128)
        self._refrescar_token = False   # el proximo resolver debe pedir token NUEVO
        self._frente = 0             # cuantos segmentos se han encolado
        self._primero = 1            # idx del segmento mas viejo en el buffer
        self._error = ""
        self._refrescos = 0          # veces que hubo que re-resolver el master
        self._descargas = 0
        self._stop = threading.Event()
        self._hilo: threading.Thread | None = None
        self._siguiente_cliente = 0

    # ------------------------------------------------------------------ lector
    def _leer_manifiesto(self, url: str) -> str:
        # Con respaldos disponibles y sin datos, no vale la pena reintentar: se
        # falla rapido para poder saltar de fuente dentro de la ventana de
        # sondeo de Jellyfin.
        intentos = 1 if (len(self.fuentes) > 1 and not self._buf) else DESCARGA_INTENTOS
        for intento in range(intentos):
            try:
                return _http(url, self.user_agent, max_bytes=MANIFIESTO_MAX_BYTES).decode("utf-8", "replace")
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 403, 410):
                    raise                       # ruta muerta: no se reintenta
                if intento + 1 >= intentos:
                    raise
            except Exception:
                if intento + 1 >= intentos:
                    raise
            time.sleep(DESCARGA_ESPERA_S * (intento + 1))
        raise RuntimeError("manifiesto ilegible")

    @staticmethod
    def _es_master(texto: str) -> bool:
        return "#EXT-X-STREAM-INF" in texto

    @staticmethod
    def _mejor_variante(texto: str, base: str = "") -> str:
        """URL de la variante de MAYOR bandwidth de un master.

        - Discussion: el master de TV Azteca trae 480p, 360p y 720p; quedarse
          con la primera servia 480p teniendo 720p disponible. Se elige la de
          mayor ancho de banda: si el cliente no puede con ella, Jellyfin
          transcodifica hacia abajo, que es lo que ya sabe hacer.
        """
        candidatos: list[tuple[int, str]] = []
        bw = 0
        for linea in texto.splitlines():
            linea = linea.strip()
            if linea.startswith("#EXT-X-STREAM-INF"):
                m = re.search(r"BANDWIDTH=(\d+)", linea)
                bw = int(m.group(1)) if m else 0
            elif linea and not linea.startswith("#"):
                candidatos.append((bw, urllib.parse.urljoin(base, linea)))
                bw = 0
        if not candidatos:
            return ""
        return max(candidatos, key=lambda par: par[0])[1]

    @staticmethod
    def _segmentos_del_playlist(texto: str, base: str) -> list["RefSegmento"]:
        """Refs de los segmentos de un playlist media, con su llave AES si la traen.

        - Important: ``#EXT-X-KEY`` aplica al segmento SIGUIENTE y puede cambiar
          en cada uno (medido en TV Azteca: una llave por segmento, cada una con
          su IV). Sin descifrar, el cliente recibe bytes cifrados y no decodifica
          nada: medido, ffprobe responde "Invalid data found".
        """
        dur = 0.0
        key_uri = ""
        iv = ""
        seq = 0
        salida: list[RefSegmento] = []
        for linea in texto.splitlines():
            linea = linea.strip()
            if linea.startswith("#EXT-X-KEY"):
                metodo = re.search(r"METHOD=([A-Za-z0-9-]+)", linea)
                uri = re.search(r'URI="([^"]+)"', linea)
                ivm = re.search(r"IV=0x([0-9A-Fa-f]+)", linea)
                if metodo and metodo.group(1).upper() != "NONE" and uri:
                    key_uri = urllib.parse.urljoin(base, uri.group(1))
                    iv = _hex_a_bytes(ivm.group(1)) if ivm else ""
                else:
                    key_uri = ""
                    iv = ""
            elif linea.startswith("#EXT-X-MEDIA-SEQUENCE"):
                try:
                    seq = int(linea.split(":", 1)[1])
                except (IndexError, ValueError):
                    seq = 0
            elif linea.startswith("#EXTINF"):
                try:
                    dur = float(linea.split(":", 1)[1].split(",")[0])
                except (IndexError, ValueError):
                    dur = 0.0
            elif linea and not linea.startswith("#"):
                salida.append(RefSegmento(urllib.parse.urljoin(base, linea), dur, key_uri, iv, seq))
                seq += 1
        return salida

    @property
    def source_url(self) -> str:
        """Fuente PRINCIPAL (identidad estable del relé para el pool)."""
        return self.fuentes[0]

    def _url_origen(self, forzar: bool = False) -> str:
        """URL viva de la fuente ACTIVA (con resolver, para refrescar tokens).

        - Note: ``forzar=True`` pide token NUEVO saltando la caché. Se usa tras
          un 401/403: el token cacheado ya murió y volver a pedirlo igual
          dejaría el canal muerto para siempre.
        """
        if self.resolver is not None:
            try:
                return self.resolver(self._indice, forzar=forzar)
            except TypeError:
                try:
                    return self.resolver(self._indice)      # resolver sin ``forzar``
                except Exception as exc:  # noqa: BLE001
                    log.error("relay: no se pudo resolver el origen", extra={"detail": str(exc)[:200]})
            except Exception as exc:  # noqa: BLE001 — un token caido no debe tumbarlo
                log.error("relay: no se pudo resolver el origen", extra={"detail": str(exc)[:200]})
        return self.fuentes[self._indice]

    def _contar_fallo(self) -> None:
        """Suma un fallo y, si son suficientes, salta a la fuente alterna.

        - Discussion: el umbral depende de si ya hay datos. Con el colchón
          VACÍO (esta fuente nunca entregó nada) se salta al PRIMER fallo: no
          hay nada que perder y esperar tres intentos deja al cliente sin
          primer byte dentro de la ventana de sondeo de Jellyfin (medido: 26 s
          de arranque con la principal muerta). Con el colchón lleno, en
          cambio, se pide ``FALLOS_PARA_CAMBIAR`` seguidos: un tropiezo suelto
          no justifica cambiar de fuente en mitad de una reproducción.
        """
        with self._lock:
            umbral = 1 if not self._buf else FALLOS_PARA_CAMBIAR
            if self._fallos >= umbral and len(self.fuentes) > 1:
                self._indice = (self._indice + 1) % len(self.fuentes)
                self._variante = ""
                self._vistos.clear()
                self._fallos = 0
                self._cambios += 1
                log.warning(
                    "relay: cambiando de fuente",
                    extra={"detail": f"indice={self._indice} {self.fuentes[self._indice][:70]}"},
                )
            else:
                self._fallos += 1

    def _resolver_variante(self, forzar: bool = False) -> str:
        """Lee el origen; si es master, baja a la mejor variante util."""
        actual = self._url_origen(forzar=forzar)
        for _ in range(3):
            texto = self._leer_manifiesto(actual)
            if not self._es_master(texto):
                self._usar_master = False
                return actual
            self._usar_master = True
            elegida = self._mejor_variante(texto, actual)
            if not elegida:
                raise RuntimeError("master sin variantes")
            actual = elegida
        raise RuntimeError("anidamiento de master mayor a 3")

    def _bucle(self) -> None:
        """Hilo lector: mantiene el colchón lleno."""
        while not self._stop.is_set():
            try:
                if not self._variante:
                    self._variante = self._resolver_variante(forzar=self._refrescar_token)
                    self._refrescar_token = False
                texto = self._leer_manifiesto(self._variante)
                if self._es_master(texto):
                    self._variante = self._resolver_variante()
                    continue
                pendientes = [
                    ref for ref in self._segmentos_del_playlist(texto, self._variante)
                    if ref.url not in self._vistos
                ]
                for ref in pendientes:
                    if self._stop.is_set():
                        return
                    datos = self._bajar_segmento(ref)
                    if datos is None:
                        continue
                    with self._lock:
                        self._vistos.add(ref.url)
                        self._frente += 1
                        self._buf[self._frente] = Segmento(self._frente, ref.url, datos, ref.dur)
                        self._descargas += 1
                        self._fallos = 0          # la fuente activa respondio
                        self._podar()
                    self._error = ""
                with self._lock:
                    self._error = ""
            except urllib.error.HTTPError as exc:
                # Ruta muerta o token vencido: se vuelve a resolver el origen.
                # 401 entra aqui: mdstrm (TV Azteca) responde 401 cuando el
                # access_token cacheado ya murio, y sin refrescarlo el canal no
                # se recupera nunca.
                if exc.code in (401, 403, 404, 410):
                    with self._lock:
                        self._refrescos += 1
                        self._variante = ""
                        self._refrescar_token = True
                    log.warning("relay: re-resolviendo origen", extra={"detail": f"HTTP {exc.code}"})
                else:
                    self._error = f"HTTP {exc.code}"
                self._contar_fallo()
            except Exception as exc:  # noqa: BLE001 — el lector nunca muere
                self._error = str(exc)[:200]
                self._contar_fallo()
            self._stop.wait(SONDEO_S)

    def _clave(self, uri: str) -> str:
        """Llave AES-128 en hex, cacheada por URI (no cambia entre segmentos)."""
        with self._lock:
            guardada = self._claves.get(uri)
        if guardada:
            return guardada
        cruda = _http(uri, self.user_agent, timeout=8.0, max_bytes=4096)
        if len(cruda) < 16:
            raise RuntimeError(f"llave AES de {len(cruda)} bytes (se esperaban 16)")
        clave = cruda[:16].hex()
        with self._lock:
            self._claves[uri] = clave
        return clave

    def _descifrar(self, datos: bytes, key_uri: str, iv: str, seq: int) -> bytes:
        """Descifra un segmento AES-128-CBC.

        - Discussion: la stdlib de Python no trae AES, pero ``openssl`` sí está
          en la imagen base. Se le pasa el segmento por stdin y devuelve el
          plano por stdout; openssl quita solo el relleno PKCS7.
        - Note: sin atributo ``IV`` en el tag, el IV es el número de secuencia
          del segmento en 128 bits big-endian (lo que dice el RFC de HLS).
        """
        clave = self._clave(key_uri)
        iv_hex = iv or f"{seq:032x}"
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-K", clave, "-iv", iv_hex],
            input=datos,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("openssl: " + proc.stderr.decode("utf-8", "replace").strip()[:150])
        return proc.stdout

    def _bajar_segmento(self, ref: "RefSegmento") -> bytes | None:
        for intento in range(DESCARGA_INTENTOS):
            try:
                crudo = _http(ref.url, self.user_agent)
                if not ref.key_uri:
                    return crudo
                plano = self._descifrar(crudo, ref.key_uri, ref.iv, ref.seq)
                with self._lock:
                    self._cifrados += 1
                return plano
            except urllib.error.HTTPError as exc:
                # 401/403 en un segmento = token vencido o UA bloqueado: se
                # propaga para que el lector re-resuelva la fuente entera.
                if exc.code in (401, 403, 404, 410):
                    raise
                self._error = str(exc)[:150]
                if intento + 1 >= DESCARGA_INTENTOS:
                    return None
                time.sleep(DESCARGA_ESPERA_S * (intento + 1))
            except Exception as exc:  # noqa: BLE001 — se reintenta o se abandona
                self._error = str(exc)[:150]
                if intento + 1 >= DESCARGA_INTENTOS:
                    return None
                time.sleep(DESCARGA_ESPERA_S * (intento + 1))
        return None

    def _podar(self) -> None:
        """Suelta lo viejo. La ventana tiene TOPE DURO, pase lo que pase.

        - Discussion: el tope NO respeta a un cliente atrasado. Si un cliente
          se queda muy atras (o su conexion murio sin avisar y su generador
          quedo colgado), respetarlo retendria la ventana entera y la memoria
          creceria sin limite — medido: 125 segmentos anclados por un cliente
          muerto. Al que se queda atras se le salta hacia adelante; ``servir``
          ya contempla que su idx desaparezca del buffer.
        """
        with self._lock:
            ahora = time.monotonic()
            for cid, (_idx, visto) in list(self._clientes.items()):
                if ahora - visto > CLIENTE_OLVIDO_S:
                    self._clientes.pop(cid, None)

            while len(self._buf) > MAX_SEGMENTOS:
                del self._buf[next(iter(self._buf))]

            total = sum(len(s.datos) for s in self._buf.values())
            while len(self._buf) > COLCHON_SEGMENTOS + 1 and total > MAX_BYTES:
                idx = next(iter(self._buf))
                total -= len(self._buf[idx].datos)
                del self._buf[idx]

            self._primero = next(iter(self._buf), self._frente + 1)

    # ----------------------------------------------------------------- control
    def arrancar(self) -> None:
        if self._hilo and self._hilo.is_alive():
            return
        self._stop.clear()
        self._hilo = threading.Thread(target=self._bucle, name="relay-hls", daemon=True)
        self._hilo.start()

    def detener(self) -> None:
        self._stop.set()
        if self._hilo:
            self._hilo.join(timeout=5)

    def vivo(self) -> bool:
        return bool(self._hilo and self._hilo.is_alive())

    def stats(self) -> dict:
        with self._lock:
            return {
                "frente": self._frente,
                "en_buffer": len(self._buf),
                "primero": self._primero,
                "clientes": len(self._clientes),
                "refrescos_master": self._refrescos,
                "segmentos_descargados": self._descargas,
                "error": self._error,
                "variante": self._variante.rsplit("/", 1)[-1],
                "fuente": self._indice,
                "fuentes": len(self.fuentes),
                "cambios_fuente": self._cambios,
                "descifrados_aes": self._cifrados,
            }

    # ---------------------------------------------------------------- cliente
    def servir(self, espera_max_s: float = ARRANQUE_MAX_S) -> Iterator[bytes]:
        """Genera el flujo continuo para UN cliente."""
        # El primer byte tiene que salir dentro de la ventana de sondeo de
        # Jellyfin (3 s): se arranca con lo que haya, no con el colchon lleno.
        limite = time.time() + espera_max_s
        while time.time() < limite:
            with self._lock:
                if self._buf:
                    break
            time.sleep(0.05)

        with self._lock:
            self._siguiente_cliente += 1
            cid = self._siguiente_cliente
            if self._buf:
                idx = max(self._primero, self._frente - COLCHON_SEGMENTOS)
            else:
                idx = self._frente + 1
            self._clientes[cid] = (idx, time.monotonic())
            colchon_inicial = max(1, self._frente - idx)
        log.info("relay: cliente enganchado", extra={"detail": f"idx={idx} frente={self._frente}"})

        try:
            while not self._stop.is_set():
                with self._lock:
                    frente = self._frente
                    primero = self._primero
                    tope = frente - colchon_inicial      # el cliente no se acerca mas
                    seg = self._buf.get(idx)
                    if idx < primero:
                        idx = primero                    # se quedo atras: se salta
                        self._clientes[cid] = (idx, time.monotonic())
                        seg = self._buf.get(idx)
                    if seg is not None:
                        self._clientes[cid] = (idx + 1, time.monotonic())
                        idx += 1
                if seg is None:
                    if idx > tope:
                        time.sleep(0.05)                 # colchon lleno: esperar
                        continue
                    time.sleep(0.1)                      # aun no hay datos: esperar
                    continue
                yield seg.datos
        finally:
            with self._lock:
                self._clientes.pop(cid, None)
            log.info("relay: cliente desenganchado", extra={"detail": f"idx={idx}"})


class RelayPool:
    """Un relé por canal, creado bajo demanda y apagado cuando queda solo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._relays: dict[str, RelayHLS] = {}

    def get(self, clave: str, fuentes, user_agent: str = "", resolver=None) -> RelayHLS:
        """Devuelve (creando si hace falta) el relé de un canal.

        - Discussion: ``fuentes`` es la principal + sus respaldos. La clave es
          el id del canal; si cambió la lista de fuentes (se re-cableó el
          catálogo o se agregó un respaldo) se descarta el relé viejo. El
          ``resolver`` se actualiza sin recrear, para que las re-resoluciones
          traigan token fresco en fuentes que lo rotan.
        """
        lista = [u for u in ([fuentes] if isinstance(fuentes, str) else fuentes) if u]
        with self._lock:
            relay = self._relays.get(clave)
            if relay is None or relay.fuentes != lista:
                if relay is not None:
                    relay.detener()
                relay = RelayHLS(lista, user_agent, resolver=resolver)
                relay.arrancar()
                self._relays[clave] = relay
            elif resolver is not None:
                relay.resolver = resolver
            return relay

    def stats(self) -> dict:
        with self._lock:
            return {k: r.stats() for k, r in self._relays.items()}

    def detener_todos(self) -> None:
        with self._lock:
            for relay in self._relays.values():
                relay.detener()
            self._relays.clear()
