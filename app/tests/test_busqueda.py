import shutil
import sqlite3
from pathlib import Path

import fitz
import pytest

import busqueda as B
import ocr_completo as O


@pytest.fixture
def base(tmp_path):
    b = tmp_path / "SIGED"
    for d in ("01_Expedientes/2022-perez-gomez/01_siged", "02_Jurisprudencia/consumo/dano-punitivo", "03_Doctrina",
              "_INBOX/sin_clasificar", "_CUARENTENA", "_SISTEMA"):
        (b / d).mkdir(parents=True)
    return b


@pytest.fixture
def db(tmp_path):
    return tmp_path / "busqueda.db"


def escribir(ruta: Path, texto: str) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def pdf(ruta: Path, texto: str) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    d = fitz.open()
    d.new_page().insert_textbox(fitz.Rect(40, 40, 550, 800), texto, fontsize=12)
    d.save(str(ruta))
    return ruta


def sync(base, db, **kw):
    return B.sincronizar(base=base, db=db, **kw)


def buscar(db, consulta, **kw):
    return B.buscar(consulta, db=db, **kw)


def test_deriva_area_caso_y_tipo_de_la_ruta(base):
    d = lambda rel: B.derivar(base / rel, base)
    assert d("01_Expedientes/2022-perez-gomez/01_siged/a.pdf") == ("01_Expedientes", "2022-perez-gomez", "01_siged")
    assert d("01_Expedientes/2022-perez-gomez/a.pdf") == ("01_Expedientes", "2022-perez-gomez", "")
    assert d("02_Jurisprudencia/consumo/dano-punitivo/f.pdf") == ("02_Jurisprudencia", "", "consumo/dano-punitivo")
    assert d("06_Expedientes_ajenos/gomez/x.pdf") == ("06_Expedientes_ajenos", "gomez", "")
    assert d("_INBOX/sin_clasificar/x.pdf") == ("_INBOX", "", "sin_clasificar")
    assert d("03_Doctrina/x.pdf") == ("03_Doctrina", "", "")


def test_consulta_segura_para_fts5():
    assert B.consulta_fts("honorarios regulacion") == '"honorarios" "regulacion"'
    assert B.consulta_fts('"tutela sindical" art') == '"tutela sindical" "art"'
    assert B.consulta_fts("poseed*") == '"poseed"*'
    assert B.consulta_fts('a"b -c:d OR (x)') == '"a" "b" "c" "d" "or" "x"'
    assert B.consulta_fts("   ") == ""


