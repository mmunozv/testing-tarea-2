"""REPARADOR: modifica el código fuente de repo/ para que los tests del bug pasen.

Uso por separado (con una localización ya guardada):
    python reparador.py <carpeta_del_bug> <localizacion.json>

Recibe:
  carpeta       Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info          contenido de bug.json como dict
  localizacion  lo que devolvió localizar() (ver localizador.py)

Hace: deja la reparación aplicada en carpeta/repo/. Solo puede cambiar código fuente, no tests.

Ciclo de cada intento (piezas 4 a 7):
  contexto acotado (regiones + test + falla + intentos previos) -> LLM -> bloques BUSCAR/REEMPLAZAR
  -> aplicar (solo a código fuente) -> sintaxis -> tests objetivo -> regresión en modulos_test
  -> si algo falla: revertir y reintentar con esa retroalimentación.
Al final queda aplicada la mejor reparación encontrada (o el código original si ninguna sirve).

Devuelve un dict con, al menos:
  {"resuelto": true}      # true si los tests objetivo pasan al terminar
"""
import json
import re
import sys
import tempfile
import threading
from pathlib import Path

import codigo
import llm
import pruebas
import tiempo

MAX_LINEAS_CODIGO = 450      # líneas de código fuente por prompt (todas las regiones juntas)
MAX_LINEAS_REGION = 160      # una región más larga se muestra como ventana alrededor del punto caliente
MAX_INTENTOS = 15           # tope de seguridad; en la práctica manda el tiempo (TIEMPO_MIN_INTENTO)
TIEMPO_MIN_INTENTO = 25.0    # no empezar un intento si queda menos que esto
MAX_LINEAS_DATOS = 60        # por archivo de datos de test

# estado compartido con el vigilante de main.py (para dejar el repo consistente si se acaba el tiempo)
_lock = threading.RLock()
_estado = {"repo": None, "tocados": set(), "mejor": None}


# --------------------------------------------------------------------------- manejo del repo

def _revertir(repo: Path, archivos) -> None:
    if archivos:
        pruebas.git(repo, "checkout", "--", *archivos)
    codigo.olvidar_cache()


def _escribir(repo: Path, cambios: dict) -> None:
    for archivo, texto in cambios.items():
        (repo / archivo).write_text(texto)
    codigo.olvidar_cache()


def dejar_estado_final() -> dict:
    """Deja en repo/ la mejor reparación encontrada (o el original). Usado también por el vigilante."""
    with _lock:
        repo = _estado["repo"]
        if repo is None:
            return {}
        _revertir(repo, sorted(_estado["tocados"]))
        mejor = _estado["mejor"]
        if mejor:
            _escribir(repo, mejor["cambios"])
            return mejor
        return {}


# --------------------------------------------------------------------------- formato de respuesta

RE_ARCHIVO = re.compile(r"^\s*[#*`>\s-]*(?:ARCHIVO|FILE|Archivo|File)\s*[:=]?\s*[*`]*\s*([\w./\\-]+\.py)")


def parsear_ediciones(texto: str):
    """Extrae bloques:
        ARCHIVO: ruta.py
        <<<<<<< BUSCAR
        ...
        =======
        ...
        >>>>>>> REEMPLAZAR
    Devuelve [(archivo|None, buscar, reemplazar)]."""
    ediciones, archivo, estado, buscar, reemplazar = [], None, None, [], []
    for linea in texto.splitlines():
        if estado is None:
            m = RE_ARCHIVO.match(linea)
            if m:
                archivo = re.sub(r"^\./", "", m.group(1).replace("\\", "/"))
            elif linea.lstrip().startswith("<<<<<<<"):
                estado, buscar = "buscar", []
        elif estado == "buscar":
            if linea.lstrip().startswith("=======") and set(linea.strip()) == {"="}:
                estado, reemplazar = "reemplazar", []
            else:
                buscar.append(linea)
        elif estado == "reemplazar":
            if linea.lstrip().startswith(">>>>>>>"):
                ediciones.append((archivo, "\n".join(buscar), "\n".join(reemplazar)))
                estado = None
            else:
                reemplazar.append(linea)
    return ediciones


class ErrorAplicar(Exception):
    pass


def _indent(s: str) -> str:
    return s[:len(s) - len(s.lstrip())]


