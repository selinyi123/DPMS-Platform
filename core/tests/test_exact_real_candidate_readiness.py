import base64
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


os.environ.setdefault(
    "ENCRYPTION_KEY",
    base64.b64encode(os.urandom(32)).decode(),
)
os.environ.setdefault("UPDATE_SECRET", "test-secret")
os.environ.setdefault("ADMIN_TOKEN", "test-admin-token")

from app.services import real_run_readiness as readiness  # noqa: E402


class ExactRealCandidateReadinessTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def lottery(**overrides):
        row = {
            "id": 11,
            "platform": "bilibili",
            "raw_url": "https://www.bilibili.com/opus/123456789?secret=no",
            "status": "pending",
            "active_runs": 0,
            "dry_success": 1,
            "shadow_success": 1,
            "failed_runs": 0,
        }
        row.update(overrides)
        return row

    @staticmethod
    def allowed_readiness():
        return {
            "allowed": True,
            "blockers": [],
            "action_plan_ready": True,
            "rule_snapshot_ready": True,
            "execution_evidence_bound": True,
            "probe_ready": True,
            "shadow_ready": True,
            "oauth_dry_run_ready": False,
        }

    async def observe(self, row, *, candidate_truncated=False):
        account_candidates = readiness.AccountScopedReadinessAccountCandidates(
            {
                "bilibili": [
                    {"account_id": 7, "platform": "bilibili"}
                ]
            },
            truncated_platforms=(
                frozenset({"bilibili"})
                if candidate_truncated
                else frozenset()
            ),
        )
        prefilter = readiness.AccountScopedReadinessCandidatePrefilter(
            account_ids_by_lottery={11: frozenset({7})}
        )
        evaluated = {
            11: {
                "account_id": 7,
                "readiness": self.allowed_readiness(),
            }
        }
        module = SimpleNamespace(
            strategy_target_is_real_valid=lambda _target: True
        )
        with patch.object(
            readiness,
            "load_account_scoped_readiness_account_candidates",
            new=AsyncMock(return_value=account_candidates),
        ), patch.object(
            readiness,
            "load_account_scoped_readiness_candidate_prefilter",
            new=AsyncMock(return_value=prefilter),
        ), patch.object(
            readiness,
            "evaluate_account_scoped_real_run_readiness_batch",
            new=AsyncMock(return_value=evaluated),
        ), patch.object(
            readiness,
            "get_platform_module",
            return_value=module,
        ), patch.object(
            readiness,
            "validate_lottery_target",
            return_value=object(),
        ):
            return await readiness.evaluate_exact_real_candidate_observation(
                [row]
            )

    async def test_exact_pair_is_ready_and_projection_is_payload_free(self):
        result = await self.observe(self.lottery())

        self.assertTrue(result["available"])
        self.assertTrue(result["ready"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(
            {
                "lottery_id": result["candidate"]["lottery_id"],
                "account_id": result["candidate"]["account_id"],
            },
            {"lottery_id": 11, "account_id": 7},
        )
        serialized = json.dumps(result, sort_keys=True)
        self.assertNotIn("raw_url", serialized)
        self.assertNotIn("secret=no", serialized)
        self.assertNotIn("evidence_path", serialized)
        self.assertNotIn("error", serialized)

    async def test_allowed_evidence_without_dry_or_shadow_is_not_real_candidate(self):
        for missing_field in ("dry_success", "shadow_success"):
            with self.subTest(missing_field=missing_field):
                result = await self.observe(
                    self.lottery(**{missing_field: 0})
                )
                self.assertTrue(result["available"])
                self.assertFalse(result["ready"])
                self.assertEqual(result["candidate_count"], 0)
                expected = (
                    "dry_validation_needed"
                    if missing_field == "dry_success"
                    else "shadow_validation_needed"
                )
                self.assertEqual(result["blocker_counts"][expected], 1)

    async def test_active_run_and_failure_limit_remain_fail_closed(self):
        for overrides, blocker in (
            ({"active_runs": 1}, "active_run_in_progress"),
            ({"failed_runs": 3}, "autopilot_failure_limit_reached"),
        ):
            with self.subTest(blocker=blocker):
                result = await self.observe(self.lottery(**overrides))
                self.assertFalse(result["ready"])
                self.assertEqual(result["blocker_counts"][blocker], 1)

    async def test_truncated_account_source_cannot_claim_queue_candidate(self):
        result = await self.observe(
            self.lottery(),
            candidate_truncated=True,
        )

        self.assertFalse(result["ready"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertTrue(result["observation_truncated"])
        self.assertEqual(
            result["blocker_code"],
            "autopilot_exact_candidate_observation_truncated",
        )

    async def test_account_source_excludes_active_leases_and_secret_columns(self):
        fetch_all = AsyncMock(return_value=[{
            "account_id": 7,
            "platform": "bilibili",
        }])
        with patch.object(
            readiness.database,
            "fetch_all",
            new=fetch_all,
        ):
            candidates = (
                await readiness
                .load_account_scoped_readiness_account_candidates(
                    ["bilibili"],
                    exclude_active_leases=True,
                )
            )

        query = fetch_all.await_args.args[0]
        select_clause = query.split("FROM accounts a", 1)[0]
        self.assertIn("SELECT a.id AS account_id, a.platform", select_clause)
        self.assertNotIn("encrypted_credential", select_clause)
        self.assertIn("FROM account_operation_leases lease", query)
        self.assertIn("lease.released_at IS NULL", query)
        self.assertIn("lease.expires_at > NOW()", query)
        self.assertEqual(candidates["bilibili"], [{
            "account_id": 7,
            "platform": "bilibili",
        }])

    def test_unknown_blocker_text_is_not_exposed(self):
        codes = readiness._safe_readiness_blocker_codes({
            "blockers": [
                "DedeUserID",
                "credential=must-not-leak",
                "exact_execution_evidence_required",
            ]
        })

        self.assertEqual(codes, [
            "account_scoped_real_run_readiness_unavailable",
            "exact_execution_evidence_required",
        ])
        self.assertNotIn("must-not-leak", json.dumps(codes))


if __name__ == "__main__":
    unittest.main()
