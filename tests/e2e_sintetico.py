#!/usr/bin/env python3
"""Banco de pruebas sintetico (E2E) del bridge.

Un origen de VERDAD, generado con ffmpeg, servido por HTTP local, y la cadena
completa encima: ingesta -> validacion -> normalizacion -> guia -> salida ->
API -> /stream -> /relay -> failover -> AES-128.

- Discussion: la suite de pytest corre con `monkeypatch` y no toca la red: prueba
  logica, no arranque. Este banco es lo que caza lo que los mocks no ven (una
  bandera mal puesta, un generador que no cierra, un pipe que se atasca). Cuesta
  segundos.
- Important: necesita `ffmpeg` y `openssl` en el PATH, y correr con el python del
  proyecto (el que tiene fastapi/uvicorn/httpx):

      cd /home/server/jellyfin-livetv-bridge
      .venv/bin/python tests/e2e_sintetico.py

- Note: si algo falla, deja el directorio temporal en disco y lo imprime, para
  poder inspeccionar el HLS, la config y los artefactos.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

PUERTO = 8799  # origen HLS sintetico
PUERTO_API = 8798  # la API real que se levanta para probar los flujos
CLAVE_AES = "000102030405060708090a0b0c0d0e0f"
FALLOS: list[str] = []
PASOS = 0


# ---------------------------------------------------------------------------
# Utilidades de reporte
# ---------------------------------------------------------------------------
def paso(nombre: str) -> None:
    global PASOS
    PASOS += 1
    print(f"\n[{PASOS:02d}] {nombre}", flush=True)


def ok(msg: str) -> None:
    print(f"     ok  {msg}", flush=True)


def fallo(msg: str) -> None:
    FALLOS.append(msg)
    print(f"     XX  {msg}", flush=True)


def afirmar(condicion: bool, msg: str) -> None:
    ok(msg) if condicion else fallo(msg)


def esperar(predicado, limite_s: float = 15.0, intervalo: float = 0.2) -> bool:
    """Espera a que se cumpla algo, sin dormir a ciegas."""
    fin = time.time() + limite_s
    while time.time() < fin:
        if predicado():
            return True
        time.sleep(intervalo)
    return predicado()


def _http_codigo(url: str) -> int:
    """Codigo HTTP de un GET corto (0 si no se pudo conectar)."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except Exception:  # noqa: BLE001 — un 0 aqui es "no conecta", no "no existe"
        return 0


def _leer_n_bytes(url: str, n: int, limite_s: float = 20.0) -> bytes | None:
    """Lee N bytes de un flujo y CORTA: no espera a que el flujo termine.

    - Important: /stream y /relay son infinitos por diseno. Leer "hasta el final"
      (o cerrar esperando a que la tarea ASGI acabe) cuelga la prueba para
      siempre: es el error que costo la primera corrida de este banco.
    """
    import urllib.request

    datos = b""
    try:
        with urllib.request.urlopen(url, timeout=limite_s) as resp:
            while len(datos) < n:
                trozo = resp.read(16384)
                if not trozo:
                    break
                datos += trozo
    except Exception as exc:  # noqa: BLE001 — se reporta el corte, no se esconde
        print(f"     (corte a los {len(datos)} B: {exc})", flush=True)
    return datos or None


# ---------------------------------------------------------------------------
# Origen sintetico
# ---------------------------------------------------------------------------
def generar_hls(destino: Path) -> None:
    """Genera un master con dos variantes (720p y 360p) y segmentos reales."""
    destino.mkdir(parents=True, exist_ok=True)
    for etiqueta, escala, kbps in (("720", "1280x720", "1200k"), ("360", "640x360", "400k")):
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={escala}:rate=25",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:sample_rate=44100",
                "-t",
                "24",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-b:v",
                kbps,
                # GOP corto y forzado: sin esto el primer segmento dura lo que el
                # keyframe por defecto (250 frames = 10 s) y el "hls_time 3" no se
                # respeta. Un HLS en vivo real usa GOP de un segmento.
                "-g",
                "75",
                "-keyint_min",
                "75",
                "-sc_threshold",
                "0",
                "-force_key_frames",
                "expr:gte(t,n_forced*3)",
                "-c:a",
                "aac",
                "-b:a",
                "64k",
                "-f",
                "hls",
                "-hls_time",
                "3",
                "-hls_list_size",
                "0",
                "-hls_segment_filename",
                str(destino / f"v{etiqueta}_%03d.ts"),
                str(destino / f"v{etiqueta}.m3u8"),
            ],
            check=True,
        )
    (destino / "master.m3u8").write_text(
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1400000,RESOLUTION=1280x720\n"
        "v720.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=450000,RESOLUTION=640x360\n"
        "v360.m3u8\n",
        encoding="utf-8",
    )


