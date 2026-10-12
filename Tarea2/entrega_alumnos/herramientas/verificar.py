#!/usr/bin/env python3
"""Verifica localmente el resultado del agente sobre un bug.

Uso: python3 herramientas/verificar.py dataset/black-2

Comprueba:
  1. Tests del bug: restaura los archivos de test y ejecuta run_test.sh (deben pasar).
  2. Regresión: ejecuta los modulos_test de bug.json y compara con los tests que pasaban
     con el código ORIGINAL (con el bug). La primera vez se mide esa línea base quitando
     temporalmente tus cambios, y se guarda en <bug>/.linea_base.json.
  3. Formato de localizacion.json y metricas.json.

Es una aproximación a la evaluación real, que compara la regresión con los tests que pasan
con la solución oficial (no la tienes). Si aquí algo falla, en la evaluación también fallará.
"""
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

MODELO = "gemini-3.5-flash-lite"
CAMPOS_METRICAS = {"bug": str, "modelo": str, "llamadas": int, "tokens_entrada": int,
                   "tokens_salida": int, "segundos": (int, float)}
ES_TEST = re.compile(r"(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$")


def run(cmd, cwd, timeout=1200):
    """Ejecuta un comando bash con caché de usuario limpia; si se cuelga, mata todo el grupo."""
    with tempfile.TemporaryDirectory() as cache:
        p = subprocess.Popen(["bash", "-c", cmd], cwd=cwd, env=dict(os.environ, XDG_CACHE_HOME=cache),
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
            return p.returncode, out
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            return -1, p.communicate()[0] or ""


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


def restaurar_tests(repo):
    """Devuelve los archivos de test que habían cambiado y los deja como en el original."""
    cambiados = [f for f in git(repo, "diff", "--name-only").split() if ES_TEST.search(f)]
    if cambiados:
        git(repo, "checkout", "--", *cambiados)
    return cambiados


def tests_que_pasan(repo, modulos):
    """Ejecuta pytest sobre los módulos de test y devuelve el conjunto de tests que pasan."""
    # pytest no recolecta tests definidos en un __init__.py: se pasa su carpeta
    rutas = [str(Path(m).parent) if m.endswith("__init__.py") else m for m in modulos]
    opt = ("-o 'python_files=__init__.py test_*.py *_test.py' "
           if any(m.endswith("__init__.py") for m in modulos) else "")
    with tempfile.NamedTemporaryFile(suffix=".xml", delete=False) as tmp:
        xml = tmp.name
    timeout = "$(python -c 'import pytest_timeout' 2>/dev/null && echo --timeout=60)"
    run(f"source env/bin/activate && python -m pytest -p no:cacheprovider -q "
        f"--continue-on-collection-errors {timeout} --junitxml={xml} {opt}{' '.join(rutas)}", repo)
    pasan = set()
    try:
        for caso in ET.parse(xml).getroot().iter("testcase"):
            if not any(h.tag in ("failure", "error", "skipped") for h in caso):
                pasan.add(f"{caso.get('classname')}::{caso.get('name')}")
    except (ET.ParseError, FileNotFoundError):
        pass
    Path(xml).unlink(missing_ok=True)
    return pasan


def linea_base(bug, repo, modulos):
    """Tests que pasan con el código original; se mide una vez y se guarda."""
    cache = bug / ".linea_base.json"
    if cache.is_file():
        return set(json.loads(cache.read_text()))
    print("        midiendo la línea base con el código original (solo la primera vez)...", flush=True)
    cambios = git(repo, "diff", "--binary")
    respaldo = bug / ".cambios_verificar.patch"   # por si se interrumpe: git apply este archivo
    respaldo.write_text(cambios)
    git(repo, "checkout", "--", ".")
    try:
        base = tests_que_pasan(repo, modulos)
    finally:
        if cambios:
            subprocess.run(["git", "apply", "--binary", str(respaldo)], cwd=repo, check=True)
        respaldo.unlink()
    cache.write_text(json.dumps(sorted(base), indent=1))
    return base


def errores_localizacion(bug, repo):
    f = bug / "localizacion.json"
    if not f.is_file():
        return ["no existe localizacion.json"]
    try:
        loc = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        return [f"localizacion.json no es JSON válido: {e}"]
    regiones = loc.get("regiones") if isinstance(loc, dict) else None
    if not isinstance(regiones, list) or not regiones:
        return ['falta la lista "regiones" (o está vacía)']
    errores = []
    for i, r in enumerate(regiones):
        if not isinstance(r, dict) or not {"archivo", "inicio", "fin"} <= r.keys():
            errores.append(f"región {i}: faltan archivo/inicio/fin")
            continue
        archivo = repo / str(r["archivo"])
        if not archivo.is_file():
            errores.append(f"región {i}: no existe {r['archivo']} (la ruta es relativa a repo/)")
        elif ES_TEST.search(str(r["archivo"])):
            errores.append(f"región {i}: {r['archivo']} es un archivo de test")
        elif not (isinstance(r["inicio"], int) and isinstance(r["fin"], int)
                  and 1 <= r["inicio"] <= r["fin"] <= len(archivo.read_text(errors="replace").splitlines())):
            errores.append(f"región {i}: líneas {r['inicio']}-{r['fin']} fuera de rango")
    return errores


def errores_metricas(bug, id_bug):
    f = bug / "metricas.json"
    if not f.is_file():
        return ["no existe metricas.json"]
    try:
        m = json.loads(f.read_text())
    except json.JSONDecodeError as e:
        return [f"metricas.json no es JSON válido: {e}"]
    errores = [f"campo {k}: falta o no es {t}" for k, t in CAMPOS_METRICAS.items()
               if not isinstance(m.get(k), t) or isinstance(m.get(k), bool)]
    errores += [f"campo sobrante: {k}" for k in m.keys() - CAMPOS_METRICAS.keys()]
    if m.get("bug") != id_bug:
        errores.append(f'"bug" debe ser "{id_bug}"')
    if m.get("modelo") != MODELO:
        errores.append(f'"modelo" debe ser "{MODELO}"')
    return errores


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python3 herramientas/verificar.py <carpeta_del_bug>")
    bug = Path(sys.argv[1]).resolve()
    repo = bug / "repo"
    info = json.loads((bug / "bug.json").read_text())
    if not (repo / "env").is_dir():
        sys.exit(f"Falta el entorno del bug: bash {bug}/setup.sh")
    print(f"== {info['id']}")
    ok = True

    cambiados = restaurar_tests(repo)
    if cambiados:
        print(f"[AVISO] tu agente modificó archivos de test (restaurados): {', '.join(cambiados)}")
    fuente = [f for f in git(repo, "diff", "--name-only").split()]
    print(f"[info]  archivos de código modificados: {', '.join(fuente) or 'ninguno'}")

    code, _ = run(f"bash '{bug / 'run_test.sh'}'", bug)
    print(f"[{'OK' if code == 0 else 'FAIL'}]    tests del bug (run_test.sh)")
    ok &= code == 0

    base = linea_base(bug, repo, info["modulos_test"])
    restaurar_tests(repo)
    perdidos = sorted(base - tests_que_pasan(repo, info["modulos_test"]))
    print(f"[{'OK' if not perdidos else 'FAIL'}]    regresión: {len(base) - len(perdidos)}/{len(base)} "
          f"tests que pasaban siguen pasando")
    for t in perdidos[:10]:
        print(f"          ahora falla: {t}")
    ok &= not perdidos

    for nombre, errores in (("localizacion.json", errores_localizacion(bug, repo)),
                            ("metricas.json", errores_metricas(bug, info["id"]))):
        print(f"[{'OK' if not errores else 'FAIL'}]    formato de {nombre}")
        for e in errores:
            print(f"          {e}")
        ok &= not errores

    print("RESULTADO:", "OK" if ok else "HAY PROBLEMAS")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
