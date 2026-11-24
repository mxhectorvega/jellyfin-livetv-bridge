"""Pruebas del pool HTTP: REUSO de conexion, redirecciones y errores.

- Discussion: hasta 2026-11-24 cada peticion abria una conexion TCP nueva y
  resolvia el DNS otra vez (`urllib.request.urlopen` no reusa ni cachea, y Python
  no cachea `getaddrinfo`). Medido con 18 reles vivos y CERO clientes, mas el
  health-check de 203 canales cada 5 min: ~14 consultas DNS/s desde CT201, ~2.9 M
  lineas/dia en el log del firewall (~1.3 GB). El arreglo es un pool de
  conexiones (ver `util.peticion`), y estas pruebas fijan lo que `urlopen` daba
  gratis: reuso, seguimiento de redirecciones y `HTTPError` con `code` en 4xx
  (el rele decide con `exc.code in (401, 403, 404, 410)`).
- Note: se usan servidores LOCALES en 127.0.0.1 (loopback) y se CUENTAN las
  conexiones aceptadas; nada de red externa. Con `urlopen`, la prueba de reuso
  habria contado una conexion por peticion.
"""

from __future__ import annotations

import gzip
import http.server
import sys
import threading
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from src import relay, util  # noqa: E402

CUERPO = b"#EXTM3U\n#EXTINF:4.0,\na.ts\n"


class _Manejador(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    ultimo_ua: str | None = None

    def log_message(self, *args):  # el servidor de pruebas no ensucia la salida
        pass

    def _responder(self, cuerpo: bytes, estado: int = 200, extra: dict | None = None) -> None:
        self.send_response(estado)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(cuerpo)))
        for clave, valor in (extra or {}).items():
            self.send_header(clave, valor)
        self.end_headers()
        if cuerpo:
            self.wfile.write(cuerpo)

    def do_HEAD(self):  # noqa: N802 — lo impone BaseHTTPRequestHandler
        if urllib.parse.urlsplit(self.path).path == "/roto":
            self._responder(b"", 404)
        else:
            self._responder(b"")

    def do_GET(self):  # noqa: N802 — lo impone BaseHTTPRequestHandler
        _Manejador.ultimo_ua = self.headers.get("User-Agent")
        partes = urllib.parse.urlsplit(self.path)
        ruta = partes.path
        if ruta == "/pl.m3u8":
            self._responder(CUERPO)
        elif ruta == "/gzip":
            self._responder(gzip.compress(CUERPO))
        elif ruta == "/redir":
            # mismo host:puerto -> la conexion SI se puede reusar
            self._responder(b"", 302, {"Location": "/pl.m3u8"})
        elif ruta == "/redir-fuera":
            puerto = urllib.parse.parse_qs(partes.query).get("puerto", [""])[0]
            self._responder(b"", 302, {"Location": f"http://127.0.0.1:{puerto}/pl.m3u8"})
        elif ruta == "/roto":
            self._responder(b"nope", 404)
        elif ruta == "/grande":
            self._responder(b"x" * 4096)
        else:
            self._responder(b"", 404)


