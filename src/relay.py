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

- **no puede adelantarse al frente** — no hay datos más allá de lo ya leído;
- **el ritmo lo pone el consumidor**, no el relé: no se estima ningún caudal
  objetivo, así que no puede frenar al cliente por perseguir un valor
  inalcanzable (el fallo que costó la pausa al minuto en el despachador viejo).
  La contrapresión del socket mantiene al lector por delante.

Y **no se usa ffmpeg**: los segmentos ya son MPEG-TS y se concatenan tal cual,
así que no hay re-timestamping, ni reinicio de PTS en cero, ni
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
from collections.abc import Iterator
from dataclasses import dataclass

from .ts_mux import muxear
from .ts_splice import Remendador, primer_pts
from .util import get_logger

log = get_logger("relay")

# Reintentos del proveedor en cada descarga (los CDN FAST fallan a rachas).
DESCARGA_INTENTOS = 4
DESCARGA_ESPERA_S = 0.8
MANIFIESTO_MAX_BYTES = 2 * 1024 * 1024
SEGMENTO_MAX_BYTES = 16 * 1024 * 1024
SONDEO_S = 0.5  # cada cuanto se relee el playlist
COLCHON_SEGMENTOS = 3  # distancia del cliente al frente de escritura
MAX_SEGMENTOS = 15  # ventana que se conserva en memoria
MAX_BYTES = 32 * 1024 * 1024  # tope de la ventana por canal
ARRANQUE_MAX_S = 3.0  # el primer byte tiene que salir antes que el sondeo de Jellyfin
CLIENTE_OLVIDO_S = 60.0  # un cliente que no avanza tanto tiempo se da por muerto
FALLOS_PARA_CAMBIAR = 3  # fallos consecutivos antes de saltar a la fuente alterna
# Memoria de "ya bajado": basta recordar la ventana viva. Era un `set` sin tope
# que crecia toda la vida del proceso (~30k URLs/dia en fuentes que rotan el
# nombre de la variante: medido en Canal 5, 2,486 en 2 h -> ~2.75 GB/año).
MAX_VISTOS = 512
MAX_CLAVES = 64  # llaves AES en cache (URI -> hex), tambien LRU
# Tope de espera entre vueltas del lector cuando la fuente no responde: sin el,
# un origen muerto con UNA sola fuente se reintentaba cada ~0.5 s para siempre.
REINTENTO_MAX_ESPERA_S = 30.0


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
    key_uri: str = ""  # URI de la llave AES-128 ("" = sin cifrar)
    iv: str = ""  # IV en hex; vacio = se usa el numero de secuencia
    seq: int = 0  # numero de secuencia (para el IV por omision)


def _hex_a_bytes(texto: str) -> str:
    return texto.strip().removeprefix("0x").removeprefix("0X").lower()


def _http(
    url: str, user_agent: str = "", timeout: float = 12.0, max_bytes: int = SEGMENTO_MAX_BYTES
) -> bytes:
    """GET crudo con User-Agent opcional (urllib no lo pone por defecto)."""
    headers = {"Accept": "*/*"}
    if user_agent:
        headers["User-Agent"] = user_agent
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read(max_bytes)


