"""Utilidades sobre el código del repositorio: estructura (AST), fragmentos y tokens.

Se usan tanto en el localizador (para convertir líneas sospechosas en funciones) como en el
reparador (para construir el contexto del prompt).
"""
import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import pruebas


@dataclass(frozen=True)
class Bloque:
    """Una función/método (o clase) del código fuente."""
    archivo: str
    nombre: str          # nombre calificado: Clase.metodo
    inicio: int          # incluye decoradores
    fin: int
    tipo: str            # "funcion" | "clase" | "modulo"
    cuerpo: int          # primera línea del cuerpo (tras la firma)

    @property
    def largo(self) -> int:
        return self.fin - self.inicio + 1


def archivos_fuente(repo: Path):
    salida = pruebas.git(repo, "ls-files", "*.py")
    return [f for f in salida.split() if pruebas.es_fuente(f)]


@lru_cache(maxsize=None)
def _leer(ruta: str) -> str:
    try:
        return Path(ruta).read_text(errors="replace")
    except OSError:
        return ""


def leer(repo: Path, archivo: str) -> str:
    return _leer(str(repo / archivo))


def olvidar_cache() -> None:
    """Llamar después de modificar archivos."""
    _leer.cache_clear()
    bloques.cache_clear()
    _indice.cache_clear()


