import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
                CREATE TABLE IF NOT EXISTS merge_registry (
                    merge_id TEXT NOT NULL,
                    source_equipment_id TEXT NOT NULL,
                    retained_equipment_id TEXT NOT NULL,
                    old_asset_no TEXT,
                    old_supervision_code TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(source_equipment_id)
                );
                CREATE INDEX IF NOT EXISTS idx_merge_registry_retained
                    ON merge_registry(retained_equipment_id);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_merge_registry_asset
                    ON merge_registry(old_asset_no);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_merge_registry_supervision
                    ON merge_registry(old_supervision_code)
                    WHERE old_supervision_code IS NOT NULL AND old_supervision_code <> '';
                CREATE TABLE IF NOT EXISTS merge_locks (
                    equipment_id TEXT PRIMARY KEY,
                    merge_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sync_fingerprints (
                    fingerprint TEXT PRIMARY KEY,
                    entity_kind TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        entities = self.list_entities(kind=kind)
        if field == "*":
            return entities
        return [
            entity
            for entity in entities
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    # ------------------------------------------------------------------
    # Device identifier merge support
    # ------------------------------------------------------------------

    @contextmanager
    def write_lock(self):
        """Acquire a database-wide write lock; all merge work happens inside it."""
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except sqlite3.OperationalError as exc:
            connection.rollback()
            raise ConflictError("database is busy with another merge: " + str(exc))
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def conn_get_entity(connection, entity_id):
        row = connection.execute(
            "SELECT * FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        return SQLiteRepository._entity_from_row(row) if row else None

    @staticmethod
    def conn_list_entities(connection, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = connection.execute(
            "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
        ).fetchall()
        return [SQLiteRepository._entity_from_row(row) for row in rows]

    @staticmethod
    def conn_put_entity(connection, entity_id, status, data, expected_version=None, bump=True):
        now = utcnow()
        row = connection.execute(
            "SELECT version FROM entities WHERE id = ?", (entity_id,)
        ).fetchone()
        if not row:
            raise NotFoundError("entity not found: " + entity_id)
        current_version = int(row["version"])
        if expected_version is not None and current_version != int(expected_version):
            raise ConflictError(
                "version conflict: expected %s, found %s"
                % (expected_version, current_version)
            )
        next_version = current_version + 1 if bump else current_version
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ? WHERE id = ?",
            (status, next_version, payload, now, entity_id),
        )
        return SQLiteRepository.conn_get_entity(connection, entity_id)

    @staticmethod
    def conn_insert_entity(connection, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection.execute(
            "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (entity_id, kind, status, payload, actor_id, now, now),
        )
        return SQLiteRepository.conn_get_entity(connection, entity_id)

    @staticmethod
    def conn_upsert_entity(connection, entity_id, kind, status, data, actor_id):
        """Insert or, for a parked record being replayed, replace in place."""
        existing = SQLiteRepository.conn_get_entity(connection, entity_id)
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        if existing:
            connection.execute(
                "UPDATE entities SET kind = ?, status = ?, version = version + 1, "
                "data = ?, updated_at = ? WHERE id = ?",
                (kind, status, payload, now, entity_id),
            )
        else:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return SQLiteRepository.conn_get_entity(connection, entity_id)

    @staticmethod
    def conn_append_audit(connection, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        connection.execute(
            "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                entity_id,
                actor_id,
                actor_role,
                action,
                from_status,
                to_status,
                json.dumps(detail, ensure_ascii=False, sort_keys=True),
                utcnow(),
            ),
        )

    @staticmethod
    def conn_acquire_merge_lock(connection, equipment_id, merge_id, role):
        try:
            connection.execute(
                "INSERT INTO merge_locks(equipment_id, merge_id, role, created_at) "
                "VALUES (?, ?, ?, ?)",
                (equipment_id, merge_id, role, utcnow()),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                "SELECT merge_id, role FROM merge_locks WHERE equipment_id = ?",
                (equipment_id,),
            ).fetchone()
            raise ConflictError(
                "equipment %s is locked by merge %s (%s)"
                % (equipment_id, row["merge_id"] if row else "?", row["role"] if row else "?")
            )

    @staticmethod
    def conn_release_merge_lock(connection, equipment_id):
        connection.execute("DELETE FROM merge_locks WHERE equipment_id = ?", (equipment_id,))

    @staticmethod
    def conn_register_merge(connection, merge_id, source, retained):
        try:
            connection.execute(
                "INSERT INTO merge_registry(merge_id, source_equipment_id, retained_equipment_id, "
                "old_asset_no, old_supervision_code, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    merge_id,
                    source["id"],
                    retained["id"],
                    str(source["data"].get("asset_no", "")).strip() or None,
                    str(source["data"].get("supervision_code", "")).strip() or None,
                    utcnow(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("source equipment already registered in a merge: " + str(exc))

    @staticmethod
    def conn_find_registry_by_code(connection, field, value):
        column = "old_asset_no" if field == "asset_no" else "old_supervision_code"
        row = connection.execute(
            "SELECT * FROM merge_registry WHERE " + column + " = ?", (value,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def conn_find_registry_by_source(connection, source_equipment_id):
        row = connection.execute(
            "SELECT * FROM merge_registry WHERE source_equipment_id = ?",
            (source_equipment_id,),
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def conn_save_fingerprint(connection, fingerprint, entity_kind, entity_id):
        # Parked records can be superseded by the entity produced on replay,
        # so the mapping may move from offline_record -> alarm/rescue_job.
        connection.execute(
            "INSERT INTO sync_fingerprints(fingerprint, entity_kind, entity_id, created_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(fingerprint) DO UPDATE SET "
            "entity_kind = excluded.entity_kind, entity_id = excluded.entity_id",
            (fingerprint, entity_kind, entity_id, utcnow()),
        )
        return entity_id

    def get_fingerprint(self, fingerprint):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id, entity_kind FROM sync_fingerprints WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return dict(row) if row else None

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
