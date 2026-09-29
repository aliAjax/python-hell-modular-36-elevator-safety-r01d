import tempfile
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    MergeBlockedError,
    PermissionDenied,
    ValidationError,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine, merge_guard_reasons
from src.service import DomainService


class DeviceMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "test.db"), RuleEngine()
        )
        self.admin = Actor("admin", "admin")

    def tearDown(self):
        self.tmp.cleanup()

    def equipment(self, asset_no, supervision_code=None):
        data = {
            "asset_no": asset_no,
            "equipment_type": "elevator",
            "location": "Block A",
            "inspection_interval_days": 365,
        }
        if supervision_code:
            data["supervision_code"] = supervision_code
        return self.service.create(self.admin, "equipment", data)

    def start_merge(self, source, retained, **extra):
        payload = {
            "source_equipment_id": source["id"],
            "retained_equipment_id": retained["id"],
        }
        payload.update(extra)
        return self.service.start_device_merge(self.admin, payload)

    def close_alarm(self, alarm, team="Alpha", dedupe_key="job"):
        self.service.transition(self.admin, alarm["id"], "dispatch", {"team": team})
        job = self.service.create(
            self.admin,
            "rescue_job",
            {"alarm_id": alarm["id"], "dedupe_key": dedupe_key, "team": team},
        )
        self.service.transition(self.admin, job["id"], "arrive", {})
        self.service.transition(self.admin, job["id"], "complete", {"outcome": "freed"})
        self.service.transition(self.admin, alarm["id"], "resolve", {"resolution": "safe"})
        self.service.transition(self.admin, alarm["id"], "close", {})
        return job

    # ------------------------------------------------------------------
    # History migration
    # ------------------------------------------------------------------

    def test_merge_migrates_history_and_keeps_source_record(self):
        source = self.equipment("BLDG-1")
        retained = self.equipment("CITY-1", supervision_code="SUP-1")

        inspection = self.service.create(
            self.admin,
            "inspection",
            {"equipment_id": source["id"], "scheduled_at": "2026-08-01T09:00:00Z", "cycle_days": 365},
        )
        inspection = self.service.transition(self.admin, inspection["id"], "pass", {"findings": "ok"})
        maintenance = self.service.create(
            self.admin,
            "maintenance",
            {"equipment_id": source["id"], "work_type": "routine", "planned_at": "2026-08-02T09:00:00Z"},
        )
        alarm = self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": source["id"], "code": "DOOR", "occurred_at": "2026-08-03T10:00:00Z"},
        )
        rescue_job = self.close_alarm(alarm, dedupe_key="old-job")
        permit = self.service.create(
            self.admin,
            "permit",
            {"equipment_id": source["id"], "purpose": "return_to_service", "requested_by": "ops"},
        )

        job = self.start_merge(source, retained, merge_id="merge-1")
        job = self.service.transition(self.admin, job["id"], "complete", {})

        self.assertEqual(job["status"], "completed")
        migrated_kinds = {item["kind"] for item in job["data"]["migrated"]}
        self.assertTrue(
            {"inspection", "maintenance", "alarm", "permit"} <= migrated_kinds
        )
        migrated_pairs = {(item["kind"], item["entity_id"]) for item in job["data"]["migrated"]}
        self.assertIn(("alarm", alarm["id"]), migrated_pairs)
        self.assertIn(("inspection", inspection["id"]), migrated_pairs)
        self.assertIn(("maintenance", maintenance["id"]), migrated_pairs)
        self.assertIn(("permit", permit["id"]), migrated_pairs)
        self.assertIn(("rescue_job", rescue_job["id"]), migrated_pairs)

        # All history points at retained; originals are preserved with
        # provenance and are not deleted.
        inspection = self.service.get(inspection["id"])
        self.assertEqual(inspection["data"]["equipment_id"], retained["id"])
        self.assertEqual(inspection["data"]["previous_equipment_id"], source["id"])
        self.assertEqual(inspection["status"], "passed")
        self.assertEqual(
            self.service.get(maintenance["id"])["data"]["equipment_id"], retained["id"]
        )
        migrated_alarm = self.service.get(alarm["id"])
        self.assertEqual(migrated_alarm["data"]["equipment_id"], retained["id"])
        self.assertEqual(
            self.service.get(permit["id"])["data"]["equipment_id"], retained["id"]
        )

        # Source is retired (not deleted) and carries the merge link; retained
        # carries the alias and is back in service.
        source = self.service.get(source["id"])
        retained = self.service.get(retained["id"])
        self.assertEqual(source["status"], "merged")
        self.assertEqual(source["data"]["merged_into"], retained["id"])
        self.assertEqual(retained["status"], "in_service")
        self.assertEqual(retained["data"]["merged_aliases"][0]["asset_no"], "BLDG-1")

    def test_devices_stay_deactivated_until_merge_completes(self):
        source = self.equipment("BLDG-2")
        retained = self.equipment("CITY-2")
        job = self.start_merge(source, retained)
        self.assertEqual(self.service.get(source["id"])["status"], "suspended")
        self.assertEqual(self.service.get(retained["id"])["status"], "suspended")
        self.assertTrue(self.service.get(retained["id"])["data"]["merge_in_progress"])
        # Cannot return the equipment to service mid-merge
        with self.assertRaises(ConflictError):
            self.service.transition(self.admin, retained["id"], "return_to_service", {})
        # finish
        self.service.transition(self.admin, job["id"], "complete", {})
        self.assertEqual(self.service.get(retained["id"])["status"], "in_service")
        self.assertNotIn("merge_in_progress", self.service.get(retained["id"])["data"])

    # ------------------------------------------------------------------
    # Blocking with evidence
    # ------------------------------------------------------------------

    def test_old_code_occupied_by_other_device_blocks_with_evidence(self):
        source = self.equipment("BLDG-3")
        retained = self.equipment("CITY-3")
        squatter = self.equipment("OTHER-3", supervision_code="BLDG-3")
        job = self.start_merge(source, retained)

        with self.assertRaises(MergeBlockedError) as caught:
            self.service.transition(self.admin, job["id"], "complete", {})
        reasons = caught.exception.reasons
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0]["reason"], "code_occupied")
        self.assertEqual(reasons[0]["occupying_equipment_id"], squatter["id"])
        self.assertEqual(reasons[0]["occupying_field"], "supervision_code")

        # The blocked state is durable and equipment stays deactivated.
        self.assertEqual(self.service.get(job["id"])["status"], "blocked")
        self.assertEqual(self.service.get(source["id"])["status"], "suspended")
        self.assertEqual(self.service.get(retained["id"])["status"], "suspended")

        # Operator releases the occupied code, then retry succeeds.
        data = dict(squatter["data"])
        data["supervision_code"] = "BLDG-3-RESOLVED"
        self.service.repository.update_entity(squatter["id"], squatter["version"], squatter["status"], data)
        job = self.service.transition(self.admin, job["id"], "retry", {})
        job = self.service.transition(self.admin, job["id"], "complete", {})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.service.get(source["id"])["status"], "merged")

    def test_new_alarm_during_merge_blocks_until_resolved(self):
        source = self.equipment("BLDG-4")
        retained = self.equipment("CITY-4")
        job = self.start_merge(source, retained)

        # Emergency intake stays open while devices are deactivated.
        alarm = self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": source["id"], "code": "ENTRAP", "occurred_at": "2026-09-29T12:00:00Z"},
        )
        with self.assertRaises(MergeBlockedError) as caught:
            self.service.transition(self.admin, job["id"], "complete", {})
        self.assertEqual(caught.exception.reasons[0]["reason"], "new_alarm")
        self.assertEqual(caught.exception.reasons[0]["alarm_id"], alarm["id"])

        # Retrying with the original request while the alarm is open stays blocked.
        with self.assertRaises(MergeBlockedError):
            self.service.transition(self.admin, job["id"], "complete", {})
        # An explicit retry does not absorb the open alarm into the baseline.
        job = self.service.transition(self.admin, job["id"], "retry", {})
        self.assertEqual(job["status"], "in_progress")
        with self.assertRaises(MergeBlockedError):
            self.service.transition(self.admin, job["id"], "complete", {})

        # Resolve the alarm following the rescue rules, then retry succeeds and
        # the alarm is migrated to retained.
        self.close_alarm(alarm, dedupe_key="new-job")
        job = self.service.transition(self.admin, job["id"], "complete", {})
        self.assertEqual(job["status"], "completed")
        self.assertEqual(self.service.get(alarm["id"])["data"]["equipment_id"], retained["id"])

    def test_duplicate_active_alarm_codes_block_merge(self):
        source = self.equipment("BLDG-44")
        retained = self.equipment("CITY-44")
        self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": source["id"], "code": "SAME", "occurred_at": "2026-09-29T12:00:00Z"},
        )
        self.service.create(
            self.admin,
            "alarm",
            {"equipment_id": retained["id"], "code": "SAME", "occurred_at": "2026-09-29T12:01:00Z"},
        )
        job = self.start_merge(source, retained)
        with self.assertRaises(MergeBlockedError) as caught:
            self.service.transition(self.admin, job["id"], "complete", {})
        self.assertEqual(
            caught.exception.reasons[0]["reason"], "duplicate_active_alarm_after_merge"
        )

    # ------------------------------------------------------------------
    # Concurrency and retry
    # ------------------------------------------------------------------

    def test_only_one_merge_succeeds_for_same_device(self):
        source = self.equipment("BLDG-5")
        retained = self.equipment("CITY-5")
        self.start_merge(source, retained, merge_id="merge-A")
        with self.assertRaises(ConflictError):
            self.start_merge(source, retained, merge_id="merge-B")
        with self.assertRaises(ConflictError):
            self.start_merge(retained, source, merge_id="merge-C")

    def test_failed_submission_retried_with_original_data_succeeds(self):
        source = self.equipment("BLDG-6")
        retained = self.equipment("CITY-6")
        job = self.start_merge(source, retained, merge_id="merge-fixed")
        # A different device claims the source's old asset code first.
        squatter = self.equipment("OTHER-6", supervision_code="BLDG-6")
        with self.assertRaises(MergeBlockedError):
            self.service.transition(self.admin, job["id"], "complete", {})
        # Same merge id with identical payload converges before completion.
        again = self.start_merge(source, retained, merge_id="merge-fixed")
        self.assertEqual(again["id"], job["id"])
        # Release the conflict and complete using the original data.
        data = dict(squatter["data"])
        data["supervision_code"] = "BLDG-6-OK"
        self.service.repository.update_entity(squatter["id"], squatter["version"], squatter["status"], data)
        job = self.service.transition(self.admin, job["id"], "complete", {})
        self.assertEqual(job["status"], "completed")
        # Retrying the very same start request after completion returns the job.
        converged = self.start_merge(source, retained, merge_id="merge-fixed")
        self.assertEqual(converged["id"], job["id"])
        self.assertEqual(converged["status"], "completed")

    def test_divergent_retained_target_is_rejected(self):
        source = self.equipment("BLDG-7")
        retained = self.equipment("CITY-7")
        job = self.start_merge(source, retained, merge_id="merge-7")
        self.service.transition(self.admin, job["id"], "complete", {})
        other = self.equipment("CITY-77")
        with self.assertRaises(ConflictError):
            self.start_merge(source, other, merge_id="merge-7b")

    # ------------------------------------------------------------------
    # Guards and roles
    # ------------------------------------------------------------------

    def test_merge_requires_admin(self):
        source = self.equipment("BLDG-8")
        retained = self.equipment("CITY-8")
        for role in ("viewer", "inspector", "dispatcher", "maintenance"):
            with self.assertRaises(PermissionDenied):
                self.service.start_device_merge(
                    Actor("u", role),
                    {"source_equipment_id": source["id"], "retained_equipment_id": retained["id"]},
                )

    def test_merge_validates_inputs(self):
        source = self.equipment("BLDG-9")
        with self.assertRaises(ValidationError):
            self.service.start_device_merge(
                self.admin,
                {"source_equipment_id": source["id"], "retained_equipment_id": source["id"]},
            )
        with self.assertRaises(ValidationError):
            self.service.start_device_merge(
                self.admin,
                {"source_equipment_id": "missing", "retained_equipment_id": source["id"]},
            )

    def test_block_action_is_system_only(self):
        source = self.equipment("BLDG-91")
        retained = self.equipment("CITY-91")
        job = self.start_merge(source, retained)
        with self.assertRaises(InvalidTransition):
            self.service.transition(self.admin, job["id"], "block", {})

    def test_import_batch_idempotency_key_converges(self):
        source = self.equipment("BLDG-92")
        retained = self.equipment("CITY-92")
        first = self.service.start_device_merge(
            self.admin,
            {"source_equipment_id": source["id"], "retained_equipment_id": retained["id"]},
            idempotency_key="batch-001",
        )
        second = self.service.start_device_merge(
            self.admin,
            {"source_equipment_id": source["id"], "retained_equipment_id": retained["id"]},
            idempotency_key="batch-001",
        )
        self.assertEqual(first["id"], second["id"])

    def test_new_history_rejected_on_merged_or_locked_device(self):
        source = self.equipment("BLDG-10")
        retained = self.equipment("CITY-10")
        self.start_merge(source, retained)
        with self.assertRaises(ConflictError):
            self.service.create(
                self.admin,
                "inspection",
                {"equipment_id": retained["id"], "scheduled_at": "2026-10-01T09:00:00Z", "cycle_days": 365},
            )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.admin,
                "maintenance",
                {"equipment_id": source["id"], "work_type": "routine", "planned_at": "2026-10-01"},
            )

    # ------------------------------------------------------------------
    # Offline synchronisation
    # ------------------------------------------------------------------

    def _complete_simple_merge(self, source, retained):
        job = self.start_merge(source, retained)
        return self.service.transition(self.admin, job["id"], "complete", {})

    def test_offline_alarm_maps_old_code_to_retained(self):
        source = self.equipment("OLD-A")
        retained = self.equipment("NEW-A", supervision_code="SUP-A")
        self._complete_simple_merge(source, retained)

        records = [
            {"source_id": "tablet-1", "record_id": "a1", "record_type": "alarm",
             "asset_no": "OLD-A", "code": "ENTRAP", "occurred_at": "2026-09-29T11:00:00Z"},
        ]
        result = self.service.merge_offline(self.admin, records)[0]
        self.assertEqual(result["status"], "synced")
        self.assertEqual(result["data"]["resolved_equipment_id"], retained["id"])
        alarm = self.service.get(result["data"]["created_entity_id"])
        self.assertEqual(alarm["data"]["equipment_id"], retained["id"])

    def test_offline_rescue_is_dispatched_once_under_old_code(self):
        dispatcher = Actor("dispatch-1", "dispatcher")
        source = self.equipment("OLD-B")
        retained = self.equipment("NEW-B")
        self._complete_simple_merge(source, retained)

        self.service.merge_offline(dispatcher, [{
            "source_id": "tab", "record_id": "a1", "record_type": "alarm",
            "asset_no": "OLD-B", "code": "X",
        }])
        rescue_record = {
            "source_id": "tab", "record_id": "r1", "record_type": "rescue_job",
            "asset_no": "OLD-B", "alarm_code": "X", "team": "Bravo",
        }
        first = self.service.merge_offline(dispatcher, [rescue_record])[0]
        self.assertEqual(first["status"], "synced")
        jobs = self.service.list("rescue_job")
        self.assertEqual(len(jobs), 1)

        # Replaying the identical record must not dispatch again.
        replay = self.service.merge_offline(dispatcher, [rescue_record])[0]
        self.assertEqual(replay["id"], first["id"])
        self.assertEqual(len(self.service.list("rescue_job")), 1)

        # Another tablet reporting the same event with an explicit shared
        # dedupe key is recorded as deduped, with no second dispatch.
        second = self.service.merge_offline(dispatcher, [{
            "source_id": "tab-2", "record_id": "r9", "record_type": "rescue_job",
            "asset_no": "OLD-B", "alarm_code": "X", "team": "Bravo",
            "dedupe_key": jobs[0]["data"]["dedupe_key"],
        }])[0]
        self.assertEqual(second["status"], "deduped")
        self.assertEqual(len(self.service.list("rescue_job")), 1)

    def test_offline_record_parked_then_replayed_after_merge(self):
        dispatcher = Actor("dispatch-2", "dispatcher")
        source = self.equipment("OLD-C")
        retained = self.equipment("NEW-C")
        job = self.start_merge(source, retained)

        parked = self.service.merge_offline(dispatcher, [{
            "source_id": "tab", "record_id": "a1", "record_type": "alarm",
            "equipment_id": source["id"], "code": "X",
        }])[0]
        self.assertEqual(parked["status"], "unmapped")
        # No alarm entity exists while parked.
        self.assertEqual(self.service.list("alarm"), [])

        self.service.transition(self.admin, job["id"], "complete", {})
        replayed = self.service.merge_offline(dispatcher, [{
            "source_id": "tab", "record_id": "a1", "record_type": "alarm",
            "equipment_id": source["id"], "code": "X",
        }])[0]
        self.assertEqual(replayed["status"], "synced")
        self.assertEqual(replayed["id"], parked["id"])
        self.assertEqual(replayed["data"]["resolved_equipment_id"], retained["id"])
        self.assertEqual(len(self.service.list("alarm")), 1)

    def test_offline_unknown_identifier_stays_unmapped(self):
        result = self.service.merge_offline(self.admin, [{
            "source_id": "tab", "record_id": "a1", "record_type": "alarm",
            "asset_no": "GHOST", "code": "X",
        }])[0]
        self.assertEqual(result["status"], "unmapped")


