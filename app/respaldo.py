"""Copias de seguridad de los datos (cartera, historial, aliases) y del codigo.

    python respaldo.py --codigo DESTINO   # git bundle del repo en DESTINO (por defecto _SISTEMA\\_respaldos\\codigo)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

CONSERVAR = 20
CARPETA = "_respaldos"


def escribir_atomico(ruta: Path, texto: str) -> None:
    """Escribe en un temporal de la misma carpeta y lo cambia de nombre: un corte nunca deja el archivo a medias."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    fd, nombre = tempfile.mkstemp(dir=ruta.parent, prefix=ruta.name + ".", suffix=".tmp")
    tmp = Path(nombre)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(texto)
        tmp.replace(ruta)
    finally:
        tmp.unlink(missing_ok=True)


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def respaldar(ruta: Path, conservar: int = CONSERVAR) -> Path | None:
    """Copia `ruta` a `<carpeta>/_respaldos/<nombre>.<fecha>.bak` si cambio desde la ultima copia.

    Devuelve la copia nueva, o None si no hacia falta. Conserva las ultimas `conservar` por archivo.
    """
    if not ruta.is_file():
        return None
    destino = ruta.parent / CARPETA
    previas = sorted(destino.glob(f"{ruta.name}.*.bak")) if destino.is_dir() else []
    if previas and previas[-1].stat().st_size == ruta.stat().st_size and _hash(previas[-1]) == _hash(ruta):
        return None
    destino.mkdir(exist_ok=True)
    copia = destino / f"{ruta.name}.{datetime.now():%Y%m%d-%H%M%S-%f}.bak"
    tmp = copia.with_name(copia.name + ".tmp")  # un corte a mitad de copia no deja un .bak truncado como "ultima copia"
    try:
        shutil.copy2(ruta, tmp)
        tmp.replace(copia)
    finally:
        tmp.unlink(missing_ok=True)
    previas.append(copia)
    for vieja in previas[:-conservar] if conservar > 0 else []:
        vieja.unlink(missing_ok=True)
    return copia


def respaldar_codigo(destino: Path) -> Path:
    repo = Path(__file__).resolve().parent
    destino.mkdir(parents=True, exist_ok=True)
    salida = destino / f"organizador-{datetime.now():%Y%m%d-%H%M%S}.bundle"
    try:
        subprocess.run(["git", "bundle", "create", str(salida), "--all"], cwd=repo, check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "bundle", "verify", str(salida)], cwd=repo, check=True, capture_output=True, text=True)
    except BaseException:
        salida.unlink(missing_ok=True)  # un bundle que no verifica no es un respaldo
        raise
    return salida


if __name__ == "__main__":
    from config import BASE

    ap = argparse.ArgumentParser()
    ap.add_argument("--codigo", type=Path, nargs="?", const=BASE / "_SISTEMA" / CARPETA / "codigo",
                    help="crea un git bundle del codigo en esta carpeta")
    args = ap.parse_args()
    if args.codigo is None:
        ap.error("indica --codigo [DESTINO]")
    try:
        print(f"Respaldo del codigo: {respaldar_codigo(args.codigo)}")
    except subprocess.CalledProcessError as exc:
        print(exc.stderr or exc, file=sys.stderr)
        sys.exit(1)
