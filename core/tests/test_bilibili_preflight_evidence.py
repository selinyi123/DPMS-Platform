import copy
import unittest

from app.action_plan import compute_bilibili_api_config_hash
from app.services.bilibili_preflight_evidence import (
    API_PREFLIGHT_KIND,
    BilibiliPreflightEvidenceError,
    extract_bilibili_dynamic_id,
    hash_preflight_observation,
    validate_preflight_observation,
    validate_preflight_observation_binding,
)


DYNAMIC_ID = "1234567890123456789"
FOLLOW_TARGET = "@ASUS华硕官方UP"


def complete_observation(execution_revision: int = 7) -> dict:
    actions = ["followed", "liked", "commented", "reposted"]
    return {
        "version": 1,
        "probe_kind": API_PREFLIGHT_KIND,
        "execution_path_id": "bilibili_api_v2",
        "preflight_contract_version": 1,
        "execution_revision": execution_revision,
        "config_hash": compute_bilibili_api_config_hash(execution_revision),
        "side_effects": False,
        "account_authenticated": True,
        "api_preflight_complete": True,
        "requested_dynamic_id": DYNAMIC_ID,
        "observed_dynamic_id": DYNAMIC_ID,
        "target_type": 2,
        "target_uid": 987654321,
        "author_handle": FOLLOW_TARGET,
        "follow_target_handle": FOLLOW_TARGET,
        "target_identity": {
            "verified": True,
            "dynamic_id": DYNAMIC_ID,
            "author_uid": 987654321,
            "author_handle": FOLLOW_TARGET,
        },
        "comment_rid_str": DYNAMIC_ID,
        "comment_type": 17,
        "required_actions": actions,
        "api_actions": ["follow", "like", "comment", "repost"],
        "capability_checks": {action: True for action in actions},
    }


def validate(value: dict, execution_revision: int = 7):
    return validate_preflight_observation(
        value,
        expected_dynamic_id=DYNAMIC_ID,
        expected_actions=("followed", "liked", "commented", "reposted"),
        expected_execution_revision=execution_revision,
        expected_config_hash=compute_bilibili_api_config_hash(execution_revision),
        expected_follow_handle=FOLLOW_TARGET,
    )


