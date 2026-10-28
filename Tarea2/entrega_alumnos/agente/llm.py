"""Cliente de Gemini con reintentos, control de tiempo y contabilidad de costo.

- Reintenta errores temporales (429, 5xx, red, timeouts) con espera creciente, sin pasarse
  del presupuesto de tiempo del bug.
- Nunca lanza excepciones hacia el agente: si no hay respuesta útil devuelve None.
- Cuenta solo las llamadas que obtuvieron respuesta, y suma prompt_token_count y
  candidates_token_count de cada respuesta (lo que exige metricas.json).
"""
import random
import re
import time

import tiempo

MODELO = "gemini-3.5-flash-lite"
CODIGOS_TEMPORALES = {408, 429, 500, 502, 503, 504}


class Contador:
    def __init__(self):
        self.llamadas = 0
        self.tokens_entrada = 0
        self.tokens_salida = 0
        self.por_etapa = {}

    def sumar(self, etapa: str, entrada: int, salida: int) -> None:
        self.llamadas += 1
        self.tokens_entrada += entrada
        self.tokens_salida += salida
        e = self.por_etapa.setdefault(etapa, {"llamadas": 0, "tokens_entrada": 0, "tokens_salida": 0})
        e["llamadas"] += 1
        e["tokens_entrada"] += entrada
        e["tokens_salida"] += salida


# contador global: lo comparten localizador y reparador y lo lee main.py
CONTADOR = Contador()
_cliente = None
_sin_cliente = False


def _obtener_cliente():
    """Crea el cliente una sola vez. La API key se lee de GEMINI_API_KEY (lo hace genai.Client)."""
    global _cliente, _sin_cliente
    if _cliente is None and not _sin_cliente:
        try:
            from google import genai
            _cliente = genai.Client()
        except Exception as e:   # sin clave o sin librería: el agente sigue sin LLM
            print(f"[llm] no se pudo crear el cliente de Gemini: {e}")
            _sin_cliente = True
    return _cliente


def disponible() -> bool:
    return _obtener_cliente() is not None


def _codigo_error(e: Exception):
    codigo = getattr(e, "code", None) or getattr(e, "status_code", None)
    if isinstance(codigo, int):
        return codigo
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(e)[:200])
    return int(m.group(1)) if m else None


def _espera_sugerida(e: Exception):
    """Gemini a veces indica cuánto esperar en un 429 (retryDelay: '12s')."""
    m = re.search(r"retry(?:Delay|_delay)?['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", str(e))
    return float(m.group(1)) if m else None


def generar(prompt: str, etapa: str, temperatura: float = 0.2, max_tokens: int = 4096,
            reserva: float = 5.0, timeout_llamada: float = 60.0):
    """Envía `prompt` al modelo y devuelve el texto de la respuesta, o None si no fue posible.

    reserva: segundos del presupuesto que deben quedar libres después de la llamada.
    """
    cliente = _obtener_cliente()
    if cliente is None:
        return None
    from google.genai import types

    intento = 0
    while True:
        disponible = tiempo.restante() - reserva
        if disponible < 5:
            print(f"[llm] sin tiempo para llamar al modelo ({etapa})")
            return None
        limite = min(timeout_llamada, disponible)
        config = types.GenerateContentConfig(
            temperature=temperatura,
            max_output_tokens=max_tokens,
            http_options=types.HttpOptions(timeout=int(limite * 1000)),
        )
        t0 = time.monotonic()
        try:
            resp = cliente.models.generate_content(model=MODELO, contents=prompt, config=config)
        except Exception as e:
            codigo = _codigo_error(e)
            temporal = codigo is None or codigo in CODIGOS_TEMPORALES
            print(f"[llm] error ({etapa}, intento {intento + 1}): {codigo} {str(e)[:160]}")
            if not temporal:
                return None   # 400/403/404...: reintentar no sirve
            espera = _espera_sugerida(e) or min(30.0, 2.0 * 2 ** intento)
            espera += random.uniform(0, 1)
            if tiempo.restante() - reserva - espera < 8:
                print("[llm] no queda tiempo para otro reintento")
                return None
            time.sleep(espera)
            intento += 1
            continue

        # hubo respuesta: se cuenta, aunque su contenido no sirva
        uso = getattr(resp, "usage_metadata", None)
        entrada = (getattr(uso, "prompt_token_count", None) or 0) if uso else 0
        salida = (getattr(uso, "candidates_token_count", None) or 0) if uso else 0
        CONTADOR.sumar(etapa, entrada, salida)
        try:
            texto = resp.text
        except Exception:
            texto = None
        print(f"[llm] {etapa}: {entrada} tokens entrada, {salida} salida, "
              f"{time.monotonic() - t0:.1f}s")
        if not texto or not texto.strip():
            print("[llm] respuesta vacía o bloqueada")
            return None
        return texto
