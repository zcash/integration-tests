#!/usr/bin/env python3
# Copyright (c) 2010 ArtForz -- public domain half-a-node
# Copyright (c) 2012 Jeff Garzik
# Copyright (c) 2010-2016 The Bitcoin Core developers
# Copyright (c) 2017-2022 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# p2p.py - Bitcoin P2P network half-a-node
#
# The P2P transport layer, split out of mininode.py, which now holds only the
# consensus data structures and their (de)serialization.
#
# NodeConn: an object which manages p2p connectivity to a bitcoin node
# NodeConnCB: a base class that describes the interface for receiving
#             callbacks with network messages from a NodeConn
# msg_block, msg_tx, msg_headers, etc.:
#     data structures that represent network messages
#
# DORMANT ON PYTHON 3.12+. This module is built on `asyncore`, which was
# removed from the standard library in Python 3.12 (PEP 594). Every test that
# drives it is currently in DISABLED_SCRIPTS in qa/pull-tester/rpc-tests.py,
# because they exercise zcashd P2P behaviour that zebrad does not implement, so
# nothing in the running suite imports this module. Porting NodeConn and
# NetworkThread to `asyncio` (or `selectors`) is a prerequisite for re-enabling
# any of them.

try:
    import asyncore
except ModuleNotFoundError as e:  # pragma: no cover - see the note above
    raise ModuleNotFoundError(
        "test_framework.p2p requires the `asyncore` module, which was removed "
        "in Python 3.12. The P2P tests that use it are disabled; porting "
        "NodeConn/NetworkThread to asyncio is required before they can run."
    ) from e

import logging
import random
import socket
import struct
import sys
import time
from io import BytesIO
from threading import RLock, Thread

from .mininode import (
    BIP0031_VERSION,
    CAddress,
    CAlert,
    CBlock,
    CBlockHeader,
    CBlockLocator,
    CInv,
    CTransaction,
    MY_SUBVERSION,
    SAPLING_PROTO_VERSION,
    SPROUT_PROTO_VERSION,
    deser_string,
    deser_uint256,
    deser_vector,
    ser_string,
    ser_uint256,
    ser_vector,
    sha256,
)


# Keep our own socket map for asyncore, so that we can track disconnects
# ourselves (to workaround an issue with closing an asyncore socket when
# using select)
mininode_socket_map = dict()

# One lock for synchronizing all data access between the networking thread (see
# NetworkThread below) and the thread running the test logic.  For simplicity,
# NodeConn acquires this lock whenever delivering a message to a NodeConnCB,
# and whenever adding anything to the send buffer (in send_message()).  This
# lock should be acquired in the thread running the test logic to synchronize
# access to any data shared with the NodeConnCB or NodeConn.
mininode_lock = RLock()


# Objects that correspond to messages on the wire
class msg_version(object):
    command = b"version"

    def __init__(self, protocol_version=SPROUT_PROTO_VERSION):
        self.nVersion = protocol_version
        self.nServices = 1
        self.nTime = int(time.time())
        self.addrTo = CAddress()
        self.addrFrom = CAddress()
        self.nNonce = random.getrandbits(64)
        self.strSubVer = MY_SUBVERSION
        self.nStartingHeight = -1

    def deserialize(self, f):
        self.nVersion = struct.unpack("<i", f.read(4))[0]
        if self.nVersion == 10300:
            self.nVersion = 300
        self.nServices = struct.unpack("<Q", f.read(8))[0]
        self.nTime = struct.unpack("<q", f.read(8))[0]
        self.addrTo = CAddress()
        self.addrTo.deserialize(f)
        if self.nVersion >= 106:
            self.addrFrom = CAddress()
            self.addrFrom.deserialize(f)
            self.nNonce = struct.unpack("<Q", f.read(8))[0]
            self.strSubVer = deser_string(f)
            if self.nVersion >= 209:
                self.nStartingHeight = struct.unpack("<i", f.read(4))[0]
            else:
                self.nStartingHeight = None
        else:
            self.addrFrom = None
            self.nNonce = None
            self.strSubVer = None
            self.nStartingHeight = None

    def serialize(self):
        r = b""
        r += struct.pack("<i", self.nVersion)
        r += struct.pack("<Q", self.nServices)
        r += struct.pack("<q", self.nTime)
        r += self.addrTo.serialize()
        r += self.addrFrom.serialize()
        r += struct.pack("<Q", self.nNonce)
        r += ser_string(self.strSubVer)
        r += struct.pack("<i", self.nStartingHeight)
        return r

    def __repr__(self):
        return 'msg_version(nVersion=%i nServices=%i nTime=%s addrTo=%s addrFrom=%s nNonce=0x%016X strSubVer=%s nStartingHeight=%i)' \
            % (self.nVersion, self.nServices, time.ctime(self.nTime),
               self.addrTo, self.addrFrom, self.nNonce,
               self.strSubVer, self.nStartingHeight)


