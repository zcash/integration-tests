#!/usr/bin/env python3
# Copyright (c) 2014-2016 The Bitcoin Core developers
# Copyright (c) 2016-2024 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .


#
# Helpful routines for regression testing
#

from __future__ import annotations

import os
import sys

from binascii import hexlify, unhexlify
from base64 import b64encode
from decimal import Decimal, ROUND_DOWN
import json
import http.client
import random
import shutil
import subprocess
import tarfile
import tempfile
import time
import toml
import re
import errno

from enum import Enum
from typing import Callable

from . import coverage
from .proxy import ServiceProxy, JSONRPCException
from .authproxy import AuthServiceProxy
from .authproxy import JSONRPCException as AuthJSONRPCException

# Node and wallet proxies raise different JSONRPCException classes (proxy.py
# for nodes, authproxy.py for wallets); helpers that catch RPC errors must
# accept either.
_RPC_EXCEPTIONS = (JSONRPCException, AuthJSONRPCException)

from test_framework.config import (
    ZainoConfig,
    ZebraConfig,
    ZebraArgs,
    ZalletArgs,
    render_regtest_nuparams,
)

COVERAGE_DIR = None
PRE_BLOSSOM_BLOCK_TARGET_SPACING = 150
POST_BLOSSOM_BLOCK_TARGET_SPACING = 75

SPROUT_BRANCH_ID = 0x00000000
OVERWINTER_BRANCH_ID = 0x5BA81B19
SAPLING_BRANCH_ID = 0x76B809BB
BLOSSOM_BRANCH_ID = 0x2BB40E60
HEARTWOOD_BRANCH_ID = 0xF5B9230B
CANOPY_BRANCH_ID = 0xE9FF75A6
NU5_BRANCH_ID = 0xC2D6D0B4
NU6_BRANCH_ID = 0xC8E71055
NU6_1_BRANCH_ID = 0x4DEC4DF0
NU6_2_BRANCH_ID = 0x5437f330
NU6_3_BRANCH_ID = 0x37A5165B

# The maximum number of nodes a single test can spawn
MAX_NODES = 8
# Don't assign rpc or p2p ports lower than this
PORT_MIN = 11000
# The number of ports to "reserve" for p2p, rpc and wallet rpc each
PORT_RANGE = 5000

def zebrad_binary():
    return os.getenv("ZEBRAD", os.path.join("src", "zebrad"))

def zaino_binary():
    return os.getenv("ZAINOD", os.path.join("src", "zainod"))

def zallet_binary():
    return os.getenv("ZALLET", os.path.join("src", "zallet"))

def zebrad_config(datadir):
    base_location = os.path.join('qa', 'defaults', 'zebrad', 'config.toml')
    new_location = os.path.join(datadir, "config.toml")
    if not os.path.exists(new_location):
        shutil.copyfile(base_location, new_location)
    return new_location

def zallet_config(datadir):
    base_location = os.path.join('qa', 'defaults', 'zallet')
    if not os.path.exists(datadir):
        shutil.copytree(base_location, datadir)
    return os.path.join(datadir, "zallet.toml")

def zainod_config(datadir):
    base_location = os.path.join('qa', 'defaults', 'zainod', 'zaino_config.toml')
    new_location = os.path.join(datadir, "zaino_config.toml")
    os.makedirs(datadir, exist_ok=True)
    if not os.path.exists(new_location):
        shutil.copyfile(base_location, new_location)
    return new_location

class PortSeed:
    # Must be initialized with a unique integer for each process
    n = None

def enable_coverage(dirname):
    """Maintain a log of which RPC calls are made during testing."""
    global COVERAGE_DIR
    COVERAGE_DIR = dirname


def get_rpc_proxy(url, node_number, timeout=None):
    """
    Args:
        url (str): URL of the RPC server to call
        node_number (int): the node number (or id) that this calls to

    Kwargs:
        timeout (int): HTTP timeout in seconds

    Returns:
        ServiceProxy. convenience object for making RPC calls.

    """
    proxy_kwargs = {}
    if timeout is not None:
        proxy_kwargs['timeout'] = timeout

    proxy = ServiceProxy(url, **proxy_kwargs)
    proxy.url = url  # store URL on proxy for info

    coverage_logfile = coverage.get_filename(
        COVERAGE_DIR, node_number) if COVERAGE_DIR else None

    return coverage.AuthServiceProxyWrapper(proxy, coverage_logfile)


def p2p_port(n):
    assert(n <= MAX_NODES)
    return PORT_MIN + n + (MAX_NODES * PortSeed.n) % (PORT_RANGE - 1 - MAX_NODES)

def rpc_port(n):
    return PORT_MIN + PORT_RANGE + n + (MAX_NODES * PortSeed.n) % (PORT_RANGE - 1 - MAX_NODES)

def wallet_rpc_port(n):
    return PORT_MIN + (PORT_RANGE * 2) + n + (MAX_NODES * PortSeed.n) % (PORT_RANGE - 1 - MAX_NODES)

def indexer_rpc_port(n):
    return PORT_MIN + (PORT_RANGE * 3) + n + (MAX_NODES * PortSeed.n) % (PORT_RANGE - 1 - MAX_NODES)

def zaino_rpc_port(n):
    return PORT_MIN + (PORT_RANGE * 4) + n + (MAX_NODES * PortSeed.n) % (PORT_RANGE - 1 - MAX_NODES)

def zaino_grpc_port(n):
    return PORT_MIN + (PORT_RANGE * 5) + n + (MAX_NODES * PortSeed.n) % (PORT_RANGE - 1 - MAX_NODES)

def check_json_precision():
    """Make sure json library being used does not lose precision converting ZEC values"""
    n = Decimal("20000000.00000003")
    zatoshis = int(json.loads(json.dumps(float(n)))*1.0e8)
    if zatoshis != 2000000000000003:
        raise RuntimeError("JSON encode/decode loses precision")

def bytes_to_hex_str(byte_str):
    return hexlify(byte_str).decode('ascii')

def hex_str_to_bytes(hex_str):
    return unhexlify(hex_str.encode('ascii'))

def str_to_b64str(string):
    return b64encode(string.encode('utf-8')).decode('ascii')

def sync_blocks(nodes, wallets=None, wait=0.125, timeout=60, allow_different_tips=False):
    """
    Wait until everybody has the same tip, and has notified
    all internal listeners of them.

    If allow_different_tips is True, waits until everyone has
    the same block count.
    """
    while timeout > 0:
        if allow_different_tips:
            tips = [ x.getblockcount() for x in nodes ]
        else:
            tips = [ x.getbestblockhash() for x in nodes ]
        if tips == [ tips[0] ]*len(tips):
            if not wallets:
                return True
            break
        time.sleep(wait)
        timeout -= wait

    wallet_status = None
    if wallets:
        # `getwalletstatus` omits `wallet_tip` until the wallet has committed;
        # poll instead of raising KeyError.
        while timeout > 0:
            wallet_status = [ x.getwalletstatus() for x in wallets ]
            if all('wallet_tip' in w for w in wallet_status):
                key = 'height' if allow_different_tips else 'blockhash'
                wallet_node_tips = [ w['node_tip'][key] for w in wallet_status ]
                wallet_tips = [ w['wallet_tip'][key] for w in wallet_status ]
                if tips == wallet_node_tips and tips == wallet_tips:
                    return True
            time.sleep(wait)
            timeout -= wait

    print('Node tips:', tips)
    if wallet_status is not None:
        print('Wallet statuses:', wallet_status)
    raise AssertionError("Block sync failed")

