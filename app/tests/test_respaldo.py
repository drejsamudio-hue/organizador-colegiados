import os
import subprocess
import sys
import time
from pathlib import Path

import respaldo as R


def test_respalda_una_copia_con_fecha_y_no_repite_si_no_cambio(tmp_path):
    f = tmp_path / "cartera.json"
    f.write_text("uno", encoding="utf-8")
    a = R.respaldar(f)
    assert a is not None and a.read_text(encoding="utf-8") == "uno" and a.parent == tmp_path / "_respaldos"
    assert R.respaldar(f) is None
    f.write_text("dos", encoding="utf-8")
    b = R.respaldar(f)
    assert b is not None and b != a and len(list((tmp_path / "_respaldos").iterdir())) == 2


def test_conserva_solo_las_ultimas(tmp_path):
    f = tmp_path / "cartera.json"
    for i in range(7):
        f.write_text(str(i), encoding="utf-8")
        R.respaldar(f, conservar=3)
    quedan = sorted((tmp_path / "_respaldos").iterdir())
    assert len(quedan) == 3
    assert [p.read_text(encoding="utf-8") for p in quedan] == ["4", "5", "6"]


def test_sin_archivo_no_hace_nada(tmp_path):
    assert R.respaldar(tmp_path / "no_existe.json") is None
    assert not (tmp_path / "_respaldos").exists()


def test_no_mezcla_respaldos_de_archivos_distintos(tmp_path):
    for n in ("a.json", "b.json"):
        (tmp_path / n).write_text(n, encoding="utf-8")
        R.respaldar(tmp_path / n, conservar=1)
    assert len(list((tmp_path / "_respaldos").iterdir())) == 2


def test_guardar_cartera_respalda_la_version_anterior(tmp_path):
    from cartera import Causa, guardar, normalizar_expte
    ruta = tmp_path / "cartera.json"
    guardar([Causa(normalizar_expte("111111/2022"))], ruta)
    assert not (tmp_path / "_respaldos").exists()
    guardar([Causa(normalizar_expte("111111/2022")), Causa(normalizar_expte("222222/2023"))], ruta)
    (previo,) = list((tmp_path / "_respaldos").iterdir())
    assert "111111/2022" in previo.read_text(encoding="utf-8") and "222222/2023" not in previo.read_text(encoding="utf-8")


def test_bundle_de_codigo_se_puede_verificar(tmp_path):
    repo = Path(__file__).resolve().parent.parent
    r = subprocess.run([sys.executable, str(repo / "respaldo.py"), "--codigo", str(tmp_path)],
                       capture_output=True, text=True, cwd=repo)
    assert r.returncode == 0, r.stderr
    (bundle,) = list(tmp_path.glob("organizador-*.bundle"))
    v = subprocess.run(["git", "bundle", "verify", str(bundle)], capture_output=True, text=True, cwd=repo)
    assert v.returncode == 0
