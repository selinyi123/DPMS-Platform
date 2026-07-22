import asyncio
import copy
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "httpx" not in sys.modules and importlib.util.find_spec("httpx") is None:
    httpx = types.ModuleType("httpx")
    httpx.AsyncBaseTransport = object
    httpx.AsyncClient = object
    httpx.TransportError = RuntimeError
    sys.modules["httpx"] = httpx

from app.action_plan import compute_bilibili_api_config_hash  # noqa: E402
from app.bilibili.preflight import (  # noqa: E402
    API_PREFLIGHT_KIND,
    hash_preflight_observation,
    run_readonly_api_preflight,
    validate_preflight_observation,
)


DYNAMIC_ID = "123456789012345678"
FOLLOW_HANDLE = "@ASUS华硕官方UP"


def validation_kwargs(*, execution_revision=9, actions=None, follow_handle=FOLLOW_HANDLE):
    required_actions = (
        ("followed", "liked", "commented", "reposted")
        if actions is None
        else actions
    )
    return {
        "expected_dynamic_id": DYNAMIC_ID,
        "expected_actions": required_actions,
        "expected_execution_revision": execution_revision,
        "expected_config_hash": compute_bilibili_api_config_hash(execution_revision),
        "expected_follow_handle": follow_handle,
    }


def detail_payload():
    return {
        "code": 0,
        "data": {
            "item": {
                "id_str": DYNAMIC_ID,
                "type": "DYNAMIC_TYPE_WORD",
                "basic": {"comment_id_str": DYNAMIC_ID},
                "modules": {
                    "module_author": {
                        "mid": 10086,
                        "name": FOLLOW_HANDLE[1:],
                    },
                    "module_dynamic": {"desc": {"rich_text_nodes": []}},
                },
            }
        },
    }


class FakeReadOnlyClient:
    instances = []

    def __init__(self, cookie_header, config=None):
        self.cookie_header = cookie_header
        self.config = config
        self.calls = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def check_login(self):
        self.calls.append("GET:login")
        return True

    async def get_dynamic_detail(self, dynamic_id):
        self.calls.append(f"GET:detail:{dynamic_id}")
        return detail_payload()


