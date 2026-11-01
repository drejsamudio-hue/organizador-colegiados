import hashlib
import shutil

import fitz
import pytest

import ocr_completo as O
from extractor import ocr_disponible

requiere_ocr = pytest.mark.skipif(not ocr_disponible(), reason="falta spa.traineddata")

FRASE = "Señor Juez: promueve acción de prescripción. Honorarios del poseedor, año 2022."


def pdf_con_texto(ruta, paginas=1):
    d = fitz.open()
    for _ in range(paginas):
        d.new_page().insert_text((50, 100), FRASE * 2, fontsize=12)
    d.save(str(ruta))
    return ruta


def pdf_escaneado(ruta, paginas=1, texto=FRASE):
    d = fitz.open()
    for _ in range(paginas):
        origen = fitz.open()
        p = origen.new_page()
        p.insert_text((50, 100), texto, fontsize=14)
        pix = p.get_pixmap(dpi=200)
        q = d.new_page(width=p.rect.width, height=p.rect.height)
        q.insert_image(q.rect, pixmap=pix)
    d.save(str(ruta))
    return ruta


def test_pdf_con_texto_no_es_escaneado(tmp_path):
    assert O.es_escaneado(pdf_con_texto(tmp_path / "a.pdf", 3)) is False


def test_pdf_de_imagenes_es_escaneado(tmp_path):
    assert O.es_escaneado(pdf_escaneado(tmp_path / "a.pdf", 3)) is True


def test_muestrea_varias_paginas_y_no_solo_la_primera(tmp_path):
    d = fitz.open()
    d.new_page()
    for _ in range(2):
        d.new_page().insert_text((50, 100), FRASE * 2, fontsize=12)
    d.save(str(tmp_path / "a.pdf"))
    assert O.es_escaneado(tmp_path / "a.pdf") is False


def test_archivo_ilegible_no_es_escaneado(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"no soy un pdf")
    assert O.es_escaneado(tmp_path / "a.pdf") is False


def test_dpi_baja_si_la_pagina_es_muy_grande():
    assert O.dpi_para(595, 842) == 300
    assert O.dpi_para(5000, 5000) < 300
    assert O.dpi_para(5000, 5000) * 5000 / 72 <= O.LADO_MAXIMO


def test_hash_es_sha256_del_contenido(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"contenido")
    assert O.hash_archivo(f) == hashlib.sha256(b"contenido").hexdigest()


@requiere_ocr
def test_ocr_genera_pdf_buscable_y_txt_con_tildes_y_enes(tmp_path):
    salida = tmp_path / "ocr"
    r = O.ocr_completo(pdf_escaneado(tmp_path / "esc.pdf"), salida, con_pdf=True)
    assert r["estado"] == "ok" and r["paginas"] == 1
    assert r["pdf"].parent == salida / r["hash"]
    txt = r["txt"].read_text(encoding="utf-8")
    for palabra in ("Señor", "acción", "prescripción", "año"):
        assert palabra in txt
    with fitz.open(str(r["pdf"])) as buscable:
        assert buscable.page_count == 1
        assert "prescripción" in buscable[0].get_text()


@requiere_ocr
def test_ocr_de_varias_paginas_las_une_en_orden(tmp_path):
    d = fitz.open()
    for texto in ("Primera pagina alfa", "Segunda pagina beta", "Tercera pagina gamma"):
        origen = fitz.open()
        p = origen.new_page()
        p.insert_text((50, 100), texto, fontsize=16)
        q = d.new_page(width=p.rect.width, height=p.rect.height)
        q.insert_image(q.rect, pixmap=p.get_pixmap(dpi=200))
    d.save(str(tmp_path / "tres.pdf"))
    r = O.ocr_completo(tmp_path / "tres.pdf", tmp_path / "ocr")
    assert r["paginas"] == 3
    txt = r["txt"].read_text(encoding="utf-8").lower()
    assert txt.index("alfa") < txt.index("beta") < txt.index("gamma")


@requiere_ocr
def test_mismo_contenido_con_otra_ruta_reutiliza_la_salida(tmp_path):
    salida = tmp_path / "ocr"
    original = pdf_escaneado(tmp_path / "esc.pdf")
    primero = O.ocr_completo(original, salida)
    movido = tmp_path / "otra_carpeta" / "renombrado.pdf"
    movido.parent.mkdir()
    shutil.move(str(original), str(movido))
    segundo = O.ocr_completo(movido, salida)
    assert segundo["estado"] == "reutilizado"
    assert segundo["hash"] == primero["hash"] and segundo["pdf"] == primero["pdf"]
    assert len(list(salida.iterdir())) == 1


@requiere_ocr
def test_no_modifica_el_original(tmp_path):
    f = pdf_escaneado(tmp_path / "esc.pdf")
    antes = O.hash_archivo(f)
    O.ocr_completo(f, tmp_path / "ocr")
    assert O.hash_archivo(f) == antes


