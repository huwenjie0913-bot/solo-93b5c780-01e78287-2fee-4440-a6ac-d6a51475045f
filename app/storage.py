"""SQLite 场景版本库：场景可复查、可派生新版本。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from typing import Optional

from .models import Scenario, ScenarioSummary, ScenarioVersion

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scenarios (
    id          TEXT NOT NULL,
    version     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    parent_version INTEGER,
    note        TEXT,
    payload     TEXT NOT NULL,
    PRIMARY KEY (id, version)
);
"""


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def create(
        self, scenario: Scenario, scenario_id: Optional[str] = None, note: Optional[str] = None
    ) -> ScenarioVersion:
        sid = scenario_id or uuid.uuid4().hex[:12]
        return self._insert(sid, 1, scenario, None, note)

    def add_version(
        self,
        scenario_id: str,
        scenario: Scenario,
        parent_version: Optional[int] = None,
        note: Optional[str] = None,
    ) -> ScenarioVersion:
        latest = self.latest_version(scenario_id)
        if latest is None:
            raise KeyError(f"场景 {scenario_id} 不存在，请先创建")
        pv = parent_version if parent_version is not None else latest
        if not self.exists_version(scenario_id, pv):
            raise KeyError(f"父版本 {pv} 不存在")
        return self._insert(scenario_id, latest + 1, scenario, pv, note)

    def _insert(
        self,
        sid: str,
        version: int,
        scenario: Scenario,
        parent_version: Optional[int],
        note: Optional[str],
    ) -> ScenarioVersion:
        created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        payload = scenario.model_dump_json()
        self._conn.execute(
            "INSERT INTO scenarios (id, version, name, created_at, parent_version, note, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (sid, version, scenario.name, created, parent_version, note, payload),
        )
        self._conn.commit()
        return ScenarioVersion(
            scenario_id=sid,
            version=version,
            name=scenario.name,
            created_at=created,
            parent_version=parent_version,
            note=note,
            payload=scenario,
        )

    def exists_version(self, scenario_id: str, version: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM scenarios WHERE id=? AND version=?", (scenario_id, version)
        ).fetchone()
        return row is not None

    def latest_version(self, scenario_id: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT MAX(version) AS v FROM scenarios WHERE id=?", (scenario_id,)
        ).fetchone()
        return row["v"] if row and row["v"] is not None else None

    def get(self, scenario_id: str, version: Optional[int] = None) -> ScenarioVersion:
        if version is None:
            version = self.latest_version(scenario_id)
            if version is None:
                raise KeyError(f"场景 {scenario_id} 不存在")
        row = self._conn.execute(
            "SELECT * FROM scenarios WHERE id=? AND version=?", (scenario_id, version)
        ).fetchone()
        if row is None:
            raise KeyError(f"场景 {scenario_id} 版本 {version} 不存在")
        return ScenarioVersion(
            scenario_id=row["id"],
            version=row["version"],
            name=row["name"],
            created_at=row["created_at"],
            parent_version=row["parent_version"],
            note=row["note"],
            payload=Scenario(**json.loads(row["payload"])),
        )

    def list_versions(self, scenario_id: str) -> list[ScenarioSummary]:
        rows = self._conn.execute(
            "SELECT id, version, name, created_at, parent_version, note"
            " FROM scenarios WHERE id=? ORDER BY version",
            (scenario_id,),
        ).fetchall()
        return [
            ScenarioSummary(
                scenario_id=r["id"],
                version=r["version"],
                name=r["name"],
                created_at=r["created_at"],
                parent_version=r["parent_version"],
                note=r["note"],
            )
            for r in rows
        ]

    def list_scenarios(self) -> list[ScenarioSummary]:
        rows = self._conn.execute(
            "SELECT s.* FROM scenarios s"
            " JOIN (SELECT id, MAX(version) mv FROM scenarios GROUP BY id) m"
            " ON s.id=m.id AND s.version=m.mv ORDER BY s.created_at DESC"
        ).fetchall()
        return [
            ScenarioSummary(
                scenario_id=r["id"],
                version=r["version"],
                name=r["name"],
                created_at=r["created_at"],
                parent_version=r["parent_version"],
                note=r["note"],
            )
            for r in rows
        ]
