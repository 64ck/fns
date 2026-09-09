#!/usr/bin/env bash
# Быстрый запуск портала: окружение -> зависимости -> БД -> сервер.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON=${PYTHON:-python3}
if [ ! -d .venv ]; then
  echo "→ создаю виртуальное окружение .venv"
  "$PYTHON" -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

if [ ! -f data/db/fns.sqlite ]; then
  echo "→ базы нет: создаю и наполняю демонстрационными данными"
  python -m fnsportal init
  python -m fnsportal demo
  python -m fnsportal build-terms
fi

python -m fnsportal serve "$@"