class msg_verack(object):
    command = b"verack"

    def __init__(self):
        pass

    def deserialize(self, f):
        pass

    def serialize(self):
        return b""

    def __repr__(self):
        return "msg_verack()"


class msg_addr(object):
    command = b"addr"

    def __init__(self):
        self.addrs = []

    def deserialize(self, f):
        self.addrs = deser_vector(f, CAddress)

    def serialize(self):
        return ser_vector(self.addrs)

    def __repr__(self):
        return "msg_addr(addrs=%r)" % (self.addrs,)


class msg_alert(object):
    command = b"alert"

    def __init__(self):
        self.alert = CAlert()

    def deserialize(self, f):
        self.alert = CAlert()
        self.alert.deserialize(f)

    def serialize(self):
        r = b""
        r += self.alert.serialize()
        return r

    def __repr__(self):
        return "msg_alert(alert=%s)" % (repr(self.alert), )


class msg_inv(object):
    command = b"inv"

    def __init__(self, inv=None):
        if inv is None:
            self.inv = []
        else:
            self.inv = inv

    def deserialize(self, f):
        self.inv = deser_vector(f, CInv)

    def serialize(self):
        return ser_vector(self.inv)

    def __repr__(self):
        return "msg_inv(inv=%s)" % (repr(self.inv))


class msg_getdata(object):
    command = b"getdata"

    def __init__(self, inv=None):
        self.inv = inv if inv != None else []

    def deserialize(self, f):
        self.inv = deser_vector(f, CInv)

    def serialize(self):
        return ser_vector(self.inv)

    def __repr__(self):
        return "msg_getdata(inv=%s)" % (repr(self.inv))


class msg_notfound(object):
    command = b"notfound"

    def __init__(self):
        self.inv = []

    def deserialize(self, f):
        self.inv = deser_vector(f, CInv)

    def serialize(self):
        return ser_vector(self.inv)

    def __repr__(self):
        return "msg_notfound(inv=%r)" % (self.inv,)


class msg_getblocks(object):
    command = b"getblocks"

    def __init__(self):
        self.locator = CBlockLocator()
        self.hashstop = 0

    def deserialize(self, f):
        self.locator = CBlockLocator()
        self.locator.deserialize(f)
        self.hashstop = deser_uint256(f)

    def serialize(self):
        r = b""
        r += self.locator.serialize()
        r += ser_uint256(self.hashstop)
        return r

    def __repr__(self):
        return "msg_getblocks(locator=%s hashstop=%064x)" \
            % (repr(self.locator), self.hashstop)


class msg_tx(object):
    command = b"tx"

    def __init__(self, tx=CTransaction()):
        self.tx = tx

    def deserialize(self, f):
        self.tx.deserialize(f)

    def serialize(self):
        return self.tx.serialize()

    def __repr__(self):
        return "msg_tx(tx=%s)" % (repr(self.tx))


class msg_block(object):
    command = b"block"

    def __init__(self, block=None):
        if block is None:
            self.block = CBlock()
        else:
            self.block = block

    def deserialize(self, f):
        self.block.deserialize(f)

    def serialize(self):
        return self.block.serialize()

    def __repr__(self):
        return "msg_block(block=%s)" % (repr(self.block))


class msg_getaddr(object):
    command = b"getaddr"

    def __init__(self):
        pass

    def deserialize(self, f):
        pass

    def serialize(self):
        return b""

    def __repr__(self):
        return "msg_getaddr()"


class msg_ping_prebip31(object):
    command = b"ping"

    def __init__(self):
        pass

    def deserialize(self, f):
        pass

    def serialize(self):
        return b""

    def __repr__(self):
        return "msg_ping() (pre-bip31)"


