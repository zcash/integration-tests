from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ChainMetadata(_message.Message):
    __slots__ = ("saplingCommitmentTreeSize", "orchardCommitmentTreeSize")
    SAPLINGCOMMITMENTTREESIZE_FIELD_NUMBER: _ClassVar[int]
    ORCHARDCOMMITMENTTREESIZE_FIELD_NUMBER: _ClassVar[int]
    saplingCommitmentTreeSize: int
    orchardCommitmentTreeSize: int
    def __init__(self, saplingCommitmentTreeSize: _Optional[int] = ..., orchardCommitmentTreeSize: _Optional[int] = ...) -> None: ...

class CompactBlock(_message.Message):
    __slots__ = ("protoVersion", "height", "hash", "prevHash", "time", "header", "vtx", "chainMetadata")
    PROTOVERSION_FIELD_NUMBER: _ClassVar[int]
    HEIGHT_FIELD_NUMBER: _ClassVar[int]
    HASH_FIELD_NUMBER: _ClassVar[int]
    PREVHASH_FIELD_NUMBER: _ClassVar[int]
    TIME_FIELD_NUMBER: _ClassVar[int]
    HEADER_FIELD_NUMBER: _ClassVar[int]
    VTX_FIELD_NUMBER: _ClassVar[int]
    CHAINMETADATA_FIELD_NUMBER: _ClassVar[int]
    protoVersion: int
    height: int
    hash: bytes
    prevHash: bytes
    time: int
    header: bytes
    vtx: _containers.RepeatedCompositeFieldContainer[CompactTx]
    chainMetadata: ChainMetadata
    def __init__(self, protoVersion: _Optional[int] = ..., height: _Optional[int] = ..., hash: _Optional[bytes] = ..., prevHash: _Optional[bytes] = ..., time: _Optional[int] = ..., header: _Optional[bytes] = ..., vtx: _Optional[_Iterable[_Union[CompactTx, _Mapping]]] = ..., chainMetadata: _Optional[_Union[ChainMetadata, _Mapping]] = ...) -> None: ...

class CompactTx(_message.Message):
    __slots__ = ("index", "txid", "fee", "spends", "outputs", "actions", "vin", "vout")
    INDEX_FIELD_NUMBER: _ClassVar[int]
    TXID_FIELD_NUMBER: _ClassVar[int]
    FEE_FIELD_NUMBER: _ClassVar[int]
    SPENDS_FIELD_NUMBER: _ClassVar[int]
    OUTPUTS_FIELD_NUMBER: _ClassVar[int]
    ACTIONS_FIELD_NUMBER: _ClassVar[int]
    VIN_FIELD_NUMBER: _ClassVar[int]
    VOUT_FIELD_NUMBER: _ClassVar[int]
    index: int
    txid: bytes
    fee: int
    spends: _containers.RepeatedCompositeFieldContainer[CompactSaplingSpend]
    outputs: _containers.RepeatedCompositeFieldContainer[CompactSaplingOutput]
    actions: _containers.RepeatedCompositeFieldContainer[CompactOrchardAction]
    vin: _containers.RepeatedCompositeFieldContainer[CompactTxIn]
    vout: _containers.RepeatedCompositeFieldContainer[TxOut]
    def __init__(self, index: _Optional[int] = ..., txid: _Optional[bytes] = ..., fee: _Optional[int] = ..., spends: _Optional[_Iterable[_Union[CompactSaplingSpend, _Mapping]]] = ..., outputs: _Optional[_Iterable[_Union[CompactSaplingOutput, _Mapping]]] = ..., actions: _Optional[_Iterable[_Union[CompactOrchardAction, _Mapping]]] = ..., vin: _Optional[_Iterable[_Union[CompactTxIn, _Mapping]]] = ..., vout: _Optional[_Iterable[_Union[TxOut, _Mapping]]] = ...) -> None: ...

class CompactTxIn(_message.Message):
    __slots__ = ("prevoutTxid", "prevoutIndex")
    PREVOUTTXID_FIELD_NUMBER: _ClassVar[int]
    PREVOUTINDEX_FIELD_NUMBER: _ClassVar[int]
    prevoutTxid: bytes
    prevoutIndex: int
    def __init__(self, prevoutTxid: _Optional[bytes] = ..., prevoutIndex: _Optional[int] = ...) -> None: ...

class TxOut(_message.Message):
    __slots__ = ("value", "scriptPubKey")
    VALUE_FIELD_NUMBER: _ClassVar[int]
    SCRIPTPUBKEY_FIELD_NUMBER: _ClassVar[int]
    value: int
    scriptPubKey: bytes
    def __init__(self, value: _Optional[int] = ..., scriptPubKey: _Optional[bytes] = ...) -> None: ...

class CompactSaplingSpend(_message.Message):
    __slots__ = ("nf",)
    NF_FIELD_NUMBER: _ClassVar[int]
    nf: bytes
    def __init__(self, nf: _Optional[bytes] = ...) -> None: ...

class CompactSaplingOutput(_message.Message):
    __slots__ = ("cmu", "ephemeralKey", "ciphertext")
    CMU_FIELD_NUMBER: _ClassVar[int]
    EPHEMERALKEY_FIELD_NUMBER: _ClassVar[int]
    CIPHERTEXT_FIELD_NUMBER: _ClassVar[int]
    cmu: bytes
    ephemeralKey: bytes
    ciphertext: bytes
    def __init__(self, cmu: _Optional[bytes] = ..., ephemeralKey: _Optional[bytes] = ..., ciphertext: _Optional[bytes] = ...) -> None: ...

class CompactOrchardAction(_message.Message):
    __slots__ = ("nullifier", "cmx", "ephemeralKey", "ciphertext")
    NULLIFIER_FIELD_NUMBER: _ClassVar[int]
    CMX_FIELD_NUMBER: _ClassVar[int]
    EPHEMERALKEY_FIELD_NUMBER: _ClassVar[int]
    CIPHERTEXT_FIELD_NUMBER: _ClassVar[int]
    nullifier: bytes
    cmx: bytes
    ephemeralKey: bytes
    ciphertext: bytes
    def __init__(self, nullifier: _Optional[bytes] = ..., cmx: _Optional[bytes] = ..., ephemeralKey: _Optional[bytes] = ..., ciphertext: _Optional[bytes] = ...) -> None: ...
