"""Regresiones de la auditoria multidimensional (informe A, 2026-11), verificadas contra el codigo de colegiados:
indice sin filas a medias, cartera ilegible o con BOM, bloqueo de la cartera entre procesos, alias con caracteres
de regex, OCR sin temporal compartido, respaldos atomicos y deshacer que no mueve archivos fuera de la base."""
import csv
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import fitz
import pytest

import busqueda as B
import cartera as C
import cartera_cli
import config
import extractor as X
import fallo_md
import fase2_expedientes as F2
import ingesta as I
import kb_index
import kb_publicar
import ocr_completo as O
import respaldo
from cartera import Causa, CarteraCorrupta, CarteraOcupada, bloqueada, cargar, normalizar_expte
from cartera_cruce import Cruce, aplicar
from cartera_fuentes import _reparar

RAIZ_APP = Path(I.__file__).resolve().parent
CUERPO = "Providencia del juzgado. Se tiene presente lo manifestado por la parte y se ordena el traslado. " * 2
requiere_ocr = pytest.mark.skipif(not X.ocr_disponible(), reason="falta spa.traineddata")


def escribir(ruta: Path, texto: str, encoding: str = "utf-8") -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding=encoding)
    return ruta


def pdf_con_texto(ruta: Path, paginas: list[str]) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    d = fitz.open()
    for t in paginas:
        d.new_page().insert_textbox(fitz.Rect(40, 40, 550, 800), t, fontsize=10)
    d.save(str(ruta))
    return ruta


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    base = tmp_path / "SIGED"
    for d in ("01_Expedientes/2022-perez-gomez/01_siged", "_INBOX", "_SISTEMA"):
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


# ── busqueda: D015, D018, D020 ───────────────────────────────────────

def _base_busqueda(tmp_path: Path) -> Path:
    b = tmp_path / "SIGED"
    for d in ("01_Expedientes/2022-perez-gomez/01_siged", "_SISTEMA"):
        (b / d).mkdir(parents=True)
    return b


