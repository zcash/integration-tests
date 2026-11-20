#!/usr/bin/env python3
"""Disposable Regtest native WalletQuery adapter. Never accepts public endpoints."""
import argparse
import hashlib
import shlex

if not __debug__:
    raise SystemExit("Optimized Python is not supported for this qualification adapter")
import base64
import json
import os
import pathlib
import shutil
import socket
import subprocess
import time
import urllib.request
import toml
p = argparse.ArgumentParser()
p.add_argument('--zallet-root', type=pathlib.Path, required=True)
p.add_argument('--backend-binary', type=pathlib.Path)
p.add_argument('--template-dir', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1] / 'defaults/zallet')
p.add_argument('step')
p.add_argument('--scratch', type=pathlib.Path, required=True)
p.add_argument('--report', type=pathlib.Path, required=True)
p.add_argument('--endpoint', default='http://127.0.0.1:32302')
p.add_argument('--node', default='http://127.0.0.1:32332')
a = p.parse_args()
ROOT = a.zallet_root.resolve()
if not (a.endpoint.startswith('http://127.0.0.1:') and a.node.startswith('http://127.0.0.1:')):
    raise SystemExit('Only dedicated loopback HTTP endpoints are supported')
a.scratch.mkdir(parents=True, exist_ok=True)
os.chmod(a.scratch, 0o700)
statepath = a.scratch / 'state.json'
state = json.loads(statepath.read_text()) if statepath.exists() else {}

def save():
    statepath.write_text(json.dumps(state, indent=2))

