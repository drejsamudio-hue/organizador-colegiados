import tempfile
from pathlib import Path

import config


def test_los_tests_no_apuntan_a_datos_reales():
    temporal = Path(tempfile.gettempdir()).resolve()
    for nombre in ("CONFIG_PATH", "BASE", "JUSTI_DIR", "OCR_CACHE", "OCR_DIR", "BUSQUEDA_DB", "CORPUS"):
        ruta = Path(getattr(config, nombre)).resolve()
        assert ruta.is_relative_to(temporal), f"{nombre} apunta a {ruta}"


def test_por_defecto_solo_localhost(monkeypatch, tmp_path):
    monkeypatch.delenv("ORGANIZADOR_HOSTS", raising=False)
    monkeypatch.setattr(config, "BASE", tmp_path)
    assert config.hosts_extra() == []
    assert config.hosts_permitidos() == {"localhost", "127.0.0.1"}


def test_hosts_extra_desde_variable_y_archivo(monkeypatch, tmp_path):
    (tmp_path / "_SISTEMA").mkdir()
    (tmp_path / "_SISTEMA" / "hosts_permitidos.txt").write_text(
        "# tunel del estudio\nEstudio.Example.com  # principal\n\nhttps://otro.example.com:8443/x\nlocalhost\n",
        encoding="utf-8")
    monkeypatch.setattr(config, "BASE", tmp_path)
    monkeypatch.setenv("ORGANIZADOR_HOSTS", "uno.example.com; dos.example.com:5050,estudio.example.com")
    assert config.hosts_extra() == ["uno.example.com", "dos.example.com", "estudio.example.com", "otro.example.com"]
    assert config.hosts_permitidos() == {"localhost", "127.0.0.1", "uno.example.com", "dos.example.com",
                                         "estudio.example.com", "otro.example.com"}


def test_hosts_extra_expuesto_como_lista():
    assert isinstance(config.HOSTS_EXTRA, list)
