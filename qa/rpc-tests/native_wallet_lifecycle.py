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
from urllib.parse import urlsplit
import toml

from test_framework.util import COINBASE_MATURITY, INTERNAL_FEE, MIN_CONFIRMATIONS, PrivacyPolicy

# A private payment small enough to leave the recovered funding spendable.
PAYMENT_ZEC = 0.01
# One coinbase per shielding operation keeps proving work bounded.
SHIELDING_INPUT_LIMIT = 1
# Confirm the shielded source before recovery and spending.
SHIELDING_CONFIRMATIONS = 10
# Verbose transaction reads return canonical identity and mined height.
VERBOSE_TRANSACTION = 1
def loopback_http(value):
    endpoint = urlsplit(value)
    if (endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1'
            or endpoint.port is None or endpoint.username is not None
            or endpoint.password is not None or endpoint.path not in ('', '/')
            or endpoint.query or endpoint.fragment):
        raise argparse.ArgumentTypeError('Only dedicated loopback HTTP endpoints are supported')
    return value


p = argparse.ArgumentParser()
p.add_argument('--zallet-root', type=pathlib.Path, required=True)
p.add_argument('--backend-binary', type=pathlib.Path)
p.add_argument('--template-dir', type=pathlib.Path, default=pathlib.Path(__file__).resolve().parents[1] / 'defaults/zallet')
p.add_argument('step')
p.add_argument('--scratch', type=pathlib.Path, required=True)
p.add_argument('--report', type=pathlib.Path, required=True)
p.add_argument('--endpoint', type=loopback_http, default='http://127.0.0.1:32302')
p.add_argument('--node', type=loopback_http, default='http://127.0.0.1:32332')
a = p.parse_args()
ROOT = a.zallet_root.resolve()
a.scratch.mkdir(parents=True, exist_ok=True)
os.chmod(a.scratch, 0o700)
statepath = a.scratch / 'state.json'
state = json.loads(statepath.read_text()) if statepath.exists() else {}

def save():
    statepath.write_text(json.dumps(state, indent=2))

class RpcError(RuntimeError):
    def __init__(self, error):
        super().__init__(error.get('message', 'RPC error'))
        self.code = error.get('code')


