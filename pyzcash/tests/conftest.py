"""Shared fixtures.

The raw test vectors live in vectors.py; this module exposes them as fixtures,
along with the constructed objects several test modules need. Anything fed to
``@pytest.mark.parametrize`` is imported from vectors.py directly instead,
because parametrize is resolved at collection time and cannot consume a fixture.
"""

from __future__ import annotations

from typing import Protocol

import pytest

from pyzcash import Network
from pyzcash.address import Receiver, ReceiverType, UnifiedAddress
from tests import vectors


class MakeUnified(Protocol):
    """The signature of the ``make_unified`` factory fixture."""

    def __call__(self, *receivers: Receiver) -> UnifiedAddress: ...


# --- networks ---------------------------------------------------------------


@pytest.fixture(scope="session")
def mainnet() -> Network:
    return Network.MAIN


@pytest.fixture(scope="session")
def testnet() -> Network:
    return Network.TEST


@pytest.fixture(scope="session")
def regtest() -> Network:
    """The network the fixtures in vectors.py come from."""
    return Network.REGTEST


# --- real encodings from the integration suite's test-wallet -----------------


@pytest.fixture(scope="session")
def sapling_extfvk() -> str:
    """A Sapling extended full viewing key: Bech32, 169-byte payload."""
    return vectors.SAPLING_EXTFVK


@pytest.fixture(scope="session")
def unified_fvk() -> str:
    """A unified full viewing key: Bech32m, F4Jumbled."""
    return vectors.UNIFIED_FVK


@pytest.fixture(scope="session")
def p2sh_address() -> str:
    return vectors.P2SH_ADDRESS


@pytest.fixture(scope="session")
def sapling_address() -> str:
    return vectors.SAPLING_ADDRESS


@pytest.fixture(scope="session")
def unified_address() -> str:
    return vectors.UNIFIED_ADDRESS


# --- constructed receivers --------------------------------------------------
#
# Receivers are immutable, so one instance can be shared across the session.


@pytest.fixture(scope="session")
def p2pkh_receiver() -> Receiver:
    return Receiver(type=ReceiverType.P2PKH, data=bytes(20))


@pytest.fixture(scope="session")
def p2sh_receiver() -> Receiver:
    return Receiver(type=ReceiverType.P2SH, data=bytes(20))


@pytest.fixture(scope="session")
def sapling_receiver() -> Receiver:
    return Receiver(type=ReceiverType.SAPLING, data=bytes(43))


@pytest.fixture(scope="session")
def orchard_receiver() -> Receiver:
    return Receiver(type=ReceiverType.ORCHARD, data=bytes(43))


@pytest.fixture
def make_unified(mainnet: Network) -> MakeUnified:
    """Build a mainnet unified address from the given receivers.

    A factory rather than a value, because most of these tests are about which
    receiver combinations the ZIP 316 rules admit, so each one needs a different
    address, and several expect construction to raise.
    """

    def _make(*receivers: Receiver) -> UnifiedAddress:
        return UnifiedAddress(network=mainnet, receivers=tuple(receivers))

    return _make
