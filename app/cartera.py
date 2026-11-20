"""Registro de causas propias: numero normalizado, carpeta canonica y alias."""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path

from respaldo import CARPETA, respaldar

RE_NUMERO = re.compile(
    r"^\s*(?P<numero>\d{1,7})\s*[/-]\s*(?P<anio>\d{4})"
    r"(?:\s*bis\s*(?P<bis>\d+)\s*[/-]\s*(?P<bis_anio>\d{4}|\d{2}))?"
    r"(?:\s*\(\s*EX\s*(?P<ex>[^)]+?)\s*\))?\s*$",
    re.I,
)


@dataclass(frozen=True)
class Expte:
    numero: int
    anio: int
    bis: int | None = None
    bis_anio: int | None = None
    ex: str | None = None

    @property
    def clave(self) -> str:
        return f"{self.numero}/{self.anio}"

    @property
    def clave_completa(self) -> str:
        if self.bis is None:
            return self.clave
        return f"{self.clave} bis{self.bis}/{self.bis_anio:02d}"

    @property
    def texto(self) -> str:
        return self.clave_completa + (f" (EX {self.ex})" if self.ex else "")

    @property
    def sospechoso(self) -> bool:
        return len(str(self.numero)) > 6


def normalizar_expte(texto: str) -> Expte | None:
    if not isinstance(texto, str):  # un JSON a mano con "expte": 12345
        return None
    m = RE_NUMERO.match(texto)
    if not m:
        return None
    g = m.groupdict()
    anio = int(g["anio"])
    if not 1980 <= anio <= 2035:
        return None
    return Expte(
        numero=int(g["numero"]),
        anio=anio,
        bis=int(g["bis"]) if g["bis"] else None,
        bis_anio=int(g["bis_anio"]) % 100 if g["bis_anio"] else None,
        ex=g["ex"].strip() if g["ex"] else None,
    )


@dataclass
class Causa:
    expte: Expte
    id_expediente: str | None = None
    caratula: str | None = None
    dependencia: str | None = None
    carpeta: str | None = None
    carpeta_prevista: str | None = None
    fuentes: list[str] = field(default_factory=list)


def fusionar(causas: list[Causa]) -> list[Causa]:
    """Une por numero completo. El primero que trae un dato gana; las fuentes se suman."""
    por_clave: dict[str, Causa] = {}
    for c in causas:
        previa = por_clave.get(c.expte.clave_completa)
        if previa is None:
            por_clave[c.expte.clave_completa] = replace(c, fuentes=list(c.fuentes))
            continue
        for campo in ("id_expediente", "caratula", "dependencia", "carpeta", "carpeta_prevista"):
            if not getattr(previa, campo) and getattr(c, campo):
                setattr(previa, campo, getattr(c, campo))
        previa.fuentes += [f for f in c.fuentes if f not in previa.fuentes]
        if c.expte.ex and not previa.expte.ex:
            previa.expte = replace(previa.expte, ex=c.expte.ex)
    return list(por_clave.values())


def posibles_erratas(causas: list[Causa]) -> list[tuple[str, str]]:
    """Pares del mismo anio cuyos numeros difieren en un solo digito (123450 vs 123460)."""
    pares = []
    for i, a in enumerate(causas):
        for b in causas[i + 1:]:
            if a.expte.anio != b.expte.anio or a.expte.bis != b.expte.bis:
                continue
            x, y = str(a.expte.numero), str(b.expte.numero)
            if len(x) == len(y) and sum(p != q for p, q in zip(x, y)) == 1:
                pares.append((a.expte.clave_completa, b.expte.clave_completa))
    return pares


def guardar(causas: list[Causa], ruta: Path) -> None:
    filas = []
    for c in causas:
        d = asdict(c)
        d["expte"] = c.expte.texto
        filas.append(d)
    texto = json.dumps({"generado": datetime.now().isoformat(timespec="seconds"), "causas": filas},
                       ensure_ascii=False, indent=2)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    respaldar(ruta)
    fd, tmp = tempfile.mkstemp(dir=ruta.parent, prefix=ruta.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(texto)
        os.replace(tmp, ruta)
    finally:
        Path(tmp).unlink(missing_ok=True)


class CarteraCorrupta(ValueError):
    """cartera.json existe pero no se puede leer (guardado interrumpido, no es UTF-8, no es un objeto JSON).
    Nunca se lo toma por una cartera vacia: quien la guardara despues la pisaria con solo lo nuevo."""


class CarteraOcupada(OSError):
    """Otro proceso (el panel o la consola) esta modificando la cartera."""


POSICION_BLOQUEO = 1 << 30  # lejos del contenido, como el bloqueo de las tandas


def _trabar(f, trabar: bool) -> None:
    f.seek(POSICION_BLOQUEO)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK if trabar else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f, (fcntl.LOCK_EX | fcntl.LOCK_NB) if trabar else fcntl.LOCK_UN)


@contextmanager
def bloqueada(ruta: Path, espera: float = 20.0):
    """Una sola modificacion de la cartera por vez, entre procesos. Quien lee, cambia y guarda la cartera lo hace
    adentro de este bloque: si no, el segundo guardado pisa lo que guardo el primero. El sistema operativo libera el
    bloqueo si el proceso muere. Orden de bloqueos: este por fuera, el de las tandas (ingesta) por dentro."""
    lock = ruta.with_name(ruta.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+b") as f:
        limite = time.monotonic() + espera
        while True:
            try:
                _trabar(f, True)
                break
            except OSError:
                if time.monotonic() >= limite:
                    raise CarteraOcupada("la cartera la esta modificando otro proceso (el panel o la consola); "
                                         "probar de nuevo en un momento") from None
                time.sleep(0.2)
        try:
            yield
        finally:
            _trabar(f, False)


def cargar(ruta: Path) -> list[Causa]:
    """Tolera campos desconocidos y filas sin expte legible: se ignoran esas filas, no todo el archivo. Un archivo
    ilegible o truncado levanta CarteraCorrupta (es un ValueError), nunca devuelve una cartera vacia."""
    if not ruta.exists():
        return []
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8-sig"))  # con BOM si lo re-guardo Excel o el Bloc de notas
    except ValueError as exc:  # JSONDecodeError y UnicodeDecodeError
        raise CarteraCorrupta(f"{ruta.name} no se puede leer ({exc}); la ultima copia buena esta en "
                              f"{ruta.parent / CARPETA}") from exc
    if not isinstance(datos, dict):
        raise CarteraCorrupta(f"{ruta.name} no tiene el formato esperado (se esperaba un objeto con 'causas'); "
                              f"la ultima copia buena esta en {ruta.parent / CARPETA}")
    causas = []
    campos_validos = {f.name for f in fields(Causa)} - {"expte"}
    for d in datos.get("causas", []):
        if not isinstance(d, dict):
            continue
        e = normalizar_expte(d.get("expte"))
        if e is None:
            continue
        causas.append(Causa(expte=e, **{k: v for k, v in d.items() if k in campos_validos}))
    return causas