def rpc(method, params=(), node=False, endpoint=None):
    url = endpoint or (a.node if node else state['rpc'])
    req = urllib.request.Request(url, json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': list(params)}).encode(), {'Content-Type': 'application/json'})
    if not node:
        req.add_header('Authorization', 'Basic ' + base64.b64encode(b'zebra:zebra').decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        v = json.load(r)
    if v.get('error'):
        raise RpcError(v['error'])
    return v['result']

def wait(fn, seconds=120):
    end = time.monotonic() + seconds
    err = None
    while time.monotonic() < end:
        try:
            v = fn()
            if v:
                return v
        except (OSError, RpcError) as e:
            if isinstance(e, RpcError) and e.code != -10:
                raise
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

def op(opid, endpoint=None):

    def done():
        v = rpc('z_getoperationstatus', [[opid]], endpoint=endpoint)
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


def connection_snapshot():
    """Observe only sockets owned by this wallet, excluding its incoming RPC."""
    process = pathlib.Path('/proc') / str(state['pid'])
    owned = set()
    for descriptor in (process / 'fd').iterdir():
        try:
            link = os.readlink(descriptor)
        except FileNotFoundError:
            continue
        if link.startswith('socket:['):
            owned.add(link.removeprefix('socket:[').removesuffix(']'))
    expected = urlsplit(a.endpoint)
    rpc_port = urlsplit(state['rpc']).port
    observations = []
    for filename, family in [('tcp', socket.AF_INET), ('tcp6', socket.AF_INET6)]:
        for line in (process / 'net' / filename).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[9] not in owned or fields[3] != '01':
                continue
            local_port = int(fields[1].split(':')[1], 16)
            if local_port == rpc_port:
                continue
            address, port = fields[2].split(':')
            raw = bytes.fromhex(address)
            raw = raw[::-1] if family == socket.AF_INET else b''.join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
            peer = socket.inet_ntop(family, raw)
            if peer.startswith('::ffff:'):
                peer = peer.removeprefix('::ffff:')
            observations.append({'peer': peer, 'port': int(port, 16)})
    return {'target_connected': any(item == {'peer': expected.hostname, 'port': expected.port} for item in observations),
            'unexpected_connections': sum(item != {'peer': expected.hostname, 'port': expected.port} for item in observations),
            'observed_connections': observations}


result = {'schema_version': 2, 'endpoint': a.endpoint, 'fallback_used': False,
          'step': a.step, 'status': 'completed', 'assertions_enabled': True,
          'assertion_mode': 'always-on',
          'endpoint_attribution': 'configured'}
try:
    if a.step == 'bootstrap':
        require(not state, 'bootstrap requires fresh scratch')
        (a.scratch / 'bin').mkdir()
        for name, source in [('zallet', ROOT / 'target/debug/zallet'),
                             ('zallet-zinder', a.backend_binary.resolve() if a.backend_binary else ROOT / 'backends/zinder/target/debug/zallet-zinder')]:
            shutil.copy2(source, a.scratch / 'bin' / name)
        result['wallet_status'] = initialize()
        mine(1)
        mine(COINBASE_MATURITY + MIN_CONFIRMATIONS)
        shielding = rpc('z_shieldcoinbase', [state['miner'], state['address'], INTERNAL_FEE, SHIELDING_INPUT_LIMIT, None, PrivacyPolicy.ALLOW_REVEALED_SENDERS])
        state['shield_txid'] = op(shielding['opid'])
        require(state['shield_txid'] in rpc('getrawmempool', node=True), 'shield transaction missing from node')
        save()
        mine()
        mine(SHIELDING_CONFIRMATIONS)
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
        outputs = rpc('z_listunspent', [MIN_CONFIRMATIONS])
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
        producer_dir = a.scratch / 'producer'
        producer_config = toml.load(producer_dir / 'zallet.toml')
        endpoint = 'http://' + producer_config['rpc']['bind'][0]
        log = open(a.scratch / 'producer.log', 'ab')
        producer = subprocess.Popen([
            str(a.scratch / 'bin/zallet'), '--datadir=' + str(producer_dir),
            '--config=' + str(producer_dir / 'zallet.toml'), 'start'],
            stdout=log, stderr=log, start_new_session=True)
        try:
            wait(lambda: rpc('getwalletinfo', endpoint=endpoint))
            wait(lambda: rpc('getwalletstatus', endpoint=endpoint).get('fully_synced_height')
                 == rpc('getblockcount', node=True))
            address = rpc('z_getaddressforaccount', [state['source']['account'], ['orchard'], None], endpoint=endpoint)['address']
            opid = rpc('z_sendmany', [address, [{'address': destination, 'amount': PAYMENT_ZEC}],
                                   MIN_CONFIRMATIONS, INTERNAL_FEE, PrivacyPolicy.FULL_PRIVACY], endpoint=endpoint)
            state['txid'] = op(opid, endpoint=endpoint)
            save()
        finally:
            if producer.poll() is None:
                try:
                    rpc('stop', endpoint=endpoint)
                    producer.wait(timeout=30)
                except (OSError, RpcError, subprocess.TimeoutExpired):
                    producer.terminate()
                    producer.wait(timeout=30)
            log.close()
        result['transaction'] = {'fingerprint_sha256': tx_fingerprint(state['txid']), 'observation': 'submitted'}
    elif a.step == 'pending':
        require(state['txid'] in rpc('getrawmempool', node=True), 'submitted transaction missing from node mempool')
        rows = wait(lambda: [t for t in rpc('z_listtransactions', [state['account']])
                            if t['txid'] == state['txid'] and t.get('mined_height') is None])
        old_pid = state['pid']
        stop()
        log = a.scratch / 'wallet.log'
        offset = log.stat().st_size
        start()
        wait(lambda: ('Scanning mempool tx ' + state['txid']) in log.read_bytes()[offset:].decode(errors='replace'))
        rows = wait(lambda: [t for t in rpc('z_listtransactions', [state['account']])
                            if t['txid'] == state['txid'] and t.get('mined_height') is None])
        require(state['pid'] != old_pid, 'pending wallet process did not restart')
        result['incoming_mempool_after_restart'] = True
        result['transaction'] = {'fingerprint_sha256': tx_fingerprint(rows[0]['txid']), 'observation': 'wallet-pending'}
    elif a.step == 'mined':
        mine()
        transaction = rpc('getrawtransaction', [state['txid'], VERBOSE_TRANSACTION], node=True)
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
        state['expected_balance'] = rpc('z_getbalanceforaccount', [state['account']])
        state['expected_mined_height'] = next(t['mined_height'] for t in rpc('z_listtransactions', [state['account']]) if t['txid'] == state['txid'])
        save()
        stop()
        previous_exited = exited(old_pid)
        result['wallet_status'] = start()
        require(old_pid != state['pid'], 'wallet process identity did not change')
        require(rpc('z_getbalanceforaccount', [state['account']]) == state['expected_balance'], 'reopened wallet balance changed')
        require(any(t['txid'] == state['txid'] and t.get('mined_height') == state['expected_mined_height'] for t in rpc('z_listtransactions', [state['account']])), 'reopened wallet lost mined transaction')
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
        expected = {k: v for k, v in state['expected_balance']['pools'].items() if k != 'transparent'}
        actual = {k: v for k, v in result['balance']['pools'].items() if k != 'transparent'}
        require(actual == expected, 'resumed shielded balance changed')
        require(any(t['txid'] == state['txid'] and t.get('mined_height') == state['expected_mined_height'] for t in rpc('z_listtransactions', [state['account']])), 'resumed wallet lost mined transaction')
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
if result['status'] == 'completed' and a.step not in {'bootstrap', 'cleanup'}:
    snapshot = wait(lambda: value if (value := connection_snapshot())['target_connected'] else None)
    require(snapshot['unexpected_connections'] == 0, 'unexpected wallet socket connection')
    a.report.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(snapshot, sort_keys=True).encode()
    a.report.with_name('connection-snapshot.json').write_bytes(encoded)
    result['endpoint_evidence'] = {'scope': 'configured', 'target_connected': snapshot['target_connected'],
                                   'unexpected_connections': snapshot['unexpected_connections'],
                                   'trace_fingerprint_sha256': hashlib.sha256(encoded).hexdigest()}
if result['status'] == 'completed':
    result['assertions'] = {a.step: True}
a.report.parent.mkdir(parents=True, exist_ok=True)
a.report.write_text(json.dumps(result, indent=2))
print(json.dumps(result))
raise SystemExit(0 if result['status'] == 'completed' else 1)