class RelayHLS:
    """Lee un origen HLS y mantiene un colchón de segmentos reemisible."""

    def __init__(
        self,
        fuentes,
        user_agent: str = "",
        resolver=None,
        nombre: str = "",
        remendar: bool = True,
        paso_cuadro: int = 3003,
    ) -> None:
        """``fuentes`` es la principal seguida de sus respaldos (en orden).

        - Discussion: si la principal deja de responder ``FALLOS_PARA_CAMBIAR``
          veces seguidas, el relé salta a la siguiente fuente. La ventana del
          colchón NO se toca: el índice del cliente es propio y monotónico, así
          que el flujo sigue aunque la fuente nueva traiga otra línea de tiempo
          (el reproductor puede dar un tirón en el salto; eso es el punto 3a).
        - ``resolver(idx)`` devuelve la URL viva de la fuente ``idx`` (para
          refrescar URLs firmadas o tokens de la fuente). Sin resolver, se usa la
          URL tal cual.
        - ``nombre`` es el id del canal y va en TODAS las líneas de log. Sin él,
          un "re-resolviendo origen HTTP 404" repetido no dice QUÉ canal está
          caído: medido en producción, varias horas de ruido sin saber a quién.
        """
        self.nombre = nombre
        self.fuentes = [u for u in ([fuentes] if isinstance(fuentes, str) else fuentes) if u]
        if not self.fuentes:
            raise ValueError("el rele necesita al menos una fuente")
        self.resolver = resolver
        self.user_agent = user_agent
        self._indice = 0
        self._fallos = 0
        self._cambios = 0

        self._lock = threading.RLock()  # _podar() se llama con el lock tomado
        self._buf: OrderedDict[int, Segmento] = OrderedDict()
        # LRU acotado, no un set sin tope: ver MAX_VISTOS.
        self._vistos: OrderedDict[str, None] = OrderedDict()
        self._clientes: dict[int, tuple[int, float]] = {}  # id -> (idx, ultimo avance)
        self._tuvo_clientes = False  # distingue precalentado de "quedo ocioso"
        self._ultimo_cliente = time.monotonic()

        self._variante = ""  # "" = aun hay que resolver el master
        self._usar_master = True  # el source_url es un master
        self._claves: OrderedDict[str, str] = OrderedDict()  # URI de llave AES -> hex
        self._cifrados = 0  # segmentos descifrados (AES-128)
        self._refrescar_token = False  # el proximo resolver debe pedir token NUEVO
        self._frente = 0  # cuantos segmentos se han encolado
        self._primero = 1  # idx del segmento mas viejo en el buffer
        self._error = ""
        self._refrescos = 0  # veces que hubo que re-resolver el master
        self._descargas = 0
        self._espera_s = SONDEO_S  # espera entre vueltas del lector (backoff)
        # Remiendo de la linea de tiempo (ver src/ts_splice.py). Con una sola
        # fuente, o antes del primer cambio, `procesar` solo LEE: no escribe un
        # byte mientras el delta sea 0. El desplazamiento se calcula nada mas
        # cuando el segmento que llega viene de una fuente distinta a la del
        # ultimo que se guardo.
        self.remendar = remendar
        self.paso_cuadro = paso_cuadro
        self._rem = Remendador()
        self._fuente_de_datos = self._indice
        self._remiendos = 0
        # Rendicion de audio aparte (EXT-X-MEDIA:TYPE=AUDIO). Si el master la
        # trae, cada segmento se muxe con su gemelo de audio antes de entrar al
        # colchon: sin esto el 34% del catalogo de Pluto se oye mudo.
        self._audio_pl = ""
        self._muxeados = 0
        self._sin_audio = 0
        # PID que se le dio al audio muxeado. Se recuerda para que TODOS los
        # segmentos del canal lo compartan: si se eligiera de nuevo en cada uno,
        # el PID cambiaria entre segmentos y un reproductor que no relea la PSI
        # perderia el audio al cambiar. 0 = todavia sin elegir.
        self._pid_audio = 0
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
                return _http(url, self.user_agent, max_bytes=MANIFIESTO_MAX_BYTES).decode(
                    "utf-8", "replace"
                )
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 403, 410):
                    raise  # ruta muerta: no se reintenta
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

        - Discussion: un master suele traer varias calidades; quedarse con la
          primera servia 480p teniendo 720p disponible. Se elige la de
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
    def _segmentos_del_playlist(texto: str, base: str) -> list[RefSegmento]:
        """Refs de los segmentos de un playlist media, con su llave AES si la traen.

        - Important: ``#EXT-X-KEY`` aplica al segmento SIGUIENTE y puede cambiar
          en cada uno (medido: una llave por segmento, cada una con
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
                    return self.resolver(self._indice)  # resolver sin ``forzar``
                except Exception as exc:  # noqa: BLE001
                    log.error(
                        "relay: no se pudo resolver el origen",
                        extra={"channel": self.nombre, "detail": str(exc)[:200]},
                    )
            except Exception as exc:  # noqa: BLE001 — un token caido no debe tumbarlo
                log.error(
                    "relay: no se pudo resolver el origen",
                    extra={"channel": self.nombre, "detail": str(exc)[:200]},
                )
        return self.fuentes[self._indice]

    def _contar_fallo(self) -> None:
        """Suma un fallo y, si son suficientes, salta a la fuente alterna.

        - Discussion: el umbral depende de si ya hay datos. Con el colchón
          VACÍO (esta fuente nunca entregó nada) se salta al PRIMER fallo: no
          hay nada que perder y esperar deja al cliente sin primer byte dentro
          de la ventana de sondeo de Jellyfin. Con el colchón lleno se piden
          ``FALLOS_PARA_CAMBIAR`` seguidos: un tropiezo suelto no justifica
          cambiar de fuente en mitad de una reproducción.
        - Important: el contador de ESTA llamada entra en la cuenta. La version
          anterior comparaba ``self._fallos >= umbral`` antes de incrementar, asi
          que en la practica cambiaba al SEGUNDO fallo (doc: primero) y al
          CUARTO con colchon lleno (doc: tres). Medido con la simulacion.
        """
        with self._lock:
            umbral = 1 if not self._buf else FALLOS_PARA_CAMBIAR
            if self._fallos + 1 >= umbral and len(self.fuentes) > 1:
                self._indice = (self._indice + 1) % len(self.fuentes)
                self._variante = ""
                self._vistos.clear()
                self._fallos = 0
                self._cambios += 1
                log.warning(
                    "relay: cambiando de fuente",
                    extra={
                        "channel": self.nombre,
                        "detail": f"indice={self._indice} {self.fuentes[self._indice][:70]}",
                    },
                )
            else:
                self._fallos += 1

    @staticmethod
    def _rendicion_audio(master: str, base: str) -> str:
        """URI de la rendicion de audio aparte, si el master la declara.

        - Discussion: medido en Pluto, **66 de 197 canales** publican el audio en
          su propia playlist y la variante solo lo referencia con
          ``AUDIO="audio"``::

              #EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",URI="audio/.../audio.m3u8"
              #EXT-X-STREAM-INF:...,AUDIO="audio"

          El rele bajaba solo la variante de video, asi que servia imagen sin
          sonido. ffprobe si veia el audio (resuelve el master completo) y por
          eso ``/status`` decia ``audio: aac`` en canales que se oian mudos.
        - Returns: la URL de la playlist de audio, o ``""`` si no hay.
        """
        for linea in master.splitlines():
            if linea.startswith("#EXT-X-MEDIA") and "TYPE=AUDIO" in linea:
                coincidencia = re.search(r'URI="([^"]+)"', linea)
                if coincidencia:
                    return urllib.parse.urljoin(base, coincidencia.group(1))
        return ""

    def _resolver_variante(self, forzar: bool = False) -> str:
        """Lee el origen; si es master, baja a la mejor variante util."""
        actual = self._url_origen(forzar=forzar)
        # Se recalcula en cada re-resolucion, pero NO se borra al bajar a la
        # variante: si se borrara ahi, se perderia la rendicion de audio que el
        # master acaba de declarar (medido: el rele servia mudo con
        # `rendicion=False` en un canal que si la trae).
        self._audio_pl = ""
        for _ in range(3):
            texto = self._leer_manifiesto(actual)
            if not self._es_master(texto):
                self._usar_master = False
                return actual
            self._usar_master = True
            encontrada = self._rendicion_audio(texto, actual)
            if encontrada:
                self._audio_pl = encontrada
            elegida = self._mejor_variante(texto, actual)
            if not elegida:
                raise RuntimeError("master sin variantes")
            actual = elegida
        raise RuntimeError("anidamiento de master mayor a 3")

    def _refs_audio(self) -> dict[int, RefSegmento]:
        """Segmentos de la playlist de audio, por su posicion.

        - Discussion: el audio y el video de un mismo canal vienen alineados
          (medido: mismo numero de segmentos, duraciones con 13 ms de
          diferencia), asi que se empareja por POSICION dentro de la playlist.
          El numero de secuencia NO sirve para emparejar: medido, difieren en 1.
        """
        if not self._audio_pl:
            return {}
        try:
            texto = self._leer_manifiesto(self._audio_pl)
        except Exception as exc:  # noqa: BLE001 — sin audio se sirve el video
            log.warning(
                "relay: no se pudo leer la playlist de audio",
                extra={"channel": self.nombre, "detail": str(exc)[:120]},
            )
            return {}
        return {n: ref for n, ref in enumerate(self._segmentos_del_playlist(texto, self._audio_pl))}

    def _muxear_audio(self, video: bytes, ref_audio: RefSegmento) -> bytes:
        """Le pega el audio a un segmento de video. Si algo falla, deja el video.

        - Important: nunca tumba el flujo. Si el audio no baja o el muxeo falla,
          se sirve el video solo (como antes) y se cuenta en ``sin_audio``: un
          canal mudo es mejor que un canal muerto, y el contador lo delata.
        """
        try:
            audio = self._bajar_segmento(ref_audio)
        except Exception:  # noqa: BLE001
            audio = None
        if not audio:
            with self._lock:
                self._sin_audio += 1
            return video
        try:
            unido, reporte = muxear(bytes(video), bytes(audio), pid_audio=self._pid_audio or None)
        except Exception as exc:  # noqa: BLE001 — no tumbar por un muxeo
            with self._lock:
                self._sin_audio += 1
            log.warning(
                "relay: muxeo fallido", extra={"channel": self.nombre, "detail": str(exc)[:140]}
            )
            return video
        if reporte.get("muxeado"):
            with self._lock:
                self._muxeados += 1
                # Se recuerda para los siguientes segmentos del canal.
                self._pid_audio = reporte.get("pid_audio") or self._pid_audio
        return unido

    def _recordar_visto(self, url: str) -> None:
        """Marca un segmento como ya bajado, con memoria ACOTADA (LRU).

        - Discussion: ``_vistos`` era un ``set`` que solo se vaciaba al cambiar
          de fuente, asi que crecia durante toda la vida del proceso en fuentes
          que rotan el nombre del playlist variante (medido en Canal 5: 2,486
          URLs en 2 h, ~30k/dia -> ~2.75 GB/año de RSS). Con la ventana viva
          alcanza: lo unico que hay que evitar es rebajar un segmento que aun
          esta en el colchon.
        - Important: se llama con el lock del lector tomado.
        """
        self._vistos[url] = None
        self._vistos.move_to_end(url)
        while len(self._vistos) > MAX_VISTOS:
            self._vistos.popitem(last=False)

    def _recordar_clave(self, uri: str, clave_hex: str) -> None:
        """Cachea una llave AES por URI con tope (LRU), no para siempre."""
        self._claves[uri] = clave_hex
        self._claves.move_to_end(uri)
        while len(self._claves) > MAX_CLAVES:
            self._claves.popitem(last=False)

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
                refs = self._segmentos_del_playlist(texto, self._variante)
                # ``_vistos`` se lee aquí SIN lock, y es correcto: este hilo (el
                # lector) es su ÚNICO escritor. Los otros dos accesos son el
                # clear() de _contar_fallo y el len() de stats(), y los dos van
                # bajo el lock. Tomarlo aquí no protegería de nada y sugeriría
                # una carrera entre hilos que no existe.
                pendientes = [(n, ref) for n, ref in enumerate(refs) if ref.url not in self._vistos]
                audio_refs = self._refs_audio() if pendientes else {}
                for n, ref in pendientes:
                    if self._stop.is_set():
                        return
                    self._esperar_hueco()
                    datos = self._bajar_segmento(ref)
                    if datos is None:
                        continue
                    ref_audio = audio_refs.get(n)
                    if ref_audio is not None:
                        datos = self._muxear_audio(datos, ref_audio)
                    datos = self._remendar(datos)
                    with self._lock:
                        self._recordar_visto(ref.url)
                        self._frente += 1
                        self._buf[self._frente] = Segmento(self._frente, ref.url, datos, ref.dur)
                        self._descargas += 1
                        self._fallos = 0  # la fuente activa respondio
                        self._podar()
                    self._error = ""
                with self._lock:
                    self._error = ""
            except urllib.error.HTTPError as exc:
                # Ruta muerta o token vencido: se vuelve a resolver el origen.
                # 401 entra aqui: la fuente responde 401 cuando su token
                # cacheado ya murio, y sin refrescarlo el canal no se recupera
                # nunca.
                if exc.code in (401, 403, 404, 410):
                    with self._lock:
                        self._refrescos += 1
                        self._variante = ""
                        self._refrescar_token = True
                    log.warning(
                        "relay: re-resolviendo origen",
                        extra={"channel": self.nombre, "detail": f"HTTP {exc.code}"},
                    )
                else:
                    self._error = f"HTTP {exc.code}"
                self._contar_fallo()
            except Exception as exc:  # noqa: BLE001 — el lector nunca muere
                self._error = str(exc)[:200]
                self._contar_fallo()
            # Espera entre vueltas. Normal: SONDEO_S. Si la fuente viene
            # fallando, crece hasta REINTENTO_MAX_ESPERA_S: un origen muerto con
            # una sola fuente golpeaba el CDN cada ~0.5 s para siempre.
            self._espera_s = (
                SONDEO_S
                if self._fallos == 0
                else min(REINTENTO_MAX_ESPERA_S, SONDEO_S * (2 ** min(self._fallos, 6)))
            )
            self._stop.wait(self._espera_s)

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
            self._recordar_clave(uri, clave)
        return clave

    def _descifrar(self, datos: bytes, key_uri: str, iv: str, seq: int) -> bytes:
        """Descifra un segmento AES-128-CBC.

        - Discussion: la stdlib de Python no trae AES, pero ``openssl`` sí está
          en la imagen base. Se le pasa el segmento por stdin y devuelve el
          plano por stdout; openssl quita solo el relleno PKCS7.
        - Note: la llave va en el argv del hijo porque ``openssl enc`` NO acepta
          una llave CRUDA por archivo: ``-kfile`` y ``-pass`` son para
          contraseñas y derivan la llave con un KDF, así que no sirven para una
          llave AES ya dada en hex. La exposición queda acotada a quien pueda
          leer ``/proc/<pid>/cmdline``: en este contenedor el único proceso con
          el mismo UID es el propio relé, que YA tiene la llave en memoria
          (``self._claves``), y root del host la podría leer de ahí igual. El
          riesgo real es que ese argv acabe en un log si alguien captura la
          lista de procesos, y por eso el argv no se registra NUNCA. Cambiarlo
          pide o la dependencia ``cryptography`` o un enlace por ctypes a
          libcrypto; ninguna de las dos se justifica para este caso.
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

    def _bajar_segmento(self, ref: RefSegmento) -> bytes | None:
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

    def _remendar(self, datos: bytes) -> bytes:
        """Ajusta la linea de tiempo cuando la fuente activa cambio.

        - Discussion: se aplica ANTES de entrar al colchon, para que el cliente
          reciba los sellos ya remendados. Con `delta == 0` (una sola fuente, o
          antes del primer cambio) esto es una pasada de lectura pura.
        - Important: el empalme se calcula contra `primer_pts` del segmento
          CRUDO que acaba de llegar, que es el primer sello de la fuente nueva.
        """
        if not self.remendar:
            return datos
        buf = bytearray(datos)
        if self._fuente_de_datos != self._indice:
            delta = self._rem.sincronizar(
                self._rem.ultimo_pts_emitido, primer_pts(buf), paso=self.paso_cuadro
            )
            self._fuente_de_datos = self._indice
            self._remiendos += 1
            log.warning(
                "relay: empalmando linea de tiempo",
                extra={"channel": self.nombre, "detail": f"fuente={self._indice} delta={delta}"},
            )
        self._rem.procesar(buf)
        return bytes(buf)

    def _esperar_hueco(self, max_s: float = 30.0) -> None:
        """No baja mas si el colchon esta lleno y hay alguien viendo.

        - Discussion: sin esto el lector se adelanta al tope, `_podar()` suelta
          por la IZQUIERDA —que es donde esta el cliente, porque va
          ``COLCHON_SEGMENTOS`` detras— y se tiran segmentos que nadie consumio.
          Medido en un cambio de fuente: 24.1 s de contenido perdidos, que es lo
          que el espectador ve como tiron.
        - Important: SIN clientes no se espera. El precalentado necesita que el
          colchon siga avanzando para que el primer byte salga en ~0.02 s;
          frenarlo dejaria segmentos rancios esperando a que alguien entre.
        - Note: ``max_s`` evita que un cliente atascado congele al lector para
          siempre; pasado ese rato vuelve al comportamiento viejo.
        """
        limite = time.monotonic() + max_s
        while not self._stop.is_set():
            with self._lock:
                if not self._clientes or len(self._buf) < MAX_SEGMENTOS:
                    return
            if time.monotonic() >= limite:
                return
            self._stop.wait(0.2)

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
                "vistos": len(self._vistos),
                "claves": len(self._claves),
                "tuvo_clientes": self._tuvo_clientes,
                "espera_s": round(self._espera_s, 2),
                "remendando": self.remendar,
                "remiendos": self._remiendos,
                "ultimo_pts": self._rem.ultimo_pts_emitido,
                "rendicion_audio": bool(self._audio_pl),
                "muxeados": self._muxeados,
                "sin_audio": self._sin_audio,
                "pid_audio": self._pid_audio,
            }

    # ---------------------------------------------------------------- cliente
    def servir(self, espera_max_s: float = ARRANQUE_MAX_S) -> Iterator[bytes]:
        """Genera el flujo continuo para UN cliente.

        - Discussion: se arranca con lo que haya, no con el colchon lleno: el
          primer byte tiene que salir dentro de la ventana de sondeo de Jellyfin
          (3 s) o el probe se rinde.
        - Important: el rele NO dosifica al cliente. La unica cota es lo que ya
          se leyo (``_frente``): si el cliente corre mas que el lector, se queda
          esperando datos. El colchon se mantiene porque el reproductor real
          consume a 1x y el lector va por delante. La version anterior calculaba
          un ``tope`` UNA vez y nunca lo actualizaba, asi que no controlaba
          nada: era el comentario el que prometia un control que no existia.
        """
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
            self._tuvo_clientes = True
        log.info(
            "relay: cliente enganchado",
            extra={"channel": self.nombre, "detail": f"idx={idx} frente={self._frente}"},
        )

        try:
            while not self._stop.is_set():
                with self._lock:
                    seg = self._buf.get(idx)
                    if idx < self._primero:  # se quedo atras: se salta
                        idx = self._primero
                        self._clientes[cid] = (idx, time.monotonic())
                        seg = self._buf.get(idx)
                    if seg is not None:
                        self._clientes[cid] = (idx + 1, time.monotonic())
                        idx += 1
                if seg is None:
                    time.sleep(0.05)  # aun no hay datos en idx
                    continue
                yield seg.datos
        finally:
            with self._lock:
                self._clientes.pop(cid, None)
                self._ultimo_cliente = time.monotonic()
            log.info(
                "relay: cliente desenganchado",
                extra={"channel": self.nombre, "detail": f"idx={idx}"},
            )

    # ------------------------------------------------------------ introspeccion
    @property
    def clientes(self) -> int:
        """Clientes enganchados ahora mismo (para el barrido del pool)."""
        with self._lock:
            return len(self._clientes)

    @property
    def ultimo_cliente(self) -> float:
        """Instante (monotonico) en que se desengacho el ultimo cliente."""
        with self._lock:
            return self._ultimo_cliente

    @property
    def tuvo_clientes(self) -> bool:
        """True si alguna vez sirvio a alguien (lo distingue del precalentado)."""
        return self._tuvo_clientes