def test_pdf_con_texto_no_se_procesa_ni_escribe(tmp_path):
    salida = tmp_path / "ocr"
    r = O.ocr_completo(pdf_con_texto(tmp_path / "txt.pdf"), salida)
    assert r["estado"] == "con_texto"
    assert not salida.exists()


@requiere_ocr
def test_salida_incompleta_se_rehace(tmp_path):
    salida = tmp_path / "ocr"
    f = pdf_escaneado(tmp_path / "esc.pdf")
    r = O.ocr_completo(f, salida)
    r["txt"].unlink()
    again = O.ocr_completo(f, salida)
    assert again["estado"] == "ok" and again["txt"].is_file()


def test_sin_tesseract_devuelve_sin_ocr_y_no_escribe(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "ocr_disponible", lambda: False)
    f = pdf_escaneado(tmp_path / "esc.pdf")
    r = O.ocr_completo(f, tmp_path / "ocr")
    assert r["estado"] == "sin_ocr" and not (tmp_path / "ocr").exists()


@requiere_ocr
def test_la_imagen_del_buscable_va_en_gris_y_jpeg_para_no_inflar_el_archivo(tmp_path):
    r = O.ocr_completo(pdf_escaneado(tmp_path / "esc.pdf"), tmp_path / "ocr", con_pdf=True)
    with fitz.open(str(r["pdf"])) as buscable:
        imagenes = [buscable.extract_image(x[0]) for x in buscable[0].get_images()]
    assert imagenes and all(i["ext"] == "jpeg" and i["colorspace"] == 1 for i in imagenes)


def pdf_escaneado_paginas(ruta, textos):
    d = fitz.open()
    for texto in textos:
        origen = fitz.open()
        p = origen.new_page()
        p.insert_text((50, 100), texto, fontsize=16)
        q = d.new_page(width=p.rect.width, height=p.rect.height)
        q.insert_image(q.rect, pixmap=p.get_pixmap(dpi=200))
    d.save(str(ruta))
    return ruta


def test_pdf_ilegible_se_informa_como_tal(tmp_path):
    f = tmp_path / "roto.pdf"
    f.write_bytes(b"no soy un pdf")
    r = O.ocr_completo(f, tmp_path / "ocr")
    assert r["estado"] == "ilegible" and not (tmp_path / "ocr").exists()


@requiere_ocr
def test_la_ingesta_lee_el_texto_completo_por_hash_aunque_el_archivo_se_haya_movido(tmp_path, monkeypatch):
    import extractor as X
    monkeypatch.setattr(X, "OCR_CACHE", tmp_path / "cache")
    monkeypatch.setattr(O, "OCR_DIR", tmp_path / "ocr")
    original = pdf_escaneado_paginas(tmp_path / "esc.pdf", ["Se tiene presente lo manifestado por la parte."] * 3 + ["Cuarta pagina con la palabra zeta"])
    O.ocr_completo(original, tmp_path / "ocr")
    movido = tmp_path / "otra" / "renombrado.pdf"
    movido.parent.mkdir()
    shutil.move(str(original), str(movido))
    texto, err = X.extraer_universal(movido)
    assert err == "ocr" and "zeta" in texto.lower()
    assert not (tmp_path / "cache").exists()


def test_sin_salida_por_hash_no_hay_texto_completo(tmp_path, monkeypatch):
    monkeypatch.setattr(O, "OCR_DIR", tmp_path / "vacio")
    assert O.texto_por_hash(pdf_escaneado(tmp_path / "esc.pdf")) is None


@requiere_ocr
def test_lote_procesa_registra_y_en_la_segunda_pasada_reutiliza(tmp_path):
    entrada = tmp_path / "in"
    (entrada / "sub").mkdir(parents=True)
    pdf_escaneado(entrada / "sub" / "esc.pdf")
    pdf_con_texto(entrada / "txt.pdf")
    (entrada / "roto.pdf").write_bytes(b"no soy un pdf")
    (entrada / "nota.txt").write_text("no es pdf", encoding="utf-8")
    salida, log = tmp_path / "ocr", tmp_path / "ocr_log.csv"
    filas = O.procesar_lote([entrada], salida, log)
    assert {f["archivo"]: f["estado"] for f in filas} == {
        str(entrada / "sub" / "esc.pdf"): "ok", str(entrada / "txt.pdf"): "con_texto", str(entrada / "roto.pdf"): "ilegible"}
    assert len(log.read_text(encoding="utf-8").splitlines()) == 4
    otra = O.procesar_lote([entrada], salida, log)
    assert sorted(f["estado"] for f in otra) == ["con_texto", "ilegible", "reutilizado"]


