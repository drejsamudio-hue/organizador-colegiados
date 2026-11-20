"""Ingesta continua: rutea lo que haya en _INBOX hacia la estructura.

Uso:
    python ingesta.py            # rutea todo lo que encuentre en _INBOX
    python ingesta.py --dry-run  # muestra a donde iria cada archivo, sin mover
    python ingesta.py --src DIR  # rutea desde otra carpeta (ej: una carpeta de descargas)
    python ingesta.py --csv RUTA # ademas guarda las propuestas (destino, motivo, confianza)
    python ingesta.py --omitir expediente_nuevo   # esos motivos no se mueven (solo motivos conocidos, ver MOTIVOS)
    python ingesta.py --src DIR --incluir-no-clasificados  # tambien saca lo no reconocido a _INBOX
    python ingesta.py --aplicar-csv RUTA  # mueve lo aprobado en un CSV de simulacion (accion=mover), sin reclasificar

Cada fila de propuesta lleva el hash del contenido en el momento de simular: al aplicar, un archivo que cambio
queda en origen, y una propuesta sin hash (anterior a esta verificacion) no mueve nada. Tandas (aplicar o
deshacer) de a una por vez, en todos los procesos. La zona del sistema (_SISTEMA, indices, propuestas) nunca entra.

Soporta PDF, .txt, .md, .docx, .doc. Cada movimiento queda registrado en
_SISTEMA/INGESTA_RUTEO.csv (origen, destino) para poder deshacerlo.
Orden de decision: cartera propia (id de SIGED o numero) -> expediente -> alias ->
jurisprudencia/legislacion -> nombre de partes -> doctrina (rama, especialidad, tipo).

Confianza: "alta" solo cuando hay una senal fuerte. Un texto con forma de sentencia que termina como
doctrina, o que cita un tribunal pero no tiene marcas de sentencia, sale como maximo "media". La cartera
solo rutea a carpetas que existen en disco; un cartera.json o aliases_cartera.json ilegible se ignora
con un aviso en stderr.
"""
from __future__ import annotations

import argparse
import csv
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).parent))
import busqueda  # noqa: E402
from cartera import cargar as cargar_cartera  # noqa: E402
from doctrina import clasificar_doctrina, ruta_doctrina, tema_de  # noqa: E402
from extractor import extraer_universal, paginas_pdf  # noqa: E402
from fase2_expedientes import (  # noqa: E402
    JURIS_RE, MATRICULA_RE, OWNER_RE, buscar_caratula, buscar_expte, destino_unico,
)
from fase3_juris import FALLO, clasificar  # noqa: E402
from fase6b_renombres import RENOMBRES  # noqa: E402

from config import APELLIDOS_PROPIOS, BASE  # noqa: E402
from ocr_completo import hash_archivo  # noqa: E402
from respaldo import respaldar  # noqa: E402
import kb_index  # noqa: E402
import kb_publicar  # noqa: E402
INBOX = BASE / "_INBOX"
CASOS = BASE / "01_Expedientes"
SISTEMA = BASE / "_SISTEMA"
RUTEO_CSV = SISTEMA / "INGESTA_RUTEO.csv"
TANDAS_CSV = SISTEMA / "TANDAS.csv"
CARTERA_JSON = SISTEMA / "cartera.json"
ALIAS_CARTERA = SISTEMA / "aliases_cartera.json"
ALIAS_FILE = SISTEMA / "aliases.json"

IGNORAR = {"_sin_texto", "sin_texto", "sin_clasificar", "formato_desconocido",
           "desde_Organizado", "expediente_dudoso", "no_pdf"}

# Zona interna del sistema: nunca entra en una tanda, ni de origen ni de destino. Elegir la carpeta del estudio
# como origen es un error facil y no puede terminar moviendo la cartera, el indice o el registro de movimientos.
DIRS_RESERVADOS = {"_SISTEMA"}
ARCHIVOS_RESERVADOS = {"busqueda.db", "busqueda.db-wal", "busqueda.db-shm", "INGESTA_RUTEO.csv", "TANDAS.csv",
                       "PUBLICADOS.csv", "cartera.json", "aliases.json", "aliases_cartera.json",
                       "carpetas_propias.json", "dashboard_log.json", "CARTERA_APLICADO.csv",
                       "PROPIEDAD_CARPETAS.csv", "hosts_permitidos.txt", "ingesta.lock", ".tanda.lock"}
RE_RESERVADO = re.compile(r"^(?:REORG_[\w.-]+\.csv|\.~[0-9a-f]+\.incompleto)$")  # propuestas y copias a medias entre unidades


def es_reservado(p: Path) -> bool:
    """True si el archivo o la carpeta es de la zona interna: no se recorre ni se mueve en las tandas."""
    if p.name in ARCHIVOS_RESERVADOS or RE_RESERVADO.match(p.name):
        return True
    return p.name in DIRS_RESERVADOS or any(padre.name in DIRS_RESERVADOS for padre in p.parents)


FORMATOS_CON_TEXTO = {".pdf", ".txt", ".md", ".docx", ".doc", ".csv", ".log"}
FORMATOS_SIN_TEXTO = {
    ".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp",
    ".ogg", ".mp3", ".mp4", ".wav",
    ".rar", ".zip", ".7z",
    ".xlsx", ".xls", ".pptx",
    ".lnk", ".prn",
}

TEXTO_LARGO = {".md", ".txt", ".docx", ".doc"}
NO_CLASIFICADOS = {"sin_texto", "sin_clasificar", "formato_desconocido"}
MOTIVOS = ("cartera", "doctrina", "expediente", "expediente_nuevo", "alias", "jurisprudencia",
           "match_carpeta", "escrito_propio", "rama", "sin_clasificar", "sin_texto", "formato_desconocido")


class Destino(NamedTuple):
    carpeta: Path
    motivo: str
    detalle: str = ""
    confianza: str = "media"


# ── Aliases ──────────────────────────────────────────────────────────

def _mtime(p: Path) -> float:
    return p.stat().st_mtime if p.exists() else 0.0


_alias_cache: tuple[tuple[float, float], list[tuple[re.Pattern, str]]] = ((0.0, 0.0), [])
_alias_por_archivo: dict[Path, list[tuple[re.Pattern, str]]] = {}


def _leer_aliases(archivo: Path) -> list[tuple[re.Pattern, str]]:
    """Un archivo ilegible conserva su ultima version buena y no arrastra al otro."""
    if not archivo.exists():
        return []
    compilados = []
    try:
        for a in json.loads(archivo.read_text(encoding="utf-8-sig")).get("aliases", []):
            try:
                compilados.append((re.compile(a["pattern"], re.IGNORECASE), a["caso"]))
            except re.error:
                print(f"[alias invalido, ignorado] {a['pattern']}", file=sys.stderr)
    except (json.JSONDecodeError, OSError, KeyError, TypeError, AttributeError) as exc:
        print(f"[aliases ilegibles, se usa la version anterior] {archivo.name}: {exc}", file=sys.stderr)
        return _alias_por_archivo.get(archivo, [])
    _alias_por_archivo[archivo] = compilados
    return compilados


def cargar_aliases() -> list[tuple[re.Pattern, str]]:
    """Primero los manuales (aliases.json), despues los generados desde la cartera."""
    global _alias_cache
    firma = (_mtime(ALIAS_FILE), _mtime(ALIAS_CARTERA))
    if firma != _alias_cache[0]:
        _alias_cache = (firma, _leer_aliases(ALIAS_FILE) + _leer_aliases(ALIAS_CARTERA))
    return _alias_cache[1]


def _caso_valido(caso: object) -> bool:
    if not isinstance(caso, str) or not caso:
        return False
    d, raiz = (CASOS / caso).resolve(), CASOS.resolve()
    return d != raiz and d.is_relative_to(raiz) and d.is_dir()


