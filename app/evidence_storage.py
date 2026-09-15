"""证据保全包的 SQLite 持久化（与场景库同库、独立表，仅追加的单主链）。

链一致性靠写事务保证：入库在 ``BEGIN IMMEDIATE`` 锁内重建链快照、重新执行
全部纯逻辑校验，再一次性写入包记录与逐文件记录；并发提交若造成双重后继或
版本重复，后到者在锁内复算时必然发现冲突，整个事务回滚、绝不写入。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional

from .evidence import (
    ChainHead,
    ChainSnapshot,
    EvidencePackageDetail,
    EvidencePackageSummary,
    PackageComputed,
    PackageLink,
    StoredFile,
    HandoverHash,
    HandoverRange,
    MerkleLevel,
    FileRef,
    ChangedFile,
    PackageDiff,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS evidence_packages (
    version TEXT PRIMARY KEY,
    previous_root_hash TEXT,
    predecessor_version TEXT,
    root_hash TEXT NOT NULL UNIQUE,
    merkle_root TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    total_bytes INTEGER NOT NULL,
    handover_last_seq INTEGER NOT NULL,
    handover_last_timestamp TEXT NOT NULL,
    chain_position INTEGER NOT NULL UNIQUE,
    committed_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evidence_files (
    package_version TEXT NOT NULL,
    input_index INTEGER NOT NULL,
    sorted_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    digest TEXT NOT NULL,
    leaf_hash TEXT NOT NULL,
    PRIMARY KEY (package_version, path)
);
CREATE INDEX IF NOT EXISTS idx_evidence_files_version ON evidence_files(package_version);
"""


class EvidenceVersionNotFound(KeyError):
    """指定版本/文件在证据链上不存在（映射为 HTTP 404）。"""


class EvidenceStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        # isolation_level=None：显式事务（BEGIN IMMEDIATE），避免隐式事务歧义
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def write_lock(self) -> Iterator[None]:
        """获取立即写锁：锁内重建的链快照对其他写者是稳定一致的。"""

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ----------------------------- 链视图 -----------------------------

    def snapshot(self, version: str, previous_root_hash: Optional[str]) -> ChainSnapshot:
        head_row = self._conn.execute(
            "SELECT * FROM evidence_packages ORDER BY chain_position DESC LIMIT 1"
        ).fetchone()
        has_any = head_row is not None
        head = (
            ChainHead(
                version=head_row["version"],
                root_hash=head_row["root_hash"],
                length=self._conn.execute(
                    "SELECT COUNT(*) c FROM evidence_packages"
                ).fetchone()["c"],
            )
            if head_row
            else None
        )
        predecessor = None
        successor_version = None
        if previous_root_hash is not None:
            prow = self._conn.execute(
                "SELECT * FROM evidence_packages WHERE root_hash=?",
                (previous_root_hash,),
            ).fetchone()
            if prow is not None:
                predecessor = PackageLink(
                    version=prow["version"],
                    root_hash=prow["root_hash"],
                    handover_last_seq=prow["handover_last_seq"],
                    handover_last_timestamp=prow["handover_last_timestamp"],
                )
            srow = self._conn.execute(
                "SELECT version FROM evidence_packages WHERE previous_root_hash=?",
                (previous_root_hash,),
            ).fetchone()
            if srow is not None:
                successor_version = srow["version"]
        version_exists = (
            self._conn.execute(
                "SELECT 1 FROM evidence_packages WHERE version=?", (version,)
            ).fetchone()
            is not None
        )
        return ChainSnapshot(
            has_any=has_any,
            head=head,
            predecessor=predecessor,
            successor_version=successor_version,
            version_exists=version_exists,
        )

    def head(self) -> Optional[ChainHead]:
        row = self._conn.execute(
            "SELECT version, root_hash FROM evidence_packages"
            " ORDER BY chain_position DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        length = self._conn.execute(
            "SELECT COUNT(*) c FROM evidence_packages"
        ).fetchone()["c"]
        return ChainHead(version=row["version"], root_hash=row["root_hash"], length=length)

    # ----------------------------- 写入 -----------------------------

    def insert(self, candidate: PackageComputed) -> None:
        """在 write_lock() 内调用：写入包记录与逐文件记录。"""

        committed_at = candidate.committed_at or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
        candidate.committed_at = committed_at
        position_row = self._conn.execute(
            "SELECT COALESCE(MAX(chain_position), 0) + 1 AS p FROM evidence_packages"
        ).fetchone()
        self._conn.execute(
            "INSERT INTO evidence_packages (version, previous_root_hash, predecessor_version,"
            " root_hash, merkle_root, file_count, total_bytes, handover_last_seq,"
            " handover_last_timestamp, chain_position, committed_at, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                candidate.version,
                candidate.previous_root_hash,
                candidate.predecessor_version,
                candidate.root_hash,
                candidate.merkle_root,
                candidate.file_count,
                candidate.total_bytes,
                candidate.handover_last_seq,
                candidate.handover_last_timestamp,
                position_row["p"],
                committed_at,
                candidate.model_dump_json(),
            ),
        )
        self._conn.executemany(
            "INSERT INTO evidence_files (package_version, input_index, sorted_index,"
            " path, size, digest, leaf_hash) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    candidate.version,
                    f.input_index,
                    f.sorted_index,
                    f.path,
                    f.size,
                    f.digest,
                    f.leaf_hash,
                )
                for f in candidate.files
            ],
        )

    # ----------------------------- 读取 -----------------------------

    def list_packages(self) -> list[EvidencePackageSummary]:
        rows = self._conn.execute(
            "SELECT payload FROM evidence_packages ORDER BY chain_position"
        ).fetchall()
        return [self._summary(json.loads(r["payload"])) for r in rows]

    def _detail_row(self, version: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM evidence_packages WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise EvidenceVersionNotFound(f"证据包版本 {version} 不存在")
        return row

    def get_detail(self, version: str) -> EvidencePackageDetail:
        row = self._detail_row(version)
        p = json.loads(row["payload"])
        length = self._conn.execute(
            "SELECT chain_position FROM evidence_packages WHERE version=?", (version,)
        ).fetchone()["chain_position"]
        return EvidencePackageDetail(
            version=p["version"],
            previous_root_hash=p["previous_root_hash"],
            predecessor_version=p["predecessor_version"],
            root_hash=p["root_hash"],
            merkle_root=p["merkle_root"],
            file_count=p["file_count"],
            total_bytes=p["total_bytes"],
            handover_last_seq=p["handover_last_seq"],
            handover_last_timestamp=p["handover_last_timestamp"],
            committed_at=p["committed_at"],
            files=[StoredFile(**f) for f in p["files"]],
            handovers=[HandoverHash(**h) for h in p["handovers"]],
            merkle_levels=[MerkleLevel(**m) for m in p["merkle_levels"]],
            chain_length=length,
        )

    @staticmethod
    def _summary(p: dict) -> EvidencePackageSummary:
        return EvidencePackageSummary(
            version=p["version"],
            previous_root_hash=p["previous_root_hash"],
            predecessor_version=p["predecessor_version"],
            root_hash=p["root_hash"],
            merkle_root=p["merkle_root"],
            file_count=p["file_count"],
            total_bytes=p["total_bytes"],
            handover_last_seq=p["handover_last_seq"],
            handover_last_timestamp=p["handover_last_timestamp"],
            committed_at=p["committed_at"],
        )

    def get_file_row(self, version: str, norm_path: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM evidence_files WHERE package_version=? AND path=?",
            (version, norm_path),
        ).fetchone()
        if row is None:
            raise EvidenceVersionNotFound(
                f"证据包 {version} 中不存在文件 {norm_path!r}"
            )
        return row

    # ----------------------------- 版本对比 -----------------------------

    def diff(self, left_version: str, right_version: str) -> PackageDiff:
        left = self.get_detail(left_version)
        right = self.get_detail(right_version)
        lmap = {f.path: f for f in left.files}
        rmap = {f.path: f for f in right.files}

        def ref(f: StoredFile) -> FileRef:
            return FileRef(
                path=f.path,
                size=f.size,
                digest=f.digest,
                leaf_hash=f.leaf_hash,
                input_index=f.input_index,
                sorted_index=f.sorted_index,
            )

        only_left = [ref(lmap[p]) for p in sorted(set(lmap) - set(rmap))]
        only_right = [ref(rmap[p]) for p in sorted(set(rmap) - set(lmap))]
        changed: list[ChangedFile] = []
        for p in sorted(set(lmap) & set(rmap)):
            lf, rf = lmap[p], rmap[p]
            if lf.size != rf.size or lf.digest != rf.digest:
                changed.append(ChangedFile(path=p, left=ref(lf), right=ref(rf)))

        def handover_range(detail: EvidencePackageDetail) -> HandoverRange:
            if not detail.handovers:
                return HandoverRange()
            return HandoverRange(
                first_seq=detail.handovers[0].seq,
                last_seq=detail.handovers[-1].seq,
                first_timestamp=detail.handovers[0].timestamp,
                last_timestamp=detail.handovers[-1].timestamp,
                count=len(detail.handovers),
            )

        hl, hr = handover_range(left), handover_range(right)
        explanation = (
            f"{left_version}（根 {left.root_hash[:12]}…）→ {right_version}"
            f"（根 {right.root_hash[:12]}…）：仅左 {len(only_left)} 个、"
            f"仅右 {len(only_right)} 个、内容变化 {len(changed)} 个；"
            f"交接记录 {hl.count} → {hr.count} 条（末序号 {hl.last_seq} → {hr.last_seq}）"
        )
        return PackageDiff(
            left_version=left.version,
            left_root_hash=left.root_hash,
            right_version=right.version,
            right_root_hash=right.root_hash,
            files_only_left=only_left,
            files_only_right=only_right,
            files_changed=changed,
            handovers_left=hl,
            handovers_right=hr,
            explanation=explanation,
        )
