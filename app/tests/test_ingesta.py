import csv
import json
import os
import stat
import sys

import fitz
import pytest

import ingesta as I
from cartera import Causa, guardar, normalizar_expte

CUERPO = "Providencia del juzgado. Se tiene presente lo manifestado por la parte y se ordena el traslado. " * 2
FIRMA = " Tomo 12 Folio 34 del Colegio de Abogados."
NOTA_DOCTRINAL = (
    "Nota a fallo. El dano punitivo del art. 52 bis de la ley 24.240 y la multa civil en la relacion de "
    "consumo. Segun la doctrina y la jurisprudencia del tribunal, el derecho del consumidor exige "
    "responsabilidad del proveedor. " * 6
)


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    base = tmp_path / "SIGED"
    for d in ("01_Expedientes/2022-perez-gomez", "01_Expedientes/2023-vidal-luna", "_INBOX", "_SISTEMA"):
        (base / d).mkdir(parents=True)
    for nombre, valor in {
        "BASE": base, "INBOX": base / "_INBOX", "CASOS": base / "01_Expedientes", "SISTEMA": base / "_SISTEMA",
        "RUTEO_CSV": base / "_SISTEMA" / "INGESTA_RUTEO.csv", "TANDAS_CSV": base / "_SISTEMA" / "TANDAS.csv", "CARTERA_JSON": base / "_SISTEMA" / "cartera.json",
        "ALIAS_CARTERA": base / "_SISTEMA" / "aliases_cartera.json", "ALIAS_FILE": tmp_path / "sin_aliases.json",
    }.items():
        monkeypatch.setattr(I, nombre, valor)
    monkeypatch.setattr(I, "_exptes_cache", None)
    monkeypatch.setattr(I, "_indice_cache", (-1.0, I.IndiceCartera([])))
    monkeypatch.setattr(I, "_alias_cache", ((-1.0, -1.0), []))
    return base


def cartera(base, *causas):
    guardar(list(causas), base / "_SISTEMA" / "cartera.json")


def causa(numero, carpeta, id_expediente=None):
    return Causa(normalizar_expte(numero), id_expediente=id_expediente, carpeta=carpeta)


def escribir(tmp_path, nombre, texto):
    p = tmp_path / nombre
    if p.suffix == ".pdf":
        doc = fitz.open()
        doc.new_page().insert_textbox(fitz.Rect(36, 36, 560, 800), texto, fontsize=9)
        doc.save(str(p))
    else:
        p.write_text(texto, encoding="utf-8")
    return p


def test_id_de_siged_en_el_nombre_manda_a_la_causa(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez", "555555"))
    d = I.destino_de(escribir(tmp_path, "999999-2020__user0000-id555555.txt", CUERPO))
    assert d.carpeta == entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged"
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "id", "alta")


