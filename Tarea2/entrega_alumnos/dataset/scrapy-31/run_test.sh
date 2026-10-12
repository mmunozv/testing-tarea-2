#!/usr/bin/env bash
# Ejecuta los tests que exponen el bug. Código de salida 0 = todos pasan.
# Se usa una caché de usuario limpia (XDG_CACHE_HOME) en cada ejecución: algunos
# proyectos cachean artefactos generados por su propio código (p.ej. gramáticas).
# También se restauran los archivos de test del repo: algunos tests reescriben sus propios
# datos al ejecutarse (p.ej. formatean en el sitio un archivo de tests/data/).
cd "$(dirname "$0")/repo"
source env/bin/activate
export XDG_CACHE_HOME="$(mktemp -d)"
trap 'rm -rf "$XDG_CACHE_HOME"' EXIT
tests_repo="$(git ls-files | grep -E '(^|/)(tests?|testing)/|(^|/)test_[^/]*$|_test\.py$|(^|/)conftest\.py$')"
[ -z "$tests_repo" ] || echo "$tests_repo" | xargs git checkout -q -- 2>/dev/null
status=0
python -m unittest -q tests.test_downloadermiddleware_cookies.CookiesMiddlewareTest.test_do_not_break_on_non_utf8_header || status=1
exit $status
