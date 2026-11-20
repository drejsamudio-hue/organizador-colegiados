"""Dashboard: correcciones de la auditoria multidimensional (H0xx) portadas a colegiados, y su interfaz.

La interfaz (dashboard_app.html) no tiene navegador en pytest: lo que se puede comprobar de ella es estatico y se
comprueba sobre el texto; el recorrido real lo cubre humo_ui.py."""
import copy
import csv
import gc
import json
import shutil
import sys
import warnings
from html.parser import HTMLParser
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dashboard as D  # noqa: E402
import ingesta as I  # noqa: E402
from cartera import Causa, guardar, normalizar_expte  # noqa: E402

HTML = (Path(D.__file__).resolve().parent / "dashboard_app.html").read_text(encoding="utf-8")


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
        if hasattr(D, nombre):
            monkeypatch.setattr(D, nombre, valor)
    monkeypatch.setattr(I, "_exptes_cache", None)
    monkeypatch.setattr(I, "_indice_cache", (-1.0, I.IndiceCartera([])))
    monkeypatch.setattr(I, "_alias_cache", ((-1.0, -1.0), []))
    monkeypatch.setattr(D, "LOG_FILE", base / "_SISTEMA" / "dashboard_log.json")
    monkeypatch.setattr(D, "JUSTI_DIR", tmp_path / "justi")
    monkeypatch.setattr(D, "_inv", {"t": 0.0, "datos": None})
    monkeypatch.delenv("ORGANIZADOR_HOSTS", raising=False)
    guardar([Causa(normalizar_expte("111111/2022"), caratula="PEREZ c/ GOMEZ", carpeta="2022-perez-gomez")],
            base / "_SISTEMA" / "cartera.json")
    return base


@pytest.fixture
def cliente(entorno):
    D.app.config["TESTING"] = True
    return D.app.test_client()


# -- H001: los CSV se cierran ---------------------------------------------------------------------

def test_los_csv_de_historial_y_fuera_se_leen_y_se_cierran(entorno, cliente):
    sis = entorno / "_SISTEMA"
    (sis / "CARTERA_APLICADO.csv").write_text("fecha,accion,origen,destino\n2026-01-01,crear,,x\n", encoding="utf-8")
    (sis / "PROPIEDAD_CARPETAS.csv").write_text("carpeta,clase,matricula,apellido\n2023-vidal-luna,ajena,0,0\n",
                                                encoding="utf-8-sig")
    with warnings.catch_warnings(record=True) as avisos:
        warnings.simplefilter("always", ResourceWarning)
        historial = cliente.get("/api/historial").get_json()
        fuera = cliente.get("/api/fuera").get_json()
        gc.collect()
    assert historial["carpetas"][0]["destino"] == "x" and fuera[0]["clase"] == "ajena"
    assert not [a for a in avisos if issubclass(a.category, ResourceWarning)]


# -- H006: escritorio.json roto o de otra forma ---------------------------------------------------

@pytest.mark.parametrize("contenido", ["{no es json", "null", '{"a": 1}', "[null, 3, \"x\"]", ""])
def test_escritorio_ilegible_o_con_filas_raras_no_da_500(entorno, cliente, tmp_path, contenido):
    ruta = tmp_path / "justi" / "2026-11-19" / "escritorio.json"
    ruta.parent.mkdir(parents=True)
    ruta.write_text(contenido, encoding="utf-8")
    r = cliente.get("/api/escritorio")
    assert r.status_code == 200 and r.get_json()["filas"] == []


def test_escritorio_ignora_solo_las_filas_que_no_son_objetos(entorno, cliente, tmp_path):
    ruta = tmp_path / "justi" / "2026-11-19" / "escritorio.json"
    ruta.parent.mkdir(parents=True)
    ruta.write_text(json.dumps([None, {"numero": "111111/2022", "designacion": "PEREZ"}]), encoding="utf-8")
    filas = cliente.get("/api/escritorio").get_json()["filas"]
    assert len(filas) == 1 and filas[0]["en_cartera"] is True


