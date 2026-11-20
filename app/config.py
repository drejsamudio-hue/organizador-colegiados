"""Configuracion del organizador: variables de entorno > config.json del usuario > valores por defecto."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path


def _ruta(variable: str, defecto: str | Path) -> Path:
    return Path(os.environ.get(variable) or defecto)


CONFIG_PATH = _ruta("ORGANIZADOR_CONFIG", Path(os.environ.get("APPDATA") or Path.home()) / "Organizador" / "config.json")


def _leer() -> dict:
    try:
        datos = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return datos if isinstance(datos, dict) else {}


_CFG = _leer()
CONFIGURADO = bool(_CFG) or bool(os.environ.get("ORGANIZADOR_BASE"))
ESTUDIO = str(_CFG.get("estudio") or "")
UBICACION = str(_CFG.get("ubicacion") or "")

BASE = _ruta("ORGANIZADOR_BASE", _CFG.get("base") or Path.home() / "Organizador")
JUSTI_DIR = _ruta("ORGANIZADOR_JUSTI_DIR", BASE / "_SISTEMA" / "escritorio")
def _tessdata() -> Path:
    """Carpeta con spa.traineddata: la del usuario (donde se instala), una junto al .exe o ~/tessdata."""
    propia = CONFIG_PATH.parent / "tessdata"
    candidatas = [propia, Path.home() / "tessdata"]
    if getattr(sys, "frozen", False):
        candidatas.insert(1, Path(sys.executable).parent / "tessdata")
    return next((c for c in candidatas if (c / "spa.traineddata").is_file()), propia)


TESSDATA = _ruta("TESSDATA_PREFIX", _tessdata())
OCR_CACHE = _ruta("ORGANIZADOR_OCR_CACHE", BASE / "_SISTEMA" / "ocr_cache")
OCR_DIR = _ruta("ORGANIZADOR_OCR_DIR", BASE / "_SISTEMA" / "ocr_por_hash")
BUSQUEDA_DB = _ruta("ORGANIZADOR_BUSQUEDA_DB", BASE / "_SISTEMA" / "busqueda.db")
# antiword viene con Git para Windows, pero su carpeta no suele estar en el PATH; sin antiword los .doc se leen en binario
ANTIWORD = _ruta("ORGANIZADOR_ANTIWORD", shutil.which("antiword") or r"C:\Program Files\Git\mingw64\bin\antiword.exe")
CORPUS = _ruta("ORGANIZADOR_CORPUS", BASE / "_CORPUS")


def _apellidos() -> tuple[str, ...]:
    crudo = os.environ.get("ORGANIZADOR_APELLIDOS")
    lista = crudo.split(",") if crudo is not None else _CFG.get("apellidos") or []
    if isinstance(lista, str):
        lista = lista.split(",")
    return tuple(a.strip().lower() for a in lista if isinstance(a, str) and a.strip())


APELLIDOS_PROPIOS = _apellidos()

HOSTS_LOCALES = frozenset({"localhost", "127.0.0.1"})


def _host(texto: str) -> str:
    """Nombre de host de una entrada escrita a mano: sin esquema, sin ruta y sin puerto."""
    h = texto.strip().lower().split("://", 1)[-1].split("/", 1)[0]
    return h.rsplit(":", 1)[0] if h.count(":") == 1 else h


def hosts_extra() -> list[str]:
    """Hosts adicionales con los que se puede hablar al dashboard (p. ej. un tunel propio): los de la variable
    ORGANIZADOR_HOSTS (separados por coma) y los de _SISTEMA/hosts_permitidos.txt (uno por linea, # comenta).
    Por defecto no hay ninguno: solo localhost. El tunel debe traer su propia autenticacion; el dashboard no la tiene."""
    out: list[str] = []
    crudos = os.environ.get("ORGANIZADOR_HOSTS", "").replace(";", ",").split(",")
    try:
        crudos += (BASE / "_SISTEMA" / "hosts_permitidos.txt").read_text(encoding="utf-8-sig").splitlines()  # sin BOM al frente
    except OSError:
        pass
    for crudo in crudos:
        h = _host(crudo.split("#", 1)[0])
        if h and h not in HOSTS_LOCALES and h not in out:
            out.append(h)
    return out


def hosts_permitidos() -> set[str]:
    """localhost siempre, mas hosts_extra(); se lee en cada llamada para que el archivo se pueda editar sin reiniciar."""
    return set(HOSTS_LOCALES) | set(hosts_extra())


HOSTS_EXTRA = hosts_extra()


def guardar_config(datos: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_PATH)
