#!/usr/bin/env python3
"""Pin a local Zallet native lifecycle replay to the released Zinder fixture."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ZINDER_REVISION = '830b426060169f5b2e94d84ff0f134bc1ad96004'
ZALLET_REVISION = '38ef6a9b344063ca17b9989eeab22b68a82b6394'
STEPS = ('bootstrap', 'restore', 'transparent-discovery', 'shielded-discovery',
         'send', 'pending', 'mined', 'scanned', 'restart', 'resume')


def digest(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zallet-root', type=Path, required=True)
    parser.add_argument('--backend-binary', type=Path)
    parser.add_argument('--ready-receipt', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.zallet_root.resolve()
    receipt = args.ready_receipt.resolve()
    ready = json.loads(receipt.read_text())
    if ready['release'] != 'v0.21.0' or ready['zinder_commit'] != ZINDER_REVISION:
        parser.error('the fixture must identify the exact released Zinder v0.21.0')
    revision = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != ZALLET_REVISION:
        parser.error('Zallet must be at the reviewed native integration revision ' + ZALLET_REVISION)
    interpreter = Path(sys.executable).resolve()
    adapter = Path(__file__).with_name('native_wallet_lifecycle.py').resolve()
    templates = adapter.parents[1] / 'defaults/zallet'
    client = args.backend_binary.resolve() if args.backend_binary else root / 'backends/zinder/target/debug/zallet-zinder'
    launcher = root / 'target/debug/zallet'
    inputs = [interpreter, Path('/usr/bin/script').resolve(), adapter, Path(__file__).resolve(), client, launcher,
              receipt, templates / 'zallet.toml', templates / 'encryption-identity.txt']

    def command(step):
        return {'command': [str(interpreter), str(adapter), '--zallet-root', str(root),
                            '--template-dir', str(templates), '--backend-binary', str(client), step, '--scratch', '{scratch}',
                            '--report', '{report}', '--endpoint', '{endpoint}',
                            '--node', ready['node_rpc']],
                'expect': {'/assertions/' + step: True}}

    case = {'wallet': 'zallet', 'version': '0.1.0-beta.4-38ef6a9-native-0.21.0',
            'variant': 'ordinary' if args.backend_binary else 'certification', 'network': 'zcash-regtest', 'implementation': 'app',
            'interface': 'native-wallet-query', 'endpoint': ready['native_wallet_query'],
            'require_no_fallback': True, 'restart_scope': 'process-restart',
            'client': {'path': str(client), 'sha256': digest(client)},
            'inputs': {str(path): digest(path) for path in inputs},
            'steps': {step: command(step) for step in STEPS}, 'cleanup': command('cleanup')}
    args.output.write_text(json.dumps({'schema_version': 2, 'timeout_seconds': 360,
                                     'cases': {'zallet-native': case}}, indent=2) + '\n')


if __name__ == '__main__':
    main()
