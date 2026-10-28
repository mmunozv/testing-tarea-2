"""Ejecución e interpretación de tests (pieza 2).

Todo se ejecuta con el entorno del bug (repo/env, Python 3.8) en un subproceso con límite de tiempo.
Funciona igual para proyectos con unittest o pytest: los tests objetivo se ejecutan con los comandos
de bug.json tal cual, y los módulos de regresión siempre con pytest (que también ejecuta tests de
unittest) para obtener el resultado de cada test en un XML JUnit.
"""
import os
import re
import shlex
import signal
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import tiempo

# misma definición de "archivo de test" que usan run_test.sh y verificar.py
ES_TEST = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")
DIRS_IGNORADOS = ("env/", ".git/", "build/", "dist/", ".tox/", "docs/")


def es_test(ruta: str) -> bool:
    return bool(ES_TEST.search(ruta))


def es_fuente(ruta: str) -> bool:
    """Archivo .py del proyecto que se puede modificar (ni test, ni entorno, ni documentación)."""
    return ruta.endswith(".py") and not es_test(ruta) and not ruta.startswith(DIRS_IGNORADOS)


# --------------------------------------------------------------------------- subprocesos

_activos = set()   # procesos en curso (para matarlos si se acaba el tiempo)


def matar_todos() -> None:
    for p in list(_activos):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass

