import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine())
        self.admin = Actor("admin", "admin")
        self.dispatcher = Actor("dispatcher", "dispatcher")

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, kind, data, actor=None):
        return self.service.create(actor or self.admin, kind, data)

    def act(self, entity, action, data=None, actor=None):
        return self.service.transition(actor or self.admin, entity["id"], action, data or {})

    def equipment(self, asset_no, **extra):
        data = {"asset_no": asset_no, "equipment_type": "elevator", "location": "A", "inspection_interval_days": 365}
        data.update(extra)
        return self.create("equipment", data)

    def test_history_migrates_to_retained_equipment(self):
        source = self.equipment("OLD-1")
        target = self.equipment("RET-1")

        inspection = self.create("inspection", {"equipment_id": source["id"], "scheduled_at": "2026-09-27T09:00:00Z", "cycle_days": 365})
        self.act(inspection, "pass", {"findings": "normal"})
        maintenance = self.create("maintenance", {"equipment_id": source["id"], "work_type": "routine", "planned_at": "2026-09-27"})
        self.act(maintenance, "start", {})
        self.act(maintenance, "complete", {"completed_at": "2026-09-28"})
        alarm = self.create("alarm", {"equipment_id": source["id"], "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"})
        self.act(alarm, "dispatch", {"team": "Alpha"})
        permit = self.create("permit", {"equipment_id": source["id"], "purpose": "return_to_service", "requested_by": "ops"})
        self.act(permit, "request_review", {})

        merge = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])
        self.assertEqual(merge["status"], "pending")
        self.assertEqual(merge["data"]["snapshot"]["inspection"], 1)
        self.assertEqual(merge["data"]["snapshot"]["maintenance"], 1)
        self.assertEqual(merge["data"]["snapshot"]["alarm"], 1)
        self.assertEqual(merge["data"]["snapshot"]["permit"], 1)
        # 完成前设备保持停用
        self.assertEqual(self.service.get(source["id"])["status"], "suspended")

        completed = self.service.execute_merge(self.admin, merge["id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["data"]["migrated"]["inspection"], 1)
        self.assertEqual(completed["data"]["migrated"]["maintenance"], 1)
        self.assertEqual(completed["data"]["migrated"]["alarm"], 1)
        self.assertEqual(completed["data"]["migrated"]["permit"], 1)

        # 原记录不删，设备变为已合并
        self.assertEqual(self.service.get(source["id"])["status"], "merged")
        self.assertEqual(self.service.get(inspection["id"])["data"]["equipment_id"], target["id"])
        self.assertEqual(self.service.get(maintenance["id"])["data"]["equipment_id"], target["id"])
        self.assertEqual(self.service.get(alarm["id"])["data"]["equipment_id"], target["id"])
        self.assertEqual(self.service.get(permit["id"])["data"]["equipment_id"], target["id"])
        self.assertIsNotNone(self.service.get(inspection["id"]))
        self.assertIsNotNone(self.service.get(alarm["id"]))

    def test_two_batches_submitting_same_merge_only_one_succeeds(self):
        source = self.equipment("OLD-2")
        target = self.equipment("RET-2")

        first = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])
        self.assertEqual(first["status"], "pending")

        with self.assertRaises(ConflictError):
            self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])

        self.service.execute_merge(self.admin, first["id"])

        # 合并失败后带原数据重试成功
        retry = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])
        self.assertEqual(retry["status"], "completed")
        self.assertEqual(retry["id"], first["id"])

    def test_merge_blocked_when_new_alarm_appears_during_migration(self):
        source = self.equipment("OLD-3")
        target = self.equipment("RET-3")

        merge = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])

        self.create("alarm", {"equipment_id": source["id"], "code": "NEW-ALARM", "occurred_at": "2026-09-27T11:00:00Z"})

        with self.assertRaises(ConflictError) as ctx:
            self.service.execute_merge(self.admin, merge["id"])
        self.assertIn("NEW-ALARM", str(ctx.exception))
        self.assertIn("新增", str(ctx.exception))

        blocked = self.service.get(merge["id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertTrue(blocked["data"]["basis"])
        # 完成前设备保持停用
        self.assertEqual(self.service.get(source["id"])["status"], "suspended")

    def test_merge_blocked_when_old_code_occupied(self):
        source = self.equipment("OLD-4")
        target = self.equipment("RET-4")
        # 绕过唯一性校验，塞入一台占用旧编码的设备
        self.service.repository.create_entity(
            "equip-other", "equipment", "in_service",
            {"asset_no": "OLD-4", "equipment_type": "elevator", "location": "B", "inspection_interval_days": 365},
            self.admin.user_id,
        )

        merge = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])
        with self.assertRaises(ConflictError) as ctx:
            self.service.execute_merge(self.admin, merge["id"])
        self.assertIn("OLD-4", str(ctx.exception))
        self.assertIn("占用", str(ctx.exception))
        self.assertEqual(self.service.get(merge["id"])["status"], "blocked")

    def test_merge_blocked_when_old_code_already_aliased(self):
        source = self.equipment("OLD-5")
        first_target = self.equipment("RET-5A")
        second_target = self.equipment("RET-5B")

        first = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=first_target["id"])
        self.service.execute_merge(self.admin, first["id"])

        with self.assertRaises(ConflictError) as ctx:
            self.service.merge_equipment(self.admin, source_asset_no="OLD-5", target_id=second_target["id"])
        self.assertIn("OLD-5", str(ctx.exception))
        self.assertIn("RET-5A", str(ctx.exception))

    def test_records_cannot_be_created_against_merged_equipment(self):
        source = self.equipment("OLD-6")
        target = self.equipment("RET-6")
        merge = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])
        self.service.execute_merge(self.admin, merge["id"])

        with self.assertRaises(ConflictError):
            self.create("alarm", {"equipment_id": source["id"], "code": "X1", "occurred_at": "2026-09-27T12:00:00Z"})

    def test_offline_sync_maps_old_code_to_retained_equipment(self):
        source = self.equipment("OLD-7")
        target = self.equipment("RET-7")
        merge = self.service.merge_equipment(self.admin, source_id=source["id"], target_id=target["id"])
        self.service.execute_merge(self.admin, merge["id"])

        records = [
            {"source_id": "tablet-1", "record_id": "alarm-1", "kind": "alarm",
             "data": {"equipment_asset_no": "OLD-7", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tablet-1", "record_id": "job-1", "kind": "rescue_job",
             "data": {"alarm_ref": {"source_id": "tablet-1", "record_id": "alarm-1"}, "dedupe_key": "rescue-1", "team": "Alpha"}},
        ]
        synced = self.service.merge_offline(self.dispatcher, records)
        self.assertEqual(len(synced), 2)
        alarm = synced[0]
        job = synced[1]
        self.assertEqual(alarm["kind"], "alarm")
        self.assertEqual(alarm["data"]["equipment_id"], target["id"])
        self.assertEqual(job["kind"], "rescue_job")
        self.assertEqual(job["data"]["alarm_id"], alarm["id"])

        # 重复同步不重复派任务
        again = self.service.merge_offline(self.dispatcher, records)
        self.assertEqual(len(again), 2)
        self.assertEqual(again[0]["id"], alarm["id"])
        self.assertEqual(again[1]["id"], job["id"])
        jobs = self.service.list("rescue_job")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["data"]["dedupe_key"], "rescue-1")

    def test_offline_sync_dedupes_rescue_jobs_by_key(self):
        equipment = self.equipment("OLD-8")
        records = [
            {"source_id": "tablet-2", "record_id": "alarm-1", "kind": "alarm",
             "data": {"equipment_id": equipment["id"], "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
            {"source_id": "tablet-2", "record_id": "job-1", "kind": "rescue_job",
             "data": {"alarm_ref": {"source_id": "tablet-2", "record_id": "alarm-1"}, "dedupe_key": "rescue-2", "team": "Alpha"}},
        ]
        self.service.merge_offline(self.dispatcher, records)

        duplicate = [
            {"source_id": "tablet-3", "record_id": "job-2", "kind": "rescue_job",
             "data": {"alarm_ref": {"source_id": "tablet-2", "record_id": "alarm-1"}, "dedupe_key": "rescue-2", "team": "Beta"}},
        ]
        with self.assertRaises(ConflictError):
            self.service.merge_offline(self.dispatcher, duplicate)

    def test_offline_sync_requires_known_equipment(self):
        records = [
            {"source_id": "tablet-4", "record_id": "alarm-1", "kind": "alarm",
             "data": {"equipment_asset_no": "UNKNOWN-99", "code": "DOOR-JAM", "occurred_at": "2026-09-27T10:00:00Z"}},
        ]
        with self.assertRaises(ValidationError):
            self.service.merge_offline(self.dispatcher, records)


if __name__ == "__main__":
    unittest.main()
