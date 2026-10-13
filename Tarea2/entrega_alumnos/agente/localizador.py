"""LOCALIZADOR: decide en qué parte del código fuente está el bug.

Uso por separado (para probarlo solo):
    python localizador.py <carpeta_del_bug>      # imprime la localización en JSON

Recibe:
  carpeta  Path de la carpeta del bug (contiene repo/, bug.json, run_test.sh, ...)
  info     contenido de bug.json como dict (id, comandos_test, archivos_test, modulos_test, ...)

Devuelve un dict con, al menos, este formato (se guarda como localizacion.json y se evalúa):
  {
    "regiones": [
      {"archivo": "black.py", "inicio": 120, "fin": 145}
    ]
  }

Cómo localiza (piezas 2 y 3):
  1. Ejecuta los tests objetivo midiendo cobertura -> salida/traceback + líneas ejecutadas.
  2. En paralelo ejecuta los módulos de test completos con cobertura por test (si caben en el
     tiempo): da la línea base de regresión y permite un puntaje tipo Ochiai (líneas que ejecutan
     los tests que fallan y no los que pasan).
  3. Combina señales en un puntaje por función: frames del traceback, Ochiai, cobertura,
     coincidencia de nombres con el test y archivos importados por el test.
  4. Una llamada al LLM reordena los mejores candidatos (solo firmas, prompt acotado).
Campos que empiezan con "_" son internos (se pasan al reparador y no se escriben en el JSON).
"""
import json
import math
import re
import sys
import tempfile
import threading
from collections import defaultdict
from pathlib import Path

import codigo
import llm
import pruebas
import tiempo

MAX_REGIONES = 8          # regiones reportadas en localizacion.json
MAX_CANDIDATOS_LLM = 25   # funciones que se muestran al LLM para reordenar
TIMEOUT_MODULOS = 50.0    # tiempo máximo para la ejecución de módulos con cobertura por test


# --------------------------------------------------------------------------- señales

def _ochiai(cobertura_ctx, objetivos_test):
    """Sospecha por línea a partir de la cobertura por test de los módulos completos.

    Un contexto es "fallido" si corresponde a un test objetivo. Devuelve {(archivo, linea): score}.
    """
    if not cobertura_ctx:
        return {}
    nombres = {(o.clase, o.funcion) for o in objetivos_test}

    def es_fallido(ctx: str) -> bool:
        partes = re.sub(r"\[.*$", "", ctx).replace("::", ".").split(".")
        return any(partes[-1] == f and (c is None or c in partes) for c, f in nombres)

    todos = set()
    for v in cobertura_ctx.values():
        for cs in v.get("ctx", {}).values():
            todos.update(cs)
    fallidos = {c for c in todos if es_fallido(c)}
    if not fallidos:
        return {}
    total_f = len(fallidos)
    res = {}
    for archivo, v in cobertura_ctx.items():
        for ln, cs in v.get("ctx", {}).items():
            cs = set(cs)
            ef = len(cs & fallidos)
            if ef == 0:
                continue
            ep = len(cs) - ef
            res[(archivo, int(ln))] = ef / math.sqrt(total_f * (ef + ep))
    return res


def _frames_fuente(salida, repo):
    """Frames de código fuente con un peso: más peso a los más cercanos al error."""
    pesos = defaultdict(float)
    frames = pruebas.frames(salida, repo)
    # se agrupan en tracebacks: un frame de test después de uno fuente inicia otro bloque
    bloque = []
    bloques = []
    for f in frames:
        if f.es_test and bloque and not bloque[-1].es_test:
            bloques.append(bloque)
            bloque = []
        bloque.append(f)
    if bloque:
        bloques.append(bloque)
    for bl in bloques:
        fuente = [f for f in bl if pruebas.es_fuente(f.archivo)]
        for i, f in enumerate(fuente):
            cercania = (i + 1) / len(fuente)          # 1.0 = el más profundo
            pesos[(f.archivo, f.linea)] = max(pesos[(f.archivo, f.linea)], 1.5 + 3.0 * cercania)
    return pesos


def _contexto_test(repo, objetivos_test):
    """Tokens del nombre del test, identificadores de su código y archivos que importa."""
    tokens_nombre, idents, importados = set(), set(), set()
    for o in objetivos_test:
        tokens_nombre |= codigo.tokens(o.funcion)
        if o.clase:
            tokens_nombre |= codigo.tokens(o.clase)
        for b in codigo.bloques(repo, o.archivo):
            if b.nombre.split(".")[-1] == o.funcion:
                idents |= codigo.identificadores(repo, o.archivo, b.inicio, b.fin)
        importados.update(codigo.imports_de(repo, o.archivo))
    return tokens_nombre, idents, importados


# --------------------------------------------------------------------------- puntaje

