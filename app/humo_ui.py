"""Prueba de humo de la interfaz: recorre las secciones y el alta de cartera en un navegador real.

Usa una base sintetica (prueba_local.py, puerto 5051). Nunca toca la base ni la configuracion del usuario.
Uso: python humo_ui.py        (sale con codigo 1 si algo falla)
Requiere: pip install playwright y Microsoft Edge.
"""
from __future__ import annotations

import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

AQUI = Path(__file__).resolve().parent
URL = "http://localhost:5051"
PAGINAS = ["overview", "buscar", "expedientes", "cartera", "tandas", "aliases", "historial", "tutorial"]
ESPERA = 15_000


def _puerto_libre() -> bool:
    try:
        urllib.request.urlopen(URL + "/api/aliases", timeout=1)
        return False
    except OSError:
        return True


def _arrancar() -> subprocess.Popen:
    proc = subprocess.Popen([sys.executable, str(AQUI / "prueba_local.py"), "--limpiar"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=AQUI)
    for _ in range(40):
        try:
            urllib.request.urlopen(URL + "/api/aliases", timeout=1)
            return proc
        except OSError:
            time.sleep(0.25)
    proc.kill()
    raise SystemExit("La instancia de prueba no arranco")


def main() -> int:
    if not _puerto_libre():
        print("El puerto 5051 ya esta en uso; cerra esa instancia antes de correr la prueba.")
        return 1
    fallos: list[str] = []
    proc = _arrancar()
    try:
        with sync_playwright() as pw:
            nav = pw.chromium.launch(channel="msedge", headless=True)
            pag = nav.new_page()
            errores: list[str] = []
            externos: list[str] = []
            fallidas: list[str] = []
            pag.on("console", lambda m: errores.append(m.text) if m.type == "error" else None)
            pag.on("pageerror", lambda e: errores.append(str(e)))
            pag.on("response", lambda r: fallidas.append(f"{r.status} {r.url}") if r.status >= 400 else None)
            pag.on("request", lambda r: externos.append(r.url) if not r.url.startswith(URL) else None)

            def comprobar(nombre: str, ok: bool) -> None:
                print(("  ok   " if ok else "  FALLA ") + nombre)
                if not ok:
                    fallos.append(nombre)

            pag.goto(URL)
            for p in PAGINAS:
                pag.click(f'.nav-item[data-page="{p}"]')
                pag.wait_for_selector(f"#page-{p}.active", timeout=ESPERA)
                pag.wait_for_load_state("networkidle")
                comprobar(f"seccion {p}", pag.locator(f"#page-{p} .error-box, #page-{p} .err").count() == 0)

            pag.click('.nav-item[data-page="buscar"]')
            pag.fill("#buscarQ", "prescripcion")
            pag.press("#buscarQ", "Enter")
            pag.wait_for_selector("#buscarResultados .res-item")
            comprobar("buscar: resultado con fragmento resaltado y sin tildes en la consulta",
                      "prescripción" in pag.inner_text("#buscarResultados .res-frag mark"))
            comprobar("buscar: el HTML del documento queda como texto", pag.locator("#buscarResultados b").count() == 0)
            pag.select_option("#buscarArea", "01_Expedientes")
            pag.wait_for_function("document.getElementById('buscarResumen').textContent.includes('1 resultado')")
            comprobar("buscar: filtro por area", True)
            comprobar("buscar: ruta relativa al area",
                      pag.inner_text("#buscarResultados .res-ruta").startswith("01_Expedientes"))

            pag.click('.nav-item[data-page="expedientes"]')
            pag.wait_for_selector('#expBody tr[data-caso="2022-perez-gomez"]')
            pag.focus('#expBody tr[data-caso="2022-perez-gomez"]')
            pag.keyboard.press("Enter")
            pag.wait_for_selector("#page-buscar.active")
            comprobar("expedientes: la fila abre Buscar filtrado por la causa, sin el area anterior",
                      pag.input_value("#buscarCaso") == "2022-perez-gomez" and pag.input_value("#buscarArea") == "")
            pag.wait_for_selector("#buscarResultados .res-item")
            etiquetas = pag.locator("#buscarResultados .res-item .tag-alias").all_inner_texts()
            comprobar("expedientes: repite la consulta dentro de la causa",
                      bool(etiquetas) and all(t == "2022-perez-gomez" for t in etiquetas))
            pag.fill("#buscarCaso", "")
            pag.dispatch_event("#buscarCaso", "input")
            comprobar("buscar: sin causa vuelve el texto de ayuda original", "Frase exacta" in pag.get_attribute("#buscarQ", "placeholder"))

            pag.click('.nav-item[data-page="cartera"]')
            pag.wait_for_selector("#carteraBody tr")
            antes = pag.locator("#carteraBody tr").count()

            pag.fill("#altaExpte", "333333/2023")
            pag.fill("#altaCaratula", '<img src=x onerror="window.__xss=1"> ROBLES c/ SOSA')
            pag.click("#altaUnoBtn")
            pag.wait_for_function("document.getElementById('altaMsg').textContent.includes('Agregada')")
            pag.wait_for_function(f"document.querySelectorAll('#carteraBody tr').length === {antes + 1}")
            comprobar("alta de una causa", True)
            comprobar("caratula con HTML queda como texto",
                      pag.locator("#carteraBody img").count() == 0 and not pag.evaluate("!!window.__xss"))

            pag.click('.alta-tab[data-tab="lista"]')
            pag.fill("#altaTexto", "111111/2022\n555555/2024 LUNA c/ SOL\nxx")
            pag.click("#altaListaBtn")
            pag.wait_for_function("document.getElementById('altaMsg').textContent.includes('1 agregadas')")
            comprobar("lista: agregadas, repetidas e invalidas",
                      "1 ya estaban" in pag.inner_text("#altaMsg") and "xx" in pag.inner_text("#altaMsg"))

            pag.once("dialog", lambda d: d.accept("PAZ c/ RUIZ s/ Cobro"))
            pag.click('button[data-accion="editar"][data-expte="333333/2023"]')
            pag.wait_for_function(
                "[...document.querySelectorAll('#carteraBody tr')].some(r => r.innerText.includes('PAZ c/ RUIZ'))")
            comprobar("editar caratula", True)

            pag.once("dialog", lambda d: d.accept())
            pag.click('button[data-accion="quitar"][data-expte="555555/2024"]')
            pag.wait_for_function(
                "![...document.querySelectorAll('#carteraBody tr')].some(r => r.innerText.includes('555555/2024'))")
            comprobar("quitar causa", True)

            comprobar("sin errores de consola", not errores)
            comprobar("sin pedidos a internet", not externos)
            for e in errores:
                print("    consola:", e)
            for u in fallidas:
                print("    respuesta:", u)
            for u in externos:
                print("    externo:", u)
            nav.close()
    finally:
        proc.kill()
    print("RESULTADO:", "FALLA" if fallos else "todo bien")
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(main())