class _Servidor(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.conexiones = 0

    def get_request(self):
        peticion, direccion = super().get_request()
        self.conexiones += 1
        return peticion, direccion


@pytest.fixture()
def servidores():
    """Fabrica de servidores locales; los apaga todos al terminar la prueba."""
    hechos: list[_Servidor] = []

    def _nuevo() -> _Servidor:
        srv = _Servidor(("127.0.0.1", 0), _Manejador)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        hechos.append(srv)
        return srv

    util.soltar_conexiones()  # cache por hilo limpio: una prueba no contamina a otra
    yield _nuevo
    for srv in hechos:
        srv.shutdown()
        srv.server_close()
    util.soltar_conexiones()


@pytest.fixture()
def servidor(servidores) -> _Servidor:
    return servidores()


def _url(srv: _Servidor, ruta: str) -> str:
    return f"http://127.0.0.1:{srv.server_address[1]}{ruta}"


# --------------------------------------------------------------- pool / rele
def test_dos_sondeos_usan_UNA_conexion(servidor):
    """El punto del arreglo: 2 sondeos, 1 conexion. Con urlopen eran 2."""
    for _ in range(2):
        assert relay._http(_url(servidor, "/pl.m3u8")).startswith(b"#EXTM3U")
    assert servidor.conexiones == 1


def test_muchos_sondeos_siguen_usando_una_conexion(servidor):
    """50 vueltas del lector = 1 conexion. Es lo que quita los GB de log."""
    for _ in range(50):
        relay._http(_url(servidor, "/pl.m3u8"))
    assert servidor.conexiones == 1


def test_sigue_la_redireccion_y_reusa_la_conexion(servidor):
    assert relay._http(_url(servidor, "/redir")).startswith(b"#EXTM3U")
    assert servidor.conexiones == 1


def test_la_redireccion_a_otro_destino_suelta_la_conexion(servidor, servidores):
    """Si el salto cambia de host:puerto, la conexion vieja NO sirve: se suelta."""
    otro = servidores()
    relay._http(_url(servidor, f"/redir-fuera?puerto={otro.server_address[1]}"))
    assert otro.conexiones == 1
    # Al primero se le solto la conexion: volver a el abre una NUEVA.
    relay._http(_url(servidor, "/pl.m3u8"))
    assert servidor.conexiones == 2


def test_cada_hilo_abre_la_suya(servidor):
    """El cache es por hilo: dos lectores no se pisan la conexion."""
    barrera = threading.Barrier(3)

    def golpear():
        barrera.wait()
        relay._http(_url(servidor, "/pl.m3u8"))

    hilos = [threading.Thread(target=golpear) for _ in range(3)]
    for hilo in hilos:
        hilo.start()
    for hilo in hilos:
        hilo.join()
    assert servidor.conexiones == 3


def test_404_del_rele_levanta_HTTPError_con_su_codigo(servidor):
    with pytest.raises(urllib.error.HTTPError) as exc:
        relay._http(_url(servidor, "/roto"))
    assert exc.value.code == 404


def test_recorta_a_max_bytes_y_tira_esa_conexion(servidor):
    """Una respuesta a medio leer no se puede reusar: se descarta y se sigue."""
    assert len(relay._http(_url(servidor, "/grande"), max_bytes=100)) == 100
    assert servidor.conexiones == 1
    relay._http(_url(servidor, "/pl.m3u8"))
    assert servidor.conexiones == 2


def test_manda_el_user_agent_cuando_lo_hay(servidor):
    """Algunos CDN filtran por UA; el rele lo declara por canal."""
    relay._http(_url(servidor, "/pl.m3u8"), user_agent="VLC/3.0.20")
    assert _Manejador.ultimo_ua == "VLC/3.0.20"


# ----------------------------------------------------- helper del probe/health
def test_http_get_reusa_la_conexion(servidor):
    """El health-check sondea 203 canales cada 5 min: sin reuso, ese era el churn."""
    for _ in range(30):
        assert util.http_get(_url(servidor, "/pl.m3u8")) == CUERPO
    assert servidor.conexiones == 1


def test_http_get_sigue_redirecciones(servidor):
    assert util.http_get(_url(servidor, "/redir")) == CUERPO
    assert servidor.conexiones == 1


def test_http_get_descomprime_gzip(servidor):
    """El camino de las guias .xml.gz: se detecta por el magic number."""
    assert util.http_get(_url(servidor, "/gzip")) == CUERPO


def test_http_get_levanta_RuntimeError_en_404(servidor):
    with pytest.raises(RuntimeError, match="HTTP 404"):
        util.http_get(_url(servidor, "/roto"))


def test_http_get_falla_de_red_con_texto_util(servidor):
    """Puerto cerrado: el mensaje conserva el 'fallo de red en' de siempre."""
    url = f"http://127.0.0.1:{servidor.server_address[1] + 1}/pl.m3u8"
    with pytest.raises(RuntimeError, match="fallo de red en"):
        util.http_get(url, timeout=1.0)


def test_http_head_status(servidor):
    assert util.http_head_status(_url(servidor, "/pl.m3u8")) == 200
    assert util.http_head_status(_url(servidor, "/roto")) == 404


def test_http_head_status_devuelve_0_cuando_no_hay_conexion(servidor):
    url = f"http://127.0.0.1:{servidor.server_address[1] + 1}/pl.m3u8"
    assert util.http_head_status(url, timeout=1.0) == 0