def _candidatos(repo, salida, cobertura, ochiai, objetivos_test):
    pesos_tb = _frames_fuente(salida, repo)
    tokens_nombre, idents, importados = _contexto_test(repo, objetivos_test)
    mencionados = set(pruebas.modulos_mencionados(salida, repo))
    tokens_idents = set()
    for i in idents:
        tokens_idents |= codigo.tokens(i)

    # 1) qué funciones entran como candidatas (y el máximo de cada señal por línea)
    cubiertas = defaultdict(set)   # bloque -> líneas del cuerpo ejecutadas
    if cobertura:
        for archivo, v in cobertura.items():
            for ln in v["lineas"]:
                b = codigo.bloque_de_linea(repo, archivo, ln, solo_funciones=True)
                if b and ln >= b.cuerpo:
                    cubiertas[b].add(ln)
    candidatos = set(cubiertas)
    tb_bloque, och_bloque = defaultdict(float), defaultdict(float)
    for (archivo, ln), p in pesos_tb.items():
        b = codigo.bloque_de_linea(repo, archivo, ln)
        if b:
            candidatos.add(b)
            tb_bloque[b] = max(tb_bloque[b], p)
    for (archivo, ln), s in ochiai.items():
        b = codigo.bloque_de_linea(repo, archivo, ln, solo_funciones=True)
        if b and ln >= b.cuerpo:
            candidatos.add(b)
            och_bloque[b] = max(och_bloque[b], s)
    if not cobertura:   # sin cobertura: todas las funciones de los archivos relacionados
        for archivo in importados | mencionados | {a for a, _ in pesos_tb}:
            candidatos.update(codigo.funciones(repo, archivo))

    # 2) puntaje de cada candidata
    max_och = max(ochiai.values(), default=1.0) or 1.0
    puntajes = {}
    for b in candidatos:
        detalle = {}
        tb = tb_bloque.get(b, 0.0)
        if tb:
            detalle["traceback"] = round(tb, 2)
        och = och_bloque.get(b, 0.0)
        if och:   # relativo a la línea más sospechosa del proyecto
            detalle["ochiai"] = round(4.0 * och / max_och, 2)
        if b in cubiertas:
            detalle["cobertura"] = 0.5
        simple = b.nombre.split(".")[-1]
        tks = codigo.tokens(b.nombre) | codigo.tokens(Path(b.archivo).stem)
        coinc = len(tks & tokens_nombre) + 0.3 * len(tks & tokens_idents)
        if coinc:
            detalle["nombres"] = round(min(3.0, 1.0 * coinc), 2)
        if simple in idents and not simple.startswith("__"):
            detalle["usado_en_test"] = 1.5
        if b.archivo in importados:
            detalle["importado_por_test"] = 0.5
        if b.archivo in mencionados:
            detalle["mencionado_en_error"] = 1.5
        puntajes[b] = (sum(detalle.values()), detalle)

    orden = sorted(puntajes, key=lambda b: (-puntajes[b][0], b.largo))
    return [(b, puntajes[b][0], puntajes[b][1]) for b in orden]


# --------------------------------------------------------------------------- LLM

PROMPT_LOCALIZAR = """Eres un experto en depuración de programas Python. Un test del proyecto falla \
por un bug en el código fuente (no en el test). Debes indicar en qué funciones del código \
fuente es más probable que esté el bug que hay que corregir.

## Test(s) que fallan
{tests}

## Salida de los tests (recortada)
```
{salida}
```

## Funciones candidatas
Obtenidas ejecutando el test (cobertura, traceback y nombres). Formato: [id] archivo:líneas firma
{candidatos}

## Respuesta
Responde SOLO con un objeto JSON, sin texto adicional, con los ids de las funciones ordenadas \
de más a menos sospechosa (máximo 5), así:
{{"ids": [3, 0, 7], "razon": "explicación breve"}}
"""


def _reordenar_con_llm(repo, candidatos, tests_txt, salida):
    lista = []
    for i, (b, p, _) in enumerate(candidatos[:MAX_CANDIDATOS_LLM]):
        lista.append(f"[{i}] {b.archivo}:{b.inicio}-{b.fin} {codigo.firma(repo, b)}")
    prompt = PROMPT_LOCALIZAR.format(tests=tests_txt[:6000], salida=pruebas.recortar_salida(salida, 4000),
                                     candidatos="\n".join(lista))
    texto = llm.generar(prompt, "localizador", temperatura=0.0, max_tokens=512, reserva=60)
    if not texto:
        return None, None
    m = re.search(r"\{.*\}", texto, re.S)
    try:
        datos = json.loads(m.group(0)) if m else {}
        ids = [int(x) for x in datos.get("ids", [])]
    except (ValueError, TypeError, AttributeError):
        ids = [int(x) for x in re.findall(r"\b(\d+)\b", texto)]   # respuesta mal formada: rescata números
    ids = [i for i in dict.fromkeys(ids) if 0 <= i < min(len(candidatos), MAX_CANDIDATOS_LLM)]
    return ids[:5], (datos.get("razon") if m and isinstance(datos, dict) else None)


# --------------------------------------------------------------------------- etapa