def sync_blocks_with_reconnect(rpcs, peer_idx, max_attempts=3, reconnect_pause=2):
    """`sync_blocks` wrapped in retries that re-issue `addnode` between attempts.

    Works around the 8-node `rebuild_cache` mesh sometimes failing to converge
    within `sync_blocks`'s 60s timeout (zebra #10329, #10332).
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            sync_blocks(rpcs)
            return
        except AssertionError as e:
            last_err = e
            if attempt < max_attempts:
                print(
                    "sync_blocks attempt {}/{} timed out; reconnecting peers "
                    "to node {} and retrying".format(attempt, max_attempts, peer_idx)
                )
                for i in range(len(rpcs)):
                    if i != peer_idx:
                        try:
                            connect_nodes_bi(rpcs, i, peer_idx)
                        except Exception:
                            # Best-effort; next sync_blocks will surface real failures.
                            pass
                time.sleep(reconnect_pause)
    raise last_err

def sync_mempools(nodes, wallets=None, wait=0.5, timeout=60):
    """
    Wait until everybody has the same transactions in their memory
    pools, and has notified all internal listeners of them

    Returns `True` when all wallets are in synced, or if no wallet is given.
    """
    while timeout > 0:
        pool = set(nodes[0].getrawmempool())
        num_match = 1
        for i in range(1, len(nodes)):
            if set(nodes[i].getrawmempool()) == pool:
                num_match = num_match+1
        if num_match == len(nodes):
            if not wallets:
                return True
            break
        time.sleep(wait)
        timeout -= wait

    if wallets:
        # Now that the mempools are in sync, wait for the internal
        # notifications to finish. `getwalletstatus` omits `wallet_tip` until
        # the wallet has a committed tip, so treat its absence as "not synced
        # yet" and keep polling instead of raising KeyError.
        while timeout > 0:
            wallet_status = [ x.getwalletstatus() for x in wallets ]
            if all('wallet_tip' in w for w in wallet_status):
                tips = [ (w['node_tip']['blockhash'], w['wallet_tip']['blockhash']) for w in wallet_status ]
                if tips == [ tips[0] ]*len(tips) and tips[0][0] == tips[0][1]:
                    return True
            time.sleep(wait)
            timeout -= wait

    print('Wallet view of tips:', wallet_status)
    raise AssertionError("Mempool sync failed")

bitcoind_processes = {}

def initialize_datadir(dirname, n, clock_offset=0):
    datadir = node_dir(dirname, n)
    if not os.path.isdir(datadir):
        os.makedirs(datadir)
    config_rpc_port = rpc_port(n)
    config_p2p_port = p2p_port(n)
    config_indexer_port = indexer_rpc_port(n)

    update_zebrad_conf(datadir, config_rpc_port, config_p2p_port, config_indexer_port, None)

    return datadir

def update_zebrad_conf(datadir, rpc_port, p2p_port, indexer_port, extra_args=None):
    config_path = zebrad_config(datadir)

    with open(config_path, "r", encoding="utf8") as f:
        config_file = toml.load(f)

    zebra_config = ZebraConfig(
        network_listen_address='127.0.0.1:'+str(p2p_port),
        rpc_listen_address='127.0.0.1:'+str(rpc_port),
        indexer_listen_address='127.0.0.1:'+str(indexer_port),
        data_dir=datadir,
        extra_args=extra_args)

    config_file = zebra_config.update(config_file)

    with open(config_path, "w", encoding="utf8") as f:
        toml.dump(config_file, f)

    return config_path

def rpc_url(i, rpchost=None):
    host = '127.0.0.1'
    port = rpc_port(i)
    if rpchost:
        parts = rpchost.split(':')
        if len(parts) == 2:
            host, port = parts
        else:
            host = rpchost
    # For zebra, we just use a non-authenticated endpoint.
    return "http://%s:%d" % (host, int(port))

def rpc_zaino_url(i, rpchost=None):
    host = '127.0.0.1'
    port = zaino_rpc_port(i)
    if rpchost:
        parts = rpchost.split(':')
        if len(parts) == 2:
            host, port = parts
        else:
            host = rpchost
    #
    return "http://%s:%d" % (host, int(port))

# Maximum seconds to wait for a spawned process (zebrad/zallet/zainod) to become
# ready (its RPC responding) before giving up. Without a bound, a process that
# starts but never finishes init -- e.g. a stuck sync, or a port still held by an
# orphaned process from a prior aborted run -- would spin in the loops below
# forever, hanging the run until CI's multi-hour hard limit. Raising instead
# fails fast and surfaces the cause. Override with the PROC_START_TIMEOUT env var.
PROC_START_TIMEOUT = int(os.getenv("PROC_START_TIMEOUT", "120"))

def wait_for_zebrad_start(process, url, i):
    '''
    Wait for bitcoind to start. This means that RPC is accessible and fully initialized.
    Raise an exception if bitcoind exits during initialization, or fails to become
    ready within PROC_START_TIMEOUT seconds.
    '''
    deadline = time.time() + PROC_START_TIMEOUT
    while True:
        if process.poll() is not None:
            raise Exception('%s node %d exited with status %i during initialization' % (zebrad_binary(), i, process.returncode))
        if time.time() > deadline:
            raise Exception('%s node %d failed to become ready within %d seconds' % (zebrad_binary(), i, PROC_START_TIMEOUT))
        try:
            rpc = get_rpc_proxy(url, i)
            rpc.getblockcount()
            break # break out of loop on success
        except IOError as e:
            if e.errno != errno.ECONNREFUSED: # Port not yet open?
                raise # unknown IO error
        except JSONRPCException as e: # Initialization phase
            if not (
                # RPC in warmup?
                e.error['code'] == -343 or
                # zebrad race condition
                (e.error['code'] == -1 and e.error['message'] == 'No blocks in state')
            ):
                raise # unknown JSON RPC exception
        time.sleep(0.25)

def initialize_chain(test_dir, num_nodes, cachedir, cache_behavior='current'):
    """
    Create a set of node datadirs in `test_dir`, based upon the specified
    `cache_behavior` value. The following values are recognized for
    `cache_behavior`:

    * 'current': create a 200-block-long chain (with wallet) for MAX_NODES
      in `cachedir` if necessary. Afterward, create num_nodes copies in
      `test_dir` from the cache. The resulting nodes will be configured to
      use the -clockoffset config argument when starting to ensure that
      the cached chain is not treated as being excessively out-of-date.
    * 'sprout': use persisted chain data containing known amounts of Sprout
      funds from the files in `qa/rpc-tests/cache/sprout`. This allows
      testing of Sprout spends even though Sprout outputs can no longer
      be created by zcashd software. The resulting nodes will be configured to
      use the -clockoffset config argument when starting to ensure that
      the cached chain is not treated as being excessively out-of-date.
    * 'fresh': force re-creation of the cache, and then start as for `current`.
    * 'clean': start the nodes without cached chain data, allowing the test
      to take full control of chain setup.
    """
    assert num_nodes <= MAX_NODES

    def rebuild_cache():
        #find and delete old cache directories if any exist
        for i in range(MAX_NODES):
            node_i_dir = node_dir(cachedir, i)
            if os.path.isdir(node_i_dir):
                shutil.rmtree(node_i_dir)
            wallet_i_dir = wallet_dir(cachedir, i)
            if os.path.isdir(wallet_i_dir):
                shutil.rmtree(wallet_i_dir)

        # Create zallets and generate miner addresses:
        zallet = zallet_binary()
        miner_addresses = {}
        for i in range(MAX_NODES):
            datadir = wallet_dir(cachedir, i)
            update_zallet_conf(datadir, rpc_port(i), wallet_rpc_port(i),
                               indexer_port=indexer_rpc_port(i),
                               zebra_state_dir=node_dir(cachedir, i))

            args = [ zallet, "-d="+datadir, "init-wallet-encryption" ]
            process = subprocess.Popen(args)
            process.wait()

            args = [ zallet, "-d="+datadir, "generate-mnemonic" ]
            process = subprocess.Popen(args)
            process.wait()

            args = [ zallet, "-d="+datadir, "regtest", "generate-account-and-miner-address" ]
            process = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
            (miner_address, _) = process.communicate()

            miner_addresses[i] = miner_address

        # Create cache directories, run bitcoinds:
        block_time = int(time.time()) - (200 * PRE_BLOSSOM_BLOCK_TARGET_SPACING)
        for i in range(MAX_NODES):
            datadir = initialize_datadir(cachedir, i)

            config = update_zebrad_conf(datadir, rpc_port(i), p2p_port(i), indexer_rpc_port(i), ZebraArgs(
                miner_address=miner_addresses[i],
            ))
            args = [ zebrad_binary(), "-c="+config, "start" ]

            bitcoind_processes[i] = subprocess.Popen(args)
            if os.getenv("PYTHON_DEBUG", ""):
                print("initialize_chain: %s started, waiting for RPC to come up" % (zebrad_binary(),))
            wait_for_zebrad_start(bitcoind_processes[i], rpc_url(i), i)
            if os.getenv("PYTHON_DEBUG", ""):
                print("initialize_chain: RPC successfully started")

        rpcs = []
        for i in range(MAX_NODES):
            try:
                rpcs.append(get_rpc_proxy(rpc_url(i), i))
            except:
                sys.stderr.write("Error connecting to "+rpc_url(i)+"\n")
                sys.exit(1)

        # Create a 200-block-long chain; each of the 4 first nodes
        # gets 25 mature blocks and 25 immature.
        # Note: To preserve compatibility with older versions of
        # initialize_chain, only 4 nodes will generate coins.
        #
        # Blocks are created with timestamps 2.5 minutes apart (matching the
        # chain defaulting above to Sapling active), starting 200 * 2.5 minutes
        # before the current time.
        for i in range(2):
            for peer in range(4):
                # Connect the other nodes to the mining peer.
                for i in range(0, MAX_NODES):
                    if i != peer:
                        connect_nodes_bi(rpcs, i, peer)
                # Mine the blocks
                for j in range(25):
                    rpcs[peer].generate(1)
                    block_time += PRE_BLOSSOM_BLOCK_TARGET_SPACING
                # Must sync before next peer mines; retry variant handles the
                # 8-node mesh occasionally missing sync_blocks's 60s window.
                sync_blocks_with_reconnect(rpcs, peer)
                # Shut down and restart every zebrad node.
                # This works around a zebrad problem where it won't broadcast
                # received blocks to other connected nodes, and is a workaround
                # for zebrad not supporting `addnode remove`.
                # TODO: Remove this workaround once either of the following is resolved:
                # - https://github.com/ZcashFoundation/zebra/issues/10329
                # - https://github.com/ZcashFoundation/zebra/issues/10332
                stop_nodes(rpcs)
                wait_bitcoinds()
                for i in range(MAX_NODES):
                    config = zebrad_config(node_dir(cachedir, i))
                    args = [ zebrad_binary(), "-c="+config, "start" ]
                    bitcoind_processes[i] = subprocess.Popen(args)
                    if os.getenv("PYTHON_DEBUG", ""):
                        print("initialize_chain: %s started, waiting for RPC to come up" % (zebrad_binary(),))
                    wait_for_zebrad_start(bitcoind_processes[i], rpc_url(i), i)
                    if os.getenv("PYTHON_DEBUG", ""):
                        print("initialize_chain: RPC successfully started")
                for i in range(MAX_NODES):
                    try:
                        rpcs.append(get_rpc_proxy(rpc_url(i), i))
                    except:
                        sys.stderr.write("Error connecting to "+rpc_url(i)+"\n")
                        sys.exit(1)
        # Check that local time isn't going backwards
        assert_greater_than(time.time() + 1, block_time)

        # Run zallets:
        for i in range(MAX_NODES):
            datadir = wallet_dir(cachedir, i)
            args = [ zallet, "-d="+datadir, "start" ]

            zallet_processes[i] = subprocess.Popen(args)
            if os.getenv("PYTHON_DEBUG", ""):
                print("initialize_chain: wallet started, waiting for RPC to come up")
            wait_for_wallet_start(zallet_processes[i], rpc_url_wallet(i), i)
            if os.getenv("PYTHON_DEBUG", ""):
                print("initialize_chain: RPC successfully started for wallet {} with pid {}".format(i, zallet_processes[i].pid))

        wallets = []
        for i in range(MAX_NODES):
            try:
                wallets.append(get_rpc_auth_proxy(rpc_url_wallet(i), i))
            except:
                sys.stderr.write("Error connecting to "+rpc_url_wallet(i)+"\n")
                sys.exit(1)

        # Wait for zallets to catch up to their nodes. Bounded so a stuck
        # 8-node mesh (zebra #10329, #10332) fails fast instead of hitting the
        # CI 6h cap. stderr survives `create_cache.py`'s stdout capture.
        sync_deadline = time.time() + 300
        loop_start = time.time()
        last_log = loop_start
        while True:
            wallet_status = [ x.getwalletstatus() for x in wallets ]
            if all('wallet_tip' in w for w in wallet_status):
                tips = [ (w['node_tip']['blockhash'], w['wallet_tip']['blockhash']) for w in wallet_status ]
                if tips == [ tips[0] ]*len(tips) and tips[0][0] == tips[0][1]:
                    print(
                        "[rebuild_cache] wallets converged after {:.1f}s".format(
                            time.time() - loop_start
                        ),
                        file=sys.stderr,
                    )
                    break
            now = time.time()
            if now - last_log >= 30:
                heights = [w.get('node_tip', {}).get('height') for w in wallet_status]
                print(
                    "[rebuild_cache] waiting for wallets to converge "
                    "({:.0f}s elapsed, heights={})".format(now - loop_start, heights),
                    file=sys.stderr,
                )
                last_log = now
            if now > sync_deadline:
                heights = [w.get('node_tip', {}).get('height') for w in wallet_status]
                print(
                    "[rebuild_cache] wallet sync did not converge in {:.0f}s; "
                    "final heights={}".format(now - loop_start, heights),
                    file=sys.stderr,
                )
                print('Wallet statuses:', wallet_status, file=sys.stderr)
                raise AssertionError("Wallet sync did not converge within deadline")
            time.sleep(0.25)

        # Shut them down, and clean up cache directories:
        stop_wallets(wallets)
        wait_zallets()
        stop_nodes(rpcs)
        wait_bitcoinds()
        for i in range(MAX_NODES):
            # record the system time at which the cache was regenerated
            with open(node_file(cachedir, i, 'cache_config.json'), "w", encoding="utf8") as cache_conf_file:
                cache_config = { "cache_time": time.time() }
                cache_conf_json = json.dumps(cache_config, indent=4)
                cache_conf_file.write(cache_conf_json)

    def init_from_cache():
        for i in range(num_nodes):
            from_dir = node_dir(cachedir, i)
            to_dir = node_dir(test_dir, i)
            shutil.copytree(from_dir, to_dir)

            from_wallet_dir = wallet_dir(cachedir, i)
            to_wallet_dir = wallet_dir(test_dir, i)
            shutil.copytree(from_wallet_dir, to_wallet_dir)

            with open(node_file(test_dir, i, 'cache_config.json'), "r", encoding="utf8") as cache_conf_file:
                cache_conf = json.load(cache_conf_file)
                # obtain the clock offset as a negative number of seconds
                offset = round(cache_conf['cache_time']) - round(time.time())
                # overwrite port/rpcport and clock offset in zcash.conf
                initialize_datadir(test_dir, i, clock_offset=offset)

    def init_persistent(cache_behavior):
        assert num_nodes <= 4 # only 4 nodes with Sprout funds are supported
        cache_path = persistent_cache_path(cache_behavior)
        if not os.path.isdir(cache_path):
            raise Exception('No cache available for cache behavior %s' % cache_behavior)

        chain_cache_filename = os.path.join(cache_path, "chain_cache.tar.gz")
        if not os.path.exists(chain_cache_filename):
            raise Exception('Chain cache missing for cache behavior %s' % cache_behavior)

        for i in range(num_nodes):
            to_dir = os.path.join(test_dir, "node"+str(i), "regtest")
            os.makedirs(to_dir)

            # Copy the same chain data to all nodes
            with tarfile.open(chain_cache_filename, "r:gz") as chain_cache_file:
                tarfile_extractall(chain_cache_file, to_dir)

            # Copy in per-node wallet data
            wallet_tgz_filename = os.path.join(cache_path, "node"+str(i)+"_wallet.tar.gz")
            if not os.path.exists(wallet_tgz_filename):
                raise Exception('Wallet cache missing for cache behavior %s, node %d' % (cache_behavior, i))
            with tarfile.open(wallet_tgz_filename, "r:gz") as wallet_tgz_file:
                tarfile_extractall(wallet_tgz_file, os.path.join(to_dir, "wallet.dat"))

            # Copy in per-node wallet config and update zcash.conf to set the
            # clock offsets correctly.
            cache_conf_filename = os.path.join(to_dir, 'cache_config.json')
            if not os.path.exists(cache_conf_filename):
                raise Exception('Cache config missing for cache behavior %s, node %d' % (cache_behavior, i))
            with open(cache_conf_filename, "r", encoding="utf8") as cache_conf_file:
                cache_conf = json.load(cache_conf_file)
                # obtain the clock offset as a negative number of seconds
                offset = round(cache_conf['cache_time']) - round(time.time())
                # overwrite port/rpcport and clock offset in zcash.conf
                initialize_datadir(test_dir, i, clock_offset=offset)

    def cache_rebuild_required():
        for i in range(MAX_NODES):
            node_path = node_dir(cachedir, i)
            if os.path.isdir(node_path):
                if not os.path.isfile(node_file(cachedir, i, 'cache_config.json')):
                    return True
            else:
                return True
        return False

    if cache_behavior == 'current':
        if cache_rebuild_required(): rebuild_cache()
        init_from_cache()
    elif cache_behavior == 'fresh':
        rebuild_cache()
        init_from_cache()
    elif cache_behavior == 'clean':
        initialize_chain_clean(test_dir, num_nodes)
    else:
        init_persistent(cache_behavior)

def initialize_chain_clean(test_dir, num_nodes):
    """
    Create an empty blockchain and num_nodes wallets.
    Useful if a test case wants complete control over initialization.
    """
    for i in range(num_nodes):
        initialize_datadir(test_dir, i)

def persistent_cache_path(cache_behavior):
    return os.path.join(
        os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
        'cache',
        cache_behavior
    )

def persistent_cache_exists(cache_behavior):
    cache_path = persistent_cache_path(cache_behavior)
    return os.path.isdir(cache_path)

# Clean up, zip, and persist the generated datadirs. Record the generation
# time so that we can correctly set the system clock offset in tests that
# restore their node states using the resulting files.
def persist_node_caches(tmpdir, cache_behavior, num_nodes):
    cache_path = persistent_cache_path(cache_behavior)
    if os.path.exists(cache_path):
        raise Exception('Cache already exists for cache behavior %s' % cache_behavior)
    os.mkdir(cache_path)

    for i in range(num_nodes):
        node_path = os.path.join(tmpdir, 'node' + str(i), 'regtest')

        # Clean up the files that we don't want to persist
        os.remove(os.path.join(node_path, 'debug.log'))
        os.remove(os.path.join(node_path, 'db.log'))
        os.remove(os.path.join(node_path, 'peers.dat'))

        # Persist the wallet file for the node to the cache
        wallet_tgz_filename = os.path.join(cache_path, 'node' + str(i) + '_wallet.tar.gz')
        with tarfile.open(wallet_tgz_filename, "w:gz") as wallet_tgz_file:
            wallet_tgz_file.add(os.path.join(node_path, 'wallet.dat'), arcname="")

        # Persist the chain data and cache config just once; it will be reused
        # for all of the nodes when loading from the cache.
        if i == 0:
            # Move the wallet.dat file out of the way so that it doesn't
            # pollute the chain cache tarfile
            shutil.move(
                    os.path.join(node_path, 'wallet.dat'),
                    os.path.join(tmpdir, 'wallet.dat.0'))

            # Store the current time so that we can correctly set the clock
            # offset when restoring from the cache.
            cache_config = { "cache_time": time.time() }
            cache_conf_filename = os.path.join(cache_path, 'cache_config.json')
            with open(cache_conf_filename, "w", encoding="utf8") as cache_conf_file:
                cache_conf_json = json.dumps(cache_config, indent=4)
                cache_conf_file.write(cache_conf_json)

            # Persist the chain data.
            chain_cache_filename = os.path.join(cache_path, 'chain_cache.tar.gz')
            with tarfile.open(chain_cache_filename, "w:gz") as chain_cache_file:
                chain_cache_file.add(node_path, arcname="")

            # Move the wallet file back into place
            shutil.move(
                    os.path.join(tmpdir, 'wallet.dat.0'),
                    os.path.join(node_path, 'wallet.dat'))


def _rpchost_to_args(rpchost):
    '''Convert optional IP:port spec to rpcconnect/rpcport args'''
    if rpchost is None:
        return []

    match = re.match(r'(\[[0-9a-fA-f:]+\]|[^:]+)(?::([0-9]+))?$', rpchost)
    if not match:
        raise ValueError('Invalid RPC host spec ' + rpchost)

    rpcconnect = match.group(1)
    rpcport = match.group(2)

    if rpcconnect.startswith('['): # remove IPv6 [...] wrapping
        rpcconnect = rpcconnect[1:-1]

    rv = ['-rpcconnect=' + rpcconnect]
    if rpcport:
        rv += ['-rpcport=' + rpcport]
    return rv

def start_node(i, dirname, extra_args=None, rpchost=None, timewait=None, binary=None, stderr=None):
    """
    Start a bitcoind and return RPC connection to it
    """
    datadir = node_dir(dirname, i)
    if binary is None:
        binary = zebrad_binary()
    config = update_zebrad_conf(datadir, rpc_port(i), p2p_port(i), indexer_rpc_port(i), extra_args)
    args = [ binary, "-c="+config, "start" ]

    bitcoind_processes[i] = subprocess.Popen(args, stderr=stderr)
    if os.getenv("PYTHON_DEBUG", ""):
        print("start_node: bitcoind started, waiting for RPC to come up")
    url = rpc_url(i, rpchost)
    wait_for_zebrad_start(bitcoind_processes[i], url, i)
    if os.getenv("PYTHON_DEBUG", ""):
        print("start_node: RPC successfully started for node {} with pid {}".format(i, bitcoind_processes[i].pid))
    proxy = get_rpc_proxy(url, i, timeout=timewait)

    if COVERAGE_DIR:
        coverage.write_all_rpc_commands(COVERAGE_DIR, proxy)

    return proxy

def assert_start_raises_init_error(i, dirname, extra_args=None, expected_msg=None):
    with tempfile.SpooledTemporaryFile(max_size=2**16) as log_stderr:
        try:
            node = start_node(i, dirname, extra_args, stderr=log_stderr)
            stop_node(node, i)
        except Exception as e:
            assert ("%s node %d exited" % (zebrad_binary(), i)) in str(e) # node must have shutdown
            if expected_msg is not None:
                log_stderr.seek(0)
                stderr = log_stderr.read().decode('utf-8')
                if expected_msg not in stderr:
                    raise AssertionError("Expected error \"" + expected_msg + "\" not found in:\n" + stderr)
        else:
            if expected_msg is None:
                assert_msg = "%s should have exited with an error" % (zebrad_binary(),)
            else:
                assert_msg = "%s should have exited with expected error %r" % (zebrad_binary(), expected_msg)
            raise AssertionError(assert_msg)

def start_nodes(num_nodes, dirname, extra_args=None, rpchost=None, binary=None):
    """
    Start multiple bitcoinds, return RPC connections to them
    """
    if extra_args is None: extra_args = [ None for _ in range(num_nodes) ]
    if binary is None: binary = [ None for _ in range(num_nodes) ]
    rpcs = []
    try:
        for i in range(num_nodes):
            rpcs.append(start_node(i, dirname, extra_args[i], rpchost, binary=binary[i]))
    except: # If one node failed to start, stop the others
        stop_nodes(rpcs)
        raise
    return rpcs

def node_dir(dirname, n_node):
    return os.path.join(dirname, "node"+str(n_node))

def node_file(dirname, n_node, filename):
    return os.path.join(node_dir(dirname, n_node), filename)

def wallet_dir(dirname, n_wallet):
    return os.path.join(dirname, "wallet"+str(n_wallet))

def check_node(i):
    bitcoind_processes[i].poll()
    return bitcoind_processes[i].returncode

def stop_node(node, i):
    try:
        node.stop()
    except http.client.CannotSendRequest as e:
        print("WARN: Unable to stop node: " + repr(e))
    bitcoind_processes[i].wait()
    del bitcoind_processes[i]

def stop_nodes(nodes):
    for node in nodes:
        try:
            node.stop()
        except http.client.CannotSendRequest as e:
            print("WARN: Unable to stop node: " + repr(e))
    del nodes[:] # Emptying array closes connections as a side effect

def set_node_times(nodes, t):
    for node in nodes:
        node.setmocktime(t)

# Maximum seconds to wait for a child process to exit after it has been asked
# to stop, before we force-kill it. This bounds the cleanup waits below so they
# can never deadlock the test: if a process was started but never sent a stop
# -- e.g. the zebrad/zallet left running when cache setup aborts midway, as
# happens when a zallet fails to sync with "Missing Orchard tree state" -- a
# bare .wait() would otherwise hang until the CI job's multi-hour hard limit.
# This is shutdown grace time, not test runtime: it only starts after stop_*().
PROC_WAIT_TIMEOUT = 60

def wait_or_kill(proc):
    '''Wait for proc to exit, force-killing it if it overruns PROC_WAIT_TIMEOUT.'''
    try:
        proc.wait(timeout=PROC_WAIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()

def wait_bitcoinds():
    # Wait for all bitcoinds to cleanly exit
    for bitcoind in list(bitcoind_processes.values()):
        wait_or_kill(bitcoind)
    bitcoind_processes.clear()

def connect_nodes(from_connection, node_num):
    ip_port = "127.0.0.1:"+str(p2p_port(node_num))
    # TODO: Replace `add` with `onetry` if zebrad implements it.
    from_connection.addnode(ip_port, "add")
    # poll until version handshake complete to avoid race conditions
    # with transaction relaying
    while True:
        for peer in from_connection.getpeerinfo():
            if peer['addr'] == ip_port:
                return
            else:
                time.sleep(1)

def connect_nodes_bi(nodes, a, b):
    connect_nodes(nodes[a], b)

def find_output(node, txid, amount):
    """
    Return index to output of txid with value amount
    Raises exception if there is none.
    """
    txdata = node.getrawtransaction(txid, 1)
    for i in range(len(txdata["vout"])):
        if txdata["vout"][i]["value"] == amount:
            return i
    raise RuntimeError("find_output txid %s : %s not found"%(txid,str(amount)))


def gather_inputs(from_node, amount_needed, confirmations_required=1):
    """
    Return a random set of unspent txouts that are enough to pay amount_needed
    """
    assert(confirmations_required >=0)
    utxo = from_node.listunspent(confirmations_required)
    random.shuffle(utxo)
    inputs = []
    total_in = Decimal("0.00000000")
    while total_in < amount_needed and len(utxo) > 0:
        t = utxo.pop()
        total_in += t["amount"]
        inputs.append({ "txid" : t["txid"], "vout" : t["vout"], "address" : t["address"] } )
    if total_in < amount_needed:
        raise RuntimeError("Insufficient funds: need %d, have %d"%(amount_needed, total_in))
    return (total_in, inputs)

def make_change(from_node, amount_in, amount_out, fee):
    """
    Create change output(s), return them
    """
    outputs = {}
    amount = amount_out+fee
    change = amount_in - amount
    if change > amount*2:
        # Create an extra change output to break up big inputs
        change_address = from_node.getnewaddress()
        # Split change in two, being careful of rounding:
        outputs[change_address] = Decimal(change/2).quantize(Decimal('0.00000001'), rounding=ROUND_DOWN)
        change = amount_in - amount - outputs[change_address]
    if change > 0:
        outputs[from_node.getnewaddress()] = change
    return outputs

def random_transaction(nodes, amount, min_fee, fee_increment, fee_variants):
    """
    Create a random transaction.
    Returns (txid, hex-encoded-transaction-data, fee)
    """
    from_node = random.choice(nodes)
    to_node = random.choice(nodes)
    fee = min_fee + fee_increment*random.randint(0,fee_variants)

    (total_in, inputs) = gather_inputs(from_node, amount+fee)
    outputs = make_change(from_node, total_in, amount, fee)
    outputs[to_node.getnewaddress()] = float(amount)

    rawtx = from_node.createrawtransaction(inputs, outputs)
    signresult = from_node.signrawtransaction(rawtx)
    txid = from_node.sendrawtransaction(signresult["hex"], True)

    return (txid, signresult["hex"], fee)

def assert_equal(expected, actual, message=""):
    if expected != actual:
        if message:
            message = "; %s" % message
        raise AssertionError("(left == right)%s\n  left: <%s>\n right: <%s>" % (message, str(expected), str(actual)))

def assert_true(condition, message = ""):
    if not condition:
        raise AssertionError(message)

def assert_false(condition, message = ""):
    assert_true(not condition, message)

def assert_greater_than(thing1, thing2):
    if thing1 <= thing2:
        raise AssertionError("%s <= %s"%(str(thing1),str(thing2)))

def assert_raises(exc, fun, *args, **kwds):
    assert_raises_message(exc, None, fun, *args, **kwds)

def assert_raises_message(ExceptionType, errstr, func, *args, **kwargs):
    """
    Asserts that func throws and that the exception contains 'errstr'
    in its message.
    """
    try:
        func(*args, **kwargs)
    except ExceptionType as e:
        if errstr is not None and errstr not in str(e):
            raise AssertionError("Invalid exception string: Couldn't find %r in %r" % (
                errstr, str(e)))
    except Exception as e:
        raise AssertionError("Unexpected exception raised: " + type(e).__name__)
    else:
        raise AssertionError("No exception raised")

def fail(message=""):
    raise AssertionError(message)


class OperationStatus(str, Enum):
    """The lifecycle status of an async wallet operation, as reported in the
    `status` field of z_getoperationstatus / z_getoperationresult (and accepted
    by the `wait_and_assert_operationid_status*` helpers below). Being
    `str`-valued, a member compares equal to the wire string and can be passed
    straight to those helpers or compared against RPC output."""
    QUEUED = 'queued'
    EXECUTING = 'executing'
    SUCCESS = 'success'
    FAILED = 'failed'
    CANCELLED = 'cancelled'


# Returns an async operation result
def wait_and_assert_operationid_status_result(node, myopid, in_status=OperationStatus.SUCCESS, in_errormsg=None, timeout=300):
    print('waiting for async operation {}'.format(myopid))
    result = None
    for _ in range(1, timeout):
        results = node.z_getoperationresult([myopid])
        if len(results) > 0:
            result = results[0]
            break
        time.sleep(1)

    assert_true(result is not None, "timeout occurred")
    status = result['status']

    debug = os.getenv("PYTHON_DEBUG", "")
    if debug:
        print('...returned status: {}'.format(status))

    errormsg = None
    if status == OperationStatus.FAILED:
        errormsg = result['error']['message']
        if debug:
            print('...returned error: {}'.format(errormsg))
        assert_equal(in_errormsg, errormsg)

    assert_equal(in_status, status, "Operation returned mismatched status. Error Message: {}".format(errormsg))

    return result


# Returns txid if operation was a success or None
def wait_and_assert_operationid_status(node, myopid, in_status=OperationStatus.SUCCESS, in_errormsg=None, timeout=300):
    result = wait_and_assert_operationid_status_result(node, myopid, in_status, in_errormsg, timeout)
    if result['status'] == OperationStatus.SUCCESS:
        return result['result']['txid']
    else:
        return None

# Find a coinbase address on the node, filtering by the number of UTXOs it has.
# If no filter is provided, returns the coinbase address on the node containing
# the greatest number of spendable UTXOs.
# The default cached chain has one address per coinbase output.
def get_coinbase_address(node, expected_utxos=None):
    addrs = [utxo['address'] for utxo in node.listunspent() if utxo['generated']]
    assert(len(set(addrs)) > 0)

    if expected_utxos is None:
        addrs = [(addrs.count(a), a) for a in set(addrs)]
        return sorted(addrs, reverse=True)[0][1]

    addrs = [a for a in set(addrs) if addrs.count(a) == expected_utxos]
    assert(len(addrs) > 0)
    return addrs[0]

def check_node_log(self, node_number, line_to_check, stop_node = True):
    print("Checking node " + str(node_number) + " logs")
    if stop_node:
        self.nodes[node_number].stop()
        bitcoind_processes[node_number].wait()
    logpath = self.options.tmpdir + "/node" + str(node_number) + "/regtest/debug.log"
    with open(logpath, "r", encoding="utf8") as myfile:
        logdata = myfile.readlines()
    for (n, logline) in enumerate(logdata):
        if line_to_check in logline:
            return n
    raise AssertionError(repr(line_to_check) + " not found")

def nustr(branch_id):
    return '%08x' % branch_id

def nuparams(branch_id, height):
    return '-nuparams=%s:%d' % (nustr(branch_id), height)

def tarfile_extractall(tarfile, path):
    if sys.version_info >= (3, 11, 4):
        tarfile.extractall(path=path, filter='data')
    else:
        tarfile.extractall(path=path)


# Wallet utilities

zallet_processes = {}

ZALLET_RPC_DEFAULT_USERNAME = "zebra"
ZALLET_RPC_DEFAULT_PASSWORD = "zebra"

def get_rpc_auth_proxy(url, node_number, timeout=None):
    """
    Args:
        url (str): URL of the RPC server to call
        node_number (int): the node number (or id) that this calls to

    Kwargs:
        timeout (int): HTTP timeout in seconds

    Returns:
        AuthServiceProxy. convenience object for making RPC calls.

    """
    proxy_kwargs = {}
    if timeout is not None:
        proxy_kwargs['timeout'] = timeout

    proxy = AuthServiceProxy(url, **proxy_kwargs)
    proxy.url = url  # store URL on proxy for info

    coverage_logfile = coverage.get_filename(
        COVERAGE_DIR, node_number) if COVERAGE_DIR else None

    return coverage.AuthServiceProxyWrapper(proxy, coverage_logfile)

def prepare_wallets_for_mining(num_wallets, dirname, binary=None, zallet_args=None):
    """
    Creates the datadir for multiple wallets, sets up their first account, and
    returns a transparent address for each of them to use for mining.

    `zallet_args` (a per-wallet list of `ZalletArgs`, or None) configures the
    wallet BEFORE its database is created here. This matters because creating the
    wallet runs the librustzcash migrations, some of which bake network-upgrade
    activation heights into SQL views (e.g. the Ironwood shard-scan-ranges view
    embeds the NU6.3 activation height). The database must therefore be created
    with the SAME activation heights that the wallet is later started with; if a
    NU is only configured afterwards (in `start_wallets`), the migration bakes a
    NULL activation height and the pool's notes never become spendable.
    """
    if binary is None: binary = [ zallet_binary() for _ in range(num_wallets) ]
    if zallet_args is None: zallet_args = [ None for _ in range(num_wallets) ]
    miner_addresses = []
    for i in range(num_wallets):
        datadir = wallet_dir(dirname, i)
        if os.path.exists(os.path.join(datadir, "wallet.db")):
            raise Exception('Wallet %d already exists, cannot prepare it for mining' % i)

        zallet = binary[i]

        update_zallet_conf(datadir, rpc_port(i), wallet_rpc_port(i), zallet_args[i],
                           indexer_port=indexer_rpc_port(i),
                           zebra_state_dir=node_dir(dirname, i))

        args = [ zallet, "-d="+datadir, "init-wallet-encryption" ]
        process = subprocess.Popen(args)
        process.wait()

        args = [ zallet, "-d="+datadir, "generate-mnemonic" ]
        process = subprocess.Popen(args)
        process.wait()

        args = [ zallet, "-d="+datadir, "regtest", "generate-account-and-miner-address" ]
        process = subprocess.Popen(args, stdout=subprocess.PIPE, text=True)
        (miner_address, _) = process.communicate()

        miner_addresses.append(miner_address)
    return miner_addresses

def start_wallets(num_wallets, dirname, extra_args=None, rpchost=None, binary=None, zallet_args=None):
    """
    Start multiple wallets, return RPC connections to them
    """
    if extra_args is None: extra_args = [ None for _ in range(num_wallets) ]
    if binary is None: binary = [ None for _ in range(num_wallets) ]
    if zallet_args is None: zallet_args = [ None for _ in range(num_wallets) ]
    rpcs = []
    try:
        for i in range(num_wallets):
            rpcs.append(start_wallet(i, dirname, extra_args[i], rpchost, binary=binary[i], zallet_args=zallet_args[i]))
    except: # If one wallet failed to start, stop the others
        stop_wallets(rpcs)
        raise
    return rpcs

def start_wallet(i, dirname, extra_args=None, rpchost=None, timewait=None, binary=None, stderr=None, zallet_args=None):
    """
    Start a Zallet wallet and return RPC connection to it
    """

    datadir = wallet_dir(dirname, i)
    wallet_db = os.path.join(datadir, "wallet.db")
    prepare = not os.path.exists(wallet_db)

    if binary is None:
        binary = zallet_binary()

    validator_port = rpc_port(i)
    zallet_port = wallet_rpc_port(i)

    update_zallet_conf(datadir, validator_port, zallet_port, zallet_args,
                       indexer_port=indexer_rpc_port(i),
                       zebra_state_dir=node_dir(dirname, i))

    # We prepare the wallet if it is new
    if prepare:
        args = [ binary, "-d="+datadir, "init-wallet-encryption" ]
        process = subprocess.Popen(args, stderr=stderr)
        process.wait()

        args = [ binary, "-d="+datadir, "generate-mnemonic" ]
        process = subprocess.Popen(args, stderr=stderr)
        process.wait()

    # Start the wallet
    args = [ binary, "-d="+datadir, "start" ]

    if extra_args is not None: args.extend(extra_args)
    zallet_processes[i] = subprocess.Popen(args, stderr=stderr)
    if os.getenv("PYTHON_DEBUG", ""):
        print("start_wallet: wallet started, waiting for RPC to come up")
    url = rpc_url_wallet(i, rpchost)
    wait_for_wallet_start(zallet_processes[i], url, i)
    if os.getenv("PYTHON_DEBUG", ""):
        print("start_wallet: RPC successfully started for wallet {} with pid {}".format(i, zallet_processes[i].pid))
    proxy = get_rpc_auth_proxy(url, i, timeout=timewait)
    if COVERAGE_DIR:
        coverage.write_all_rpc_commands(COVERAGE_DIR, proxy)

    return proxy

# The zallet backend the launcher execs (top-level `backend` key in
# zallet.toml). CI sets this to run the same RPC suite against both the `zaino`
# and `zebra` backends; it defaults to `zaino` to match the checked-in default
# config and local runs.
def zallet_backend():
    return os.getenv("ZALLET_BACKEND", "zaino")

def update_zallet_conf(datadir, validator_port, zallet_port, extra_args=None,
                       indexer_port=None, zebra_state_dir=None):
    config_path = zallet_config(datadir)

    with open(config_path, "r", encoding="utf8") as f:
        config_file = toml.load(f)

    config_file['rpc']['bind'][0] = '127.0.0.1:'+str(zallet_port)
    config_file['indexer']['validator_address'] = '127.0.0.1:'+str(validator_port)

    # Select the backend the launcher execs. The `zebra` backend does not talk
    # to zainod; it opens the co-located zebrad's state database read-only and
    # follows the tip over zebrad's gRPC indexer interface, so it needs an
    # [indexer.read_state_service] section pointing at that zebrad. Both the
    # indexer gRPC port and zebrad's state directory are required for it.
    backend = zallet_backend()
    config_file['backend'] = backend
    if backend == "zebra":
        assert indexer_port is not None and zebra_state_dir is not None, \
            "the zebra backend requires indexer_port and zebra_state_dir"
        config_file['indexer']['read_state_service'] = {
            'grpc_address': '127.0.0.1:'+str(indexer_port),
            'zebra_state_path': os.path.abspath(zebra_state_dir),
        }
    else:
        # Never leave a stale zebra section behind when reusing a config file.
        config_file['indexer'].pop('read_state_service', None)

    extra_args = extra_args or ZalletArgs()

    # Params that update_zallet_conf knows how to apply. Any param set on
    # `extra_args` that is not authorized here is rejected rather than silently
    # ignored.
    AUTHORIZED_PARAMS = {"activation_heights"}
    defaults = vars(ZalletArgs())
    provided = {k for k, v in vars(extra_args).items() if v != defaults.get(k)}
    assert provided <= AUTHORIZED_PARAMS, \
        "Unsupported zallet params %s; authorized: %s" % (
            sorted(provided - AUTHORIZED_PARAMS), sorted(AUTHORIZED_PARAMS))

    if extra_args.activation_heights:
        config_file.setdefault('consensus', {})
        config_file['consensus']['network'] = 'regtest'
        config_file['consensus']['regtest_nuparams'] = \
            render_regtest_nuparams(extra_args.activation_heights)

    with open(config_path, "w", encoding="utf8") as f:
        toml.dump(config_file, f)

    return config_path

def stop_wallets(wallets):
    for wallet in wallets:
        try:
            wallet.stop()
        except http.client.CannotSendRequest as e:
            print("WARN: Unable to stop wallet: " + repr(e))
        except BrokenPipeError as e:
            print("WARN: Wallet already stopped: " + repr(e))
        except ConnectionRefusedError as e:
            print("WARN: Wallet already stopped: " + repr(e))
    del wallets[:] # Emptying array closes connections as a side effect

def wait_zallets():
    # Wait for all zallets to cleanly exit
    for zallet in list(zallet_processes.values()):
        wait_or_kill(zallet)
    zallet_processes.clear()

def wait_for_wallet_start(process, url, i):
    '''
    Wait for the wallet to start. This means that RPC is accessible and fully initialized.
    Raise an exception if zallet exits during initialization, or fails to become
    ready within PROC_START_TIMEOUT seconds.
    '''
    deadline = time.time() + PROC_START_TIMEOUT
    while True:
        if process.poll() is not None:
            raise Exception('%s wallet %d exited with status %i during initialization' % (zallet_binary(), i, process.returncode))
        if time.time() > deadline:
            raise Exception('%s wallet %d failed to become ready within %d seconds' % (zallet_binary(), i, PROC_START_TIMEOUT))
        try:
            rpc = get_rpc_auth_proxy(url, i)
            rpc.getwalletinfo()
            break # break out of loop on success
        except IOError as e:
            if e.errno != errno.ECONNREFUSED: # Port not yet open?
                raise # unknown IO error
        except JSONRPCException as e: # Initialization phase
            if e.error['code'] != -28: # RPC in warmup?
                raise # unknown JSON RPC exception
        time.sleep(0.25)

def rpc_url_wallet(i, rpchost=None):
    host = '127.0.0.1'
    port = wallet_rpc_port(i)
    if rpchost:
        parts = rpchost.split(':')
        if len(parts) == 2:
            host, port = parts
        else:
            host = rpchost
    return "http://%s:%s@%s:%d" % (ZALLET_RPC_DEFAULT_USERNAME, ZALLET_RPC_DEFAULT_PASSWORD, host, int(port))


# Zaino utilities

zainod_processes = {}

def start_zainos(num_nodes, dirname, extra_args=None, rpchost=None, binary=None):
    """
    Start multiple zainod's, return RPC connections to them
    """
    if extra_args is None: extra_args = [ None for _ in range(num_nodes) ]
    if binary is None: binary = [ None for _ in range(num_nodes) ]
    rpcs = []
    try:
        for i in range(num_nodes):
            rpcs.append(start_zaino(i, dirname, extra_args[i], rpchost, binary=binary[i]))
    except: # If one node failed to start, stop the others
        stop_zainos(rpcs)
        raise
    return rpcs

def start_zaino(i, dirname, extra_args=None, rpchost=None, timewait=None, binary=None, stderr=None):
    """
    Start a zainod and return RPC connection to it
    """
    datadir = os.path.join(dirname, "zaino"+str(i))
    if binary is None:
        binary = zaino_binary()
    config = update_zainod_conf(datadir, rpc_port(i), indexer_rpc_port(i), zaino_rpc_port(i), zaino_grpc_port(i), extra_args)
    args = [ binary, "start", "-c="+config ]

    zainod_processes[i] = subprocess.Popen(args, stderr=stderr)
    if os.getenv("PYTHON_DEBUG", ""):
        print("start_node: zainod started, waiting for RPC to come up")
    url = rpc_zaino_url(i, rpchost)
    wait_for_zainod_start(zainod_processes[i], url, i)
    if os.getenv("PYTHON_DEBUG", ""):
        print("start_node: RPC successfully started for node {} with pid {}".format(i, zainod_processes[i].pid))
    proxy = get_rpc_proxy(url, i, timeout=timewait)

    if COVERAGE_DIR:
        coverage.write_all_rpc_commands(COVERAGE_DIR, proxy)

    return proxy

def wait_for_zainod_start(process, url, i):
    '''
    Wait for zainod to start. This means that RPC is accessible and fully initialized.
    Raise an exception if zainod exits during initialization, or fails to become
    ready within PROC_START_TIMEOUT seconds.
    '''
    deadline = time.time() + PROC_START_TIMEOUT
    while True:
        if process.poll() is not None:
            raise Exception('%s node %d exited with status %i during initialization' % (zaino_binary(), i, process.returncode))
        if time.time() > deadline:
            raise Exception('%s node %d failed to become ready within %d seconds' % (zaino_binary(), i, PROC_START_TIMEOUT))
        try:
            rpc = get_rpc_proxy(url, i)
            rpc.getblockcount()
            break # break out of loop on success
        except IOError as e:
            if e.errno != errno.ECONNREFUSED: # Port not yet open?
                raise # unknown IO error
        except JSONRPCException as e: # Initialization phase
            if e.error['code'] != -343: # RPC in warmup?
                raise # unknown JSON RPC exception
        time.sleep(0.25)

def update_zainod_conf(datadir, rpc_port, indexer_port, zaino_rpc_port, zaino_grpc_port, extra_args=None):
    config_path = zainod_config(datadir)

    with open(config_path, "r", encoding="utf8") as f:
        config_file = toml.load(f)

    zaino_config = ZainoConfig(
        json_rpc_listen_address='127.0.0.1:'+str(zaino_rpc_port),
        grpc_listen_address='127.0.0.1:'+str(zaino_grpc_port),
        validator_grpc_listen_address='127.0.0.1:'+str(indexer_port),
        validator_jsonrpc_listen_address='127.0.0.1:'+str(rpc_port)
    )

    config_file = zaino_config.update(config_file)

    with open(config_path, "w", encoding="utf8") as f:
        toml.dump(config_file, f)

    return config_path

def stop_zainos(zainos):
    # TODO: Add a `stop` RPC method to zainod
    del zainos[:] # Emptying array closes connections as a side effect

def wait_zainods():
    # Wait for all zainods to cleanly exit
    for zainod in list(zainod_processes.values()):
        # TODO: Add a `stop` RPC method to zainod
        try:
            zainod.terminate()
        except Exception:
            pass
        wait_or_kill(zainod)
    zainod_processes.clear()

def stop_all_processes():
    '''
    Forcibly terminate every zebrad, zainod and zallet process we spawned,
    regardless of whether a test data structure still references it.
    '''
    for processes in (bitcoind_processes, zallet_processes, zainod_processes):
        for p in list(processes.values()):
            try:
                p.terminate() # send SIGHIGH
            except Exception:
                pass
        for p in list(processes.values()):
            wait_or_kill(p)
        processes.clear()


# ---------------------------------------------------------------------------
# Z3-stack (zebrad + zaino + zallet) wallet-test helpers.
#
# Shared by the zallet wallet tests (wallet_z_shieldcoinbase.py,
# wallet_z_shieldcoinbase_multi_taddr.py, wallet_z_sendfromaccount.py, ...).
# These wrap the timing quirks of driving a real wallet against a live chain:
# the wallet's scan tip can lag the node tip, so spendability and scanned
# views must be polled rather than assumed.
# ---------------------------------------------------------------------------

# Coinbase outputs require 100 confirmations before zallet counts them spendable.
COINBASE_MATURITY = 100

# Seconds a mature-coinbase count must hold steady before a spend.
# z_listunspent (tip-change-driven) surfaces new coinbase before zallet's
# recover_history scan task (30s idle tick, not woken on tip change) makes it
# spendable to the proposal builder, so the window must outlast that tick.
# Drop once recover_history wakes on tip change / a sync RPC lands
# (zcash/wallet#316).
COINBASE_SETTLE_SECS = 35

# Wallets and nodes handed to tests are RPC proxies (see `get_rpc_proxy`): a
# coverage wrapper around an AuthServiceProxy that dispatches arbitrary RPC
# method names via `__getattr__`.
RpcProxy = coverage.AuthServiceProxyWrapper


class Pool(str, Enum):
    """A value pool a note or UTXO can belong to, as reported in the `pool`
    field of z_listunspent / z_getbalances. Being `str`-valued, a member
    compares equal to (and serializes as) its wire string, so it can be used
    directly in RPC arguments, dict lookups, and equality checks against RPC
    output."""
    TRANSPARENT = 'transparent'
    SAPLING = 'sapling'
    ORCHARD = 'orchard'
    IRONWOOD = 'ironwood'


class TotalBalanceField(str, Enum):
    """A summary field of `z_gettotalbalance`: the `transparent` pool, the
    aggregate shielded balance (`private`), or their `total`. This is NOT a
    `Pool`: `private`/`total` are roll-ups across pools, not value pools, so
    they have no `Pool` member. Being `str`-valued, a member is usable directly
    as the dict key into `z_gettotalbalance`'s output."""
    TRANSPARENT = 'transparent'
    PRIVATE = 'private'
    TOTAL = 'total'


