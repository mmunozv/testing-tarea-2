#!/usr/bin/env bash
# Reconstruye dataset/<bug>/repo/ (código con el bug, git con un único commit) desde BugsInPy,
# para cuando no se tiene la carpeta repo/ del Tarea2.zip del curso.
#   bash herramientas/reconstruir_repos.sh                 # todos los bugs de dataset/
#   bash herramientas/reconstruir_repos.sh black-12 scrapy-4
# Estado "con bug" de BugsInPy: tests del commit corregido + código fuente con el bug
# (se aplica al revés el bug_patch.txt, que solo toca código fuente).
# Después: bash herramientas/preparar_todos.sh   (instala repo/env y comprueba que los tests fallan)
set -e
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="${CACHE:-$HOME/.cache/tarea2_bugsinpy}"
mkdir -p "$CACHE"
[ -d "$CACHE/BugsInPy" ] || git clone -q --depth 1 https://github.com/soarsmu/BugsInPy "$CACHE/BugsInPy"

[ $# -gt 0 ] && BUGS=("$@") || BUGS=($(cd "$RAIZ/dataset" && ls -d */ | tr -d /))
for id in "${BUGS[@]}"; do
  id="$(basename "$id")"; proyecto="${id%-*}"; n="${id##*-}"
  B="$RAIZ/dataset/$id"; INFO="$CACHE/BugsInPy/projects/$proyecto/bugs/$n"
  [ -f "$B/bug.json" ] && [ -f "$INFO/bug.info" ] || { echo "$id: no encontrado"; continue; }
  if [ -d "$B/repo/.git" ] && [ -z "$FORZAR" ]; then echo "$id: repo/ ya existe (FORZAR=1 para rehacerlo)"; continue; fi
  url="$(sed -n 's/^github_url="\(.*\)"/\1/p' "$CACHE/BugsInPy/projects/$proyecto/project.info")"
  fixed="$(sed -n 's/^fixed_commit_id="\(.*\)"/\1/p' "$INFO/bug.info")"
  [ -d "$CACHE/$proyecto" ] || git clone -q "$url" "$CACHE/$proyecto"
  rm -rf "$B/repo"; mkdir -p "$B/repo"
  git -C "$CACHE/$proyecto" archive "$fixed" | tar -x -C "$B/repo"
  (cd "$B/repo" && git init -q && git apply -R "$INFO/bug_patch.txt" \
     && git add -A && git -c user.name=bug -c user.email=bug@local commit -q -m initial)
  echo "$id: repo/ listo"
done