def validar_alias(pattern: str, caso: str) -> str | None:
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"regex invalido: {exc}"
    if rx.search(""):
        return "el patron coincide con cualquier nombre"
    if not caso or not (CASOS / caso).is_dir():
        return f"no existe la carpeta {caso!r}"
    if not _caso_valido(caso):
        return f"la carpeta {caso!r} queda fuera de {CASOS.name}"
    return None


def _match_alias(nombre: str) -> str | None:
    nombre = _sin_tildes(nombre)
    for patron, caso in cargar_aliases():
        if patron.search(nombre) and _caso_valido(caso):
            return caso
    return None


# ── Match contra carpetas existentes ─────────────────────────────────

_PALABRAS_GENERICAS = {
    "banco", "estado", "nacion", "poder", "judicial", "provincia",
    "misiones", "posadas", "municipalidad", "ministerio", "defensa",
    "salud", "trabajo", "seguridad", "social", "tribunal", "juzgado",
    "primera", "instancia", "civil", "comercial", "laboral", "penal",
    "contencioso", "administrativo", "nacional", "federal", "anses",
    "establecimiento", "servicios", "argentina", "argentino", "fiduciarios",
    "demanda", "apelacion", "recurso", "consumo", "desalojo", "debate",
    "imprimir", "ahorro", "instituto", "norma", "expte", "amparo",
    "ejecucion", "sucesion", "divorcio", "despido", "danos", "perjuicios",
    "cobro", "pesos", "sumas", "dinero", "accion", "medida", "cautelar",
    "sociedad", "anonima", "limitada", "responsabilidad", "empresa", "compania",
    "seguros", "asociacion", "cooperativa", "consumidores", "usuarios", "colegio",
    "publico", "abogados", "medios", "buenos", "aires", "direccion", "general",
    "secretaria", "empleo", "internacional",
}
_PALABRAS_GENERICAS |= set(APELLIDOS_PROPIOS)  # el titular firma todo: no identifica causa


def asegurar_estructura() -> None:
    for d in (INBOX, CASOS, SISTEMA):
        d.mkdir(parents=True, exist_ok=True)


def _sin_tildes(t: str) -> str:
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().lower()


VENTANA = 12


def _utiles(partes: list[str]) -> list[str]:
    return [p for p in partes if len(p) >= 5 and not p.isdigit() and p not in _PALABRAS_GENERICAS]


def _match_carpetas_existentes(texto: str) -> str | None:
    """Busca el apellido del actor a <= VENTANA palabras de una palabra del demandado,
    como en una caratula. Las carpetas son AAAA-actor(3 palabras)-demandado.
    Ante empate no decide."""
    if not texto or len(texto) < 50:
        return None
    pos: dict[str, list[int]] = {}
    for i, w in enumerate(re.findall(r"[a-z0-9]+", _sin_tildes(texto))):
        pos.setdefault(w, []).append(i)
    candidatos: list[tuple[int, str]] = []
    for d in CASOS.iterdir():
        if not d.is_dir() or d.name.startswith("sinanio-") or "-expte-" in d.name:
            continue
        partes = d.name.split("-")[1:]
        actor, dem = _utiles(partes[:3]), _utiles(partes[3:])
        if not actor or not dem or actor[0] not in pos:
            continue
        cerca = {t for t in dem
                 if any(abs(i - j) <= VENTANA for i in pos[actor[0]] for j in pos.get(t, ()))}
        if cerca:
            candidatos.append((len(actor[0]) + sum(len(t) for t in cerca), d.name))
    if not candidatos:
        return None
    candidatos.sort(reverse=True)
    if len(candidatos) > 1 and candidatos[0][0] == candidatos[1][0]:
        return None
    return candidatos[0][1]


_exptes_cache: dict[str, str] | None = None


def _mapa_exptes() -> dict[str, str]:
    """expte -> carpeta, aprendido de los ruteos previos. Solo exptes que fueron a una unica carpeta."""
    global _exptes_cache
    if _exptes_cache is None:
        m: dict[str, Counter] = {}
        aprendido: list[tuple[str, str, str]] = []  # (expte, carpeta, donde quedo el archivo), en orden
        for nombre in ("FASE2_RUTEO.csv", "FASE4_RUTEO.csv", RUTEO_CSV.name):
            ruta = BASE / "_SISTEMA" / nombre
            if not ruta.exists():
                continue
            with ruta.open(encoding="utf-8", newline="") as fh:
                for r in csv.DictReader(fh):
                    if r.get("motivo") == "deshacer":  # el movimiento que se deshizo ya no ensena nada
                        for i in range(len(aprendido) - 1, -1, -1):
                            if aprendido[i][2] == r.get("origen"):
                                del aprendido[i]
                                break
                        continue
                    expte = r.get("expediente") or (
                        r.get("detalle") if r.get("motivo") in ("expediente", "expediente_nuevo") else None)
                    partes = PureWindowsPath(r.get("destino", "")).parts
                    if expte and "01_Expedientes" in partes[:-1]:
                        caso = partes[partes.index("01_Expedientes") + 1]
                        aprendido.append((expte, RENOMBRES.get(caso, caso), r.get("destino", "")))
        for expte, caso, _ in aprendido:
            m.setdefault(expte, Counter())[caso] += 1
        _exptes_cache = {e: next(iter(c)) for e, c in m.items()
                         if len(c) == 1 and (CASOS / next(iter(c))).is_dir()}
    return _exptes_cache


def _misma_caratula(caratula: str, carpeta: str) -> bool:
    sufijo = carpeta.split("-", 1)[1] if "-" in carpeta else carpeta
    if caratula.startswith(sufijo) or sufijo.startswith(caratula):
        return True
    a, b = caratula.split("-"), sufijo.split("-")
    propios = set(_utiles(b[3:])) or set(_utiles(b[1:]))
    return a[0] == b[0] and len(a[0]) >= 4 and bool(propios & set(_utiles(a[1:])))


def carpeta_de_caso(expte: str, caratula: str | None) -> tuple[Path, bool, bool]:
    """(carpeta, existe, vinculada). `vinculada` solo cuando el vinculo viene del historial de ruteos: una
    carpeta que coincide por nombre puede ser de otra causa con el mismo numero."""
    caso = _mapa_exptes().get(expte)
    if caso:
        return CASOS / caso, True, True
    num, anio = expte.split("/")
    for d in sorted(CASOS.iterdir()):
        if not d.is_dir():
            continue
        if f"expte-{num}-{anio}" in d.name:
            return d, True, False
        if caratula and d.name.startswith(anio) and _misma_caratula(caratula, d.name):
            return d, True, False
    return CASOS / f"{anio}-{caratula or f'expte-{num}-{anio}'}", False, False


# ── Cartera propia ───────────────────────────────────────────────────

RE_ID = re.compile(r"(?<![a-z])id[-_]?(\d{5,8})(?!\d)", re.I)
RE_NUM_FECHA = re.compile(r"(?<!\d)\d{1,6}[-_]\d{4}-\d{2}-\d{2}(?!\d)")
RE_NUM_ANIO = re.compile(r"(?<!\d)(\d{1,7})[-_/ ]?((?:19|20)\d{2})(?!\d)")
RE_BIS = re.compile(r"bis[-_ ]?(\d+)(?:\s*[-_/]\s*((?:19|20)?\d{2})(?!\d))?", re.I)
RE_BIS_SIN_NUMERO = re.compile(r"(?<![a-z])bis(?![a-z\d]|[-_ ]\d)", re.I)
# dd-mm-aaaa y CUIT: sin quitarlos, "escrito 15-03-2024" se lee como expediente 3/2024
RE_FECHA_DMA = re.compile(r"(?<!\d)\d{1,2}[-_/.]\d{1,2}[-_/.](?:19|20)\d{2}(?!\d)")
RE_CUIT = re.compile(r"(?<!\d)\d{2}-?\d{8}-?\d(?!\d)")


