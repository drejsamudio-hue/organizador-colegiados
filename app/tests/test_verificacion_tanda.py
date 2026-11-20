"""Regresiones de la verificacion de tandas (revision externa de 2026-11).

Un archivo editado entre la simulacion y la aplicacion no se mueve, un destino que aparece entremedio no se
pisa, la zona del sistema nunca es candidata, la interfaz y la consola comparten un bloqueo (aunque el proceso
que lo tenia muera), los incidentes del mismo principal no se mezclan y una carpeta que solo coincide por nombre
no recibe archivos con confianza alta.
"""
import csv
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import ingesta as I
import ocr_completo
from cartera import Causa, cargar, guardar, normalizar_expte
from cartera_alta import agregar, carpeta_libre, compilar_patron, expte_en_nombre, parsear_lista
from cartera_cruce import cruzar, indice_exptes, reporte_md

CUERPO = "Providencia del juzgado. Se tiene presente lo manifestado por la parte y se ordena el traslado. " * 2
RAIZ_APP = Path(I.__file__).resolve().parent


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    base = tmp_path / "SIGED"
    for d in ("01_Expedientes/2022-perez-gomez/01_siged", "01_Expedientes/2023-vidal-luna", "_INBOX", "_SISTEMA"):
        (base / d).mkdir(parents=True)
    for nombre, valor in {
        "BASE": base, "INBOX": base / "_INBOX", "CASOS": base / "01_Expedientes", "SISTEMA": base / "_SISTEMA",
        "RUTEO_CSV": base / "_SISTEMA" / "INGESTA_RUTEO.csv", "TANDAS_CSV": base / "_SISTEMA" / "TANDAS.csv",
        "CARTERA_JSON": base / "_SISTEMA" / "cartera.json",
        "ALIAS_CARTERA": base / "_SISTEMA" / "aliases_cartera.json", "ALIAS_FILE": tmp_path / "sin_aliases.json",
    }.items():
        monkeypatch.setattr(I, nombre, valor)
    monkeypatch.setattr(I, "_exptes_cache", None)
    monkeypatch.setattr(I, "_indice_cache", (-1.0, I.IndiceCartera([])))
    monkeypatch.setattr(I, "_alias_cache", ((-1.0, -1.0), []))
    monkeypatch.setattr(I, "ESPERA_BLOQUEO", 0.3)
    return base


def escribir(ruta: Path, texto: str) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def fila(origen: Path, destino: Path, **extra) -> dict:
    return {"archivo": str(origen), "destino": str(destino), "accion": "mover", "hash": I.hash_archivo(origen), **extra}


def correr(monkeypatch, *argumentos):
    monkeypatch.setattr(sys, "argv", ["ingesta.py", *argumentos])
    I.main()


VIDAL = Path("01_Expedientes") / "2023-vidal-luna" / "01_siged"


# ── 1. Identidad del archivo, de la simulacion a la aplicacion ───────

def test_el_archivo_editado_entre_simulacion_y_aplicacion_queda_en_origen(entorno, tmp_path):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    filas = [fila(a, entorno / VIDAL)]
    a.write_text(CUERPO.replace("traslado", "medida cautelar"), encoding="utf-8")  # edicion posterior
    r = I.aplicar_filas(filas, "t")
    assert r["movidos"] == 0 and a.is_file()
    assert any("cambio desde la simulacion, queda en origen" in x for x in r["avisos"])
    assert I.leer_ruteo() == [] and I.leer_tandas() == []  # no quedo rastro de movimiento


def test_una_propuesta_vieja_sin_identidad_no_se_mueve(entorno, tmp_path):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    r = I.aplicar_filas([{"archivo": str(a), "destino": str(entorno / VIDAL), "accion": "mover"}], "t")  # CSV anterior
    assert r["movidos"] == 0 and a.is_file()
    assert any("propuesta sin identidad" in x and "volver a simular" in x for x in r["avisos"])
    r = I.aplicar_filas([{"archivo": str(a), "destino": str(entorno / VIDAL), "accion": "mover", "hash": "  "}], "t")
    assert r["movidos"] == 0 and a.is_file()