class msg_ping(object):
    command = b"ping"

    def __init__(self, nonce=0):
        self.nonce = nonce

    def deserialize(self, f):
        self.nonce = struct.unpack("<Q", f.read(8))[0]

    def serialize(self):
        r = b""
        r += struct.pack("<Q", self.nonce)
        return r

    def __repr__(self):
        return "msg_ping(nonce=%08x)" % self.nonce


class msg_pong(object):
    command = b"pong"

    def __init__(self, nonce=0):
        self.nonce = nonce

    def deserialize(self, f):
        self.nonce = struct.unpack("<Q", f.read(8))[0]

    def serialize(self):
        r = b""
        r += struct.pack("<Q", self.nonce)
        return r

    def __repr__(self):
        return "msg_pong(nonce=%08x)" % self.nonce


class msg_mempool(object):
    command = b"mempool"

    def __init__(self):
        pass

    def deserialize(self, f):
        pass

    def serialize(self):
        return b""

    def __repr__(self):
        return "msg_mempool()"


# getheaders message has
# number of entries
# vector of hashes
# hash_stop (hash of last desired block header, 0 to get as many as possible)
class msg_getheaders(object):
    command = b"getheaders"

    def __init__(self):
        self.locator = CBlockLocator()
        self.hashstop = 0

    def deserialize(self, f):
        self.locator = CBlockLocator()
        self.locator.deserialize(f)
        self.hashstop = deser_uint256(f)

    def serialize(self):
        r = b""
        r += self.locator.serialize()
        r += ser_uint256(self.hashstop)
        return r

    def __repr__(self):
        return "msg_getheaders(locator=%s, stop=%064x)" \
            % (repr(self.locator), self.hashstop)


# headers message has
# <count> <vector of block headers>
class msg_headers(object):
    command = b"headers"

    def __init__(self):
        self.headers = []

    def deserialize(self, f):
        # comment in bitcoind indicates these should be deserialized as blocks
        blocks = deser_vector(f, CBlock)
        for x in blocks:
            self.headers.append(CBlockHeader(x))

    def serialize(self):
        blocks = [CBlock(x) for x in self.headers]
        return ser_vector(blocks)

    def __repr__(self):
        return "msg_headers(headers=%s)" % repr(self.headers)


class msg_reject(object):
    command = b"reject"
    REJECT_MALFORMED = 1

    def __init__(self):
        self.message = b""
        self.code = 0
        self.reason = b""
        self.data = 0

    def deserialize(self, f):
        self.message = deser_string(f)
        self.code = struct.unpack("<B", f.read(1))[0]
        self.reason = deser_string(f)
        if (self.code != self.REJECT_MALFORMED and
                (self.message == b"block" or self.message == b"tx")):
            self.data = deser_uint256(f)

    def serialize(self):
        r = ser_string(self.message)
        r += struct.pack("<B", self.code)
        r += ser_string(self.reason)
        if (self.code != self.REJECT_MALFORMED and
                (self.message == b"block" or self.message == b"tx")):
            r += ser_uint256(self.data)
        return r

    def __eq__(self, other):
        return (
            (type(self) == type(other)) and (
                (self.message, self.code, self.reason, self.data) ==
                (other.message, other.code, other.reason, other.data)
            )
        )

    def __repr__(self):
        return "msg_reject: %s %d %s [%064x]" \
            % (self.message, self.code, self.reason, self.data)


class msg_filteradd(object):
    command = b"filteradd"

    def __init__(self):
        self.data = b""

    def deserialize(self, f):
        self.data = deser_string(f)

    def serialize(self):
        return ser_string(self.data)

    def __repr__(self):
        return "msg_filteradd(data=%r)" % (self.data,)


class msg_filterclear(object):
    command = b"filterclear"

    def __init__(self):
        pass

    def deserialize(self, f):
        pass

    def serialize(self):
        return b""

    def __repr__(self):
        return "msg_filterclear()"