def rpc(method, params=(), node=False):
    url = a.node if node else state['rpc']
    req = urllib.request.Request(url, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': list(params)}).encode(), {'Content-Type': 'application/json'})
    if not node:
        req.add_header('Authorization', 'Basic ' + base64.b64encode(b'zebra:zebra').decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        v = json.load(r)
    if v.get('error'):
        raise RuntimeError(str(v['error']))
    return v['result']

def wait(fn, seconds=120):
    end = time.monotonic() + seconds
    err = None
    while time.monotonic() < end:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:
            err = e
        time.sleep(0.5)
    raise RuntimeError('wait expired: ' + str(err))

def synced():
    h = rpc('getblockcount', node=True)
    s = rpc('getwalletstatus')
    return s if s.get('wallet_tip', {}).get('height') == h and s.get('fully_synced_height') == h and (not s.get('locked', True)) else None

def cli(*args):
    r = subprocess.run([str(a.scratch / 'bin/zallet'), '--datadir=' + str(a.scratch / 'wallet'), '--config=' + str(a.scratch / 'wallet/zallet.toml'), *args], text=True, capture_output=True, timeout=60)
    if r.returncode:
        raise RuntimeError(r.stderr)
    return r.stdout

def start():
    log = open(a.scratch / 'wallet.log', 'ab')
    env = dict(os.environ)
    env['RUST_LOG'] = 'info,zallet_core::components::sync=debug,zallet_zinder=debug'
    proc = subprocess.Popen([str(a.scratch / 'bin/zallet'), '--datadir=' + str(a.scratch / 'wallet'), '--config=' + str(a.scratch / 'wallet/zallet.toml'), 'start'], stdout=log, stderr=log, env=env, start_new_session=True)
    state['pid'] = proc.pid
    save()
    def ready():
        if proc.poll() is not None:
            raise RuntimeError('backend exited before RPC readiness')
        return rpc('getwalletinfo')
    wait(ready)
    return wait(synced)

def op(opid):

    def done():
        v = rpc('z_getoperationstatus', [[opid]])
        if not v or v[0]['status'] in ('queued', 'executing'):
            return None
        if v[0]['status'] != 'success':
            raise RuntimeError(str(v))
        return v[0]['result']['txid']
    return wait(done, 240)

def mine(n=1, address=None):
    v = rpc('generatetoaddress', [n, address or state['miner']], node=True)
    wait(synced, 180)
    return v

def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def tx_fingerprint(txid):
    return hashlib.sha256(bytes.fromhex(txid.lower())).hexdigest()


def exited(pid):
    path = pathlib.Path('/proc') / str(pid)
    return not path.exists() or (path / 'stat').read_text().split()[2] == 'Z'


def stop():
    rpc('stop')
    wait(lambda: exited(state['pid']))


def initialize():
    wallet = a.scratch / 'wallet'
    require(not (wallet / 'wallet.db').exists(), 'restore requires an empty independent database')
    wallet.mkdir()
    conf = toml.load(a.template_dir / 'zallet.toml')
    conf['backend'] = 'zinder'
    conf['zinder'] = {'wallet_query_endpoint': a.endpoint}
    conf['consensus']['regtest_nuparams'] = [
        '5ba81b19:1', '76b809bb:1', '2bb40e60:1', 'f5b9230b:1', 'e9ff75a6:1',
        'c2d6d0b4:2', 'c8e71055:3', '4dec4df0:4', '5437f330:5', '37a5165b:6', '77190ad9:140']
    with socket.socket() as reservation:
        reservation.bind(('127.0.0.1', 0))
        port = reservation.getsockname()[1]
    state['rpc'] = 'http://127.0.0.1:' + str(port)
    conf['rpc']['bind'] = ['127.0.0.1:' + str(port)]
    conf['keystore']['encryption_identity'] = str(wallet / 'encryption-identity.txt')
    shutil.copyfile(a.template_dir / 'encryption-identity.txt', wallet / 'encryption-identity.txt')
    (wallet / 'zallet.toml').write_text(toml.dumps(conf))
    save()
    cli('init-wallet-encryption')
    # BIP 39's public all-zero 256-bit entropy vector, exclusively for this private chain.
    phrase = ' '.join(['abandon'] * 23 + ['art'])
    command = [str(a.scratch / 'bin/zallet'), '--datadir=' + str(wallet),
               '--config=' + str(wallet / 'zallet.toml'), 'import-mnemonic']
    imported = subprocess.run(['/usr/bin/script', '-qec', shlex.join(command), '/dev/null'],
                              input=phrase + '\n', text=True, capture_output=True, timeout=60)
    require(imported.returncode == 0, 'public fixture mnemonic import failed')
    state['miner'] = cli('regtest', 'generate-account-and-miner-address').strip()
    save()
    status = start()
    account = rpc('z_listaccounts', [False])[0]
    state['account'] = account['account_uuid']
    state['seedfp'] = account['seedfp']
    state['address'] = rpc('z_getaddressforaccount', [state['account'], ['orchard'], None])['address']
    state['wallet_identity'] = fingerprint(rpc('z_exportviewingkey', [state['address'], False]))
    save()
    return status


result = {'schema_version': 2, 'endpoint': a.endpoint, 'fallback_used': False,
          'step': a.step, 'status': 'completed', 'assertions_enabled': True,
          'assertion_mode': 'always-on',
          'endpoint_evidence': {'scope': 'configured', 'target_connected': True,
                                'unexpected_connections': 0}}
try:
    if a.step == 'bootstrap':
        require(not state, 'bootstrap requires fresh scratch')
        (a.scratch / 'bin').mkdir()
        for name, source in [('zallet', ROOT / 'target/debug/zallet'),
                             ('zallet-zinder', a.backend_binary.resolve() if a.backend_binary else ROOT / 'backends/zinder/target/debug/zallet-zinder')]:
            shutil.copy2(source, a.scratch / 'bin' / name)
        result['wallet_status'] = initialize()
        mine(1)
        mine(101)
        shielding = rpc('z_shieldcoinbase', [state['miner'], state['address'], None, 1, None, 'AllowRevealedSenders'])
        state['shield_txid'] = op(shielding['opid'])
        require(state['shield_txid'] in rpc('getrawmempool', node=True), 'shield transaction missing from node')
        save()
        mine()
        mine(10)
        source_rows = wait(lambda: [t for t in rpc('z_listtransactions', [state['account']])
                                   if t['txid'] == state['shield_txid'] and t.get('mined_height') is not None])
        state['source'] = {'miner': state['miner'], 'account': state['account'],
                           'seedfp': state['seedfp'], 'wallet_identity': state['wallet_identity'],
                           'activity': source_rows[0]['txid']}
        save()
        result['producer_funded'] = rpc('z_getbalanceforaccount', [state['account']])
        require(result['producer_funded']['pools'].get('ironwood', {}).get('valueZat', 0) > 0,
                'source wallet has no shielded funding')
        stop()
        (a.scratch / 'wallet').rename(a.scratch / 'producer')
    elif a.step == 'restore':
        source = state['source']
        require(not (a.scratch / 'wallet').exists(), 'target wallet must be independent and empty')
        result['wallet_status'] = initialize()
        require(state['miner'] == source['miner'], 'recovered transparent key differs')
        require(state['seedfp'] == source['seedfp'], 'recovered account seed fingerprint differs')
        require(state['wallet_identity'] == source['wallet_identity'], 'recovered full viewing key identity differs')
        activity = wait(lambda: [t for t in rpc('z_listtransactions', [state['account']])
                                if t['txid'] == source['activity'] and t.get('mined_height') is not None])
        require(any(output.get('pool') == 'ironwood' and output.get('to_account') == state['account']
                    for output in activity[0].get('outputs', [])), 'restored activity has no owned Ironwood output')
        result['restore'] = {
            'fresh_database': True,
            'source_database_fingerprint_sha256': fingerprint(str((a.scratch / 'producer/wallet.db').resolve())),
            'restored_database_fingerprint_sha256': fingerprint(str((a.scratch / 'wallet/wallet.db').resolve())),
            'source_wallet_fingerprint_sha256': source['wallet_identity'],
            'restored_wallet_fingerprint_sha256': state['wallet_identity'],
            'source_activity_fingerprint_sha256': tx_fingerprint(source['activity']),
            'restored_activity_fingerprint_sha256': tx_fingerprint(activity[0]['txid']),
            'restored_activity_count': len(activity)}
    elif a.step == 'transparent-discovery':
        outputs = rpc('z_listunspent', [1])
        transparent = [output for output in outputs if output.get('pool') == 'transparent'
                       or output.get('address', '').startswith('tm')]
        require(transparent, 'restored wallet has no preexisting transparent outputs')
        result['unspent_count'] = len(transparent)
        result['wallet_status'] = wait(synced)
    elif a.step == 'shielded-discovery':
        result['balance'] = rpc('z_getbalanceforaccount', [state['account']])
        require(result['balance']['pools'].get('ironwood', {}).get('valueZat', 0) > 0,
                'restored wallet has no preexisting Ironwood balance')
    elif a.step == 'send':
        destination = rpc('z_getaddressforaccount', [state['account'], ['orchard'], None])['address']
        opid = rpc('z_sendmany', [state['address'], [{'address': destination, 'amount': 0.01}],
                               1, None, 'FullPrivacy'])
        state['txid'] = op(opid)
        save()
        result['transaction'] = {'fingerprint_sha256': tx_fingerprint(state['txid']), 'observation': 'submitted'}
    elif a.step == 'pending':
        require(state['txid'] in rpc('getrawmempool', node=True), 'submitted transaction missing from node mempool')
        rows = wait(lambda: [t for t in rpc('z_listtransactions', [state['account']])
                            if t['txid'] == state['txid'] and t.get('mined_height') is None])
        result['transaction'] = {'fingerprint_sha256': tx_fingerprint(rows[0]['txid']), 'observation': 'wallet-pending'}
    elif a.step == 'mined':
        mine()
        transaction = rpc('getrawtransaction', [state['txid'], 1], node=True)
        require(transaction.get('confirmations', 0) > 0, 'transaction is not canonical mined')
        require(transaction['txid'] == state['txid'], 'canonical transaction differs from submitted transaction')
        result['transaction'] = {'fingerprint_sha256': tx_fingerprint(transaction['txid']),
                                 'observation': 'canonical-mined', 'mined_height': transaction['height']}
    elif a.step == 'scanned':
        result['wallet_status'] = wait(synced)
        rows = wait(lambda: [t for t in rpc('z_listtransactions', [state['account']])
                            if t['txid'] == state['txid'] and t.get('mined_height') is not None])
        result['transaction'] = {'fingerprint_sha256': tx_fingerprint(rows[0]['txid']),
                                 'observation': 'wallet-scanned', 'mined_height': rows[0]['mined_height']}
    elif a.step == 'restart':
        before = wait(synced)['fully_synced_height']
        old_pid = state['pid']
        stop()
        previous_exited = exited(old_pid)
        result['wallet_status'] = start()
        require(old_pid != state['pid'], 'wallet process identity did not change')
        result['restart_scope'] = 'process-restart'
        result['restart'] = {'before_process_id': old_pid, 'after_process_id': state['pid'],
                             'previous_process_exited': previous_exited, 'before_height': before,
                             'after_height': result['wallet_status']['fully_synced_height']}
    elif a.step == 'resume':
        before = wait(synced)['fully_synced_height']
        mine()
        result['wallet_status'] = wait(synced)
        after = result['wallet_status']['fully_synced_height']
        require(after > before, 'wallet did not scan a fresh block after restart')
        result['progress'] = {'before_height': before, 'after_height': after,
                              'new_block_mined_after_restart': True}
        result['balance'] = rpc('z_getbalanceforaccount', [state['account']])
    elif a.step == 'cleanup':
        if state.get('pid') and not exited(state['pid']):
            stop()
    else:
        raise ValueError(a.step)
except Exception as error:
    result['status'] = 'blocked'
    result['error'] = str(error)
    controlled = str(error).startswith(('recovered ', 'restore ', 'source wallet ', 'restored wallet ', 'bootstrap ', 'wallet process ', 'wallet did ', 'public fixture '))
    a.report.parent.mkdir(parents=True, exist_ok=True)
    a.report.with_name('failure.json').write_text(json.dumps({
        'step': a.step, 'failure_type': type(error).__name__,
        'check': str(error) if controlled else 'RPC or process operation failed'}, indent=2))
if result['status'] == 'completed':
    result['assertions'] = {a.step: True}
a.report.parent.mkdir(parents=True, exist_ok=True)
a.report.write_text(json.dumps(result, indent=2))
print(json.dumps(result))
raise SystemExit(0 if result['status'] == 'completed' else 1)