class FundSource(str, Enum):
    """The `fund_source` selector accepted by z_sendfromaccount /
    z_proposetransaction, naming where an account's funds may be drawn from.
    The two shielded values coincide with `Pool` names; `ANY_TRANSPARENT` is a
    keyword with no `Pool` equivalent. (An array of transparent address strings
    is also accepted, but that is not an enumerable constant.) Being
    `str`-valued, a member can be passed straight to the RPC."""
    ORCHARD = 'orchard'
    SAPLING = 'sapling'
    ANY_TRANSPARENT = 'any_transparent'


class PrivacyPolicy(str, Enum):
    """A zallet privacy policy, ordered from strictest (FullPrivacy) to most
    permissive (NoPrivacy). Each names the information leakage a send is
    allowed to incur. Being `str`-valued, a member can be passed straight to an
    RPC and compared against the `privacy_policy` an RPC returns."""
    FULL_PRIVACY = 'FullPrivacy'
    ALLOW_REVEALED_AMOUNTS = 'AllowRevealedAmounts'
    ALLOW_REVEALED_RECIPIENTS = 'AllowRevealedRecipients'
    ALLOW_REVEALED_SENDERS = 'AllowRevealedSenders'
    ALLOW_FULLY_TRANSPARENT = 'AllowFullyTransparent'
    ALLOW_LINKING_ACCOUNT_ADDRESSES = 'AllowLinkingAccountAddresses'
    NO_PRIVACY = 'NoPrivacy'


