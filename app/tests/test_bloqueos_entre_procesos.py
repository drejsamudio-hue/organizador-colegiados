"""Bloqueos entre procesos: el panel respeta el bloqueo de la cartera que toma la consola, y el bloqueo de las tandas
sigue trabando el mismo byte que las versiones anteriores (un .exe viejo y uno nuevo sobre la misma base se excluyen)."""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import dashboard as D  # noqa: E402
import ingesta as I  # noqa: E402

RAIZ_APP = Path(I.__file__).resolve().parent


def _hijo(codigo: str, *args: str) -> subprocess.Popen:
    hijo = subprocess.Popen([sys.executable, "-c", textwrap.dedent(codigo), str(RAIZ_APP), *args],
                            stdout=subprocess.PIPE, text=True)
    assert hijo.stdout.readline().strip() == "listo"
    return hijo


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    sistema = tmp_path / "_SISTEMA"
    sistema.mkdir()
    monkeypatch.setattr(I, "SISTEMA", sistema)
    monkeypatch.setattr(D, "LOG_FILE", sistema / "dashboard_log.json")
    monkeypatch.setattr(D, "ESPERA_CARTERA", 0.3)
    D.app.config["TESTING"] = True
    return D.app.test_client()


def test_el_panel_no_modifica_la_cartera_que_otro_proceso_tiene_tomada(tmp_path, cliente):
    ruta = tmp_path / "_SISTEMA" / "cartera.json"
    hijo = _hijo("""
        import sys, time
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from cartera import bloqueada
        with bloqueada(Path(sys.argv[2])):
            print("listo", flush=True)
            time.sleep(30)
    """, str(ruta))
    try:
        r = cliente.post("/api/cartera", json={"expte": "333333/2023", "caratula": "ROBLES c/ SOSA"})
        assert r.status_code == 409 and "otro proceso" in r.get_json()["error"]
        assert not ruta.exists()
    finally:
        hijo.kill()
        hijo.wait()
    assert cliente.post("/api/cartera", json={"expte": "333333/2023"}).status_code == 200


def test_el_bloqueo_de_tandas_excluye_a_una_version_anterior_que_traba_el_byte_cero(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "RUTEO_CSV", tmp_path / "_SISTEMA" / "INGESTA_RUTEO.csv")
    lock = tmp_path / "_SISTEMA" / "ingesta.lock"
    lock.parent.mkdir(parents=True)
    hijo = _hijo("""
        import os, sys, time
        f = open(sys.argv[2], "a+b")
        f.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("listo", flush=True)
        time.sleep(30)
    """, str(lock))
    try:
        with pytest.raises(I.TandaEnCurso):
            with I.bloqueo_tanda(espera=0.3):
                pass
    finally:
        hijo.kill()
        hijo.wait()
    with I.bloqueo_tanda(espera=5):  # el pid de quien lo tiene se sigue pudiendo leer para el aviso
        assert I._pid_del_bloqueo(lock) == os.getpid()