def _region(b, puntaje=None, detalle=None, origen="heuristica"):
    r = {"archivo": b.archivo, "inicio": b.inicio, "fin": b.fin, "nombre": b.nombre, "origen": origen}
    if puntaje is not None:
        r["puntaje"] = round(puntaje, 2)
    if detalle:
        r["senales"] = detalle
    return r


def _agregar_cabeceras(repo, regiones):
    """Si las regiones más sospechosas son métodos, agrega la cabecera de su clase (atributos de
    clase y configuración): muchos arreglos tocan un atributo usado por el método."""
    for r in list(regiones[:2]):
        b = next((x for x in codigo.bloques(repo, r["archivo"])
                  if x.inicio == r["inicio"] and x.fin == r["fin"]), None)
        cab = codigo.cabecera_clase(repo, b) if b else None
        if not cab or cab[1] - cab[0] > 60:
            continue
        if any(x["archivo"] == r["archivo"] and x["inicio"] <= cab[0] and cab[1] <= x["fin"] for x in regiones):
            continue
        regiones.insert(regiones.index(r) + 1, {
            "archivo": r["archivo"], "inicio": cab[0], "fin": cab[1],
            "nombre": b.nombre.rsplit(".", 1)[0] + " (cabecera de la clase)", "origen": "clase"})
    del regiones[MAX_REGIONES:]


def _regiones_respaldo(repo, info, salida):
    """Si no hay candidatas: archivos mencionados en el error o importados por los tests."""
    archivos = pruebas.modulos_mencionados(salida, repo)
    for o in pruebas.objetivos(info, repo):
        archivos += codigo.imports_de(repo, o.archivo)
    archivos = list(dict.fromkeys(archivos)) or codigo.archivos_fuente(repo)[:1]
    regiones = []
    for a in archivos[:MAX_REGIONES]:
        n = codigo.num_lineas(repo, a)
        if n:
            regiones.append({"archivo": a, "inicio": 1, "fin": n, "nombre": "<archivo>", "origen": "respaldo"})
    return regiones


def localizar(carpeta: Path, info: dict) -> dict:
    repo = carpeta / "repo"
    dir_tmp = tempfile.mkdtemp(prefix="agente_")
    objetivos_test = pruebas.objetivos(info, repo)

    # (2) módulos completos con cobertura por test, en paralelo con los tests objetivo
    resultado_modulos = {}

    def correr_modulos():
        estados, _, cob = pruebas.correr_modulos(repo, info.get("modulos_test", []),
                                                 tiempo.acotar(TIMEOUT_MODULOS, 60), dir_tmp,
                                                 cobertura_por_test=True)
        resultado_modulos.update(estados=estados, cobertura=cob)

    hilo = threading.Thread(target=correr_modulos, daemon=True)
    if info.get("modulos_test"):
        hilo.start()

    # (1) tests objetivo con cobertura
    res, cobertura = pruebas.correr_objetivo_con_cobertura(repo, info, dir_tmp)
    print(f"[localizador] tests objetivo: {'pasan' if res.pasa else 'fallan'}; "
          f"cobertura: {len(cobertura or {})} archivos fuente")
    if hilo.is_alive():
        hilo.join(timeout=tiempo.acotar(TIMEOUT_MODULOS + 15, 60))
    linea_base = resultado_modulos.get("estados")
    ochiai = _ochiai(resultado_modulos.get("cobertura"), objetivos_test)
    print(f"[localizador] módulos de test: "
          f"{'sin resultado' if linea_base is None else f'{len(linea_base)} tests'}; "
          f"líneas con Ochiai: {len(ochiai)}")

    # (3) puntaje por función
    candidatos = _candidatos(repo, res.salida, cobertura, ochiai, objetivos_test)
    tests_txt = "\n\n".join(codigo.fuente_test(repo, o) for o in objetivos_test) or \
        "\n".join(info.get("comandos_test", []))

    # (4) el LLM reordena los mejores candidatos
    regiones, razon = [], None
    if candidatos:
        ids, razon = _reordenar_con_llm(repo, candidatos, tests_txt, res.salida)
        elegidos = ids or []
        for i in elegidos:
            b, p, d = candidatos[i]
            regiones.append(_region(b, p, d, origen="llm"))
        for i, (b, p, d) in enumerate(candidatos):
            if len(regiones) >= MAX_REGIONES:
                break
            if i not in elegidos:
                regiones.append(_region(b, p, d))
    if not regiones:
        regiones = _regiones_respaldo(repo, info, res.salida)
    else:
        _agregar_cabeceras(repo, regiones)

    return {
        "regiones": regiones,
        "razon_llm": razon,
        "tests_objetivo_fallan": not res.pasa,
        # internos para el reparador (no se escriben en localizacion.json)
        "_salida": res.salida,
        "_linea_base": linea_base,
        "_dir_tmp": dir_tmp,
    }


def publicable(localizacion: dict) -> dict:
    """Versión de la localización que se escribe en localizacion.json (sin campos internos)."""
    return {k: v for k, v in localizacion.items() if not k.startswith("_")}


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    print(json.dumps(publicable(localizar(carpeta, info)), indent=2, ensure_ascii=False))
