#!/usr/bin/env bash
# Instala el entorno de varios bugs (setup.sh) y comprueba que sus tests FALLAN (el bug está presente).
#   bash herramientas/preparar_todos.sh                      # todos los bugs de dataset/
#   bash herramientas/preparar_todos.sh dataset/black-1 dataset/black-2
#   FORZAR=1 bash herramientas/preparar_todos.sh             # reinstala aunque ya exista repo/env
# Si Python 3.8 no está en pyenv: PYTHON=/ruta/a/python3.8 bash herramientas/preparar_todos.sh
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
[ $# -gt 0 ] && BUGS=("$@") || BUGS=("$RAIZ"/dataset/*/)
ok=0; mal=0
for B in "${BUGS[@]}"; do
  B="$(cd "$B" && pwd)"; id="$(basename "$B")"
  [ -f "$B/bug.json" ] || { echo "$id: no es un paquete de bug (falta bug.json)"; continue; }
  printf '%-14s ' "$id"
  if [ -d "$B/repo/env" ] && [ -z "$FORZAR" ]; then
    printf 'entorno ya instalado  '
  elif bash "$B/setup.sh" > "$B/setup.log" 2>&1; then
    printf 'entorno instalado     '
  else
    echo "ERROR en setup.sh (ver $B/setup.log)"; mal=$((mal+1)); continue
  fi
  if bash "$B/run_test.sh" > /dev/null 2>&1; then
    echo "AVISO: los tests PASAN; ¿repo/ tiene cambios? (cd $B/repo && git diff)"; mal=$((mal+1))
  else
    echo "tests fallan: listo"; ok=$((ok+1))
  fi
done
echo "Listos: $ok | con problemas: $mal"