_SIGLAS_SIN_VALOR = {"otro", "otra", "pshm"}


def _tokens_de_parte(texto: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", _sin_tildes(texto))
            if len(w) >= 4 and w not in _PALABRAS_GENERICAS and w not in _SIGLAS_SIN_VALOR]


def _partes_de(caratula: str) -> tuple[list[str], list[str]]:
    m = re.match(r"^(.+?)\s+c\s*/\s*(.+?)(?:\s+s\s*/.*)?$", caratula, re.I)
    if m:
        return _tokens_de_parte(m[1])[:3], _tokens_de_parte(m[2])[:3]
    return _tokens_de_parte(re.split(r"\s+s\s*/", caratula, flags=re.I)[0])[:3], []


class IndiceCartera:
    def __init__(self, causas):
        self.por_id: dict[str, str] = {}
        self.por_clave: dict[str, str] = {}
        self.por_bis: dict[str, str] = {}  # clave completa del incidente ("N/AAAA bisB/AA"), con su anio
        self.vinculadas: set[str] = set()  # carpetas confirmadas en la cartera (c.carpeta)
        self.previstas: set[str] = set()
        self.partes: list[tuple[str, list[str], list[str]]] = []
        for c in causas:
            carpeta = c.carpeta or c.carpeta_prevista
            if not carpeta:
                continue
            if c.carpeta:
                self.vinculadas.add(c.carpeta)
            else:
                self.previstas.add(carpeta)
            if c.id_expediente:
                self.por_id[c.id_expediente] = carpeta
            if c.expte.bis is None:
                self.por_clave[c.expte.clave] = carpeta
            else:  # dos incidentes bis1 del mismo principal de anios distintos son causas distintas
                self.por_bis[c.expte.clave_completa] = carpeta
            if c.caratula:
                actor, dem = _partes_de(c.caratula)
                if actor:
                    self.partes.append((carpeta.split("/")[0], actor, dem))

    def por_partes(self, nombre: str) -> str | None:
        """Carpeta si el nombre del archivo nombra a las partes de una unica causa."""
        palabras = set(re.findall(r"[a-z]+", _sin_tildes(nombre)))
        hits = set()
        for carpeta, actor, dem in self.partes:
            if dem:
                ok = actor[0] in palabras and any(t in palabras for t in dem)
            else:
                ok = len(actor) >= 2 and actor[0] in palabras and actor[1] in palabras
            if ok:
                hits.add(carpeta)
        return next(iter(hits)) if len(hits) == 1 else None

    def por_numero(self, numero: int, anio: int, bis: int | None = None, bis_anio: int | None = None) -> str | None:
        clave = f"{numero}/{anio}"
        if bis is not None:
            if bis_anio is not None and f"{clave} bis{bis}/{bis_anio % 100:02d}" in self.por_bis:
                return self.por_bis[f"{clave} bis{bis}/{bis_anio % 100:02d}"]
            # Sin el anio del incidente (o con uno que no coincide) solo se rutea si hay exactamente uno con ese
            # bis: con dos o mas la causa es ambigua. Sin ningun incidente con ese bis sigue el principal
            # (recibos y codigos de banco que traen "BIS" en el nombre).
            candidatos = {v for k, v in self.por_bis.items() if k.startswith(f"{clave} bis{bis}/")}
            if bis_anio is None and len(candidatos) == 1:
                return next(iter(candidatos))
            if candidatos:
                return None
        return self.por_clave.get(clave)


_indice_cache: tuple[float, IndiceCartera] = (-1.0, IndiceCartera([]))


def _indice() -> IndiceCartera:
    global _indice_cache
    m = _mtime(CARTERA_JSON)
    if m != _indice_cache[0]:
        try:
            indice = IndiceCartera(cargar_cartera(CARTERA_JSON))
        except (ValueError, OSError, KeyError, TypeError, AttributeError) as exc:  # ValueError: JSON roto o no UTF-8
            print(f"[cartera ilegible, se ignora] {CARTERA_JSON.name}: {exc}", file=sys.stderr)
            indice = IndiceCartera([])
        _indice_cache = (m, indice)
    return _indice_cache[1]


def _caso_cartera(nombre: str, texto: str | None) -> tuple[str, str, bool] | None:
    """(carpeta, via, senal_fuerte). Por id de SIGED, numero o nombre de las partes en el nombre; por numero en el
    texto si se pasa. `senal_fuerte` distingue el vinculo confirmado (id de SIGED, carpeta vinculada en la cartera o
    carpeta que se crearia al mover) de la mera coincidencia de nombre con una carpeta que existe sin vinculo."""
    idx = _indice()

    def existe(carpeta: str | None) -> str | None:
        return carpeta if carpeta and ((CASOS / carpeta).is_dir() or carpeta in idx.previstas) else None

    m = RE_ID.search(nombre)
    if m and existe(idx.por_id.get(m.group(1))):
        return idx.por_id[m.group(1)], "id", True
    sin_fecha = RE_CUIT.sub(" ", RE_FECHA_DMA.sub(" ", RE_NUM_FECHA.sub(" ", nombre)))
    bis = RE_BIS.search(sin_fecha)
    for mm in RE_NUM_ANIO.finditer(sin_fecha):
        carpeta = existe(idx.por_numero(int(mm.group(1)), int(mm.group(2)), int(bis.group(1)) if bis else None,
                                        int(bis.group(2)) if bis and bis.group(2) else None))
        if carpeta:
            # "bis" sin numero: puede ser un incidente, no la causa principal
            if not bis and RE_BIS_SIN_NUMERO.search(sin_fecha):
                return carpeta, "numero_nombre_bis", False
            # la carpeta existe pero la causa no la tiene vinculada: coincide el nombre, no la causa
            return carpeta, "numero_nombre", carpeta in idx.vinculadas or not (CASOS / carpeta).is_dir()
    carpeta = existe(idx.por_partes(nombre))
    if carpeta:
        return carpeta, "partes", False
    if texto:
        e = buscar_expte(texto)
        if e:
            n, a = e.split("/")
            carpeta = existe(idx.por_numero(int(n), int(a)))
            if carpeta:
                return carpeta, "numero_texto", False
    return None


# ── Clasificacion ────────────────────────────────────────────────────

EDITABLES = {".docx", ".doc", ".odt", ".rtf", ".md", ".txt"}
RE_EXPORTACION_SIGED = re.compile(r"__user|(?<![a-z])id[-_]?\d{5}|^expediente[-_ ]\d|limpio", re.I)


def _borrador(nombre: str, ext: str, via: str) -> bool:
    """Trabajo propio sin matricula: SIGED baja PDF; los editables son propios salvo las exportaciones del expediente.
    El texto no sirve para decidir: las providencias de SIGED tambien nombran al letrado."""
    return via != "id" and ext in EDITABLES and not RE_EXPORTACION_SIGED.search(nombre)


def _por_rama(p: Path, ext: str, texto: str) -> Destino:
    largo = len(texto.strip())
    if not ((ext == ".pdf" and largo > 120) or (ext in TEXTO_LARGO and largo > 400)):
        return Destino(INBOX / "sin_clasificar", "sin_clasificar", "", "baja")
    carpeta, rama = clasificar(texto)
    es_fallo = rama.startswith("fallo_sin_tribunal")
    if not (es_fallo or rama.startswith("doctrina_")):
        if BASE / "02_Jurisprudencia" in carpeta.parents:
            carpeta = BASE / "02_Jurisprudencia" / Path(*tema_de(texto, p.name))
        return Destino(carpeta, "rama", rama)
    d = clasificar_doctrina(texto, p.name, paginas_pdf(p) if ext == ".pdf" else 0)
    if d is None or (es_fallo and d.confianza == "baja"):
        if es_fallo:
            return Destino(BASE / "02_Jurisprudencia" / Path(*tema_de(texto, p.name)), "rama", rama)
        return Destino(INBOX / "sin_clasificar", "sin_clasificar", "no es doctrina", "baja")
    confianza = "media" if es_fallo and d.confianza == "alta" else d.confianza
    return Destino(ruta_doctrina(d, BASE), "doctrina", f"{d.tipo} | {d.motivo}", confianza)