class UpgradeStatus(str, Enum):
    """The activation status of a network upgrade, as reported in the `status`
    field of each entry of `getblockchaininfo()['upgrades']`. Being `str`-valued,
    a member compares equal to the wire string, so it can be asserted directly
    against RPC output."""
    DISABLED = 'disabled'
    PENDING = 'pending'
    ACTIVE = 'active'


def transparent_utxos(wallet: RpcProxy, minconf: int = 1) -> list[dict]:
    """The wallet's transparent UTXOs with at least `minconf` confirmations."""
    return [u for u in wallet.z_listunspent(minconf)
            if u.get('pool') == Pool.TRANSPARENT]


def mature_transparent_utxos(wallet: RpcProxy) -> list[dict]:
    """The wallet's mature transparent coinbase UTXOs (>= COINBASE_MATURITY
    confirmations)."""
    return transparent_utxos(wallet, COINBASE_MATURITY)


def mature_coinbase_on_address(wallet: RpcProxy, taddr: str) -> list[dict]:
    """The wallet's mature transparent coinbase UTXOs held on `taddr`."""
    return [u for u in mature_transparent_utxos(wallet)
            if u.get('address') == taddr]


def first_transparent_receiver(wallet: RpcProxy, ua: str) -> str:
    """Return the transparent (P2PKH, else P2SH) receiver of a unified address
    created with a transparent component."""
    receivers = wallet.z_listunifiedreceivers(ua)
    if 'p2pkh' in receivers:
        return receivers['p2pkh']
    if 'p2sh' in receivers:
        return receivers['p2sh']
    raise AssertionError(
        "UA has no transparent receiver: {!r} -> {!r}".format(ua, receivers))


