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
  - archivo: ruta relativa a repo/, de un archivo de código fuente (no de test)
  - inicio, fin: líneas (desde 1, ambas incluidas) del archivo con el bug
Puedes añadir más campos (p. ej. tu análisis o el coste en tokens) para usarlos en el reparador.
"""
import json
import sys
from pathlib import Path


def localizar(carpeta: Path, info: dict) -> dict:
    # TODO
    raise NotImplementedError


if __name__ == "__main__":
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    print(json.dumps(localizar(carpeta, info), indent=2, ensure_ascii=False))
