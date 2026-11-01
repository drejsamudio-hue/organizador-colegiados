from pathlib import Path

import busqueda as B
import buscar as C


def armar(tmp_path, monkeypatch):
    base = tmp_path / "SIGED"
    (base / "01_Expedientes/2022-perez-gomez/01_siged").mkdir(parents=True)
    (base / "03_Doctrina").mkdir(parents=True)
    (base / "01_Expedientes/2022-perez-gomez/01_siged/a.txt").write_text("La prescripción adquisitiva del inmueble", encoding="utf-8")
    (base / "03_Doctrina/b.txt").write_text("Otra prescripción, esta vez liberatoria", encoding="utf-8")
    db = tmp_path / "busqueda.db"
    B.sincronizar(base=base, db=db)
    monkeypatch.setattr(B, "BUSQUEDA_DB", db)
    monkeypatch.setattr(C, "BASE", base)
    return base


def test_muestra_resultados_con_ruta_relativa_y_fragmento(tmp_path, monkeypatch, capsys):
    armar(tmp_path, monkeypatch)
    assert C.main(["prescripcion"]) == 0
    salida = capsys.readouterr().out
    assert "2 resultados" in salida
    assert str(Path("01_Expedientes", "2022-perez-gomez", "01_siged", "a.txt")) in salida
    assert "«prescripción»" in salida


def test_filtra_por_area_y_caso(tmp_path, monkeypatch, capsys):
    armar(tmp_path, monkeypatch)
    C.main(["prescripcion", "--area", "03_Doctrina"])
    assert "1 resultado" in capsys.readouterr().out
    C.main(["prescripcion", "--caso", "2022-perez-gomez"])
    assert "1 resultado" in capsys.readouterr().out


def test_sin_resultados_sale_con_codigo_1(tmp_path, monkeypatch, capsys):
    armar(tmp_path, monkeypatch)
    assert C.main(["inexistente"]) == 1
    assert "Sin resultados" in capsys.readouterr().out


def test_sin_indice_avisa_como_armarlo(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(B, "BUSQUEDA_DB", tmp_path / "no_existe.db")
    assert C.main(["algo"]) == 2
    assert "busqueda.py --reconstruir" in capsys.readouterr().out


def test_el_limite_recorta_pero_informa_el_total(tmp_path, monkeypatch, capsys):
    armar(tmp_path, monkeypatch)
    C.main(["prescripcion", "--limite", "1"])
    salida = capsys.readouterr().out
    assert "2 resultados" in salida and "mostrando 1" in salida
