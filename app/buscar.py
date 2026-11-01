"""Busca en el texto completo de los documentos del estudio (indice busqueda.db).

    python buscar.py "prescripcion adquisitiva"
    python buscar.py "\"tutela sindical\"" --area 01_Expedientes --caso 2022-perez-gomez --limite 30
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import busqueda
from config import BASE


def _relativa(ruta: str) -> str:
    try:
        return str(Path(ruta).relative_to(BASE))
    except ValueError:
        return ruta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("consulta")
    ap.add_argument("--area")
    ap.add_argument("--caso")
    ap.add_argument("--tipo")
    ap.add_argument("--limite", type=int, default=15)
    args = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if not busqueda.estado()["docs"]:
        print("No hay indice. Armarlo con: python busqueda.py --reconstruir")
        return 2
    r = busqueda.buscar(args.consulta, args.area, args.caso, args.tipo, args.limite)
    if not r["total"]:
        print("Sin resultados.")
        return 1
    mostrados = len(r["resultados"])
    print(f"{r['total']} resultado{'s' if r['total'] != 1 else ''} ({r['ms']} ms)"
          + (f", mostrando {mostrados}" if mostrados < r["total"] else ""))
    for i, x in enumerate(r["resultados"], 1):
        print(f"\n{i}. {_relativa(x['ruta'])}")
        print(f"   {x['fragmento']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
