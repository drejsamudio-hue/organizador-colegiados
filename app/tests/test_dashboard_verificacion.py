"""Dashboard: identidad de archivo al aplicar, destinos relativos, 409 por otra tanda, hosts del tunel,
cartera ilegible y una sola extraccion por archivo (correcciones de la revision de 2026-11, portadas a colegiados)."""
import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import dashboard as D  # noqa: E402
import ingesta as I  # noqa: E402
from cartera import Causa, guardar, normalizar_expte  # noqa: E402
from ocr_completo import hash_archivo  # noqa: E402

CUERPO = "Providencia del juzgado. Se tiene presente lo manifestado por la parte y se ordena el traslado. " * 2


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
    monkeypatch.setattr(config, "BASE", base)
    monkeypatch.delenv("ORGANIZADOR_HOSTS", raising=False)
    return base


@pytest.fixture
def cliente(entorno):
    D.app.config["TESTING"] = True
    return D.app.test_client()


def escribir(ruta: Path, texto: str) -> Path:
    ruta.parent.mkdir(parents=True, exist_ok=True)
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def propuesta(entorno, nombre, filas):
    ruta = entorno / "_SISTEMA" / nombre
    with ruta.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(D.COLUMNAS)
        w.writerows(filas)
    return ruta


DESTINO_REL = "01_Expedientes/2023-vidal-luna/01_siged"


def fila_ui(a, destino=DESTINO_REL, accion="mover"):
    return {"archivo": str(a), "destino": destino, "motivo": "cartera", "detalle": "numero_nombre",
            "confianza": "media", "accion": accion}


# -- A1: identidad y extracto salen de la simulacion --------------------------------------------

