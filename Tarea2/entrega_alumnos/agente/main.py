"""Agente de reparación de bugs: punto de entrada (pieza 1, orquestación).

Uso (el entorno del bug debe estar instalado con setup.sh):
    python main.py <carpeta_del_bug>
    python main.py ../dataset/black-2

Flujo: localizar -> reparar -> escribir localizacion.json y metricas.json en <carpeta_del_bug>/.
La reparación queda aplicada en <carpeta_del_bug>/repo/, que es lo que se evalúa.

Garantías:
  - Los dos JSON se escriben siempre, aunque una etapa lance una excepción.
  - Un vigilante corta la ejecución antes del límite de 3 minutos: deja en repo/ la mejor
    reparación encontrada hasta ese momento (o el original) y escribe los resultados.
"""
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

import llm
import localizador
import pruebas
import reparador
import tiempo
from localizador import localizar, publicable

MODELO = llm.MODELO
_escrito = threading.Lock()
_terminado = False


def escribir_metricas(carpeta: Path, id_bug: str, t0: float) -> None:
    c = llm.CONTADOR
    metricas = {
        "bug": id_bug,
        "modelo": MODELO,
        "llamadas": c.llamadas,
        "tokens_entrada": c.tokens_entrada,
        "tokens_salida": c.tokens_salida,
        "segundos": round(time.perf_counter() - t0, 1),
    }
    (carpeta / "metricas.json").write_text(json.dumps(metricas, indent=2))


def escribir_localizacion(carpeta: Path, localizacion: dict) -> None:
    (carpeta / "localizacion.json").write_text(
        json.dumps(publicable(localizacion), indent=2, ensure_ascii=False))


def main():
    global _terminado
    if len(sys.argv) != 2:
        sys.exit("Uso: python main.py <carpeta_del_bug>")
    carpeta = Path(sys.argv[1]).resolve()
    info = json.loads((carpeta / "bug.json").read_text())
    t0 = time.perf_counter()
    tiempo.reiniciar()
    estado = {"localizacion": None}

    def vigilante():
        """Si se acerca el límite duro: deja el repo consistente, escribe todo y termina."""
        global _terminado
        with _escrito:
            if _terminado:
                return
            _terminado = True
            print(f"[main] límite de tiempo ({tiempo.LIMITE_DURO:.0f}s): se termina con lo que hay")
            pruebas.matar_todos()
            try:
                reparador.dejar_estado_final()
            except Exception:
                traceback.print_exc()
            if estado["localizacion"] is None:
                escribir_localizacion(carpeta, {"regiones": []})
            escribir_metricas(carpeta, info["id"], t0)
        os._exit(0)

    alarma = threading.Timer(tiempo.LIMITE_DURO, vigilante)
    alarma.daemon = True
    alarma.start()

    resultado = {}
    try:
        try:
            localizacion = localizar(carpeta, info)
        except Exception:
            # el localizador falló: se sigue con una localización de respaldo para intentar reparar igual
            traceback.print_exc()
            localizacion = {"regiones": localizador._regiones_respaldo(carpeta / "repo", info, ""),
                            "error": "falló el localizador; regiones de respaldo"}
        estado["localizacion"] = localizacion
        escribir_localizacion(carpeta, localizacion)
        print(f"[main] localización: " + ", ".join(
            f"{r['archivo']}:{r['inicio']}-{r['fin']}" for r in localizacion["regiones"][:5]))
        resultado = reparar_seguro(carpeta, info, localizacion)
    except Exception:
        traceback.print_exc()
        if estado["localizacion"] is None:
            escribir_localizacion(carpeta, {"regiones": [], "error": "falló el localizador"})
    finally:
        with _escrito:
            if not _terminado:
                _terminado = True
                alarma.cancel()
                escribir_metricas(carpeta, info["id"], t0)
    c = llm.CONTADOR
    print(f"{info['id']}: {'RESUELTO' if resultado.get('resuelto') else 'NO RESUELTO'} "
          f"({c.llamadas} llamadas, {c.tokens_entrada}+{c.tokens_salida} tokens, "
          f"{time.perf_counter() - t0:.1f}s) {json.dumps(c.por_etapa)}")


def reparar_seguro(carpeta: Path, info: dict, localizacion: dict) -> dict:
    try:
        return reparador.reparar(carpeta, info, localizacion)
    except Exception:
        traceback.print_exc()
        reparador.dejar_estado_final()
        return {"resuelto": False}


if __name__ == "__main__":
    main()