class RelayPool:
    """Un relé por canal, creado bajo demanda y apagado cuando queda solo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._relays: dict[str, RelayHLS] = {}

    def get(
        self,
        clave: str,
        fuentes,
        user_agent: str = "",
        resolver=None,
        remendar: bool = True,
        paso_cuadro: int = 3003,
    ) -> RelayHLS:
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
                relay = RelayHLS(
                    lista,
                    user_agent,
                    resolver=resolver,
                    nombre=clave,
                    remendar=remendar,
                    paso_cuadro=paso_cuadro,
                )
                relay.arrancar()
                self._relays[clave] = relay
            else:
                if resolver is not None:
                    relay.resolver = resolver
                relay.remendar = remendar
                relay.paso_cuadro = paso_cuadro
            return relay

    def stats(self) -> dict:
        with self._lock:
            return {k: r.stats() for k, r in self._relays.items()}

    def barrer(self, vigentes: set[str], idle_s: float = 900.0) -> list[str]:
        """Apaga los relés que ya no sirven a nadie.

        - Discussion: un relé sostiene ~330 KB/s de bajada CONTINUA, tenga o no
          clientes. ``detener_todos`` existía pero nadie la llamaba, así que
          cambiar de catálogo dejaba vivos los del anterior para siempre (medido:
          10 relés, 0 clientes, 26 Mbps, horas encendidos).
        - Note: se apaga (a) el canal que ya no está en el catálogo publicado y
          (b) el que YA tuvo clientes y lleva ``idle_s`` sin ninguno. Los relés
          recién precalentados no entran en (b): ese es justo el punto del
          precalentado, y matarlos devolvería el arranque lento.
        - Returns: ids de los relés apagados.
        """
        ahora = time.monotonic()
        apagar: list[RelayHLS] = []
        apagados: list[str] = []
        with self._lock:
            for clave, relay in list(self._relays.items()):
                ocioso = (
                    relay.tuvo_clientes
                    and relay.clientes == 0
                    and ahora - relay.ultimo_cliente > idle_s
                )
                if clave not in vigentes or ocioso:
                    apagar.append(relay)
                    apagados.append(clave)
                    del self._relays[clave]
        # Fuera del lock: detener() hace join del hilo (hasta 5 s) y no hay
        # razon para bloquear un get() mientras tanto.
        for relay in apagar:
            relay.detener()
        if apagados:
            log.info("relay: reles apagados", extra={"detail": f"{len(apagados)}: {apagados[:5]}"})
        return apagados

    def detener_todos(self) -> None:
        with self._lock:
            relays = list(self._relays.values())
            self._relays.clear()
        for relay in relays:
            relay.detener()
