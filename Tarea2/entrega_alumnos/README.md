# Tarea: agente de reparación automática de bugs

Construye un agente con Gemini (`gemini-3.5-flash-lite`) que **localice y repare bugs reales de proyectos Python**
usando solo el repositorio con el bug y los tests que lo exponen.

```
agente/            esqueleto que debes completar
├── main.py        punto de entrada: localizar -> reparar -> escribir resultados
├── localizador.py localizar(carpeta, info) -> {"regiones": [...]}
├── reparador.py   reparar(carpeta, info, localizacion) -> {"resuelto": ...}
├── requirements.txt    dependencias del agente (google-genai)
└── config.env.example  plantilla para tu API key
herramientas/      utilidades (no se evalúan): instalar bugs, verificar resultados, ejecutar lotes
dataset/
├── bugs.csv       índice: id, proyecto, versión de Python, runner y tests que fallan
└── <proyecto>-N/  un paquete por bug (bugs reales de los proyectos black, fastapi y scrapy)
```

Tu agente se evaluará también con **bugs que no están en este dataset** (con el mismo formato de paquete),
así que no puede depender de nada específico de ningún bug ni proyecto: ni nombres de archivos, ni de
funciones, ni de tests.

## Requisitos

- **Linux, macOS o WSL (Ubuntu).**
- **Python 3.14** para el agente (probado con 3.14.4). Con pyenv: `pyenv install 3.14.4`.
- **Python 3.8.x** para ejecutar los bugs, en un entorno aparte (la versión exacta está en `bug.json`). Lo
  más simple es pyenv: `pyenv install 3.8.3`. Si lo tienes en otra ruta: `PYTHON=/ruta/a/python3.8 bash setup.sh`.
- **Una API key de Gemini:** https://aistudio.google.com/apikey

## El paquete de un bug

Todos los bugs tienen la misma forma, sea cual sea el proyecto:

```
black-2/
├── repo/              código del proyecto con el bug (git con un único commit)
├── bug.json           metadatos del bug
├── requirements.txt   dependencias del proyecto en la versión del bug
├── setup.sh           crea el entorno del bug en repo/env
└── run_test.sh        ejecuta los tests que exponen el bug
```

`bug.json`:

| Campo | Qué es |
|---|---|
| `id`, `proyecto`, `python` | Identificador, proyecto y versión de Python del bug |
| `comandos_test` | Comandos que ejecutan los tests que fallan (los mismos que usa `run_test.sh`, desde `repo/`) |
| `archivos_test` | Archivos de test relacionados con el bug |
| `modulos_test` | Módulos de test completos, útiles para detectar si tu cambio rompe otra cosa |

## Uso

```bash
bash dataset/black-2/setup.sh        # una vez por bug: instala repo/env (1-2 minutos)
bash dataset/black-2/run_test.sh     # ejecuta los tests del bug
echo $?                              # 1 = fallan (bug presente), 0 = pasan
```

- `run_test.sh` imprime la salida de los tests (incluido el traceback) y **restaura los archivos de test**
  del repo antes de ejecutarlos: cualquier cambio en los tests se pierde.
- Para ejecutar otros tests, usa el entorno del bug desde `repo/`:
  ```bash
  cd dataset/black-2/repo && source env/bin/activate
  python -m pytest tests/test_black.py      # p. ej. uno de los modulos_test
  ```
- `repo/` es un repositorio git: `git diff` muestra lo que ha cambiado y `git checkout -- .` vuelve al código original.

## El agente

El agente usa su propio entorno (**Python 3.14**), distinto del de cada bug (Python 3.8). Para instalarlo, una sola vez:

```bash
cd agente
python3.14 -m venv .venv             # o ~/.pyenv/versions/3.14.4/bin/python -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.env.example config.env     # y pega tu clave en GEMINI_API_KEY
```

Para ejecutarlo, con el entorno del bug ya instalado:

```bash
cd agente
set -a; source config.env; set +a    # exporta GEMINI_API_KEY
.venv/bin/python main.py ../dataset/black-2
```

Con `GEMINI_API_KEY` exportada, `genai.Client()` toma la clave sola. **No entregues `config.env`:** contiene tu clave.

Al terminar deben quedar en la carpeta del bug:

- la reparación aplicada en `repo/`;
- `localizacion.json`, con lo que devolvió el localizador;
- `metricas.json` (ver [Métricas](#métricas)).

Cada archivo del esqueleto indica en su docstring qué recibe, qué devuelve y cómo probarlo por separado:

| Archivo | Comando | Recibe | Devuelve |
|---|---|---|---|
| `main.py` | `python main.py <carpeta_del_bug>` | carpeta del bug | escribe `localizacion.json` y `metricas.json` |
| `localizador.py` | `python localizador.py <carpeta_del_bug>` | carpeta del bug y `bug.json` | `{"regiones": [{"archivo", "inicio", "fin"}]}` |
| `reparador.py` | `python reparador.py <carpeta_del_bug> <localizacion.json>` | carpeta, `bug.json` y localización | `{"resuelto": bool}` y el código de `repo/` modificado |

Puedes añadir los módulos, dependencias y campos extra que quieras, pero **no cambies** el comando de `main.py`
ni los formatos de `localizacion.json` y `metricas.json`: se usan para evaluar.

## Herramientas

Tres utilidades para no perder tiempo en lo que no se evalúa. Se ejecutan desde la raíz de la entrega.

| Comando | Qué hace |
|---|---|
| `bash herramientas/preparar_todos.sh [bugs...]` | Ejecuta `setup.sh` de todos los bugs (o de los indicados) y comprueba que sus tests fallan. Los ya instalados se saltan (`FORZAR=1` para reinstalar). |
| `python3 herramientas/verificar.py dataset/black-2` | Revisa el resultado de tu agente en un bug: tests del bug, regresión, archivos de test modificados y formato de `localizacion.json` y `metricas.json`. |
| `bash herramientas/ejecutar_lote.sh [bugs...]` | Ejecuta el agente sobre varios bugs (con el límite de 3 minutos) mostrando su salida en la terminal, y genera `resultados/lote-<fecha>/` con `resumen.csv` (resuelto, llamadas, tokens y tiempo por bug) y, por bug, `localizacion.json` y `metricas.json`. |

Sobre `verificar.py`:
- La regresión se compara con los tests que pasaban **con el código original**. La primera vez
  mide esa línea base quitando temporalmente tus cambios (tarda unos minutos) y la guarda en `.linea_base.json`.
- La evaluación real compara con los tests que pasan con la solución oficial, así que el resultado puede
  diferir en casos raros. Si aquí algo falla, en la evaluación también fallará.

Sobre `ejecutar_lote.sh`:
- **Descarta los cambios** que haya en `repo/` de cada bug antes de ejecutar el agente, para que todos
  partan del código original, y **no guarda ninguna copia** de esos cambios ni de los que haga el agente.
  Si quieres conservarlos, hazlo antes (p. ej. `git -C dataset/black-2/repo diff > cambios.patch`).
- La columna `resuelto` solo mira los tests del bug (`run_test.sh`); para revisar también la regresión y los
  formatos de un bug, usa `verificar.py`.

**Para la entrega:** incluye la carpeta `resultados/lote-<fecha>/` de la ejecución de la **versión final** de
tu agente sobre los **12 bugs públicos** (`bash herramientas/ejecutar_lote.sh`, sin argumentos).

## Reglas

1. **Solo puedes modificar código fuente,** nunca archivos de test. Para evaluar se restauran los tests
   originales, así que cambiarlos no sirve de nada.
2. **El agente no puede usar información externa al paquete:** nada de buscar el fix en internet
   ni en ninguna otra fuente fuera de lo entregado.
3. **El agente deja la reparación aplicada en `repo/`.** Se evalúa el estado de `repo/` al terminar.
4. **Modelo obligatorio: `gemini-3.5-flash-lite`** para todas las llamadas al LLM. No se admiten otros modelos
   ni otros proveedores.
5. **Reporta el coste por bug** en `metricas.json` (ver abajo).
6. **El localizador y el reparador son etapas separadas:** el reparador recibe la localización como entrada.
7. **Límite de tiempo: 3 minutos por bug** para el agente (`python main.py <bug>`), sin contar la
   instalación del entorno (`setup.sh`), que se hace antes y por separado. Si el agente no termina en ese
   tiempo, se detiene y el bug cuenta como no reparado.

## Métricas

Al terminar con cada bug, el agente escribe `metricas.json` en la carpeta del paquete
(`dataset/black-2/metricas.json`, fuera de `repo/`), con exactamente estos campos:

```json
{
  "bug": "black-2",
  "modelo": "gemini-3.5-flash-lite",
  "llamadas": 2,
  "tokens_entrada": 3634,
  "tokens_salida": 448,
  "segundos": 131.4
}
```

| Campo | Qué es |
|---|---|
| `bug` | `id` del bug (de `bug.json`) |
| `modelo` | Siempre `gemini-3.5-flash-lite` |
| `llamadas` | Llamadas al LLM que terminaron con respuesta (no cuentes los reintentos fallidos) |
| `tokens_entrada`, `tokens_salida` | Suma, en todas las llamadas, de `usage_metadata.prompt_token_count` y `usage_metadata.candidates_token_count` de cada respuesta de Gemini |
| `segundos` | Tiempo total del agente sobre el bug, de principio a fin: incluye la ejecución de tests que haga el agente, pero **no** la instalación del entorno (`setup.sh`) |

Cuenta **todas** las llamadas al LLM que haga el agente, sean de la etapa que sean.

## Consejos prácticos

**Entorno**
- **En WSL, trabaja dentro del sistema de archivos de WSL** (`~/...`), no en `/mnt/c/...`: allí todo es
  mucho más lento y se rompen los enlaces simbólicos del repo. Descomprime el .zip ya dentro de WSL (`unzip`).
- **Instalar Python 3.8 (bugs) y 3.14 (agente) con pyenv en Linux o WSL (Ubuntu):**
  ```bash
  sudo apt install -y build-essential curl git libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
       libsqlite3-dev libffi-dev liblzma-dev
  curl https://pyenv.run | bash      # y sigue las instrucciones para añadirlo a ~/.bashrc
  pyenv install 3.8.3                # para los bugs
  pyenv install 3.14.4               # para el agente
  ```
- **Instalar Python 3.8 (bugs) y 3.14 (agente) con pyenv en macOS (Homebrew, shell zsh):**
  ```bash
  brew install pyenv
  echo 'export PYENV_ROOT="$HOME/.pyenv"' >> ~/.zshrc
  echo '[[ -d $PYENV_ROOT/bin ]] && export PATH="$PYENV_ROOT/bin:$PATH"' >> ~/.zshrc
  echo 'eval "$(pyenv init - zsh)"' >> ~/.zshrc
  exec zsh                           # recarga la configuración del shell
  pyenv install 3.8.3                # para los bugs
  pyenv install 3.14.4               # para el agente
  ```
  En Mac con **Apple Silicon** (M1/M2/M3…), compilar Python 3.8.3 puede requerir las dependencias de
  Homebrew: `brew install openssl readline xz zlib`. Si aun así no compila, instala una versión 3.8 más
  reciente (p. ej. `pyenv install 3.8.18`): `setup.sh` usa cualquier 3.8.x instalada con pyenv.
- **macOS y `timeout`:** macOS no trae el comando `timeout`. Las herramientas funcionan igual sin él (usan
  una alternativa en Python), pero si instalas `brew install coreutils` usan `gtimeout`, igual que en Linux.
- **Tiempos orientativos:** `setup.sh` tarda de 30 segundos a 4 minutos por bug según la máquina, así que
  `preparar_todos.sh` puede necesitar de 10 a 50 minutos para los 12 bugs (se hace una sola
  vez). Ejecutar un agente sobre todos los bugs ya instalados lleva, como máximo, 3 minutos por bug
  (36 minutos en el peor caso; un agente eficiente, mucho menos). Los tests de un bug tardan segundos; los
  `modulos_test` completos, hasta unos minutos. Tenlo en cuenta con el límite de 3 minutos: ejecutar
  muchas veces los `modulos_test` completos dentro del agente puede consumirlo.

**Gemini**
- **El plan gratuito tiene límites** de peticiones por minuto y por día (en nuestras pruebas, **500
  peticiones al día por proyecto de Google y por modelo**; pueden cambiar, consúltalos en
  https://ai.google.dev/gemini-api/docs/rate-limits y en AI Studio). Si tu agente hace ~3 llamadas por
  bug, un lote con los 12 bugs gasta ~36 peticiones. Prueba con pocos bugs y reserva los lotes completos.
- **Gemini devuelve a veces 503 (saturado) o 429 (límite alcanzado).** Son temporales: tu agente debería
  reintentar con una espera creciente en vez de fallar. No dejes las ejecuciones finales para el último día.

## Evaluación

Para cada bug: primero se instala su entorno (`setup.sh`, sin límite de tiempo) y después se ejecuta tu
agente con un límite de **3 minutos**. Un bug se considera reparado si el agente termina a tiempo y, con los
tests originales restaurados:

- los tests de `run_test.sh` pasan, y
- no se rompe ningún test que pasaba con la solución oficial del bug.

Tu agente se ejecuta **dos veces** sobre cada bug (el modelo no es totalmente determinista): el bug cuenta
como reparado si lo repara en **alguna** de las dos. La evaluación usa estos 12 bugs y otros 12 que no están en
este dataset (de los mismos proyectos y con una dificultad similar). Todos se pueden reparar con `gemini-3.5-flash-lite` dentro del
límite de tiempo.

Las ejecuciones de evaluación usan la API key del equipo docente: tu `config.env` no se usa (y no debes
entregarlo). Tu agente debe leer la clave de la variable de entorno `GEMINI_API_KEY`.