# -- H007 / H041 / H047: sin 01_Expedientes -------------------------------------------------------

def test_sin_carpeta_de_expedientes_ninguna_pantalla_da_500(entorno, cliente):
    shutil.rmtree(entorno / "01_Expedientes")
    for url in ("/api/stats", "/api/expedientes", "/api/tipos", "/api/fuera", "/api/cartera"):
        assert cliente.get(url).status_code == 200, url
    stats = cliente.get("/api/stats").get_json()
    assert stats["total_causas"] == 0 and stats["total_archivos"] == 0


def test_el_inventario_no_se_sirve_de_un_cache_vencido_al_desmontar_la_unidad(entorno):
    assert D._inventario()
    shutil.rmtree(entorno / "01_Expedientes")
    assert D._inventario() == {} and D._inv["datos"] is None


# -- H014: invalidar el cache a mitad de una consulta no devuelve None ----------------------------

def test_invalidar_el_inventario_mientras_se_arma_no_devuelve_none(entorno, monkeypatch):
    class Cache(dict):
        def update(self, *a, **k):
            super().update(*a, **k)
            self["datos"] = None  # otro hilo (mover o deshacer) invalida justo despues de armarlo

    monkeypatch.setattr(D, "_inv", Cache(t=0.0, datos=None))
    assert isinstance(D._inventario(), dict)


# -- H008: el cruce se une por clave, no por identidad de objeto ----------------------------------

def test_api_cartera_sigue_andando_si_el_cruce_devuelve_copias(entorno, cliente, monkeypatch):
    original = D.cruzar

    def con_copias(*a, **k):
        out = []
        for x in original(*a, **k):
            y = copy.copy(x)
            object.__setattr__(y, "causa", copy.copy(x.causa))
            out.append(y)
        return out

    monkeypatch.setattr(D, "cruzar", con_copias)
    causa = cliente.get("/api/cartera").get_json()["causas"][0]
    assert causa["accion"] is not None


# -- H010: una sola consulta de existencia por fila ----------------------------------------------

def test_propuestas_consulta_cada_archivo_una_sola_vez(entorno, cliente, tmp_path, monkeypatch):
    archivos = []
    for i in range(3):
        a = tmp_path / f"a{i}.txt"
        a.write_text("x", encoding="utf-8")
        archivos.append(a)
    ruta = entorno / "_SISTEMA" / "REORG_20260101-000000-abcdef.csv"
    with ruta.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(D.COLUMNAS)
        for a, accion in zip(archivos, ("mover", "mover", "queda")):
            w.writerow([a, entorno / "01_Expedientes", "cartera", "", "media", accion, "", "h"])
    consultas = []
    es_archivo = Path.is_file

    def contado(self):
        consultas.append(str(self))
        return es_archivo(self)

    monkeypatch.setattr(Path, "is_file", contado)
    resumen = cliente.get("/api/propuestas").get_json()[0]
    monkeypatch.undo()
    assert [c for c in consultas if c in {str(a) for a in archivos}] == [str(a) for a in archivos]
    assert (resumen["a_mover"], resumen["pendientes"], resumen["disponibles"]) == (2, 2, 3)


# -- H012: el nombre de la propuesta no admite salto de linea final --------------------------------

def test_el_nombre_de_propuesta_no_admite_salto_de_linea_final(entorno, cliente):
    assert D.RE_PROPUESTA.match("REORG_a.csv") and not D.RE_PROPUESTA.match("REORG_a.csv\n")
    assert cliente.get("/api/propuestas/REORG_a.csv%0A").status_code == 404


# -- H017 / H033: cabeceras y Sec-Fetch-Site ------------------------------------------------------

def test_las_respuestas_llevan_cabeceras_de_endurecimiento(entorno, cliente):
    for url in ("/", "/api/aliases"):
        h = cliente.get(url).headers
        assert h["X-Frame-Options"] == "DENY" and h["X-Content-Type-Options"] == "nosniff"
        assert h["Referrer-Policy"] == "no-referrer"


