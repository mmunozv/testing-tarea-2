#!/usr/bin/env bash
# Crea el entorno del bug en repo/env. Requiere Python 3.8.3 (por defecto vía pyenv).
#   PYTHON=/ruta/a/python3.8 bash setup.sh
set -e
# `timeout` es de GNU coreutils (en macOS con Homebrew se llama gtimeout); si no hay, sin límite
if command -v timeout >/dev/null 2>&1; then CON_LIMITE="timeout 300"
elif command -v gtimeout >/dev/null 2>&1; then CON_LIMITE="gtimeout 300"
else CON_LIMITE=""; fi
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-$HOME/.pyenv/versions/3.8.3/bin/python}"
# si no está la versión exacta, sirve cualquier 3.8.x de pyenv o python3.8 del sistema
[ -x "$PY" ] || PY="$(ls -d $HOME/.pyenv/versions/3.8.*/bin/python 2>/dev/null | tail -1)"
[ -x "$PY" ] || PY="$(command -v python3.8)" || { echo "Falta Python 3.8.3"; exit 1; }
cd "$HERE/repo"
rm -rf env
"$PY" -m venv env
source env/bin/activate
pip install -q --disable-pip-version-check --upgrade "pip<24" "setuptools<70" wheel
if ! pip install -q --disable-pip-version-check -r "$HERE/requirements.txt"; then
  # requirements.txt es un `pip freeze` completo: paquete a paquete y sin resolver dependencias,
  # para respetar exactamente las versiones fijadas (se omiten las que no existen en esta
  # plataforma, p.ej. pywin32 en un freeze hecho en Windows)
  echo "(instalación en bloque falló; instalando paquete a paquete con las versiones exactas)"
  while read -r req; do
    pip install -q --disable-pip-version-check --no-deps "$req" 2>/dev/null || echo "  no se pudo instalar: $req"
  done < "$HERE/requirements.txt"
fi
# A partir de aquí las versiones fijadas por requirements.txt se congelan como restricciones:
# lo que se instale después (extras, herramientas) nunca las actualiza (p.ej. pytest==5.4.2)
CONS="$(mktemp)"
pip freeze --exclude-editable > "$CONS"
PIPC="pip install -q --disable-pip-version-check -c $CONS"
# proyecto en modo editable + sus dependencias de test declaradas como extras (solo añade lo que falte)
$PIPC -e ".[test,tests,testing]" 2>/dev/null \
  || $PIPC --no-deps -e . 2>/dev/null \
  || echo "(el proyecto no se instaló en modo editable; se usa desde el directorio)"
# requisitos de test declarados en archivos del propio repo (p.ej. tests/requirements-py3.txt),
# paquete a paquete: si alguno no se puede instalar con las versiones fijadas, se omite
for f in tests/requirements*.txt test/requirements*.txt requirements[-_]test*.txt test[-_]requirements*.txt \
         requirements[-_]dev*.txt dev[-_]requirements*.txt; do
  [ -f "$f" ] || continue
  echo "requisitos de test: $f"
  # se omiten herramientas que no hacen falta para ejecutar los tests (cobertura, paralelismo,
  # linters, docs): versiones antiguas de esos plugins pueden romper pytest
  grep -vE '^[[:space:]]*(#|-|$)' "$f" \
    | grep -viE '^\s*(pytest-cov|pytest-xdist|pytest-pep8|pytest-flake8|coveralls|codecov|flake8|pylint|mypy|tox|sphinx|black|isort)\b' \
    | while read -r req; do
    $CON_LIMITE $PIPC "$req" 2>/dev/null || echo "  omitido: $req"
  done
done
pip freeze --exclude-editable > "$CONS"
# herramientas para ejecutar y analizar tests (si el proyecto no las trae ya), compatibles con lo fijado
python -c "import pytest" 2>/dev/null || $PIPC "pytest<8"
pip freeze --exclude-editable > "$CONS"
python -c "import coverage" 2>/dev/null || $PIPC "coverage<8" || echo "(no se pudo instalar coverage)"
python -c "import pytest_timeout" 2>/dev/null || $PIPC "pytest-timeout<3" || echo "(no se pudo instalar pytest-timeout)"
# pasos adicionales del bug
pip install Twisted
pip install pytest
pip install Pillow
pip install utils
pip install mitmproxy
pip install leveldb
# plugins de pytest incompatibles con la versión fijada de pytest: se desinstalan
for i in 1 2 3; do
  bad=$(python -m pytest --collect-only -q -p no:cacheprovider tests/test_spider.py 2>&1 \
        | sed -n "s/.*PluginValidationError.*<module '\([A-Za-z0-9_]*\).*/\1/p" | head -1)
  [ -z "$bad" ] && break
  echo "desinstalando plugin de pytest incompatible: $bad"
  pip uninstall -y -q "${bad//_/-}" || pip uninstall -y -q "$bad" || break
done
# dependencias de test no declaradas: se detectan al recolectar los tests y se instalan
for i in 1 2 3 4 5; do
  miss=$(python -m pytest --collect-only -q -p no:cacheprovider tests/test_spider.py 2>&1 \
         | sed -n "s/.*No module named '\([^'.]*\).*/\1/p" | head -1)
  [ -z "$miss" ] && break
  echo "instalando dependencia de test no declarada: $miss"
  $PIPC "$miss" 2>/dev/null || $PIPC "${miss//_/-}" || break
done
echo "Entorno listo: $(python --version)"