class BilibiliPreflightEvidenceTests(unittest.TestCase):
    def test_exact_observation_is_hash_addressed(self):
        observation = complete_observation()

        evidence = validate(observation)

        self.assertEqual(hash_preflight_observation(observation), evidence.observation_hash)
        self.assertEqual(FOLLOW_TARGET, evidence.observation["target_identity"]["author_handle"])

    def test_follow_target_identity_mismatch_fails_closed(self):
        observation = complete_observation()
        observation["target_identity"]["author_handle"] = "@另一个账号"
        observation["author_handle"] = "@另一个账号"

        with self.assertRaisesRegex(
            BilibiliPreflightEvidenceError,
            "bilibili_api_preflight_follow_target_mismatch",
        ):
            validate(observation)

    def test_credential_revision_invalidates_observation(self):
        observation = complete_observation(execution_revision=7)

        with self.assertRaisesRegex(
            BilibiliPreflightEvidenceError,
            "bilibili_api_preflight_execution_revision_mismatch",
        ):
            validate(observation, execution_revision=8)

    def test_side_effecting_or_incomplete_observation_is_rejected(self):
        for field, value in (
            ("side_effects", True),
            ("account_authenticated", False),
            ("api_preflight_complete", False),
        ):
            with self.subTest(field=field):
                observation = complete_observation()
                observation[field] = value
                with self.assertRaises(BilibiliPreflightEvidenceError):
                    validate(observation)

    def test_schema_rejects_hidden_extra_data(self):
        observation = complete_observation()
        observation["cookie"] = "must-not-be-recorded"

        with self.assertRaisesRegex(
            BilibiliPreflightEvidenceError,
            "bilibili_api_preflight_observation_schema_invalid",
        ):
            validate(observation)

    def test_action_order_and_capabilities_are_exact(self):
        observation = complete_observation()
        observation["required_actions"] = list(reversed(observation["required_actions"]))

        with self.assertRaises(BilibiliPreflightEvidenceError):
            validate(observation)

    def test_hash_changes_when_identity_changes(self):
        observation = complete_observation()
        changed = copy.deepcopy(observation)
        changed["target_identity"]["author_uid"] += 1

        self.assertNotEqual(
            hash_preflight_observation(observation),
            hash_preflight_observation(changed),
        )

    def test_source_and_evidence_hashes_must_both_match_canonical_json(self):
        observation = complete_observation()
        observation_hash = hash_preflight_observation(observation)
        common = {
            "source_observation_kind": API_PREFLIGHT_KIND,
            "source_observation_hash": observation_hash,
            "evidence_observation_kind": API_PREFLIGHT_KIND,
            "evidence_observation_hash": observation_hash,
            "expected_dynamic_id": DYNAMIC_ID,
            "expected_actions": ("followed", "liked", "commented", "reposted"),
            "expected_execution_revision": 7,
            "expected_config_hash": compute_bilibili_api_config_hash(7),
            "expected_follow_handle": FOLLOW_TARGET,
        }

        validate_preflight_observation_binding(observation, **common)
        for field in ("source_observation_hash", "evidence_observation_hash"):
            with self.subTest(field=field):
                changed = dict(common)
                changed[field] = "0" * 64
                with self.assertRaisesRegex(
                    BilibiliPreflightEvidenceError,
                    "bilibili_api_preflight_observation_hash_mismatch",
                ):
                    validate_preflight_observation_binding(observation, **changed)

    def test_source_and_evidence_kinds_must_both_be_api_preflight(self):
        observation = complete_observation()
        observation_hash = hash_preflight_observation(observation)

        with self.assertRaisesRegex(
            BilibiliPreflightEvidenceError,
            "bilibili_api_preflight_observation_kind_mismatch",
        ):
            validate_preflight_observation_binding(
                observation,
                source_observation_kind="selector_snapshot_v1",
                source_observation_hash=observation_hash,
                evidence_observation_kind=API_PREFLIGHT_KIND,
                evidence_observation_hash=observation_hash,
                expected_dynamic_id=DYNAMIC_ID,
                expected_actions=("followed", "liked", "commented", "reposted"),
                expected_execution_revision=7,
                expected_config_hash=compute_bilibili_api_config_hash(7),
                expected_follow_handle=FOLLOW_TARGET,
            )

    def test_dynamic_id_extraction_is_exact(self):
        self.assertEqual(
            DYNAMIC_ID,
            extract_bilibili_dynamic_id(f"https://www.bilibili.com/opus/{DYNAMIC_ID}"),
        )
        self.assertEqual(
            DYNAMIC_ID,
            extract_bilibili_dynamic_id(
                f"canonical://bilibili/dynamic/opus_{DYNAMIC_ID}"
            ),
        )
        for target in (
            "https://www.bilibili.com/video/BV1example",
            "http://t.bilibili.com/123456789012",
            "https://t.bilibili.com:444/123456789012",
            "https://t.bilibili.com:443@evil.example/123456789012",
            f"canonical://bilibili:123/dynamic/{DYNAMIC_ID}",
            f"canonical://bilibili:not-a-port/dynamic/{DYNAMIC_ID}",
            f"canonical://operator@bilibili/dynamic/{DYNAMIC_ID}",
            "https://t.bilibili.com/" + "\uff11" * 12,
            "https://t.bilibili.com/" + "1" * 21,
            "\uff11" * 12,
            "1" * 21,
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    BilibiliPreflightEvidenceError,
                    "bilibili_dynamic_target_required",
                ):
                    extract_bilibili_dynamic_id(target)

    def test_dynamic_id_extraction_fails_closed_for_malformed_canonical_authority(self):
        for target in (
            f"canonical://[bilibili/dynamic/{DYNAMIC_ID}",
            f"canonical://bilibili／evil/dynamic/{DYNAMIC_ID}",
            f"canonical://bilibili：443/dynamic/{DYNAMIC_ID}",
        ):
            with self.subTest(target=target):
                with self.assertRaisesRegex(
                    BilibiliPreflightEvidenceError,
                    "bilibili_dynamic_target_required",
                ):
                    extract_bilibili_dynamic_id(target)


if __name__ == "__main__":
    unittest.main()
