import unittest
from pathlib import Path


SERVICE_ROOT = Path(__file__).resolve().parents[1]


def _first_existing(*paths: Path) -> Path:
    for path in paths:
        if path.is_file():
            return path
    raise FileNotFoundError(
        "Expected one of: " + ", ".join(str(path) for path in paths)
    )


class RedisDurabilityContractTests(unittest.TestCase):
    def test_compose_redis_uses_aof_and_a_named_data_volume(self):
        compose_path = _first_existing(
            SERVICE_ROOT / "docker-compose.yml",
            SERVICE_ROOT.parent / "docker-compose.yml",
        )
        compose = compose_path.read_text(encoding="utf-8")
        redis_service = compose.split("\n  redis:\n", 1)[1].split(
            "\nnetworks:\n",
            1,
        )[0]
        redis_entrypoint = _first_existing(
            SERVICE_ROOT / "docker" / "redis" / "entrypoint.sh",
            SERVICE_ROOT.parent
            / "docker"
            / "redis"
            / "entrypoint.sh",
        ).read_text(encoding="utf-8")
        volumes = compose.split("\nvolumes:\n", 1)[1]

        self.assertIn("--appendonly", redis_entrypoint)
        self.assertIn("--appendfsync", redis_entrypoint)
        self.assertIn("--protected-mode yes", redis_entrypoint)
        self.assertIn('acl_file="/tmp/users.acl"', redis_entrypoint)
        self.assertNotIn('acl_file="/data/users.acl"', redis_entrypoint)
        self.assertIn("- redis-data:/data", redis_service)
        self.assertIn("redis-data:", volumes)

        phase_one_acl = redis_entrypoint.split(
            'cat >"$acl_tmp" <<EOF',
            1,
        )[1].split("EOF", 1)[0]
        self.assertIn(
            "user default on nopass ~* &* +@all",
            phase_one_acl,
        )
        self.assertIn("+info", phase_one_acl)
        self.assertIn("+acl|whoami", phase_one_acl)
        self.assertIn(
            'bootstrap_identity="$(bootstrap_cli ACL WHOAMI',
            redis_entrypoint,
        )
        self.assertIn(
            '[ "$bootstrap_identity" = "bootstrap" ]',
            redis_entrypoint,
        )
        self.assertLess(
            redis_entrypoint.index("loading:0"),
            redis_entrypoint.index('XGROUP CREATE "$stream_key"'),
        )

    def test_compose_redis_uses_named_acl_users_and_fail_closed_clients(self):
        compose_path = _first_existing(
            SERVICE_ROOT / "docker-compose.yml",
            SERVICE_ROOT.parent / "docker-compose.yml",
        )
        compose = compose_path.read_text(encoding="utf-8")
        redis_service = compose.split("\n  redis:\n", 1)[1].split(
            "\nnetworks:\n",
            1,
        )[0]
        core_service = compose.split("\n  core-api:\n", 1)[1].split(
            "\n  worker:\n",
            1,
        )[0]
        worker_service = compose.split("\n  worker:\n", 1)[1].split(
            "\n  mysql:\n",
            1,
        )[0]
        redis_entrypoint = _first_existing(
            SERVICE_ROOT / "docker" / "redis" / "entrypoint.sh",
            SERVICE_ROOT.parent
            / "docker"
            / "redis"
            / "entrypoint.sh",
        ).read_text(encoding="utf-8")

        self.assertIn("docker/redis/entrypoint.sh", redis_service)
        self.assertIn("REDIS_HEALTH_PASSWORD", redis_service)
        self.assertIn("REDIS_GROUP_ADMIN_PASSWORD", redis_service)
        self.assertIn("DEPLOYMENT_MODE", redis_service)
        self.assertIn("consumer-groups.tsv", redis_service)
        self.assertIn("--user health", redis_service)
        self.assertIn("ACL WHOAMI", redis_service)
        health_acl = next(
            line
            for line in redis_entrypoint.splitlines()
            if line.startswith("user health on")
        )
        self.assertIn("+acl|whoami", health_acl.casefold())
        self.assertIn("read_only: true", redis_service)
        self.assertIn("no-new-privileges:true", redis_service)
        self.assertIn("cap_drop:", redis_service)
        self.assertIn("- ALL", redis_service)
        self.assertIn("--reuid redis", redis_entrypoint)
        self.assertIn("chown -h redis:redis", redis_entrypoint)
        self.assertIn(
            "validate_production_password",
            redis_entrypoint,
        )
        for password_name in (
            "REDIS_CORE_PASSWORD",
            "REDIS_WORKER_PASSWORD",
            "REDIS_HEALTH_PASSWORD",
            "REDIS_GROUP_ADMIN_PASSWORD",
        ):
            self.assertIn(
                f"validate_production_password \\\n  {password_name}",
                redis_entrypoint,
            )
        self.assertIn(
            "Redis ACL passwords must be mutually distinct",
            redis_entrypoint,
        )
        self.assertIn("user bootstrap off", redis_entrypoint)
        self.assertIn("user group-admin on", redis_entrypoint)
        core_acl = next(
            line
            for line in redis_entrypoint.splitlines()
            if line.startswith("user core on")
        )
        worker_acl = next(
            line
            for line in redis_entrypoint.splitlines()
            if line.startswith("user worker on")
        )
        self.assertNotIn("+xgroup|create", core_acl.casefold())
        self.assertNotIn("+xgroup|destroy", core_acl.casefold())
        self.assertNotIn("+xgroup|setid", core_acl.casefold())
        self.assertIn("+xgroup|delconsumer", core_acl.casefold())
        core_delconsumer_selector = next(
            selector
            for selector in core_acl.casefold().split(") ")
            if selector.startswith("(+xgroup|delconsumer ")
        )
        self.assertIn("~notify_events", core_delconsumer_selector)
        self.assertIn(
            "~discovery_scan_requests:v1:*",
            core_delconsumer_selector,
        )
        self.assertNotIn("~lottery_tasks", core_delconsumer_selector)
        self.assertNotIn("~login_requests", core_delconsumer_selector)
        self.assertNotIn("+xgroup|create", worker_acl.casefold())
        self.assertIn("+xgroup|delconsumer", worker_acl.casefold())
        self.assertNotIn(
            "(+eval +xadd",
            worker_acl.casefold(),
        )
        self.assertIn(
            "(+xadd ~notify_events ~failed_task_messages",
            worker_acl.casefold(),
        )
        self.assertIn("REDIS_USERNAME: core", core_service)
        self.assertIn(
            'REDIS_ACL_PREFLIGHT_REQUIRED: "true"',
            core_service,
        )
        self.assertIn("REDIS_USERNAME: worker", compose)
        self.assertIn(
            'REDIS_ACL_PREFLIGHT_REQUIRED: "true"',
            compose,
        )

    def test_core_startup_reconciles_epoch_before_accepting_requests(self):
        main_path = _first_existing(
            SERVICE_ROOT / "app" / "main.py",
            SERVICE_ROOT.parent / "core" / "app" / "main.py",
        )
        main_source = main_path.read_text(encoding="utf-8")

        reconcile_call = main_source.index(
            "await reconcile_owned_stream_epochs("
        )
        app_ready = main_source.index(
            "background_tasks = _start_core_background_tasks("
        )
        self.assertLess(reconcile_call, app_ready)


if __name__ == "__main__":
    unittest.main()