@lru_cache(maxsize=None)
def bloques(repo: Path, archivo: str):
    """Funciones y clases del archivo, en orden de aparición (anidadas incluidas)."""
    fuente = leer(repo, archivo)
    try:
        arbol = ast.parse(fuente)
    except (SyntaxError, ValueError):
        return ()
    res = []

    def visitar(nodo, prefijo):
        for hijo in ast.iter_child_nodes(nodo):
            if isinstance(hijo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inicio = min([hijo.lineno] + [d.lineno for d in hijo.decorator_list])
                nombre = f"{prefijo}{hijo.name}"
                cuerpo = hijo.body[0].lineno if hijo.body else hijo.lineno
                # si el cuerpo empieza con docstring, el cuerpo "real" va después
                if (len(hijo.body) > 1 and isinstance(hijo.body[0], ast.Expr)
                        and isinstance(getattr(hijo.body[0], "value", None), ast.Constant)
                        and isinstance(hijo.body[0].value.value, str)):
                    cuerpo = hijo.body[1].lineno
                tipo = "clase" if isinstance(hijo, ast.ClassDef) else "funcion"
                res.append(Bloque(archivo, nombre, inicio, hijo.end_lineno, tipo, cuerpo))
                visitar(hijo, nombre + ".")
            else:
                visitar(hijo, prefijo)

    visitar(arbol, "")
    return tuple(res)


@lru_cache(maxsize=None)
def _indice(repo: Path, archivo: str):
    """Para cada línea del archivo, (función más interna, bloque más interno) que la contiene."""
    n = num_lineas(repo, archivo)
    func = [None] * (n + 2)
    cualquiera = [None] * (n + 2)
    # los bloques vienen en orden de aparición: los anidados se escriben después (más internos)
    for b in bloques(repo, archivo):
        for i in range(b.inicio, min(b.fin, n) + 1):
            cualquiera[i] = b
            if b.tipo == "funcion":
                func[i] = b
    return func, cualquiera


def bloque_de_linea(repo: Path, archivo: str, linea: int, solo_funciones=False):
    """El bloque más interno que contiene la línea (función si existe; si no, clase)."""
    func, cualquiera = _indice(repo, archivo)
    if not 0 < linea < len(func):
        return None
    if func[linea] is not None or solo_funciones:
        return func[linea]
    return cualquiera[linea]


def funciones(repo: Path, archivo: str):
    return [b for b in bloques(repo, archivo) if b.tipo == "funcion"]


def fragmento(repo: Path, archivo: str, inicio: int, fin: int, numerado: bool = False) -> str:
    lineas = leer(repo, archivo).splitlines()
    inicio, fin = max(1, inicio), min(len(lineas), fin)
    if numerado:
        return "\n".join(f"{i:5d}| {lineas[i - 1]}" for i in range(inicio, fin + 1))
    return "\n".join(lineas[inicio - 1:fin])


def num_lineas(repo: Path, archivo: str) -> int:
    return len(leer(repo, archivo).splitlines())


def firma(repo: Path, b: Bloque) -> str:
    """Primera(s) línea(s) de la definición, para listar candidatos de forma compacta."""
    lineas = leer(repo, b.archivo).splitlines()
    res = []
    for i in range(b.inicio, min(b.fin, b.inicio + 6) + 1):
        res.append(lineas[i - 1].strip())
        if lineas[i - 1].rstrip().endswith(":") and not lineas[i - 1].lstrip().startswith("@"):
            break
    return " ".join(res)[:200]


# --------------------------------------------------------------------------- tests

def fuente_test(repo: Path, objetivo) -> str:
    """Código del test objetivo (y del setUp de su clase, si existe)."""
    archivo = objetivo.archivo
    partes = []
    for b in bloques(repo, archivo):
        nombre = b.nombre.split(".")
        if nombre[-1] == objetivo.funcion and (objetivo.clase is None or objetivo.clase in nombre):
            partes.insert(0, (b, "test"))
        elif objetivo.clase and nombre[-1] in ("setUp", "setup_method") and objetivo.clase in nombre:
            partes.append((b, "setup"))
    textos = []
    for b, _ in partes[:2]:
        fin = min(b.fin, b.inicio + 120)
        texto = fragmento(repo, archivo, b.inicio, fin)
        if fin < b.fin:
            texto += "\n    # [... test recortado ...]"
        textos.append(f"# {archivo} (líneas {b.inicio}-{fin})\n{texto}")
    return "\n\n".join(textos)


def imports_de(repo: Path, archivo: str):
    """Archivos fuente importados por un archivo (p. ej. el módulo de test)."""
    try:
        arbol = ast.parse(leer(repo, archivo))
    except (SyntaxError, ValueError):
        return []
    modulos = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            modulos.update(a.name for a in nodo.names)
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and not nodo.level:
            modulos.add(nodo.module)
            modulos.update(f"{nodo.module}.{a.name}" for a in nodo.names)
    res = []
    for m in modulos:
        base = m.replace(".", "/")
        for cand in (base + ".py", base + "/__init__.py"):
            if (repo / cand).is_file() and pruebas.es_fuente(cand):
                res.append(cand)
    return sorted(set(res))


def identificadores(repo: Path, archivo: str, inicio: int, fin: int):
    """Nombres usados en un rango de código (variables, atributos, llamadas)."""
    texto = fragmento(repo, archivo, inicio, fin)
    return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", texto))


STOP = {"self", "test", "tests", "assert", "equal", "equals", "true", "false", "none", "the",
        "return", "def", "class", "import", "from", "is", "in", "not", "and", "or", "if", "else",
        "for", "with", "as", "a", "an", "of", "to", "it", "raises", "case", "testcase", "setup",
        "py", "init", "str", "int", "len", "dict", "list", "assertequal", "asserttrue"}


def tokens(nombre: str):
    """Divide identificadores en palabras: 'getSitemapUrls_from' -> {get, sitemap, urls, from}."""
    nombre = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", nombre)
    return {t for t in re.split(r"[^A-Za-z0-9]+", nombre.lower()) if len(t) > 2 and t not in STOP}


def cabecera_clase(repo: Path, b: Bloque):
    """Para un método: rango de la cabecera de su clase (firma, docstring y atributos de clase,
    hasta el primer método). None si no es un método."""
    if "." not in b.nombre:
        return None
    nombre_clase = b.nombre.rsplit(".", 1)[0]
    for c in bloques(repo, b.archivo):
        if c.tipo == "clase" and c.nombre == nombre_clase and c.inicio <= b.inicio <= c.fin:
            hijos = [h.inicio for h in bloques(repo, b.archivo)
                     if h.nombre.startswith(nombre_clase + ".") and h.nombre.count(".") == nombre_clase.count(".") + 1]
            fin = (min(hijos) - 1) if hijos else c.fin
            return c.inicio, max(c.inicio, fin)
    return None
