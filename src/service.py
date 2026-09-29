from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .repository import utcnow
from .rules import RuleEngine


CHILD_KINDS = ("inspection", "maintenance", "alarm", "permit")


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

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

    def _find_merge_for_source(self, source_id):
        merges = [
            m for m in self.repository.list_entities("equipment_merge")
            if m["data"].get("source_id") == source_id
        ]
        if not merges:
            return None
        return max(merges, key=lambda m: (m["created_at"], m["id"]))

    def _find_completed_merge(self, source_id):
        for merge in self.repository.list_entities("equipment_merge", status="completed"):
            if merge["data"].get("source_id") == source_id:
                return merge
        return None

    def _resolve_equipment_id(self, code):
        if code is None:
            return None
        code = str(code).strip()
        if not code:
            return None
        equipment = self.repository.get_entity(code)
        if equipment and equipment["kind"] == "equipment":
            merge = self._find_completed_merge(equipment["id"])
            return merge["data"]["target_id"] if merge else equipment["id"]
        rows = self.repository.find_entities("equipment", "asset_no", code)
        if rows:
            equipment = rows[0]
            merge = self._find_completed_merge(equipment["id"])
            return merge["data"]["target_id"] if merge else equipment["id"]
        return None

    def merge_equipment(self, actor, source_id=None, source_asset_no=None, target_id=None):
        """Prepare an equipment identifier merge: suspend the source equipment and
        snapshot the records that will move to the retained equipment."""
        source = None
        if source_id:
            source = self.repository.get_entity(source_id)
            if not source or source["kind"] != "equipment":
                raise NotFoundError("source equipment not found: " + str(source_id))
        elif source_asset_no:
            rows = self.repository.find_entities("equipment", "asset_no", str(source_asset_no).strip())
            if not rows:
                raise NotFoundError("source equipment not found by asset_no: " + str(source_asset_no))
            source = rows[0]
        else:
            raise ValidationError("source_id or source_asset_no is required")
        if not target_id:
            raise ValidationError("target_id is required")
        target = self.repository.get_entity(target_id)
        if not target or target["kind"] != "equipment":
            raise NotFoundError("retained equipment not found: " + str(target_id))
        if source["id"] == target["id"]:
            raise ValidationError("source and retained equipment must be different")

        existing = self._find_merge_for_source(source["id"])
        if existing:
            if existing["status"] == "completed" and existing["data"].get("target_id") == target["id"]:
                return existing
            if existing["status"] == "completed":
                raise ConflictError(
                    "旧编码 %s 已合并到保留设备 %s，不能再并入 %s"
                    % (
                        existing["data"].get("old_code"),
                        existing["data"].get("retained_code") or existing["data"].get("target_id"),
                        target["data"].get("asset_no") or target["id"],
                    )
                )
            if existing["status"] == "pending":
                raise ConflictError("merge already in progress for source equipment")
            if existing["status"] == "blocked" and existing["data"].get("target_id") == target["id"]:
                return existing
            raise ConflictError("merge is blocked: " + "; ".join(existing["data"].get("basis", [])))

        if source["status"] == "in_service":
            source = self.transition(actor, source["id"], "suspend", {})
        elif source["status"] != "suspended":
            raise ConflictError(
                "source equipment must be suspended before merge (current status: " + source["status"] + ")"
            )

        snapshot = self.repository.count_by_equipment(source["id"], CHILD_KINDS)
        merge_data = {
            "source_id": source["id"],
            "target_id": target["id"],
            "old_code": source["data"].get("asset_no"),
            "retained_code": target["data"].get("asset_no"),
            "snapshot": dict(snapshot),
            "migrated": {},
            "basis": [],
            "requested_by": actor.user_id,
        }
        merge = self.repository.create_entity(
            str(uuid4()), "equipment_merge", "pending", merge_data, actor.user_id
        )
        self.audit.record(
            merge["id"], actor, "merge_prepare", None, "pending",
            {"source_id": source["id"], "target_id": target["id"], "snapshot": snapshot},
        )
        return merge

    def execute_merge(self, actor, merge_id):
        """Execute a prepared merge after re-validating blocking conditions."""
        merge = self.repository.get_entity(merge_id)
        if not merge or merge["kind"] != "equipment_merge":
            raise NotFoundError("merge not found: " + str(merge_id))
        if merge["status"] == "completed":
            return merge
        if merge["status"] not in ("pending", "blocked"):
            raise ConflictError("merge cannot be executed from status " + merge["status"])

        data = dict(merge["data"])
        source_id = data["source_id"]
        target_id = data["target_id"]
        source = self.repository.get_entity(source_id)
        if not source or source["kind"] != "equipment":
            raise NotFoundError("source equipment not found: " + source_id)

        basis = []
        old_code = data.get("old_code")
        if old_code is not None:
            for equipment in self.repository.list_entities("equipment"):
                if equipment["id"] in (source_id, target_id):
                    continue
                if equipment["data"].get("asset_no") == old_code:
                    basis.append(
                        "旧编码 %s 已被设备 %s(asset_no=%s) 占用"
                        % (old_code, equipment["id"], equipment["data"].get("asset_no"))
                    )
            for prior in self.repository.list_entities("equipment_merge", status="completed"):
                if prior["id"] == merge_id:
                    continue
                if prior["data"].get("old_code") == old_code and prior["data"].get("target_id") != target_id:
                    basis.append(
                        "旧编码 %s 已合并到保留设备 %s，不能再并入 %s"
                        % (
                            old_code,
                            prior["data"].get("retained_code") or prior["data"].get("target_id"),
                            target["data"].get("asset_no") or target_id,
                        )
                    )

        current_alarms = self.repository.find_entities("alarm", "equipment_id", source_id)
        snapshot_alarm = data.get("snapshot", {}).get("alarm", 0)
        if len(current_alarms) > snapshot_alarm:
            basis.append(
                "迁移期间设备 %s 新增 %d 条报警（快照 %d 条）"
                % (source_id, len(current_alarms), snapshot_alarm)
            )
            for alarm in current_alarms:
                basis.append(
                    "报警 %s code=%s occurred_at=%s"
                    % (alarm["id"], alarm["data"].get("code"), alarm["data"].get("occurred_at"))
                )

        if basis:
            blocked = dict(data)
            blocked["basis"] = basis
            self.repository.update_entity(merge_id, merge["version"], "blocked", blocked)
            self.audit.record(merge_id, actor, "merge_block", merge["status"], "blocked", {"basis": basis})
            raise ConflictError("合并中止：" + "；".join(basis))

        if source["status"] == "suspended":
            source = self.transition(actor, source_id, "merge", {})
        elif source["status"] != "merged":
            raise ConflictError("source equipment must be suspended to complete merge")

        migrated = self.repository.reassign_equipment(source_id, target_id, list(CHILD_KINDS))
        completed = dict(data)
        completed["migrated"] = migrated
        completed["basis"] = []
        completed["completed_at"] = utcnow()
        updated = self.repository.update_entity(merge_id, merge["version"], "completed", completed)
        self.audit.record(
            merge_id, actor, "merge_execute", merge["status"], "completed", {"migrated": migrated}
        )
        return updated

    def merge_offline(self, actor, records):
        """Materialize offline records, mapping old equipment codes to retained
        equipment via completed merges. Idempotent by (source_id, record_id)."""
        if not isinstance(records, list):
            raise ValidationError("records must be a list")
        created = []
        index = {}
        for raw in records:
            if not isinstance(raw, dict):
                raise ValidationError("each offline record must be an object")
            source_id = str(raw.get("source_id", "")).strip()
            record_id = str(raw.get("record_id", "")).strip()
            if not source_id or not record_id:
                raise ValidationError("source_id and record_id are required")
            kind = self.rules.normalize_kind(str(raw.get("kind", "")).strip())
            if kind not in self.rules.INITIAL_STATUS or kind == "equipment_merge":
                raise ValidationError("unsupported offline record kind: " + str(raw.get("kind")))
            idem_key = "offline:" + source_id + ":" + record_id
            existing_id = self.repository.get_idempotency(actor.user_id, idem_key)
            if existing_id:
                existing = self.repository.get_entity(existing_id)
                if existing:
                    index[(source_id, record_id)] = existing_id
                    created.append(existing)
                    continue
            payload = dict(raw.get("data") or {})
            equipment_ref = payload.pop("equipment_asset_no", None)
            if equipment_ref is not None or "equipment_id" in self.rules.CREATE_REQUIRED.get(kind, ()):
                resolved = self._resolve_equipment_id(equipment_ref or payload.get("equipment_id"))
                if not resolved:
                    raise ValidationError("offline record requires a known equipment or asset_no")
                payload["equipment_id"] = resolved
            alarm_ref = payload.pop("alarm_ref", None)
            if alarm_ref:
                ref_source = str(alarm_ref.get("source_id", "")).strip()
                ref_record = str(alarm_ref.get("record_id", "")).strip()
                alarm_entity_id = index.get((ref_source, ref_record))
                if not alarm_entity_id:
                    alarm_entity_id = self.repository.get_idempotency(
                        actor.user_id, "offline:" + ref_source + ":" + ref_record
                    )
                if not alarm_entity_id:
                    raise ValidationError("offline rescue_job requires a synced alarm_ref")
                payload["alarm_id"] = alarm_entity_id
            entity = self.create(actor, kind, payload)
            self.repository.save_idempotency(actor.user_id, idem_key, entity["id"])
            index[(source_id, record_id)] = entity["id"]
            created.append(entity)
        return created

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
