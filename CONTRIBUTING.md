# Contribuir

## Como probar y verificar

```bash
# Entorno
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# Suite de pytest (sin tocar la red: todo mockeado)
.venv/bin/python -m pytest -q

# Estilo (se COMPRUEBA, no se pide por favor)
.venv/bin/ruff check .
.venv/bin/ruff format --check .

# Bancos: NO corren en CI porque necesitan red contra el CDN real
# E2E: genera HLS real con ffmpeg, sirve por HTTP y pasa la cadena completa
.venv/bin/python tests/e2e_sintetico.py

# Cosido de PCR/PTS/DTS sobre una captura real del rele
.venv/bin/python tests/banco_ts.py /ruta/a/captura.ts

# Failover contra streams REALES del catalogo
.venv/bin/python tests/banco_failover.py <canal> [<canal_ajeno>]
```

## Reglas de estilo

- Linea maxima: **100 columnas** (fijada en `pyproject.toml`, verifica ruff).
- Comillas dobles (fijada en `pyproject.toml`).
- Ruff es la unica autoridad; no hay `black`, `isort`, ni `flake8` por separado.

## Que NO entra al repositorio

- **Secretos**: tokens, llaves, URLs firmadas, credenciales.
- **IPs de LAN** reales (ni en docs, ni en tests, ni en ejemplos). Usa `example.invalid` (RFC 2606) o variables de entorno.
- Archivos de estado: `config/sources.yml`, `data/state.json`, `.env`, `*.bak`.

## Reportar problemas

- Abre un **issue** en GitHub con la plantilla correspondiente.
- Para fallos de seguridad: usa el reporte privado (`Security` > `Report a vulnerability`), no un issue publico. Ver `SECURITY.md`.

## Citas de normas

Toda cita de norma lleva su **seccion exacta** (ver `docs/ESTANDARES.md`). Una cita sin seccion es una mencion, no una cita.

## Pull requests

- Pruebas verdes (`pytest -q`).
- Ruff limpio (`ruff check . && ruff format --check .`).
- Documentacion actualizada si cambia el comportamiento (README, docs/).
- Sin secretos ni IPs de LAN.
- Entrada en `CHANGELOG.md` si aplica (formato Keep a Changelog).