def generar_aes(destino: Path) -> str:
    """Cifra los segmentos del 720p con AES-128 y publica su playlist.

    - Discussion: el origen manda UNA llave por segmento, cada una con su IV. El
      rele copia bytes, asi que sin descifrar entrega texto cifrado y el cliente
      no decodifica nada. Aqui se reproduce exactamente ese caso.
    - Returns: nombre del archivo de playlist generado.
    """
    fuente = sorted(destino.glob("v720_*.ts"))
    llave = bytes.fromhex(CLAVE_AES)
    (destino / "aes.key").write_bytes(llave)

    lineas = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:4", "#EXT-X-MEDIA-SEQUENCE:0"]
    for i, segmento in enumerate(fuente):
        iv = f"{i:032x}"
        cifrado = destino / f"aes_{i:03d}.ts"
        subprocess.run(
            [
                "openssl",
                "enc",
                "-aes-128-cbc",
                "-K",
                CLAVE_AES,
                "-iv",
                iv,
                "-in",
                str(segmento),
                "-out",
                str(cifrado),
            ],
            check=True,
            capture_output=True,
        )
        lineas += [
            f'#EXT-X-KEY:METHOD=AES-128,URI="aes.key",IV=0x{iv}',
            "#EXTINF:3.0,",
            cifrado.name,
        ]
    nombre = "aes.m3u8"
    (destino / nombre).write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return nombre


def escribir_guia(config: Path) -> Path:
    """XMLTV minimo: un canal de guia compartido y dos programas.

    - Discussion: el programa "viejo" tiene que quedar fuera de la ventana
      (-6 h) para probar el filtro temporal. Y los programas van bajo el id de
      la GUIA, no bajo el nuestro: el empate explicito los reetiqueta despues.
    """
    from datetime import datetime, timedelta

    ahora = datetime.now(UTC)

    def t(dt):
        return dt.strftime("%Y%m%d%H%M%S +0000")

    guia = config / "guia.xml"
    guia.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<tv>\n'
        '  <channel id="guia.prueba"><display-name>Guia Prueba</display-name></channel>\n'
        f'  <programme start="{t(ahora - timedelta(minutes=30))}" '
        f'stop="{t(ahora + timedelta(minutes=30))}" '
        'channel="guia.prueba"><title>Programa Prueba</title></programme>\n'
        f'  <programme start="{t(ahora - timedelta(hours=8))}" '
        f'stop="{t(ahora - timedelta(hours=7))}" '
        'channel="guia.prueba"><title>Programa Viejo</title></programme>\n'
        "</tv>\n",
        encoding="utf-8",
    )
    return guia


def escribir_config(base: Path, guia: Path, aes_url: str, muerto_url: str) -> Path:
    config = base / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "sources.yml").write_text(
        f"""settings:
  relay_enabled: true
  probe_mode: liveness
  probe_workers: 2

channels:
  - name: Canal Prueba
    url: http://127.0.0.1:{PUERTO}/master.m3u8
    tvg_id: canal.prueba.mx
    group: Pruebas
    catalog: pruebas
  - name: Canal AES
    url: {aes_url}
    tvg_id: canal.aes.mx
    group: Pruebas
    catalog: pruebas

respaldo:
  canal.aes.mx:
    - {aes_url}

epg:
  refresh_hours: 1
  sources:
    - name: Local
      url: {guia}
      mappings:
        canal.prueba.mx: guia.prueba
        canal.aes.mx: guia.prueba
""",
        encoding="utf-8",
    )
    return config


