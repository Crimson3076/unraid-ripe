#!/usr/bin/env bash
# Optional cloud workaround: download wheels on the host, then install them
# without network access in the builder. TLS/hash verification remains enabled.
set -euo pipefail

ripe_script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ripe_root=$(CDPATH= cd -- "$ripe_script_dir/.." && pwd)
ripe_python=${RIPE_BUILD_PYTHON:-python3}
ripe_image=${1:-ripe:stage1}
ripe_build_dir=$(mktemp -d "${TMPDIR:-/tmp}/ripe-offline-build.XXXXXX")
trap 'rm -rf -- "$ripe_build_dir"' EXIT

"$ripe_python" - <<'PY'
import platform, sys
if sys.version_info[:2] != (3, 12) or platform.machine() != 'x86_64' or platform.system() != 'Linux':
    raise SystemExit('This optional helper currently requires Linux x86_64 and Python 3.12, matching the target image.')
PY

"$ripe_python" -m pip download --disable-pip-version-check --no-cache-dir \
  --only-binary=:all: --no-deps -r "$ripe_root/requirements.lock" \
  -d "$ripe_build_dir/wheels"

"$ripe_python" - "$ripe_root" "$ripe_build_dir" <<'PY'
from pathlib import Path
import shutil, sys
root, build = map(Path, sys.argv[1:])
for name in ('requirements.lock', 'pyproject.toml'):
    shutil.copy2(root/name, build/name)
shutil.copytree(root/'ripe', build/'ripe', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
dockerfile = (root/'Dockerfile').read_text()
online = 'RUN python -m pip install --no-cache-dir --no-deps -r requirements.lock'
offline = ('COPY wheels /tmp/ripe-wheels\n'
           'RUN python -m pip install --no-cache-dir --no-deps --no-index '
           '--find-links=/tmp/ripe-wheels -r requirements.lock '
           '&& rm -rf /tmp/ripe-wheels')
if dockerfile.count(online) != 1:
    raise SystemExit('Dockerfile install command changed; review this helper before use.')
(build/'Dockerfile').write_text(dockerfile.replace(online, offline))
PY

docker build --network=none --tag "$ripe_image" "$ripe_build_dir"