def nu_activation_all_at_1() -> dict:
    """Activation heights putting every network upgrade at height 1, the
    standard Z3-stack wallet-test regtest config (NU5+ active from height 1).

    Returns a fresh dict on each call so callers can pass it to `ZebraArgs`
    without sharing mutable state."""
    return {"NU5": 1, "NU6": 1, "NU6.1": 1, "NU6.2": 1}


def nu_activation_all_at_1_with_ironwood() -> dict:
    """Like `nu_activation_all_at_1`, but also activates NU6.3 (Ironwood) at
    height 1.

    NU6.3 is deliberately kept out of the standard helper/defaults so that
    existing wallet tests keep receiving shielded funds into the Orchard pool.
    Once NU6.3 is active, funds received to an Orchard receiver land in the
    Ironwood pool instead, so only tests that expect Ironwood should use this.

    Both zebrad and zallet must activate NU6.3 at the same height, otherwise
    zebra's coinbase commits to the NU6.2 branch id while zallet expects the
    NU6.3 branch id and rejects the first block. Pass this dict to BOTH
    `ZebraArgs.activation_heights` and `ZalletArgs.activation_heights`."""
    return {"NU5": 1, "NU6": 1, "NU6.1": 1, "NU6.2": 1, "NU6.3": 1}


def assert_shieldcoinbase_preflight_shape(result: dict) -> None:
    """Assert the `z_shieldcoinbase` pre-flight response has the zcashd-shaped
    fields with the right types:
    `{ remainingUTXOs, remainingValue, shieldingUTXOs, shieldingValue, opid }`."""
    assert_true(isinstance(result, dict),
                "Expected dict, got {}: {!r}".format(type(result), result))
    for key in ('remainingUTXOs', 'remainingValue',
                'shieldingUTXOs', 'shieldingValue', 'opid'):
        assert_true(key in result,
                    "Missing field {!r} in response: {!r}".format(key, result))
    assert_true(isinstance(result['remainingUTXOs'], int))
    assert_true(isinstance(result['shieldingUTXOs'], int))
    assert_true(isinstance(result['opid'], str))
    # remainingValue / shieldingValue are JSON numbers; Decimal-able.
    Decimal(result['remainingValue'])
    Decimal(result['shieldingValue'])


