#!/usr/bin/env bash
# Crea el entorno del bug en repo/env. Requiere Python 3.8.3 (por defecto vía pyenv).
#   PYTHON=/ruta/a/python3.8 bash setup.sh
set -e
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
  echo "(instalación en bloque falló; instalando paquete a paquete)"
  while read -r req; do pip install -q --disable-pip-version-check "$req" || echo "  no se pudo instalar: $req"; done < "$HERE/requirements.txt"
fi
# proyecto en modo editable + sus dependencias de test declaradas como extras (solo añade lo que falte)
pip install -q --disable-pip-version-check -e ".[test,tests,testing]" 2>/dev/null \
  || pip install -q --disable-pip-version-check --no-deps -e . 2>/dev/null \
  || echo "(el proyecto no se instaló en modo editable; se usa desde el directorio)"
# herramientas para ejecutar y analizar tests (si el proyecto no las trae ya)
python -c "import pytest" 2>/dev/null || pip install -q --disable-pip-version-check "pytest<8"
python -c "import coverage" 2>/dev/null || pip install -q --disable-pip-version-check "coverage<8"

# dependencias de test no declaradas: se detectan al recolectar los tests y se instalan
for i in 1 2 3 4 5; do
  miss=$(python -m pytest --collect-only -q -p no:cacheprovider tests/test_datetime_custom_encoder.py tests/test_jsonable_encoder.py 2>&1 \
         | sed -n "s/.*No module named '\([^'.]*\).*/\1/p" | head -1)
  [ -z "$miss" ] && break
  echo "instalando dependencia de test no declarada: $miss"
  pip install -q --disable-pip-version-check "$miss" 2>/dev/null \
    || pip install -q --disable-pip-version-check "${miss//_/-}" || break
done
echo "Entorno listo: $(python --version)"