def _aplicar_en_texto(texto: str, buscar: str, reemplazar: str, preferidas=()) -> str:
    """Reemplaza `buscar` por `reemplazar`. Tolera espacios finales e indentación distinta."""
    if not buscar.strip():
        raise ErrorAplicar("el bloque BUSCAR está vacío")
    # 1) coincidencia exacta
    n = texto.count(buscar)
    if n == 1:
        return texto.replace(buscar, reemplazar, 1)
    lineas = texto.split("\n")
    b_lineas = buscar.strip("\n").split("\n")
    r_lineas = reemplazar.strip("\n").split("\n") if reemplazar.strip() else []

    def buscar_por(norm):
        objetivo = [norm(l) for l in b_lineas]
        k = len(objetivo)
        return [i for i in range(len(lineas) - k + 1)
                if all(norm(lineas[i + j]) == objetivo[j] for j in range(k))]

    # 2) por líneas ignorando espacios finales; 3) ignorando también la indentación
    for norm, reindentar in ((str.rstrip, False), (str.strip, True)):
        pos = buscar_por(norm)
        if len(pos) > 1 and preferidas:   # varias: la que cae en una región sospechosa
            dentro = [i for i in pos if any(a <= i + 1 <= b for a, b in preferidas)]
            pos = dentro if len(dentro) == 1 else pos
        if len(pos) == 1:
            i = pos[0]
            nuevas = r_lineas
            if reindentar:
                delta_real, delta_mod = _indent(lineas[i]), _indent(b_lineas[0])
                nuevas = []
                for l in r_lineas:
                    if l.startswith(delta_mod):
                        nuevas.append(delta_real + l[len(delta_mod):] if l.strip() else l)
                    else:
                        nuevas.append(delta_real + l.lstrip() if l.strip() else l)
            return "\n".join(lineas[:i] + nuevas + lineas[i + len(b_lineas):])
        if len(pos) > 1:
            raise ErrorAplicar(f"el bloque BUSCAR aparece {len(pos)} veces; incluye más líneas de contexto "
                               f"para que sea único:\n{buscar[:300]}")
    raise ErrorAplicar(f"el bloque BUSCAR no coincide con el código actual:\n{buscar[:400]}")


def _resolver_archivo(repo: Path, archivo, buscar: str, mostrados):
    """Determina a qué archivo fuente aplica la edición."""
    fuentes = None
    if archivo:
        archivo = archivo.strip()
        if pruebas.es_test(archivo):
            raise ErrorAplicar(f"{archivo} es un archivo de test: solo se puede modificar código fuente")
        if (repo / archivo).is_file() and pruebas.es_fuente(archivo):
            return archivo
        fuentes = codigo.archivos_fuente(repo)
        sufijo = [f for f in fuentes if f.endswith("/" + archivo) or f == archivo]
        if len(sufijo) == 1:
            return sufijo[0]
    # sin archivo (o no encontrado): el archivo mostrado donde aparece el texto
    clave = buscar.strip().split("\n")[0].strip()
    en = [a for a in mostrados if clave and clave in codigo.leer(repo, a)]
    if len(en) == 1:
        return en[0]
    raise ErrorAplicar(f"no se pudo determinar el archivo de la edición ({archivo or 'sin ARCHIVO'})")


def aplicar_ediciones(repo: Path, ediciones, mostrados, rangos):
    """Aplica todas las ediciones en memoria y devuelve {archivo: texto_nuevo} (sin escribir)."""
    nuevos = {}
    for archivo, buscar, reemplazar in ediciones:
        archivo = _resolver_archivo(repo, archivo, buscar, mostrados)
        texto = nuevos.get(archivo, codigo.leer(repo, archivo))
        preferidas = [(a, b) for (f, a, b) in rangos if f == archivo]
        nuevos[archivo] = _aplicar_en_texto(texto, buscar, reemplazar, preferidas)
    return {a: t for a, t in nuevos.items() if t != codigo.leer(repo, a)}


def _error_sintaxis(repo: Path, archivos):
    """Compila los archivos con el Python del bug (3.8): detecta sintaxis inválida para esa versión."""
    lista = " ".join(f"'{a}'" for a in archivos)
    codigo_salida, out, _ = pruebas.ejecutar(
        f"python -c \"import sys\nfor f in sys.argv[1:]: compile(open(f, 'rb').read(), f, 'exec')\" {lista}",
        repo, tiempo.acotar(20, 2))
    if codigo_salida != 0:
        return out.strip()[-1500:]
    return None


# --------------------------------------------------------------------------- contexto (prompt)

