import time

import pytest

import busqueda as B
import dashboard as D


@pytest.fixture
def base(tmp_path, monkeypatch):
    b = tmp_path / "SIGED"
    for d in ("01_Expedientes/2022-perez-gomez/01_siged", "03_Doctrina", "_INBOX", "_SISTEMA"):
        (b / d).mkdir(parents=True)
    (b / "01_Expedientes/2022-perez-gomez/01_siged/a.txt").write_text("La prescripción adquisitiva", encoding="utf-8")
    (b / "03_Doctrina/b.txt").write_text("Prescripción liberatoria", encoding="utf-8")
    db = tmp_path / "busqueda.db"
    B.sincronizar(base=b, db=db)
    monkeypatch.setattr(B, "BUSQUEDA_DB", db)
    monkeypatch.setattr(D, "BASE", b)
    monkeypatch.setattr(D, "_busqueda_act", {"estado": "ocioso", "resultado": None, "error": None})
    return b


@pytest.fixture
def cliente():
    return D.app.test_client()


def test_buscar_devuelve_total_resultados_y_fragmento(base, cliente):
    r = cliente.get("/api/buscar", query_string={"q": "prescripcion"}).get_json()
    assert r["total"] == 2 and len(r["resultados"]) == 2 and "«" in r["resultados"][0]["fragmento"]


def test_buscar_filtra_y_limita(base, cliente):
    r = cliente.get("/api/buscar", query_string={"q": "prescripcion", "area": "03_Doctrina"}).get_json()
    assert r["total"] == 1
    r = cliente.get("/api/buscar", query_string={"q": "prescripcion", "caso": "2022-perez-gomez"}).get_json()
    assert r["total"] == 1
    r = cliente.get("/api/buscar", query_string={"q": "prescripcion", "limite": "1"}).get_json()
    assert r["total"] == 2 and len(r["resultados"]) == 1


def test_buscar_vacio_o_con_limite_invalido_no_falla(base, cliente):
    assert cliente.get("/api/buscar").get_json()["total"] == 0
    assert cliente.get("/api/buscar", query_string={"q": "prescripcion", "limite": "abc"}).status_code == 200
    r = cliente.get("/api/buscar", query_string={"q": "prescripcion", "limite": "100000"}).get_json()
    assert len(r["resultados"]) <= 100


def test_estado_del_indice(base, cliente):
    e = cliente.get("/api/buscar/estado").get_json()
    assert e["docs"] == 2 and e["areas"] == ["01_Expedientes", "03_Doctrina"] and e["actualizado"]
    assert e["actualizacion"]["estado"] == "ocioso"


def test_actualizar_corre_en_segundo_plano_y_encuentra_lo_nuevo(base, cliente):
    (base / "03_Doctrina/nuevo.txt").write_text("usucapion breve", encoding="utf-8")
    assert cliente.post("/api/buscar/actualizar").status_code == 200
    for _ in range(100):
        a = cliente.get("/api/buscar/estado").get_json()["actualizacion"]
        if a["estado"] != "corriendo":
            break
        time.sleep(0.05)
    assert a["estado"] == "listo" and a["resultado"]["nuevos"] == 1
    assert cliente.get("/api/buscar", query_string={"q": "usucapion"}).get_json()["total"] == 1


def test_abrir_archivo_y_carpeta_solo_si_esta_en_el_indice(base, cliente, monkeypatch):
    llamadas = []
    monkeypatch.setattr(D, "_lanzar", lambda modo, ruta: llamadas.append((modo, ruta)))
    ruta = str(base / "03_Doctrina/b.txt")
    assert cliente.post("/api/buscar/abrir", json={"ruta": ruta}).status_code == 200
    assert cliente.post("/api/buscar/abrir", json={"ruta": ruta, "carpeta": True}).status_code == 200
    assert llamadas == [("archivo", ruta), ("carpeta", ruta)]


def test_abrir_rechaza_lo_que_no_esta_en_el_indice(base, cliente, monkeypatch):
    llamadas = []
    monkeypatch.setattr(D, "_lanzar", lambda modo, ruta: llamadas.append((modo, ruta)))
    ajena = base / "_SISTEMA" / "cartera.json"
    ajena.write_text("{}", encoding="utf-8")
    for ruta in (str(ajena), str(base / "03_Doctrina" / ".." / "03_Doctrina" / "b.txt"), r"C:\Windows\notepad.exe", "", None):
        assert cliente.post("/api/buscar/abrir", json={"ruta": ruta}).status_code == 404
    assert cliente.post("/api/buscar/abrir", json={}).status_code == 404
    assert llamadas == []


def test_abrir_un_archivo_que_ya_no_existe_da_404(base, cliente, monkeypatch):
    monkeypatch.setattr(D, "_lanzar", lambda modo, ruta: pytest.fail("no debe lanzar"))
    ruta = base / "03_Doctrina/b.txt"
    ruta.unlink()
    assert cliente.post("/api/buscar/abrir", json={"ruta": str(ruta)}).status_code == 404
