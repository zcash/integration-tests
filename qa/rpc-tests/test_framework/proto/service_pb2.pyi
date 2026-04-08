import compact_formats_pb2 as _compact_formats_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PoolType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    POOL_TYPE_INVALID: _ClassVar[PoolType]
    TRANSPARENT: _ClassVar[PoolType]
    SAPLING: _ClassVar[PoolType]
    ORCHARD: _ClassVar[PoolType]

class ShieldedProtocol(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    sapling: _ClassVar[ShieldedProtocol]
    orchard: _ClassVar[ShieldedProtocol]
POOL_TYPE_INVALID: PoolType
TRANSPARENT: PoolType
SAPLING: PoolType
ORCHARD: PoolType
sapling: ShieldedProtocol
orchard: ShieldedProtocol

class BlockID(_message.Message):
    __slots__ = ("height", "hash")
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    height: int
    hash: bytes
    def __init__(self, height: _Optional[int] = ..., hash: _Optional[bytes] = ...) -> None: ...

class BlockRange(_message.Message):
    __slots__ = ("start", "end", "poolTypes")
    START_FIELD_NUMBER: _ClassVar[int]
    END_FIELD_NUMBER: _ClassVar[int]
    POOLTYPES_FIELD_NUMBER: _ClassVar[int]
    start: BlockID
    end: BlockID
    poolTypes: _containers.RepeatedScalarFieldContainer[PoolType]
    def __init__(self, start: _Optional[_Union[BlockID, _Mapping]] = ..., end: _Optional[_Union[BlockID, _Mapping]] = ..., poolTypes: _Optional[_Iterable[_Union[PoolType, str]]] = ...) -> None: ...

class TxFilter(_message.Message):
    __slots__ = ("block", "index", "hash")
    BLOCK_FIELD_NUMBER: _ClassVar[int]
    INDEX_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    block: BlockID
    index: int
    hash: bytes
    def __init__(self, block: _Optional[_Union[BlockID, _Mapping]] = ..., index: _Optional[int] = ..., hash: _Optional[bytes] = ...) -> None: ...

class RawTransaction(_message.Message):
    __slots__ = ("data", "height")
    DATA_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    height: int
    def __init__(self, data: _Optional[bytes] = ..., height: _Optional[int] = ...) -> None: ...

class SendResponse(_message.Message):
    __slots__ = ("errorCode", "errorMessage")
    ERRORCODE_FIELD_NUMBER: _ClassVar[int]
    ERRORMESSAGE_FIELD_NUMBER: _ClassVar[int]
    errorCode: int
    errorMessage: str
    def __init__(self, errorCode: _Optional[int] = ..., errorMessage: _Optional[str] = ...) -> None: ...

class ChainSpec(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class LightdInfo(_message.Message):
    __slots__ = ("version", "vendor", "taddrSupport", "chainName", "saplingActivationHeight", "consensusBranchId", "blockHeight", "gitCommit", "branch", "buildDate", "buildUser", "estimatedHeight", "zcashdBuild", "zcashdSubversion", "donationAddress", "upgradeName", "upgradeHeight", "lightwalletProtocolVersion")
    VERSION_FIELD_NUMBER: _ClassVar[int]
    VENDOR_FIELD_NUMBER: _ClassVar[int]
    TADDRSUPPORT_FIELD_NUMBER: _ClassVar[int]
    CHAINNAME_FIELD_NUMBER: _ClassVar[int]
    SAPLINGACTIVATIONHEIGHT_FIELD_NUMBER: _ClassVar[int]
    CONSENSUSBRANCHID_FIELD_NUMBER: _ClassVar[int]
    BLOCKHEIGHT_FIELD_NUMBER: _ClassVar[int]
    GITCOMMIT_FIELD_NUMBER: _ClassVar[int]
    BRANCH_FIELD_NUMBER: _ClassVar[int]
    BUILDDATE_FIELD_NUMBER: _ClassVar[int]
    BUILDUSER_FIELD_NUMBER: _ClassVar[int]
    ESTIMATEDHEIGHT_FIELD_NUMBER: _ClassVar[int]
    ZCASHDBUILD_FIELD_NUMBER: _ClassVar[int]
    ZCASHDSUBVERSION_FIELD_NUMBER: _ClassVar[int]
    DONATIONADDRESS_FIELD_NUMBER: _ClassVar[int]
    UPGRADENAME_FIELD_NUMBER: _ClassVar[int]
    UPGRADEHEIGHT_FIELD_NUMBER: _ClassVar[int]
    LIGHTWALLETPROTOCOLVERSION_FIELD_NUMBER: _ClassVar[int]
    version: str
    vendor: str
    taddrSupport: bool
    chainName: str
    saplingActivationHeight: int
    consensusBranchId: str
    blockHeight: int
    gitCommit: str
    branch: str
    buildDate: str
    buildUser: str
    estimatedHeight: int
    zcashdBuild: str
    zcashdSubversion: str
    donationAddress: str
    upgradeName: str
    upgradeHeight: int
    lightwalletProtocolVersion: str
    def __init__(self, version: _Optional[str] = ..., vendor: _Optional[str] = ..., taddrSupport: bool = ..., chainName: _Optional[str] = ..., saplingActivationHeight: _Optional[int] = ..., consensusBranchId: _Optional[str] = ..., blockHeight: _Optional[int] = ..., gitCommit: _Optional[str] = ..., branch: _Optional[str] = ..., buildDate: _Optional[str] = ..., buildUser: _Optional[str] = ..., estimatedHeight: _Optional[int] = ..., zcashdBuild: _Optional[str] = ..., zcashdSubversion: _Optional[str] = ..., donationAddress: _Optional[str] = ..., upgradeName: _Optional[str] = ..., upgradeHeight: _Optional[int] = ..., lightwalletProtocolVersion: _Optional[str] = ...) -> None: ...

class TransparentAddressBlockFilter(_message.Message):
    __slots__ = ("address", "range")
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    RANGE_FIELD_NUMBER: _ClassVar[int]
    address: str
    range: BlockRange
    def __init__(self, address: _Optional[str] = ..., range: _Optional[_Union[BlockRange, _Mapping]] = ...) -> None: ...

class Duration(_message.Message):
    __slots__ = ("intervalUs",)
    INTERVALUS_FIELD_NUMBER: _ClassVar[int]
    intervalUs: int
    def __init__(self, intervalUs: _Optional[int] = ...) -> None: ...

class PingResponse(_message.Message):
    __slots__ = ("entry", "exit")
    ENTRY_FIELD_NUMBER: _ClassVar[int]
    EXIT_FIELD_NUMBER: _ClassVar[int]
    entry: int
    exit: int
    def __init__(self, entry: _Optional[int] = ..., exit: _Optional[int] = ...) -> None: ...

class Address(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class AddressList(_message.Message):
    __slots__ = ("addresses",)
    ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    addresses: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, addresses: _Optional[_Iterable[str]] = ...) -> None: ...

class Balance(_message.Message):
    __slots__ = ("valueZat",)
    VALUEZAT_FIELD_NUMBER: _ClassVar[int]
    valueZat: int
    def __init__(self, valueZat: _Optional[int] = ...) -> None: ...

class GetMempoolTxRequest(_message.Message):
    __slots__ = ("exclude_txid_suffixes", "poolTypes")
    EXCLUDE_TXID_SUFFIXES_FIELD_NUMBER: _ClassVar[int]
    POOLTYPES_FIELD_NUMBER: _ClassVar[int]
    exclude_txid_suffixes: _containers.RepeatedScalarFieldContainer[bytes]
    poolTypes: _containers.RepeatedScalarFieldContainer[PoolType]
    def __init__(self, exclude_txid_suffixes: _Optional[_Iterable[bytes]] = ..., poolTypes: _Optional[_Iterable[_Union[PoolType, str]]] = ...) -> None: ...

class TreeState(_message.Message):
    __slots__ = ("network", "height", "hash", "time", "saplingTree", "orchardTree")
    NETWORK_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    SAPLINGTREE_FIELD_NUMBER: _ClassVar[int]
    ORCHARDTREE_FIELD_NUMBER: _ClassVar[int]
    network: str
    height: int
    hash: str
    time: int
    saplingTree: str
    orchardTree: str
    def __init__(self, network: _Optional[str] = ..., height: _Optional[int] = ..., hash: _Optional[str] = ..., time: _Optional[int] = ..., saplingTree: _Optional[str] = ..., orchardTree: _Optional[str] = ...) -> None: ...

class GetSubtreeRootsArg(_message.Message):
    __slots__ = ("startIndex", "shieldedProtocol", "maxEntries")
    STARTINDEX_FIELD_NUMBER: _ClassVar[int]
    SHIELDEDPROTOCOL_FIELD_NUMBER: _ClassVar[int]
    MAXENTRIES_FIELD_NUMBER: _ClassVar[int]
    startIndex: int
    shieldedProtocol: ShieldedProtocol
    maxEntries: int
    def __init__(self, startIndex: _Optional[int] = ..., shieldedProtocol: _Optional[_Union[ShieldedProtocol, str]] = ..., maxEntries: _Optional[int] = ...) -> None: ...

class SubtreeRoot(_message.Message):
    __slots__ = ("rootHash", "completingBlockHash", "completingBlockHeight")
    ROOTHASH_FIELD_NUMBER: _ClassVar[int]
    COMPLETINGBLOCKHASH_FIELD_NUMBER: _ClassVar[int]
    COMPLETINGBLOCKHEIGHT_FIELD_NUMBER: _ClassVar[int]
    rootHash: bytes
    completingBlockHash: bytes
    completingBlockHeight: int
    def __init__(self, rootHash: _Optional[bytes] = ..., completingBlockHash: _Optional[bytes] = ..., completingBlockHeight: _Optional[int] = ...) -> None: ...

class GetAddressUtxosArg(_message.Message):
    __slots__ = ("addresses", "startHeight", "maxEntries")
    ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    STARTHEIGHT_FIELD_NUMBER: _ClassVar[int]
    MAXENTRIES_FIELD_NUMBER: _ClassVar[int]
    addresses: _containers.RepeatedScalarFieldContainer[str]
    startHeight: int
    maxEntries: int
    def __init__(self, addresses: _Optional[_Iterable[str]] = ..., startHeight: _Optional[int] = ..., maxEntries: _Optional[int] = ...) -> None: ...

class GetAddressUtxosReply(_message.Message):
    __slots__ = ("address", "txid", "index", "script", "valueZat", "height")
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    TXID_FIELD_NUMBER: _ClassVar[int]
    INDEX_FIELD_NUMBER: _ClassVar[int]
    SCRIPT_FIELD_NUMBER: _ClassVar[int]
    VALUEZAT_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    address: str
    txid: bytes
    index: int
    script: bytes
    valueZat: int
    height: int
    def __init__(self, address: _Optional[str] = ..., txid: _Optional[bytes] = ..., index: _Optional[int] = ..., script: _Optional[bytes] = ..., valueZat: _Optional[int] = ..., height: _Optional[int] = ...) -> None: ...

class GetAddressUtxosReplyList(_message.Message):
    __slots__ = ("addressUtxos",)
    ADDRESSUTXOS_FIELD_NUMBER: _ClassVar[int]
    addressUtxos: _containers.RepeatedCompositeFieldContainer[GetAddressUtxosReply]
    def __init__(self, addressUtxos: _Optional[_Iterable[_Union[GetAddressUtxosReply, _Mapping]]] = ...) -> None: ...
