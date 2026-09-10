# PyInstaller spec — genera un único ejecutable por plataforma:
#   pyinstaller zoom2txt.spec
# El usuario final no necesita Python; solo yt-dlp junto al .exe o en PATH.
import sys
import os
import json
import hashlib
from pathlib import Path

# Isolate DLL discovery from unrelated tools on the developer's PATH.
# In particular, a Poppler/Conda ICU DLL is not Windows' ICU API used by Qt.
if sys.platform == 'win32':
    windows_root = Path(os.environ['SystemRoot'])
    python_root = Path(sys.executable).resolve().parent
    os.environ['PATH'] = os.pathsep.join(map(str, (
        windows_root / 'System32', windows_root, python_root, python_root / 'Scripts'
    )))

runtime_platform = 'win-x64' if sys.platform == 'win32' else 'linux-x64'
runtime_name = 'deno.exe' if sys.platform == 'win32' else 'deno'
runtime_root = Path(SPECPATH) / 'assets' / 'runtime' / runtime_platform
manifest = json.loads((runtime_root / 'manifest.json').read_text(encoding='utf-8'))
for filename, field in ((runtime_name, 'executable_sha256'), ('LICENSE.deno.txt', 'license_sha256')):
    with (runtime_root / filename).open('rb') as runtime_file:
        if hashlib.file_digest(runtime_file, 'sha256').hexdigest() != manifest[field]:
            raise RuntimeError('Runtime integrity check failed: ' + filename)

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/fonts','assets/fonts'),('assets/check.svg','assets'),
           ('assets/LICENSE.lucide.txt','assets'),('assets/ui','assets/ui'),
           (str(runtime_root / runtime_name), 'runtime'),
           (str(runtime_root / 'LICENSE.deno.txt'), 'runtime'),
           (str(runtime_root / 'manifest.json'), 'runtime')],
    hiddenimports=[
        'keyring.backends.Windows',
        'keyring.backends.SecretService',
        'keyring.backends.kwallet',
        'googleapiclient',
        'google_auth_oauthlib',
        'tzdata',
    ],
    hooksconfig={},
    excludes=['tkinter'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Zoom2TXT',
    icon='assets/zoom2txt.ico',
    version='assets/version_info.txt' if sys.platform == 'win32' else None,
    debug=False,
    strip=False,
    upx=False,
    console=False,          # app de ventana, sin consola
    disable_windowed_traceback=False,
)