def destino_de(p: Path, texto: tuple[str | None, str | None] | None = None) -> Destino:
    """`texto`: (texto, error) ya extraido, para no leer el archivo dos veces cuando el llamador tambien lo necesita."""
    ext = p.suffix.lower()
    nombre = p.stem

    if ext not in FORMATOS_CON_TEXTO and ext not in FORMATOS_SIN_TEXTO:
        return Destino(INBOX / "formato_desconocido", "formato_desconocido", ext, "baja")

    if texto is None:
        texto, err = (None, None) if ext in FORMATOS_SIN_TEXTO else extraer_universal(p)
    else:
        texto, err = texto
    if err in ("doc_lectura_binaria", "ocr_ilegible"):
        texto = None  # basura binaria + metadatos: no sirve para decidir por contenido
    hay_texto = bool(texto and len(texto.strip()) >= 30)
    firmado = hay_texto and bool(MATRICULA_RE.search(texto))
    propio = firmado or (hay_texto and bool(OWNER_RE.search(texto)))
    es_juris = hay_texto and not propio and bool(JURIS_RE.search(texto[:4000]))

    hit = _caso_cartera(nombre, texto if hay_texto and not es_juris else None)
    if hit:
        carpeta, via, fuerte = hit
        sub_c = "00_escritos_propios" if firmado or _borrador(nombre, ext, via) else "01_siged"
        return Destino(CASOS / carpeta / sub_c, "cartera", via, "alta" if fuerte else "media")

    if not hay_texto:
        caso = _match_alias(nombre)
        if caso:
            return Destino(CASOS / caso / "00_escritos_propios", "alias", caso)
        sub = "sin_texto" if ext in FORMATOS_SIN_TEXTO else "_sin_texto"
        return Destino(INBOX / sub, "sin_texto", err or "", "baja")

    expte = buscar_expte(texto)
    caratula = buscar_caratula(texto)
    sub = "00_escritos_propios" if propio else "01_siged"
    sub_e = "00_escritos_propios" if firmado else "01_siged"

    if expte and (propio or caratula):
        carpeta, existe, vinculada = carpeta_de_caso(expte, caratula)
        if existe:  # la carpeta que solo coincide por nombre puede ser de otra causa con el mismo numero: a revisar
            return Destino(carpeta / sub_e, "expediente", expte, "alta" if vinculada else "media")
        # Un numero que no es de la cartera no abre carpeta: casi siempre es una causa citada o ajena.

    caso = _match_alias(nombre)
    if caso:
        return Destino(CASOS / caso / sub, "alias", caso)

    if es_juris:
        carpeta, rama = clasificar(texto)
        if rama.startswith("legislacion"):
            return Destino(carpeta, "rama", rama)
        if not rama.startswith(("fallo_sin_tribunal", "doctrina_")):
            return Destino(BASE / "02_Jurisprudencia" / Path(*tema_de(texto, p.name)), "jurisprudencia", rama,
                           "alta" if FALLO.search(texto[:4000]) else "media")
        return _por_rama(p, ext, texto)  # menciona "doctrina"/"jurisprudencia" pero sin tribunal identificado

    caso = _match_carpetas_existentes(texto)
    if caso:
        return Destino(CASOS / caso / sub, "match_carpeta", caso)

    if propio:
        return Destino(BASE / "05_Modelos_y_Escritos", "escrito_propio")

    return _por_rama(p, ext, texto)


def destino_ajeno(p: Path, carpeta: str, texto: tuple[str | None, str | None] | None = None) -> Destino:
    """Archivo de una carpeta de causa que no es del Dr.: los fallos y la doctrina van a su tema; el resto,
    a 06_Expedientes_ajenos conservando el nombre de la carpeta. `texto`: como en destino_de."""
    if texto is None:
        texto, err = extraer_universal(p)
    else:
        texto, err = texto
    texto = texto if err not in ("doc_lectura_binaria", "ocr_ilegible") else None
    if texto and len(texto.strip()) > 200:
        destino, rama = clasificar(texto)
        if rama.startswith("legislacion"):
            return Destino(destino, "rama", rama)
        if not rama.startswith("doctrina_") and (FALLO.search(texto[:4000]) or not rama.startswith("fallo_sin")):
            return Destino(BASE / "02_Jurisprudencia" / Path(*tema_de(texto, p.name)), "jurisprudencia", rama,
                           "alta" if FALLO.search(texto[:4000]) else "media")
        d = clasificar_doctrina(texto, p.name, paginas_pdf(p) if p.suffix.lower() == ".pdf" else 0)
        if d:
            return Destino(ruta_doctrina(d, BASE), "doctrina", f"{d.tipo} | {d.motivo}", d.confianza)
    return Destino(BASE / "06_Expedientes_ajenos" / carpeta, "ajena", carpeta)


def ignorado(p: Path, origen: Path) -> bool:
    try:
        return bool(set(p.relative_to(origen).parts[:-1]) & IGNORAR)
    except ValueError:
        return False


RE_REPO_DESCARGADO = re.compile(r"-(main|master)$")
MARCAS_DE_PROYECTO = {"package.json", ".git", "node_modules", "pyproject.toml", ".venv", "venv", "setup.py"}


def archivos_pendientes(origen: Path) -> list[Path]:
    """Recorre origen sin entrar en las carpetas ignoradas ni en proyectos de codigo. La zona interna del sistema
    (_SISTEMA, indices, propuestas) nunca es candidata."""
    out = []
    for raiz, dirs, archivos in os.walk(origen):
        if raiz != str(origen) and (MARCAS_DE_PROYECTO & (set(dirs) | set(archivos))
                                    or RE_REPO_DESCARGADO.search(Path(raiz).name) or "SKILL.md" in archivos):
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in IGNORAR and d not in MARCAS_DE_PROYECTO and d not in DIRS_RESERVADOS]
        out += [Path(raiz) / a for a in archivos if not es_reservado(Path(raiz) / a)]
    return out


COLUMNAS_RUTEO = ["fecha", "origen", "destino", "motivo", "detalle", "tamano", "mtime_ns"]


def _leer_csv(ruta: Path) -> tuple[list[str], list[dict]]:
    """(cabecera, filas) de un CSV propio, tal cual o re-guardado por Excel (BOM, punto y coma, cp1252)."""
    datos = ruta.read_bytes()
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            texto = datos.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # cp1252 no define unos pocos bytes (0x81, 0x8d, 0x8f, 0x90, 0x9d)
        texto = datos.decode("latin-1")
    lineas = texto.splitlines()
    if not lineas:
        return [], []
    try:
        dialecto = csv.Sniffer().sniff(lineas[0], delimiters=",;\t")
    except csv.Error:
        dialecto = csv.excel
    lector = csv.DictReader(lineas, dialect=dialecto)
    filas = list(lector)
    return list(lector.fieldnames or []), filas


