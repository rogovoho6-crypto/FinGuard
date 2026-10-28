"""Check Git's tracked/staged paths before publishing or running CI."""
from pathlib import PurePosixPath
import subprocess
import sys

files = subprocess.check_output(['git', 'ls-files', '--cached', '-z']).decode('utf-8').split('\0')
bad = []
for file in filter(None, files):
    p = PurePosixPath(file)
    if (p.name.startswith('.env') and p.name != '.env.example'
            or p.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.pem', '.key', '.pfx', '.p12', '.zip')
            or any(part in ('.venv', 'node_modules', '__pycache__', 'data', 'backups', 'validation') for part in p.parts)
            or file.startswith(('frontend/dist/', 'build/', 'dist/'))):
        bad.append(file)
if bad:
    print('Remove private/runtime files from the Git index:\n' + '\n'.join(bad))
    sys.exit(1)
print(f'PASS: {len(list(filter(None, files)))} tracked paths; no forbidden runtime files. Review file contents before publishing.')
