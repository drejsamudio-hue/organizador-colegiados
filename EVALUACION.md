# Evaluación exhaustiva — Organizador de expedientes (colegiados)

Fecha: 2026-11-23 · Rama: `arena/3c06a102-organizador-colegiados` (base `9673a44`)
Alcance: revisión de código completa (~14.000 líneas Python + 2.000 de frontend), suite de tests,
pruebas funcionales contra la app corriendo (API + flujo de punta a punta), seguridad, rendimiento,
concurrencia y robustez de datos.

---

## 1. Resumen ejecutivo

La app está **por encima de la media** en lo que más importa en una herramienta que mueve archivos:
no pisa ni borra nada, registra cada movimiento, todo se puede deshacer, valida el hash antes de mover,
escapa el HTML en el frontend y trae una suite de tests muy densa (498 casos, 88 % de cobertura en los
módulos centrales). El diseño defensivo está pensado de verdad, no de adorno.

Encontré **2 defectos reales y los corregí** (uno de ellos hacía que ciertos archivos con comillas en el
texto no se pudieran mover nunca; el otro era un escape de la base en Linux/macOS), más una lista de
mejoras y riesgos menores documentados aquí. Los 11 tests que quedan en rojo en Linux **no son bugs del
programa**: son tests escritos contra el comportamiento de Windows, como se explica en la sección 4.

| Área | Estado |
|---|---|
| Integridad de archivos (mover/deshacer) | Excelente — sin pisar, sin borrar, con hash y registro |
| Seguridad web | Buena — anti DNS-rebinding, anti-CSRF, XSS escapado, sin inyección SQL |
| Manejo de errores / datos corruptos | Muy bueno — respaldos, escritura atómica, nunca pisa un JSON ilegible |
| Concurrencia | Buena — bloqueo entre procesos, aunque hay algún hueco menor (ver 5.2) |
| Rendimiento | Correcto hasta ~20.000 causas / 2.000 archivos; `/api/propuestas` escala mal (5.3) |
| Tests | Muy buenos, pero no son portables a Linux (los 11 en rojo) |
| Portabilidad | Windows-only en varios puntos clave (5.1) |
| Código muerto / higiene | Menor: imports sin usar, módulos heredados sueltos |

---

## 2. Defectos corregidos en esta revisión

### 2.1 Crítico — un extracto con comillas desalineaba el CSV de la simulación

**Dónde:** `app/dashboard.py`, `_extracto()` (usado por `_simular` y `/api/fuera/ajena`).

**Qué pasaba.** El extracto (los primeros 160 caracteres del documento) se guarda como una columna del
CSV `REORG_*.csv`, junto al `hash` que identifica al archivo. El recorte se hacía *después* de juntar el
texto, sin mirar las comillas. Si el corte caía entre las dos comillas de un entrecomillado
(`… s/ "DANOS Y PERJUICIOS", con costas`), el CSV guardaba `"… s/ ""DANOS Y PERJUICIOS"", con costas`
— **un campo abierto y mal cerrado** — y al releerlo el parser se comía la línea siguiente.

Consecuencia real, reproducida contra la app corriendo: la fila quedaba con `hash = ' con costas'`
(o con el hash de la fila de al lado) y el archivo **no se podía mover nunca**: la interfaz mostraba
“Mover 1” y al aplicar respondía `[cambio desde la simulación, queda en origen]`. Con el agravante de
que el mensaje culpaba al usuario de haber modificado un archivo que no había tocado. Textos con
comillas son lo normal en escritos judiciales (`"DANOS Y PERJUICIOS"`, pulgadas en pericias, citas).

**Arreglo.** El extracto pasa las comillas rectas a tipográficas (`«»`) antes de recortar, y el recorte
ya no parte un par por la mitad. Se conserva el contenido y el CSV queda siempre bien formado.

**Verificación.** Dos tests nuevos en `test_dashboard_verificacion.py` y reproducción de punta a punta
contra el servidor: simular → revisar → aplicar → el archivo se mueve y queda registrado.

### 2.2 Alto — `_resolver_destino` no bloqueaba `..` escrito con barra invertida

**Dónde:** `app/dashboard.py`, `_resolver_destino()`.

**Qué pasaba.** El control anti-escape era `if ".." in p.parts`. En Linux y macOS, `Path("01_Expedientes\..\..\x")`
es **un solo componente** (la barra invertida es un carácter normal), así que el chequeo no lo veía y el
destino se resolvía como `BASE/01_Expedientes\..\..\x`. Hoy el endpoint igual devolvía 400 porque después
compara con `is_relative_to(base)`, pero era una defensa en profundidad que no funcionaba y dejaba un test
del propio repo en rojo. **Arreglo:** se separan las dos barras a mano, se rechaza `..` en cualquier
forma, se rechaza `\x00` y se normalizan los `.` internos. También quedó verificado que un enlace simbólico
dentro de `01_Expedientes` que apunte a `/etc` sigue rechazándose (`resolve()` + `is_relative_to`).