def test_numero_en_el_nombre_y_escrito_firmado(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    d = I.destino_de(escribir(tmp_path, "escrito 111111-2022 final.txt", CUERPO + FIRMA))
    assert d.carpeta == entorno / "01_Expedientes" / "2022-perez-gomez" / "00_escritos_propios"
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_nombre", "alta")


def test_numero_pegado_al_anio_como_en_los_recibos(entorno, tmp_path):
    cartera(entorno, causa("77123/2023", "2023-vidal-luna"))
    d = I.destino_de(escribir(tmp_path, "PLAZO_FIJO_EXPTE_771232023_BIS_12.txt", CUERPO))
    assert d.carpeta.parent.name == "2023-vidal-luna"


def test_fecha_de_guardado_no_es_numero_de_expediente(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    d = I.destino_de(escribir(tmp_path, "111111-2022-05-08_10-00-00.txt", CUERPO))
    assert d.motivo != "cartera"


@pytest.mark.parametrize("nombre", ["escrito 15-03-2024.txt", "notif 01_12_2024.txt", "cedula 1.3.2024.txt",
                                    "cuit 20-30123456-3 2024.txt"])
def test_fechas_dd_mm_aaaa_y_cuit_no_son_numero_de_expediente(entorno, tmp_path, nombre):
    cartera(entorno, causa("3/2024", "2022-perez-gomez"), causa("12/2024", "2023-vidal-luna"))
    d = I.destino_de(escribir(tmp_path, nombre, CUERPO))
    assert d.motivo != "cartera", d


def test_bis_sin_numero_no_va_con_confianza_alta_a_la_principal(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    d = I.destino_de(escribir(tmp_path, "111111-2022-bis.txt", CUERPO))
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_nombre_bis", "media")
    d = I.destino_de(escribir(tmp_path, "escrito 111111-2022 bisagra.txt", CUERPO))
    assert d.confianza == "alta"


def test_bis_con_carpeta_propia_gana_al_principal(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"), causa("111111/2022 bis1/23", "2023-vidal-luna"))
    a = I.destino_de(escribir(tmp_path, "111111-2022__id1.txt", CUERPO))
    assert a.carpeta.parent.name == "2022-perez-gomez"
    b = I.destino_de(escribir(tmp_path, "111111-2022 bis1-23 cedula.txt", CUERPO))
    assert b.carpeta.parent.name == "2023-vidal-luna"


def test_numero_en_el_texto_de_la_cartera_tiene_confianza_media(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    d = I.destino_de(escribir(tmp_path, "cedula.txt", "Expte. N 111111/2022. " + CUERPO))
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_texto", "media")


def test_alias_ignora_tildes_del_nombre(entorno, tmp_path):
    (entorno / "_SISTEMA" / "aliases_cartera.json").write_text(
        json.dumps({"aliases": [{"pattern": r"(?<![a-z])vidal(?![a-z])", "caso": "2023-vidal-luna"}]}), encoding="utf-8")
    assert I._match_alias("Escrito de Vidál") == "2023-vidal-luna"


def test_nota_doctrinal_va_a_su_rama_y_no_a_jurisprudencia(entorno, tmp_path):
    d = I.destino_de(escribir(tmp_path, "nota-punitivo.md", NOTA_DOCTRINAL))
    assert d.carpeta.parent == entorno / "03_Doctrina" / "consumo"
    assert d.motivo == "doctrina" and d.detalle.startswith("nota |")
    assert d.confianza in {"alta", "media"}


def test_pdf_que_no_es_doctrina_no_ensucia_03_doctrina(entorno, tmp_path):
    pdf = tmp_path / "manual.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Manual de servicio de la moto. Cambio de aceite y bujias. " * 6)
    doc.save(str(pdf))
    d = I.destino_de(pdf)
    assert (d.motivo, d.confianza) == ("sin_clasificar", "baja")
    assert d.carpeta == entorno / "_INBOX" / "sin_clasificar"


def test_confianza_de_los_motivos_existentes(entorno, tmp_path):
    assert I.destino_de(tmp_path / "x.exe").confianza == "baja"
    assert I.destino_de(escribir(tmp_path, "vacio.txt", "corto")).confianza == "baja"


def correr(monkeypatch, *argumentos):
    monkeypatch.setattr(sys, "argv", ["ingesta.py", *argumentos])
    I.main()


def test_main_mueve_registra_y_respeta_omitir(entorno, tmp_path, monkeypatch):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    origen = tmp_path / "origen"
    origen.mkdir()
    escribir(origen, "cedula 111111-2022.pdf", CUERPO)
    escribir(origen, "nota.md", NOTA_DOCTRINAL)
    (origen / "logo.svg").write_text("<svg/>")

    correr(monkeypatch, "--src", str(origen), "--dry-run", "--csv", str(tmp_path / "p.csv"))
    filas = list(csv.DictReader((tmp_path / "p.csv").open(encoding="utf-8-sig")))
    assert {f["motivo"]: f["accion"] for f in filas} == {"cartera": "mover", "doctrina": "mover", "formato_desconocido": "queda"}
    assert (origen / "nota.md").exists()

    correr(monkeypatch, "--src", str(origen), "--omitir", "doctrina")
    assert (entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged" / "cedula 111111-2022.pdf").exists()
    assert (origen / "nota.md").exists() and (origen / "logo.svg").exists()
    assert len(I.RUTEO_CSV.read_text(encoding="utf-8").splitlines()) == 2


SENTENCIA_PUNITIVO = (
    "VISTOS Y CONSIDERANDO: el dano punitivo del art. 52 bis de la ley 24.240 y la multa civil en la relacion "
    "de consumo. Segun la doctrina y la jurisprudencia del tribunal, el derecho del consumidor exige "
    "responsabilidad del proveedor. RESUELVE: hacer lugar al reclamo. " * 6
)
ARTICULO_CSJN = (
    "El dano punitivo del art. 52 bis de la ley 24.240 y la multa civil en la relacion de consumo. "
    "Segun la doctrina, la Corte Suprema de Justicia de la Nacion y el derecho del consumidor exigen "
    "responsabilidad del proveedor. " * 6
)


def test_texto_con_forma_de_sentencia_nunca_sale_como_seguro(entorno, tmp_path):
    d = I.destino_de(escribir(tmp_path, "resolucion-punitivo.md", SENTENCIA_PUNITIVO))
    assert d.motivo in {"doctrina", "rama"}
    assert d.confianza != "alta"


def test_cartera_ignora_carpeta_que_no_existe_en_disco(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-fantasma-inexistente"))
    d = I.destino_de(escribir(tmp_path, "escrito 111111-2022 final.txt", CUERPO + FIRMA))
    assert d.motivo != "cartera"


def test_articulo_que_cita_a_la_corte_sin_marcas_de_sentencia_tiene_confianza_media(entorno, tmp_path):
    d = I.destino_de(escribir(tmp_path, "articulo-csjn.md", ARTICULO_CSJN))
    assert d.motivo == "jurisprudencia"
    assert d.confianza == "media"


def test_omitir_rechaza_motivos_desconocidos(entorno, tmp_path, monkeypatch):
    with pytest.raises(SystemExit):
        correr(monkeypatch, "--src", str(tmp_path), "--omitir", "expediente_nuevoo")


def test_omitir_acepta_todos_los_motivos_conocidos():
    assert {"cartera", "doctrina", "expediente_nuevo", "sin_clasificar"} <= set(I.MOTIVOS)


def test_cartera_json_corrupto_no_rompe_el_ruteo(entorno, tmp_path, capsys):
    (entorno / "_SISTEMA" / "cartera.json").write_text("{esto no es json", encoding="utf-8")
    d = I.destino_de(escribir(tmp_path, "escrito 111111-2022.txt", CUERPO))
    assert d.motivo != "cartera"
    assert "cartera" in capsys.readouterr().err.lower()


def test_aliases_corruptos_no_rompen_el_ruteo(entorno, tmp_path, capsys):
    (entorno / "_SISTEMA" / "aliases_cartera.json").write_text("[[[", encoding="utf-8")
    assert I._match_alias("Escrito de Vidal") is None
    assert "alias" in capsys.readouterr().err.lower()


def test_aliases_generados_corruptos_no_pierden_los_manuales(entorno, tmp_path, monkeypatch):
    manual = tmp_path / "aliases_manuales.json"
    manual.write_text(json.dumps({"aliases": [{"pattern": "vidal", "caso": "2023-vidal-luna"}]}), encoding="utf-8")
    monkeypatch.setattr(I, "ALIAS_FILE", manual)
    (entorno / "_SISTEMA" / "aliases_cartera.json").write_text("[[[", encoding="utf-8")
    assert I._match_alias("Escrito de Vidal") == "2023-vidal-luna"


def test_archivo_de_un_bis_va_a_su_subcarpeta(entorno, tmp_path):
    (entorno / "01_Expedientes" / "2022-perez-gomez" / "bis1-23").mkdir()
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"), causa("111111/2022 bis1/23", "2022-perez-gomez/bis1-23"))
    d = I.destino_de(escribir(tmp_path, "111111-2022 bis1-23 cedula.pdf", CUERPO))
    assert d.carpeta == entorno / "01_Expedientes" / "2022-perez-gomez" / "bis1-23" / "01_siged"
    p = I.destino_de(escribir(tmp_path, "111111-2022 providencia.pdf", CUERPO))
    assert p.carpeta == entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged"


def test_carpeta_prevista_se_usa_aunque_no_exista_todavia(entorno, tmp_path):
    guardar([Causa(normalizar_expte("111111/2024"), carpeta_prevista="2024-vidal-luna")], entorno / "_SISTEMA" / "cartera.json")
    d = I.destino_de(escribir(tmp_path, "111111-2024 cedula.pdf", CUERPO))
    assert d.carpeta == entorno / "01_Expedientes" / "2024-vidal-luna" / "01_siged"
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "numero_nombre", "alta")


def con_caratula(numero, caratula, carpeta):
    return Causa(normalizar_expte(numero), caratula=caratula, carpeta=carpeta)


def test_partes_unicas_se_reconocen_en_el_nombre_del_archivo(entorno, tmp_path):
    (entorno / "01_Expedientes" / "2021-vidal-juan-moreno").mkdir()
    cartera(entorno, con_caratula("222222/2021", "VIDAL JUAN C/ MORENO SA S/ Danos", "2021-vidal-juan-moreno"))
    d = I.destino_de(escribir(tmp_path, "Vidal Juan contra Moreno - cedula.pdf", CUERPO))
    assert d.carpeta == entorno / "01_Expedientes" / "2021-vidal-juan-moreno" / "01_siged"
    assert (d.motivo, d.detalle, d.confianza) == ("cartera", "partes", "media")


def test_apellido_compartido_por_dos_causas_no_decide(entorno, tmp_path):
    (entorno / "01_Expedientes" / "2021-vidal-juan-moreno").mkdir()
    (entorno / "01_Expedientes" / "2022-vidal-juan-lopez").mkdir()
    cartera(entorno,
            con_caratula("222222/2021", "VIDAL JUAN C/ MORENO SA S/ Danos", "2021-vidal-juan-moreno"),
            con_caratula("333333/2022", "VIDAL JUAN C/ LOPEZ SA S/ Danos", "2022-vidal-juan-lopez"))
    assert I.destino_de(escribir(tmp_path, "vidal escrito.txt", CUERPO)).motivo != "cartera"
    d = I.destino_de(escribir(tmp_path, "vidal lopez escrito.txt", CUERPO))
    assert d.carpeta.parent.name == "2022-vidal-juan-lopez"


def test_borradores_de_la_causa_van_a_escritos_propios(entorno, tmp_path):
    import docx
    cartera(entorno, causa("111111/2022", "2022-perez-gomez", "555555"))
    propios = entorno / "01_Expedientes" / "2022-perez-gomez" / "00_escritos_propios"
    siged = entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged"
    d = docx.Document()
    d.add_paragraph("Expte. N 111111/2022. " + CUERPO)
    d.save(str(tmp_path / "contestacion perez.docx"))
    assert I.destino_de(tmp_path / "contestacion perez.docx").carpeta == propios
    assert I.destino_de(escribir(tmp_path, "Demanda_Perez 111111-2022 v2.md", CUERPO)).carpeta == propios
    assert I.destino_de(escribir(tmp_path, "notas_caso_v3.txt", "Expte. N 111111/2022. " + CUERPO)).carpeta == propios
    assert I.destino_de(escribir(tmp_path, "111111-2022__user0000-id555555.md", CUERPO)).carpeta == siged
    assert I.destino_de(escribir(tmp_path, "expediente_111111_2022_limpio.txt", CUERPO)).carpeta == siged

def test_proyectos_de_codigo_y_carpetas_ignoradas_no_se_recorren(tmp_path):
    for rel in ("web/package.json", "web/src/app.js", "web/leeme.txt", "bot/.git/HEAD", "bot/main.py",
                "sin_clasificar/viejo.txt", "skills-main/a/doc.md", "mi-skill/SKILL.md", "mi-skill/ref.md", "cedula.pdf", "sub/escrito.docx"):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    assert sorted(p.relative_to(tmp_path).as_posix() for p in I.archivos_pendientes(tmp_path)) == [
        "cedula.pdf", "sub/escrito.docx"]


def test_aplicar_csv_mueve_solo_lo_aprobado_sin_reclasificar(entorno, tmp_path, monkeypatch, capsys):
    origen = tmp_path / "origen"
    origen.mkdir()
    a, b, c = (escribir(origen, n, CUERPO) for n in ("a.txt", "b.txt", "c.txt"))
    destino = entorno / "01_Expedientes" / "2023-vidal-luna" / "00_escritos_propios"
    filas = [["archivo", "destino", "motivo", "detalle", "confianza", "accion", "hash"],
             [a, destino, "cartera", "manual", "media", "mover", I.hash_archivo(a)],
             [b, destino, "cartera", "", "media", "queda", ""],
             [origen / "ya-no-esta.txt", destino, "cartera", "", "media", "mover", ""],
             [c, tmp_path / "fuera-de-la-base", "cartera", "", "media", "mover", ""]]
    csv_ok = tmp_path / "aprobado.csv"
    with csv_ok.open("w", newline="", encoding="cp1252") as f:
        csv.writer(f, delimiter=";").writerows(filas)  # como lo guarda Excel en castellano

    correr(monkeypatch, "--aplicar-csv", str(csv_ok))

    assert (destino / "a.txt").exists() and not a.exists()
    assert b.exists() and c.exists()
    registro = list(csv.DictReader(I.RUTEO_CSV.open(encoding="utf-8")))
    assert [(r["motivo"], r["detalle"]) for r in registro] == [("cartera", "manual")]
    salida = capsys.readouterr().out
    assert "ya-no-esta.txt" in salida and "fuera-de-la-base" in salida


def test_causa_donde_el_actor_es_el_estudio_no_se_reconoce_por_nombre(entorno, tmp_path, monkeypatch):
    monkeypatch.setattr(I, "_PALABRAS_GENERICAS", I._PALABRAS_GENERICAS | {"robles", "marcos", "andres"})
    (entorno / "01_Expedientes" / "2020-robles-banco-macro").mkdir()
    cartera(entorno, con_caratula("444444/2020", "ROBLES MARCOS ANDRES C/ BANCO MACRO SA S/ Sumarisimos", "2020-robles-banco-macro"))
    assert I.destino_de(escribir(tmp_path, "macro banco resumen.txt", CUERPO)).motivo != "cartera"


def test_jurisprudencia_va_por_tema_y_el_tribunal_queda_en_el_detalle(entorno, tmp_path):
    d = I.destino_de(escribir(tmp_path, "articulo-csjn.md", ARTICULO_CSJN))
    assert d.carpeta.parent == entorno / "02_Jurisprudencia" / "consumo"
    assert d.detalle == "csjn"


def test_ninguna_ruta_de_jurisprudencia_queda_por_tribunal(entorno, tmp_path):
    textos = [ARTICULO_CSJN, SENTENCIA_PUNITIVO, "Superior Tribunal de Justicia de Misiones, Posadas. " + CUERPO * 3]
    for i, t in enumerate(textos):
        d = I.destino_de(escribir(tmp_path, f"f{i}.md", t))
        if (entorno / "02_Jurisprudencia") in d.carpeta.parents:
            assert d.carpeta.parent.parent == entorno / "02_Jurisprudencia"
            assert d.carpeta.parent.name not in {"misiones", "nacional"}


def test_aplicar_registra_la_tanda_y_deshacer_la_revierte(entorno, tmp_path):
    origen = tmp_path / "o"
    origen.mkdir()
    a, b = escribir(origen, "a.txt", CUERPO), escribir(origen, "b.txt", CUERPO)
    dst = entorno / "01_Expedientes" / "2023-vidal-luna" / "01_siged"
    r = I.aplicar_filas([{"archivo": str(a), "destino": str(dst), "accion": "mover", "hash": I.hash_archivo(a)},
                         {"archivo": str(b), "destino": str(dst), "accion": "mover", "hash": I.hash_archivo(b)}], "prueba.csv")
    assert (r["movidos"], r["tanda"]) == (2, "t0001")
    t = I.leer_tandas()[0]
    assert (t["origen"], t["primera_fila"], t["ultima_fila"]) == ("prueba.csv", "0", "1")

    escribir(origen, "b.txt", "otro")  # el origen de b quedo ocupado
    d = I.deshacer_filas(0, 1)
    assert d["restaurados"] == 1 and a.exists() and (dst / "b.txt").exists()
    assert any("ocupado" in x for x in d["avisos"])
    assert I.leer_ruteo()[-1]["motivo"] == "deshacer"
    assert I.leer_tandas()[-1]["origen"] == "deshacer:0-1"


def test_tandas_del_historial_viejo_se_derivan_por_pausas():
    filas = [{"fecha": f} for f in ("2026-09-26T10:00:00", "2026-09-26T10:00:30", "2026-09-26T11:00:00",
                                     "2026-09-26T11:00:10", "2026-09-26T12:00:00")]
    explicitas = [{"id": "t0001", "fecha": "x", "origen": "c.csv", "movidos": "1", "primera_fila": "4", "ultima_fila": "4"}]
    ids = [t["id"] for t in I.tandas_historicas(filas, explicitas)]
    assert ids == ["h0-1", "h2-3", "t0001"]


def test_pdf_escaneado_se_lee_por_ocr(tmp_path, monkeypatch):
    import extractor as X
    if not X.ocr_disponible():
        pytest.skip("Tesseract con espanol no instalado")
    monkeypatch.setattr(X, "OCR_CACHE", tmp_path / "cache")
    texto = fitz.open()
    texto.new_page().insert_textbox(fitz.Rect(40, 40, 570, 500), "PROVIDENCIA. Posadas, 3 de marzo. Agreguese la cedula acompanada y tengase presente. Notifiquese a las partes por el termino de ley.", fontsize=16)
    imagen = texto[0].get_pixmap(dpi=200)
    escaneado = fitz.open()
    escaneado.new_page().insert_image(escaneado[0].rect, pixmap=imagen)
    escaneado.save(str(tmp_path / "scan.pdf"))
    t, err = X.extraer_universal(tmp_path / "scan.pdf")
    assert err == "ocr" and "PROVIDENCIA" in t.upper()
    assert list((tmp_path / "cache").iterdir())


def test_ocr_ilegible_se_distingue_del_texto_real():
    import extractor as X
    assert X.calidad_texto("Posadas, 3 de marzo. Se tiene presente lo manifestado por la parte en autos.") > X.OCR_CALIDAD_MINIMA
    assert X.calidad_texto("AEAS Ae o NE eaos E a e o OeeassaoTEESEERAoa3% A AE e e o A O e et dead AU ula") < X.OCR_CALIDAD_MINIMA


def test_numero_ajeno_a_la_cartera_no_crea_carpeta(entorno, tmp_path):
    texto = "Expte. N 987654/2019. Autos: RUIZ JUAN c/ SOSA PEDRO s/ Danos. " + CUERPO + FIRMA
    d = I.destino_de(escribir(tmp_path, "escrito.md", texto))
    assert d.motivo != "expediente_nuevo"
    assert "987654" not in str(d.carpeta)


def test_aplicar_puede_renombrar_y_deshacer_restaura_el_nombre(entorno, tmp_path):
    origen = tmp_path / "o"
    origen.mkdir()
    a = escribir(origen, "fallo (12).pdf.txt", CUERPO)
    dst = entorno / "02_Jurisprudencia" / "consumo" / "dano-punitivo"
    r = I.aplicar_filas([{"archivo": str(a), "destino": str(dst), "accion": "mover",
                          "nombre_nuevo": "2023-03-12_STJ_perez-gomez", "hash": I.hash_archivo(a)}], "x")
    assert r["movidos"] == 1 and (dst / "2023-03-12_STJ_perez-gomez.txt").exists()
    I.deshacer_filas(0, 0)
    assert a.exists()


def _dos_fallos(tmp_path):
    origen = tmp_path / "origen"
    origen.mkdir()
    escribir(origen, "articulo-csjn.md", ARTICULO_CSJN)
    escribir(origen, "articulo-csjn-2.md", ARTICULO_CSJN)
    return origen


def test_main_publica_y_reindexa_una_vez(entorno, tmp_path, monkeypatch):
    origen = _dos_fallos(tmp_path)
    llamadas, rebuilds = [], []
    monkeypatch.setattr(I.kb_publicar, "publicar", lambda p, m, d, *a: llamadas.append((p, m)) or p)
    monkeypatch.setattr(I.kb_index, "rebuild", lambda *a: rebuilds.append(a))
    correr(monkeypatch, "--src", str(origen), "--incluir-no-clasificados")
    assert len(llamadas) == 2 and all(p.exists() for p, _ in llamadas)
    assert len(rebuilds) == 1


def test_main_dry_run_no_publica(entorno, tmp_path, monkeypatch):
    origen = _dos_fallos(tmp_path)
    llamadas, rebuilds = [], []
    monkeypatch.setattr(I.kb_publicar, "publicar", lambda *a: llamadas.append(a))
    monkeypatch.setattr(I.kb_index, "rebuild", lambda *a: rebuilds.append(a))
    correr(monkeypatch, "--src", str(origen), "--dry-run")
    assert not llamadas and not rebuilds


def test_aplicar_filas_publica_y_reindexa_una_vez(entorno, tmp_path, monkeypatch):
    a = escribir(tmp_path, "uno.txt", CUERPO)
    b = escribir(tmp_path, "dos.txt", CUERPO)
    dst = entorno / "dest"
    rebuilds = []
    monkeypatch.setattr(I.kb_publicar, "publicar", lambda p, m, d, *x: p)
    monkeypatch.setattr(I.kb_index, "rebuild", lambda *x: rebuilds.append(x))
    filas = [{"archivo": str(f), "destino": str(dst), "accion": "mover", "motivo": "jurisprudencia",
              "hash": I.hash_archivo(f)} for f in (a, b)]
    r = I.aplicar_filas(filas, "t")
    assert r["publicados"] == 2 and len(rebuilds) == 1


def test_error_de_publicar_o_rebuild_no_corta_la_ingesta(entorno, tmp_path, monkeypatch):
    origen = _dos_fallos(tmp_path)

    def falla(*a):
        raise RuntimeError("boom")

    monkeypatch.setattr(I.kb_publicar, "publicar", falla)
    monkeypatch.setattr(I.kb_index, "rebuild", falla)
    correr(monkeypatch, "--src", str(origen), "--incluir-no-clasificados")
    assert not list(origen.iterdir())
    monkeypatch.setattr(I.kb_publicar, "publicar", lambda p, *a: p)
    origen2 = _dos_fallos(tmp_path / "x") if (tmp_path / "x").mkdir() is None else None
    correr(monkeypatch, "--src", str(origen2), "--incluir-no-clasificados")
    assert not list(origen2.iterdir())


def test_asegurar_estructura_crea_las_carpetas_de_una_base_vacia(tmp_path, monkeypatch):
    b = tmp_path / "base"
    for nombre, valor in {"INBOX": b / "_INBOX", "CASOS": b / "01_Expedientes", "SISTEMA": b / "_SISTEMA"}.items():
        monkeypatch.setattr(I, nombre, valor)
    I.asegurar_estructura()
    assert all((b / d).is_dir() for d in ("_INBOX", "01_Expedientes", "_SISTEMA"))


def test_alias_que_apunta_fuera_de_la_base_se_rechaza(entorno, tmp_path):
    (tmp_path / "fuera").mkdir()
    assert (entorno / "01_Expedientes" / ".." / ".." / "fuera").resolve() == (tmp_path / "fuera").resolve()
    err = I.validar_alias("secreto", "../../fuera")
    assert err is not None and "fuera" in err
    assert I.validar_alias("perez", "2022-perez-gomez") is None


def test_mover_no_sale_de_la_base(entorno, tmp_path):
    origen = escribir(tmp_path, "doc.txt", CUERPO)
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    with pytest.raises(OSError, match="fuera de la base"):
        I.mover(origen, fuera, "prueba")
    assert origen.exists() and not list(fuera.iterdir())


def test_si_falla_el_registro_el_archivo_vuelve_al_origen(entorno, tmp_path, monkeypatch):
    origen = escribir(tmp_path, "doc.txt", CUERPO)
    destino = entorno / "01_Expedientes" / "2022-perez-gomez"

    def sin_disco(*a, **k):
        raise OSError("disco lleno")

    monkeypatch.setattr(I, "_anotar", sin_disco)
    with pytest.raises(OSError, match="disco lleno"):
        I.mover(origen, destino, "prueba")
    assert origen.exists() and not (destino / "doc.txt").exists()


def test_si_falla_el_registro_al_deshacer_el_archivo_no_queda_sin_historial(entorno, tmp_path, monkeypatch):
    origen = escribir(tmp_path, "doc.txt", CUERPO)
    destino = entorno / "01_Expedientes" / "2022-perez-gomez"
    movido = I.mover(origen, destino, "prueba")
    assert movido.exists() and not origen.exists()

    def sin_disco(*a, **k):
        raise OSError("sin permisos")

    monkeypatch.setattr(I, "_anotar", sin_disco)
    r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 0 and any("queda donde estaba" in a for a in r["avisos"])
    assert movido.exists() and not origen.exists()


def test_un_enlace_simbolico_hacia_afuera_no_permite_salir_de_la_base(entorno, tmp_path):
    fuera = tmp_path / "fuera"
    fuera.mkdir()
    enlace = entorno / "01_Expedientes" / "2024-enlace"
    try:
        enlace.symlink_to(fuera, target_is_directory=True)
    except OSError:
        pytest.skip("este sistema no permite crear enlaces simbolicos sin permisos")
    err = I.validar_alias("secreto", "2024-enlace")
    assert err is not None and "fuera" in err
    origen = escribir(tmp_path, "doc.txt", CUERPO)
    with pytest.raises(OSError, match="fuera de la base"):
        I.mover(origen, enlace, "prueba")
    assert origen.exists() and not list(fuera.iterdir())


@pytest.mark.parametrize("caso", ["../_SISTEMA", "..", ".", "absoluta"])
def test_alias_cargado_desde_archivo_no_sale_de_expedientes(entorno, caso):
    if caso == "absoluta":
        caso = str(entorno / "_SISTEMA")
    (entorno / "_SISTEMA" / "aliases_cartera.json").write_text(
        json.dumps({"aliases": [{"pattern": "vidal", "caso": caso}]}), encoding="utf-8")
    assert I._match_alias("escrito vidal") is None


def test_aplicar_respalda_el_historial_antes_de_mover(entorno, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("x", encoding="utf-8")
    I.mover(a, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    antes = I.RUTEO_CSV.read_text(encoding="utf-8")
    b = tmp_path / "b.txt"
    b.write_text("y", encoding="utf-8")
    I.aplicar_filas([{"archivo": str(b), "destino": str(entorno / "01_Expedientes" / "2022-perez-gomez"),
                      "accion": "mover", "hash": I.hash_archivo(b)}], "t")
    (copia,) = list((entorno / "_SISTEMA" / "_respaldos").glob("INGESTA_RUTEO.csv.*.bak"))
    assert copia.read_text(encoding="utf-8") == antes


@pytest.mark.skipif(os.name != "nt", reason="el bloqueo por archivo abierto es de Windows")
def test_mover_un_archivo_abierto_falla_sin_dejar_copia(entorno):
    src = entorno / "_INBOX" / "abierto.txt"
    src.write_text("x", encoding="utf-8")
    destino = entorno / "01_Expedientes/2022-perez-gomez"
    with src.open("rb"):
        with pytest.raises(OSError, match="abierto"):
            I.mover(src, destino, "prueba")
    assert src.is_file() and not (destino / "abierto.txt").exists()
    assert I.leer_ruteo() == []


def test_mover_nunca_pisa_un_destino_que_aparecio_entre_medio(entorno, monkeypatch):
    src = entorno / "_INBOX" / "a.txt"
    src.write_text("nuevo", encoding="utf-8")
    ocupado = entorno / "01_Expedientes/2022-perez-gomez/a.txt"
    ocupado.write_text("viejo", encoding="utf-8")
    monkeypatch.setattr(I, "destino_unico", lambda d: d)
    with pytest.raises(OSError, match="ya existe"):
        I.mover(src, ocupado.parent, "prueba")
    assert ocupado.read_text(encoding="utf-8") == "viejo" and src.read_text(encoding="utf-8") == "nuevo"


def _filas_mover(base, nombres, destino):
    filas = []
    for n in nombres:
        f = base / "_INBOX" / n
        f.write_text(n, encoding="utf-8")
        filas.append({"archivo": str(f), "destino": str(destino), "motivo": "prueba", "detalle": "", "accion": "mover",
                      "hash": I.hash_archivo(f)})
    return filas


def test_tandas_simultaneas_no_se_mezclan_y_deshacer_revierte_solo_la_suya(entorno, monkeypatch):
    import threading
    import time
    original = I.mover

    def lento(*a, **k):
        time.sleep(0.05)
        return original(*a, **k)
    monkeypatch.setattr(I, "mover", lento)
    destino = entorno / "01_Expedientes/2022-perez-gomez"
    a = _filas_mover(entorno, [f"a{i}.txt" for i in range(4)], destino)
    b = _filas_mover(entorno, [f"b{i}.txt" for i in range(4)], destino)
    hilos = [threading.Thread(target=I.aplicar_filas, args=(f, n)) for f, n in ((a, "A"), (b, "B"))]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    tandas = {t["origen"]: t for t in I.leer_tandas()}
    rangos = [set(range(int(t["primera_fila"]), int(t["ultima_fila"]) + 1)) for t in tandas.values()]
    assert len(rangos) == 2 and not rangos[0] & rangos[1] and len(rangos[0] | rangos[1]) == 8
    t = tandas["A"]
    I.deshacer_filas(int(t["primera_fila"]), int(t["ultima_fila"]))
    assert sorted(p.name for p in (entorno / "_INBOX").iterdir()) == [f"a{i}.txt" for i in range(4)]


def test_una_fila_que_ya_estaba_en_su_carpeta_no_corre_el_rango_de_la_tanda(entorno):
    destino = entorno / "01_Expedientes/2022-perez-gomez"
    quieto = destino / "quieto.txt"
    quieto.write_text("x", encoding="utf-8")
    filas = [{"archivo": str(quieto), "destino": str(destino), "accion": "mover", "hash": I.hash_archivo(quieto)}] \
        + _filas_mover(entorno, ["m.txt"], destino)
    r = I.aplicar_filas(filas, "C")
    assert r["movidos"] == 1
    t = I.leer_tandas()[0]
    assert (t["primera_fila"], t["ultima_fila"]) == ("0", "0") and len(I.leer_ruteo()) == 1


def test_con_el_registro_tomado_aplicar_avisa_y_no_mueve(entorno, monkeypatch):
    monkeypatch.setattr(I, "ESPERA_BLOQUEO", 0.3)
    filas = _filas_mover(entorno, ["x.txt"], entorno / "01_Expedientes/2022-perez-gomez")
    with I._bloqueo():
        with pytest.raises(I.TandaEnCurso, match="otra tanda.*en curso"):
            I.aplicar_filas(filas, "D")
    assert (entorno / "_INBOX/x.txt").is_file() and I.leer_ruteo() == []


def test_deshacer_saltea_un_archivo_que_cambio_desde_que_se_movio(entorno):
    destino = entorno / "01_Expedientes/2022-perez-gomez"
    I.aplicar_filas(_filas_mover(entorno, ["a.txt", "b.txt"], destino), "E")
    (destino / "a.txt").write_text("editado despues", encoding="utf-8")
    r = I.deshacer_filas(0, 1)
    assert r["restaurados"] == 1 and any("cambio desde que se movio" in a for a in r["avisos"])
    assert (destino / "a.txt").is_file() and (entorno / "_INBOX/b.txt").is_file()


def test_un_registro_viejo_suma_las_columnas_y_sus_filas_se_siguen_deshaciendo(entorno):
    destino = entorno / "01_Expedientes/2022-perez-gomez"
    viejo = destino / "v.txt"
    viejo.write_text("v", encoding="utf-8")
    I.RUTEO_CSV.write_text("fecha,origen,destino,motivo,detalle\n"
                           f"2026-01-01T00:00:00,{entorno / '_INBOX' / 'v.txt'},{viejo},prueba,\n", encoding="utf-8")
    I.aplicar_filas(_filas_mover(entorno, ["n.txt"], destino), "F")
    filas = I.leer_ruteo()
    assert list(filas[0]) == I.COLUMNAS_RUTEO and filas[0]["tamano"] == "" and filas[1]["tamano"] == "5"
    assert I.deshacer_filas(0, 0)["restaurados"] == 1


def _publicacion_falsa(monkeypatch, corpus):
    def publicar(archivo, motivo, detalle):
        md = corpus / f"{archivo.stem}.md"
        md.parent.mkdir(parents=True, exist_ok=True)
        md.write_text("# fallo", encoding="utf-8")
        return md
    monkeypatch.setattr(I.kb_publicar, "publicar", publicar)
    reindexados = []
    monkeypatch.setattr(I, "_reindexar", lambda: reindexados.append(1))
    return reindexados


def test_deshacer_quita_del_corpus_lo_que_publico_la_tanda(entorno, tmp_path, monkeypatch):
    reindexados = _publicacion_falsa(monkeypatch, tmp_path / "corpus")
    destino = entorno / "01_Expedientes/2022-perez-gomez"
    I.aplicar_filas(_filas_mover(entorno, ["f1.txt", "f2.txt"], destino), "G")
    assert sorted(p.name for p in (tmp_path / "corpus").iterdir()) == ["f1.md", "f2.md"]
    (tmp_path / "corpus/f2.md").write_text("# fallo con notas mias", encoding="utf-8")
    r = I.deshacer_filas(0, 1)
    assert r["despublicados"] == 1 and any("version editada" in a for a in r["avisos"])
    assert [p.name for p in (tmp_path / "corpus").iterdir()] == ["f2.md"]
    assert len(reindexados) == 2


@pytest.mark.parametrize("modulo", ["fase2_expedientes", "fase3_juris"])
def test_los_scripts_historicos_no_corren_sin_el_flag(modulo, monkeypatch):
    import importlib
    m = importlib.import_module(modulo)
    monkeypatch.setattr(sys, "argv", [f"{modulo}.py"])
    with pytest.raises(SystemExit) as exc:
        m.main()
    assert "--ejecutar-historico" in str(exc.value)

def _rename_sin_cruzar_unidad(monkeypatch):
    """Simula un pendrive: os.rename falla si origen o destino estan en el y el otro no."""
    real = os.rename
    cruces = []

    def rename(a, b):
        if ("pendrive" in str(a)) != ("pendrive" in str(b)):
            cruces.append((str(a), str(b)))
            raise OSError(18, "Invalid cross-device link")
        return real(a, b)
    monkeypatch.setattr(I.os, "rename", rename)
    return cruces


def _en_pendrive(tmp_path, texto="contenido"):
    origen = tmp_path / "pendrive" / "escrito.txt"
    origen.parent.mkdir(exist_ok=True)
    origen.write_text(texto, encoding="utf-8")
    return origen


def test_mover_desde_otra_unidad_copia_y_borra_el_origen(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = tmp_path / "pendrive" / "escrito.txt"
    origen.parent.mkdir()
    origen.write_text("contenido", encoding="utf-8")
    dst = I.mover(origen, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    assert not origen.exists() and dst.read_text(encoding="utf-8") == "contenido"
    assert I.leer_ruteo()[-1]["destino"] == str(dst)


def test_mover_desde_otra_unidad_no_pisa_ni_deja_copia_si_el_origen_esta_abierto(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = tmp_path / "pendrive" / "escrito.txt"
    origen.parent.mkdir()
    origen.write_text("contenido", encoding="utf-8")
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    real_unlink = I.Path.unlink

    def unlink(self, *a, **k):
        if self == origen:
            raise PermissionError("abierto")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(I.Path, "unlink", unlink)
    with pytest.raises(OSError, match="abierto"):
        I.mover(origen, carpeta, "prueba")
    assert origen.read_text(encoding="utf-8") == "contenido"
    assert not list(carpeta.iterdir())

    ocupado = tmp_path / "ocupado.txt"
    ocupado.write_text("previo", encoding="utf-8")
    with pytest.raises(OSError, match="ya existe"):
        I._mover_entre_unidades(origen, ocupado)
    assert ocupado.read_text(encoding="utf-8") == "previo"


def test_deshacer_despues_de_mover_desde_otra_unidad(entorno, tmp_path, monkeypatch):
    cruces = _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    dst = I.mover(origen, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    r = I.deshacer_indices([len(I.leer_ruteo()) - 1], "prueba")
    assert r["restaurados"] == 1 and origen.read_text(encoding="utf-8") == "contenido" and not dst.exists()
    assert cruces[-1] == (str(dst), str(origen))  # la vuelta al pendrive tambien fue por copia verificada


def test_mover_desde_otra_unidad_descarta_una_copia_que_no_coincide(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    monkeypatch.setattr(I, "_sha256_archivo", lambda ruta: "0" * 64)
    with pytest.raises(OSError, match="no coincide"):
        I.mover(origen, carpeta, "prueba")
    assert origen.read_text(encoding="utf-8") == "contenido" and not list(carpeta.iterdir())


def test_mover_desde_otra_unidad_sigue_aunque_no_se_puedan_copiar_las_fechas(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)

    def utime(*a, **k):
        raise PermissionError("FAT no acepta la fecha")
    monkeypatch.setattr(I.os, "utime", utime)
    dst = I.mover(origen, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    assert not origen.exists() and dst.read_text(encoding="utf-8") == "contenido"


def test_mover_desde_otra_unidad_un_origen_de_solo_lectura(entorno, tmp_path, monkeypatch):
    cruces = _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    os.chmod(origen, stat.S_IREAD)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    dst = I.mover(origen, carpeta, "prueba")
    assert cruces and not origen.exists() and dst.read_text(encoding="utf-8") == "contenido"
    assert not os.access(dst, os.W_OK)  # como os.rename en la misma unidad, conserva el solo lectura
    assert [p.name for p in carpeta.iterdir()] == [dst.name]

    r = I.deshacer_indices([len(I.leer_ruteo()) - 1], "prueba")
    assert r["restaurados"] == 1 and origen.read_text(encoding="utf-8") == "contenido" and not dst.exists()
    os.chmod(origen, stat.S_IWRITE)


def test_solo_lectura_que_no_se_puede_borrar_no_deja_huerfanos(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    os.chmod(origen, stat.S_IREAD)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    real_unlink = I.Path.unlink

    def unlink(self, *a, **k):
        if self == origen:
            raise PermissionError("abierto")
        return real_unlink(self, *a, **k)
    monkeypatch.setattr(I.Path, "unlink", unlink)
    with pytest.raises(OSError, match="abierto en otro programa"):
        I.mover(origen, carpeta, "prueba")
    assert origen.read_text(encoding="utf-8") == "contenido" and not os.access(origen, os.W_OK)
    assert not list(carpeta.iterdir())
    os.chmod(origen, stat.S_IWRITE)


def test_si_el_origen_cambia_durante_la_copia_no_se_borra(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    real_fsync = I.os.fsync

    def fsync(fd):
        real_fsync(fd)
        with open(origen, "a", encoding="utf-8") as f:
            f.write(" y algo mas")
    monkeypatch.setattr(I.os, "fsync", fsync)
    with pytest.raises(OSError, match="cambio durante la copia"):
        I.mover(origen, carpeta, "prueba")
    assert origen.read_text(encoding="utf-8") == "contenido y algo mas" and not list(carpeta.iterdir())


def test_si_el_origen_se_borro_pese_al_error_queda_la_copia_verificada(tmp_path, monkeypatch):
    origen = _en_pendrive(tmp_path)
    dst = tmp_path / "base" / "escrito.txt"
    dst.parent.mkdir()
    real_unlink = I.Path.unlink

    def unlink(self, *a, **k):
        real_unlink(self, *a, **k)
        if self == origen:
            raise OSError(5, "error de E/S al confirmar")
    monkeypatch.setattr(I.Path, "unlink", unlink)
    I._mover_entre_unidades(origen, dst)
    assert not origen.exists() and dst.read_text(encoding="utf-8") == "contenido"
    assert [p.name for p in dst.parent.iterdir()] == ["escrito.txt"]


def test_si_el_origen_desaparece_durante_la_copia_queda_como_incompleto(tmp_path, monkeypatch):
    origen = _en_pendrive(tmp_path)
    dst = tmp_path / "base" / "escrito.txt"
    dst.parent.mkdir()

    def fsync(fd):
        raise OSError(5, "el pendrive se desconecto")
    real_existe = I._existe
    monkeypatch.setattr(I.os, "fsync", fsync)
    monkeypatch.setattr(I, "_existe", lambda r: False if r == origen else real_existe(r))
    with pytest.raises(OSError, match="copia sin verificar"):
        I._mover_entre_unidades(origen, dst)
    restos = list(dst.parent.iterdir())
    assert not dst.exists() and len(restos) == 1 and restos[0].name.endswith(".incompleto")  # la busqueda no lo indexa
    assert restos[0].read_text(encoding="utf-8") == "contenido"
    monkeypatch.undo()
    I._mover_entre_unidades(origen, dst)  # el resto de un intento anterior no bloquea el nombre
    assert dst.read_text(encoding="utf-8") == "contenido"


def test_si_no_se_sabe_si_el_origen_sigue_no_da_por_hecho_el_traslado(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    real_unlink = I.Path.unlink

    def unlink(self, *a, **k):
        if self == origen:
            raise OSError(1167, "el dispositivo no esta conectado")
        return real_unlink(self, *a, **k)
    real_existe = I._existe
    monkeypatch.setattr(I.Path, "unlink", unlink)
    monkeypatch.setattr(I, "_existe", lambda r: None if r == origen else real_existe(r))
    filas = len(I.leer_ruteo())
    with pytest.raises(OSError) as e:
        I.mover(origen, carpeta, "prueba")
    assert "no se pudo comprobar si el origen" in " ".join(getattr(e.value, "__notes__", []))
    assert len(I.leer_ruteo()) == filas  # no se registra un traslado que quizas no paso
    assert origen.exists() and (carpeta / "escrito.txt").read_text(encoding="utf-8") == "contenido"


def test_un_guardado_del_origen_justo_antes_de_borrarlo_se_detecta(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    rename = I.os.rename

    def rename_y_editar(a, b):
        rename(a, b)
        if str(a).endswith(".incompleto"):
            origen.write_text("version nueva", encoding="utf-8")
    monkeypatch.setattr(I.os, "rename", rename_y_editar)
    with pytest.raises(OSError, match="cambio durante la copia"):
        I.mover(origen, carpeta, "prueba")
    assert origen.read_text(encoding="utf-8") == "version nueva" and not list(carpeta.iterdir())


def test_no_pisa_un_destino_que_aparece_durante_la_copia(tmp_path, monkeypatch):
    origen = _en_pendrive(tmp_path)
    dst = tmp_path / "base" / "escrito.txt"
    dst.parent.mkdir()
    real_fsync = I.os.fsync

    def fsync(fd):
        real_fsync(fd)
        dst.write_text("de otro", encoding="utf-8")
    monkeypatch.setattr(I.os, "fsync", fsync)
    with pytest.raises(OSError, match="ya existe"):
        I._mover_entre_unidades(origen, dst)
    assert dst.read_text(encoding="utf-8") == "de otro" and origen.read_text(encoding="utf-8") == "contenido"
    assert [x.name for x in dst.parent.iterdir()] == ["escrito.txt"]


def test_conserva_la_fecha_del_origen(tmp_path):
    origen = _en_pendrive(tmp_path)
    os.utime(origen, ns=(1_600_000_000_000_000_000, 1_600_000_000_123_456_700))
    dst = tmp_path / "base" / "escrito.txt"
    dst.parent.mkdir()
    I._mover_entre_unidades(origen, dst)
    assert dst.stat().st_mtime_ns == 1_600_000_000_123_456_700


def test_un_ctrl_c_justo_despues_del_rename_no_deja_duplicado(tmp_path, monkeypatch):
    origen = _en_pendrive(tmp_path)
    dst = tmp_path / "base" / "escrito.txt"
    dst.parent.mkdir()
    rename = I.os.rename

    def rename_e_interrumpir(a, b):
        rename(a, b)
        raise KeyboardInterrupt
    monkeypatch.setattr(I.os, "rename", rename_e_interrumpir)
    with pytest.raises(KeyboardInterrupt):
        I._mover_entre_unidades(origen, dst)
    assert origen.read_text(encoding="utf-8") == "contenido" and not list(dst.parent.iterdir())


def test_un_ctrl_c_al_anotar_avisa_donde_quedo(entorno, tmp_path, monkeypatch):
    origen = _en_pendrive(tmp_path)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"

    def anotar(*a, **k):
        raise KeyboardInterrupt
    monkeypatch.setattr(I, "_anotar", anotar)
    with pytest.raises(KeyboardInterrupt) as e:
        I.mover(origen, carpeta, "prueba")
    assert "quedo sin registrar" in " ".join(e.value.__notes__)


def test_un_nombre_que_el_registro_no_puede_guardar_no_se_mueve(entorno, tmp_path):
    origen = tmp_path / ("raro" + chr(0xDC81) + ".txt")
    try:
        origen.write_text("x", encoding="utf-8")
    except (OSError, UnicodeEncodeError):
        pytest.skip("el sistema de archivos no acepta el nombre")
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    with pytest.raises(OSError, match="no puede guardar"):
        I.mover(origen, carpeta, "prueba")
    assert origen.exists() and not list(carpeta.iterdir())


def test_deshacer_que_no_puede_anotar_conserva_la_fecha_para_reintentar(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    dst = I.mover(origen, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    fila = len(I.leer_ruteo()) - 1
    anotar_real, utime_real = I._anotar, I.os.utime

    def anotar(*a, **k):
        raise OSError("ruteo abierto en Excel")

    def utime(ruta, *a, **k):
        if "pendrive" in str(ruta):
            raise PermissionError("FAT")
        return utime_real(ruta, *a, **k)
    monkeypatch.setattr(I, "_anotar", anotar)
    monkeypatch.setattr(I.os, "utime", utime)
    r = I.deshacer_indices([fila], "prueba")
    assert r["restaurados"] == 0 and dst.exists()
    monkeypatch.setattr(I, "_anotar", anotar_real)
    r = I.deshacer_indices([fila], "prueba")
    assert r["restaurados"] == 1 and origen.read_text(encoding="utf-8") == "contenido", r["avisos"]


def test_mover_avisa_si_no_puede_anotar_ni_devolver(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"

    def anotar(*a, **k):
        raise OSError("ruteo bloqueado")
    monkeypatch.setattr(I, "_anotar", anotar)
    real = I._mover_entre_unidades

    def mover_entre(src, dst):
        if "pendrive" in str(dst):
            raise OSError("el pendrive ya no esta")
        real(src, dst)
    monkeypatch.setattr(I, "_mover_entre_unidades", mover_entre)
    with pytest.raises(OSError, match="sin registrar"):
        I.mover(origen, carpeta, "prueba")
    assert (carpeta / "escrito.txt").read_text(encoding="utf-8") == "contenido"  # el unico ejemplar sigue ahi


def test_deshacer_avisa_si_no_puede_anotar_ni_devolver(entorno, tmp_path, monkeypatch):
    _rename_sin_cruzar_unidad(monkeypatch)
    origen = _en_pendrive(tmp_path)
    dst = I.mover(origen, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    fila = len(I.leer_ruteo()) - 1

    def anotar(*a, **k):
        raise OSError("ruteo bloqueado")
    monkeypatch.setattr(I, "_anotar", anotar)
    real = I._mover_entre_unidades

    def mover_entre(src, d):
        if "pendrive" in str(src):
            raise OSError("el pendrive ya no esta")
        real(src, d)
    monkeypatch.setattr(I, "_mover_entre_unidades", mover_entre)
    r = I.deshacer_indices([fila], "prueba")
    assert r["restaurados"] == 0 and any("sin registrar" in a for a in r["avisos"])
    assert origen.read_text(encoding="utf-8") == "contenido" and not dst.exists()


def _movido(entorno, tmp_path, nombre="a.txt"):
    a = tmp_path / nombre
    a.write_text("contenido " + nombre, encoding="utf-8")
    return a, I.mover(a, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")


def test_registro_guardado_por_excel_no_pierde_el_historial(entorno, tmp_path):
    a, dst = _movido(entorno, tmp_path)
    filas = I.leer_ruteo()
    texto = "\ufeff" + ";".join(I.COLUMNAS_RUTEO) + "\r\n" + "".join(
        ";".join(f[c] for c in I.COLUMNAS_RUTEO) + "\r\n" for f in filas)
    I.RUTEO_CSV.write_bytes(texto.encode("utf-8"))  # como lo guarda Excel: BOM, punto y coma, CRLF
    b, _ = _movido(entorno, tmp_path, "b.txt")
    filas = I.leer_ruteo()
    assert [f["origen"] for f in filas] == [str(a), str(b)]
    assert list((entorno / "_SISTEMA" / "_respaldos").glob("INGESTA_RUTEO.csv.*.bak"))
    assert I.deshacer_filas(0, 0)["restaurados"] == 1 and a.is_file() and not dst.exists()


def test_registro_en_cp1252_con_tildes_se_lee(entorno, tmp_path):
    a, _ = _movido(entorno, tmp_path, "cédula.txt")
    f = I.leer_ruteo()[0]
    I.RUTEO_CSV.write_bytes((";".join(I.COLUMNAS_RUTEO) + "\r\n" + ";".join(f[c] for c in I.COLUMNAS_RUTEO)
                             + "\r\n").encode("cp1252"))
    assert I.leer_ruteo()[0]["origen"] == str(a)
    _movido(entorno, tmp_path, "b.txt")
    assert len(I.leer_ruteo()) == 2


def test_registro_ilegible_no_se_reescribe_y_el_archivo_no_se_mueve(entorno, tmp_path):
    _movido(entorno, tmp_path)
    I.RUTEO_CSV.write_text("columna rara;otra\nx;y\n", encoding="utf-8")
    antes = I.RUTEO_CSV.read_bytes()
    b = tmp_path / "b.txt"
    b.write_text("y", encoding="utf-8")
    with pytest.raises(OSError, match="no se anota"):
        I.mover(b, entorno / "01_Expedientes" / "2022-perez-gomez", "prueba")
    assert b.is_file() and I.RUTEO_CSV.read_bytes() == antes


def test_deshacer_no_actua_si_el_registro_se_edito_desde_la_tanda(entorno, tmp_path):
    for n in ("a.txt", "b.txt"):
        x = tmp_path / n
        x.write_text(n, encoding="utf-8")
    I.aplicar_filas([{"archivo": str(tmp_path / "a.txt"), "destino": str(entorno / "01_Expedientes" / "2022-perez-gomez"),
                      "accion": "mover", "hash": I.hash_archivo(tmp_path / "a.txt")}], "t1")
    I.aplicar_filas([{"archivo": str(tmp_path / "b.txt"), "destino": str(entorno / "01_Expedientes" / "2023-vidal-luna"),
                      "accion": "mover", "hash": I.hash_archivo(tmp_path / "b.txt")}], "t2")
    filas = I.leer_ruteo()
    with I.RUTEO_CSV.open("w", newline="", encoding="utf-8") as f:  # se borro la primera fila en Excel
        w = csv.DictWriter(f, fieldnames=I.COLUMNAS_RUTEO)
        w.writeheader()
        w.writerows(filas[1:])
    r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 0 and "cambio desde la tanda" in r["avisos"][0]
    assert (entorno / "01_Expedientes" / "2023-vidal-luna" / "b.txt").is_file()


def test_reubicar_carpeta_mantiene_deshacer_de_tandas_viejas(entorno, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("x", encoding="utf-8")
    I.aplicar_filas([{"archivo": str(a), "destino": str(entorno / "01_Expedientes" / "2022-perez-gomez"),
                      "accion": "mover", "hash": I.hash_archivo(a)}], "t1")
    vieja, nueva = entorno / "01_Expedientes" / "2022-perez-gomez", entorno / "01_Expedientes" / "2022-perez-gomez-cobro"
    vieja.rename(nueva)
    assert I.reubicar_carpeta(vieja, nueva) == 1
    r = I.deshacer_filas(0, 0)
    assert r["restaurados"] == 1 and a.is_file()


def test_aplicar_con_destino_que_sale_de_la_base_no_corta_la_tanda(entorno, tmp_path):
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_text("x", encoding="utf-8")
    b.write_text("y", encoding="utf-8")
    r = I.aplicar_filas([
        {"archivo": str(a), "destino": str(entorno / "01_Expedientes" / "2022-perez-gomez"), "accion": "mover",
         "hash": I.hash_archivo(a)},
        {"archivo": str(b), "destino": str(entorno / "01_Expedientes" / ".." / ".." / "fuera"), "accion": "Mover",
         "hash": I.hash_archivo(b)}], "t")
    assert r["movidos"] == 1 and r["tanda"] and b.is_file()
    assert any("fuera" in x for x in r["avisos"])


def test_cli_mueve_con_bloqueo_y_deja_una_tanda_que_se_puede_deshacer(entorno, tmp_path, monkeypatch):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    a = escribir(entorno / "_INBOX", "escrito 111111-2022.txt", CUERPO + FIRMA)
    tomado = []
    real = I._bloqueo
    monkeypatch.setattr(I, "_bloqueo", lambda *x, **k: (tomado.append(1), real(*x, **k))[1])
    monkeypatch.setattr(sys, "argv", ["ingesta.py"])
    I.main()
    assert tomado and not a.exists()
    (t,) = I.leer_tandas()
    assert t["origen"].startswith("cli:")
    assert I.deshacer_filas(int(t["primera_fila"]), int(t["ultima_fila"]))["restaurados"] == 1 and a.is_file()


def test_aplicar_avisa_cuando_el_nombre_estaba_ocupado(entorno, tmp_path):
    carpeta = entorno / "01_Expedientes" / "2022-perez-gomez"
    (carpeta / "a.txt").write_text("viejo", encoding="utf-8")
    nuevo = tmp_path / "a.txt"
    nuevo.write_text("nuevo", encoding="utf-8")
    r = I.aplicar_filas([{"archivo": str(nuevo), "destino": str(carpeta), "accion": "mover",
                          "hash": I.hash_archivo(nuevo)}], "t")
    assert r["movidos"] == 1 and (carpeta / "a.txt").read_text(encoding="utf-8") == "viejo"
    assert any("quedo como a__1.txt" in x for x in r["avisos"])


def test_tandas_editadas_a_mano_con_numeros_rotos_se_ignoran(entorno, tmp_path):
    a = tmp_path / "a.txt"
    a.write_text("x", encoding="utf-8")
    I.aplicar_filas([{"archivo": str(a), "destino": str(entorno / "01_Expedientes" / "2022-perez-gomez"),
                      "accion": "mover", "hash": I.hash_archivo(a)}], "t")
    with I.TANDAS_CSV.open("a", encoding="utf-8", newline="") as f:
        f.write("t0002,2026-01-01,a mano,1,cero,uno,\n")
    assert [t["id"] for t in I.leer_tandas()] == ["t0001"]
    assert I.tandas_historicas(I.leer_ruteo(), I.leer_tandas())


def test_causas_sin_carpeta_en_disco(entorno):
    from cartera import Causa, normalizar_expte
    causas = [causa("111111/2022", "2022-perez-gomez"), causa("222222/2023", "2023-renombrada"),
              Causa(normalizar_expte("333333/2024"))]
    assert I.causas_sin_carpeta(causas) == {"faltantes": ["2023-renombrada"], "sin_nombre": 1}


CEDULA_CAMARA = ("CEDULA DE NOTIFICACION. Camara de Apelaciones en lo Civil y Comercial, Sala II, Posadas. "
                 "Expte. N 111111/2022. Queda Ud. debidamente notificado de la resolución "
                 "dictada en autos. ") * 3


def test_cedula_de_camara_de_una_causa_de_la_cartera_va_a_la_causa_y_no_a_jurisprudencia(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    d = I.destino_de(escribir(tmp_path, "notificacion.pdf", CEDULA_CAMARA))
    assert d.motivo in {"cartera", "expediente"}
    assert d.carpeta == entorno / "01_Expedientes" / "2022-perez-gomez" / "01_siged"


def test_fallo_que_cita_un_numero_de_la_cartera_sigue_yendo_a_jurisprudencia(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    d = I.destino_de(escribir(tmp_path, "articulo-csjn.md", ARTICULO_CSJN + " Ver también expte. 111111/2022."))
    assert d.motivo == "jurisprudencia"


def test_fallo_sobre_cedulas_de_notificacion_sigue_yendo_a_jurisprudencia(entorno, tmp_path):
    cartera(entorno, causa("111111/2022", "2022-perez-gomez"))
    fallo = ("Camara de Apelaciones en lo Civil y Comercial, Sala II, Posadas. SENTENCIA. Y VISTOS: los autos sobre "
             "nulidad de la cedula de notificacion cursada al demandado. CONSIDERANDO: que la cedula de notificacion "
             "debe contener copia de la demanda. RESUELVE: hacer lugar al recurso. ") * 3
    d = I.destino_de(escribir(tmp_path, "fallo.pdf", fallo))
    assert d.motivo == "jurisprudencia" and entorno / "02_Jurisprudencia" in d.carpeta.parents