class MergeRulesTest(unittest.TestCase):
    def _equipment(self, equipment_id, asset_no=None, supervision_code=None,
                   status="in_service"):
        data = {}
        if asset_no:
            data["asset_no"] = asset_no
        if supervision_code:
            data["supervision_code"] = supervision_code
        return {"id": equipment_id, "kind": "equipment", "status": status, "data": data}

    def test_guard_detects_cross_namespace_occupancy(self):
        source = self._equipment("s", asset_no="A1")
        retained = self._equipment("r", asset_no="A2")
        other = self._equipment("q", asset_no="Q9", supervision_code="A1")
        reasons = merge_guard_reasons(source, retained, [source, retained, other], [], set())
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0]["reason"], "code_occupied")
        self.assertEqual(reasons[0]["occupying_field"], "supervision_code")

    def test_guard_detects_only_new_alarms(self):
        source = self._equipment("s", asset_no="A1")
        retained = self._equipment("r", asset_no="A2")
        baseline = {"alarm-old"}
        alarms = [
            {"id": "alarm-old", "kind": "alarm", "status": "received",
             "data": {"equipment_id": "s", "code": "K"}},
            {"id": "alarm-new", "kind": "alarm", "status": "dispatched",
             "data": {"equipment_id": "r", "code": "K2"}},
        ]
        reasons = merge_guard_reasons(source, retained, [source, retained], alarms, baseline)
        self.assertEqual([r["alarm_id"] for r in reasons], ["alarm-new"])

    def test_closed_new_alarm_does_not_block(self):
        source = self._equipment("s", asset_no="A1")
        retained = self._equipment("r", asset_no="A2")
        alarms = [
            {"id": "alarm-closed", "kind": "alarm", "status": "closed",
             "data": {"equipment_id": "s", "code": "K"}},
        ]
        self.assertEqual(merge_guard_reasons(source, retained, [source, retained], alarms, set()), [])


if __name__ == "__main__":
    unittest.main()