def _columnas_al_dia() -> None:
    """Un registro anterior (sin tamano ni mtime_ns) o re-guardado por Excel se reescribe una vez en el formato propio.
    Antes se respalda, y si no se lo puede leer completo no se toca: mejor no anotar que perder el historial."""
    with RUTEO_CSV.open("rb") as f:
        primera = f.readline()
    if primera.rstrip(b"\r\n") == ",".join(COLUMNAS_RUTEO).encode():
        return
    cabecera, filas = _leer_csv(RUTEO_CSV)
    if not {"origen", "destino"} <= set(cabecera):
        raise OSError(f"no se reconocen las columnas de {RUTEO_CSV.name} (se edito a mano?): "
                      "no se anota para no perder el historial")
    lineas = sum(1 for linea in RUTEO_CSV.read_bytes().splitlines()[1:] if linea.strip())
    if len(filas) < lineas or any(not (f.get("origen") or "").strip() for f in filas):
        raise OSError(f"{RUTEO_CSV.name} no se pudo leer completo: no se anota para no perder el historial")
    respaldar(RUTEO_CSV)
    tmp = RUTEO_CSV.with_name(RUTEO_CSV.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS_RUTEO, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    tmp.replace(RUTEO_CSV)


def _anotar(origen: Path, destino: Path, motivo: str, detalle: str = "") -> None:
    """Anota un movimiento ya hecho, con tamano y fecha del archivo para que deshacer reconozca que es el mismo."""
    RUTEO_CSV.parent.mkdir(parents=True, exist_ok=True)
    nuevo = not RUTEO_CSV.exists()
    if not nuevo:
        _columnas_al_dia()
    st = Path(destino).stat()
    with RUTEO_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(COLUMNAS_RUTEO)
        w.writerow([datetime.now().isoformat(timespec="seconds"), origen, destino, motivo, detalle,
                    st.st_size, st.st_mtime_ns])


def _renombrar_sin_pisar(src: Path, dst: Path) -> None:
    """Renombra sin reemplazar NUNCA un destino que exista y sin ventana de carrera: FileExistsError si ya esta.

    - Windows: os.rename ya es sin reemplazo (MoveFileEx sin REPLACE_EXISTING).
    - Linux: renameat2(RENAME_NOREPLACE) si el kernel lo da; si no, link + unlink (atomico, falla si existe).
      Nunca un rename a pelo: en POSIX reemplazaria un destino que aparezca entre el chequeo y el rename."""
    if os.name == "nt":
        os.rename(src, dst)
        return
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        res = libc.renameat2(ctypes.c_int(-100), os.fsencode(src), ctypes.c_int(-100),
                             os.fsencode(dst), ctypes.c_uint(1))  # AT_FDCWD, RENAME_NOREPLACE
        err = ctypes.get_errno()
    except (AttributeError, OSError):  # sin libc o sin renameat2 (glibc viejo, macOS)
        res, err = -1, errno.ENOSYS
    if res == 0:
        return
    if err not in (errno.ENOSYS, errno.EINVAL, errno.ENOTSUP, errno.EOPNOTSUPP):
        raise OSError(err, os.strerror(err), str(src), None, str(dst))
    os.link(src, dst)  # FileExistsError si dst aparecio entremedio: eso se quiere
    os.unlink(src)


def _copiar_sin_pisar(src: Path, dst: Path) -> None:
    """Copia creando el destino en modo exclusivo: FileExistsError, sin truncar nada, si ya existe."""
    with open(src, "rb") as entrada, open(dst, "xb") as salida:
        shutil.copyfileobj(entrada, salida, 1 << 20)
    shutil.copystat(src, dst)


def _renombrar(src: Path, dst: Path) -> None:
    """Mueve sin copiar ni pisar: FileExistsError si el destino existe (el llamador prueba otro nombre) y un aviso
    claro si el archivo esta abierto en otro programa. Entre unidades copia, verifica y recien despues borra."""
    try:
        _renombrar_sin_pisar(src, dst)
    except FileExistsError as exc:
        raise FileExistsError(f"el destino ya existe: {dst}") from exc
    except PermissionError as exc:
        raise OSError(f"abierto en otro programa o sin permiso: {src}") from exc
    except OSError as exc:
        if getattr(exc, "winerror", None) != 17 and exc.errno != errno.EXDEV:
            raise
        _mover_entre_unidades(src, dst)


def _sha256_archivo(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        for bloque in iter(lambda: f.read(1 << 20), b""):
            h.update(bloque)
    return h.hexdigest()


def _existe(ruta: Path) -> bool | None:
    """True o False si se sabe; None si la unidad no responde (un pendrive desconectado no es "no existe")."""
    try:
        os.stat(ruta)
        return True
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def _mover_entre_unidades(src: Path, dst: Path) -> None:
    """os.rename no cruza de unidad (un pendrive, otro disco). Copia a un temporal `.~<azar>.incompleto` (la busqueda
    no lo indexa), verifica por sha256, lo renombra al nombre final sin pisar, comprueba que el origen no cambio y
    recien entonces lo borra. Si algo falla, la copia se descarta solo si se sabe que el origen sigue: nunca quedan
    cero ejemplares, y lo que no se pueda limpiar queda nombrado en el error."""
    if _existe(dst) is not False:
        raise FileExistsError(f"el destino ya existe: {dst}")
    antes = os.stat(src)
    tmp = dst.with_name(f".~{secrets.token_hex(6)}.incompleto")
    copia: Path | None = None
    verificada = renombrando = borrando = solo_lectura = False
    try:
        with open(tmp, "xb") as salida:
            copia = tmp
            h = hashlib.sha256()
            with open(src, "rb") as entrada:
                for bloque in iter(lambda: entrada.read(1 << 20), b""):
                    h.update(bloque)
                    salida.write(bloque)
            salida.flush()
            os.fsync(salida.fileno())  # en un pendrive que se desconecta, la copia tiene que estar en disco antes de borrar el origen
        try:
            os.utime(tmp, ns=(antes.st_atime_ns, antes.st_mtime_ns))
        except OSError:
            pass  # FAT/exFAT o red pueden rechazar las fechas; el contenido se verifica igual
        if _sha256_archivo(tmp) != h.hexdigest():
            raise OSError(f"la copia en {tmp} no coincide con el origen")
        renombrando = True
        _renombrar_sin_pisar(tmp, dst)  # misma carpeta: no cruza de unidad y falla si el destino aparecio entretanto
        copia, verificada = dst, True
        solo_lectura = not os.access(src, os.W_OK)
        if solo_lectura:  # Windows no borra un archivo de solo lectura; os.rename en la misma unidad si lo mueve
            os.chmod(src, stat.S_IWRITE)
        despues = os.stat(src)  # justo antes de borrar: un guardado durante la copia se detecta aca
        if (despues.st_size, despues.st_mtime_ns) != (antes.st_size, antes.st_mtime_ns):
            raise OSError(f"el origen cambio durante la copia: {src}")
        borrando = True
        src.unlink()
    except BaseException as exc:
        if renombrando and copia == tmp and not os.path.lexists(tmp) and os.path.lexists(dst):
            copia, verificada = dst, True  # el rename se hizo aunque la excepcion llego despues
        origen = _existe(src)
        if origen is None:
            exc.add_note(f"no se pudo comprobar si el origen {src} sigue; " + (
                f"queda la copia {copia}" if copia else "no se copio nada"))
            raise
        if origen:
            if solo_lectura:
                try:
                    os.chmod(src, stat.S_IREAD)
                except OSError as exc3:
                    exc.add_note(f"{src} quedo sin el atributo de solo lectura: {exc3}")
            if copia is not None:
                try:
                    copia.unlink(missing_ok=True)
                except OSError as exc2:
                    exc.add_note(f"no se pudo descartar la copia {copia}: {exc2}")
                    raise
            if isinstance(exc, FileExistsError):
                raise FileExistsError(f"el destino ya existe: {dst}") from exc
            if borrando and isinstance(exc, PermissionError):
                raise OSError(f"abierto en otro programa o sin permiso: {src}") from exc
            raise
        if not verificada:
            aviso = f"el origen desaparecio durante la copia; " + (
                f"queda una copia sin verificar en {copia}" if copia else "no se copio nada")
            if isinstance(exc, Exception):
                raise OSError(aviso) from exc
            exc.add_note(aviso)
            raise
        if not isinstance(exc, Exception):
            exc.add_note(f"el traslado se completo: el archivo esta en {dst}")
            raise
        # el origen ya no esta y la copia verificada tiene el nombre final: el traslado se completo
    if solo_lectura:
        try:
            os.chmod(dst, stat.S_IREAD)
        except OSError:
            pass  # el contenido ya esta a salvo; solo se pierde el atributo


def mover(src: Path, carpeta: Path, motivo: str, detalle: str = "", nombre: str | None = None) -> Path:
    """nombre: nombre nuevo del archivo (sin cambiar la extension); por defecto conserva el original."""
    if src.parent.resolve() == carpeta.resolve() and not nombre:
        return src
    if not carpeta.resolve().is_relative_to(BASE.resolve()):
        raise OSError(f"destino fuera de la base: {carpeta}")
    if es_reservado(src) or es_reservado(carpeta):
        raise OSError(f"la zona del sistema no entra en las tandas: {src if es_reservado(src) else carpeta}")
    carpeta.mkdir(parents=True, exist_ok=True)
    pedido = carpeta / ((Path(nombre).stem + src.suffix) if nombre else src.name)
    dst = destino_unico(pedido)
    for ruta in (src, dst):
        try:
            str(ruta).encode("utf-8")
        except UnicodeEncodeError as exc:
            raise OSError(f"el nombre tiene caracteres que el registro no puede guardar: {ruta!r}") from exc
    for _ in range(4):  # si el nombre aparece entremedio se prueba otro (nota__1.txt): nunca se pisa
        try:
            _renombrar(src, dst)
            break
        except FileExistsError:
            dst = destino_unico(pedido)
    else:
        raise OSError(f"el destino ya existe y se sigue ocupando, no se pisa: {src}")
    try:
        _anotar(src, dst, motivo, detalle)
    except Exception as exc:
        try:
            _renombrar(dst, src)
        except OSError as exc2:
            raise OSError(f"no se pudo anotar el movimiento ({exc}) ni devolverlo al origen ({exc2}): "
                          f"el archivo quedo sin registrar en {dst}") from exc
        raise
    except BaseException as exc:
        exc.add_note(f"el archivo quedo sin registrar en {dst}")
        raise
    busqueda.registrar_movimiento(src, dst, base=BASE)
    global _exptes_cache
    _exptes_cache = None
    return dst


def causas_sin_carpeta(causas: list) -> dict:
    """{'faltantes': carpetas asignadas que no estan en disco, 'sin_nombre': causas sin carpeta ni prevista}."""
    faltantes = sorted(c.carpeta for c in causas if c.carpeta and not (CASOS / c.carpeta).is_dir())
    return {"faltantes": faltantes, "sin_nombre": sum(1 for c in causas if not c.carpeta and not c.carpeta_prevista)}


def _leer_propuestas(ruta: Path) -> list[dict]:
    """El CSV de la simulacion, tal cual o re-guardado por Excel (punto y coma, cp1252). Un CSV vacio da []; una
    cabecera que el Sniffer no reconoce se lee con coma, como la escriben esta aplicacion y la consola."""
    return _leer_csv(ruta)[1]


def leer_ruteo() -> list[dict]:
    if not RUTEO_CSV.exists():
        return []
    return _leer_csv(RUTEO_CSV)[1]


COLUMNAS_TANDAS = ["id", "fecha", "origen", "movidos", "primera_fila", "ultima_fila", "huella"]


def leer_tandas() -> list[dict]:
    """Las tandas validas del registro; una fila editada a mano con numeros rotos se ignora (queda en el archivo)."""
    if not TANDAS_CSV.exists():
        return []
    validas = []
    for t in _leer_csv(TANDAS_CSV)[1]:
        try:
            if 0 <= int(t["primera_fila"]) <= int(t["ultima_fila"]):
                validas.append(t)
        except (KeyError, TypeError, ValueError):
            continue
    return validas


def huella_filas(filas: list[dict], primera: int, ultima: int) -> str:
    """Identifica las filas de una tanda por su contenido: si el registro se edita (filas borradas o reordenadas en
    Excel), los indices guardados dejan de apuntar a esas filas y deshacer lo detecta."""
    sel = filas[primera:ultima + 1]
    if len(sel) != ultima - primera + 1:
        return ""
    texto = "\n".join(f"{f.get('fecha', '')}|{f.get('origen', '')}|{f.get('destino', '')}" for f in sel)
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


def _escribir_tandas(tandas: list[dict]) -> None:
    respaldar(TANDAS_CSV)
    tmp = TANDAS_CSV.with_name(TANDAS_CSV.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS_TANDAS, extrasaction="ignore")
        w.writeheader()
        w.writerows(tandas)
    tmp.replace(TANDAS_CSV)


def _registrar_tanda(origen: str, primera: int, ultima: int) -> str:
    """Una tanda = un rango de filas de INGESTA_RUTEO.csv (indices desde 0, sin la cabecera) y su huella."""
    tandas = leer_tandas()
    tid = f"t{len(tandas) + 1:04d}"
    _escribir_tandas([*tandas, {"id": tid, "fecha": datetime.now().isoformat(timespec="seconds"), "origen": origen,
                                "movidos": ultima - primera + 1, "primera_fila": primera, "ultima_fila": ultima,
                                "huella": huella_filas(leer_ruteo(), primera, ultima)}])
    return tid


def tandas_historicas(filas: list[dict], explicitas: list[dict], pausa: int = 120) -> list[dict]:
    """Tandas registradas mas las derivadas del historial viejo: filas seguidas con menos de `pausa` segundos."""
    cubiertas = {i for t in explicitas for i in range(int(t["primera_fila"]), int(t["ultima_fila"]) + 1)}
    out = [dict(t, derivada=False) for t in explicitas]
    actual: list[int] = []

    def cerrar():
        if actual:
            a, b = actual[0], actual[-1]
            out.append({"id": f"h{a}-{b}", "fecha": filas[a]["fecha"], "origen": "historial",
                        "movidos": b - a + 1, "primera_fila": a, "ultima_fila": b, "derivada": True})
            actual.clear()

    anterior = None
    for i, f in enumerate(filas):
        if i in cubiertas:
            cerrar()
            anterior = None
            continue
        try:
            t = datetime.fromisoformat(f["fecha"])
        except (ValueError, KeyError):
            t = anterior
        if actual and (anterior is None or t is None or (t - anterior).total_seconds() > pausa):
            cerrar()
        actual.append(i)
        anterior = t
    cerrar()
    return sorted(out, key=lambda t: int(t["primera_fila"]))


ESPERA_BLOQUEO = 20.0


class Ocupado(OSError):
    """Otra aplicacion o deshacer (de este u otro proceso) tiene el registro tomado."""


class TandaEnCurso(Ocupado):
    """Otra tanda (aplicar o deshacer, desde el dashboard o la consola) esta corriendo sobre la misma base."""


POSICION_BLOQUEO = 0  # el mismo byte que las versiones anteriores: un .exe viejo y uno nuevo se excluyen
POSICION_PID = 1  # fuera del byte trabado: en Windows el pid se puede leer mientras dura el bloqueo


def _trabar(f, trabar: bool) -> None:
    f.seek(POSICION_BLOQUEO)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if trabar else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, (fcntl.LOCK_EX | fcntl.LOCK_NB) if trabar else fcntl.LOCK_UN)


def _pid_del_bloqueo(ruta: Path) -> int | None:
    try:
        with ruta.open("rb") as f:
            f.seek(POSICION_PID)
            return int(f.read(20).decode("ascii", "ignore").strip()) or None
    except (OSError, ValueError):
        return None


@contextmanager
def bloqueo_tanda(espera: float | None = None):
    """Una sola tanda (aplicar o deshacer) por vez, en todos los procesos: dashboard, consola y cartera_cli
    comparten _SISTEMA/ingesta.lock. Mientras dura, el archivo lleva el pid de quien lo tiene (para avisar de quien
    es). Si hay otra, espera hasta `espera` segundos (ESPERA_BLOQUEO si no se da) y levanta TandaEnCurso. El sistema
    operativo libera el bloqueo si el proceso muere: un bloqueo huerfano no existe, nunca queda trabado."""
    ruta = RUTEO_CSV.with_name("ingesta.lock")
    ruta.parent.mkdir(parents=True, exist_ok=True)
    with ruta.open("a+b") as f:
        limite = time.monotonic() + (ESPERA_BLOQUEO if espera is None else espera)
        while True:
            try:
                _trabar(f, True)
                break
            except OSError:
                if time.monotonic() >= limite:
                    pid = _pid_del_bloqueo(ruta)
                    raise TandaEnCurso(f"hay otra tanda (aplicar o deshacer) en curso{f' (proceso {pid})' if pid else ''}"
                                       "; probar de nuevo en un momento") from None
                time.sleep(0.2)
        try:
            f.truncate(POSICION_PID)
            f.seek(POSICION_PID)
            f.write(str(os.getpid()).encode("ascii"))
            f.flush()
        except OSError:
            pass  # solo se pierde el aviso del pid
        try:
            yield
        finally:
            try:
                f.truncate(POSICION_PID)
                f.flush()
            except OSError:
                pass
            _trabar(f, False)


def _bloqueo(espera: float | None = None):
    return bloqueo_tanda(espera)


def _tanda_de_lo_escrito(origen: str, inicio: int) -> str | None:
    """Con el bloqueo tomado, las filas nuevas del registro son exactamente las de esta tanda."""
    fin = len(leer_ruteo())
    return _registrar_tanda(origen, inicio, fin - 1) if fin > inicio else None


def deshacer_filas(primera: int, ultima: int) -> dict:
    """Devuelve cada archivo del rango a su origen, del ultimo al primero. Nunca pisa ni borra."""
    for t in leer_tandas():
        if (t.get("primera_fila"), t.get("ultima_fila")) == (str(primera), str(ultima)) and t.get("huella"):
            if huella_filas(leer_ruteo(), primera, ultima) != t["huella"]:
                return {"restaurados": 0, "tanda": None, "despublicados": 0,
                        "avisos": [f"[el historial cambio desde la tanda {t['id']} (se edito {RUTEO_CSV.name}?): "
                                   "no se deshace nada para no mover archivos de otra tanda]"]}
            break
    return deshacer_indices(list(range(primera, ultima + 1)), f"filas {primera}-{ultima}")


def deshacer_indices(indices: list[int], etiqueta: str) -> dict:
    """Como deshacer_filas, pero con filas sueltas del registro (para revertir solo algunas de una tanda)."""
    try:
        with _bloqueo():
            return _deshacer_indices(indices, etiqueta)
    except Ocupado as exc:
        return {"restaurados": 0, "avisos": [str(exc)], "tanda": None, "despublicados": 0}


def _deshacer_indices(indices: list[int], etiqueta: str) -> dict:
    respaldar(RUTEO_CSV)
    filas = leer_ruteo()
    inicio, restaurados, avisos = len(filas), 0, []
    publicados, despublicados = _leer_publicados(), 0
    for f in (filas[i] for i in sorted(set(indices), reverse=True) if 0 <= i < len(filas)):
        if f.get("motivo") == "deshacer":
            continue
        dst, org = Path(f["destino"]), Path(f["origen"])
        if es_reservado(dst) or es_reservado(org):
            avisos.append(f"[archivo de la zona del sistema, no se toca] {dst}")
            continue
        if not dst.resolve().is_relative_to(BASE.resolve()):  # el origen puede estar afuera (Descargas); el destino no
            avisos.append(f"[destino fuera de {BASE}, no se toca] {dst}")
            continue
        if not dst.is_file():
            avisos.append(f"[ya no esta en el destino] {dst}")
            continue
        if org.exists():
            avisos.append(f"[el origen esta ocupado] {org}")
            continue
        st = dst.stat()
        if f.get("tamano"):  # las filas viejas no lo tienen: se deshacen como antes
            if (str(st.st_size), str(st.st_mtime_ns)) != (f["tamano"], f.get("mtime_ns")):
                avisos.append(f"[cambio desde que se movio, queda donde esta] {dst}")
                continue
        try:
            org.parent.mkdir(parents=True, exist_ok=True)
            _renombrar(dst, org)
        except FileExistsError:
            avisos.append(f"[el origen se ocupo entremedio] {org}")
            continue
        except OSError as exc:
            avisos.append(f"[ERROR] {dst}: {exc}")
            continue
        try:
            _anotar(dst, org, "deshacer", etiqueta)
        except Exception as exc:
            try:
                _renombrar(org, dst)
            except OSError as exc2:
                avisos.append(f"[ERROR] no se pudo anotar ({exc}) ni devolverlo ({exc2}): quedo sin registrar en {org}")
                continue
            try:  # la ida y vuelta por un pendrive FAT puede perder la fecha que deshacer usa para comprobar
                os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
            except OSError:
                pass
            avisos.append(f"[no se pudo anotar, queda donde estaba] {dst}: {exc}")
            continue
        except BaseException as exc:
            exc.add_note(f"el archivo quedo sin registrar en {org}")
            raise
        busqueda.registrar_movimiento(dst, org, base=BASE)
        restaurados += 1
        aviso = _despublicar(dst, publicados)
        despublicados += aviso == ""
        if aviso:
            avisos.append(aviso)
    global _exptes_cache
    _exptes_cache = None  # lo deshecho ya no ensena a que carpeta va un expediente
    tanda = _tanda_de_lo_escrito(f"deshacer:{etiqueta.removeprefix('filas ')}", inicio)
    if despublicados:
        _reindexar()
    return {"restaurados": restaurados, "avisos": avisos, "tanda": tanda, "despublicados": despublicados}


def reubicar_carpeta(vieja: Path, nueva: Path) -> int:
    """Una carpeta de causa renombrada (cartera_cli cruzar --aplicar): el registro sigue a los archivos a su ruta
    nueva para que deshacer las tandas viejas siga funcionando. Devuelve cuantas rutas cambio. Llamar con _bloqueo()."""
    if not RUTEO_CSV.exists():
        return 0
    _columnas_al_dia()
    filas = leer_ruteo()
    vieja_s, nueva_s, cambiadas = str(vieja), str(nueva), 0
    for f in filas:
        for campo in ("origen", "destino"):
            v = f.get(campo) or ""
            if v == vieja_s or v.startswith(vieja_s + os.sep):
                f[campo] = nueva_s + v[len(vieja_s):]
                cambiadas += 1
    if not cambiadas:
        return 0
    respaldar(RUTEO_CSV)
    tmp = RUTEO_CSV.with_name(RUTEO_CSV.name + ".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS_RUTEO, extrasaction="ignore")
        w.writeheader()
        w.writerows(filas)
    tmp.replace(RUTEO_CSV)
    tandas = leer_tandas()
    if any(t.get("huella") for t in tandas):  # las huellas se recalculan sobre las rutas nuevas
        for t in tandas:
            if t.get("huella"):
                t["huella"] = huella_filas(filas, int(t["primera_fila"]), int(t["ultima_fila"]))
        _escribir_tandas(tandas)
    return cambiadas


def _publicados_csv() -> Path:
    return RUTEO_CSV.with_name("PUBLICADOS.csv")


def _anotar_publicado(archivo: Path, md: Path) -> None:
    """Que .md del corpus nacio de que archivo, con su hash: deshacer lo quita solo si nadie lo toco."""
    ruta = _publicados_csv()
    nuevo = not ruta.exists()
    with ruta.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if nuevo:
            w.writerow(["archivo", "md", "hash"])
        w.writerow([archivo, md, hashlib.sha256(Path(md).read_bytes()).hexdigest()])


def _leer_publicados() -> dict[str, tuple[str, str]]:
    ruta = _publicados_csv()
    if not ruta.exists():
        return {}
    with ruta.open(encoding="utf-8", newline="") as f:
        return {r["archivo"]: (r["md"], r["hash"]) for r in csv.DictReader(f)}


def _despublicar(archivo: Path, publicados: dict) -> str | None:
    """Quita del corpus el .md publicado desde `archivo`. Devuelve un aviso si no corresponde quitarlo."""
    if str(archivo) not in publicados:
        return None
    md, h = publicados[str(archivo)]
    md = Path(md)
    if not md.is_file():
        return None
    if hashlib.sha256(md.read_bytes()).hexdigest() != h:
        return f"[el corpus tiene una version editada, queda] {md}"
    md.unlink()
    return ""


def _publicar(dst: Path, motivo: str, detalle: str) -> bool:
    try:
        ruta = kb_publicar.publicar(dst, motivo, detalle)
        if ruta is None and (motivo == "jurisprudencia" or detalle.startswith("legislacion_")):
            print(f"  [AVISO] no publicado (ya publicado / mismo nombre / sin id): {dst}")
        if ruta is not None:
            _anotar_publicado(dst, ruta)
        return ruta is not None
    except Exception as exc:  # noqa: BLE001
        print(f"  [AVISO publicacion] {dst}: {exc}")
        return False


def _reindexar() -> None:
    try:
        print(f"Indice del corpus: {kb_index.rebuild()}")
    except Exception as exc:  # noqa: BLE001
        print(f"  [AVISO reindexacion] {exc}")


def aplicar_filas(filas: list[dict], origen: str) -> dict:
    """Mueve exactamente las filas con accion=mover, al destino escrito, sin volver a clasificar.

    Cada fila lleva `hash` (sha256 del contenido al simular): si el archivo cambio despues, queda en el origen y se
    avisa; una fila sin hash (propuesta anterior a la verificacion) no se mueve: hay que volver a simular. Levanta
    TandaEnCurso (sin mover nada) si otra tanda tiene el bloqueo despues de esperar ESPERA_BLOQUEO."""
    with _bloqueo():
        return _aplicar_filas(filas, origen)


def _aplicar_filas(filas: list[dict], origen: str) -> dict:
    respaldar(RUTEO_CSV)
    inicio = len(leer_ruteo())
    movidos, avisos, publicados = 0, [], 0
    for f in filas:
        if f.get("accion", "").strip().lower() != "mover":
            continue
        src, destino = Path(f["archivo"]), Path(f["destino"])
        if not src.is_file():
            avisos.append(f"[ya no esta] {src}")
            continue
        if es_reservado(src) or es_reservado(destino):
            avisos.append(f"[archivo de la zona del sistema, no se toca] {src} -> {destino}")
            continue
        if not destino.resolve().is_relative_to(BASE.resolve()):
            avisos.append(f"[destino fuera de {BASE}] {src} -> {destino}")
            continue
        h_simulado = (f.get("hash") or "").strip()
        if not h_simulado:
            avisos.append(f"[propuesta sin identidad (anterior a la verificacion): volver a simular] {src}")
            continue
        try:
            h_actual = hash_archivo(src)
        except OSError as exc:
            avisos.append(f"[no se pudo verificar, queda en origen] {src}: {exc}")
            continue
        if h_actual != h_simulado:
            avisos.append(f"[cambio desde la simulacion, queda en origen] {src}")
            continue
        try:
            dst = mover(src, destino, f.get("motivo", ""), f.get("detalle", ""), f.get("nombre_nuevo") or None)
            if dst == src:  # ya estaba en esa carpeta: no se movio ni se anoto
                continue
            pedido = (Path(f["nombre_nuevo"]).stem + src.suffix) if f.get("nombre_nuevo") else src.name
            if dst.name != pedido:
                avisos.append(f"[ya habia un archivo con ese nombre: quedo como {dst.name}] {src}")
            movidos += 1
            publicados += _publicar(dst, f.get("motivo", ""), f.get("detalle", ""))
        except OSError as exc:
            avisos.append(f"[ERROR] {src}: {exc}")
    tanda = _tanda_de_lo_escrito(origen, inicio)
    if publicados:
        _reindexar()
    return {"movidos": movidos, "avisos": avisos, "tanda": tanda, "publicados": publicados}


def aplicar_csv(ruta: Path) -> dict:
    r = aplicar_filas(_leer_propuestas(ruta), ruta.name)
    for a in r["avisos"]:
        print(f"  {a}")
    print(f"\nMovidos: {r['movidos']} | avisos: {len(r['avisos'])} | tanda: {r['tanda']}\nRegistro: {RUTEO_CSV}")
    return r


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--src", type=Path, default=None,
                    help="Carpeta origen (default: _INBOX)")
    ap.add_argument("--csv", type=Path, default=None, help="Guarda las propuestas en un CSV")
    ap.add_argument("--omitir", nargs="+", default=[], choices=MOTIVOS, metavar="MOTIVO",
                    help="Motivos que no se mueven: " + ", ".join(MOTIVOS))
    ap.add_argument("--incluir-no-clasificados", action="store_true",
                    help="Fuera del INBOX, tambien mueve lo que no se reconoce (a _INBOX/sin_clasificar, etc.)")
    ap.add_argument("--aplicar-csv", type=Path, default=None, metavar="CSV",
                    help="Mueve las filas con accion=mover de un CSV de simulacion revisado, sin reclasificar")
    args = ap.parse_args()
    asegurar_estructura()

    if args.aplicar_csv:
        try:
            aplicar_csv(args.aplicar_csv)
        except TandaEnCurso as exc:
            print(f"[AVISO] no se movio nada: {exc}")
        return

    origen = args.src or INBOX
    if not origen.exists():
        print(f"No existe: {origen}")
        return

    pendientes = archivos_pendientes(origen)
    if not pendientes:
        print(f"{origen}: sin novedades.")
        return

    print(f"{len(pendientes)} archivos para rutear"
          f"{' (simulacion)' if args.dry_run else ''}\n")
    conteo: Counter = Counter()
    filas, a_mover = [], []
    for p in pendientes:
        d = destino_de(p)
        conteo[d.motivo] += 1
        se_queda = d.motivo in args.omitir or (
            origen != INBOX and d.motivo in NO_CLASIFICADOS and not args.incluir_no_clasificados)
        h = ""
        if args.csv or not (se_queda or args.dry_run):  # la identidad se toma al clasificar: al aplicar se verifica
            try:
                h = hash_archivo(p)
            except OSError:
                pass
        filas.append([p, d.carpeta, d.motivo, d.detalle, d.confianza, "queda" if se_queda else "mover", h])
        if se_queda:
            continue
        if args.dry_run:
            rel = p.relative_to(origen)
            try:
                dst_rel = d.carpeta.relative_to(BASE)
            except ValueError:
                dst_rel = d.carpeta
            print(f"  {str(rel)[:60]:60s} -> {dst_rel}  [{d.motivo} {d.detalle}]")
            continue
        a_mover.append({"archivo": str(p), "destino": str(d.carpeta), "motivo": d.motivo, "detalle": d.detalle,
                        "accion": "mover", "hash": h})
    if a_mover:  # con el mismo bloqueo, respaldo y registro de tanda que el dashboard: se puede deshacer
        try:
            r = aplicar_filas(a_mover, f"cli:{origen.name}")
        except TandaEnCurso as exc:
            print(f"[AVISO] no se movio nada: {exc}")
        else:
            for a in r["avisos"]:
                print(f"  {a}")
            print(f"\nMovidos: {r['movidos']} | tanda: {r['tanda']}")
    if args.csv:
        with args.csv.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["archivo", "destino", "motivo", "detalle", "confianza", "accion", "hash"])
            w.writerows(filas)
        print(f"\nPropuestas: {args.csv}")
    print("\n--- Resumen ---")
    for k, v in conteo.most_common():
        print(f"  {k}: {v}")
    if not args.dry_run:
        print(f"\nRegistro: {RUTEO_CSV}")


if __name__ == "__main__":
    main()