# This is what a callback should look like for NodeConn
# Reimplement the on_* functions to provide handling for events
class NodeConnCB(object):
    def __init__(self):
        self.verack_received = False

    # Derived classes should call this function once to set the message map
    # which associates the derived classes' functions to incoming messages
    def create_callback_map(self):
        self.cbmap = {
            b"version": self.on_version,
            b"verack": self.on_verack,
            b"addr": self.on_addr,
            b"alert": self.on_alert,
            b"inv": self.on_inv,
            b"getdata": self.on_getdata,
            b"notfound": self.on_notfound,
            b"getblocks": self.on_getblocks,
            b"tx": self.on_tx,
            b"block": self.on_block,
            b"getaddr": self.on_getaddr,
            b"ping": self.on_ping,
            b"pong": self.on_pong,
            b"headers": self.on_headers,
            b"getheaders": self.on_getheaders,
            b"reject": self.on_reject,
            b"mempool": self.on_mempool
        }

    def deliver(self, conn, message):
        with mininode_lock:
            try:
                self.cbmap[message.command](conn, message)
            except:
                print("ERROR delivering %r (%s)" % (message,
                                                    sys.exc_info()[0]))

    def on_version(self, conn, message):
        if message.nVersion >= 209:
            conn.send_message(msg_verack())
        conn.ver_send = min(SPROUT_PROTO_VERSION, message.nVersion)
        if message.nVersion < 209:
            conn.ver_recv = conn.ver_send

    def on_verack(self, conn, message):
        conn.ver_recv = conn.ver_send
        self.verack_received = True

    def on_inv(self, conn, message):
        want = msg_getdata()
        for i in message.inv:
            if i.type != 0:
                want.inv.append(i)
        if len(want.inv):
            conn.send_message(want)

    def on_addr(self, conn, message): pass
    def on_alert(self, conn, message): pass
    def on_getdata(self, conn, message): pass
    def on_notfound(self, conn, message): pass
    def on_getblocks(self, conn, message): pass
    def on_tx(self, conn, message): pass
    def on_block(self, conn, message): pass
    def on_getaddr(self, conn, message): pass
    def on_headers(self, conn, message): pass
    def on_getheaders(self, conn, message): pass
    def on_ping(self, conn, message):
        if conn.ver_send > BIP0031_VERSION:
            conn.send_message(msg_pong(message.nonce))
    def on_reject(self, conn, message): pass
    def on_close(self, conn): pass
    def on_mempool(self, conn): pass
    def on_pong(self, conn, message): pass