@pytest.mark.parametrize("sitio,esperado", [("cross-site", 403), ("same-site", 403), ("same-origin", 400), ("none", 400)])
def test_un_post_de_otro_sitio_se_rechaza_por_sec_fetch_site(entorno, cliente, sitio, esperado):
    # 400 = pasa el filtro y el cuerpo vacio se rechaza despues
    assert cliente.post("/api/aliases", json={}, headers={"Sec-Fetch-Site": sitio}).status_code == esperado


def test_un_get_de_otro_sitio_sigue_permitido(entorno, cliente):
    assert cliente.get("/api/aliases", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200


# -- H036: alias con regex invalida ---------------------------------------------------------------

def test_alias_con_regex_invalida_da_400_y_no_escribe(entorno, cliente):
    r = cliente.post("/api/aliases", json=[{"pattern": "(", "caso": "2022-perez-gomez"}])
    assert r.status_code == 400 and r.get_json()["ok"] is False and r.get_json()["errores"]
    assert not I.ALIAS_FILE.exists()


# -- interfaz: comprobaciones estaticas -------------------------------------------------------------

class _Controles(HTMLParser):
    def __init__(self):
        super().__init__()
        self.sin_nombre, self.en_label, self.labels_for = [], 0, set()
        self.controles = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self.en_label += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit", "button") \
                and "hidden" not in a:
            self.controles.append((a, self.en_label > 0))

    def handle_endtag(self, tag):
        if tag == "label":
            self.en_label -= 1


def test_todo_control_estatico_tiene_nombre_accesible():
    p = _Controles()
    p.feed(HTML)
    sin = [a.get("id") or a.get("class") for a, dentro in p.controles
           if not (dentro or a.get("aria-label") or a.get("aria-labelledby") or a.get("id") in p.labels_for)]
    assert not sin, sin


def test_los_controles_dibujados_por_script_tambien_llevan_aria_label():
    for clase in ("fila-check", "grupo-check", "fila-destino", "grupo-destino", "esc-sel", "esc-car", "alias-del"):
        i = HTML.index(f'class="{clase}"') if f'class="{clase}"' in HTML else HTML.index(clase + '" ')
        etiqueta = HTML[HTML.rfind("<", 0, i):HTML.index(">", i)]
        assert "aria-label" in etiqueta, clase
    assert 'id="revConf" aria-label=' in HTML


def test_salto_al_contenido_y_errores_anunciados():
    assert 'class="skip-link" href="#contenido"' in HTML and 'id="contenido"' in HTML
    assert "setAttribute('role', 'alert')" in HTML and ":scope > .error-box" in HTML


def test_esc_neutraliza_el_acento_grave():
    assert "'`':'&#96;'" in HTML


def test_la_revision_conserva_foco_y_desplazamiento_al_redibujar():
    cuerpo = HTML[HTML.index("function renderRevision()"):HTML.index("// El destino se manda relativo")]
    assert "foco(el)" in cuerpo and "scrollTop" in cuerpo and "restaurarFoco(el, enfocado)" in cuerpo


def test_las_areas_de_buscar_se_refrescan_y_las_acciones_de_fuera_avisan_errores():
    assert "buscarListo" not in HTML and "areasMostradas" in HTML
    fuera = HTML[HTML.index("const paginaCartera"):HTML.index("document.getElementById('fueraClase').addEventListener")]
    assert fuera.count("catch (err)") == 2 and "b.disabled = false; b.textContent = 'Ajena'" in fuera
    assert "verTanda(b.dataset.a, b.dataset.b).catch(" in HTML


def test_no_se_suben_archivos_vacios_y_el_filtro_de_cartera_espera():
    assert "files.filter(f => f.size > 0);" in HTML
    assert "setTimeout(renderCartera, 120)" in HTML