def wait_for_stable_transparent(wallet: RpcProxy, min_count: int = 1,
                                minconf: int = 1, timeout: int = 300,
                                settle_secs: int = COINBASE_SETTLE_SECS) -> list[dict]:
    """
    Return the wallet's transparent UTXOs (>= `minconf` confirmations) once the
    count is at least `min_count` and has held steady for `settle_secs`
    consecutive seconds. This is the signal the proposal builder's transparent
    input selection tracks (`z_listunspent`), which lags a fresh UTXO becoming
    visible to it (zcash/wallet#316).
    """
    deadline = time.time() + timeout
    last_count = -1
    stable_secs = 0
    utxos: list[dict] = []
    while time.time() < deadline:
        try:
            utxos = transparent_utxos(wallet, minconf)
            count = len(utxos)
            if count >= min_count and count == last_count:
                stable_secs += 1
            else:
                stable_secs = 0
            last_count = count
            if count >= min_count and stable_secs >= settle_secs:
                return utxos
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError(
        "wait_for_stable_transparent: timeout after {}s; last saw {} transparent "
        "UTXOs (wanted >= {} stable for {}s)".format(
            timeout, last_count, min_count, settle_secs))


def wait_for_mature_coinbase_count(wallet: RpcProxy, expected_count: int,
                                   timeout: int = 300,
                                   settle_secs: int = COINBASE_SETTLE_SECS) -> list[dict]:
    """
    Return the wallet's mature transparent coinbase UTXOs once the count has
    held at `expected_count` for `settle_secs` consecutive seconds.

    The steady-count requirement is the sync barrier: z_listunspent reaches a
    new tip before the proposal builder's spendable view does, so we wait for
    the views to converge (no getwalletstatus RPC yet, zcash/wallet#316).
    """
    deadline = time.time() + timeout
    last_count = -1
    stable_secs = 0
    transparent = []
    while time.time() < deadline:
        try:
            transparent = mature_transparent_utxos(wallet)
            count = len(transparent)
            if count == expected_count and last_count == expected_count:
                stable_secs += 1
            else:
                stable_secs = 0
            last_count = count
            if count == expected_count and stable_secs >= settle_secs:
                return transparent
        except Exception:
            pass
        time.sleep(1)

    raise AssertionError(
        "wait_for_mature_coinbase_count: timeout after {}s; last saw {} mature "
        "transparent UTXOs (wanted exactly {} stable for {}s)".format(
            timeout, last_count, expected_count, settle_secs))


