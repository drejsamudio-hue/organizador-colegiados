"""OCR de todas las paginas de un PDF escaneado. La salida (TXT; el PDF buscable solo con --pdf) se guarda por hash del
contenido, no por ruta: el organizador mueve y renombra archivos y la salida tiene que seguirlos.

    OCR_DIR/<sha256>/buscable.pdf
    OCR_DIR/<sha256>/texto.txt

Nunca toca el original. Si la salida ya existe y esta completa, se reutiliza.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

from config import BASE, BUSQUEDA_DB, OCR_DIR, TESSDATA
from extractor import OCR_DPI, calidad_texto, ocr_disponible

MUESTRA_MINIMA = 30
LADO_MAXIMO = 6000
JPEG_CALIDAD = 70
PDF_BUSCABLE = "buscable.pdf"
TXT = "texto.txt"
LOG = "ocr_log.csv"
CAMPOS_LOG = ["fecha", "archivo", "hash", "estado", "paginas", "calidad", "segundos"]


def hash_archivo(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _legible(path: Path) -> bool:
    try:
        with fitz.open(str(path)) as doc:
            return not doc.is_encrypted and doc.page_count > 0
    except Exception:  # noqa: BLE001
        return False


def es_escaneado(path: Path) -> bool:
    """Escaneado si mas de la mitad de la primera, la del medio y la ultima pagina casi no tiene texto.
    Mirar solo la primera pagina sobreestima (portadas, caratulas)."""
    try:
        with fitz.open(str(path)) as doc:
            if doc.is_encrypted or doc.page_count == 0:
                return False
            n = doc.page_count
            muestra = sorted({0, n // 2, n - 1})
            vacias = sum(len(doc[i].get_text().strip()) < MUESTRA_MINIMA for i in muestra)
            return vacias * 2 > len(muestra)
    except Exception:  # noqa: BLE001
        return False


def dpi_para(ancho_pt: float, alto_pt: float, dpi: int = OCR_DPI) -> int:
    """Baja el dpi si el lado mayor de la pagina rasterizada pasaria de LADO_MAXIMO pixeles."""
    lado = max(ancho_pt, alto_pt)
    if lado * dpi / 72 <= LADO_MAXIMO:
        return dpi
    return max(72, int(LADO_MAXIMO * 72 / lado))


def texto_guardado(carpeta: Path) -> str | None:
    """Texto de una salida por hash; None si falta o quedo vacia. El PDF buscable no hace falta para leerlo."""
    txt = carpeta / TXT
    try:
        if not txt.is_file() or txt.stat().st_size == 0:
            return None
        texto = txt.read_text(encoding="utf-8")
    except OSError:
        return None
    return texto if texto.strip() else None


def _completo(carpeta: Path, reintentar_vacios: bool = False) -> bool:
    if reintentar_vacios:
        return texto_guardado(carpeta) is not None
    return (carpeta / TXT).is_file()


def _resultado(estado: str, h: str, carpeta: Path | None = None, paginas: int = 0, calidad: float | None = None) -> dict:
    r = {"estado": estado, "hash": h, "paginas": paginas, "calidad": calidad, "pdf": None, "txt": None}
    if carpeta is not None:
        r["txt"] = carpeta / TXT
        r["pdf"] = carpeta / PDF_BUSCABLE if (carpeta / PDF_BUSCABLE).is_file() else None
    return r


def texto_por_hash(path: Path, destino: Path | None = None, h: str | None = None) -> str | None:
    """Texto del OCR completo de este contenido, si ya se hizo (esté donde esté el archivo ahora).
    `h` evita volver a leer el archivo entero cuando el hash ya se calculo."""
    return texto_guardado((destino or OCR_DIR) / (h or hash_archivo(Path(path))))


def ocr_completo(path: Path, destino: Path | None = None, simular: bool = False,
                 reintentar_vacios: bool = False, h: str | None = None, con_pdf: bool = False) -> dict:
    """Estados: ok, reutilizado, con_texto (no hace falta OCR), ilegible, sin_ocr (falta Tesseract/idioma),
    pendiente (solo en simulacion: habria que hacer el OCR). El indice solo usa el texto: el PDF buscable
    (pesado, ~50 veces el texto) se genera solo si se pide."""
    path = Path(path)
    destino = destino or OCR_DIR
    h = h or hash_archivo(path)
    carpeta = destino / h
    if _completo(carpeta, reintentar_vacios):
        return _resultado("reutilizado", h, carpeta)
    if not _legible(path):
        return _resultado("ilegible", h)
    if not es_escaneado(path):
        return _resultado("con_texto", h)
    if not ocr_disponible():
        return _resultado("sin_ocr", h)
    if simular:
        return _resultado("pendiente", h)

    carpeta.mkdir(parents=True, exist_ok=True)
    # temporal propio de esta corrida: dos corridas sobre el mismo contenido (la consola y el panel) no se pisan
    tmp = Path(tempfile.mkdtemp(prefix="tmp-", dir=carpeta))
    try:
        textos = []
        with fitz.open() as salida:
            with fitz.open(str(path)) as doc:
                paginas = doc.page_count
                for pg in doc:
                    dpi = dpi_para(pg.rect.width, pg.rect.height)
                    pix = pg.get_pixmap(dpi=dpi)
                    # PyMuPDF no hace OCR sobre un pixmap en gris (devuelve vacio): se reconoce en RGB y se reemplaza la imagen
                    with fitz.open("pdf", pix.pdfocr_tobytes(language="spa", tessdata=str(TESSDATA))) as una:
                        textos.append(una[0].get_text())
                        if not con_pdf:
                            continue
                        gris = fitz.Pixmap(fitz.csGRAY, pix)
                        una[0].replace_image(una[0].get_images()[0][0], stream=gris.tobytes("jpeg", jpg_quality=JPEG_CALIDAD))
                        salida.insert_pdf(una)
            if con_pdf:
                salida.save(str(tmp / PDF_BUSCABLE), garbage=3, deflate=True)
        texto = "\n\f\n".join(textos)
        (tmp / TXT).write_text(texto, encoding="utf-8")
        if con_pdf:
            (tmp / PDF_BUSCABLE).replace(carpeta / PDF_BUSCABLE)
        (tmp / TXT).replace(carpeta / TXT)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _resultado("ok", h, carpeta, paginas, calidad_texto(texto))


def _pdfs(carpetas: list[Path], excluir: Path) -> list[Path]:
    salida = []
    for carpeta in carpetas:
        for raiz, dirs, archivos in os.walk(carpeta):
            dirs[:] = sorted(d for d in dirs if d != "_SISTEMA" and Path(raiz, d) != excluir)
            salida += [Path(raiz) / a for a in sorted(archivos) if a.lower().endswith(".pdf")]
    return salida


def _hashes_conocidos(db: Path | None) -> dict[str, tuple[str, int, int]]:
    """ruta -> (hash, tamano, mtime_ns) segun el indice de busqueda: evita releer cada PDF entero en el disco."""
    db = Path(db or BUSQUEDA_DB)
    if not db.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            return {r: (h, t, m) for r, h, t, m in con.execute("SELECT ruta, hash, tamano, mtime_ns FROM docs")}
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def _hash_rapido(pdf: Path, conocidos: dict) -> str | None:
    previo = conocidos.get(str(pdf))
    if previo is None:
        return None
    st = pdf.stat()
    return previo[0] if (previo[1], previo[2]) == (st.st_size, st.st_mtime_ns) else None


def _anotar(log: Path, fila: dict) -> None:
    log.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not log.exists()
    with log.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CAMPOS_LOG, extrasaction="ignore")
        if nuevo:
            w.writeheader()
        w.writerow(fila)


def procesar_lote(carpetas: list[Path], destino: Path | None = None, log: Path | None = None,
                  simular: bool = False, limite: int | None = None, avisar=None,
                  reintentar_vacios: bool = False, db: Path | None = None, con_pdf: bool = False) -> list[dict]:
    """OCR completo de todos los PDF escaneados bajo `carpetas`. Reanudable: lo ya hecho se reconoce por hash.
    `limite` cuenta los OCR realizados (no los reutilizados). Un error en un archivo no corta el lote."""
    destino = Path(destino or OCR_DIR)
    log = Path(log) if log else destino / LOG
    filas, hechos = [], 0
    conocidos = _hashes_conocidos(db)
    for pdf in _pdfs([Path(c) for c in carpetas], destino):
        if limite is not None and hechos >= limite:
            break
        t0 = time.monotonic()
        try:
            r = ocr_completo(pdf, destino, simular, reintentar_vacios, _hash_rapido(pdf, conocidos), con_pdf)
            fila = {"hash": r["hash"], "estado": r["estado"], "paginas": r["paginas"],
                    "calidad": "" if r["calidad"] is None else round(r["calidad"], 3)}
        except Exception as exc:  # noqa: BLE001
            fila = {"hash": "", "estado": f"error: {exc}", "paginas": 0, "calidad": ""}
        fila |= {"fecha": datetime.now().isoformat(timespec="seconds"), "archivo": str(pdf),
                 "segundos": round(time.monotonic() - t0, 1)}
        hechos += fila["estado"] == "ok"
        if not simular:
            _anotar(log, fila)
        filas.append(fila)
        if avisar:
            avisar(fila)
    return filas


def _prioridad_baja() -> None:
    if os.name != "nt":
        return
    import ctypes
    ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL


def main() -> None:
    ap = argparse.ArgumentParser(description="OCR completo de los PDF escaneados (reanudable, no toca originales).")
    ap.add_argument("carpetas", nargs="*", type=Path,
                    help="por defecto toda la base indexada (01_ a 06_ y _INBOX; no _SISTEMA ni _CUARENTENA)")
    ap.add_argument("--simular", action="store_true", help="solo lista lo que haria")
    ap.add_argument("--limite", type=int, help="maximo de OCR a realizar en esta corrida")
    ap.add_argument("--pdf", action="store_true", help="genera tambien el PDF buscable (pesado; el indice no lo usa)")
    ap.add_argument("--reintentar-vacios", action="store_true", help="rehace las salidas que quedaron sin texto")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")
    _prioridad_baja()
    cuenta: dict[str, int] = {}

    def avisar(fila: dict) -> None:
        cuenta[fila["estado"]] = cuenta.get(fila["estado"], 0) + 1
        if fila["estado"] in ("ok", "pendiente") or fila["estado"].startswith("error"):
            print(f'{fila["estado"]:10} {fila["paginas"]:>4} pag {fila["segundos"]:>6} s  {Path(fila["archivo"]).name}', flush=True)

    import busqueda  # aca y no arriba: busqueda importa este modulo
    carpetas = args.carpetas or busqueda.raices(BASE)
    try:
        procesar_lote(carpetas, simular=args.simular, limite=args.limite, avisar=avisar,
                      reintentar_vacios=args.reintentar_vacios, con_pdf=args.pdf)
    except KeyboardInterrupt:
        print("Interrumpido: lo hecho queda guardado; volver a correr continua.")
    print(" | ".join(f"{k}: {v}" for k, v in sorted(cuenta.items())) or "Nada para procesar.")
    if cuenta.get("sin_ocr"):
        print("[AVISO] hay PDF escaneados sin procesar porque falta el idioma del OCR: "
              "instalar el idioma espanol desde el panel (seccion de OCR) o copiar spa.traineddata a "
              f"{TESSDATA}.")
    if cuenta.get("ok") and BUSQUEDA_DB.is_file():
        r = busqueda.sincronizar(base=BASE)
        print("Indice de busqueda: " + " | ".join(f"{k}: {v}" for k, v in r.items()))


if __name__ == "__main__":
    main()