def ejecutar(cmd: str, repo: Path, timeout: float, env_extra: dict = None):
    """Ejecuta `cmd` con el entorno del bug activado. Devuelve (código, salida, se_agotó_el_tiempo)."""
    with tempfile.TemporaryDirectory() as cache:
        env = dict(os.environ, XDG_CACHE_HOME=cache, PYTHONDONTWRITEBYTECODE="1", **(env_extra or {}))
        env.pop("PYTHONPATH", None)
        p = subprocess.Popen(["bash", "-c", f"source env/bin/activate && {cmd}"], cwd=repo, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", start_new_session=True)
        _activos.add(p)
        try:
            out, _ = p.communicate(timeout=max(1.0, timeout))
            return p.returncode, out, False
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out = p.communicate()[0] or ""
            return -1, out, True
        finally:
            _activos.discard(p)


def git(repo: Path, *args) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


def restaurar_tests(repo: Path) -> None:
    """Deja los archivos de test como en el original (algunos tests reescriben sus datos)."""
    cambiados = [f for f in git(repo, "diff", "--name-only").split() if es_test(f)]
    if cambiados:
        git(repo, "checkout", "--", *cambiados)


# --------------------------------------------------------------------------- tests objetivo

@dataclass
class TestObjetivo:
    """Un test que expone el bug, identificado de forma independiente del runner."""
    archivo: str            # tests/test_x.py
    clase: str = None       # TestCase (o None para funciones de pytest)
    funcion: str = ""       # test_algo (sin parámetros)
    original: str = ""      # id tal como aparece en el comando


def parsear_objetivo(id_test: str, repo: Path):
    """Interpreta ids de pytest (a/b.py::C::t[x]) y de unittest (a.b.C.t)."""
    if "::" in id_test or id_test.endswith(".py"):
        partes = id_test.split("::")
        nombres = [re.sub(r"\[.*$", "", p) for p in partes[1:]]
        return TestObjetivo(partes[0], nombres[0] if len(nombres) > 1 else None,
                            nombres[-1] if nombres else "", id_test)
    partes = id_test.split(".")
    for i in range(len(partes), 0, -1):   # el prefijo más largo que es un archivo es el módulo
        ruta = "/".join(partes[:i]) + ".py"
        if (repo / ruta).is_file():
            resto = partes[i:]
            return TestObjetivo(ruta, resto[0] if len(resto) > 1 else None,
                                resto[-1] if resto else "", id_test)
    return None


def objetivos(info: dict, repo: Path):
    """Extrae de comandos_test los tests objetivo."""
    res = []
    for cmd in info.get("comandos_test", []):
        for tok in shlex.split(cmd):
            if tok.startswith("-") or tok in ("python", "python3", "pytest", "unittest", "-m"):
                continue
            if "::" in tok or re.fullmatch(r"[\w]+(\.[\w]+){2,}", tok) or tok.endswith(".py"):
                t = parsear_objetivo(tok, repo)
                if t and (repo / t.archivo).is_file():
                    res.append(t)
    return res


def comandos_agrupados(info: dict):
    """Une los comandos que solo difieren en el test (p. ej. 4 'python -m unittest -q X'),
    para pagar una sola vez el arranque de Python."""
    grupos = {}
    for cmd in info.get("comandos_test", []):
        toks = shlex.split(cmd)
        if len(toks) >= 2 and not toks[-1].startswith("-"):
            grupos.setdefault(shlex.join(toks[:-1]), []).append(toks[-1])
        else:
            grupos.setdefault(cmd, [])
    return [f"{pre} {' '.join(shlex.quote(t) for t in ts)}".strip() for pre, ts in grupos.items()]


def _rutas_relativas(salida: str, repo: Path) -> str:
    """Rutas del repo como relativas (más cortas para el prompt e iguales a las de la localización)."""
    return salida.replace(str(repo.resolve()) + "/", "")


@dataclass
class Resultado:
    pasa: bool
    salida: str
    agotado: bool = False


def correr_objetivo(repo: Path, info: dict, timeout: float = 90.0) -> Resultado:
    """Ejecuta los tests que exponen el bug. pasa=True si todos terminan con código 0."""
    restaurar_tests(repo)
    salidas, pasa, agotado = [], True, False
    for cmd in comandos_agrupados(info):
        codigo, out, ag = ejecutar(cmd, repo, tiempo.acotar(timeout, reserva=3))
        salidas.append(f"$ {cmd}\n{_rutas_relativas(out, repo)}")
        pasa &= codigo == 0
        agotado |= ag
    restaurar_tests(repo)
    return Resultado(pasa, "\n".join(salidas), agotado)


# --------------------------------------------------------------------------- cobertura

def _comando_con_cobertura(cmd: str, rc: str) -> str:
    """Transforma 'python -m X ...', 'pytest ...' o 'python a.py ...' para ejecutarlo con coverage."""
    toks = shlex.split(cmd)
    base = ["python", "-m", "coverage", "run", f"--rcfile={rc}"]
    if len(toks) >= 3 and toks[0].startswith("python") and toks[1] == "-m":
        nuevo = base + ["-m"] + toks[2:]
    elif toks and toks[0] in ("pytest", "py.test"):
        nuevo = base + ["-m", "pytest"] + toks[1:]
    elif len(toks) >= 2 and toks[0].startswith("python"):
        nuevo = base + toks[1:]
    else:
        return None
    return shlex.join(nuevo)


LEER_COBERTURA = r"""
import json, sys, coverage
c = coverage.Coverage(data_file=sys.argv[1]); c.load(); d = c.get_data()
out = {}
for f in d.measured_files():
    lineas = d.lines(f) or []
    ctx = {}
    if len(sys.argv) > 2:
        try:
            for ln, cs in (d.contexts_by_lineno(f) or {}).items():
                ctx[ln] = [x for x in cs if x]
        except Exception:
            pass
    out[f] = {"lineas": sorted(lineas), "ctx": ctx}
json.dump(out, open(sys.argv[1] + ".json", "w"))
"""


def _leer_cobertura(repo: Path, datos: str, con_contextos: bool, timeout: float):
    import json
    script = Path(datos + "_leer.py")
    script.write_text(LEER_COBERTURA)
    args = f"python {shlex.quote(str(script))} {shlex.quote(datos)}" + (" ctx" if con_contextos else "")
    ejecutar(args, repo, timeout)
    try:
        crudo = json.loads(Path(datos + ".json").read_text())
    except (OSError, ValueError):
        return None
    res = {}
    for f, v in crudo.items():
        try:
            rel = str(Path(f).resolve().relative_to(repo.resolve()))
        except ValueError:
            continue
        if es_fuente(rel):
            res[rel] = v
    return res


def _rcfile(dir_tmp: str, repo: Path, contextos: bool) -> str:
    """Configuración propia de coverage: solo mide archivos del repo (sin env/) y, si se pide,
    registra qué test ejecutó cada línea (contexto dinámico test_function)."""
    rc = Path(dir_tmp) / "coveragerc"
    raiz = repo.resolve()
    rc.write_text("[run]\n"
                  f"include =\n    {raiz}/*\n"
                  f"omit =\n    {raiz}/env/*\n"
                  + ("dynamic_context = test_function\n" if contextos else ""))
    return str(rc)


def correr_objetivo_con_cobertura(repo: Path, info: dict, dir_tmp: str, timeout: float = 90.0):
    """Ejecuta los tests objetivo midiendo cobertura. Devuelve (Resultado, cobertura|None)
    con cobertura = {archivo_relativo: {"lineas": [...]}} solo de código fuente."""
    restaurar_tests(repo)
    rc = _rcfile(dir_tmp, repo, contextos=False)
    datos = str(Path(dir_tmp) / "cov_objetivo")
    salidas, pasa, agotado, transformables = [], True, False, True
    for cmd in comandos_agrupados(info):
        nuevo = _comando_con_cobertura(cmd, rc)
        if nuevo is None:
            transformables = False
            nuevo = cmd
        codigo, out, ag = ejecutar(nuevo, repo, tiempo.acotar(timeout, reserva=3),
                                   {"COVERAGE_FILE": datos + "." + str(len(salidas))})
        salidas.append(f"$ {cmd}\n{_rutas_relativas(out, repo)}")
        pasa &= codigo == 0
        agotado |= ag
    restaurar_tests(repo)
    res = Resultado(pasa, "\n".join(salidas), agotado)
    if "No module named coverage" in res.salida:
        return correr_objetivo(repo, info, timeout), None
    if not transformables and len(salidas) == 0:
        return res, None
    # combinar los archivos de datos de cada comando
    cobertura = {}
    for i in range(len(salidas)):
        parcial = _leer_cobertura(repo, f"{datos}.{i}", False, tiempo.acotar(20, 3)) or {}
        for f, v in parcial.items():
            previo = set(cobertura.get(f, {}).get("lineas", []))
            cobertura[f] = {"lineas": sorted(previo | set(v["lineas"]))}
    return res, (cobertura or None)


# --------------------------------------------------------------------------- módulos de regresión

def _rutas_modulos(modulos):
    # pytest no recolecta tests definidos en un __init__.py: se pasa su carpeta
    rutas = [str(Path(m).parent) if m.endswith("__init__.py") else m for m in modulos]
    opt = ("-o 'python_files=__init__.py test_*.py *_test.py' "
           if any(m.endswith("__init__.py") for m in modulos) else "")
    return opt + " ".join(shlex.quote(r) for r in rutas)


def _id_test(caso, repo: Path) -> str:
    """Id estable de un test a partir del XML JUnit (formato 'clase::nombre' como verificar.py)."""
    return f"{caso.get('classname')}::{caso.get('name')}"


def _leer_junit(xml: str, repo: Path):
    """Devuelve {id: "pasa"|"falla"} o None si no hay XML."""
    try:
        raiz = ET.parse(xml).getroot()
    except (ET.ParseError, FileNotFoundError):
        return None
    estados = {}
    for caso in raiz.iter("testcase"):
        hijos = {h.tag for h in caso}
        if "skipped" in hijos:
            estado = "omitido"
        elif hijos & {"failure", "error"}:
            estado = "falla"
        else:
            estado = "pasa"
        estados[_id_test(caso, repo)] = estado
    return estados


def correr_modulos(repo: Path, modulos, timeout: float, dir_tmp: str, cobertura_por_test=False):
    """Ejecuta los módulos de test completos con pytest.

    Devuelve (estados, salida, cobertura): estados = {id_test: pasa|falla|omitido} (None si no
    terminó a tiempo). Con cobertura_por_test=True mide además qué test ejecutó cada línea
    (sirve al localizador para calcular sospechas tipo Ochiai)."""
    restaurar_tests(repo)
    xml = str(Path(dir_tmp) / f"junit_{os.getpid()}_{int(tiempo.transcurrido() * 1000)}.xml")
    pytest_args = (f"-p no:cacheprovider -q -o junit_family=xunit1 --junitxml={shlex.quote(xml)} "
                   f"{_rutas_modulos(modulos)}")
    env_extra = {}
    if cobertura_por_test:
        rc = _rcfile(dir_tmp, repo, contextos=True)
        datos = str(Path(dir_tmp) / "cov_modulos")
        env_extra["COVERAGE_FILE"] = datos
        cmd = f"python -m coverage run --rcfile={shlex.quote(rc)} -m pytest {pytest_args}"
    else:
        cmd = f"python -m pytest {pytest_args}"
    codigo, out, agotado = ejecutar(cmd, repo, timeout, env_extra)
    restaurar_tests(repo)
    estados = None if agotado else _leer_junit(xml, repo)
    cobertura = None
    if cobertura_por_test and estados is not None:
        cobertura = _leer_cobertura(repo, datos, True, tiempo.acotar(30, 3))
    return estados, out, cobertura


def correr_tests_por_id(repo: Path, ids, timeout: float, dir_tmp: str):
    """Vuelve a ejecutar solo algunos tests (ids en formato clase::nombre de _leer_junit)."""
    nodos = []
    for t in ids:
        clase, nombre = t.split("::", 1)
        partes = clase.split(".")
        for i in range(len(partes), 0, -1):
            ruta = "/".join(partes[:i]) + ".py"
            if (repo / ruta).is_file():
                nodos.append("::".join([ruta, *partes[i:], nombre]))
                break
    if not nodos:
        return {}
    restaurar_tests(repo)
    xml = str(Path(dir_tmp) / f"junit_rerun_{int(tiempo.transcurrido() * 1000)}.xml")
    cmd = (f"python -m pytest -p no:cacheprovider -q -o junit_family=xunit1 "
           f"--junitxml={shlex.quote(xml)} " + " ".join(shlex.quote(n) for n in nodos))
    _, _, agotado = ejecutar(cmd, repo, timeout)
    restaurar_tests(repo)
    return (None if agotado else _leer_junit(xml, repo)) or {}


# --------------------------------------------------------------------------- interpretación de la salida

RE_FRAME_PY = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
RE_FRAME_PYTEST = re.compile(r"^([^\s:]+\.py):(\d+):(?: in (\S+))?", re.M)
RE_MODULO = re.compile(r"(?:from|module) '([\w.]+)'")


@dataclass
class Frame:
    archivo: str   # relativo a repo/
    linea: int
    funcion: str
    es_test: bool = field(default=False)


def frames(salida: str, repo: Path):
    """Frames de los tracebacks (formato de Python y de pytest) que caen dentro del repo."""
    raiz = repo.resolve()
    res = []
    for m in list(RE_FRAME_PY.finditer(salida)) + list(RE_FRAME_PYTEST.finditer(salida)):
        ruta, linea, func = m.group(1), int(m.group(2)), m.group(3) or "?"
        p = Path(ruta)
        if not p.is_absolute():
            p = raiz / p
        try:
            rel = str(p.resolve().relative_to(raiz))
        except (ValueError, OSError):
            continue
        if rel.startswith(("env/", ".git/")):
            continue
        res.append(Frame(rel, linea, func, es_test(rel)))
    return res


def modulos_mencionados(salida: str, repo: Path):
    """Archivos fuente nombrados en mensajes como "cannot import name 'x' from 'pkg.mod'"."""
    res = []
    for m in RE_MODULO.finditer(salida):
        base = m.group(1).replace(".", "/")
        for cand in (base + ".py", base + "/__init__.py"):
            if (repo / cand).is_file() and es_fuente(cand):
                res.append(cand)
    return res


RE_AVISO = re.compile(r"\w*Warning: ")
RE_FRAME_EXTERNO = re.compile(r'File "[^"]*(/env/|/site-packages/|/lib/python3[.\d]*/)[^"]*", line \d+')
RE_FRAME_PYTEST_EXTERNO = re.compile(r"^\S*(/env/|/site-packages/|/lib/python3[.\d]*/)\S*\.py:\d+")


def _limpiar_salida(salida: str) -> str:
    """Quita el ruido de la salida de los tests: avisos (warnings) y frames de librerías externas
    o de la biblioteca estándar (con su línea de código). Así lo que queda son los frames del
    proyecto y del test (dónde falla la aserción) y el mensaje final del error."""
    res, saltar = [], False
    for l in salida.splitlines():
        if saltar:   # línea de código que acompaña a un aviso o a un frame descartado
            saltar = False
            if l.startswith(" ") and not l.lstrip().startswith(("File ", "Traceback")):
                continue
        if RE_AVISO.search(l) and not l.startswith((" ", "E ")) or l.strip().startswith("warnings.warn"):
            saltar = True
            continue
        if RE_FRAME_EXTERNO.search(l) or RE_FRAME_PYTEST_EXTERNO.match(l):
            saltar = True
            continue
        res.append(l)
    return "\n".join(res)


def recortar_salida(salida: str, max_chars: int = 5000) -> str:
    """Deja lo más informativo de la salida de los tests: los tracebacks y el final."""
    texto = _limpiar_salida(salida)
    if len(texto) <= max_chars:
        return texto
    # prioriza desde el primer traceback / bloque de fallos
    for marca in ("Traceback (most recent call last)", "= FAILURES =", "FAIL:", "ERROR:"):
        i = texto.find(marca)
        if i != -1 and len(texto) - i <= max_chars:
            return texto[i:]
    mitad = max_chars // 2
    return texto[:mitad] + "\n[... salida recortada ...]\n" + texto[-mitad:]
