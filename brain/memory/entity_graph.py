"""
Entity graph -- GBrain-inspired "grows with you" memory layer.

ROADMAP.md, Phase 3: "Entity graph & synthesis (GBrain-inspired) -- the
actual 'grows with you' mechanism Claim A explicitly deferred." T4 answers
"what's my AoPS block time" (one key, one value, latest wins). This answers
"who is Priya" or "what do you know about the robotics club" by aggregating
every mention and relation seen across turns into one place, instead of the
last mention silently overwriting the ones before it the way a T4 key would.

v1 scope, deliberately: storage (entities + relations, both keyed so repeat
mentions accumulate instead of duplicating) and a deterministic text
synthesis of what's on file for one entity. No LLM call lives in this
module -- extraction is the caller's job (see conversation.py's post-turn
curation pass, which now has entity_note/entity_relate alongside
remember/forget). Left for a follow-up, not built here: proactive surfacing
through the heartbeat, multi-hop graph traversal, and an LLM-written prose
synthesis instead of this module's plain-text one -- see PROGRESS.md.

Self-contained SQLite store (own file, own connection), same pattern as
LocalDB and T5's archive.db -- works with no Obsidian vault mounted, which
matters for a cloud sandbox that has no vault access at all.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

DB_PATH = Path(__file__).parent.parent / "data" / "entity_graph.db"

# Cap on stored notes per entity -- a long-lived entity mentioned weekly for
# a year shouldn't grow its synthesis without bound; the most recent notes
# are the most likely to still be relevant.
_MAX_NOTES_PER_ENTITY = 20


class EntityGraph:
    """Entities + relations between them, both keyed for idempotent upserts."""

    def __init__(self, db_path: Optional[Path] = None) -> None:
        self.db_path = db_path or DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    def _init_db(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL UNIQUE,
                entity_type TEXT DEFAULT 'unknown',
                notes_json TEXT DEFAULT '[]',
                mention_count INTEGER DEFAULT 0,
                first_seen TEXT DEFAULT (datetime('now')),
                last_seen TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type);

            CREATE TABLE IF NOT EXISTS entity_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_a TEXT NOT NULL,
                relation TEXT NOT NULL,
                entity_b TEXT NOT NULL,
                relation_key TEXT NOT NULL UNIQUE,
                mention_count INTEGER DEFAULT 1,
                first_seen TEXT DEFAULT (datetime('now')),
                last_seen TEXT DEFAULT (datetime('now'))
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Key normalization
    # ------------------------------------------------------------------

    @staticmethod
    def _norm(name: str) -> str:
        """Case/whitespace-insensitive dedupe key. Strips '|' since relation
        keys join name/relation/name on that character -- a name containing
        one would otherwise be ambiguous with a segment boundary."""
        return " ".join((name or "").replace("|", " ").strip().lower().split())

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------

    def upsert_entity(
        self, name: str, entity_type: str = "unknown", note: Optional[str] = None
    ) -> int:
        """Create the entity on first mention, or bump its mention count and
        append a note on a repeat mention. entity_type only overwrites a
        prior 'unknown' -- the first real classification sticks."""
        name = (name or "").strip()
        if not name:
            raise ValueError("name is required")
        key = self._norm(name)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entities WHERE name_key = ?", (key,)
            ).fetchone()
            if row is None:
                notes = [note] if note else []
                cur = self._conn.execute(
                    "INSERT INTO entities "
                    "(name, name_key, entity_type, notes_json, mention_count, "
                    " first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))",
                    (name, key, entity_type or "unknown", json.dumps(notes)),
                )
                self._conn.commit()
                return cur.lastrowid

            notes = json.loads(row["notes_json"] or "[]")
            if note and note not in notes:
                notes.append(note)
                notes = notes[-_MAX_NOTES_PER_ENTITY:]
            new_type = row["entity_type"]
            if entity_type and entity_type != "unknown":
                new_type = entity_type
            self._conn.execute(
                "UPDATE entities SET entity_type = ?, notes_json = ?, "
                "mention_count = mention_count + 1, last_seen = datetime('now') "
                "WHERE id = ?",
                (new_type, json.dumps(notes), row["id"]),
            )
            self._conn.commit()
            return row["id"]

    def add_relation(self, entity_a: str, relation: str, entity_b: str) -> int:
        """Record a relation between two entities, creating either side that
        doesn't exist yet so a relation always resolves back to a real
        entity. Repeat mentions of the same (a, relation, b) triple bump a
        count rather than duplicating the row."""
        entity_a = (entity_a or "").strip()
        relation = (relation or "").strip()
        entity_b = (entity_b or "").strip()
        if not entity_a or not relation or not entity_b:
            raise ValueError("entity_a, relation, and entity_b are all required")

        self.upsert_entity(entity_a)
        self.upsert_entity(entity_b)

        rkey = f"{self._norm(entity_a)}|{self._norm(relation)}|{self._norm(entity_b)}"
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM entity_relations WHERE relation_key = ?", (rkey,)
            ).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO entity_relations "
                    "(entity_a, relation, entity_b, relation_key, mention_count, "
                    " first_seen, last_seen) "
                    "VALUES (?, ?, ?, ?, 1, datetime('now'), datetime('now'))",
                    (entity_a, relation, entity_b, rkey),
                )
                self._conn.commit()
                return cur.lastrowid
            self._conn.execute(
                "UPDATE entity_relations SET mention_count = mention_count + 1, "
                "last_seen = datetime('now') WHERE id = ?",
                (row["id"],),
            )
            self._conn.commit()
            return row["id"]

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def get_entity(self, name: str) -> Optional[Dict[str, Any]]:
        key = self._norm(name)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entities WHERE name_key = ?", (key,)
            ).fetchone()
        return self._row_to_entity(row) if row is not None else None

    def list_entities(
        self, entity_type: Optional[str] = None, limit: int = 50
    ) -> List[Dict[str, Any]]:
        with self._lock:
            if entity_type:
                rows = self._conn.execute(
                    "SELECT * FROM entities WHERE entity_type = ? "
                    "ORDER BY last_seen DESC LIMIT ?",
                    (entity_type, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM entities ORDER BY last_seen DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def get_relations_for(self, name: str) -> List[Dict[str, Any]]:
        """Every relation with this entity on either side (one hop)."""
        key = self._norm(name)
        if not key:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM entity_relations "
                "WHERE relation_key LIKE ? OR relation_key LIKE ? "
                "ORDER BY last_seen DESC",
                (f"{key}|%", f"%|{key}"),
            ).fetchall()
        return [
            {
                "entity_a": r["entity_a"],
                "relation": r["relation"],
                "entity_b": r["entity_b"],
                "mention_count": r["mention_count"],
                "first_seen": r["first_seen"],
                "last_seen": r["last_seen"],
            }
            for r in rows
        ]

    def synthesize(self, name: str) -> str:
        """Plain-text synthesis of everything on file for one entity --
        deterministic on purpose (v1 scope note above), so it's testable
        without a live LLM and safe to call from a read-only tool."""
        entity = self.get_entity(name)
        if entity is None:
            return f"No entity graph data for '{name}'."

        lines = [
            f"{entity['name']} ({entity['entity_type']}) -- mentioned "
            f"{entity['mention_count']}x, first seen {entity['first_seen']}, "
            f"last seen {entity['last_seen']}."
        ]
        if entity["notes"]:
            lines.append("Notes:")
            lines.extend(f"  - {n}" for n in entity["notes"])

        relations = self.get_relations_for(name)
        if relations:
            lines.append("Relations:")
            lines.extend(
                f"  - {r['entity_a']} {r['relation']} {r['entity_b']}"
                for r in relations
            )
        return "\n".join(lines)

    @staticmethod
    def _row_to_entity(row: sqlite3.Row) -> Dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "entity_type": row["entity_type"],
            "notes": json.loads(row["notes_json"] or "[]"),
            "mention_count": row["mention_count"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
        }


_graph_instance: Optional[EntityGraph] = None


def get_entity_graph() -> EntityGraph:
    global _graph_instance
    if _graph_instance is None:
        _graph_instance = EntityGraph()
    return _graph_instance