@requiere_ocr
def test_lote_en_simulacion_no_escribe_nada(tmp_path):
    entrada = tmp_path / "in"
    entrada.mkdir()
    pdf_escaneado(entrada / "esc.pdf")
    salida, log = tmp_path / "ocr", tmp_path / "ocr_log.csv"
    filas = O.procesar_lote([entrada], salida, log, simular=True)
    assert [f["estado"] for f in filas] == ["pendiente"]
    assert not salida.exists() and not log.exists()


@requiere_ocr
def test_lote_respeta_el_limite_y_sigue_en_la_proxima_tanda(tmp_path):
    entrada = tmp_path / "in"
    entrada.mkdir()
    for i in range(3):
        pdf_escaneado(entrada / f"esc{i}.pdf", texto=f"{FRASE} numero {i}")
    salida, log = tmp_path / "ocr", tmp_path / "ocr_log.csv"
    assert [f["estado"] for f in O.procesar_lote([entrada], salida, log, limite=2)].count("ok") == 2
    segunda = [f["estado"] for f in O.procesar_lote([entrada], salida, log, limite=2)]
    assert sorted(segunda) == ["ok", "reutilizado", "reutilizado"]


def test_lote_omite_la_carpeta_de_salida_si_esta_dentro_de_la_entrada(tmp_path):
    entrada = tmp_path / "in"
    salida = entrada / "ocr"
    salida.mkdir(parents=True)
    pdf_con_texto(salida / "yo.pdf")
    assert O.procesar_lote([entrada], salida, tmp_path / "log.csv") == []


@requiere_ocr
def test_salida_vacia_se_reutiliza_salvo_que_se_pida_reintentar(tmp_path):
    salida = tmp_path / "ocr"
    f = pdf_escaneado(tmp_path / "esc.pdf")
    r = O.ocr_completo(f, salida)
    r["txt"].write_text(" \n", encoding="utf-8")
    assert O.ocr_completo(f, salida)["estado"] == "reutilizado"
    again = O.ocr_completo(f, salida, reintentar_vacios=True)
    assert again["estado"] == "ok" and O.texto_guardado(again["txt"].parent)


def test_texto_guardado_ignora_salidas_vacias_o_faltantes(tmp_path):
    assert O.texto_guardado(tmp_path / "no-existe") is None
    (tmp_path / O.TXT).write_text("", encoding="utf-8")
    assert O.texto_guardado(tmp_path) is None
    (tmp_path / O.TXT).write_text("año", encoding="utf-8")
    assert O.texto_guardado(tmp_path) == "año"


def test_el_lote_usa_el_hash_del_indice_si_el_archivo_no_cambio(tmp_path, monkeypatch):
    import sqlite3
    entrada = tmp_path / "in"
    entrada.mkdir()
    f = pdf_con_texto(entrada / "a.pdf")
    st = f.stat()
    db = tmp_path / "busqueda.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE docs (ruta TEXT, hash TEXT, tamano INTEGER, mtime_ns INTEGER)")
    con.execute("INSERT INTO docs VALUES (?,?,?,?)", (str(f), "h" * 64, st.st_size, st.st_mtime_ns))
    con.commit()
    con.close()
    monkeypatch.setattr(O, "hash_archivo", lambda p: pytest.fail("no debe releer el archivo"))
    filas = O.procesar_lote([entrada], tmp_path / "ocr", tmp_path / "log.csv", simular=True, db=db)
    assert [(x["hash"], x["estado"]) for x in filas] == [("h" * 64, "con_texto")]


def test_sin_carpetas_recorre_toda_la_base_menos_sistema_y_cuarentena(tmp_path, monkeypatch, capsys):
    base = tmp_path / "SIGED"
    for d in ("_INBOX", "01_Expedientes/2022-perez-gomez", "_SISTEMA", "_CUARENTENA"):
        (base / d).mkdir(parents=True)
        pdf_con_texto(base / d / "x.pdf")
    vistos = []
    monkeypatch.setattr(O, "BASE", base)
    monkeypatch.setattr(O, "procesar_lote", lambda carpetas, **kw: vistos.extend(carpetas) or [])
    monkeypatch.setattr("sys.argv", ["ocr_completo.py", "--simular"])
    O.main()
    assert sorted(p.name for p in vistos) == ["01_Expedientes", "_INBOX"]


@requiere_ocr
def test_por_defecto_solo_guarda_el_texto(tmp_path):
    r = O.ocr_completo(pdf_escaneado(tmp_path / "esc.pdf"), tmp_path / "ocr")
    assert r["estado"] == "ok" and r["pdf"] is None
    assert [p.name for p in (tmp_path / "ocr" / r["hash"]).iterdir()] == [O.TXT]
    assert "prescripción" in O.texto_guardado(tmp_path / "ocr" / r["hash"])