def test_la_identidad_verifica_el_contenido_no_la_fecha(entorno, tmp_path):
    """Mismo largo, distinto contenido: solo el hash lo distingue."""
    a = escribir(tmp_path / "o" / "a.txt", "arrendamiento " * 3)
    filas = [fila(a, entorno / VIDAL)]
    a.write_text("servidumbre!! " * 3, encoding="utf-8")
    assert I.aplicar_filas(filas, "t")["movidos"] == 0


def test_la_identidad_igual_mueve(entorno, tmp_path):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    r = I.aplicar_filas([fila(a, entorno / VIDAL)], "t")
    assert r["movidos"] == 1 and (entorno / VIDAL / "a.txt").is_file() and not a.exists()


def test_el_hash_es_el_mismo_que_calcula_ocr_completo(tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    assert I.hash_archivo(a) == ocr_completo.hash_archivo(a)


def test_un_archivo_que_no_se_puede_leer_para_verificar_queda_en_origen(entorno, tmp_path, monkeypatch):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    filas = [fila(a, entorno / VIDAL)]

    def falla(ruta):
        raise PermissionError("abierto")
    monkeypatch.setattr(I, "hash_archivo", falla)
    r = I.aplicar_filas(filas, "t")
    assert r["movidos"] == 0 and a.is_file() and any("no se pudo verificar" in x for x in r["avisos"])


def test_la_consola_escribe_el_hash_y_aplicar_csv_lo_verifica(entorno, tmp_path, monkeypatch, capsys):
    guardar([Causa(normalizar_expte("111111/2022"), carpeta="2022-perez-gomez")], entorno / "_SISTEMA" / "cartera.json")
    origen = tmp_path / "origen"
    a = escribir(origen / "escrito 111111-2022.txt", CUERPO)
    propuesta = tmp_path / "propuesta.csv"
    correr(monkeypatch, "--src", str(origen), "--dry-run", "--csv", str(propuesta))
    (r,) = list(csv.DictReader(propuesta.open(encoding="utf-8-sig")))
    assert r["hash"] == I.hash_archivo(a) and a.is_file()
    a.write_text(CUERPO + " editado", encoding="utf-8")
    correr(monkeypatch, "--aplicar-csv", str(propuesta))
    assert a.is_file() and "cambio desde la simulacion" in capsys.readouterr().out


def test_la_consola_mueve_con_la_identidad_tomada_al_clasificar(entorno, tmp_path, monkeypatch):
    guardar([Causa(normalizar_expte("111111/2022"), carpeta="2022-perez-gomez")], entorno / "_SISTEMA" / "cartera.json")
    origen = tmp_path / "origen"
    a = escribir(origen / "escrito 111111-2022.txt", CUERPO)
    correr(monkeypatch, "--src", str(origen))
    assert not a.exists() and len(list((entorno / "01_Expedientes" / "2022-perez-gomez").rglob(a.name))) == 1


def test_una_propuesta_vacia_o_de_cabecera_rara_se_lee(tmp_path):
    vacia = tmp_path / "vacia.csv"
    vacia.write_bytes(b"")
    assert I._leer_propuestas(vacia) == []
    rara = tmp_path / "rara.csv"
    rara.write_text("x\n1\n", encoding="utf-8")
    assert I._leer_propuestas(rara) == [{"x": "1"}]
    excel = tmp_path / "excel.csv"
    excel.write_text("archivo;destino\r\nA;B\r\n", encoding="cp1252")
    assert I._leer_propuestas(excel) == [{"archivo": "A", "destino": "B"}]


# ── 2. Nunca pisa, sin ventana de carrera ────────────────────────────

def test_mover_nunca_pisa_un_destino_que_aparecio_entremedio(entorno, tmp_path, monkeypatch):
    carpeta = entorno / VIDAL
    carpeta.mkdir(parents=True, exist_ok=True)
    a = escribir(tmp_path / "nota.txt", "ESCRITO ORIGINAL")
    real_unico, carrera = I.destino_unico, [False]

    def unico_con_carrera(dest):
        libre = real_unico(dest)
        if dest.name == "nota.txt" and not carrera[0]:
            carrera[0] = True
            escribir(carpeta / "nota.txt", "APARECIO ENTREMEDIO")  # justo despues del chequeo
            return dest  # el nombre que parecia libre
        return libre

    monkeypatch.setattr(I, "destino_unico", unico_con_carrera)
    dst = I.mover(a, carpeta, "cartera", "prueba")
    assert dst.name == "nota__1.txt"
    assert (carpeta / "nota.txt").read_text(encoding="utf-8") == "APARECIO ENTREMEDIO"  # no se piso
    assert dst.read_text(encoding="utf-8") == "ESCRITO ORIGINAL" and not a.exists()


def test_mover_entre_unidades_tambien_prueba_otro_nombre_si_el_destino_aparece(entorno, tmp_path, monkeypatch):
    """Si el rename falla por unidad distinta, la copia verificada tampoco pisa: FileExistsError y otro nombre."""
    carpeta = entorno / VIDAL
    a = escribir(tmp_path / "nota.txt", "ESCRITO ORIGINAL")
    real_unico, real_renombrar, estado = I.destino_unico, I._renombrar_sin_pisar, {"carrera": False}

    def unico_con_carrera(dest):
        libre = real_unico(dest)
        if dest.name == "nota.txt" and not estado["carrera"]:
            estado["carrera"] = True
            escribir(carpeta / "nota.txt", "APARECIO ENTREMEDIO")
            return dest
        return libre

    def otra_unidad(src, dst):
        if src == a:
            raise OSError(I.errno.EXDEV, "cross-device")
        real_renombrar(src, dst)
    monkeypatch.setattr(I, "destino_unico", unico_con_carrera)
    monkeypatch.setattr(I, "_renombrar_sin_pisar", otra_unidad)
    dst = I.mover(a, carpeta, "cartera", "prueba")
    assert dst.name == "nota__1.txt" and (carpeta / "nota.txt").read_text(encoding="utf-8") == "APARECIO ENTREMEDIO"
    assert dst.read_text(encoding="utf-8") == "ESCRITO ORIGINAL" and not a.exists()
    assert not list(carpeta.glob(".~*"))  # no queda copia a medias


def test_mover_con_el_nombre_siempre_ocupado_no_pisa_y_avisa(entorno, tmp_path, monkeypatch):
    carpeta = entorno / VIDAL
    a = escribir(tmp_path / "nota.txt", "NUEVO")
    ocupado = escribir(carpeta / "nota.txt", "VIEJO")
    monkeypatch.setattr(I, "destino_unico", lambda d: d)
    with pytest.raises(OSError, match="ya existe"):
        I.mover(a, carpeta, "cartera")
    assert ocupado.read_text(encoding="utf-8") == "VIEJO" and a.read_text(encoding="utf-8") == "NUEVO"


def test_renombrar_sin_pisar_falla_si_el_destino_existe(tmp_path):
    src = escribir(tmp_path / "a.txt", "x")
    existente = escribir(tmp_path / "b.txt", "y")
    with pytest.raises(FileExistsError):
        I._renombrar_sin_pisar(src, existente)
    assert existente.read_text(encoding="utf-8") == "y" and src.exists()
    libre = tmp_path / "c.txt"
    I._renombrar_sin_pisar(src, libre)
    assert libre.read_text(encoding="utf-8") == "x" and not src.exists()


def test_copiar_sin_pisar_no_trunca_un_destino_existente(tmp_path):
    src = escribir(tmp_path / "a.txt", "x" * 100)
    existente = escribir(tmp_path / "b.txt", "ORIGINAL")
    with pytest.raises(FileExistsError):
        I._copiar_sin_pisar(src, existente)
    assert existente.read_text(encoding="utf-8") == "ORIGINAL"
    nuevo = tmp_path / "c.txt"
    I._copiar_sin_pisar(src, nuevo)
    assert nuevo.read_text(encoding="utf-8") == "x" * 100 and src.is_file()


def test_mover_entre_unidades_no_trunca_un_destino_existente(tmp_path):
    src = escribir(tmp_path / "a.txt", "x" * 100)
    existente = escribir(tmp_path / "b.txt", "ORIGINAL")
    with pytest.raises(FileExistsError):
        I._mover_entre_unidades(src, existente)
    assert existente.read_text(encoding="utf-8") == "ORIGINAL" and src.is_file()


def test_deshacer_no_pisa_un_origen_que_se_ocupo_entremedio(entorno, tmp_path, monkeypatch):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    I.aplicar_filas([fila(a, entorno / VIDAL)], "t")
    real = I._renombrar

    def con_carrera(src, dst):
        escribir(a, "OTRO ARCHIVO")  # el origen se ocupa justo antes de devolver
        real(src, dst)
    monkeypatch.setattr(I, "_renombrar", con_carrera)
    r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 0 and any("se ocupo entremedio" in x for x in r["avisos"])
    assert a.read_text(encoding="utf-8") == "OTRO ARCHIVO" and (entorno / VIDAL / "a.txt").is_file()


# ── 3. La zona del sistema no entra en las tandas ────────────────────

def test_la_zona_del_sistema_nunca_es_candidata(entorno):
    for n in ("cartera.json", "INGESTA_RUTEO.csv", "busqueda.db", "ingesta.lock", "TANDAS.csv"):
        escribir(entorno / "_SISTEMA" / n, "x")
    escribir(entorno / "_SISTEMA" / "ocr_por_hash" / "abc" / "texto.txt", "ocr")
    escribir(entorno / "REORG_vieja.csv", "propuesta")
    escribir(entorno / "busqueda.db-wal", "wal")
    escribir(entorno / "01_Expedientes" / "2022-perez-gomez" / ".~a1b2c3d4e5f6.incompleto", "copia a medias")
    escribir(entorno / "_INBOX" / "escrito.pdf", "contenido")
    pendientes = I.archivos_pendientes(entorno)  # el origen es la propia base: error facil
    assert [p.name for p in pendientes] == ["escrito.pdf"]


def test_es_reservado():
    base = Path("F:/estudio")
    assert I.es_reservado(base / "_SISTEMA" / "x" / "y.txt") and I.es_reservado(base / "_SISTEMA")
    assert I.es_reservado(base / "cartera.json") and I.es_reservado(base / "REORG_20260101-1.csv")
    assert I.es_reservado(base / "a" / ".~0123456789ab.incompleto")
    assert not I.es_reservado(base / "01_Expedientes" / "2022-x" / "escrito.pdf")
    assert not I.es_reservado(base / "REORGANIZADO.csv")


def test_aplicar_rechaza_filas_que_tocan_la_zona_del_sistema(entorno, tmp_path):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    dentro = escribir(entorno / "_SISTEMA" / "cartera.json", "{}")
    filas = [fila(a, entorno / "_SISTEMA" / "x"), fila(dentro, entorno / "01_Expedientes")]
    r = I.aplicar_filas(filas, "t")
    assert r["movidos"] == 0 and a.is_file() and dentro.is_file()
    assert len([x for x in r["avisos"] if "zona del sistema" in x]) == 2


def test_mover_rechaza_la_zona_del_sistema(entorno, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    with pytest.raises(OSError, match="zona del sistema"):
        I.mover(a, entorno / "_SISTEMA" / "algo", "prueba")
    dentro = escribir(entorno / "_SISTEMA" / "cartera.json", "{}")
    with pytest.raises(OSError, match="zona del sistema"):
        I.mover(dentro, entorno / VIDAL, "prueba")
    assert a.is_file() and dentro.is_file() and not (entorno / "_SISTEMA" / "algo").exists()


def test_deshacer_no_toca_la_zona_del_sistema(entorno):
    cartera = escribir(entorno / "_SISTEMA" / "cartera.json", "{}")
    I.RUTEO_CSV.write_text("fecha,origen,destino,motivo,detalle,tamano,mtime_ns\n"
                           f"2026-01-01T00:00:00,{entorno / '_INBOX' / 'c.json'},{cartera},prueba,,2,0\n",
                           encoding="utf-8")
    r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 0 and any("zona del sistema" in x for x in r["avisos"]) and cartera.is_file()


# ── 4. Un bloqueo de tanda compartido por UI y consola ───────────────

def test_no_hay_dos_tandas_a_la_vez(entorno):
    with I.bloqueo_tanda():
        with pytest.raises(I.TandaEnCurso, match="otra tanda.*en curso"):
            with I.bloqueo_tanda():
                pass
    with I.bloqueo_tanda():  # liberado, se puede volver a tomar
        pass


def test_tanda_en_curso_es_un_ocupado_y_el_bloqueo_viejo_es_el_mismo(entorno):
    assert issubclass(I.TandaEnCurso, I.Ocupado) and issubclass(I.Ocupado, OSError)
    with I._bloqueo():
        with pytest.raises(I.Ocupado):
            with I.bloqueo_tanda(espera=0):
                pass
    with I.bloqueo_tanda():
        with pytest.raises(I.TandaEnCurso):
            with I._bloqueo(espera=0):
                pass


def test_el_bloqueo_deja_el_pid_y_no_lo_deja_al_soltarlo(entorno):
    lock = entorno / "_SISTEMA" / "ingesta.lock"
    with I.bloqueo_tanda():
        assert I._pid_del_bloqueo(lock) == I.os.getpid()
        with pytest.raises(I.TandaEnCurso, match=f"proceso {I.os.getpid()}"):
            with I.bloqueo_tanda(espera=0):
                pass
    assert I._pid_del_bloqueo(lock) is None


def test_un_bloqueo_con_basura_de_un_proceso_que_murio_se_recupera(entorno):
    (entorno / "_SISTEMA" / "ingesta.lock").write_text("99999999", encoding="utf-8")
    with I.bloqueo_tanda(espera=0):
        pass
    (entorno / "_SISTEMA" / "ingesta.lock").write_text("no-es-un-pid", encoding="utf-8")
    with I.bloqueo_tanda(espera=0):
        pass


def test_el_bloqueo_de_otro_proceso_se_respeta_y_se_libera_si_muere(entorno):
    codigo = textwrap.dedent("""
        import sys, time
        from pathlib import Path
        sys.path.insert(0, sys.argv[1])
        import ingesta
        ingesta.RUTEO_CSV = Path(sys.argv[2])
        with ingesta.bloqueo_tanda():
            print("listo", flush=True)
            time.sleep(60)
    """)
    hijo = subprocess.Popen([sys.executable, "-c", codigo, str(RAIZ_APP), str(I.RUTEO_CSV)],
                            stdout=subprocess.PIPE, text=True)
    try:
        assert hijo.stdout.readline().strip() == "listo"
        with pytest.raises(I.TandaEnCurso, match=f"proceso {hijo.pid}"):
            with I.bloqueo_tanda(espera=0):
                pass
        with pytest.raises(I.TandaEnCurso):
            I.aplicar_filas([], "t")
    finally:
        hijo.kill()
        hijo.wait()
    with I.bloqueo_tanda(espera=10):  # murio sin soltar: el sistema operativo libero el bloqueo
        pass


def test_la_consola_no_mueve_nada_si_hay_otra_tanda(entorno, tmp_path, monkeypatch, capsys):
    guardar([Causa(normalizar_expte("111111/2022"), carpeta="2022-perez-gomez")], entorno / "_SISTEMA" / "cartera.json")
    a = escribir(tmp_path / "o" / "escrito 111111-2022.txt", CUERPO)
    with I.bloqueo_tanda():
        correr(monkeypatch, "--src", str(a.parent))
    assert a.is_file() and "no se movio nada" in capsys.readouterr().out and I.leer_ruteo() == []
    propuesta = escribir(tmp_path / "p.csv", f"archivo,destino,accion,hash\n{a},{entorno / VIDAL},mover,{I.hash_archivo(a)}\n")
    with I.bloqueo_tanda():
        correr(monkeypatch, "--aplicar-csv", str(propuesta))
    assert a.is_file() and "no se movio nada" in capsys.readouterr().out


def test_deshacer_con_otra_tanda_en_curso_avisa_sin_mover(entorno, tmp_path):
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    I.aplicar_filas([fila(a, entorno / VIDAL)], "t")
    with I.bloqueo_tanda():
        r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 0 and "otra tanda" in r["avisos"][0] and (entorno / VIDAL / "a.txt").is_file()


# ── 5. Identidad de causa: incidentes y carpetas ─────────────────────

def _incidentes():
    return [Causa(normalizar_expte("111111/2022"), carpeta="2022-perez-gomez"),
            Causa(normalizar_expte("111111/2022 bis1/23"), carpeta="2022-perez-gomez/bis1-23"),
            Causa(normalizar_expte("111111/2022 bis1/24"), carpeta="2022-perez-gomez/bis1-24")]


def test_dos_incidentes_bis1_de_anios_distintos_no_se_mezclan():
    idx = I.IndiceCartera(_incidentes())
    assert idx.por_numero(111111, 2022, 1) is None  # ambiguo: no elige solo
    assert idx.por_numero(111111, 2022, 1, 23) == "2022-perez-gomez/bis1-23"
    assert idx.por_numero(111111, 2022, 1, 24) == "2022-perez-gomez/bis1-24"
    assert idx.por_numero(111111, 2022, 1, 2024) == "2022-perez-gomez/bis1-24"
    assert idx.por_numero(111111, 2022, 1, 25) is None  # hay incidentes con ese bis, pero no de ese anio
    assert idx.por_numero(111111, 2022) == "2022-perez-gomez"
    assert idx.por_numero(111111, 2022, 2) == "2022-perez-gomez"  # ningun bis2: sigue el principal


def test_un_unico_incidente_se_rutea_sin_el_anio():
    idx = I.IndiceCartera(_incidentes()[:2])
    assert idx.por_numero(111111, 2022, 1) == "2022-perez-gomez/bis1-23"


def test_el_nombre_con_el_anio_del_incidente_va_al_incidente_correcto(entorno, tmp_path):
    for sub in ("bis1-23", "bis1-24"):
        (entorno / "01_Expedientes" / "2022-perez-gomez" / sub).mkdir()
    guardar(_incidentes(), entorno / "_SISTEMA" / "cartera.json")
    destino = lambda nombre: I.destino_de(escribir(tmp_path / nombre, CUERPO))  # noqa: E731
    d = destino("111111-2022 bis1-24 cedula.txt")
    assert d.carpeta.parent == entorno / "01_Expedientes" / "2022-perez-gomez" / "bis1-24"
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_nombre", "alta")
    d = destino("111111-2022 bis1-2023 cedula.txt")
    assert d.carpeta.parent.name == "bis1-23"
    assert destino("111111-2022 bis1 cedula.txt").motivo != "cartera"  # ambiguo: dos incidentes bis1


def test_la_lista_pegada_conserva_el_incidente():
    items, invalidas = parsear_lista("5786/2022 bis1/24 PEREZ c/ GARCIA s/ DAÑOS\n1234/2023 LUNA c/ SOL")
    assert not invalidas
    assert items[0][0].clave_completa == "5786/2022 bis1/24"  # el bis no va a la caratula
    assert items[0][1] == "PEREZ c/ GARCIA s/ DAÑOS"
    assert items[1][0].clave_completa == "1234/2023"


def test_el_escaneo_captura_el_incidente_en_nombres():
    assert expte_en_nombre("expte 5786-2022 bis 1-24").clave_completa == "5786/2022 bis1/24"
    assert expte_en_nombre("expte 5786-2022 BIS1/2024").clave_completa == "5786/2022 bis1/24"
    assert expte_en_nombre("expte 5786-2022").clave_completa == "5786/2022"
    propio = compilar_patron(r"EXP(\d+)\.(\d{4})")
    assert expte_en_nombre("EXP987.2020 sentencia", propio).clave_completa == "987/2020"


def test_una_causa_nueva_no_pretende_una_carpeta_que_ya_existe():
    e = normalizar_expte("222222/2024")
    nuevas, _ = agregar([], [(e, "VIDAL c/ LUNA")], "lista", en_disco={"2024-vidal-luna"})
    assert nuevas[0].carpeta_prevista == "2024-vidal-luna-222222"  # no la adopta por nombre
    nuevas, _ = agregar([], [(e, "VIDAL c/ LUNA")], "lista", en_disco={"2024-vidal-luna", "2024-vidal-luna-222222"})
    assert nuevas[0].carpeta_prevista == "2024-vidal-luna-222222-2"
    nuevas, _ = agregar([], [(e, "VIDAL c/ LUNA")], "lista")
    assert nuevas[0].carpeta_prevista == "2024-vidal-luna"


def test_carpeta_libre_no_repite_el_nombre_con_numero():
    c = Causa(normalizar_expte("222222/2024"), caratula="VIDAL c/ LUNA")
    assert carpeta_libre(c, {"2024-vidal-luna", "2024-vidal-luna-222222", "2024-vidal-luna-222222-2"}) \
        == "2024-vidal-luna-222222-3"


def test_carpeta_existente_sin_vincular_rutea_con_confianza_media(entorno, tmp_path):
    """Coincidir el nombre no es lo mismo que estar vinculada: baja a media para revisar."""
    (entorno / "01_Expedientes" / "2024-luna-sol").mkdir()
    guardar([Causa(normalizar_expte("222222/2024"), carpeta_prevista="2024-luna-sol")],
            entorno / "_SISTEMA" / "cartera.json")
    d = I.destino_de(escribir(tmp_path / "escrito 222222-2024.txt", CUERPO))
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_nombre", "media")


def test_carpeta_vinculada_rutea_con_confianza_alta(entorno, tmp_path):
    (entorno / "01_Expedientes" / "2024-luna-sol").mkdir()
    guardar([Causa(normalizar_expte("222222/2024"), carpeta="2024-luna-sol")], entorno / "_SISTEMA" / "cartera.json")
    d = I.destino_de(escribir(tmp_path / "escrito 222222-2024.txt", CUERPO))
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_nombre", "alta")


def test_carpeta_prevista_que_se_crea_al_mover_sigue_en_alta(entorno, tmp_path):
    guardar([Causa(normalizar_expte("222222/2024"), carpeta_prevista="2024-luna-sol")],
            entorno / "_SISTEMA" / "cartera.json")
    d = I.destino_de(escribir(tmp_path / "escrito 222222-2024.txt", CUERPO))
    assert (d.motivo, d.confianza) == ("cartera", "alta")


def test_el_expediente_hallado_solo_por_nombre_de_carpeta_va_en_media(entorno, tmp_path):
    (entorno / "01_Expedientes" / "2024-expte-333333-2024").mkdir()
    texto = "Expte. N 333333/2024 PEREZ JUAN c/ GOMEZ SA s/ Danos y perjuicios. " + CUERPO + " Tomo 12 Folio 34 del Colegio de Abogados."
    d = I.destino_de(escribir(tmp_path / "escrito.txt", texto))
    assert (d.motivo, d.detalle, d.confianza) == ("expediente", "333333/2024", "media")


def test_el_expediente_con_vinculo_en_el_historial_va_en_alta(entorno, tmp_path):
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    I.RUTEO_CSV.write_text("fecha,origen,destino,motivo,detalle,tamano,mtime_ns\n"
                           f"2026-01-01T00:00:00,x,{carpeta / '01_siged' / 'a.txt'},expediente,333333/2024,1,0\n",
                           encoding="utf-8")
    texto = "Expte. N 333333/2024 PEREZ JUAN c/ GOMEZ SA s/ Danos y perjuicios. " + CUERPO + " Tomo 12 Folio 34 del Colegio de Abogados."
    d = I.destino_de(escribir(tmp_path / "escrito.txt", texto))
    assert (d.motivo, d.carpeta.parent.name, d.confianza) == ("expediente", "2022-perez-gomez", "alta")


# ── Cruce: adopcion visible y recorrido unico ────────────────────────

def test_la_adopcion_por_nombre_se_ve_en_el_cruce_y_en_el_informe(tmp_path):
    casos = tmp_path / "01_Expedientes"
    (casos / "2024-vidal-luna").mkdir(parents=True)
    c = Causa(normalizar_expte("111111/2024"), caratula="VIDAL c/ LUNA", carpeta_prevista="2024-vidal-luna")
    (x,) = cruzar([c], casos, {})
    assert (x.accion, x.via) == ("ok", "prevista") and "adoptada por nombre" in x.nota and "cruzar" in x.nota
    assert "adoptada por nombre" in reporte_md([x], [], [], [])
    c2 = Causa(normalizar_expte("111111/2024"), caratula="VIDAL c/ LUNA", carpeta="2024-vidal-luna")
    (y,) = cruzar([c2], casos, {})
    assert y.via == "cartera" and y.nota == ""


def test_el_cruce_recorre_la_carpeta_una_sola_vez(tmp_path, monkeypatch):
    import cartera_cruce
    casos = tmp_path / "01_Expedientes"
    for i in range(30):
        (casos / f"2022-expte-{100000 + i}-2022").mkdir(parents=True)
    causas = [Causa(normalizar_expte(f"{100000 + i}/2022")) for i in range(30)]
    llamadas = []
    real = cartera_cruce.indice_exptes
    monkeypatch.setattr(cartera_cruce, "indice_exptes", lambda d: (llamadas.append(d), real(d))[1])
    cruces = cruzar(causas, casos, {})
    assert len(llamadas) == 1 and all(x.via == "nombre" for x in cruces)


def test_indice_exptes(tmp_path):
    casos = tmp_path / "01_Expedientes"
    for n in ("2022-expte-111111-2022", "2019-b-expte-111111-2022", "2023-vidal-luna"):
        (casos / n).mkdir(parents=True)
    (casos / "suelto-expte-5-2020.txt").write_text("x", encoding="utf-8")
    assert indice_exptes(casos) == {"expte-111111-2022": "2019-b-expte-111111-2022"}  # la primera por nombre
    assert indice_exptes(tmp_path / "no-existe") == {}


# ── v1: sin doble lectura y cartera tolerante ────────────────────────

def test_destino_de_y_ajeno_usan_el_texto_ya_extraido_sin_leer_el_archivo(entorno, tmp_path, monkeypatch):
    guardar([Causa(normalizar_expte("111111/2022"), carpeta="2022-perez-gomez")], entorno / "_SISTEMA" / "cartera.json")
    a = escribir(tmp_path / "escrito 111111-2022.txt", CUERPO)

    def no_leer(*x, **k):
        raise AssertionError("el archivo se leyo de nuevo")
    monkeypatch.setattr(I, "extraer_universal", no_leer)
    d = I.destino_de(a, texto=(CUERPO, None))
    assert (d.motivo, d.detalle) == ("cartera", "numero_nombre")
    assert I.destino_de(a, texto=(None, None)).motivo == "cartera"  # formato sin texto: se lee nada
    assert I.destino_ajeno(a, "2020-ajena-causa", texto=(None, "doc_lectura_binaria")).motivo == "ajena"
    assert I.destino_ajeno(a, "2020-ajena-causa", texto=(CUERPO, None)).carpeta \
        == entorno / "06_Expedientes_ajenos" / "2020-ajena-causa"


def test_la_cartera_ignora_campos_desconocidos_y_filas_rotas(tmp_path):
    ruta = tmp_path / "cartera.json"
    ruta.write_text('{"causas": [{"expte": "111111/2022", "carpeta": "2022-a", "campo_nuevo": 1},'
                    ' {"expte": "basura"}, {"sin_expte": 1}, "texto", 5,'
                    ' {"expte": "222222/2023 bis1/24", "caratula": "X c/ Y"}]}', encoding="utf-8")
    causas = cargar(ruta)
    assert [c.expte.clave_completa for c in causas] == ["111111/2022", "222222/2023 bis1/24"]
    assert causas[0].carpeta == "2022-a" and causas[1].caratula == "X c/ Y"
