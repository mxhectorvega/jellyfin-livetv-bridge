"""Verificacion EN VIVO del empalme: failover real sobre un canal con respaldo.

No simula el reloj como el banco sintetico: usa el canal de verdad, su respaldo
de verdad, y mata la fuente principal a media reproduccion con un proxy local
que empieza a devolver 404. Le mide el PTS a la SALIDA del rele con ffprobe.

Uso: .venv/bin/python tests/banco_failover.py <clave_de_respaldo>
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))

import yaml  # noqa: E402
from src.relay import RelayHLS  # noqa: E402

CLAVE = sys.argv[1] if len(sys.argv) > 1 else "canal.22.de.mxico.mx"
# Segundo argumento opcional: usar el stream de OTRO canal como respaldo. Sirve
# porque los respaldos declarados en sources.yml viven en el MISMO CDN que la
# principal (mismo reloj -> no hay salto que coser, y el empalme no se puede
# distinguir de un no-empalme).
CLAVE_AJENA = sys.argv[2] if len(sys.argv) > 2 else None
BASE = Path("/home/server/jellyfin-livetv-bridge")
SEGUNDOS = 45
MUERE_A = 12.0
# Bytes por segundo que emula el consumidor (~3.2 Mbps): el ritmo de un
# reproductor, para que el rele no drene la playlist a lo bruto.
CAUDAL = 400_000.0


class Flaky(BaseHTTPRequestHandler):
    """Proxy que reenvia al origen real hasta que se cumple el reloj."""

    origen = ""
    t0 = 0.0
    vivo = True

    def log_message(self, *a):
        pass

    def do_GET(self):
        if not Flaky.vivo or time.time() - Flaky.t0 > MUERE_A:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"muerto")
            return
        try:
            with urllib.request.urlopen(Flaky.origen, timeout=10) as r:
                datos = r.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)
        except Exception:
            self.send_response(502)
            self.end_headers()


def pts_de(archivo: Path) -> list[float]:
    r = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_packets",
            "-show_entries",
            "packet=pts_time",
            "-of",
            "json",
            str(archivo),
        ],
        capture_output=True,
        timeout=240,
    )
    if r.returncode != 0:
        return []
    try:
        d = json.loads(r.stdout.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return []
    return [float(p["pts_time"]) for p in d.get("packets", []) if p.get("pts_time")]


def escalon(t: list[float]) -> tuple[float, float]:
    if len(t) < 2:
        return 0.0, 0.0
    difs = sorted(((abs(t[i + 1] - t[i]), i) for i in range(len(t) - 1)), reverse=True)
    return difs[0][0], difs[0][1] / len(t)


def correr(remendar: bool) -> tuple[float, float, int, int]:
    cfg = yaml.safe_load(open(BASE / "config/sources.yml"))
    respaldos = (cfg.get("respaldo") or {}).get(CLAVE) or []
    estado = json.loads((BASE / "data/state.json").read_text())
    canal = next((c for c in estado["canales"] if c["id"] == CLAVE), None)
    if canal is None or not respaldos:
        raise SystemExit(f"{CLAVE} no tiene respaldo en el catalogo")
    if CLAVE_AJENA:
        ajeno = next((c for c in estado["canales"] if c["id"] == CLAVE_AJENA), None)
        if ajeno is None:
            raise SystemExit(f"{CLAVE_AJENA} no esta en el catalogo")
        respaldos = [ajeno["source_url"]]
        print(f"  (respaldo forzado: stream real de {CLAVE_AJENA}, otro reloj)")

    Flaky.origen = canal["source_url"]
    Flaky.t0 = time.time()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Flaky)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    puerto = srv.server_address[1]

    relay = RelayHLS(
        [f"http://127.0.0.1:{puerto}/vivo.m3u8", str(respaldos[0])], nombre=CLAVE, remendar=remendar
    )
    relay.arrancar()
    salida = Path(f"/home/server/tmp_lvb/failover_{'con' if remendar else 'sin'}.ts")
    t0 = time.time()
    escritos = 0
    with salida.open("wb") as fh:
        for trozo in relay.servir(espera_max_s=10.0):
            fh.write(trozo)
            escritos += len(trozo)
            # Un reproductor de verdad consume a 1x. Sin este freno el script
            # drena la playlist entera a la velocidad de la red (medido: 168 s
            # de contenido en 45 s de reloj) y el rele nunca se comporta como en
            # produccion.
            objetivo = CAUDAL * (time.time() - t0)
            if escritos > objetivo:
                # Se duerme lo que haga falta, sin tope corto: un segmento son
                # ~9 s de contenido y con un tope de 0.5 s el freno no frena
                # (medido: 168 s de contenido en 45 s de reloj otra vez).
                time.sleep(max(0.0, escritos / CAUDAL - (time.time() - t0)))
            if time.time() - t0 > SEGUNDOS:
                break
    st = relay.stats()
    relay.detener()
    srv.shutdown()
    return (*escalon(pts_de(salida)), st["cambios_fuente"], st["remiendos"])


print(f"canal: {CLAVE} | la fuente principal muere a los {MUERE_A:.0f} s de {SEGUNDOS} s")
for flag in (False, True):
    paso, pos, cambios, remiendos = correr(flag)
    etiqueta = "CON empalme" if flag else "SIN empalme"
    print(
        f"{etiqueta:12} escalon mayor {paso:9.3f} s (en {pos:5.0%} del flujo) "
        f"| cambios de fuente={cambios} remiendos={remiendos}"
    )
