# Pull Request

## Checklist

- [ ] Pruebas verdes: `.venv/bin/python -m pytest -q` (210 passed)
- [ ] Ruff limpio: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
- [ ] Documentacion actualizada si cambia el comportamiento (README, docs/CONFIGURACION.md, docs/ESTANDARES.md, CHANGELOG.md)
- [ ] Sin secretos, tokens, URLs firmadas, ni IPs de LAN en el diff
- [ ] Entrada en `CHANGELOG.md` si aplica (formato Keep a Changelog, seccion correcta: Added/Changed/Fixed/Removed/Security)

## Descripcion

Breve: que cambia y por que. Si cierra un issue, `Closes #<numero>`.
