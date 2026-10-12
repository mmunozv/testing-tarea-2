#!/usr/bin/env bash
# Ejecuta el agente sobre varios bugs (ya preparados) y resume los resultados en una tabla.
#   bash herramientas/ejecutar_lote.sh                      # todos los bugs de dataset/
#   bash herramientas/ejecutar_lote.sh dataset/black-1 dataset/black-2
# Cada bug parte del código original: se DESCARTAN los cambios que haya en su repo/ y no se guarda
# ninguna copia de ellos (si quieres conservarlos, hazlo antes, p. ej. con git diff > cambios.patch).
# La salida del agente se muestra en la terminal.
# Resultados en resultados/lote-<fecha>/: resumen.csv y, por bug, localizacion.json y metricas.json.
# Límite de tiempo por bug: LIMITE=180 segundos por defecto (el mismo de la evaluación; no incluye setup.sh).
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
AGENTE="$RAIZ/agente"
PY="$AGENTE/.venv/bin/python"; [ -x "$PY" ] || PY=python3
[ -f "$AGENTE/config.env" ] && { set -a; source "$AGENTE/config.env"; set +a; }
[ -n "$GEMINI_API_KEY" ] || { echo "Falta GEMINI_API_KEY (agente/config.env)"; exit 1; }
export PYTHONUNBUFFERED=1   # la salida del agente aparece en la terminal al momento
[ $# -gt 0 ] && BUGS=("$@") || BUGS=("$RAIZ"/dataset/*/)
SALIDA="$RAIZ/resultados/lote-$(date +%Y%m%d-%H%M%S)"; mkdir -p "$SALIDA"
CSV="$SALIDA/resumen.csv"
echo "bug;resuelto;llamadas;tokens_entrada;tokens_salida;segundos" > "$CSV"

# con_limite SEGUNDOS comando...: ejecuta el comando y lo detiene si supera el límite (código 124).
# Usa `timeout` (Linux), `gtimeout` (macOS con `brew install coreutils`) o, si no hay ninguno, Python.
con_limite() {
  if command -v timeout >/dev/null 2>&1; then timeout "$@"
  elif command -v gtimeout >/dev/null 2>&1; then gtimeout "$@"
  else python3 -c '
import os, signal, subprocess, sys
p = subprocess.Popen(sys.argv[2:], start_new_session=True)
try:
    sys.exit(p.wait(timeout=float(sys.argv[1])))
except subprocess.TimeoutExpired:
    os.killpg(p.pid, signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
    sys.exit(124)
' "$@"
  fi
}

for B in "${BUGS[@]}"; do
  B="$(cd "$B" && pwd)"; id="$(basename "$B")"; D="$SALIDA/$id"; mkdir -p "$D"
  [ -d "$B/repo/env" ] || { echo "$id: sin entorno (bash herramientas/preparar_todos.sh $B)"; continue; }
  echo "== $id"
  (cd "$B/repo" && git checkout -q -- .)
  rm -f "$B/localizacion.json" "$B/metricas.json"
  (cd "$AGENTE" && con_limite "${LIMITE:-180}" "$PY" main.py "$B") \
    || echo "   el agente terminó con error o superó el límite de ${LIMITE:-180}s"
  bash "$B/run_test.sh" > /dev/null 2>&1 && res=si || res=no   # comprobación independiente del agente
  cp "$B/localizacion.json" "$B/metricas.json" "$D/" 2>/dev/null
  python3 - "$B/metricas.json" "$id" "$res" >> "$CSV" <<'P'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    m = {}
print(";".join([sys.argv[2], sys.argv[3]] + [str(m.get(k, "")) for k in
               ("llamadas", "tokens_entrada", "tokens_salida", "segundos")]))
P
  echo "   resuelto: $res"
done

echo; column -t -s ';' "$CSV"
python3 - "$CSV" <<'P'
import csv, sys
filas = list(csv.DictReader(open(sys.argv[1]), delimiter=";"))
n = sum(f["resuelto"] == "si" for f in filas)
print(f"\nResueltos: {n}/{len(filas)}")
P
echo "Resultados en $SALIDA"