def wait_for_stable_mature_coinbase(wallet: RpcProxy, min_count: int = 1,
                                    timeout: int = 300,
                                    settle_secs: int = COINBASE_SETTLE_SECS) -> list[dict]:
    """
    Return the wallet's mature transparent coinbase UTXOs once the count is at
    least `min_count` and has held steady for `settle_secs` consecutive
    seconds. Unlike `wait_for_mature_coinbase_count`, this does not require an
    exact target, so it stays correct after earlier steps have spent an
    unknown number of coinbase UTXOs.
    """
    deadline = time.time() + timeout
    last_count = -1
    stable_secs = 0
    transparent = []
    while time.time() < deadline:
        try:
            transparent = mature_transparent_utxos(wallet)
            count = len(transparent)
            if count >= min_count and count == last_count:
                stable_secs += 1
            else:
                stable_secs = 0
            last_count = count
            if count >= min_count and stable_secs >= settle_secs:
                return transparent
        except Exception:
            pass
        time.sleep(1)

    raise AssertionError(
        "wait_for_stable_mature_coinbase: timeout after {}s; last saw {} mature "
        "transparent UTXOs (wanted >= {} stable for {}s)".format(
            timeout, last_count, min_count, settle_secs))


def wait_for_tx_scanned(wallet: RpcProxy, txid: str, timeout: int = 120) -> dict:
    """
    Return `txid`'s `z_viewtransaction` view once it carries a `fee`, i.e. the
    confirming block is scanned. Note this only guarantees the per-transaction
    view is current: `z_gettotalbalance`'s summary is computed from a separate
    internal scan tip that can still lag by a block, so poll it with
    `wait_for_total_balance` rather than reading it once right after this
    returns.
    """
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            tx = wallet.z_viewtransaction(txid)
            if 'fee' in tx:
                return tx
        except Exception as e:
            last_err = e
        time.sleep(1)
    raise AssertionError(
        "wait_for_tx_scanned: timeout after {}s for txid {} ({})".format(
            timeout, txid, last_err))