def test_aplicar_desde_la_interfaz_resuelve_destinos_y_conserva_extracto_e_identidad(entorno, cliente, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    dst = entorno / "01_Expedientes" / "2023-vidal-luna" / "01_siged"
    ruta = propuesta(entorno, "REORG_prueba.csv",
                     [[a, dst, "cartera", "numero_nombre", "media", "mover", "Providencia del juzgado...", hash_archivo(a)]])
    r = cliente.post("/api/propuestas/REORG_prueba.csv/aplicar", json={"filas": [fila_ui(a)]})
    assert r.status_code == 200 and r.get_json()["movidos"] == 1
    assert (dst / "a.txt").is_file() and not a.exists()
    guardadas = list(csv.DictReader(ruta.open(encoding="utf-8-sig")))
    assert guardadas[0]["extracto"] == "Providencia del juzgado..."
    assert guardadas[0]["hash"] == hash_archivo(dst / "a.txt")
    assert Path(guardadas[0]["destino"]).is_absolute()


def test_un_hash_o_extracto_mandado_por_el_navegador_no_pisa_los_de_la_simulacion(entorno, cliente, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    dst = entorno / "01_Expedientes" / "2023-vidal-luna" / "01_siged"
    ruta = propuesta(entorno, "REORG_x.csv", [[a, dst, "cartera", "", "media", "mover", "extracto original", hash_archivo(a)]])
    a.write_text(CUERPO + " editado despues de simular", encoding="utf-8")
    f = {**fila_ui(a), "hash": "0" * 64, "extracto": "inventado"}
    r = cliente.post("/api/propuestas/REORG_x.csv/aplicar", json={"filas": [f]}).get_json()
    assert r["movidos"] == 0 and any("cambio desde la simulacion" in x for x in r["avisos"])
    assert a.is_file()
    fila = next(csv.DictReader(ruta.open(encoding="utf-8-sig")))
    assert fila["extracto"] == "extracto original" and fila["hash"] != "0" * 64


def test_una_propuesta_sin_hash_no_se_mueve_desde_la_interfaz(entorno, cliente, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    propuesta(entorno, "REORG_vieja.csv", [[a, entorno / "_INBOX", "cartera", "", "media", "mover", "", ""]])
    r = cliente.post("/api/propuestas/REORG_vieja.csv/aplicar", json={"filas": [fila_ui(a)]}).get_json()
    assert r["movidos"] == 0 and any("volver a simular" in x for x in r["avisos"]) and a.is_file()


def test_la_simulacion_y_la_ajena_escriben_el_hash(entorno, tmp_path):
    origen = tmp_path / "origen"
    a = escribir(origen / "cedula 111111-2022.txt", CUERPO)
    D._simulaciones["s1"] = {"id": "s1", "origen": str(origen), "csv": "REORG_s1.csv", "total": None,
                             "hechos": 0, "estado": "corriendo", "error": None}
    D._simular("s1", origen)
    assert D._simulaciones["s1"]["estado"] == "listo"
    filas = list(csv.DictReader((entorno / "_SISTEMA" / "REORG_s1.csv").open(encoding="utf-8-sig")))
    assert filas[0]["hash"] == hash_archivo(a) and filas[0]["extracto"].startswith("Providencia")


def test_carpeta_ajena_escribe_hash_y_extracto(entorno, cliente):
    ajena = entorno / "01_Expedientes" / "2009-ruiz-sosa" / "01_siged"
    f = escribir(ajena / "fallo.txt", "VISTOS Y CONSIDERANDO. RESUELVE: rechazar. Superior Tribunal de Justicia. " * 10)
    r = cliente.post("/api/fuera/ajena", json={"carpeta": "2009-ruiz-sosa"}).get_json()
    fila = next(csv.DictReader((entorno / "_SISTEMA" / r["csv"]).open(encoding="utf-8-sig")))
    assert fila["hash"] == hash_archivo(f) and fila["extracto"].startswith("VISTOS")


def test_un_archivo_que_no_se_puede_leer_queda_sin_identidad_y_no_tumba_la_simulacion(entorno, tmp_path, monkeypatch):
    origen = tmp_path / "origen"
    escribir(origen / "cedula 111111-2022.txt", CUERPO)
    escribir(origen / "otro 111111-2022.txt", CUERPO)
    real = D.hash_archivo

    def falla_el_primero(p):
        if p.name.startswith("cedula"):
            raise PermissionError("bloqueado")
        return real(p)

    monkeypatch.setattr(D, "hash_archivo", falla_el_primero)
    D._simulaciones["s2"] = {"id": "s2", "origen": str(origen), "csv": "REORG_s2.csv", "total": None,
                             "hechos": 0, "estado": "corriendo", "error": None}
    D._simular("s2", origen)
    assert D._simulaciones["s2"]["estado"] == "listo"
    por_nombre = {Path(f["archivo"]).name: f["hash"]
                  for f in csv.DictReader((entorno / "_SISTEMA" / "REORG_s2.csv").open(encoding="utf-8-sig"))}
    assert por_nombre["cedula 111111-2022.txt"] == "" and len(por_nombre["otro 111111-2022.txt"]) == 64


# -- A3: destinos relativos -----------------------------------------------------------------------

def test_destinos_que_salen_de_la_base_o_son_de_otro_sistema_dan_400(entorno, cliente, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    propuesta(entorno, "REORG_d.csv", [[a, entorno / "_INBOX", "cartera", "", "media", "mover", "", hash_archivo(a)]])
    for malo in ("../../fuera", "01_Expedientes\\..\\..\\x", "/etc/x", str(tmp_path / "fuera")):
        r = cliente.post("/api/propuestas/REORG_d.csv/aplicar", json={"filas": [fila_ui(a, malo)]})
        assert r.status_code == 400, malo
    assert a.is_file()


def test_resolver_destino_rechaza_vacio_y_rutas_con_punto_punto(entorno):
    assert D._resolver_destino("") is None and D._resolver_destino("  ") is None
    assert D._resolver_destino("a/../b") is None
    assert D._resolver_destino("01_Expedientes/x") == entorno / "01_Expedientes" / "x"


def test_las_filas_que_quedan_conservan_el_destino_absoluto_de_la_simulacion(entorno, cliente, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    dudoso = entorno / "_INBOX" / "sin_clasificar"
    ruta = propuesta(entorno, "REORG_q.csv", [[a, dudoso, "sin_clasificar", "", "baja", "queda", "", hash_archivo(a)]])
    f = fila_ui(a, destino="", accion="queda")
    assert cliente.post("/api/propuestas/REORG_q.csv/aplicar", json={"filas": [f]}).status_code == 200
    fila = next(csv.DictReader(ruta.open(encoding="utf-8-sig")))
    assert Path(fila["destino"]) == dudoso and a.is_file()


# -- A4: una sola tanda a la vez ------------------------------------------------------------------

def test_el_dashboard_devuelve_409_si_hay_otra_tanda(entorno, cliente, tmp_path, monkeypatch):
    monkeypatch.setattr(I, "ESPERA_BLOQUEO", 0.3)
    a = escribir(tmp_path / "a.txt", CUERPO)
    propuesta(entorno, "REORG_testlock.csv",
              [[a, entorno / "01_Expedientes" / "2023-vidal-luna" / "01_siged", "cartera", "", "media", "mover", "x",
                hash_archivo(a)]])
    with I.bloqueo_tanda():
        r = cliente.post("/api/propuestas/REORG_testlock.csv/aplicar", json={"filas": [fila_ui(a)]})
    assert r.status_code == 409 and "otra tanda" in r.get_json()["error"]
    assert a.is_file()


# -- A8: Revisar habilitado y origen precargado ---------------------------------------------------

def test_una_propuesta_de_solo_dudosos_se_puede_revisar(entorno, cliente, tmp_path):
    a = escribir(tmp_path / "a.txt", CUERPO)
    propuesta(entorno, "REORG_dudosos.csv",
              [[a, entorno / "_INBOX" / "sin_clasificar", "sin_clasificar", "", "baja", "queda", "sin senal", hash_archivo(a)]])
    resumen = cliente.get("/api/propuestas").get_json()[0]
    assert resumen["a_mover"] == 0 and resumen["pendientes"] == 0 and resumen["disponibles"] == 1


def test_config_trae_la_bandeja_para_precargar_el_origen(entorno, cliente):
    r = cliente.get("/api/config").get_json()
    assert r["inbox"] == str(entorno / "_INBOX") and r["base"] == str(entorno)


# -- A7: hosts del tunel --------------------------------------------------------------------------

def test_el_dashboard_rechaza_hosts_desconocidos(entorno, cliente):
    assert cliente.get("/api/aliases", headers={"Host": "estudio.example.com"}).status_code == 403
    assert cliente.get("/api/aliases", headers={"Host": "localhost:5050"}).status_code == 200


def test_los_hosts_del_tunel_se_configuran_y_funcionan(entorno, cliente):
    (entorno / "_SISTEMA" / "hosts_permitidos.txt").write_text("# tunel del estudio\nEstudio.Example.com\n", encoding="utf-8")
    assert cliente.get("/api/aliases", headers={"Host": "estudio.example.com"}).status_code == 200
    assert cliente.get("/api/aliases", headers={"Host": "otro.example.com"}).status_code == 403
    r = cliente.post("/api/buscar/actualizar", headers={"Host": "estudio.example.com",
                                                        "Origin": "https://estudio.example.com"})
    assert r.status_code == 200
    r = cliente.post("/api/buscar/actualizar", headers={"Host": "estudio.example.com", "Origin": "https://otro.example.com"})
    assert r.status_code == 403


def test_los_hosts_del_tunel_tambien_se_toman_de_la_variable_de_entorno(entorno, cliente, monkeypatch):
    monkeypatch.setenv("ORGANIZADOR_HOSTS", "tunel.example.org")
    assert cliente.get("/api/aliases", headers={"Host": "tunel.example.org"}).status_code == 200


# -- cartera ilegible -----------------------------------------------------------------------------

def test_una_cartera_ilegible_no_tumba_las_pantallas_y_no_se_pisa(entorno, cliente):
    ruta = entorno / "_SISTEMA" / "cartera.json"
    ruta.write_text("{esto no es json", encoding="utf-8")
    for url in ("/api/cartera", "/api/stats", "/api/escritorio", "/api/fuera"):
        assert cliente.get(url).status_code == 200, url
    assert cliente.get("/api/cartera").get_json() == {"causas": [], "resumen": {}}
    for r in (cliente.post("/api/cartera", json={"expte": "123456/2023"}),
              cliente.post("/api/cartera/lista", json={"texto": "123456/2023"}),
              cliente.post("/api/cartera/asignar-carpetas"),
              cliente.patch("/api/cartera", json={"expte": "123456/2023", "caratula": "A c/ B"}),
              cliente.delete("/api/cartera", json={"expte": "123456/2023"}),
              cliente.post("/api/aliases/cartera/regenerar")):
        assert r.status_code == 409 and "cartera.json" in r.get_json()["error"]
    assert ruta.read_text(encoding="utf-8") == "{esto no es json"


def test_una_cartera_con_otra_forma_tampoco_tumba_la_lectura(entorno, cliente):
    (entorno / "_SISTEMA" / "cartera.json").write_text("[1, 2]", encoding="utf-8")
    r = cliente.get("/api/cartera")
    assert r.status_code == 200 and r.get_json()["causas"] == []


# -- A2 (parte del dashboard): una causa nueva no pretende una carpeta existente ------------------

def test_el_alta_no_pretende_una_carpeta_que_ya_existe_en_disco(entorno, cliente):
    r = cliente.post("/api/cartera", json={"expte": "444444/2023", "caratula": "VIDAL c/ LUNA"})
    assert r.status_code == 200
    causa = json.loads((entorno / "_SISTEMA" / "cartera.json").read_text(encoding="utf-8"))["causas"][0]
    assert causa["carpeta_prevista"] != "2023-vidal-luna" and causa["carpeta_prevista"].startswith("2023-vidal-luna")


def test_asignar_carpetas_y_editar_tampoco_pretenden_carpetas_existentes(entorno, cliente):
    guardar([Causa(normalizar_expte("444444/2023"), caratula="VIDAL c/ LUNA")], entorno / "_SISTEMA" / "cartera.json")
    assert cliente.post("/api/cartera/asignar-carpetas").get_json()["asignadas"] == 1
    causas = json.loads((entorno / "_SISTEMA" / "cartera.json").read_text(encoding="utf-8"))["causas"]
    assert causas[0]["carpeta_prevista"] != "2023-vidal-luna"

    guardar([Causa(normalizar_expte("555555/2023"), caratula="X c/ Y")], entorno / "_SISTEMA" / "cartera.json")
    assert cliente.patch("/api/cartera", json={"expte": "555555/2023", "caratula": "VIDAL c/ LUNA"}).status_code == 200
    causas = json.loads((entorno / "_SISTEMA" / "cartera.json").read_text(encoding="utf-8"))["causas"]
    assert causas[0]["carpeta_prevista"] != "2023-vidal-luna"


# -- v1 #4: una sola extraccion por archivo -------------------------------------------------------

def test_cada_archivo_se_extrae_una_sola_vez_al_simular(entorno, tmp_path, monkeypatch):
    origen = tmp_path / "origen"
    nombres = ["cedula 111111-2022.txt", "otro 222222-2023.txt"]
    for n in nombres:
        escribir(origen / n, CUERPO)
    llamadas = []
    real = D.extraer_universal

    def contado(p, *a, **k):
        llamadas.append(Path(p).name)
        return real(p, *a, **k)

    monkeypatch.setattr(D, "extraer_universal", contado)
    monkeypatch.setattr(I, "extraer_universal", contado)
    D._simulaciones["s3"] = {"id": "s3", "origen": str(origen), "csv": "REORG_s3.csv", "total": None,
                             "hechos": 0, "estado": "corriendo", "error": None}
    D._simular("s3", origen)
    assert D._simulaciones["s3"]["estado"] == "listo"
    assert sorted(llamadas) == sorted(nombres)


def test_cada_archivo_de_una_carpeta_ajena_se_extrae_una_sola_vez(entorno, cliente, monkeypatch):
    f = escribir(entorno / "01_Expedientes" / "2009-ruiz-sosa" / "01_siged" / "fallo.txt",
                 "VISTOS Y CONSIDERANDO. RESUELVE: rechazar. Superior Tribunal de Justicia. " * 10)
    llamadas = []
    real = D.extraer_universal

    def contado(p, *a, **k):
        llamadas.append(Path(p).name)
        return real(p, *a, **k)

    monkeypatch.setattr(D, "extraer_universal", contado)
    monkeypatch.setattr(I, "extraer_universal", contado)
    assert cliente.post("/api/fuera/ajena", json={"carpeta": "2009-ruiz-sosa"}).status_code == 200
    assert llamadas == [f.name]


# -- A5: comillas en el extracto y rutas con barra invertida (revision de 2026-11) -----------------

def test_un_extracto_con_comillas_no_desalinea_el_csv_de_la_simulacion(entorno, cliente, tmp_path):
    """El texto de un escrito suele traer comillas ("DANOS Y PERJUICIOS"). Dentro del CSV desalineaban las
    columnas: la fila perdia su hash y el archivo ya no se podia mover (o tomaba el hash de otra fila)."""
    cuerpo = ('Expte 1234/2020 PEREZ, JUAN c/ GOMEZ s/ "DANOS Y PERJUICIOS", con costas y algo mas de texto '
              'para que el extracto supere el limite de ciento sesenta caracteres y se recorte')
    a = escribir(tmp_path / "con comillas.txt", cuerpo)
    D._simulaciones["s5"] = {"id": "s5", "origen": str(tmp_path), "csv": "REORG_s5.csv", "total": None,
                             "hechos": 0, "estado": "corriendo", "error": None}
    D._simular("s5", tmp_path)
    fila = next(f for f in I._leer_propuestas(entorno / "_SISTEMA" / "REORG_s5.csv") if f["archivo"] == str(a))
    assert fila["hash"] == hash_archivo(a)                      # la fila conserva su propia identidad
    assert '"' not in fila["extracto"] and fila["extracto"].count("«") == fila["extracto"].count("»")
    r = cliente.post("/api/propuestas/REORG_s5.csv/aplicar", json={"filas": [fila_ui(a, DESTINO_REL)]})
    assert r.status_code == 200 and r.get_json()["movidos"] == 1


def test_resolver_destino_rechaza_punto_punto_escrito_con_barra_invertida(entorno):
    """En POSIX '..\\..\\x' es un solo nombre de archivo: sin separarlo a mano salia de la base."""
    assert D._resolver_destino("01_Expedientes\\..\\..\\x") is None
    assert D._resolver_destino("01_Expedientes\\x") == entorno / "01_Expedientes" / "x"
    assert D._resolver_destino("..\\x") is None
    # una absoluta se resuelve y el endpoint la rechaza por quedar fuera de la base (400), como antes
    assert not D._resolver_destino("/etc/x").resolve().is_relative_to(entorno.resolve())
