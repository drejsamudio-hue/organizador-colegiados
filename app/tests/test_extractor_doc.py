from pathlib import Path

from docx import Document

from extractor import extraer_universal


def test_doc_que_en_realidad_es_html(tmp_path):
    p = tmp_path / "medida.doc"
    p.write_text("<html><head><style>p{color:red}</style></head><body><p>Medida autosatisfactiva &aacute; "
                 "favor de PEREZ</p><script>x()</script></body></html>", encoding="utf-8")
    texto, err = extraer_universal(p)
    assert err is None
    assert "Medida autosatisfactiva á favor de PEREZ" in texto
    assert "color" not in texto and "x()" not in texto


def test_doc_que_en_realidad_es_docx(tmp_path):
    d = Document()
    d.add_paragraph("Recurso de apelacion contra la sentencia")
    p = tmp_path / "recurso.doc"
    d.save(str(p))
    texto, err = extraer_universal(p)
    assert err is None and "Recurso de apelacion" in texto


def test_doc_temporal_de_word_no_da_texto(tmp_path):
    p = tmp_path / "~$ricion.doc"
    p.write_bytes(b"\x05Estudio Perez" + b"\x00" * 100)
    texto, err = extraer_universal(p)
    assert texto is None


def test_doc_que_no_se_puede_abrir_devuelve_error_sin_cortar(tmp_path):
    import extractor as X
    carpeta = tmp_path / "bloqueado.doc"
    carpeta.mkdir()
    texto, err = X.extraer_universal(carpeta)
    assert texto is None and err.startswith("ERROR_LECTURA")


def test_antiword_se_llama_con_la_ruta_de_config(tmp_path, monkeypatch):
    import subprocess
    import extractor as X
    f = tmp_path / "viejo.doc"
    f.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 600)
    llamados = []

    def falso(cmd, **kw):
        llamados.append(cmd[0])
        return subprocess.CompletedProcess(cmd, 0, stdout="escrito de demanda", stderr="")
    monkeypatch.setattr(X, "ANTIWORD", tmp_path / "antiword.exe")
    monkeypatch.setattr(X.subprocess, "run", falso)
    assert X.extraer_universal(f) == ("escrito de demanda", None)
    assert llamados == [str(tmp_path / "antiword.exe")]
