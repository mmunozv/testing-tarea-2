# Diseño del agente

```
main.py ──► localizador.localizar() ──► localizacion.json
        └─► reparador.reparar(localizacion) ──► repo/ modificado
        └─► metricas.json (siempre, incluso con errores o sin tiempo)
```

| Módulo | Responsabilidad |
|---|---|
| `main.py` | Orquestación, escritura de los JSON y vigilante de tiempo (corta a los 172 s) |
| `tiempo.py` | Reloj único del agente: presupuesto de 160 s y `acotar()` para cada timeout |
| `llm.py` | Cliente Gemini: reintentos con espera creciente (429/5xx/red), contador de llamadas y tokens |
| `pruebas.py` | Ejecutar tests en el entorno del bug, cobertura, JUnit XML y lectura de tracebacks |
| `codigo.py` | AST del repo: funciones/clases, fragmentos, firmas, código del test, imports y tokens |
| `localizador.py` | Señales → puntaje por función → el LLM reordena → regiones |
| `reparador.py` | Prompt → bloques BUSCAR/REEMPLAZAR → aplicar → sintaxis → tests → regresión → reintentar |

## Las 10 piezas (Sección 2.2)

1. **Orquestación** – `main.py:main`. Los JSON se escriben en `finally`, y un `threading.Timer`
   (`vigilante`) deja el repo con la mejor reparación encontrada si se llega al límite duro.
2. **Ejecución de tests** – `pruebas.correr_objetivo` (con los comandos de `bug.json`, unidos si solo
   difieren en el test) y `pruebas.correr_modulos` (siempre con pytest + `--junitxml`, que también corre
   tests unittest). Por eso un proyecto pytest o unittest no cambia nada del código.
3. **Señales para localizar** – `localizador._candidatos`:
   - frames del traceback que caen en código fuente (más peso a los más profundos);
   - **Ochiai** por línea: los módulos de test se corren con `coverage` y `dynamic_context = test_function`
     (en paralelo con los tests objetivo, máx. 50 s); se marcan sospechosas las líneas que ejecuta el
     test que falla y no los que pasan;
   - cobertura de los tests objetivo, nombres del test vs. nombres de funciones, identificadores usados
     en el test, archivos importados por el test y módulos nombrados en el error.
   Después, **una** llamada al LLM reordena las 25 mejores (solo firmas, para que el prompt sea pequeño).
   Además se agrega la cabecera de la clase de los métodos más sospechosos (atributos de clase).
4. **Contexto** – `reparador._construir_regiones`: máx. 450 líneas en total; una función de más de
   160 líneas se muestra como ventana alrededor de la línea del traceback (respuesta a "¿y si la función
   tiene 2000 líneas?"). Se agregan el código del test (+ `setUp`), los datos de test y la salida recortada.
5. **Formato y aplicación** – bloques `ARCHIVO / <<<<<<< BUSCAR / ======= / >>>>>>> REEMPLAZAR`
   (`parsear_ediciones`, `_aplicar_en_texto`). Coincidencia exacta, luego ignorando espacios finales y
   luego ignorando la indentación (se re-indenta). Se rechaza cualquier archivo de test.
6. **Verificación** – compilación con el Python 3.8 del bug, tests objetivo y luego módulos de test.
   La regresión se compara con la línea base que midió el localizador (o, si no existe, se vuelven
   a correr con el código original solo los tests que fallan).
7. **Reintentos** – hasta 6 intentos mientras queden ≥ 25 s. Cada intento revierte el repo y lleva la
   retroalimentación de los 3 anteriores (diff + error/regresiones). Se amplía el contexto
   (3 → 5 → 8 regiones) y sube la temperatura.
8. **Robustez API** – `llm.generar` nunca lanza: reintenta 408/429/5xx/errores de red con espera
   exponencial (o el `retryDelay` del 429) sin pasarse del presupuesto; 4xx no temporales y respuestas
   vacías devuelven `None` y el agente sigue (sin LLM, la localización queda con la heurística).
9. **Costo** – `llm.CONTADOR` suma `prompt_token_count` y `candidates_token_count` solo de llamadas con
   respuesta; `main.escribir_metricas` lo escribe (el desglose por etapa se imprime en la terminal).
10. **Generalización** – no hay nombres de proyectos, archivos, funciones ni tests en el código: todo sale
    de `bug.json`, de la ejecución de los tests y del AST.

## Llamadas al LLM por bug
1 (localizador) + 1 por intento de reparación (típicamente 1–3). Sin API key el agente igual termina y
escribe ambos JSON (0 llamadas).