PROMPT_REPARAR = """Eres un ingeniero experto en Python. Hay un bug en el código fuente de este \
proyecto: los tests de abajo fallan. Corrige el código fuente (NUNCA los tests) para que pasen, \
sin romper otros comportamientos del proyecto.

## Test(s) que deben pasar
{tests}
{datos}
## Salida actual de los tests (con el código original)
```
{salida}
```

## Código fuente sospechoso (según el localizador, de más a menos sospechoso)
{regiones}
{previos}
## Instrucciones
1. Primero explica en 2-4 frases cuál es la causa del bug.
2. Luego entrega el cambio MÍNIMO necesario como uno o más bloques con este formato exacto:

ARCHIVO: ruta/relativa/al/archivo.py
<<<<<<< BUSCAR
líneas copiadas EXACTAMENTE del código actual (con su indentación), las justas para ser únicas
=======
líneas que las reemplazan
>>>>>>> REEMPLAZAR

- El texto de BUSCAR debe existir tal cual en el archivo indicado (no incluyas números de línea).
- Mantén el estilo del código y la compatibilidad con Python 3.8.
- No modifiques archivos de test. Puedes modificar cualquier archivo fuente mostrado.
"""


def _ventana(repo, region, calientes):
    """Rango de líneas a mostrar de una región: completa si es corta; si no, una ventana."""
    a, b = region["inicio"], region["fin"]
    if b - a + 1 <= MAX_LINEAS_REGION:
        return a, b
    dentro = sorted(l for (f, l) in calientes if f == region["archivo"] and a <= l <= b)
    centro = dentro[-1] if dentro else a + MAX_LINEAS_REGION // 2
    ini = max(a, centro - MAX_LINEAS_REGION // 2)
    return ini, min(b, ini + MAX_LINEAS_REGION - 1)


def _seleccionar_regiones(regiones, intento):
    """Qué regiones se muestran: las 3 más sospechosas, luego 5 y luego todas (con el presupuesto
    de líneas), para ampliar el contexto si el primer intento no bastó. Las cabeceras de clase
    se muestran siempre que su método esté entre las elegidas."""
    n = (3, 5, 8)[intento % 3]
    vistas, res = set(), []
    for r in regiones:
        clave = (r["archivo"], r["inicio"], r["fin"])
        # evita mostrar una región contenida en otra ya elegida
        if any(f == r["archivo"] and a <= r["inicio"] and r["fin"] <= b for f, a, b in vistas):
            continue
        vistas.add(clave)
        res.append(r)
        if len(res) >= n:
            break
    for r in regiones:
        clase = r.get("nombre", "").split(" (")[0] + "."
        if r.get("origen") == "clase" and r not in res and any(
                x["archivo"] == r["archivo"] and x.get("nombre", "").startswith(clase) for x in res):
            res.insert(1, r)
    return res


def _construir_regiones(repo, regiones, calientes):
    """Texto con el código de las regiones, respetando el presupuesto de líneas."""
    bloques, rangos, usadas = [], [], 0
    for r in regiones:
        ini, fin = _ventana(repo, r, calientes)
        disponibles = MAX_LINEAS_CODIGO - usadas
        if disponibles < 15:
            break
        fin = min(fin, ini + disponibles - 1)
        texto = codigo.fragmento(repo, r["archivo"], ini, fin)
        recorte = " (fragmento de una función más larga)" if (ini, fin) != (r["inicio"], r["fin"]) else ""
        bloques.append(f"### {r['archivo']} — líneas {ini}-{fin}, {r.get('nombre', '')}{recorte}\n"
                       f"```python\n{texto}\n```")
        rangos.append((r["archivo"], ini, fin))
        usadas += fin - ini + 1
    return "\n\n".join(bloques), rangos


def _datos_test(repo, info, objetivos_test):
    """Archivos de datos de test relacionados (p. ej. entradas/salidas esperadas), recortados."""
    modulos = {o.archivo for o in objetivos_test} | set(info.get("modulos_test", []))
    partes = []
    for a in info.get("archivos_test", []):
        if a in modulos or not (repo / a).is_file():
            continue
        lineas = codigo.leer(repo, a).splitlines()
        texto = "\n".join(lineas[:MAX_LINEAS_DATOS])
        if len(lineas) > MAX_LINEAS_DATOS:
            texto += f"\n[... {len(lineas) - MAX_LINEAS_DATOS} líneas más ...]"
        partes.append(f"### {a}\n```\n{texto}\n```")
        if len(partes) >= 2:
            break
    return ("\n## Archivos de datos del test\n" + "\n\n".join(partes) + "\n") if partes else ""


def _texto_previos(historial):
    if not historial:
        return ""
    partes = ["\n## Intentos anteriores que NO funcionaron (no los repitas; aprende de ellos)"]
    for i, h in enumerate(historial[-3:], 1):
        partes.append(f"### Intento {i}: {h['problema']}")
        if h.get("diff"):
            partes.append(f"Cambio aplicado:\n```diff\n{h['diff']}\n```")
        if h.get("detalle"):
            partes.append(f"Resultado:\n```\n{h['detalle']}\n```")
    return "\n".join(partes) + "\n"


def _diff(repo, archivos, max_lineas=50):
    d = pruebas.git(repo, "diff", "-U2", "--", *archivos)
    lineas = [l for l in d.splitlines() if not l.startswith(("diff --git", "index "))]
    if len(lineas) > max_lineas:
        lineas = lineas[:max_lineas] + ["[... diff recortado ...]"]
    return "\n".join(lineas)


# --------------------------------------------------------------------------- verificación

def _regresiones(repo, info, linea_base, dir_tmp, cambios):
    """Ejecuta los módulos de test con el cambio aplicado. Devuelve (regresiones, salida) o
    (None, motivo) si no se pudo determinar a tiempo."""
    modulos = info.get("modulos_test", [])
    if not modulos:
        return set(), ""
    timeout = tiempo.restante() - 6
    if timeout < 10:
        return None, "sin tiempo para ejecutar los módulos de test"
    estados, salida, _ = pruebas.correr_modulos(repo, modulos, timeout, dir_tmp)
    if estados is None:
        return None, "los módulos de test no terminaron a tiempo"
    if linea_base:
        regs = {t for t, e in linea_base.items() if e == "pasa" and estados.get(t, "falla") == "falla"}
    else:
        # sin línea base: los que fallan ahora se ejecutan con el código original
        fallan = {t for t, e in estados.items() if e == "falla"}
        if not fallan:
            return set(), salida
        if tiempo.restante() < 15:
            return None, "sin tiempo para comparar con el código original"
        with _lock:
            _revertir(repo, list(cambios))
            antes = pruebas.correr_tests_por_id(repo, fallan, tiempo.acotar(60, 6), dir_tmp)
            _escribir(repo, cambios)
        regs = {t for t in fallan if antes.get(t, "pasa") == "pasa"}
    return regs, salida


def _detalle_regresiones(regs, salida):
    """Nombres de los tests rotos y un extracto de su error."""
    nombres = sorted(regs)[:8]
    texto = "Tests que pasaban antes y ahora fallan (regresiones):\n" + "\n".join(f"- {n}" for n in nombres)
    extracto = pruebas.recortar_salida(salida, 1800)
    return texto + "\n\nExtracto de la salida:\n" + extracto


# --------------------------------------------------------------------------- etapa

def reparar(carpeta: Path, info: dict, localizacion: dict) -> dict:
    repo = carpeta / "repo"
    dir_tmp = localizacion.get("_dir_tmp") or tempfile.mkdtemp(prefix="agente_")
    objetivos_test = pruebas.objetivos(info, repo)
    regiones = [r for r in localizacion.get("regiones", []) if pruebas.es_fuente(r.get("archivo", ""))]
    with _lock:
        _estado.update(repo=repo, tocados=set(), mejor=None)

    salida = localizacion.get("_salida")
    if salida is None:
        res0 = pruebas.correr_objetivo(repo, info)
        if res0.pasa:
            return {"resuelto": True, "intentos": 0, "nota": "los tests objetivo ya pasaban"}
        salida = res0.salida
    linea_base = localizacion.get("_linea_base")
    calientes = [(f.archivo, f.linea) for f in pruebas.frames(salida, repo)]
    tests_txt = "\n\n".join(codigo.fuente_test(repo, o) for o in objetivos_test) or \
        "\n".join(info.get("comandos_test", []))
    datos_txt = _datos_test(repo, info, objetivos_test)
    salida_txt = pruebas.recortar_salida(salida, 4500)

    historial, intento, fallos_api = [], 0, 0

    class Historial(list):
        def append(self, h):
            print(f"[reparador] intento fallido: {h['problema']}\n{str(h.get('detalle', ''))[:300]}")
            super().append(h)
    historial = Historial()
    resultado = {"resuelto": False, "intentos": 0}
    while intento < MAX_INTENTOS and tiempo.restante() > TIEMPO_MIN_INTENTO:
        intento += 1
        mostradas = _seleccionar_regiones(regiones, intento - 1)
        regiones_txt, rangos = _construir_regiones(repo, mostradas, calientes)
        prompt = PROMPT_REPARAR.format(tests=tests_txt[:6000], datos=datos_txt, salida=salida_txt,
                                       regiones=regiones_txt or "(no se encontraron regiones)",
                                       previos=_texto_previos(historial))
        temperatura = 0.2 if intento == 1 else min(1.0, 0.4 + 0.15 * intento)
        print(f"[reparador] intento {intento}: {len(mostradas)} regiones, "
              f"~{len(prompt) // 4} tokens de prompt, temperatura {temperatura:.2f}")
        texto = llm.generar(prompt, "reparador", temperatura=temperatura, max_tokens=4096, reserva=15)
        if texto is None:
            fallos_api += 1
            if fallos_api >= 3 or not llm.disponible():
                break
            continue

        ediciones = parsear_ediciones(texto)
        if not ediciones:
            historial.append({"problema": "la respuesta no contenía bloques BUSCAR/REEMPLAZAR válidos",
                              "detalle": "Usa exactamente el formato ARCHIVO / <<<<<<< BUSCAR / ======= / "
                                         ">>>>>>> REEMPLAZAR."})
            continue
        try:
            cambios = aplicar_ediciones(repo, ediciones, [r[0] for r in rangos], rangos)
        except ErrorAplicar as e:
            print(f"[reparador] no se pudo aplicar: {str(e)[:120]}")
            historial.append({"problema": "el cambio no se pudo aplicar", "detalle": str(e)})
            continue
        if not cambios:
            historial.append({"problema": "el cambio no modificaba nada (BUSCAR igual a REEMPLAZAR)"})
            continue

        with _lock:
            _estado["tocados"].update(cambios)
            _escribir(repo, cambios)
        diff = _diff(repo, list(cambios))
        print(f"[reparador] aplicado en {', '.join(cambios)}:\n{diff}")

        error = _error_sintaxis(repo, list(cambios))
        if error:
            historial.append({"problema": "el cambio produce un error de sintaxis", "diff": diff, "detalle": error})
            with _lock:
                _revertir(repo, list(cambios))
            continue

        res = pruebas.correr_objetivo(repo, info, timeout=60)
        if not res.pasa:
            print("[reparador] los tests objetivo siguen fallando")
            problema = ("los tests no terminaron a tiempo (¿ciclo infinito?)" if res.agotado
                        else "los tests objetivo siguen fallando")
            historial.append({"problema": problema, "diff": diff,
                              "detalle": pruebas.recortar_salida(res.salida, 2000)})
            with _lock:
                _revertir(repo, list(cambios))
            continue

        print("[reparador] tests objetivo pasan; revisando regresiones...")
        regs, salida_mod = _regresiones(repo, info, linea_base, dir_tmp, cambios)
        candidato = {"cambios": cambios, "diff": diff, "regresiones": None if regs is None else len(regs),
                     "intento": intento}
        with _lock:
            previo = _estado["mejor"]
            if previo is None or (regs is not None and (previo["regresiones"] is None
                                                        or len(regs) < previo["regresiones"])):
                _estado["mejor"] = candidato
        if regs is None:
            print(f"[reparador] regresión no verificada: {salida_mod}")
            break   # tests objetivo pasan y no hay tiempo para más: se acepta
        if not regs:
            print("[reparador] sin regresiones: reparación aceptada")
            break
        print(f"[reparador] {len(regs)} regresiones; se reintenta")
        historial.append({"problema": "los tests objetivo pasan, pero se rompieron otros tests", "diff": diff,
                          "detalle": _detalle_regresiones(regs, salida_mod)})
        with _lock:
            _revertir(repo, list(cambios))

    mejor = dejar_estado_final()
    resultado["intentos"] = intento
    if mejor:
        final = pruebas.correr_objetivo(repo, info, timeout=tiempo.acotar(60, 2)) \
            if tiempo.restante() > 8 else None
        resultado["resuelto"] = bool(final.pasa) if final else True
        resultado["regresiones"] = mejor["regresiones"]
        resultado["archivos_modificados"] = sorted(mejor["cambios"])
        resultado["intento_exitoso"] = mejor["intento"]
    return resultado


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    localizacion = json.loads(Path(sys.argv[2]).read_text())
    print(json.dumps(reparar(carpeta, info, localizacion), indent=2, ensure_ascii=False))
