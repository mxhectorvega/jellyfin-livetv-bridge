"""Pruebas de ESTANDAR: que lo declarado y lo instalado no se separen.

- Discussion: ``pyproject.toml`` (PEP 621) y ``requirements.txt`` declaran las
  mismas dependencias en dos sitios. Dos listas que nadie compara se separan en
  cuanto alguien toca una; esta prueba convierte esa duplicacion en un
  invariante.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def _pyproject() -> dict:
    with (RAIZ / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)


def _requirements(nombre: str) -> set[str]:
    lineas = (RAIZ / nombre).read_text(encoding="utf-8").splitlines()
    return {ln.strip() for ln in lineas if ln.strip() and not ln.strip().startswith("#")}


def test_pyproject_declara_metadatos_pep621():
    proyecto = _pyproject()["project"]
    for campo in ("name", "version", "description", "requires-python", "license", "dependencies"):
        assert campo in proyecto, f"falta {campo} en [project]"


def test_las_dependencias_de_pyproject_y_requirements_coinciden():
    """Si alguien sube una version en un sitio y no en el otro, esto truena."""
    declaradas = set(_pyproject()["project"]["dependencies"])
    instaladas = _requirements("requirements.txt")
    assert declaradas == instaladas, f"solo en pyproject: {declaradas - instaladas}"


def test_las_dependencias_de_desarrollo_coinciden():
    declaradas = set(_pyproject()["project"]["optional-dependencies"]["dev"])
    instaladas = _requirements("requirements-dev.txt")
    assert declaradas == instaladas, f"solo en pyproject: {declaradas - instaladas}"


def test_la_version_del_paquete_y_la_del_modulo_coinciden():
    from src import __version__

    assert __version__ == _pyproject()["project"]["version"]


def test_el_limite_de_linea_esta_declarado_y_es_el_que_se_verifica():
    """PEP 8 pide 79; este proyecto declara 100 y ruff lo hace cumplir en CI."""
    ruff = _pyproject()["tool"]["ruff"]
    assert ruff["line-length"] == 100
    assert ruff["target-version"] == "py312"
    assert set(ruff["lint"]["select"]) == {"E", "F", "W", "I", "B", "UP"}


def test_el_ci_comprueba_el_estilo():
    """Un estandar que nadie comprueba es una intencion, no un estandar."""
    ci = (RAIZ / ".github/workflows/tests.yml").read_text(encoding="utf-8")
    assert "ruff check ." in ci
    assert "ruff format --check ." in ci