class BilibiliApiPreflightTests(unittest.TestCase):
    def setUp(self):
        FakeReadOnlyClient.instances.clear()

    def run_preflight(self, *, follow_handle=FOLLOW_HANDLE):
        revision = 9
        config_hash = compute_bilibili_api_config_hash(revision)
        return asyncio.run(
            run_readonly_api_preflight(
                cookie_header="SESSDATA=secret-never-persist",
                dynamic_id=DYNAMIC_ID,
                required_actions=("followed", "liked", "commented", "reposted"),
                execution_revision=revision,
                config_hash=config_hash,
                expected_follow_handle=follow_handle,
                client_factory=FakeReadOnlyClient,
            )
        )

    def test_preflight_uses_only_read_methods_and_persists_exact_identity(self):
        evidence = self.run_preflight()
        client = FakeReadOnlyClient.instances[-1]
        self.assertEqual(
            client.calls,
            ["GET:login", f"GET:detail:{DYNAMIC_ID}"],
        )
        self.assertEqual(evidence.observation["probe_kind"], API_PREFLIGHT_KIND)
        self.assertFalse(evidence.observation["side_effects"])
        self.assertEqual(
            evidence.observation["target_identity"],
            {
                "verified": True,
                "dynamic_id": DYNAMIC_ID,
                "author_uid": 10086,
                "author_handle": FOLLOW_HANDLE,
            },
        )
        self.assertEqual(evidence.observation["follow_target_handle"], FOLLOW_HANDLE)
        self.assertEqual(evidence.observation_hash, hash_preflight_observation(evidence.observation))
        self.assertNotIn("secret-never-persist", json.dumps(evidence.observation, ensure_ascii=False))

    def test_wrong_follow_target_fails_before_any_mutation_capability_exists(self):
        with self.assertRaisesRegex(RuntimeError, "follow_target_mismatch"):
            self.run_preflight(follow_handle="@另一个账号")

    def test_observation_hash_and_identity_are_revalidated(self):
        evidence = self.run_preflight()
        tampered = copy.deepcopy(evidence.observation)
        tampered["target_identity"]["author_handle"] = "@另一个账号"
        with self.assertRaisesRegex(
            ValueError,
            "observation_invalid|target_identity_invalid|follow_target_mismatch",
        ):
            validate_preflight_observation(
                tampered,
                **validation_kwargs(),
            )
        self.assertNotEqual(evidence.observation_hash, hash_preflight_observation(tampered))

    def test_account_revision_is_part_of_the_observation_contract(self):
        evidence = self.run_preflight()
        with self.assertRaisesRegex(ValueError, "execution_revision_mismatch"):
            validate_preflight_observation(
                evidence.observation,
                **validation_kwargs(execution_revision=10),
            )

    def test_observation_schema_is_exact_and_rejects_hidden_data(self):
        evidence = self.run_preflight()
        for mutation in (
            lambda value: value.update(cookie="must-not-be-recorded"),
            lambda value: value.pop("target_type"),
        ):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(evidence.observation)
                mutation(changed)
                with self.assertRaisesRegex(ValueError, "observation_schema_invalid"):
                    validate_preflight_observation(changed, **validation_kwargs())

    def test_required_actions_order_uniqueness_and_api_mapping_are_exact(self):
        evidence = self.run_preflight()
        invalid_expected_actions = (
            (),
            ("liked", "followed"),
            ("followed", "followed"),
            ("followed", "unsupported"),
        )
        for actions in invalid_expected_actions:
            with self.subTest(expected_actions=actions):
                with self.assertRaisesRegex(ValueError, "expected_actions_invalid"):
                    validate_preflight_observation(
                        evidence.observation,
                        **validation_kwargs(actions=actions),
                    )

        for field, value in (
            ("required_actions", ["followed", "commented", "liked", "reposted"]),
            ("api_actions", ["follow", "comment", "like", "repost"]),
            (
                "capability_checks",
                {"followed": True, "liked": True, "commented": True},
            ),
        ):
            with self.subTest(field=field):
                changed = copy.deepcopy(evidence.observation)
                changed[field] = value
                with self.assertRaisesRegex(ValueError, "observation_invalid"):
                    validate_preflight_observation(changed, **validation_kwargs())

    def test_forwarded_or_incomplete_target_identity_is_rejected(self):
        evidence = self.run_preflight()
        mutations = (
            ("forwarded", lambda value: value.update(target_type=1), "forwarded_origin"),
            ("zero_uid", lambda value: value.update(target_uid=0), "target_invalid"),
            (
                "malformed_handle",
                lambda value: (
                    value.update(author_handle="ASUS华硕官方UP"),
                    value["target_identity"].update(author_handle="ASUS华硕官方UP"),
                    value.update(follow_target_handle="ASUS华硕官方UP"),
                ),
                "target_identity_invalid",
            ),
            (
                "identity_uid_mismatch",
                lambda value: value["target_identity"].update(author_uid=10087),
                "target_identity_invalid",
            ),
        )
        for name, mutation, code in mutations:
            with self.subTest(name=name):
                changed = copy.deepcopy(evidence.observation)
                mutation(changed)
                with self.assertRaisesRegex(ValueError, code):
                    validate_preflight_observation(changed, **validation_kwargs())

    def test_comment_target_requires_nonempty_rid_and_positive_integer_type(self):
        evidence = self.run_preflight()
        for field, value in (
            ("comment_rid_str", ""),
            ("comment_type", 0),
            ("comment_type", True),
        ):
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(evidence.observation)
                changed[field] = value
                with self.assertRaisesRegex(ValueError, "comment_target_invalid"):
                    validate_preflight_observation(changed, **validation_kwargs())


if __name__ == "__main__":
    unittest.main(verbosity=2)
