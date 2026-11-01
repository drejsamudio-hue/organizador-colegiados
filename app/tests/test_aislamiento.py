import tempfile
from pathlib import Path

import config


def test_los_tests_no_apuntan_a_datos_reales():
    temporal = Path(tempfile.gettempdir()).resolve()
    for nombre in ("CONFIG_PATH", "BASE", "JUSTI_DIR", "OCR_CACHE", "OCR_DIR", "BUSQUEDA_DB", "CORPUS"):
        ruta = Path(getattr(config, nombre)).resolve()
        assert ruta.is_relative_to(temporal), f"{nombre} apunta a {ruta}"