---

## 3. Qué está bien (y conviene no tocar)

**Integridad de datos — el corazón de la app.**
- No pisa nunca: `renameat2(RENAME_NOREPLACE)` en Linux, `os.rename` en Windows, `link+unlink` como
  reserva; ante colisión prueba `nombre__1`, `__2`… y avisa.
- Entre unidades (pendrive, otro disco) copia a `.~<azar>.incompleto` (que la búsqueda no indexa),
  hace `fsync`, verifica sha256, comprueba que el origen no cambió y **recién entonces** borra.
  Si algo falla, nunca quedan cero ejemplares y lo que no se puede limpiar queda nombrado en el error.
- Toda la ruta de escritura de JSON/CSV es atómica (temporal en la misma carpeta + `replace`) y con
  respaldo previo (últimas 20 versiones).
- Un `cartera.json` ilegible **no se toma por vacío**: la app se niega a guardar para no pisar datos.
- El registro de movimientos guarda tamaño y `mtime_ns`: deshacer se saltea lo que el usuario modificó
  después. Verifiqué además que el *doble deshacer* de la misma tanda no mueve nada y avisa.
- La zona del sistema (`_SISTEMA`) nunca entra en una tanda, ni como origen ni como destino.

**Seguridad web** (probado con `curl` contra el servidor real):
- Host no permitido → 403 (anti DNS-rebinding), `Origin` cruzado → 403, `Sec-Fetch-Site` cruzado → 403.
- `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`.
- Las 62 interpolaciones del frontend pasan por `esc()`; no hay `innerHTML` con datos crudos.
  El resaltado de búsqueda escapa *antes* de insertar `<mark>`: correcto.
- FTS5 con parámetros enlazados; los únicos f-strings con SQL son nombres de columna de una lista fija.
- Subida de archivos (`/api/soltar`): `Path(...).name` neutraliza `..`, se rechazan nombres reservados
  de Windows (`CON`, `PRN`, `COM1`…) y caracteres inválidos. Probado: `../../evil.txt` cae dentro de la base.
- OCR: sólo https, URL fijada a un commit de Tesseract, tope de tamaño y **sha256 conocido**.
- `/api/buscar/abrir` sólo abre rutas **que están en el índice** y existen.

**Robustez funcional:** 30 movimientos aplicados en paralelo con dos “Aplicar” simultáneos → 30 movidos y
0 duplicados, la segunda tanda avisa “ya no está” en cada fila. Deshacer 30 archivos → 30 restaurados.
20 altas de cartera concurrentes → las 20 quedaron guardadas, ninguna pisó a otra.
Causas sin carpeta, nombres con `:`, `..`, o caracteres que el registro no puede codificar: se rechazan
sin romper nada. Búsqueda sin tildes, prefijos y filtros: OK.

---

## 4. Los 11 tests en rojo en Linux no son bugs del programa

Los 11 son de `test_ingesta.py` y todos giran alrededor de “mover desde otra unidad” y del borrado del
origen. La causa es la misma en todos: **simulan un pendrive parcheando `os.rename`**, pero en Linux el
código no usa `os.rename` sino `renameat2` vía `ctypes` (por diseño, para no pisar). Comprobado:

```
os.rename llamado: False | dst existe: True
=> en Linux se usa renameat2(RENAME_NOREPLACE); el monkeypatch de os.rename no tiene efecto
```

Es decir: en Linux nunca se simula el cruce de unidad, se ejecuta el camino normal (mismo disco) y las
asercciones de Windows (`PermissionError` por archivo abierto, `chmod` de solo lectura, `errno 1167`)
no aplican. En Windows, que es el sistema objetivo declarado en el README, estos tests sí ejercitan lo
que dicen.

**Recomendación** (no lo cambié porque no es un defecto del producto y afecta a 11 tests a la vez):
parchear `I.os.rename` **y** `I._renombrar_sin_pisar` en el helper, o marcar ese bloque con
`@pytest.mark.skipif(os.name != "nt")` como ya se hizo en `test_mover_un_archivo_abierto_falla_sin_dejar_copia`
(línea 549). Así la suite queda verde en los dos sistemas y se ve de un vistazo qué se prueba en cada uno.

Resultado medido: **477 pasan, 11 fallan (sólo por lo anterior), 17 omitidos**; cobertura 87 % en
`dashboard.py`, 88 % en `ingesta.py`, 92 % en `busqueda.py`, 99 % en `cartera.py`.

---

## 5. Hallazgos que conviene atender (no corregidos)