# The actual NodeConn class
# This class provides an interface for a p2p connection to a specified node
class NodeConn(asyncore.dispatcher):
    messagemap = {
        b"version": msg_version,
        b"verack": msg_verack,
        b"addr": msg_addr,
        b"alert": msg_alert,
        b"inv": msg_inv,
        b"getdata": msg_getdata,
        b"notfound": msg_notfound,
        b"getblocks": msg_getblocks,
        b"tx": msg_tx,
        b"block": msg_block,
        b"getaddr": msg_getaddr,
        b"ping": msg_ping,
        b"pong": msg_pong,
        b"headers": msg_headers,
        b"getheaders": msg_getheaders,
        b"reject": msg_reject,
        b"mempool": msg_mempool
    }
    MAGIC_BYTES = {
        "mainnet": b"\x24\xe9\x27\x64",   # mainnet
        "testnet3": b"\xfa\x1a\xf9\xbf",  # testnet3
        "regtest": b"\xaa\xe8\x3f\x5f"    # regtest
    }

    def __init__(self, dstaddr, dstport, rpc, callback, net="regtest", protocol_version=SAPLING_PROTO_VERSION):
        asyncore.dispatcher.__init__(self, map=mininode_socket_map)
        self.log = logging.getLogger("NodeConn(%s:%d)" % (dstaddr, dstport))
        self.dstaddr = dstaddr
        self.dstport = dstport
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sendbuf = b""
        self.recvbuf = b""
        self.ver_send = 209
        self.ver_recv = 209
        self.last_sent = 0
        self.state = "connecting"
        self.network = net
        self.cb = callback
        self.disconnect = False

        # stuff version msg into sendbuf
        vt = msg_version(protocol_version)
        vt.addrTo.ip = self.dstaddr
        vt.addrTo.port = self.dstport
        vt.addrFrom.ip = "0.0.0.0"
        vt.addrFrom.port = 0
        self.send_message(vt, True)
        print('MiniNode: Connecting to Bitcoin Node IP # ' + dstaddr + ':' \
            + str(dstport) + ' using version ' + str(protocol_version))

        try:
            self.connect((dstaddr, dstport))
        except:
            self.handle_close()
        self.rpc = rpc

    def show_debug_msg(self, msg):
        self.log.debug(msg)

    def handle_connect(self):
        self.show_debug_msg("MiniNode: Connected & Listening: \n")
        self.state = b"connected"

    def handle_close(self):
        self.show_debug_msg("MiniNode: Closing Connection to %s:%d... "
                            % (self.dstaddr, self.dstport))
        self.state = b"closed"
        self.recvbuf = b""
        self.sendbuf = b""
        try:
            self.close()
        except:
            pass
        self.cb.on_close(self)

    def handle_read(self):
        try:
            t = self.recv(8192)
            if len(t) > 0:
                self.recvbuf += t
                self.got_data()
        except:
            pass

    def readable(self):
        return True

    def writable(self):
        with mininode_lock:
            length = len(self.sendbuf)
        return (length > 0)

    def handle_write(self):
        with mininode_lock:
            try:
                sent = self.send(self.sendbuf)
            except:
                self.handle_close()
                return
            self.sendbuf = self.sendbuf[sent:]

    def got_data(self):
        try:
            while True:
                if len(self.recvbuf) < 4:
                    return
                if self.recvbuf[:4] != self.MAGIC_BYTES[self.network]:
                    raise ValueError("got garbage %r" % (self.recvbuf,))
                if self.ver_recv < 209:
                    if len(self.recvbuf) < 4 + 12 + 4:
                        return
                    command = self.recvbuf[4:4+12].split(b"\x00", 1)[0]
                    msglen = struct.unpack("<i", self.recvbuf[4+12:4+12+4])[0]
                    checksum = None
                    if len(self.recvbuf) < 4 + 12 + 4 + msglen:
                        return
                    msg = self.recvbuf[4+12+4:4+12+4+msglen]
                    self.recvbuf = self.recvbuf[4+12+4+msglen:]
                else:
                    if len(self.recvbuf) < 4 + 12 + 4 + 4:
                        return
                    command = self.recvbuf[4:4+12].split(b"\x00", 1)[0]
                    msglen = struct.unpack("<i", self.recvbuf[4+12:4+12+4])[0]
                    checksum = self.recvbuf[4+12+4:4+12+4+4]
                    if len(self.recvbuf) < 4 + 12 + 4 + 4 + msglen:
                        return
                    msg = self.recvbuf[4+12+4+4:4+12+4+4+msglen]
                    th = sha256(msg)
                    h = sha256(th)
                    if checksum != h[:4]:
                        raise ValueError("got bad checksum %r" % (self.recvbuf,))
                    self.recvbuf = self.recvbuf[4+12+4+4+msglen:]
                if command in self.messagemap:
                    f = BytesIO(msg)
                    t = self.messagemap[command]()
                    t.deserialize(f)
                    self.got_message(t)
                else:
                    self.show_debug_msg("Unknown command: '" + command + "' " +
                                        repr(msg))
        except Exception as e:
            print('got_data:', repr(e))
            # import  traceback
            # traceback.print_tb(sys.exc_info()[2])

    def send_message(self, message, pushbuf=False):
        if self.state != b"connected" and not pushbuf:
            return
        self.show_debug_msg("Send %s" % repr(message))
        command = message.command
        data = message.serialize()
        tmsg = self.MAGIC_BYTES[self.network]
        tmsg += command
        tmsg += b"\x00" * (12 - len(command))
        tmsg += struct.pack("<I", len(data))
        if self.ver_send >= 209:
            th = sha256(data)
            h = sha256(th)
            tmsg += h[:4]
        tmsg += data
        with mininode_lock:
            self.sendbuf += tmsg
            self.last_sent = time.time()

    def got_message(self, message):
        if message.command == b"version":
            if message.nVersion <= BIP0031_VERSION:
                self.messagemap[b'ping'] = msg_ping_prebip31
        if self.last_sent + 30 * 60 < time.time():
            self.send_message(self.messagemap[b'ping']())
        self.show_debug_msg("Recv %s" % repr(message))
        self.cb.deliver(self, message)

    def disconnect_node(self):
        self.disconnect = True


class NetworkThread(Thread):
    def run(self):
        while mininode_socket_map:
            # We check for whether to disconnect outside of the asyncore
            # loop to workaround the behavior of asyncore when using
            # select
            disconnected = []
            for fd, obj in mininode_socket_map.items():
                if obj.disconnect:
                    disconnected.append(obj)
            [ obj.handle_close() for obj in disconnected ]
            asyncore.loop(0.1, use_poll=True, map=mininode_socket_map, count=1)


# An exception we can raise if we detect a potential disconnect
# (p2p or rpc) before the test is complete
class EarlyDisconnectError(Exception):
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return repr(self.value)