def test_sincronizar_no_deja_una_fila_huerfana_si_falla_el_alta_en_fts(tmp_path, monkeypatch):
    base, db = _base_busqueda(tmp_path), tmp_path / "busqueda.db"
    escribir(base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt", "contrato de locacion rural")

    def alta_rota(con, ruta_s, base_, h, tam, mt):
        con.execute("INSERT INTO docs (ruta, hash, tamano, mtime_ns, nombre, area, caso, tipo, fuente) "
                    "VALUES (?,?,?,?,?,?,?,?,?)", (ruta_s, h, tam, mt, "a.txt", "01_Expedientes", "x", "", "texto"))
        raise B.sqlite3.OperationalError("fts roto")

    with monkeypatch.context() as m:
        m.setattr(B, "_insertar", alta_rota)
        r = B.sincronizar(base=base, db=db)
    assert r["errores"] == 1
    assert B.estado(db)["docs"] == 0  # antes: 1 fila en docs que ninguna busqueda por contenido encontraba

    r = B.sincronizar(base=base, db=db)  # sin el fallo, la proxima pasada lo indexa de verdad
    assert r["nuevos"] == 1 and r["errores"] == 0
    assert B.buscar("locacion", db=db)["total"] == 1


def test_el_alta_fallida_de_un_archivo_cambiado_no_borra_el_indice_de_otro(tmp_path, monkeypatch):
    base, db = _base_busqueda(tmp_path), tmp_path / "busqueda.db"
    a = escribir(base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt", "texto alfa bien indexado")
    b = escribir(base / "01_Expedientes/2022-perez-gomez/01_siged/b.txt", "texto beta tambien")
    B.sincronizar(base=base, db=db)

    def no_hashea(path):
        raise OSError("abierto en otro programa")

    a.write_text("texto alfa editado", encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(B, "hash_archivo", no_hashea)
        r = B.sincronizar(base=base, db=db)
    assert r["errores"] == 1 and B.estado(db)["docs"] == 2  # el viejo de a.txt se conserva: no se pudo leer el nuevo
    assert B.buscar("beta", db=db)["total"] == 1


def test_reconstruir_con_el_indice_abierto_avisa_en_vez_de_un_traceback(tmp_path, monkeypatch):
    db = tmp_path / "busqueda.db"
    db.write_bytes(b"")
    monkeypatch.setattr(B, "BUSQUEDA_DB", db)
    monkeypatch.setattr(sys, "argv", ["busqueda.py", "--reconstruir"])

    def bloqueado(self, missing_ok=False):
        raise PermissionError("[WinError 32] en uso")

    monkeypatch.setattr(Path, "unlink", bloqueado)
    with pytest.raises(SystemExit) as exc:
        B.main()
    assert "Cerra el panel" in str(exc.value) and "en uso" in str(exc.value)


def test_registrar_movimiento_deja_rastro_si_el_indice_esta_danado(tmp_path, caplog):
    base, db = _base_busqueda(tmp_path), tmp_path / "busqueda.db"
    db.write_bytes(b"esto no es una base de sqlite " * 50)
    with caplog.at_level(logging.WARNING, logger="busqueda"):
        assert B.registrar_movimiento(base / "a.txt", base / "b.txt", base=base, db=db) is False
    assert "no pudo seguir el movimiento" in caplog.text


# ── cartera: D001-D004, D006, D014 ───────────────────────────────────

def test_cartera_con_bom_se_lee(tmp_path):
    ruta = tmp_path / "cartera.json"
    ruta.write_bytes(b"\xef\xbb\xbf" + '{"causas": [{"expte": "123456/2022", "caratula": "PEÑA c/ LUNA"}]}'.encode())
    causas = cargar(ruta)
    assert [c.expte.clave for c in causas] == ["123456/2022"] and causas[0].caratula == "PEÑA c/ LUNA"


@pytest.mark.parametrize("contenido", [b"", b'{"causas": [{"expte": "123456/2022"', b"\xff\xfe\x00no es utf8 \xe9",
                                       b"[1, 2, 3]"])
def test_cartera_ilegible_levanta_un_error_claro_y_nunca_devuelve_vacio(tmp_path, contenido):
    ruta = tmp_path / "cartera.json"
    ruta.write_bytes(contenido)
    with pytest.raises(CarteraCorrupta) as exc:
        cargar(ruta)
    assert isinstance(exc.value, ValueError)  # el dashboard y la ingesta ya atrapan ValueError
    assert "cartera.json" in str(exc.value) and respaldo.CARPETA in str(exc.value)


def test_cartera_ignora_filas_sin_expte_legible_o_con_el_numero_como_entero(tmp_path):
    ruta = tmp_path / "cartera.json"
    ruta.write_text('{"causas": [{"expte": 12345}, {"caratula": "sin numero"}, "texto suelto", null, '
                    '{"expte": "123456/2022", "campo_futuro": 1}]}', encoding="utf-8")
    assert [c.expte.clave for c in cargar(ruta)] == ["123456/2022"]
    assert normalizar_expte(12345) is None and normalizar_expte(None) is None


def test_el_anio_del_incidente_tiene_dos_o_cuatro_digitos():
    assert normalizar_expte("111112/2023 bis1/24").bis_anio == 24
    assert normalizar_expte("111112/2023 bis1/2024").bis_anio == 24
    assert normalizar_expte("111112/2023 bis1/245") is None  # antes se truncaba a 45


def test_la_ingesta_ignora_una_cartera_que_no_es_utf8_en_vez_de_caerse(entorno, capsys):
    I.CARTERA_JSON.write_bytes(b'{"causas": [{"expte": "1/2022", "caratula": "Pe\xf1a"}]}')
    assert I._indice().por_clave == {}
    assert "cartera ilegible" in capsys.readouterr().err


def test_aliases_con_bom_se_leen(entorno, tmp_path):
    f = tmp_path / "aliases.json"
    f.write_bytes(b"\xef\xbb\xbf" + b'{"aliases": [{"pattern": "perez", "caso": "2022-perez-gomez"}]}')
    assert len(I._leer_aliases(f)) == 1


def test_hosts_permitidos_con_bom_no_deja_el_primer_host_inutil(tmp_path, monkeypatch):
    (tmp_path / "_SISTEMA").mkdir()
    (tmp_path / "_SISTEMA" / "hosts_permitidos.txt").write_bytes(b"\xef\xbb\xbftunel.ejemplo.com\n# nota\notro.ejemplo.com\n")
    monkeypatch.setattr(config, "BASE", tmp_path)
    monkeypatch.delenv("ORGANIZADOR_HOSTS", raising=False)
    assert config.hosts_extra() == ["tunel.ejemplo.com", "otro.ejemplo.com"]


def test_reparar_cubre_mas_variantes_de_mojibake_y_no_toca_lo_que_esta_bien():
    assert _reparar("NÂ° 5") == "N° 5"
    assert _reparar("a â€“ b") == "a – b"
    assert _reparar("PEÃ‘A") == "PEÑA"
    assert _reparar("N° 5") == "N° 5" and _reparar("Â suelto") == "Â suelto"


# ── cartera_alias: D007 ──────────────────────────────────────────────

def test_el_apellido_con_caracteres_de_regex_no_rompe_ni_da_falsos_positivos(tmp_path):
    import re
    from cartera_alias import aliases_desde_cartera
    casos = tmp_path / "01_Expedientes"
    for nombre in ("2020-o.brien-gomez", "2021-mc(donald-luna"):
        (casos / nombre / "01_siged").mkdir(parents=True)
    causas = [Causa(normalizar_expte("111111/2020"), carpeta="2020-o.brien-gomez"),
              Causa(normalizar_expte("222222/2021"), carpeta="2021-mc(donald-luna")]
    aliases, _ = aliases_desde_cartera(causas, casos)
    patrones = {a["caso"]: re.compile(a["pattern"], re.I) for a in aliases}  # antes: re.error con "mc(donald"
    assert patrones["2020-o.brien-gomez"].search("escrito o.brien")
    assert not patrones["2020-o.brien-gomez"].search("escrito oxbrien")  # el "." ya no es cualquier letra
    assert patrones["2021-mc(donald-luna"].search("mc(donald")


# ── cartera_cli: D004, D005, D009, D010, D011 ────────────────────────

def test_importar_sin_fuentes_avisa_y_no_toca_la_cartera(tmp_path, capsys):
    cartera_cli.main(["--base", str(tmp_path), "importar"])
    assert "Nada para importar" in capsys.readouterr().out
    assert not (tmp_path / "_SISTEMA" / "cartera.json").exists()


def test_importar_lee_fuentes_con_bom(tmp_path, capsys):
    propios = tmp_path / "propios.txt"
    propios.write_bytes(b"\xef\xbb\xbf123456-2022\n")
    escritorio = tmp_path / "escritorio.json"
    escritorio.write_bytes(b"\xef\xbb\xbf" + b'[{"numero": "222222/2023", "designacion": "A c/ B"}]')
    cartera_cli.main(["--base", str(tmp_path), "importar", "--propios", str(propios), "--escritorio", str(escritorio)])
    claves = {c.expte.clave for c in cargar(tmp_path / "_SISTEMA" / "cartera.json")}
    assert claves == {"123456/2022", "222222/2023"}  # con BOM, el primer numero se perdia como "no se pudo leer"
    assert "no se pudo leer" not in capsys.readouterr().out


def test_con_la_cartera_ilegible_la_consola_sale_con_un_error_y_no_la_pisa(tmp_path):
    sistema = tmp_path / "_SISTEMA"
    sistema.mkdir()
    (sistema / "cartera.json").write_bytes(b'{"causas": [{"expte": "123456/2022"')  # guardado interrumpido
    propios = escribir(tmp_path / "propios.txt", "999999-2022\n")
    with pytest.raises(SystemExit) as exc:
        cartera_cli.main(["--base", str(tmp_path), "importar", "--propios", str(propios)])
    assert "[ERROR]" in str(exc.value) and "cartera.json" in str(exc.value)
    assert (sistema / "cartera.json").read_bytes() == b'{"causas": [{"expte": "123456/2022"'


def test_aliases_respalda_el_archivo_anterior_y_no_deja_temporales(tmp_path):
    sistema = tmp_path / "_SISTEMA"
    (tmp_path / "01_Expedientes" / "2022-fernandoz-luna" / "01_siged").mkdir(parents=True)
    sistema.mkdir()
    cartera_cli.main(["--base", str(tmp_path), "importar", "--propios", str(escribir(tmp_path / "p.txt", "111111-2022\n"))])
    c = cargar(sistema / "cartera.json")
    c[0].carpeta = "2022-fernandoz-luna"
    C.guardar(c, sistema / "cartera.json")
    destino = sistema / "aliases_cartera.json"
    destino.write_text('{"aliases": ["version anterior editada a mano"]}', encoding="utf-8")
    cartera_cli.main(["--base", str(tmp_path), "aliases"])
    copias = list((sistema / respaldo.CARPETA).glob("aliases_cartera.json.*.bak"))
    assert len(copias) == 1 and "version anterior" in copias[0].read_text(encoding="utf-8")
    assert "fernandoz" in destino.read_text(encoding="utf-8")
    assert not list(sistema.glob("*.tmp"))


def test_la_cartera_se_modifica_de_a_un_proceso(tmp_path, monkeypatch):
    ruta = tmp_path / "_SISTEMA" / "cartera.json"
    codigo = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from cartera import bloqueada
        with bloqueada(Path(sys.argv[2])):
            print("listo", flush=True)
            time.sleep(30)
    """)
    hijo = subprocess.Popen([sys.executable, "-c", codigo, str(RAIZ_APP), str(ruta)], stdout=subprocess.PIPE, text=True)
    try:
        assert hijo.stdout.readline().strip() == "listo"
        with pytest.raises(CarteraOcupada):
            with bloqueada(ruta, espera=0.3):
                pass
        monkeypatch.setattr(cartera_cli, "bloqueada", lambda r: bloqueada(r, espera=0.3))
        propios = escribir(tmp_path / "p.txt", "123456-2022\n")
        with pytest.raises(SystemExit) as exc:  # la consola no pisa lo que el otro proceso esta guardando
            cartera_cli.main(["--base", str(tmp_path), "importar", "--propios", str(propios)])
        assert "otro proceso" in str(exc.value) and not ruta.exists()
    finally:
        hijo.kill()
        hijo.wait()
    with bloqueada(ruta, espera=5):  # el sistema operativo lo libero al morir el proceso: no queda trabado
        pass


def test_dos_bloques_seguidos_de_un_mismo_proceso_no_se_traban(tmp_path):
    ruta = tmp_path / "cartera.json"
    with bloqueada(ruta, espera=0.3):
        pass
    with bloqueada(ruta, espera=0.3):
        pass


# ── cartera_cruce: D013 ──────────────────────────────────────────────

def test_el_registro_de_cambios_vacio_lleva_encabezado(tmp_path):
    casos = tmp_path / "01_Expedientes"
    casos.mkdir()
    log = tmp_path / "CARTERA_APLICADO.csv"
    log.write_bytes(b"")  # existe pero esta vacio
    aplicar([Cruce(Causa(normalizar_expte("123456/2022")), None, "sin_carpeta", "crear", "2022-a-b")], casos, log)
    filas = list(csv.reader(log.open(encoding="utf-8")))
    assert filas[0] == ["fecha", "accion", "origen", "destino"] and filas[1][1:] == ["crear", "", "2022-a-b"]


# ── ingesta: I035, I038, _leer_csv ───────────────────────────────────

def _fila_ruteo(origen: Path, destino: Path) -> None:
    with I.RUTEO_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(I.COLUMNAS_RUTEO)
        w.writerow(["2026-11-01T10:00:00", origen, destino, "cartera", "", "", ""])


def test_deshacer_no_mueve_un_destino_que_esta_fuera_de_la_base(entorno, tmp_path):
    ajeno = escribir(tmp_path / "ajeno" / "importante.txt", "no es del organizador")
    _fila_ruteo(tmp_path / "otro_lado" / "x.txt", ajeno)  # historial editado a mano
    r = I.deshacer_indices([0], "prueba")
    assert r["restaurados"] == 0 and ajeno.is_file() and not (tmp_path / "otro_lado").exists()
    assert any("destino fuera de" in a for a in r["avisos"])


def test_deshacer_si_devuelve_el_archivo_a_un_origen_fuera_de_la_base(entorno, tmp_path):
    origen = escribir(tmp_path / "Descargas" / "a.txt", CUERPO)
    dst = I.mover(origen, entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged", "cartera")
    r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 1 and origen.is_file() and not dst.exists()


def test_el_expediente_deshecho_deja_de_ensenar_a_que_carpeta_va(entorno, tmp_path):
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged"
    a = escribir(tmp_path / "o" / "a.txt", CUERPO)
    b = escribir(tmp_path / "o" / "b.txt", CUERPO + "otro")
    I.mover(a, carpeta, "expediente", "111111/2022")
    I.mover(b, carpeta, "expediente", "222222/2022")
    assert I._mapa_exptes() == {"111111/2022": "2022-perez-gomez", "222222/2022": "2022-perez-gomez"}
    I.deshacer_indices([0], "solo la primera")
    assert I._mapa_exptes() == {"222222/2022": "2022-perez-gomez"}  # antes seguia mapeado a la carpeta vieja


def test_un_csv_con_bytes_que_cp1252_no_define_se_lee_igual(tmp_path):
    ruta = tmp_path / "REORG_x.csv"
    ruta.write_bytes(b"archivo,destino,accion\r\nlpez\x81.txt,carpeta,mover\r\n")  # 0x81: sin definir en cp1252
    filas = I._leer_propuestas(ruta)
    assert len(filas) == 1 and filas[0]["accion"] == "mover"


# ── ocr_completo: I002, I003 ─────────────────────────────────────────

def _escaneado(ruta: Path) -> Path:
    origen = fitz.open()
    p = origen.new_page()
    p.insert_text((50, 100), "Senor Juez: promueve accion de prescripcion adquisitiva.", fontsize=14)
    d = fitz.open()
    q = d.new_page(width=p.rect.width, height=p.rect.height)
    q.insert_image(q.rect, pixmap=p.get_pixmap(dpi=200))
    d.save(str(ruta))
    return ruta


@requiere_ocr
def test_el_ocr_no_pisa_el_trabajo_de_otra_corrida_sobre_el_mismo_contenido(tmp_path):
    pdf = _escaneado(tmp_path / "esc.pdf")
    destino = tmp_path / "ocr"
    carpeta = destino / O.hash_archivo(pdf)
    ajeno = carpeta / "tmp"  # la carpeta temporal de otra corrida en curso (consola y panel a la vez)
    ajeno.mkdir(parents=True)
    (ajeno / "texto.txt").write_text("a medio escribir", encoding="utf-8")
    r = O.ocr_completo(pdf, destino)
    assert r["estado"] == "ok" and (ajeno / "texto.txt").read_text(encoding="utf-8") == "a medio escribir"
    assert [p.name for p in carpeta.iterdir() if p.name != "tmp"] == ["texto.txt"]  # el propio temporal se limpio


@requiere_ocr
def test_si_el_ocr_falla_no_queda_temporal_ni_salida(tmp_path, monkeypatch):
    pdf = _escaneado(tmp_path / "esc.pdf")
    destino = tmp_path / "ocr"

    def falla(*a, **k):
        raise RuntimeError("tesseract murio")

    monkeypatch.setattr(O, "dpi_para", falla)
    with pytest.raises(RuntimeError):
        O.ocr_completo(pdf, destino, con_pdf=True)
    assert list((destino / O.hash_archivo(pdf)).iterdir()) == []


# ── respaldo: I013, D027, D028 ───────────────────────────────────────

def test_respaldar_no_deja_una_copia_truncada_si_se_corta_a_mitad(tmp_path, monkeypatch):
    f = escribir(tmp_path / "cartera.json", "{}")

    def corta(origen, destino, **k):
        Path(destino).write_bytes(b"{")
        raise OSError("disco lleno")

    monkeypatch.setattr(respaldo.shutil, "copy2", corta)
    with pytest.raises(OSError):
        respaldo.respaldar(f)
    assert list((tmp_path / respaldo.CARPETA).iterdir()) == []  # ni .bak ni .tmp que la proxima vez parezca la ultima copia


def test_respaldar_distingue_contenidos_del_mismo_largo_y_no_repite_lo_igual(tmp_path):
    f = escribir(tmp_path / "cartera.json", '{"a": 1}')
    assert respaldo.respaldar(f) is not None
    assert respaldo.respaldar(f) is None
    f.write_text('{"a": 2}', encoding="utf-8")  # mismo largo, otro contenido
    assert respaldo.respaldar(f) is not None


def test_el_bundle_que_no_verifica_se_borra(tmp_path, monkeypatch):
    def falso(cmd, **k):
        if "create" in cmd:
            Path(cmd[3]).write_bytes(b"bundle truncado")
            return
        raise subprocess.CalledProcessError(1, cmd, stderr="bundle roto")

    monkeypatch.setattr(respaldo.subprocess, "run", falso)
    with pytest.raises(subprocess.CalledProcessError):
        respaldo.respaldar_codigo(tmp_path / "codigo")
    assert list((tmp_path / "codigo").iterdir()) == []


# ── kb_index: I012 ───────────────────────────────────────────────────

def test_rebuild_no_usa_un_temporal_fijo_que_otra_reconstruccion_pueda_pisar(tmp_path):
    (tmp_path / "fallos").mkdir()
    ajeno = tmp_path / "LOCAL_INDEX.json.tmp"  # el temporal de otra reconstruccion en curso
    ajeno.write_text("a medio escribir", encoding="utf-8")
    destino = kb_index.rebuild(tmp_path)
    assert destino.is_file() and '"version"' in destino.read_text(encoding="utf-8")
    assert ajeno.read_text(encoding="utf-8") == "a medio escribir"
    assert [p.name for p in tmp_path.glob("*.tmp")] == ["LOCAL_INDEX.json.tmp"]


# ── fallo_md / kb_publicar / extractor / fase2 ───────────────────────

def test_limpiar_conserva_las_secciones_aunque_se_repitan_por_pagina():
    pagina = "VISTOS:\nJUZGADO CIVIL Y COMERCIAL N 2 POSADAS\nel escrito de fojas 4 y sus constancias.\n"
    out = fallo_md.limpiar(pagina * 3)
    assert out.count("VISTOS:") == 3 and "JUZGADO CIVIL" not in out
    md = fallo_md.a_markdown(Path("f.txt"), pagina * 3)
    assert md.count("## Vistos") == 3


def test_texto_completo_se_corta_en_la_cota_de_caracteres(tmp_path, monkeypatch):
    monkeypatch.setattr(fallo_md, "MAX_CHARS_PDF_COMPLETO", 600)
    pdf = pdf_con_texto(tmp_path / "digesto.pdf", [f"PAGINA{i} " + "sentencia considerando resuelve " * 30 for i in range(6)])
    texto = fallo_md.texto_completo(pdf)
    assert "PAGINA0" in texto and "PAGINA5" not in texto


def test_la_huella_es_sha256():
    assert len(fallo_md.huella("# t\ncuerpo del fallo")) == 64


def test_dos_fallos_con_el_mismo_nombre_en_carpetas_distintas_no_se_pisan(tmp_path):
    a = pdf_con_texto(tmp_path / "x" / "sentencia.pdf", ["Autos caratulados Perez c/ Gomez. " * 12 + "primero"])
    b = pdf_con_texto(tmp_path / "y" / "sentencia.pdf", ["Autos caratulados Luna c/ Vidal. " * 12 + "segundo"])
    r = fallo_md.convertir([a, b], tmp_path / "md")
    assert r["convertidos"] == 2
    assert sorted(p.name for p in (tmp_path / "md").iterdir()) == ["sentencia.md", "sentencia__1.md"]


def test_publicar_una_norma_extrae_el_texto_una_sola_vez(tmp_path, monkeypatch):
    archivo = escribir(tmp_path / "normativa sin numero.txt",
                       "Ley 24.240\nArticulo 1. Objeto de la ley.\nArticulo 2. Consumidor.\n" * 3)
    llamadas = []
    original = fallo_md.texto_completo
    monkeypatch.setattr(fallo_md, "texto_completo", lambda p: llamadas.append(p) or original(p))
    ruta = kb_publicar.publicar(archivo, "rama", "legislacion_nacion", tmp_path / "corpus")
    assert ruta is not None and ruta.name == "ley-24240.md" and len(llamadas) == 1  # antes: 3 lecturas del archivo


def test_un_doc_de_cero_bytes_no_se_confunde_con_falta_de_antiword(tmp_path):
    vacio = tmp_path / "vacio.doc"
    vacio.write_bytes(b"")
    assert X.extraer_universal(vacio) == (None, "ERROR_DOC_vacio")


def test_decidir_reconoce_las_carpetas_de_origen_con_cualquier_separador():
    f = {"ruta": Path("/pool/Referencia/a.pdf"), "expte": None, "caratula": None, "propio": False,
         "matricula": False, "juris": False, "texto_ok": True}
    assert F2.decidir(f, {})[1] == "referencia"


def test_destino_unico_no_busca_para_siempre(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(F2, "MAX_INTENTOS_NOMBRE", 20)
    with pytest.raises(OSError):
        F2.destino_unico(tmp_path / "a.txt")