### 5.1 Portabilidad — la app no arranca fuera de Windows en la práctica
- `_lanzar()` usa `subprocess.Popen(["explorer", ...])` y `os.startfile`: en Linux/macOS “Abrir”/“Carpeta”
  devuelve 400 (verificado). Aceptable si el destino es Windows, pero el README y la UI no lo dicen.
- `OCSP`/`antiword` asume `C:\Program Files\Git\mingw64\bin\antiword.exe`; en otra ruta los `.doc` viejos
  caen a lectura binaria (está previsto y avisa, correcto).

### 5.2 Concurrencia — huecos menores
- `_simulaciones` es un dict de proceso sin tope: cada simulación vive hasta reiniciar. Con muchas
  simulaciones grandes, la memoria crece sin límite. Sugerencia: descartar las de más de N horas o
  guardar sólo el estado en disco.
- El estado `"corriendo"` de una simulación se pierde si el proceso muere: la UI queda consultando para
  siempre. Convendría un `timeout` en el `while` del frontend (hoy no lo tiene).
- `/api/simular` no valida que la carpeta esté fuera de la base: simular `01_Expedientes` recorre todo y
  propone movimientos internos. No es peligroso (los destinos se validan al aplicar), pero es ruido fácil
  de evitar con un aviso.

### 5.3 Rendimiento
Medido con 20.000 causas en cartera, 2.000 movimientos y 2.100 archivos:

| Endpoint | Tiempo |
|---|---|
| `/api/cartera` | 0,08 s |
| `/api/historial` | 0,05 s |
| `/api/stats` | 0,07 s |
| `/api/buscar` | 0,002 s |
| `/api/propuestas` (200 propuestas × 100 filas) | **0,28 s y creciendo** |

`/api/propuestas` relee **todos** los CSV de simulación en cada carga de la pestaña Tandas y hace un
`is_file()` por fila. Con años de uso eso se vuelve perceptible. Sugerencia: cachear por
(nombre, mtime) y paginar la lista, o archivar las propuestas ya aplicadas.

### 5.4 Detalles de UI / robustez
- `subirArchivos()` y `btnSimular`: `while (s.estado === 'corriendo')` sin límite. Si el backend se cae,
  la UI gira para siempre.
- `_entero()` acepta `limite` negativo y lo convierte en 1 en vez de rechazarlo (cosmético).
- `/api/cartera/escanear` acepta cualquier carpeta absoluta del disco (por diseño, pero conviene que la
  UI lo advierta: en una máquina compartida se puede listar todo el disco).

### 5.5 Higiene de código
- `ruff`: 14 avisos reales, todos triviales — imports sin usar (`os` en `extractor.py`, `BASE` y
  `OCR_MINIMO` en `busqueda.py`), un f-string sin placeholders y un **import duplicado de `mover`**
  en `dashboard.py:39` que luego se redefine. Vale limpiarlos: el import muerto sugiere que se dejó de
  usar una función y nadie lo notó.
- `hashlib.sha1` en `extractor._clave_ocr`: es una clave de caché, no seguridad — conviene
  `usedforsecurity=False` para que Bandit no lo marque y quede explícita la intención.
- `fase2_expedientes.py` y `fase3_juris.py` conservan `shutil.move` en funciones que aparentemente no se
  usan desde el dashboard. Si están muertas, borrarlas reduce superficie: son las únicas que moverían
  archivos sin pasar por las garantías de `ingesta.mover`.
- `fase6b_renombres.py` está vacío por diseño (heredado); documentarlo o eliminarlo.

---

## 6. Cómo reproduje todo

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest app/tests -q            # 477 ok / 11 fallas de portabilidad / 17 omitidos
.venv/bin/python -m coverage run -m pytest app/tests && .venv/bin/python -m coverage report
.venv/bin/python -m bandit -r app -x app/tests     # sin hallazgos altos reales
ORGANIZADOR_CONFIG=/tmp/cfg/config.json .venv/bin/python app/dashboard.py --port 5050 --no-abrir
```

Sobre el servidor real se probaron: host/Origin falsos (403), path traversal por API y por CSV,
enlace simbólico a `/etc`, subida con `../` y nombres reservados, aplicar en paralelo, deshacer y
doble deshacer, 20 altas concurrentes de cartera, simulación + aplicación + deshacer de punta a punta,
búsqueda FTS con y sin tildes, y carga con 20.000 causas / 2.100 archivos.

---

## 7. Veredicto

**Listo para usar con la copia de seguridad que el propio README recomienda.** Los dos defectos
corregidos eran los únicos que podían hacer “perder” un archivo desde el punto de vista del usuario
(quedaba en el origen sin poder moverse, con un mensaje engañoso) y el único control de seguridad que
no funcionaba como decía. Lo que queda es deuda técnica sana de atender: portabilidad de los tests,
tope a las simulaciones, caché de propuestas y limpieza de imports.
