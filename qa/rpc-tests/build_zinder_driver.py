#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license.
"""Build the external native Zinder scenario driver against a Zallet checkout."""
import argparse
import json
from pathlib import Path
import subprocess
import toml

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--zallet-root', type=Path, required=True)
parser.add_argument('--build-dir', type=Path, required=True)
args = parser.parse_args()
root = args.zallet_root.resolve()
build = args.build_dir.resolve()
build.mkdir(parents=True, exist_ok=True)
manifest = {
    'package': {'name': 'zallet-zinder-scenarios', 'version': '0.0.0', 'edition': '2024'},
    'workspace': {},
    'lib': {'path': str(Path(__file__).with_name('zinder_driver.rs').resolve())},
    'dev-dependencies': {
        'zallet-zinder': {'path': str(root / 'backends/zinder'), 'features': ['bounded-scan-certification']},
        'zallet-core': {'path': str(root / 'zallet-core'), 'features': ['bounded-scan-certification']},
        'zinder-client': '=0.21.0', 'serde_json': '1', 'toml': '0.8', 'tempfile': '3',
        'tokio': {'version': '1', 'features': ['macros', 'rt-multi-thread']},
    },
}
path = build / 'Cargo.toml'
path.write_text(toml.dumps(manifest))
result = subprocess.run(['cargo', '+1.100.0', 'test', '--manifest-path', str(path),
                         '--no-run', '--message-format=json'], text=True, stdout=subprocess.PIPE, check=True)
for line in result.stdout.splitlines():
    artifact = json.loads(line)
    if artifact.get('reason') == 'compiler-artifact' and artifact.get('profile', {}).get('test') and artifact.get('executable'):
        print(artifact['executable'])
