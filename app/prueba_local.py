"""Dashboard de prueba en http://localhost:5051 sobre una base y una configuracion sinteticas en la carpeta temporal.

Nunca toca la base ni el config.json del usuario. Uso: python prueba_local.py [--limpiar]
"""
import json
import os
import runpy
import shutil
import sys
import tempfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = Path(tempfile.gettempdir()) / "organizador_colegiados_prueba"

if "--limpiar" in sys.argv:
    shutil.rmtree(RAIZ, ignore_errors=True)

base = RAIZ / "Organizador"
config = RAIZ / "config" / "config.json"
config.parent.mkdir(parents=True, exist_ok=True)
config.write_text(json.dumps({"estudio": "Estudio de prueba", "ubicacion": "", "apellidos": "", "base": str(base)}),
                  encoding="utf-8")
os.environ["ORGANIZADOR_CONFIG"] = str(config)
for variable in ("ORGANIZADOR_BASE", "ORGANIZADOR_APELLIDOS", "ORGANIZADOR_JUSTI_DIR", "ORGANIZADOR_OCR_CACHE",
                 "ORGANIZADOR_OCR_DIR", "ORGANIZADOR_BUSQUEDA_DB"):
    os.environ.pop(variable, None)
os.environ["ORGANIZADOR_CORPUS"] = str(RAIZ / "corpus")
sys.path.insert(0, str(AQUI))

from cartera import Causa, guardar, normalizar_expte  # noqa: E402

for d in ("01_Expedientes/2022-perez-gomez/01_siged", "_INBOX", "_SISTEMA",
          "escanear/Causa 666666-2024", "escanear/otros"):
    (base / d).mkdir(parents=True, exist_ok=True)
(base / "escanear/Causa 666666-2024/a.txt").write_text("x", encoding="utf-8")
(base / "escanear/otros/escrito 777777-2023.txt").write_text("x", encoding="utf-8")
if not (base / "_SISTEMA/cartera.json").exists():
    guardar([Causa(normalizar_expte("111111/2022"), caratula="PEREZ c/ GOMEZ", carpeta="2022-perez-gomez")],
            base / "_SISTEMA/cartera.json")

(base / "01_Expedientes/2022-perez-gomez/01_siged/escrito.txt").write_text(
    "Escrito de PEREZ c/ GOMEZ: la prescripción adquisitiva del inmueble. <b>no es HTML</b>", encoding="utf-8")

import busqueda  # noqa: E402
busqueda.sincronizar(base=base)

print(f"Base de prueba: {base}")
sys.argv = [str(AQUI / "dashboard.py"), "--port", "5051", "--no-abrir"]
runpy.run_path(str(AQUI / "dashboard.py"), run_name="__main__")
