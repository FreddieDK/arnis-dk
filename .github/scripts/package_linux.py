"""Package only explicit distributable files; never local data or credentials."""
from pathlib import Path
import hashlib
import platform
import shutil
import subprocess
import tarfile

root = Path(__file__).resolve().parents[2]
assert platform.system() == 'Linux' and platform.machine() == 'x86_64'
dist = root / 'dist'
package = dist / 'arnis-dk-linux-x86_64'
package.mkdir(parents=True, exist_ok=False)
shutil.copy2(root / 'target/release/arnis', package / 'arnis')
subprocess.run(['strip', str(package / 'arnis')], check=True)
(package / 'arnis').chmod(0o755)
for name in ['LICENSE', 'NOTICE', 'README-LINUX.md']:
    shutil.copy2(root / name, package / name)
for name in ['fetch.py', 'prepare.py', 'meld_bridge.py', 'requirements.txt', 'README.md', 'MELD.md']:
    target = package / 'danish_data' / name
    target.parent.mkdir(exist_ok=True)
    shutil.copy2(root / 'danish_data' / name, target)
# Preserve upstream's full credits in a readable HTML document, plus its
# original source at the path referred to by NOTICE.
credits = root / 'src/gui/js/license.js'
target = package / 'src/gui/js/license.js'
target.parent.mkdir(parents=True)
shutil.copy2(credits, target)
body = credits.read_text(encoding='utf-8').split('`', 2)[1]
(package / 'CREDITS.html').write_text('<!doctype html><meta charset="utf-8"><title>Arnis credits</title>' + body, encoding='utf-8')
shutil.copy2(root / 'assets/tree-packs/ATTRIBUTION.md', package / 'TREE-ATTRIBUTION.md')
commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=root, text=True).strip()
(package / 'BUILD-INFO.txt').write_text(
    f'Arnis-DK Linux CLI\nSource: https://github.com/FreddieDK/arnis-dk\nCommit: {commit}\n'
    'Target: x86_64-unknown-linux-gnu\nBuild OS: Ubuntu 22.04 (glibc 2.35)\n'
    'Command: cargo build --release --locked --no-default-features\n'
    'This is a modified Arnis distribution. See LICENSE, NOTICE and CREDITS.html.\n', encoding='utf-8')
archive = dist / f'{package.name}.tar.gz'
with tarfile.open(archive, 'w:gz') as output:
    output.add(package, arcname=package.name)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(dist / f'{archive.name}.sha256').write_text(f'{digest}  {archive.name}\n', encoding='ascii')
print(f'{archive.name}: {archive.stat().st_size:,} bytes; SHA-256 {digest}')