def test_indexa_solo_las_carpetas_y_formatos_previstos(base, db):
    escribir(base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt", "escrito de embargo preventivo")
    escribir(base / "_INBOX/sin_clasificar/b.md", "embargo en el inbox")
    escribir(base / "_CUARENTENA/c.txt", "embargo en cuarentena")
    escribir(base / "_SISTEMA/d.txt", "embargo en sistema")
    (base / "03_Doctrina/foto.jpg").write_bytes(b"\xff\xd8")
    r = sync(base, db)
    assert r["nuevos"] == 2
    rutas = {x["ruta"] for x in buscar(db, "embargo")["resultados"]}
    assert rutas == {str(base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt"), str(base / "_INBOX/sin_clasificar/b.md")}



def test_busqueda_sin_tildes_pero_el_fragmento_las_conserva(base, db):
    escribir(base / "03_Doctrina/a.txt", "La prescripción adquisitiva del señor, año 2022.")
    sync(base, db)
    r = buscar(db, "prescripcion adquisitiva")
    assert r["total"] == 1 and "prescripción" in r["resultados"][0]["fragmento"]
    assert buscar(db, "senor")["total"] == 1


def test_frase_exacta_distinta_de_palabras_sueltas(base, db):
    escribir(base / "03_Doctrina/a.txt", "la tutela sindical protege")
    escribir(base / "03_Doctrina/b.txt", "sindical y luego tutela")
    sync(base, db)
    assert buscar(db, "tutela sindical")["total"] == 2
    assert buscar(db, '"tutela sindical"')["total"] == 1


def test_pdf_con_texto_se_lee_completo_y_el_nombre_tambien_se_busca(base, db):
    pdf(base / "03_Doctrina/informe-balance.pdf", "uno " * 50 + "palabraclave")
    sync(base, db)
    assert buscar(db, "palabraclave")["resultados"][0]["nombre"] == "informe-balance.pdf"
    assert buscar(db, "balance")["total"] == 1


def test_pdf_escaneado_usa_la_salida_de_ocr_por_hash(base, db, tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    monkeypatch.setattr(O, "OCR_DIR", ocr)
    d = fitz.open()
    d.new_page()
    d.save(str(base / "03_Doctrina/esc.pdf"))
    carpeta = ocr / O.hash_archivo(base / "03_Doctrina/esc.pdf")
    carpeta.mkdir(parents=True)
    shutil.copy(base / "03_Doctrina/esc.pdf", carpeta / O.PDF_BUSCABLE)
    (carpeta / O.TXT).write_text("poseedor de buena fe", encoding="utf-8")
    sync(base, db)
    assert buscar(db, "poseedor")["total"] == 1
    assert sqlite3.connect(db).execute("select fuente from docs").fetchone() == ("ocr",)


def test_pdf_sin_texto_se_relee_cuando_aparece_su_ocr(base, db, tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    monkeypatch.setattr(O, "OCR_DIR", ocr)
    d = fitz.open()
    d.new_page()
    d.save(str(base / "03_Doctrina/esc.pdf"))
    assert sync(base, db)["sin_texto"] == 1
    assert buscar(db, "poseedor")["total"] == 0
    assert sync(base, db)["cambiados"] == 0
    carpeta = ocr / O.hash_archivo(base / "03_Doctrina/esc.pdf")
    carpeta.mkdir(parents=True)
    shutil.copy(base / "03_Doctrina/esc.pdf", carpeta / O.PDF_BUSCABLE)
    (carpeta / O.TXT).write_text("poseedor de buena fe", encoding="utf-8")
    assert sync(base, db)["cambiados"] == 1
    assert buscar(db, "poseedor")["total"] == 1
    assert sqlite3.connect(db).execute("select fuente from docs").fetchone() == ("ocr",)
    assert sync(base, db)["cambiados"] == 0


def test_docx_se_indexa(base, db):
    from docx import Document
    doc = Document()
    doc.add_paragraph("contrato de locacion urbana")
    (base / "05_Modelos_y_Escritos").mkdir()
    doc.save(str(base / "05_Modelos_y_Escritos/m.docx"))
    sync(base, db)
    assert buscar(db, "locacion")["total"] == 1


def test_segunda_pasada_no_hace_nada(base, db):
    escribir(base / "03_Doctrina/a.txt", "algo para indexar")
    sync(base, db)
    assert sync(base, db) == {"nuevos": 0, "movidos": 0, "cambiados": 0, "borrados": 0, "sin_texto": 0, "errores": 0}


def test_archivo_movido_conserva_el_texto_y_actualiza_la_ruta(base, db, monkeypatch):
    a = escribir(base / "_INBOX/sin_clasificar/a.txt", "cedula de notificacion unica")
    sync(base, db)
    id_antes = sqlite3.connect(db).execute("select id from docs").fetchone()
    destino = base / "01_Expedientes/2022-perez-gomez/01_siged"
    shutil.move(str(a), str(destino / "renombrado.txt"))
    monkeypatch.setattr(B, "texto_de", lambda *a, **k: pytest.fail("no debe volver a extraer"))
    r = sync(base, db)
    assert (r["movidos"], r["nuevos"], r["borrados"]) == (1, 0, 0)
    assert sqlite3.connect(db).execute("select id from docs").fetchone() == id_antes
    res = buscar(db, "cedula")["resultados"]
    assert [(x["area"], x["caso"], x["tipo"]) for x in res] == [("01_Expedientes", "2022-perez-gomez", "01_siged")]


def test_archivo_borrado_sale_del_indice(base, db):
    a = escribir(base / "03_Doctrina/a.txt", "texto efimero")
    sync(base, db)
    a.unlink()
    assert sync(base, db)["borrados"] == 1
    assert buscar(db, "efimero")["total"] == 0


def test_archivo_modificado_se_reindexa(base, db):
    a = escribir(base / "03_Doctrina/a.txt", "version vieja")
    sync(base, db)
    a.write_text("version nueva con mas texto", encoding="utf-8")
    assert sync(base, db)["cambiados"] == 1
    assert buscar(db, "vieja")["total"] == 0 and buscar(db, "nueva")["total"] == 1


def test_un_pdf_roto_se_cuenta_pero_no_corta(base, db):
    (base / "03_Doctrina/roto.pdf").write_bytes(b"no soy pdf")
    escribir(base / "03_Doctrina/ok.txt", "texto sano")
    r = sync(base, db)
    assert r["nuevos"] == 2 and r["sin_texto"] == 1
    assert buscar(db, "sano")["total"] == 1
    assert buscar(db, "roto")["total"] == 1


def test_filtros_por_area_y_caso(base, db):
    escribir(base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt", "daño punitivo en el caso")
    escribir(base / "02_Jurisprudencia/consumo/dano-punitivo/f.txt", "daño punitivo fallo")
    sync(base, db)
    assert buscar(db, "punitivo")["total"] == 2
    assert buscar(db, "punitivo", area="02_Jurisprudencia")["total"] == 1
    assert buscar(db, "punitivo", caso="2022-perez-gomez")["total"] == 1


def test_limite_de_resultados_pero_total_completo(base, db):
    for i in range(5):
        escribir(base / f"03_Doctrina/{i}.txt", "mismo termino")
    sync(base, db)
    r = buscar(db, "termino", limite=2)
    assert len(r["resultados"]) == 2 and r["total"] == 5


def test_registrar_movimiento_actualiza_sin_recorrer(base, db):
    a = escribir(base / "_INBOX/sin_clasificar/a.txt", "oficio al registro")
    sync(base, db)
    destino = base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt"
    shutil.move(str(a), str(destino))
    assert B.registrar_movimiento(a, destino, base=base, db=db) is True
    assert [x["ruta"] for x in buscar(db, "oficio")["resultados"]] == [str(destino)]


def test_registrar_movimiento_fuera_del_alcance_lo_saca_del_indice(base, db):
    a = escribir(base / "_INBOX/sin_clasificar/a.txt", "oficio al registro")
    sync(base, db)
    destino = base / "_CUARENTENA/a.txt"
    shutil.move(str(a), str(destino))
    B.registrar_movimiento(a, destino, base=base, db=db)
    assert buscar(db, "oficio")["total"] == 0


def test_registrar_movimiento_sin_indice_no_lo_crea(base, db):
    assert B.registrar_movimiento(base / "a.txt", base / "b.txt", base=base, db=db) is False
    assert not db.exists()


def test_mover_y_deshacer_de_la_ingesta_mantienen_el_indice(base, db, monkeypatch):
    import ingesta as I
    for nombre, valor in {"BASE": base, "SISTEMA": base / "_SISTEMA", "RUTEO_CSV": base / "_SISTEMA/ruteo.csv",
                          "TANDAS_CSV": base / "_SISTEMA/tandas.csv"}.items():
        monkeypatch.setattr(I, nombre, valor)
    monkeypatch.setattr(B, "BASE", base)
    monkeypatch.setattr(B, "BUSQUEDA_DB", db)
    a = escribir(base / "_INBOX/sin_clasificar/a.txt", "cedula de notificacion")
    sync(base, db)
    destino = base / "01_Expedientes/2022-perez-gomez/01_siged"
    dst = I.mover(a, destino, "prueba")
    assert [x["ruta"] for x in buscar(db, "cedula")["resultados"]] == [str(dst)]
    r = I.deshacer_indices([0], "prueba")
    assert r["restaurados"] == 1
    assert [x["ruta"] for x in buscar(db, "cedula")["resultados"]] == [str(a)]


def test_estado_sin_indice_no_lo_crea(db):
    assert B.estado(db) == {"docs": 0, "areas": [], "actualizado": None}
    assert not db.exists()


def test_estado_con_indice(base, db):
    escribir(base / "03_Doctrina/a.txt", "algo")
    escribir(base / "_INBOX/sin_clasificar/b.txt", "otro")
    sync(base, db)
    e = B.estado(db)
    assert e["docs"] == 2 and e["areas"] == ["03_Doctrina", "_INBOX"] and e["actualizado"]


def test_ruta_indexada(base, db):
    a = escribir(base / "03_Doctrina/a.txt", "algo")
    sync(base, db)
    assert B.ruta_indexada(str(a), db) is True
    assert B.ruta_indexada(str(base / "03_Doctrina/otra.txt"), db) is False
    assert B.ruta_indexada(str(base / "03_Doctrina" / ".." / "03_Doctrina" / "a.txt"), db) is False


def _salida_ocr(ocr: Path, archivo: Path, texto: str, con_pdf: bool = True) -> Path:
    carpeta = ocr / O.hash_archivo(archivo)
    carpeta.mkdir(parents=True)
    if con_pdf:
        shutil.copy(archivo, carpeta / O.PDF_BUSCABLE)
    (carpeta / O.TXT).write_text(texto, encoding="utf-8")
    return carpeta


def test_pdf_mixto_indexa_la_caratula_y_el_ocr_y_no_se_relee(base, db, tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    monkeypatch.setattr(O, "OCR_DIR", ocr)
    mixto = pdf(base / "03_Doctrina/mixto.pdf", "Caratula PEREZ c/ GOMEZ " * 10)
    _salida_ocr(ocr, mixto, "el poseedor de buena fe")
    sync(base, db)
    assert buscar(db, "poseedor")["total"] == 1 and buscar(db, "caratula")["total"] == 1
    assert sqlite3.connect(db).execute("select fuente from docs").fetchone() == ("ocr",)
    monkeypatch.setattr(B, "texto_de", lambda *a, **k: pytest.fail("no debe volver a extraer"))
    assert sync(base, db)["cambiados"] == 0


def test_ocr_vacio_no_cuenta_ni_hace_releer_en_cada_pasada(base, db, tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    monkeypatch.setattr(O, "OCR_DIR", ocr)
    d = fitz.open()
    d.new_page()
    d.save(str(base / "03_Doctrina/esc.pdf"))
    _salida_ocr(ocr, base / "03_Doctrina/esc.pdf", "  \n\f\n ")
    assert sync(base, db)["sin_texto"] == 1
    assert sync(base, db)["cambiados"] == 0


def test_el_ocr_se_lee_aunque_falte_el_pdf_buscable(base, db, tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    monkeypatch.setattr(O, "OCR_DIR", ocr)
    d = fitz.open()
    d.new_page()
    d.save(str(base / "03_Doctrina/esc.pdf"))
    _salida_ocr(ocr, base / "03_Doctrina/esc.pdf", "poseedor de buena fe", con_pdf=False)
    sync(base, db)
    assert buscar(db, "poseedor")["total"] == 1


def test_registrar_movimiento_indexa_un_archivo_que_llego_despues_de_la_ultima_pasada(base, db):
    escribir(base / "03_Doctrina/viejo.txt", "algo")
    sync(base, db)
    a = escribir(base / "_INBOX/nuevo.txt", "embargo preventivo")
    destino = base / "01_Expedientes/2022-perez-gomez/01_siged/nuevo.txt"
    shutil.move(str(a), str(destino))
    assert B.registrar_movimiento(a, destino, base=base, db=db) is True
    assert [x["ruta"] for x in buscar(db, "embargo")["resultados"]] == [str(destino)]
    assert sync(base, db) == {"nuevos": 0, "movidos": 0, "cambiados": 0, "borrados": 0, "sin_texto": 0, "errores": 0}


def test_registrar_movimiento_de_un_archivo_no_indexado_fuera_del_alcance_no_hace_nada(base, db):
    escribir(base / "03_Doctrina/viejo.txt", "algo")
    sync(base, db)
    a = escribir(base / "_INBOX/nuevo.txt", "embargo preventivo")
    destino = base / "_CUARENTENA/nuevo.txt"
    shutil.move(str(a), str(destino))
    assert B.registrar_movimiento(a, destino, base=base, db=db) is False
    assert buscar(db, "embargo")["total"] == 0


def test_html_rtf_y_odt_se_indexan(base, db):
    import zipfile
    escribir(base / "05_Modelos_y_Escritos/a.html", "<html><body><p>contrato de mutuo</p></body></html>")
    (base / "05_Modelos_y_Escritos/b.rtf").write_bytes(
        rb"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 Se\'f1or Juez: medida cautelar\par}")
    with zipfile.ZipFile(base / "05_Modelos_y_Escritos/c.odt", "w") as z:
        z.writestr("content.xml", '<office:document-content><text:p>pagare a la vista</text:p></office:document-content>')
    sync(base, db)
    assert buscar(db, "mutuo")["total"] == 1
    assert buscar(db, "cautelar")["total"] == 1 and buscar(db, "señor")["total"] == 1
    assert buscar(db, "arial")["total"] == 0
    assert buscar(db, "pagare")["total"] == 1


def test_lo_que_quedo_sin_texto_se_relee_una_vez_al_cambiar_la_lectura(base, db, monkeypatch):
    escribir(base / "03_Doctrina/a.txt", "   ")
    assert sync(base, db)["sin_texto"] == 1
    assert sync(base, db)["cambiados"] == 0
    monkeypatch.setattr(B, "VERSION_LECTURA", "99")
    assert sync(base, db)["cambiados"] == 1
    assert sync(base, db)["cambiados"] == 0


def test_buscar_sin_indice_no_crea_nada_y_lo_dice(db):
    r = B.buscar("honorarios", db=db)
    assert r["total"] == 0 and r["sin_indice"] is True
    assert not db.exists() and not db.parent.joinpath(db.name + "-wal").exists()


def test_leer_mientras_otro_proceso_actualiza(base, db):
    import threading
    escribir(base / "03_Doctrina/a.txt", "usucapion veinteanal")
    sync(base, db)
    corre = True

    def escritor():
        con = sqlite3.connect(db)
        i = 0
        while corre:
            con.execute("INSERT OR REPLACE INTO meta (clave, valor) VALUES ('x', ?)", (str(i),))
            con.commit()
            i += 1
        con.close()
    h = threading.Thread(target=escritor)
    h.start()
    try:
        for _ in range(50):
            assert buscar(db, "usucapion")["total"] == 1
    finally:
        corre = False
        h.join()


def test_leer_sin_shm_funciona_y_no_cambia_el_contenido(base, db):
    escribir(base / "03_Doctrina/a.txt", "usucapion veinteanal")
    sync(base, db)
    for sufijo in ("-wal", "-shm"):
        Path(str(db) + sufijo).unlink(missing_ok=True)
    antes = db.read_bytes()
    assert buscar(db, "usucapion")["total"] == 1 and B.estado(db)["docs"] == 1
    assert db.read_bytes() == antes


def test_raiz_que_falta_no_borra_sus_entradas(base, db):
    escribir(base / "01_Expedientes" / "2022-perez-gomez" / "a.txt", "prescripcion adquisitiva")
    escribir(base / "03_Doctrina" / "b.txt", "dano punitivo")
    sync(base, db)
    (base / "03_Doctrina" / "b.txt").unlink()
    (base / "03_Doctrina").rmdir()
    r = sync(base, db)
    assert r["borrados"] == 0 and B.estado(db)["docs"] == 2


def test_base_desconectada_no_toca_el_indice(base, db, tmp_path):
    escribir(base / "01_Expedientes" / "2022-perez-gomez" / "a.txt", "prescripcion adquisitiva")
    sync(base, db)
    with pytest.raises(OSError, match="no esta disponible"):
        B.sincronizar(base=tmp_path / "no-existe", db=db)
    assert B.estado(db)["docs"] == 1


def test_indice_danado_se_informa_y_se_rehace(base, db):
    escribir(base / "01_Expedientes" / "2022-perez-gomez" / "a.txt", "prescripcion adquisitiva")
    db.write_bytes(b"esto no es una base sqlite" * 100)
    assert B.estado(db).get("danado") and B.buscar("prescripcion", db=db).get("danado")
    r = sync(base, db)
    assert r["nuevos"] == 1 and B.buscar("prescripcion", db=db)["total"] == 1
    assert list(db.parent.glob(db.name + ".danado-*"))


def test_pdf_mixto_con_caratula_corta_conserva_el_texto_nativo(base, db, tmp_path, monkeypatch):
    ocr = tmp_path / "ocr"
    monkeypatch.setattr(O, "OCR_DIR", ocr)
    mixto = pdf(base / "03_Doctrina/corto.pdf", "Caratula embargada")
    _salida_ocr(ocr, mixto, "el poseedor de buena fe")
    sync(base, db)
    assert buscar(db, "embargada")["total"] == 1 and buscar(db, "poseedor")["total"] == 1
