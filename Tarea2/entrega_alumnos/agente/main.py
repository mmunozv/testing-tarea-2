"""Agente de reparación de bugs: punto de entrada.

Uso (el entorno del bug debe estar instalado con setup.sh):
    python main.py <carpeta_del_bug>
    python main.py ../dataset/black-2

Flujo: localizar -> reparar -> escribir localizacion.json y metricas.json en <carpeta_del_bug>/.
La reparación queda aplicada en <carpeta_del_bug>/repo/, que es lo que se evalúa.

Puedes cambiar este archivo como quieras mientras se mantengan:
  - el comando de arriba,
  - el formato de localizacion.json (ver localizador.py) y de metricas.json (ver README).
"""
import json
import sys
import time
from pathlib import Path

from localizador import localizar
from reparador import reparar

MODELO = "gemini-3.5-flash-lite"


def main():
    if len(sys.argv) != 2:
        sys.exit("Uso: python main.py <carpeta_del_bug>")
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    t0 = time.perf_counter()

    localizacion = localizar(carpeta, info)
    (carpeta / "localizacion.json").write_text(json.dumps(localizacion, indent=2, ensure_ascii=False))

    resultado = reparar(carpeta, info, localizacion)

    # TODO: completar llamadas y tokens sumando el coste del localizador y del reparador.
    metricas = {
        "bug": info["id"],
        "modelo": MODELO,
        "llamadas": 0,
        "tokens_entrada": 0,
        "tokens_salida": 0,
        "segundos": round(time.perf_counter() - t0, 1),
    }
    (carpeta / "metricas.json").write_text(json.dumps(metricas, indent=2))
    print(f"{info['id']}: {'RESUELTO' if resultado.get('resuelto') else 'NO RESUELTO'}")


if __name__ == "__main__":
    main()
