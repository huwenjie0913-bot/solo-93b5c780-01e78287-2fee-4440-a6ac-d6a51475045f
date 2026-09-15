"""证据保全包：NFC 规范化、canonical JSON、SHA-256 Merkle 树与交接哈希链。

处理流水线（全部输入均按此顺序**确定性**复算，同一请求永远得到同一根哈希）：

1. 文件路径做 Unicode NFC 规范化，逐字符校验越界（绝对路径、盘符、``..``、
   反斜杠、NUL 等），并检查归一后路径两两不碰撞；
2. 文件按归一后路径的 **UTF-8 字节序**排序；
3. 每个文件的 ``{path, size, digest}`` 以 canonical JSON
   （``sort_keys``、无多余分隔符、UTF-8、``ensure_ascii=False``）编码，
   SHA-256 得叶子哈希；
4. 叶子两两配对构成 Merkle 树（奇数个节点时最后一个直接提升到上一层），
   域前缀 ``\\x00``（叶子）/ ``\\x01``（内部节点）抵抗第二原像混淆；
5. 交接记录按序号生成哈希链：``h_i = H(\\x02 ‖ canonical{seq,timestamp,
   handler,note,prev_hash})``，虚根为 64 个 ``0``；
6. 包根哈希 ``H(\\x03 ‖ canonical{version,previous_root_hash,merkle_root,
   handover_head_hash})``，新包凭此前向锚定到主链。

校验失败时收集**全部**结构化错误（含输入位置，如 ``files[2].digest``），
不做快速失败，便于同一请求一次定位所有冲突。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

# ----------------------------- 常量 -----------------------------

ZERO_HASH = "0" * 64
LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"
HANDOVER_PREFIX = b"\x02"
ROOT_PREFIX = b"\x03"

DEFAULT_MAX_FILES = 10_000
DEFAULT_MAX_TOTAL_BYTES = 256 * 1024 * 1024

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{64}$")
_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


# ----------------------------- 输入模型 -----------------------------


class EvidenceFileIn(BaseModel):
    """包内一个文件的声明（路径、大小、摘要）与 Base64 内容。

    ``content_base64`` 为 null 或缺省表示**列名但未交件**（缺件）。
    """

    path: str = Field(..., description="证据文件相对路径（提交后按 NFC 归一）")
    size: int = Field(..., ge=0, description="声明的文件大小（字节）")
    digest: str = Field(..., description="声明的 SHA-256 摘要（64 位十六进制，可带 sha256: 前缀）")
    content_base64: Optional[str] = Field(
        None, description="文件内容的标准 Base64；null 表示缺件"
    )


class HandoverIn(BaseModel):
    """一次交接记录（ custody transfer ）。"""

    seq: int = Field(..., ge=1, description="交接序号（全链连续，从 1 开始）")
    timestamp: str = Field(
        ..., description="交接时刻，必须是带时区的 ISO 8601（如 2026-09-15T08:00:00Z）"
    )
    handler: str = Field(..., description="交接经办人/单位")
    note: Optional[str] = Field(None, description="交接备注")


class EvidencePackageIn(BaseModel):
    """证据保全包提交请求。"""

    version: str = Field(..., description="包版本标识（字母数字 . _ -，1~128 字符，全链唯一）")
    previous_root_hash: Optional[str] = Field(
        None, description="前序包根哈希；首个包（创世包）为 null"
    )
    files: list[EvidenceFileIn] = Field(default_factory=list)
    handovers: list[HandoverIn] = Field(default_factory=list)


# ----------------------------- 结构化输出模型 -----------------------------


class EvidenceError(BaseModel):
    """一条结构化拒收原因，可定位到请求中的具体输入位置。"""

    code: str = Field(..., description="错误码，如 digest_mismatch / double_successor")
    message: str
    location: str = Field(..., description="对应输入位置，如 files[2].digest / handovers[0].timestamp")
    expected: Optional[str] = Field(None, description="期望值（复算结果）")
    actual: Optional[str] = Field(None, description="实际值（请求声明值）")
    location2: Optional[str] = Field(
        None, description="冲突涉及的第二个输入位置（如路径碰撞的另一方）"
    )


class ProofStep(BaseModel):
    """逐层包含证明中的一层。"""

    level: int = Field(..., description="该层起始节点所在的 Merkle 层（0=叶子层）")
    sibling_hash: Optional[str] = Field(
        ..., description="兄弟节点哈希；本层为奇数尾节点直接提升时为 null"
    )
    sibling_position: Optional[str] = Field(
        None, description="兄弟相对当前节点的位置：left / right；提升层为 null"
    )
    computed_hash: str = Field(..., description="应用该层后计算出的当前节点哈希")


class FileEvidence(BaseModel):
    """单个文件的复算结果与包含证明。"""

    input_index: int = Field(..., description="该文件在请求 files 数组中的原始下标")
    sorted_index: int = Field(..., description="按 NFC 路径 UTF-8 字节排序后的下标（叶子序号）")
    path_input: str = Field(..., description="请求中提交的原始路径")
    path: str = Field(..., description="NFC 归一后的路径")
    size: int
    digest: str = Field(..., description="归一化后的声明摘要（小写十六进制）")
    leaf_hash: str
    proof: list[ProofStep] = Field(default_factory=list, description="从叶子到根的逐层证明")


class HandoverHash(BaseModel):
    """交接记录及其在哈希链上的链接哈希。"""

    seq: int
    timestamp: str = Field(..., description="归一化后的 UTC 时刻（...Z）")
    timestamp_input: str = Field(..., description="请求中提交的原始时刻字符串")
    handler: str
    note: str
    prev_hash: str
    hash: str = Field(..., description="本条交接记录的链哈希")


class MerkleLevel(BaseModel):
    level: int
    nodes: list[str]


class ChainHead(BaseModel):
    """主链链头。"""

    version: str
    root_hash: str
    length: int = Field(..., description="主链上的包总数")


class PackageComputed(BaseModel):
    """一个包通过全部校验后的确定性复算结果（预检候选 / 入库结果共用）。"""

    version: str
    previous_root_hash: Optional[str] = Field(
        None, description="前序根哈希；创世包为 null"
    )
    predecessor_version: Optional[str] = Field(None, description="前序包版本；创世包为 null")
    file_count: int
    total_bytes: int
    files: list[FileEvidence] = Field(default_factory=list)
    handovers: list[HandoverHash] = Field(default_factory=list)
    handover_head_hash: str
    handover_last_seq: int
    handover_last_timestamp: str
    merkle_levels: list[MerkleLevel]
    merkle_root: str
    root_hash: str = Field(..., description="包根哈希（确定性；同一请求复算结果一致）")
    chain_head: ChainHead = Field(..., description="本包接入后的主链链头")
    committed_at: Optional[str] = Field(
        None, description="入库时间（UTC ISO 8601）；预检时为 null"
    )


class PrecheckResult(BaseModel):
    """预检结果：不写入；accepted=false 时 errors 给出全部结构化原因。"""

    accepted: bool
    errors: list[EvidenceError] = Field(default_factory=list)
    candidate: Optional[PackageComputed] = Field(
        None, description="全部条件通过时的候选包复算结果（含根哈希与逐层证明）"
    )
    current_head: Optional[ChainHead] = Field(None, description="预检时刻的主链链头")


class EvidencePackageSummary(BaseModel):
    version: str
    previous_root_hash: Optional[str]
    predecessor_version: Optional[str]
    root_hash: str
    merkle_root: str
    file_count: int
    total_bytes: int
    handover_last_seq: int
    handover_last_timestamp: str
    committed_at: str


class StoredFile(BaseModel):
    input_index: int
    sorted_index: int
    path: str
    size: int
    digest: str
    leaf_hash: str


class EvidencePackageDetail(EvidencePackageSummary):
    files: list[StoredFile] = Field(default_factory=list)
    handovers: list[HandoverHash] = Field(default_factory=list)
    merkle_levels: list[MerkleLevel] = Field(default_factory=list)
    chain_length: int


class FileRef(BaseModel):
    """版本差异中对一个文件的引用（含其在对应包请求中的输入位置）。"""

    path: str
    size: int
    digest: str
    leaf_hash: str
    input_index: int = Field(..., description="在该版本请求 files 数组中的下标")
    sorted_index: int


class ChangedFile(BaseModel):
    path: str
    left: FileRef
    right: FileRef


class HandoverRange(BaseModel):
    first_seq: Optional[int] = None
    last_seq: Optional[int] = None
    first_timestamp: Optional[str] = None
    last_timestamp: Optional[str] = None
    count: int = 0


class PackageDiff(BaseModel):
    """两个已入库版本的差异。"""

    left_version: str
    left_root_hash: str
    right_version: str
    right_root_hash: str
    files_only_left: list[FileRef] = Field(default_factory=list)
    files_only_right: list[FileRef] = Field(default_factory=list)
    files_changed: list[ChangedFile] = Field(
        default_factory=list, description="路径相同但大小或摘要变化的文件"
    )
    handovers_left: HandoverRange
    handovers_right: HandoverRange
    explanation: str


class InclusionProof(BaseModel):
    """单文件包含证明。"""

    version: str
    path: str = Field(..., description="查询路径（经 NFC 归一后用于匹配）")
    path_input: str
    input_index: int
    sorted_index: int
    canonical_leaf: str = Field(..., description="叶子的 canonical JSON 编码（UTF-8 字符串）")
    leaf_hash: str
    steps: list[ProofStep] = Field(..., description="逐层兄弟节点与每步复算哈希")
    merkle_root: str
    root_hash: str
    verified: bool = Field(..., description="用证明逐层复算得到的根是否等于该包确定性根哈希")


# ----------------------------- 链状态快照（由存储层提供） -----------------------------


@dataclass(frozen=True)
class PackageLink:
    version: str
    root_hash: str
    handover_last_seq: int
    handover_last_timestamp: str


@dataclass(frozen=True)
class ChainSnapshot:
    """校验时刻的主链视图；与事务配合即可消除并发双花/断链。"""

    has_any: bool
    head: Optional[ChainHead]
    predecessor: Optional[PackageLink]
    successor_version: Optional[str]  # 已以指定 prev_root 为前序的包版本（双重后继）
    version_exists: bool


# ----------------------------- 基础编码 -----------------------------


def canonical_json(obj: object) -> bytes:
    """确定性 canonical JSON 编码：键排序、紧凑分隔、UTF-8、不转义非 ASCII。"""

    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_digest(declared: str) -> Optional[str]:
    """归一化摘要：去 ``sha256:`` 前缀、转小写；格式非法返回 None。"""

    d = declared.strip().lower()
    if d.startswith("sha256:"):
        d = d[len("sha256:") :]
    return d if _HEX32_RE.fullmatch(d) else None


def normalize_root_hash(declared: Optional[str]) -> tuple[Optional[str], bool]:
    """归一化前序根哈希。

    返回 ``(hash, ok)``：null/空串归一为 None（创世）；非法格式 ok=False。
    """

    if declared is None or declared.strip() == "":
        return None, True
    h = declared.strip().lower()
    if h.startswith("0x"):
        h = h[2:]
    return (h, True) if _HEX32_RE.fullmatch(h) else (None, False)


def nfc(path: str) -> str:
    return unicodedata.normalize("NFC", path)


def path_is_within_root(path: str) -> bool:
    """归一后的相对路径是否越界（不得逃出包根）。"""

    if path == "":
        return False
    if "\x00" in path or any(ord(c) < 32 for c in path):
        return False
    if path.startswith(("/", "\\")):
        return False  # 绝对路径
    if "\\" in path:
        return False  # 只接受 POSIX 风格分隔，禁止 Windows 分隔符歧义
    if _DRIVE_RE.match(path):
        return False  # Windows 盘符
    parts = path.split("/")
    if any(part in ("", "..") for part in parts):
        return False  # 前导/尾随/重复分隔符，或任何向上跳转的分量
    return True


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """解析带时区的 ISO 8601 时刻到 UTC；naive 或非法返回 None。"""

    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None  # 交接时刻必须显式声明时区，避免歧义
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """UTC datetime → 紧凑 ISO 8601（Z 结尾，保留非零微秒）。"""

    dt = dt.astimezone(timezone.utc)
    if dt.microsecond:
        base = dt.strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0")
        return base + "Z"
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------- Merkle 树 -----------------------------


def leaf_hash(path: str, size: int, digest: str) -> tuple[str, str]:
    """叶子 = H(\\x00 ‖ canonical{path,size,digest})，返回 (哈希, canonical 文本)。"""

    leaf_obj = {"digest": digest, "path": path, "size": size}
    encoded = canonical_json(leaf_obj)
    return sha256_hex(LEAF_PREFIX + encoded), encoded.decode("utf-8")


def node_hash(left: str, right: str) -> str:
    return sha256_hex(NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right))


def build_levels(leaf_hashes: list[str]) -> list[list[str]]:
    """自叶子层向根构造各层；奇数个节点时最后一个直接提升（不重复哈希）。"""

    levels: list[list[str]] = [list(leaf_hashes)]
    while len(levels[-1]) > 1:
        current = levels[-1]
        nxt: list[str] = []
        for i in range(0, len(current), 2):
            if i + 1 < len(current):
                nxt.append(node_hash(current[i], current[i + 1]))
            else:
                nxt.append(current[i])  # 奇数尾节点提升
        levels.append(nxt)
    return levels


def build_proof_steps(levels: list[list[str]], sorted_index: int) -> list[ProofStep]:
    """为叶子 ``sorted_index`` 生成逐层包含证明（提升层给出 null 兄弟）。"""

    steps: list[ProofStep] = []
    cur = sorted_index
    current_hash = levels[0][cur]
    for level in range(len(levels) - 1):
        nodes = levels[level]
        if cur % 2 == 1:
            sibling = nodes[cur - 1]
            current_hash = node_hash(sibling, current_hash)
            steps.append(
                ProofStep(
                    level=level,
                    sibling_hash=sibling,
                    sibling_position="left",
                    computed_hash=current_hash,
                )
            )
        elif cur + 1 < len(nodes):
            sibling = nodes[cur + 1]
            current_hash = node_hash(current_hash, sibling)
            steps.append(
                ProofStep(
                    level=level,
                    sibling_hash=sibling,
                    sibling_position="right",
                    computed_hash=current_hash,
                )
            )
        else:
            # 奇数尾节点：本层无兄弟，直接提升
            steps.append(
                ProofStep(
                    level=level,
                    sibling_hash=None,
                    sibling_position=None,
                    computed_hash=current_hash,
                )
            )
        cur //= 2
    return steps


def verify_proof(leaf_hash_value: str, steps: list[ProofStep], expected_root: str) -> bool:
    """用逐层证明独立复算根哈希并比对。"""

    h = leaf_hash_value
    for step in steps:
        if step.sibling_hash is None:
            continue  # 提升层：哈希不变
        if step.sibling_position == "left":
            h = node_hash(step.sibling_hash, h)
        else:
            h = node_hash(h, step.sibling_hash)
        if h != step.computed_hash:
            return False
    return h == expected_root


# ----------------------------- 交接哈希链 -----------------------------


def handover_hash(seq: int, timestamp: str, handler: str, note: str, prev_hash: str) -> str:
    record = {
        "handler": handler,
        "note": note,
        "prev_hash": prev_hash,
        "seq": seq,
        "timestamp": timestamp,
    }
    return sha256_hex(HANDOVER_PREFIX + canonical_json(record))


def package_root_hash(
    version: str,
    previous_root_hash: Optional[str],
    merkle_root_value: str,
    handover_head: str,
) -> str:
    record = {
        "handover_head_hash": handover_head,
        "merkle_root": merkle_root_value,
        "previous_root_hash": previous_root_hash or ZERO_HASH,
        "version": version,
    }
    return sha256_hex(ROOT_PREFIX + canonical_json(record))


# ----------------------------- 校验与复算 -----------------------------


def _err(
    errors: list[EvidenceError],
    code: str,
    message: str,
    location: str,
    *,
    expected: Optional[str] = None,
    actual: Optional[str] = None,
    location2: Optional[str] = None,
) -> None:
    errors.append(
        EvidenceError(
            code=code,
            message=message,
            location=location,
            expected=expected,
            actual=actual,
            location2=location2,
        )
    )


def configured_limits() -> tuple[int, int]:
    """从环境变量读取包容量上限（EVIDENCE_MAX_FILES / EVIDENCE_MAX_TOTAL_BYTES）。"""

    def _int_env(name: str, default: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    return _int_env("EVIDENCE_MAX_FILES", DEFAULT_MAX_FILES), _int_env(
        "EVIDENCE_MAX_TOTAL_BYTES", DEFAULT_MAX_TOTAL_BYTES
    )


@dataclass
class _FileWork:
    index: int
    raw_path: str
    norm_path: str
    size_declared: int
    digest_input: str
    digest: Optional[str]
    content: Optional[bytes]  # None=缺件或解码失败


def evaluate_package(
    pkg: EvidencePackageIn,
    snap: Optional[ChainSnapshot],
    max_files: int = DEFAULT_MAX_FILES,
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES,
) -> tuple[bool, list[EvidenceError], Optional[PackageComputed]]:
    """对一个提交包做全部校验与确定性复算。

    返回 ``(accepted, errors, candidate)``；candidate 仅在零错误时给出。
    ``snap`` 为 None 时跳过主链相关校验（纯本地复算/测试用）。
    """

    errors: list[EvidenceError] = []

    # ---- 包级字段 ----
    version = pkg.version.strip()
    if not _VERSION_RE.fullmatch(pkg.version) or pkg.version != version:
        _err(
            errors,
            "version_invalid",
            "包版本须为 1~128 位字母数字及 . _ -，且须以字母数字开头、不含空白",
            "version",
            actual=pkg.version,
        )

    prev_root, prev_ok = normalize_root_hash(pkg.previous_root_hash)
    if not prev_ok:
        _err(
            errors,
            "previous_root_invalid",
            "前序根哈希须为 64 位十六进制 SHA-256（可带 0x 前缀），创世包传 null",
            "previous_root_hash",
            actual=str(pkg.previous_root_hash),
        )

    if not pkg.files:
        _err(errors, "package_empty", "证据包至少须包含一个文件", "files")
    if not pkg.handovers:
        _err(
            errors,
            "handovers_empty",
            "证据包至少须包含一条交接记录",
            "handovers",
        )

    # 主链状态检查前置：断链/双重后继/创世冲突/重复版本无论其余字段是否
    # 合法都必须暴露，避免被 422 类错误遮蔽链冲突。
    predecessor: Optional[PackageLink] = None
    if snap is not None:
        if prev_root is None:
            if snap.has_any:
                _err(
                    errors,
                    "genesis_exists",
                    "主链已存在创世包，新包必须提供前序根哈希",
                    "previous_root_hash",
                    expected=snap.head.root_hash if snap.head else None,
                )
        else:
            if not snap.has_any or snap.predecessor is None:
                _err(
                    errors,
                    "unknown_predecessor",
                    "前序根哈希在主链上不存在（断链），只能从创世包接入",
                    "previous_root_hash",
                    actual=prev_root,
                )
            else:
                predecessor = snap.predecessor
                if snap.successor_version is not None:
                    _err(
                        errors,
                        "double_successor",
                        "该前序包已被另一个版本承接，每个版本只能有一个后继",
                        "previous_root_hash",
                        expected=f"后继唯一（现有后继 {snap.successor_version}）",
                        actual=prev_root,
                    )
        if snap.version_exists and version and _VERSION_RE.fullmatch(version):
            _err(
                errors,
                "duplicate_version",
                "该包版本已存在于主链，版本标识全链唯一",
                "version",
                actual=version,
            )

    if len(pkg.files) > max_files:
        _err(
            errors,
            "capacity_exceeded_files",
            f"包内文件数 {len(pkg.files)} 超过容量上限 {max_files}",
            "files",
            expected=str(max_files),
            actual=str(len(pkg.files)),
        )
    declared_total = sum(f.size for f in pkg.files)
    if declared_total > max_total_bytes:
        _err(
            errors,
            "capacity_exceeded_bytes",
            f"声明总字节数 {declared_total} 超过包容量上限 {max_total_bytes} 字节",
            "files",
            expected=str(max_total_bytes),
            actual=str(declared_total),
        )

    # ---- 逐文件：归一化、越界、碰撞、Base64、大小与摘要 ----
    works: list[_FileWork] = []
    for i, f in enumerate(pkg.files):
        loc = f"files[{i}]"
        norm = nfc(f.path)
        if f.path == "":
            _err(errors, "path_empty", "文件路径不能为空", f"{loc}.path")
        elif not path_is_within_root(norm):
            _err(
                errors,
                "path_out_of_bounds",
                "归一化后路径越界：仅允许包根内的相对路径（禁止绝对路径、盘符、..、反斜杠、控制字符）",
                f"{loc}.path",
                actual=norm,
            )

        digest = normalize_digest(f.digest)
        if digest is None:
            _err(
                errors,
                "digest_invalid",
                "摘要格式非法：须为 64 位十六进制 SHA-256（可带 sha256: 前缀）",
                f"{loc}.digest",
                actual=f.digest,
            )

        content: Optional[bytes] = None
        if f.content_base64 is None:
            _err(
                errors,
                "missing_content",
                "列名文件但未提供内容（缺件）",
                f"{loc}.content_base64",
            )
        else:
            try:
                content = base64.b64decode(f.content_base64, validate=True)
            except (binascii.Error, ValueError):
                _err(
                    errors,
                    "invalid_base64",
                    "文件内容不是合法的标准 Base64",
                    f"{loc}.content_base64",
                )
                content = None
            if content is not None:
                if len(content) != f.size:
                    _err(
                        errors,
                        "size_mismatch",
                        "声明大小与实际内容字节数不一致",
                        f"{loc}.size",
                        expected=str(len(content)),
                        actual=str(f.size),
                    )
                computed = sha256_hex(content)
                if digest is not None and computed != digest:
                    _err(
                        errors,
                        "digest_mismatch",
                        "声明摘要与实际内容 SHA-256 不一致",
                        f"{loc}.digest",
                        expected=computed,
                        actual=digest,
                    )

        works.append(
            _FileWork(
                index=i,
                raw_path=f.path,
                norm_path=norm,
                size_declared=f.size,
                digest_input=f.digest,
                digest=digest,
                content=content,
            )
        )

    # NFC 归一后碰撞（含原始路径重复）
    seen_norm: dict[str, int] = {}
    for w in works:
        if w.norm_path == "":
            continue
        if w.norm_path in seen_norm:
            _err(
                errors,
                "path_collision",
                "路径经 Unicode NFC 归一后发生碰撞（不同输入路径指向同一证据路径）",
                f"files[{w.index}].path",
                actual=w.norm_path,
                location2=f"files[{seen_norm[w.norm_path]}].path",
            )
        else:
            seen_norm[w.norm_path] = w.index

    # ---- 交接记录：序号连续、时刻合法且不倒退 ----
    expected_first_seq = 1
    if predecessor is not None:
        expected_first_seq = predecessor.handover_last_seq + 1

    parsed_handovers: list[tuple[HandoverIn, datetime]] = []
    last_epoch: Optional[float] = None
    required_min_ts: Optional[str] = None
    if predecessor is not None:
        pred_dt = _parse_timestamp(predecessor.handover_last_timestamp)
        if pred_dt is not None:
            last_epoch = pred_dt.timestamp()
            required_min_ts = predecessor.handover_last_timestamp

    for i, h in enumerate(pkg.handovers):
        loc = f"handovers[{i}]"
        if not h.handler.strip():
            _err(errors, "handler_empty", "交接经办人不能为空", f"{loc}.handler")
        expected_seq = expected_first_seq + i
        if h.seq != expected_seq:
            _err(
                errors,
                "handover_sequence_gap",
                "交接序号必须连续（按提交顺序逐条加 1，承接前序包末条序号）",
                f"{loc}.seq",
                expected=str(expected_seq),
                actual=str(h.seq),
            )
        dt = _parse_timestamp(h.timestamp)
        if dt is None:
            _err(
                errors,
                "timestamp_invalid",
                "交接时刻须为带时区的合法 ISO 8601 字符串",
                f"{loc}.timestamp",
                actual=h.timestamp,
            )
            continue
        epoch = dt.timestamp()
        if last_epoch is not None and epoch < last_epoch:
            _err(
                errors,
                "handover_time_regression",
                "交接时刻不能倒退（包内单调不减，且不得早于前序包末次交接）",
                f"{loc}.timestamp",
                expected=f">= {required_min_ts}",
                actual=h.timestamp,
            )
        last_epoch = epoch
        required_min_ts = format_utc(dt)
        parsed_handovers.append((h, dt))

    if errors:
        return False, errors, None

    # ---- 全部通过：确定性复算 ----
    assert prev_ok
    ordered = sorted(works, key=lambda w: w.norm_path.encode("utf-8"))
    file_ev: list[FileEvidence] = []
    leaf_hashes: list[str] = []
    total_bytes = 0
    for sorted_index, w in enumerate(ordered):
        assert w.content is not None and w.digest is not None
        total_bytes += len(w.content)
        lh, _ = leaf_hash(w.norm_path, len(w.content), w.digest)
        leaf_hashes.append(lh)
        file_ev.append(
            FileEvidence(
                input_index=w.index,
                sorted_index=sorted_index,
                path_input=w.raw_path,
                path=w.norm_path,
                size=len(w.content),
                digest=w.digest,
                leaf_hash=lh,
            )
        )

    levels_raw = build_levels(leaf_hashes)
    merkle_root_value = levels_raw[-1][0]
    levels = [MerkleLevel(level=i, nodes=nodes) for i, nodes in enumerate(levels_raw)]
    for fe in file_ev:
        fe.proof = build_proof_steps(levels_raw, fe.sorted_index)

    handover_records: list[HandoverHash] = []
    prev_h = ZERO_HASH
    for h, dt in parsed_handovers:
        ts = format_utc(dt)
        note = h.note or ""
        hh = handover_hash(h.seq, ts, h.handler.strip(), note, prev_h)
        handover_records.append(
            HandoverHash(
                seq=h.seq,
                timestamp=ts,
                timestamp_input=h.timestamp,
                handler=h.handler.strip(),
                note=note,
                prev_hash=prev_h,
                hash=hh,
            )
        )
        prev_h = hh
    handover_head = prev_h

    root = package_root_hash(version, prev_root, merkle_root_value, handover_head)

    current_length = snap.head.length if (snap and snap.head) else 0
    chain_head = ChainHead(version=version, root_hash=root, length=current_length + 1)

    last_record = handover_records[-1]
    candidate = PackageComputed(
        version=version,
        previous_root_hash=prev_root,
        predecessor_version=predecessor.version if predecessor else None,
        file_count=len(file_ev),
        total_bytes=total_bytes,
        files=file_ev,
        handovers=handover_records,
        handover_head_hash=handover_head,
        handover_last_seq=last_record.seq,
        handover_last_timestamp=last_record.timestamp,
        merkle_levels=levels,
        merkle_root=merkle_root_value,
        root_hash=root,
        chain_head=chain_head,
    )
    return True, [], candidate
