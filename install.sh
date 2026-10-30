#!/bin/sh
set -eu
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/python" - "$APP_DIR" <<'PY'
import os
import sys
from pathlib import Path

app = Path(sys.argv[1])
applications = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'applications'
applications.mkdir(parents=True, exist_ok=True)
# Escape Desktop Entry Exec reserved characters, including paths containing spaces.
launcher = str(app / 'run.sh')
for char in ('\\', '"', '`', '$'):
    launcher = launcher.replace(char, '\\' + char)
launcher = launcher.replace('%', '%%')
launcher = launcher.replace('\\', '\\\\')
(applications / 'simplelimite.desktop').write_text(
    '[Desktop Entry]\nType=Application\nName=Simple Limite\n'
    'Comment=Monitor de uso de assistentes de programação\n'
    f'Exec=sh "{launcher}"\nIcon={app / "docs/logo.png"}\n'
    'Terminal=false\nCategories=Utility;System;\n', encoding='utf-8')
PY
printf '%s\n' 'Instalado. Abra Simple Limite pelo menu do KDE ou execute: sh run.sh'