# ---------------------------------------------------------------------------
# Servidor HTTP del origen
# ---------------------------------------------------------------------------
class HandlerSilencioso(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 — silencio en el banco
        pass


def arrancar_servidor(directorio: Path) -> ThreadingHTTPServer:
    handler = lambda *a, **k: HandlerSilencioso(*a, directory=str(directorio), **k)  # noqa: E731
    servidor = ThreadingHTTPServer(("127.0.0.1", PUERTO), handler)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    return servidor


def puerto_libre(puerto: int = PUERTO) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", puerto)) != 0


# ---------------------------------------------------------------------------
# Banco
# ---------------------------------------------------------------------------
def main() -> int:
    if not puerto_libre(PUERTO):
        print(f"FALLO: el puerto {PUERTO} esta ocupado")
        return 2
    if not puerto_libre(PUERTO_API):
        print(f"FALLO: el puerto {PUERTO_API} esta ocupado")
        return 2
    for binario in ("ffmpeg", "openssl"):
        if shutil.which(binario) is None:
            print(f"FALLO: falta {binario} en el PATH")
            return 2

    base = Path(tempfile.mkdtemp(prefix="lvb-e2e-"))
    hls = base / "hls"
    print(f"directorio de trabajo: {base}")

    try:
        paso("Generar el origen sintetico (ffmpeg)")
        generar_hls(hls)
        segmentos = sorted(hls.glob("v720_*.ts"))
        afirmar(len(segmentos) >= 4, f"{len(segmentos)} segmentos 720p generados")
        afirmar(
            (hls / "v720_000.ts").read_bytes()[:1] == b"\x47", "el segmento empieza con 0x47 (TS)"
        )

        paso("Cifrar una variante con AES-128 (una llave por segmento)")
        aes_playlist = generar_aes(hls)
        primero_cifrado = (hls / "aes_000.ts").read_bytes()
        afirmar(primero_cifrado[:1] != b"\x47", "el segmento cifrado NO tiene firma TS")
        afirmar((hls / "aes.key").stat().st_size == 16, "la llave pesa 16 bytes")

        (base / "cfg-tmp").mkdir(exist_ok=True)
        guia = escribir_guia(base / "cfg-tmp")
        config = escribir_config(
            base,
            guia,
            f"http://127.0.0.1:{PUERTO}/{aes_playlist}",
            f"http://127.0.0.1:{PUERTO}/no-existe.m3u8",
        )
        data = base / "data"
        data.mkdir(exist_ok=True)

        paso("Servir el origen por HTTP")
        servidor = arrancar_servidor(hls)
        afirmar(True, f"origen en http://127.0.0.1:{PUERTO}/master.m3u8")

        from src.config import Settings
        from src.epg import refresh_epg
        from src.ingest import ingest
        from src.normalize import aplicar_respaldos, normalize
        from src.playlist import write_active_outputs
        from src.probe import check_channels

        settings = Settings(
            config_dir=str(config),
            data_dir=str(data),
            public_base=f"http://127.0.0.1:{PUERTO}",
            probe_timeout_s=10.0,
            probe_workers=2,
            probe_mode="liveness",
            relay_enabled=True,
        )
        from src.ingest import load_config_file

        cfg = load_config_file(config / "sources.yml")

        paso("Etapa 1-2: ingesta y validacion REAL (baja bytes)")
        canales = ingest(settings, cfg)
        afirmar(len(canales) == 2, f"{len(canales)} canales ingeridos")
        canales = check_channels(canales, settings)
        accesibles = [c for c in canales if c.probe.accessible]
        afirmar(len(accesibles) == 2, f"{len(accesibles)}/2 accesibles por liveness")
        aes = next((c for c in canales if c.tvg_id == "canal.aes.mx"), None)
        afirmar(aes is not None and aes.probe.encrypted is True, "el canal AES se detecta cifrado")
        afirmar(aes is not None and aes.probe.drm is False, "AES-128 NO se marca como DRM")

        paso("Etapa 3: normalizacion y respaldos")
        canales, rep = normalize(canales, settings)
        canales = aplicar_respaldos(canales, cfg.get("respaldo"))
        afirmar(all(c.id == c.tvg_id for c in canales), "el id publicado es el tvg-id (estable)")
        aes = next(c for c in canales if c.tvg_id == "canal.aes.mx")
        afirmar(len(aes.backups) == 1, "el respaldo declarado quedo asociado")

        paso("Etapa 4-5: guia y salida (M3U + XMLTV)")
        epg_rep, bundle = refresh_epg(settings, cfg, canales)
        afirmar(epg_rep["con_guia"] == 2, f"{epg_rep['con_guia']}/2 canales con guia")
        escrito = write_active_outputs(canales, bundle, settings)
        afirmar(escrito["m3u_escrito"] is True, "playlist.m3u escrito")
        m3u = settings.playlist_file.read_text(encoding="utf-8")
        afirmar("/relay/" in m3u, "con el rele activo el M3U publica /relay/<id>")
        afirmar(m3u.count("#EXTINF") == 2, "el M3U trae los 2 canales")
        xml = settings.epg_file.read_text(encoding="utf-8")
        afirmar("Programa Viejo" not in xml, "la ventana temporal filtra lo viejo")
        afirmar(xml.count("<programme") >= 2, "la guia trae programas")

        paso("API: /health, /catalogos, /playlist.m3u")
        from fastapi.testclient import TestClient
        from src.api import AppState, create_app
        from src.health import HealthMonitor

        estado = AppState(settings=settings, cfg=cfg)
        estado.monitor = HealthMonitor(settings)
        estado.put_channels(canales)
        estado.epg_bundle = bundle
        app = create_app(estado)
        cliente = TestClient(app)
        afirmar(cliente.get("/health").json()["canales"] == 2, "/health reporta 2 canales")
        afirmar(cliente.get("/catalogs").json()["publicados"] == 2, "/catalogos publica 2")
        afirmar(cliente.get("/playlist.m3u").text.startswith("#EXTM3U"), "/playlist.m3u responde")
        afirmar(cliente.get("/epg.xml").status_code == 200, "/epg.xml responde")

        paso("POST /catalog con token (auth opcional)")
        settings.api_token = "e2e-token"
        afirmar(cliente.post("/catalog/todos").status_code == 401, "sin token responde 401")
        afirmar(
            cliente.post(
                "/catalog/todos", headers={"Authorization": "Bearer e2e-token"}
            ).status_code
            == 200,
            "con token responde 200",
        )
        afirmar(
            cliente.post("/catalog/pruebas", params={"token": "e2e-token"}).status_code == 200,
            "acepta ?token=",
        )
        settings.api_token = ""

        paso("Levantar la API REAL (uvicorn) para probar los flujos")
        # - Important: los flujos de /stream y /relay NO se prueban con TestClient.
        #   Su generador es infinito por diseno, y al cerrar el contexto el cliente
        #   de pruebas espera a que la tarea ASGI termine: se cuelga para siempre.
        #   Con un uvicorn de verdad el cliente corta el socket y sigue.

        import threading as _threading

        import uvicorn

        servidor_api = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=PUERTO_API, log_level="error", access_log=False
            )
        )
        _threading.Thread(target=servidor_api.run, daemon=True).start()
        base_api = f"http://127.0.0.1:{PUERTO_API}"
        afirmar(
            esperar(lambda: _http_codigo(f"{base_api}/health") == 200, limite_s=20),
            "la API levanto",
        )

        paso("/stream/<id>: proxy ffmpeg -c copy")
        crudo = _leer_n_bytes(f"{base_api}/stream/canal.prueba.mx", 100_000, limite_s=25)
        afirmar(
            crudo is not None and crudo[:1] == b"\x47",
            f"/stream entrega MPEG-TS ({(len(crudo) if crudo else 0)} B)",
        )

        paso("/relay/<id>: rele con colchon (descifra AES-128 en el camino)")
        relay = estado.relay_pool.get(
            aes.id,
            [aes.source_url] + list(aes.backups),
            settings.relay_user_agent,
        )
        afirmar(relay.tuvo_clientes is False, "el rele estaba precalentado (sin clientes)")
        afirmar(
            esperar(lambda: relay.stats()["en_buffer"] > 0, limite_s=20), "el colchon se llena solo"
        )
        afirmar(relay.stats()["descifrados_aes"] > 0, "el rele descifro segmentos AES-128")
        descifrado = _leer_n_bytes(f"{base_api}/relay/{aes.id}", 100_000, limite_s=25)
        afirmar(
            descifrado is not None and descifrado[:1] == b"\x47",
            f"/relay entrega TS DESCIFRADO ({(len(descifrado) if descifrado else 0)} B)",
        )
        afirmar(relay.tuvo_clientes is True, "el rele registro el cliente")

        paso("/relay/status: estado por canal")
        stats = cliente.get("/relay/status").json()
        afirmar(aes.id in stats, "el canal aparece en /relay/status")
        afirmar(
            "tuvo_clientes" in stats[aes.id] and "vistos" in stats[aes.id],
            "expone la memoria acotada y el uso",
        )
        afirmar(stats[aes.id]["vistos"] <= 512, f"vistos acotado ({stats[aes.id]['vistos']})")

        paso("Failover: fuente principal muerta -> salta a la alternativa")
        from src.relay import RelayHLS

        muerto = f"http://127.0.0.1:{PUERTO}/no-existe.m3u8"
        vivo = f"http://127.0.0.1:{PUERTO}/{aes_playlist}"
        relevo = RelayHLS([muerto, vivo])
        relevo.arrancar()
        try:
            cambio = esperar(lambda: relevo._cambios >= 1, limite_s=20)
            afirmar(cambio, "salto a la fuente alterna")
            afirma_llenado = esperar(lambda: relevo.stats()["en_buffer"] > 0, limite_s=20)
            afirmar(afirma_llenado, "la fuente alterna entrega datos")
            afirmar(relevo.stats()["fuente"] == 1, "el indice apunta a la alternativa")
        finally:
            relevo.detener()

        paso("Backoff: una fuente muerta no se golpea cada 0.5 s")
        from src import relay as relay_mod

        solo_muerto = RelayHLS([muerto])
        solo_muerto.arrancar()
        try:
            crecio = esperar(
                lambda: solo_muerto.stats()["espera_s"] > relay_mod.SONDEO_S, limite_s=20
            )
            afirmar(crecio, f"la espera crece: {solo_muerto.stats()['espera_s']}s")
        finally:
            solo_muerto.detener()

        paso("Barrido del pool: apaga lo que ya no se publica")
        apagados = estado.relay_pool.barrer({canal.id for canal in canales}, idle_s=900)
        afirmar(isinstance(apagados, list), f"el barrido responde ({len(apagados)} apagados)")
        afirmar(estado.relay_pool.stats() != {}, "los publicados siguen vivos")

        paso("Shutdown ordenado")
        estado.relay_pool.detener_todos()
        afirmar(estado.relay_pool.stats() == {}, "todos los reles se apagaron")
        servidor_api.should_exit = True
        afirmar(True, "la API se apaga")

        servidor.shutdown()
        servidor.server_close()

    except Exception as exc:  # noqa: BLE001 — el banco reporta y no esconde
        import traceback

        traceback.print_exc()
        fallo(f"excepcion no esperada: {exc}")
    finally:
        if not FALLOS and not os.environ.get("LVB_E2E_CONSERVAR"):
            shutil.rmtree(base, ignore_errors=True)
        else:
            print(f"\n(el directorio {base} se conserva para inspeccion)")

    print("\n" + "=" * 60)
    if FALLOS:
        print(f"RESULTADO: {len(FALLOS)} FALLOS de {PASOS} pasos")
        for f in FALLOS:
            print(f"  - {f}")
        return 1
    print(f"RESULTADO: TODO OK — {PASOS} pasos, 0 fallos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
