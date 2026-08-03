import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.prepare_contract_database import (  # noqa: E402
    _validated_admin_database_url,
    _validated_database_url,
)


class ContractDatabaseBootstrapSafetyTests(unittest.TestCase):
    def _environment(self, url: str) -> dict[str, str]:
        return {
            "DATABASE_URL": url,
            "DPMS_MYSQL_INTEGRATION": "1",
            "DPMS_CONTRACT_DATABASE_BOOTSTRAP": "1",
        }

    def test_accepts_loopback_disposable_contract_database(self):
        url = (
            "mysql+aiomysql://contract:secret@127.0.0.1:3306/"
            "dpms_contract_ci?charset=utf8mb4"
        )
        with patch.dict(os.environ, self._environment(url), clear=True):
            self.assertEqual(_validated_database_url(), url)

    def test_accepts_explicit_disposable_container_name(self):
        url = (
            "mysql+aiomysql://contract:secret@"
            "dpms-contract-mysql-test:3306/dpms_contract_local"
        )
        with patch.dict(os.environ, self._environment(url), clear=True):
            self.assertEqual(_validated_database_url(), url)

    def test_rejects_nonlocal_database_even_with_contract_name(self):
        url = (
            "mysql+aiomysql://contract:secret@db.example.com:3306/"
            "dpms_contract_ci"
        )
        with patch.dict(os.environ, self._environment(url), clear=True):
            with self.assertRaises(SystemExit):
                _validated_database_url()

    def test_rejects_remote_fqdn_that_mimics_container_prefix(self):
        url = (
            "mysql+aiomysql://contract:secret@"
            "dpms-contract-mysql-prod.example.com:3306/"
            "dpms_contract_ci"
        )
        with patch.dict(os.environ, self._environment(url), clear=True):
            with self.assertRaises(SystemExit):
                _validated_database_url()

    def test_rejects_malformed_disposable_container_label(self):
        url = (
            "mysql+aiomysql://contract:secret@"
            "dpms-contract-mysql--invalid:3306/dpms_contract_ci"
        )
        with patch.dict(os.environ, self._environment(url), clear=True):
            with self.assertRaises(SystemExit):
                _validated_database_url()

    def test_rejects_application_database_name(self):
        url = "mysql+aiomysql://user:secret@127.0.0.1:3306/lottery"
        with patch.dict(os.environ, self._environment(url), clear=True):
            with self.assertRaises(SystemExit):
                _validated_database_url()

    def test_requires_both_explicit_integration_flags(self):
        url = "mysql+aiomysql://user:secret@127.0.0.1:3306/dpms_contract_ci"
        with patch.dict(
            os.environ,
            {"DATABASE_URL": url, "DPMS_MYSQL_INTEGRATION": "1"},
            clear=True,
        ):
            with self.assertRaises(SystemExit):
                _validated_database_url()

    def test_accepts_admin_connection_on_same_disposable_host_and_port(self):
        application_url = (
            "mysql+aiomysql://contract:secret@"
            "dpms-contract-mysql-test:3306/dpms_contract_local"
        )
        admin_url = (
            "mysql+aiomysql://root:secret@"
            "dpms-contract-mysql-test:3306/mysql"
        )
        with patch.dict(
            os.environ,
            {"DPMS_CONTRACT_DATABASE_ADMIN_URL": admin_url},
            clear=True,
        ):
            self.assertEqual(
                _validated_admin_database_url(application_url),
                admin_url,
            )

    def test_rejects_admin_connection_on_different_container(self):
        application_url = (
            "mysql+aiomysql://contract:secret@"
            "dpms-contract-mysql-test:3306/dpms_contract_local"
        )
        admin_url = (
            "mysql+aiomysql://root:secret@"
            "dpms-contract-mysql-other:3306/mysql"
        )
        with patch.dict(
            os.environ,
            {"DPMS_CONTRACT_DATABASE_ADMIN_URL": admin_url},
            clear=True,
        ):
            with self.assertRaises(SystemExit):
                _validated_admin_database_url(application_url)

    def test_rejects_admin_connection_on_different_port(self):
        application_url = (
            "mysql+aiomysql://contract:secret@127.0.0.1:3306/"
            "dpms_contract_local"
        )
        admin_url = (
            "mysql+aiomysql://root:secret@127.0.0.1:3307/mysql"
        )
        with patch.dict(
            os.environ,
            {"DPMS_CONTRACT_DATABASE_ADMIN_URL": admin_url},
            clear=True,
        ):
            with self.assertRaises(SystemExit):
                _validated_admin_database_url(application_url)

    def test_rejects_admin_connection_outside_mysql_system_schema(self):
        application_url = (
            "mysql+aiomysql://contract:secret@127.0.0.1:3306/"
            "dpms_contract_local"
        )
        admin_url = (
            "mysql+aiomysql://root:secret@127.0.0.1:3306/"
            "dpms_contract_local"
        )
        with patch.dict(
            os.environ,
            {"DPMS_CONTRACT_DATABASE_ADMIN_URL": admin_url},
            clear=True,
        ):
            with self.assertRaises(SystemExit):
                _validated_admin_database_url(application_url)


if __name__ == "__main__":
    unittest.main()
