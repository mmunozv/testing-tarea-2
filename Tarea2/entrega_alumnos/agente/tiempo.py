"""Control del tiempo del agente: un único reloj compartido por todas las etapas.

El límite oficial es de 180 s por bug. El agente trabaja con un presupuesto menor (PRESUPUESTO)
para dejar margen a escribir los resultados y a que el proceso termine.
"""
import time

PRESUPUESTO = 160.0   # segundos de trabajo "útil" (localizar + reparar)
LIMITE_DURO = 172.0   # a partir de aquí el vigilante de main.py corta y escribe resultados

_inicio = time.monotonic()


def reiniciar() -> None:
    global _inicio
    _inicio = time.monotonic()


def transcurrido() -> float:
    return time.monotonic() - _inicio


def restante() -> float:
    """Segundos que quedan del presupuesto de trabajo (puede ser negativo)."""
    return PRESUPUESTO - transcurrido()


def acotar(segundos: float, reserva: float = 0.0) -> float:
    """Limita un timeout para no pasarse del presupuesto (dejando `reserva` segundos libres)."""
    return max(1.0, min(segundos, restante() - reserva))
