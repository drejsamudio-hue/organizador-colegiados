"""Indice de texto completo (SQLite FTS5) de los documentos del estudio: _SISTEMA/busqueda.db.

Es derivado: se puede borrar y reconstruir con `python busqueda.py --reconstruir`. Contiene texto real de causas;
vive junto a los datos, no en el repo. Cubre 01_ a 06_ y _INBOX; no _SISTEMA ni _CUARENTENA.

    python busqueda.py --actualizar     # pone al dia el indice (archivos nuevos, movidos, cambiados, borrados)
    python busqueda.py --reconstruir    # lo arma de cero
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

import fitz  # PyMuPDF

import config
import ocr_completo
from config import BASE, BUSQUEDA_DB
from extractor import OCR_MINIMO, _extraer_html, _extraer_odt, _extraer_rtf, extraer_universal
from ocr_completo import hash_archivo, texto_por_hash

EXTENSIONES = {".pdf", ".docx", ".doc", ".md", ".txt", ".csv", ".htm", ".html", ".rtf", ".odt"}
_SOLO_INDICE = {".htm": _extraer_html, ".html": _extraer_html, ".rtf": _extraer_rtf, ".odt": _extraer_odt}
# Subir cuando cambie la extraccion: la proxima pasada relee una vez lo que habia quedado sin texto
VERSION_LECTURA = "3"
AREAS_CON_CASO = {"01_Expedientes", "06_Expedientes_ajenos"}
MAX_CHARS = 5_000_000
LOTE = 200

ESQUEMA = """
CREATE TABLE IF NOT EXISTS docs (
    id INTEGER PRIMARY KEY,
    ruta TEXT NOT NULL UNIQUE,
    hash TEXT NOT NULL,
    tamano INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    nombre TEXT NOT NULL,
    area TEXT NOT NULL,
    caso TEXT NOT NULL,
    tipo TEXT NOT NULL,
    fuente TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS docs_hash ON docs(hash);
CREATE INDEX IF NOT EXISTS docs_area ON docs(area, caso);
CREATE TABLE IF NOT EXISTS meta (clave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    contenido, nombre, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def conectar(db: Path | None = None) -> sqlite3.Connection:
    db = Path(db or BUSQUEDA_DB)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript(ESQUEMA)
    except BaseException:
        con.close()  # una base danada queda libre para apartarla
        raise
    return con


def apartar_danado(db: Path | None = None) -> Path:
    """Renombra un indice danado (y su diario) a .danado-<fecha> para armarlo de nuevo; es derivado, no se pierde nada."""
    db = Path(db or BUSQUEDA_DB)
    sello = datetime.now().strftime("%Y%m%d-%H%M%S")
    for sufijo in ("", "-wal", "-shm"):
        p = Path(str(db) + sufijo)
        if p.exists():
            p.replace(p.with_name(p.name + f".danado-{sello}"))
    return db


def conectar_lectura(db: Path | None = None) -> sqlite3.Connection | None:
    """Conexion de solo lectura (no crea tablas ni cambia el modo del diario); None si no hay indice.
    SQLite puede crear busqueda.db-shm al leer una base en WAL: es parte del mecanismo, no cambia el contenido."""
    db = Path(db or BUSQUEDA_DB)
    if not db.is_file():
        return None
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def _base(base: Path | None) -> Path:
    return Path(base or config.BASE)


def raices(base: Path) -> list[Path]:
    """Carpetas que se indexan: 01_ a 06_ y _INBOX."""
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and (p.name == "_INBOX" or re.match(r"0[1-6]_", p.name)))


def derivar(ruta: Path, base: Path) -> tuple[str, str, str]:
    """(area, caso, tipo) a partir de la ruta: se recalcula cuando el archivo cambia de lugar."""
    partes = Path(ruta).relative_to(base).parts
    area = partes[0]
    carpetas = partes[1:-1]
    if area in AREAS_CON_CASO and carpetas:
        return area, carpetas[0], "/".join(carpetas[1:])
    return area, "", "/".join(carpetas)


def en_alcance(ruta: Path, base: Path) -> bool:
    try:
        partes = Path(ruta).relative_to(base).parts
    except ValueError:
        return False
    return len(partes) >= 2 and (partes[0] == "_INBOX" or bool(re.match(r"0[1-6]_", partes[0]))) \
        and not _interna(partes) and Path(ruta).suffix.lower() in EXTENSIONES


# Registros propios del corpus juridico (bootstrap.csv, consultas pendientes): no son documentos
_INTERNAS_CORPUS = {"_sistema", "_pendientes"}


def _interna(partes: tuple[str, ...]) -> bool:
    return len(partes) >= 3 and partes[0].startswith("07_") and partes[1] in _INTERNAS_CORPUS


def consulta_fts(texto: str) -> str:
    """Texto escrito por una persona -> consulta FTS5 segura: palabras en AND, "frases", prefijo con *."""
    out = []
    for frase, palabra, prefijo in re.findall(r'"([^"]*)"|(\w+)(\*?)', texto):
        if palabra:
            out.append(f'"{palabra.lower()}"' + ("*" if prefijo else ""))
        else:
            palabras = re.findall(r"\w+", frase.lower())
            if palabras:
                out.append('"' + " ".join(palabras) + '"')
    return " ".join(out)


def _texto_pdf(path: Path, h: str | None = None) -> tuple[str | None, str]:
    try:
        with fitz.open(str(path)) as doc:
            if doc.is_encrypted and not doc.authenticate(""):
                return None, ""
            total, partes = 0, []
            for pg in doc:
                t = pg.get_text()
                partes.append(t)
                total += len(t)
                if total >= MAX_CHARS:
                    break
    except Exception:  # noqa: BLE001
        return None, ""
    texto = "\n".join(partes)
    ocr = texto_por_hash(path, h=h)
    if ocr:
        # PDF mixto (caratula con texto y el resto escaneado): se indexan las dos capas, aunque el texto sea corto
        if texto.strip():
            ocr = texto + "\n" + ocr
        return ocr[:MAX_CHARS], "ocr"
    return (texto, "pdf") if texto.strip() else (None, "")


def texto_de(path: Path, h: str | None = None) -> tuple[str | None, str]:
    """(texto, fuente). fuente: pdf, ocr, texto, docx, doc; texto None si no se pudo leer."""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _texto_pdf(path, h)
    if ext in _SOLO_INDICE:  # la ingesta no los clasifica; solo se buscan
        texto, _err = _SOLO_INDICE[ext](path, MAX_CHARS)
        return (texto, "texto") if texto and texto.strip() else (None, "")
    texto, err = extraer_universal(path, MAX_CHARS)
    if err == "doc_lectura_binaria" or not (texto and texto.strip()):
        return None, ""
    return texto, ext.lstrip(".") if ext in (".docx", ".doc") else "texto"


def _recorrer(base: Path, ilegibles: list[str] | None = None) -> dict[str, tuple[int, int]]:
    """Archivos indexables con (tamano, mtime_ns). Las carpetas que no se pudieron leer van a `ilegibles`."""
    actuales = {}
    for raiz in raices(base):
        for dirpath, dirs, archivos in os.walk(raiz, onerror=lambda e: ilegibles is not None and ilegibles.append(e.filename)):
            if Path(dirpath) == raiz and raiz.name.startswith("07_"):
                dirs[:] = [d for d in dirs if d not in _INTERNAS_CORPUS]
            for nombre in archivos:
                if Path(nombre).suffix.lower() in EXTENSIONES:
                    p = os.path.join(dirpath, nombre)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    actuales[p] = (st.st_size, st.st_mtime_ns)
    return actuales


def _quitar(con: sqlite3.Connection, doc_id: int) -> None:
    con.execute("DELETE FROM fts WHERE rowid = ?", (doc_id,))
    con.execute("DELETE FROM docs WHERE id = ?", (doc_id,))


def _ocr_nuevo(fila: sqlite3.Row) -> bool:
    """PDF indexado sin la capa de OCR cuyo OCR por hash (con texto) apareció después."""
    if fila["fuente"] not in ("", "pdf") or not fila["ruta"].lower().endswith(".pdf"):
        return False
    return ocr_completo.texto_guardado(ocr_completo.OCR_DIR / fila["hash"]) is not None


def _meta(ruta: Path, base: Path) -> tuple[str, str, str, str]:
    area, caso, tipo = derivar(ruta, base)
    return ruta.name, area, caso, tipo


def _insertar(con: sqlite3.Connection, ruta_s: str, base: Path, h: str, tam: int, mt: int) -> bool:
    """Agrega un archivo al indice; False si no se le pudo sacar texto (igual queda, se busca por nombre)."""
    ruta = Path(ruta_s)
    texto, fuente = texto_de(ruta, h)
    nombre, area, caso, tipo = _meta(ruta, base)
    cur = con.execute("INSERT INTO docs (ruta, hash, tamano, mtime_ns, nombre, area, caso, tipo, fuente) "
                      "VALUES (?,?,?,?,?,?,?,?,?)", (ruta_s, h, tam, mt, nombre, area, caso, tipo, fuente))
    con.execute("INSERT INTO fts (rowid, contenido, nombre) VALUES (?,?,?)", (cur.lastrowid, texto or "", nombre))
    return texto is not None


def sincronizar(base: Path | None = None, db: Path | None = None, avisar=None) -> dict:
    """Pone el indice al dia. Un archivo movido (mismo contenido, otra ruta) solo cambia de ruta: no se vuelve a leer."""
    base = _base(base)
    if not base.is_dir():
        raise OSError(f"la base {base} no esta disponible (disco desconectado?): el indice no se toca")
    con = None
    try:
        con = conectar(db)
        existentes = {f["ruta"]: f for f in con.execute("SELECT * FROM docs")}
    except sqlite3.DatabaseError:
        if con is not None:
            con.close()
        con = conectar(apartar_danado(db))
        existentes = {}
    r = {"nuevos": 0, "movidos": 0, "cambiados": 0, "borrados": 0, "sin_texto": 0, "errores": 0}
    try:
        ilegibles: list[str] = []
        actuales = _recorrer(base, ilegibles)
        # lo que esta bajo una raiz que falta o una carpeta que no se pudo leer no se da por borrado
        presentes = {r_.name for r_ in raices(base)}
        intocables = tuple(os.path.join(c, "") for c in ilegibles)

        def desaparecio(ruta: str) -> bool:
            if ruta in actuales or ruta.startswith(intocables):
                return False
            try:
                return Path(ruta).relative_to(base).parts[0] in presentes
            except ValueError:
                return True

        desaparecidos = {ruta: f for ruta, f in existentes.items() if desaparecio(ruta)}
        por_hash: dict[str, list[sqlite3.Row]] = {}
        for f in desaparecidos.values():
            por_hash.setdefault(f["hash"], []).append(f)
        version = con.execute("SELECT valor FROM meta WHERE clave = 'version_lectura'").fetchone()
        releer_sin_texto = version is None or version[0] != VERSION_LECTURA

        def releer(f: sqlite3.Row) -> bool:
            return _ocr_nuevo(f) or (releer_sin_texto and f["fuente"] in ("", "ocr"))

        pendientes = [p for p, (tam, mt) in actuales.items()
                      if p not in existentes or (existentes[p]["tamano"], existentes[p]["mtime_ns"]) != (tam, mt)
                      or releer(existentes[p])]
        for i, ruta_s in enumerate(sorted(pendientes), 1):
            ruta, (tam, mt) = Path(ruta_s), actuales[ruta_s]
            try:
                h = hash_archivo(ruta)
                previo = existentes.get(ruta_s)
                if previo is not None and previo["hash"] == h and not releer(previo):
                    con.execute("UPDATE docs SET tamano=?, mtime_ns=? WHERE id=?", (tam, mt, previo["id"]))
                    continue
                candidatos = por_hash.get(h) or []
                nombre, area, caso, tipo = _meta(ruta, base)
                if previo is None and candidatos:
                    viejo = candidatos.pop()
                    desaparecidos.pop(viejo["ruta"], None)
                    con.execute("UPDATE docs SET ruta=?, tamano=?, mtime_ns=?, nombre=?, area=?, caso=?, tipo=? WHERE id=?",
                                (ruta_s, tam, mt, nombre, area, caso, tipo, viejo["id"]))
                    con.execute("UPDATE fts SET nombre=? WHERE rowid=?", (nombre, viejo["id"]))
                    r["movidos"] += 1
                    continue
                if previo is not None:
                    _quitar(con, previo["id"])
                    r["cambiados"] += 1
                else:
                    r["nuevos"] += 1
                r["sin_texto"] += not _insertar(con, ruta_s, base, h, tam, mt)
            except Exception as exc:  # noqa: BLE001
                r["errores"] += 1
                if avisar:
                    avisar(f"[error] {ruta_s}: {exc}")
            if i % LOTE == 0:
                con.commit()
                if avisar:
                    avisar(f"{i}/{len(pendientes)}")
        for f in desaparecidos.values():
            _quitar(con, f["id"])
            r["borrados"] += 1
        con.execute("INSERT OR REPLACE INTO meta (clave, valor) VALUES ('version_lectura', ?)", (VERSION_LECTURA,))
        con.execute("INSERT OR REPLACE INTO meta (clave, valor) VALUES ('actualizado', ?)",
                    (datetime.now().isoformat(timespec="seconds"),))
        con.commit()
    finally:
        con.close()
    return r


def registrar_movimiento(origen: Path, destino: Path, base: Path | None = None, db: Path | None = None) -> bool:
    """Actualiza la ruta de un archivo que acaba de moverse (ingesta, deshacer). No crea el indice si no existe
    y nunca falla: lo que se escape lo corrige la proxima sincronizacion."""
    db = Path(db or BUSQUEDA_DB)
    if not db.is_file():
        return False
    base = _base(base)
    try:
        con = conectar(db)
        try:
            fila = con.execute("SELECT id FROM docs WHERE ruta=?", (str(origen),)).fetchone()
            if fila is None:
                # llego despues de la ultima sincronizacion: se indexa ya en su lugar nuevo
                destino = Path(destino)
                if not en_alcance(destino, base) or \
                        con.execute("SELECT 1 FROM docs WHERE ruta=?", (str(destino),)).fetchone():
                    return False
                st = destino.stat()
                _insertar(con, str(destino), base, hash_archivo(destino), st.st_size, st.st_mtime_ns)
                con.commit()
                return True
            if en_alcance(destino, base):
                nombre, area, caso, tipo = _meta(Path(destino), base)
                st = Path(destino).stat()
                con.execute("UPDATE docs SET ruta=?, tamano=?, mtime_ns=?, nombre=?, area=?, caso=?, tipo=? WHERE id=?",
                            (str(destino), st.st_size, st.st_mtime_ns, nombre, area, caso, tipo, fila["id"]))
                con.execute("UPDATE fts SET nombre=? WHERE rowid=?", (nombre, fila["id"]))
            else:
                _quitar(con, fila["id"])
            con.commit()
            return True
        finally:
            con.close()
    except Exception:  # noqa: BLE001
        return False


def buscar(consulta: str, area: str | None = None, caso: str | None = None, tipo: str | None = None,
           limite: int = 20, db: Path | None = None) -> dict:
    """{'total', 'resultados': [{ruta, nombre, area, caso, tipo, fuente, fragmento}], 'ms'}, mejores primero."""
    t0 = time.perf_counter()
    q = consulta_fts(consulta)
    if not q:
        return {"total": 0, "resultados": [], "ms": 0}
    filtros, params = "", [q]
    for campo, valor in (("area", area), ("caso", caso), ("tipo", tipo)):
        if valor:
            filtros += f" AND d.{campo} = ?"
            params.append(valor)
    con = conectar_lectura(db)
    if con is None:
        return {"total": 0, "resultados": [], "ms": 0, "sin_indice": True}
    try:
        total = con.execute(f"SELECT count(*) FROM fts JOIN docs d ON d.id = fts.rowid WHERE fts MATCH ?{filtros}",
                            params).fetchone()[0]
        filas = con.execute(
            f"SELECT d.ruta, d.nombre, d.area, d.caso, d.tipo, d.fuente, "
            f"snippet(fts, -1, '«', '»', ' … ', 24) AS fragmento "
            f"FROM fts JOIN docs d ON d.id = fts.rowid WHERE fts MATCH ?{filtros} "
            f"ORDER BY bm25(fts, 1.0, 5.0) LIMIT ?", [*params, limite]).fetchall()
    except sqlite3.DatabaseError as exc:
        if "fts5" in str(exc).lower() or "syntax" in str(exc).lower():
            raise
        return {"total": 0, "resultados": [], "ms": 0, "danado": True}
    finally:
        con.close()
    return {"total": total, "resultados": [dict(f) for f in filas], "ms": round((time.perf_counter() - t0) * 1000)}


def estado(db: Path | None = None) -> dict:
    """{'docs', 'areas', 'actualizado'} del indice; sin indice, ceros (y no lo crea)."""
    con = conectar_lectura(db)
    if con is None:
        return {"docs": 0, "areas": [], "actualizado": None}
    try:
        docs = con.execute("SELECT count(*) FROM docs").fetchone()[0]
        areas = [f[0] for f in con.execute("SELECT DISTINCT area FROM docs ORDER BY area")]
        act = con.execute("SELECT valor FROM meta WHERE clave = 'actualizado'").fetchone()
    except sqlite3.DatabaseError:
        return {"docs": 0, "areas": [], "actualizado": None, "danado": True}
    finally:
        con.close()
    return {"docs": docs, "areas": areas, "actualizado": act[0] if act else None}


def ruta_indexada(ruta: str, db: Path | None = None) -> bool:
    """True solo si la ruta, escrita exactamente asi, esta en el indice."""
    con = conectar_lectura(db)
    if con is None:
        return False
    try:
        return con.execute("SELECT 1 FROM docs WHERE ruta = ?", (ruta,)).fetchone() is not None
    except sqlite3.DatabaseError:
        return False
    finally:
        con.close()


def _prioridad_baja() -> None:
    if os.name == "nt":
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x00004000)


def main() -> None:
    ap = argparse.ArgumentParser(description="Indice de texto completo del estudio.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--actualizar", action="store_true")
    g.add_argument("--reconstruir", action="store_true", help="borra el indice y lo arma de cero")
    args = ap.parse_args()
    sys.stdout.reconfigure(errors="replace")
    _prioridad_baja()
    if args.reconstruir:
        for sufijo in ("", "-wal", "-shm"):
            Path(str(BUSQUEDA_DB) + sufijo).unlink(missing_ok=True)
    t0 = time.monotonic()
    r = sincronizar(avisar=lambda m: print(m, flush=True))
    print(" | ".join(f"{k}: {v}" for k, v in r.items()), f"| {round(time.monotonic() - t0)} s")


if __name__ == "__main__":
    main()