def wait_for_total_balance(wallet: RpcProxy, field: TotalBalanceField,
                           predicate: Callable[[Decimal], bool],
                           minconf: int = 1, include_watchonly: bool = True,
                           timeout: int = 60) -> Decimal:
    """
    Poll `z_gettotalbalance(minconf, include_watchonly)` until `predicate`
    holds for the `Decimal` value of `field` (a `TotalBalanceField`: the
    transparent pool, the aggregate `private` balance, or the `total`), then
    return that value. On timeout, return the last value read so the caller's
    assertion can report it.

    `z_gettotalbalance`'s summary is computed from an internal scan tip that can
    lag `wallet_tip` (or a just-scanned transaction) by a block, so a single
    read right after a `generate`/scan can miss the newest coinbase or note.
    This rides out that lag; see zcash/wallet#316.
    """
    deadline = time.time() + timeout
    last = None
    while True:
        try:
            last = Decimal(
                wallet.z_gettotalbalance(minconf, include_watchonly)[field])
            if predicate(last):
                return last
        except Exception:
            pass
        if time.time() >= deadline:
            return last
        time.sleep(1)


def wait_for_pool_note(wallet: RpcProxy, pool: Pool | str, minconf: int = 1,
                       timeout: int = 120) -> list[dict]:
    """Block until the wallet has at least one unspent note in `pool` (a `Pool`
    member or the equivalent string), then return those notes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            notes = [u for u in wallet.z_listunspent(minconf)
                     if u.get('pool') == pool]
            if notes:
                return notes
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError(
        "wait_for_pool_note: timeout after {}s waiting for a {} note".format(
            timeout, pool))


# One ZEC in zatoshis. Mirrors test_framework.mininode.COIN; defined here too
# so wallet tests can convert ZEC amounts without importing mininode.
COIN = 100000000


def zat(zec: str | Decimal | int | float) -> int:
    """Convert a ZEC amount (str / Decimal / number) to integer zatoshis."""
    return int((Decimal(str(zec)) * COIN).to_integral_value())


def _balance_held(bal: dict) -> int:
    """Sum every component (spendable + locked + pending + dust) of a
    `z_getbalances` Balance object, in zatoshis."""
    total = bal['spendable']['valueZat']
    for key in ('locked', 'pending', 'dust'):
        component = bal.get(key)
        if component is not None:
            total += component['valueZat']
    return total


def _account_pool(wallet: RpcProxy, account_uuid: str, pool: Pool | str,
                  minconf: int) -> list[dict]:
    """The list of Balance objects `account_uuid` holds in `pool`, per
    `z_getbalances` (one for shielded pools, `regular`/`coinbase` for the
    transparent pool). Empty if the account holds nothing in that pool."""
    balances = wallet.z_getbalances(minconf)
    for acct in balances.get('accounts', []):
        if acct.get('account_uuid') != account_uuid:
            continue
        bal = acct.get(pool)
        if bal is None:
            return []
        if pool == Pool.TRANSPARENT:
            return [bal[sub] for sub in ('regular', 'coinbase')
                    if bal.get(sub) is not None]
        return [bal]
    return []


def account_balance_zat(wallet: RpcProxy, account_uuid: str, pool: Pool | str,
                        minconf: int = 1) -> int:
    """
    The total balance, in zatoshis, held by `account_uuid` in `pool`
    (`'transparent'` / `'sapling'` / `'orchard'`), per `z_getbalances`. This
    sums spendable, pending, and dust, so a just-confirmed note still pending
    spendability is counted. Returns 0 if the account holds nothing there.

    `pool` may be a `Pool` member or the equivalent string. Note `z_getbalances`
    reports transparent coinbase only once it is mature, so pass
    `minconf=COINBASE_MATURITY` to see mature coinbase.
    """
    return sum(_balance_held(b)
               for b in _account_pool(wallet, account_uuid, pool, minconf))


def account_spendable_zat(wallet: RpcProxy, account_uuid: str, pool: Pool | str,
                          minconf: int = 1) -> int:
    """
    The immediately-spendable balance, in zatoshis, held by `account_uuid` in
    `pool`, per `z_getbalances` (the `spendable` component only). A freshly
    confirmed note is reported as pending until the wallet's scan catches up,
    so this can lag `account_balance_zat`; use `wait_for_account_spendable` to
    block on funds becoming spendable.
    """
    return sum(b['spendable']['valueZat']
               for b in _account_pool(wallet, account_uuid, pool, minconf))


def wait_for_wallet_sync(node, wallet, timeout: int = 60) -> None:
    """Block until `wallet` reports `node`'s current tip as its wallet tip."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        target = node.getblockcount()
        status = wallet.getwalletstatus()
        if status.get('wallet_tip', {}).get('height') == target:
            # Give the transparent balance accounting a beat to catch up.
            time.sleep(2)
            return
        time.sleep(0.5)
    raise AssertionError("wallet did not sync to node tip within %ss" % timeout)


def wait_for_account_spendable(wallet: RpcProxy, account_uuid: str, pool: Pool,
                               min_zat: int = 1, minconf: int = 1,
                               timeout: int = 120) -> int:
    """
    Block until `account_uuid`'s spendable `pool` balance is at least `min_zat`
    zatoshis, then return it. Rides out the lag between a shielded note being
    confirmed and the wallet reporting it as spendable (zcash/wallet#316).
    """
    if not isinstance(pool, Pool):
        raise TypeError(
            "pool must be a Pool enum member, got {!r}".format(type(pool)))
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = account_spendable_zat(wallet, account_uuid, pool, minconf)
            if last >= min_zat:
                return last
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError(
        "wait_for_account_spendable: timeout after {}s; account {} {} spendable "
        "is {} zat, wanted >= {} zat".format(
            timeout, account_uuid, pool, last, min_zat))


def wait_for_account_balance(wallet: RpcProxy, account_uuid: str, pool: Pool,
                             expected_zat: int, minconf: int = 1,
                             timeout: int = 120) -> int:
    """
    Block until `account_uuid`'s total `pool` balance equals `expected_zat`
    zatoshis (per `account_balance_zat`, which counts pending notes), then
    return it. Tolerates the brief lag between a block being scanned and the
    balance views catching up.

    `pool` must be a `Pool` member, so callers commit to a known value pool.
    """
    if not isinstance(pool, Pool):
        raise TypeError(
            "pool must be a Pool enum member, got {!r}".format(type(pool)))
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = account_balance_zat(wallet, account_uuid, pool, minconf)
            if last == expected_zat:
                return last
        except Exception:
            pass
        time.sleep(1)
    raise AssertionError(
        "wait_for_account_balance: timeout after {}s; account {} {} total "
        "is {} zat, wanted {} zat".format(
            timeout, account_uuid, pool, last, expected_zat))


def expect_rpc_error(callable_: Callable, *args, **kwargs):
    """Invoke an RPC and return the JSONRPCException; fail if it didn't raise.

    Accepts either JSONRPCException class (nodes and wallets raise different
    ones), so it works against both node and wallet proxies.
    """
    try:
        callable_(*args, **kwargs)
    except _RPC_EXCEPTIONS as e:
        return e
    raise AssertionError(
        "Expected RPC error, but call succeeded: {}({}, {})".format(
            getattr(callable_, '__name__', '?'), args, kwargs))


def assert_in_message(e, needle: str) -> None:
    """Assert that JSONRPCException `e`'s message contains `needle`."""
    msg = e.error['message']
    assert_true(needle in msg, "Expected {!r} in error, got: {!r}".format(needle, msg))
