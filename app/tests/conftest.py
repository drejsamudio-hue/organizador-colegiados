import os
import sys
import tempfile
from pathlib import Path

os.environ["ORGANIZADOR_CONFIG"] = os.path.join(tempfile.mkdtemp(prefix="cfg_tests_"), "config.json")
os.environ.pop("ORGANIZADOR_BASE", None)
os.environ.pop("ORGANIZADOR_APELLIDOS", None)
# Sin config.json la base por defecto es ~/Organizador: la carpeta de usuario apunta a un temporal para que un hilo
# que siga corriendo despues de un monkeypatch no escriba en la base real del usuario. ORGANIZADOR_BASE no se fija
# porque marcaria el programa como ya configurado.
if not os.environ.get("TESSDATA_PREFIX") and (Path.home() / "tessdata" / "spa.traineddata").is_file():
    os.environ["TESSDATA_PREFIX"] = str(Path.home() / "tessdata")
_HOME = tempfile.mkdtemp(prefix="home_tests_")
os.environ["USERPROFILE"] = _HOME
os.environ["HOME"] = _HOME
os.environ["ORGANIZADOR_JUSTI_DIR"] = tempfile.mkdtemp(prefix="justi_tests_")
os.environ["ORGANIZADOR_OCR_CACHE"] = tempfile.mkdtemp(prefix="ocr_cache_tests_")
os.environ["ORGANIZADOR_CORPUS"] = tempfile.mkdtemp(prefix="corpus_tests_")
os.environ["ORGANIZADOR_OCR_DIR"] = tempfile.mkdtemp(prefix="ocr_tests_")
os.environ["ORGANIZADOR_BUSQUEDA_DB"] = os.path.join(tempfile.mkdtemp(prefix="busqueda_tests_"), "busqueda.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
