"""已编译场景的进程内缓存（单 worker 部署即权威缓存）。"""
from __future__ import annotations

import threading
from typing import Iterable

from .db import get_conn
from .models import Scenario
from .scenario import parse_scenario


class ScenarioStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._scenarios: dict[str, Scenario] = {}

    def load_all_from_db(self) -> int:
        conn = get_conn()
        rows = conn.execute("SELECT name, definition FROM scenarios").fetchall()
        with self._lock:
            self._scenarios.clear()
            for row in rows:
                scenario = parse_scenario(row["definition"], name_override=row["name"])
                self._scenarios[scenario.name] = scenario
        return len(rows)

    def upsert(self, name: str, definition: str) -> Scenario:
        scenario = parse_scenario(definition, name_override=name)
        conn = get_conn()
        conn.execute(
            """
            INSERT INTO scenarios(name, definition, created_at, updated_at)
            VALUES (?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                definition = excluded.definition,
                updated_at = datetime('now')
            """,
            (name, definition),
        )
        conn.commit()
        with self._lock:
            self._scenarios[name] = scenario
        return scenario

    def delete(self, name: str) -> bool:
        conn = get_conn()
        cur = conn.execute("DELETE FROM scenarios WHERE name = ?", (name,))
        conn.commit()
        with self._lock:
            existed = self._scenarios.pop(name, None) is not None
        return cur.rowcount > 0 or existed

    def get(self, name: str) -> Scenario | None:
        with self._lock:
            return self._scenarios.get(name)

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._scenarios)

    def all(self) -> Iterable[Scenario]:
        with self._lock:
            return list(self._scenarios.values())


store = ScenarioStore()
