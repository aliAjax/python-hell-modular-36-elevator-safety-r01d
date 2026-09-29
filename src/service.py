import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from .audit import AuditTrail
from .domain import (
    ConflictError,
    InvalidTransition,
    MergeBlockedError,
    NotFoundError,
    ValidationError,
)
from .repository import SQLiteRepository
from .rules import (
    MIGRATING_KINDS,
    merge_guard_reasons,
    require_role,
    validate_device_merge,
)
from .rules import RuleEngine


MERGE_ROLE = ("admin",)
ACTIVE_ALARM_STATUSES = ("received", "dispatched", "resolved")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    @staticmethod
    def _now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind, payload)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if self.rules.normalize_kind(entity["kind"]) == "device_merge":
            return self._device_merge_action(actor, entity_id, action, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    # ------------------------------------------------------------------
    # Device identifier merge
    # ------------------------------------------------------------------

    def start_device_merge(self, actor, data, idempotency_key=None):
        """Open a merge job, suspend both devices and take database-level locks.

        Either an explicit ``merge_id`` or an idempotency key makes a retried
        submission safe: the previously created (or already completed) job is
        returned unchanged.
        """
        require_role(actor, MERGE_ROLE)
        payload = dict(data or {})
        source_id = str(payload.get("source_equipment_id", "")).strip()
        retained_id = str(payload.get("retained_equipment_id", "")).strip()

        # Shape/role checks need no transaction and must not create half work.
        validate_device_merge(payload, self._lookup)

        merge_id = str(payload.get("merge_id", "")).strip()
        if not merge_id and idempotency_key:
            merge_id = "merge-" + hashlib.sha256(
                ("start\0" + actor.user_id + "\0" + idempotency_key).encode("utf-8")
            ).hexdigest()[:24]
        if not merge_id:
            merge_id = "merge-" + uuid4().hex[:24]
        reason_text = str(payload.get("reason", "")).strip()

        existing = self.repository.get_entity(merge_id)
        if existing:
            self._assert_same_merge(existing, source_id, retained_id)
            return existing

        started_at = self._now()
        with self.repository.write_lock() as conn:
            job = self.repository.conn_get_entity(conn, merge_id)
            if job:
                self._assert_same_merge(job, source_id, retained_id)
                return job

            # The source may already have finished merging in a previous batch;
            # retrying with the same original payload converges onto that job.
            registry_row = self.repository.conn_find_registry_by_source(conn, source_id)
            if registry_row:
                if registry_row["retained_equipment_id"] != retained_id:
                    raise ConflictError(
                        "source %s already merged into %s"
                        % (source_id, registry_row["retained_equipment_id"])
                    )
                completed = self.repository.conn_get_entity(conn, registry_row["merge_id"])
                if completed:
                    return completed
                raise ConflictError("source already merged: " + source_id)

            source = self.repository.conn_get_entity(conn, source_id)
            retained = self.repository.conn_get_entity(conn, retained_id)
            if not source or source["status"] == "merged":
                raise ConflictError("source equipment unavailable for merge: " + source_id)
            if not retained or retained["status"] == "merged":
                raise ValidationError("retained equipment has been merged away: " + retained_id)

            # Hard concurrency guard: only one merge can hold either device.
            self.repository.conn_acquire_merge_lock(conn, source_id, merge_id, "source")
            self.repository.conn_acquire_merge_lock(conn, retained_id, merge_id, "retained")

            source_before = source["status"]
            retained_before = retained["status"]
            source, retained = self._suspend_for_merge(conn, actor, source, retained)

            # Active-alarm baseline: anything outside this set while the merge
            # window is open halts completion.
            baseline_alarm_ids = self._active_alarm_ids(conn, source_id, retained_id)

            job_data = {
                "source_equipment_id": source_id,
                "retained_equipment_id": retained_id,
                "started_at": started_at,
                "reason": reason_text,
                "source_status_before": source_before,
                "retained_status_before": retained_before,
                "baseline_alarm_ids": baseline_alarm_ids,
                "migrated": [],
                "block_reasons": [],
            }
            job = self.repository.conn_insert_entity(
                conn, merge_id, "device_merge", "in_progress", job_data, actor.user_id
            )
            self.repository.conn_append_audit(
                conn, merge_id, actor.user_id, actor.role, "create", None, "in_progress",
                {"source_equipment_id": source_id, "retained_equipment_id": retained_id,
                 "baseline_alarm_ids": baseline_alarm_ids},
            )
        return job

    @staticmethod
    def _active_alarm_ids(conn, *equipment_ids):
        ids = set()
        for alarm in SQLiteRepository.conn_list_entities(conn, kind="alarm"):
            if (
                alarm["data"].get("equipment_id") in equipment_ids
                and alarm["status"] in ACTIVE_ALARM_STATUSES
            ):
                ids.add(alarm["id"])
        return sorted(ids)

    def _device_merge_action(self, actor, entity_id, action, data, expected_version):
        require_role(actor, MERGE_ROLE)
        if action == "retry":
            return self._retry_device_merge(actor, entity_id, expected_version)
        if action == "complete":
            return self._finish_device_merge(actor, entity_id, expected_version)
        # ``block`` is set only by the system when a guard trips; callers may
        # only observe it or retry/complete.
        raise InvalidTransition("device_merge supports retry/complete, not: " + str(action))

    def _retry_device_merge(self, actor, merge_id, expected_version):
        with self.repository.write_lock() as conn:
            job = self.repository.conn_get_entity(conn, merge_id)
            if not job:
                raise NotFoundError("entity not found: " + merge_id)
            if job["status"] == "completed":
                return job
            next_status, _patch = self.rules.validate_transition(
                actor, job, "retry", {}, self._conn_lookup(conn)
            )
            source_id = job["data"]["source_equipment_id"]
            retained_id = job["data"]["retained_equipment_id"]
            source = self.repository.conn_get_entity(conn, source_id)
            retained = self.repository.conn_get_entity(conn, retained_id)
            if not source or not retained or source["status"] == "merged":
                raise ConflictError("merge participants are no longer valid")

            data = dict(job["data"])
            data["block_reasons"] = []
            data["migrated"] = []
            # The alarm baseline is fixed at merge start and is never
            # refreshed: a closed alarm drops out of the active set on its
            # own, while any later alarm keeps halting the merge.
            job = self.repository.conn_put_entity(
                conn, merge_id, next_status, data, expected_version=expected_version
            )
            # Re-suspend participants (they may have been touched while
            # blocked) and refresh the merge-in-progress markers.
            self._suspend_for_merge(conn, actor, source, retained)
            self.repository.conn_append_audit(
                conn, merge_id, actor.user_id, actor.role, "retry", "blocked", "in_progress",
                {"baseline_alarm_ids": data.get("baseline_alarm_ids", [])},
            )
        return job

    def _finish_device_merge(self, actor, merge_id, expected_version):
        """Attempt finalization; persists ``blocked`` with evidence on guards."""
        blocked_reasons = None
        with self.repository.write_lock() as conn:
            job = self.repository.conn_get_entity(conn, merge_id)
            if not job:
                raise NotFoundError("entity not found: " + merge_id)
            if job["status"] == "completed":
                return job
            if job["status"] not in ("in_progress", "blocked"):
                raise ConflictError("merge job is not active: " + job["status"])
            if expected_version is not None and job["version"] != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, job["version"])
                )

            source_id = job["data"]["source_equipment_id"]
            retained_id = job["data"]["retained_equipment_id"]
            source = self.repository.conn_get_entity(conn, source_id)
            retained = self.repository.conn_get_entity(conn, retained_id)
            if not source or not retained:
                raise ConflictError("merge participants are missing")

            if job["status"] == "blocked":
                # Carry the original request: re-arm and re-run every guard
                # against the fixed baseline captured when the merge opened.
                data = dict(job["data"])
                data["block_reasons"] = []
                data["migrated"] = []
                job = self.repository.conn_put_entity(conn, merge_id, "in_progress", data)
                self.repository.conn_append_audit(
                    conn, merge_id, actor.user_id, actor.role, "retry",
                    "blocked", "in_progress", {"trigger": "complete_with_original_data"},
                )
                self._suspend_for_merge(conn, actor, source, retained)
                source = self.repository.conn_get_entity(conn, source_id)
                retained = self.repository.conn_get_entity(conn, retained_id)

            baseline = set(job["data"].get("baseline_alarm_ids", []))

            # Pre-migration guards: identifier ownership and new alarms.
            reasons = merge_guard_reasons(
                source,
                retained,
                self.repository.conn_list_entities(conn, kind="equipment"),
                self.repository.conn_list_entities(conn, kind="alarm"),
                baseline,
            )
            reasons.extend(self._duplicate_active_alarm_reasons(conn, source_id, retained_id))
            if reasons:
                job = self._block_merge(conn, actor, job, reasons, "pre_migration")
                blocked_reasons = reasons
            else:
                # History migration: repoint records, originals keep provenance.
                migrated = self._migrate_history(conn, actor, job, source_id, retained_id)

                # Post-migration re-check inside the same write lock: nothing
                # can be inserted between this read and the final commit.
                late_reasons = merge_guard_reasons(
                    source,
                    retained,
                    self.repository.conn_list_entities(conn, kind="equipment"),
                    self.repository.conn_list_entities(conn, kind="alarm"),
                    baseline,
                )
                if late_reasons:
                    job = self._block_merge(
                        conn, actor, job, late_reasons, "post_migration", migrated=migrated
                    )
                    blocked_reasons = late_reasons
                else:
                    # Publish the merge: retire source, release retained, alias.
                    self._retire_source(conn, actor, source, retained, merge_id)
                    self._release_retained(conn, actor, retained, source, job)
                    self.repository.conn_register_merge(conn, merge_id, source, retained)
                    self.repository.conn_release_merge_lock(conn, source_id)
                    self.repository.conn_release_merge_lock(conn, retained_id)

                    complete_data = dict(job["data"])
                    complete_data["block_reasons"] = []
                    complete_data["migrated"] = migrated
                    complete_data["completed_at"] = self._now()
                    job = self.repository.conn_put_entity(
                        conn, merge_id, "completed", complete_data
                    )
                    self.repository.conn_append_audit(
                        conn, merge_id, actor.user_id, actor.role, "complete",
                        "in_progress", "completed", {"migrated": migrated},
                    )
            # The context manager commits the blocked/completed state here.
        # Raise only after the blocked state is durable; equipment remains
        # deactivated while the job stays open.
        if blocked_reasons is not None:
            raise MergeBlockedError(blocked_reasons)
        return job

    def _block_merge(self, conn, actor, job, reasons, phase, migrated=None):
        blocked_data = dict(job["data"])
        blocked_data["block_reasons"] = reasons
        if migrated is not None:
            blocked_data["migrated"] = migrated
        job = self.repository.conn_put_entity(
            conn, job["id"], "blocked", blocked_data
        )
        self.repository.conn_append_audit(
            conn, job["id"], actor.user_id, actor.role, "block",
            "in_progress", "blocked", {"reasons": reasons, "phase": phase},
        )
        # Equipment stays deactivated while the merge is open.
        return job

    def _migrate_history(self, conn, actor, job, source_id, retained_id):
        """Repoint inspection/maintenance/alarm/permit records at retained.

        Original records are never deleted; each carries merge provenance in
        data and receives a ``merge_migrate`` audit entry.
        """
        migrated = []
        for kind in MIGRATING_KINDS:
            for entity in self.repository.conn_list_entities(conn, kind=kind):
                if entity["data"].get("equipment_id") != source_id:
                    continue
                data = dict(entity["data"])
                data["equipment_id"] = retained_id
                data["previous_equipment_id"] = source_id
                data["migrated_by_merge"] = job["id"]
                updated = self.repository.conn_put_entity(
                    conn, entity["id"], entity["status"], data, bump=False
                )
                self.repository.conn_append_audit(
                    conn, entity["id"], actor.user_id, actor.role, "merge_migrate",
                    entity["status"], updated["status"],
                    {"merge_id": job["id"], "from_equipment_id": source_id,
                     "to_equipment_id": retained_id},
                )
                migrated.append({"kind": kind, "entity_id": entity["id"]})
        # Rescue jobs reference alarms; tag them so migrated alarms keep
        # flowing through the dispatch/close rules.
        for job_entity in self.repository.conn_list_entities(conn, kind="rescue_job"):
            alarm_id = job_entity["data"].get("alarm_id")
            if not alarm_id or job_entity["data"].get("migrated_by_merge"):
                continue
            alarm = self.repository.conn_get_entity(conn, alarm_id)
            if (
                alarm
                and alarm["data"].get("equipment_id") == retained_id
                and alarm["data"].get("previous_equipment_id") == source_id
            ):
                data = dict(job_entity["data"])
                data["migrated_by_merge"] = job["id"]
                self.repository.conn_put_entity(
                    conn, job_entity["id"], job_entity["status"], data, bump=False
                )
                migrated.append({"kind": "rescue_job", "entity_id": job_entity["id"]})
        return migrated

    def _duplicate_active_alarm_reasons(self, conn, source_id, retained_id):
        """Two active alarms with the same code on retained would violate the
        global invariant after repointing; report them before migration."""
        active = [
            alarm
            for alarm in self.repository.conn_list_entities(conn, kind="alarm")
            if alarm["status"] in ACTIVE_ALARM_STATUSES
            and alarm["data"].get("equipment_id") in (source_id, retained_id)
        ]
        seen = {}
        reasons = []
        for alarm in active:
            code = alarm["data"].get("code")
            if code in seen:
                reasons.append({
                    "reason": "duplicate_active_alarm_after_merge",
                    "alarm_code": code,
                    "alarm_ids": [seen[code], alarm["id"]],
                    "retained_equipment_id": retained_id,
                })
            else:
                seen[code] = alarm["id"]
        return reasons

    def _suspend_for_merge(self, conn, actor, source, retained):
        """Both participants are deactivated for the whole merge window."""
        updated = []
        for equipment, label in ((source, "source"), (retained, "retained")):
            data = dict(equipment["data"])
            data["merge_in_progress"] = True
            if equipment["status"] == "in_service":
                equipment = self.repository.conn_put_entity(
                    conn, equipment["id"], "suspended", data
                )
                self.repository.conn_append_audit(
                    conn, equipment["id"], actor.user_id, actor.role, "suspend",
                    "in_service", "suspended", {"merge_lock": label},
                )
            else:
                equipment = self.repository.conn_put_entity(
                    conn, equipment["id"], equipment["status"], data
                )
            updated.append(equipment)
        return updated[0], updated[1]

    def _retire_source(self, conn, actor, source, retained, merge_id):
        data = dict(source["data"])
        data.pop("merge_in_progress", None)
        data["merged_into"] = retained["id"]
        data["merged_by"] = merge_id
        self.repository.conn_put_entity(conn, source["id"], "merged", data)
        self.repository.conn_append_audit(
            conn, source["id"], actor.user_id, actor.role, "merge_away",
            source["status"], "merged",
            {"merge_id": merge_id, "retained_equipment_id": retained["id"],
             "old_asset_no": source["data"].get("asset_no"),
             "old_supervision_code": source["data"].get("supervision_code")},
        )

    def _release_retained(self, conn, actor, retained, source, job):
        data = dict(retained["data"])
        data.pop("merge_in_progress", None)
        before = job["data"].get("retained_status_before", retained["status"])
        # Only an already serviceable device returns to service automatically;
        # a suspended/out_of_service device stays in its previous status.
        restored = "in_service" if before == "in_service" else before
        aliases = list(data.get("merged_aliases", []))
        aliases.append({
            "source_equipment_id": source["id"],
            "asset_no": source["data"].get("asset_no"),
            "supervision_code": source["data"].get("supervision_code"),
        })
        data["merged_aliases"] = aliases
        self.repository.conn_put_entity(conn, retained["id"], restored, data)
        self.repository.conn_append_audit(
            conn, retained["id"], actor.user_id, actor.role, "merge_release",
            retained["status"], restored,
            {"merge_id": job["id"], "source_equipment_id": source["id"]},
        )

    @staticmethod
    def _assert_same_merge(job, source_id, retained_id):
        data = job["data"]
        if (
            data.get("source_equipment_id") != source_id
            or data.get("retained_equipment_id") != retained_id
        ):
            raise ConflictError(
                "merge id %s already binds %s -> %s"
                % (job["id"], data.get("source_equipment_id"),
                   data.get("retained_equipment_id"))
            )

    def _conn_lookup(self, conn):
        def lookup(kind, field, value):
            kind = self.rules.normalize_kind(kind)
            entities = self.repository.conn_list_entities(conn, kind=kind)
            if field == "*":
                return entities
            return [
                entity
                for entity in entities
                if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
            ]
        return lookup

    # ------------------------------------------------------------------
    # Offline synchronisation: old identifiers map onto retained equipment
    # ------------------------------------------------------------------

    # Offline records parked while a merge window was open are reprocessed on
    # replay; all other fingerprints converge onto the stored result.
    REPLAYABLE_STATUSES = ("unmapped",)

    def merge_offline(self, actor, records):
        """Synchronise field records registered under old identifiers.

        Records carry (source_id, record_id); alarms/rescue jobs are resolved
        through the merge registry to the retained equipment. A stable
        fingerprint makes every record idempotent so retries never dispatch a
        rescue twice. Records that cannot be resolved yet (e.g. the merge is
        still open) are parked as ``unmapped`` and retried on the next sync.
        """
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        require_role(actor, ("admin", "dispatcher", "maintenance", "inspector"))
        results = []
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            fingerprint = self._offline_fingerprint(source_id, record_id)
            entity_id = "offline-" + fingerprint
            existing = self.repository.get_entity(entity_id)
            if existing and existing["status"] not in self.REPLAYABLE_STATUSES:
                results.append(existing)
                continue
            results.append(
                self._sync_one_offline(
                    actor, raw, source_id, record_id, fingerprint, entity_id,
                    prior=existing,
                )
            )
        return results

    def _sync_one_offline(self, actor, raw, source_id, record_id, fingerprint,
                          entity_id, prior=None):
        record_type = str(raw.get("record_type", "alarm")).strip() or "alarm"
        payload = dict(raw)
        with self.repository.write_lock() as conn:
            stored = prior
            if stored is None:
                marker = self.repository.get_fingerprint(fingerprint)
                if marker:
                    existing = self.repository.conn_get_entity(conn, marker["entity_id"])
                    if existing and existing["status"] not in self.REPLAYABLE_STATUSES:
                        return existing
                    stored = existing

            target_id, mapping = self._resolve_offline_equipment(conn, payload)
            if not target_id:
                result = self._store_offline_record(
                    conn, actor, payload, source_id, record_id, fingerprint,
                    "unmapped", entity_id,
                    mapping_note="equipment identifier not resolved",
                    replace=stored,
                )
                self.repository.conn_save_fingerprint(
                    conn, fingerprint, "offline_record", result["id"]
                )
                return result

            if record_type == "alarm":
                return self._sync_alarm(
                    conn, actor, payload, target_id, mapping,
                    source_id, record_id, fingerprint, entity_id,
                    replace=stored,
                )
            if record_type == "rescue_job":
                return self._sync_rescue(
                    conn, actor, payload, target_id, mapping,
                    source_id, record_id, fingerprint, entity_id,
                    replace=stored,
                )
            return self._store_offline_record(
                conn, actor, payload, source_id, record_id, fingerprint,
                "unmapped", entity_id,
                mapping_note="unsupported record_type: " + record_type,
                resolved_equipment_id=target_id, replace=stored,
            )

    def _sync_alarm(self, conn, actor, payload, target_id, mapping,
                    source_id, record_id, fingerprint, entity_id, replace=None):
        code = str(payload.get("code", "")).strip()
        if not code:
            return self._store_offline_record(
                conn, actor, payload, source_id, record_id, fingerprint,
                "rejected", entity_id, mapping_note="alarm requires code",
                resolved_equipment_id=target_id, replace=replace,
            )
        equipment = self.repository.conn_get_entity(conn, target_id)
        if equipment["data"].get("merge_in_progress") or equipment["status"] == "merged":
            # Merge window: park the event; replay it after completion.
            return self._store_offline_record(
                conn, actor, payload, source_id, record_id, fingerprint,
                "unmapped", entity_id, mapping_note="merge in progress, retry later",
                resolved_equipment_id=target_id, replace=replace,
            )
        # Same active alarm (device+code) already exists: do not create another.
        duplicate = None
        for alarm in self.repository.conn_list_entities(conn, kind="alarm"):
            if (
                alarm["data"].get("equipment_id") == target_id
                and alarm["data"].get("code") == code
                and alarm["status"] in ACTIVE_ALARM_STATUSES
            ):
                duplicate = alarm
                break
        data = {
            "equipment_id": target_id,
            "code": code,
            "occurred_at": payload.get("occurred_at") or self._now(),
            "offline_source_id": source_id,
            "offline_record_id": record_id,
            "sync_fingerprint": fingerprint,
        }
        if payload.get("details"):
            data["details"] = payload.get("details")
        if duplicate:
            data["duplicate_of_alarm_id"] = duplicate["id"]
            return self._store_offline_record(
                conn, actor, data, source_id, record_id, fingerprint,
                "deduped", entity_id, mapping_note="active alarm already exists",
                resolved_equipment_id=target_id, replace=replace,
            )
        self.rules.validate_create(actor, "alarm", data, self._conn_lookup(conn))
        alarm = self.repository.conn_insert_entity(
            conn, "alarm-" + fingerprint[:24], "alarm", "received", data, actor.user_id
        )
        self.repository.conn_append_audit(
            conn, alarm["id"], actor.user_id, actor.role, "offline_sync",
            None, "received",
            {"source_id": source_id, "record_id": record_id, "mapping": mapping},
        )
        return self._store_offline_record(
            conn, actor, data, source_id, record_id, fingerprint,
            "synced", entity_id, created_entity_id=alarm["id"],
            resolved_equipment_id=target_id, replace=replace,
        )

    def _sync_rescue(self, conn, actor, payload, target_id, mapping,
                     source_id, record_id, fingerprint, entity_id, replace=None):
        # Stable rescue dedupe key: the same offline record always lands on
        # the same key, and the rescue rule rejects any second active dispatch.
        dedupe_key = str(payload.get("dedupe_key", "")).strip() or (
            "offline-" + fingerprint[:32]
        )
        team = str(payload.get("team", "")).strip()
        if not team:
            return self._store_offline_record(
                conn, actor, payload, source_id, record_id, fingerprint,
                "rejected", entity_id, mapping_note="rescue_job requires team",
                resolved_equipment_id=target_id, replace=replace,
            )
        # Resolve the alarm: explicit id, or the active alarm on retained with
        # matching code (which may itself have arrived via offline sync).
        alarm = None
        alarm_id = str(payload.get("alarm_id", "")).strip()
        if alarm_id:
            candidate = self.repository.conn_get_entity(conn, alarm_id)
            if candidate and candidate["kind"] == "alarm" and candidate["status"] != "closed":
                alarm = candidate
        if not alarm:
            alarm_code = str(payload.get("alarm_code", payload.get("code", ""))).strip()
            for candidate in self.repository.conn_list_entities(conn, kind="alarm"):
                if (
                    candidate["data"].get("equipment_id") == target_id
                    and candidate["data"].get("code") == alarm_code
                    and candidate["status"] in ("received", "dispatched")
                ):
                    alarm = candidate
                    break
        if not alarm:
            return self._store_offline_record(
                conn, actor, payload, source_id, record_id, fingerprint,
                "unmapped", entity_id, mapping_note="no active alarm to dispatch rescue",
                resolved_equipment_id=target_id, replace=replace,
            )
        # Hard dedupe against already synchronised rescue jobs.
        for job in self.repository.conn_list_entities(conn, kind="rescue_job"):
            if (
                job["data"].get("dedupe_key") == dedupe_key
                and job["status"] not in ("completed", "aborted")
            ):
                data = {
                    "equipment_id": target_id,
                    "alarm_id": alarm["id"],
                    "dedupe_key": dedupe_key,
                    "duplicate_of_rescue_job_id": job["id"],
                }
                return self._store_offline_record(
                    conn, actor, data, source_id, record_id, fingerprint,
                    "deduped", entity_id, mapping_note="rescue already dispatched",
                    resolved_equipment_id=target_id, replace=replace,
                )
        data = {
            "alarm_id": alarm["id"],
            "dedupe_key": dedupe_key,
            "team": team,
            "equipment_id": target_id,
            "offline_source_id": source_id,
            "offline_record_id": record_id,
            "sync_fingerprint": fingerprint,
        }
        self.rules.validate_create(actor, "rescue_job", data, self._conn_lookup(conn))
        job = self.repository.conn_insert_entity(
            conn, "rescue-" + fingerprint[:24], "rescue_job", "dispatched",
            data, actor.user_id
        )
        self.repository.conn_append_audit(
            conn, job["id"], actor.user_id, actor.role, "offline_sync",
            None, "dispatched",
            {"source_id": source_id, "record_id": record_id, "mapping": mapping,
             "dedupe_key": dedupe_key},
        )
        return self._store_offline_record(
            conn, actor, data, source_id, record_id, fingerprint,
            "synced", entity_id, created_entity_id=job["id"],
            resolved_equipment_id=target_id, replace=replace,
        )

    def _resolve_offline_equipment(self, conn, payload):
        """Map an old asset_no / supervision_code to the retained equipment."""
        explicit_id = str(payload.get("equipment_id", "")).strip()
        identifiers = [
            ("asset_no", str(payload.get("asset_no", "")).strip()),
            ("supervision_code", str(payload.get("supervision_code", "")).strip()),
        ]
        equipment_rows = self.repository.conn_list_entities(conn, kind="equipment")
        live = {e["id"]: e for e in equipment_rows if e["status"] != "merged"}
        if explicit_id:
            if explicit_id in live:
                return explicit_id, {"mode": "direct", "equipment_id": explicit_id}
            # Explicit id may be a merged-away source; the registry redirects
            # records taken offline under the old device id.
            source_row = self.repository.conn_find_registry_by_source(conn, explicit_id)
            if source_row:
                return source_row["retained_equipment_id"], {
                    "mode": "merge_alias", "field": "equipment_id",
                    "code": explicit_id,
                    "source_equipment_id": source_row["source_equipment_id"],
                }
            for field, value in identifiers:
                if not value:
                    continue
                row = self.repository.conn_find_registry_by_code(conn, field, value)
                if row:
                    return row["retained_equipment_id"], {
                        "mode": "merge_alias", "field": field, "code": value,
                        "source_equipment_id": row["source_equipment_id"],
                    }
            return None, {"mode": "unresolved", "equipment_id": explicit_id}
        for field, value in identifiers:
            if not value:
                continue
            for equipment in equipment_rows:
                if (
                    str(equipment["data"].get(field, "")).strip() == value
                    and equipment["status"] != "merged"
                ):
                    return equipment["id"], {
                        "mode": "direct", "field": field, "code": value,
                    }
            row = self.repository.conn_find_registry_by_code(conn, field, value)
            if row:
                return row["retained_equipment_id"], {
                    "mode": "merge_alias", "field": field, "code": value,
                    "source_equipment_id": row["source_equipment_id"],
                }
        return None, {
            "mode": "unresolved",
            "identifiers": {f: v for f, v in identifiers if v},
        }

    def _store_offline_record(self, conn, actor, payload, source_id, record_id,
                              fingerprint, status, entity_id, mapping_note=None,
                              created_entity_id=None, resolved_equipment_id=None,
                              replace=None):
        data = {
            "source_id": source_id,
            "record_id": record_id,
            "record_type": payload.get("record_type", "alarm"),
            "payload": {
                k: v for k, v in payload.items()
                if k not in ("source_id", "record_id")
            },
            "sync_fingerprint": fingerprint,
            "sync_status": status,
        }
        if mapping_note:
            data["mapping_note"] = mapping_note
        if created_entity_id:
            data["created_entity_id"] = created_entity_id
        if resolved_equipment_id:
            data["resolved_equipment_id"] = resolved_equipment_id
        if replace is not None:
            data["previous_sync_status"] = replace["status"]
        record = self.repository.conn_upsert_entity(
            conn, entity_id, "offline_record", status, data, actor.user_id
        )
        self.repository.conn_append_audit(
            conn, entity_id, actor.user_id, actor.role, "merge_offline",
            replace["status"] if replace else None, status,
            {"source_id": source_id, "record_id": record_id, "note": mapping_note},
        )
        if replace is not None:
            # The parked row now resolves to a concrete alarm/rescue entity.
            self.repository.conn_save_fingerprint(
                conn, fingerprint, "offline_record", record["id"]
            )
        return record

    @staticmethod
    def _offline_fingerprint(source_id, record_id):
        return hashlib.sha256(
            (source_id + "\0" + record_id).encode("utf-8")
        ).hexdigest()[:32]

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
