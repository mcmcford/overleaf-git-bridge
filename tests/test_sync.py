from __future__ import annotations

import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bson import ObjectId

import sync


class SyncTests(unittest.TestCase):
    def make_args(self, **overrides):
        values = {
            "mongo_uri": None,
            "mongo_host": "mongo.internal",
            "mongo_port": 27017,
            "mongo_username": None,
            "mongo_password": None,
            "mongo_auth_db": "admin",
            "mongo_auth_mechanism": None,
            "mongo_tls": False,
            "connect_timeout_ms": 5000,
            "db_name": "sharelatex",
            "output_dir": "export",
            "project_id": [],
            "limit": 0,
            "include_raw": False,
            "dry_run": False,
            "check_connection": False,
            "push": False,
            "git_repo_dir": ".",
            "git_remote_name": "origin",
            "git_remote_url": "git@gitlab.example.com:group/overleaf-export.git",
            "git_branch": "main",
            "git_ssh_key_path": "C:/keys/gitlab-deploy",
            "git_access_token": None,
            "git_http_username": "",
            "git_commit_name": "Overleaf Bot",
            "git_commit_email": "bot@example.com",
            "git_commit_message": None,
        }
        values.update(overrides)
        return Namespace(**values)

    def test_build_mongo_uri_with_credentials(self):
        args = self.make_args(
            mongo_username="bridge-user",
            mongo_password="pa:ss word",
            mongo_auth_db="admin",
            mongo_auth_mechanism="SCRAM-SHA-256",
            mongo_tls=True,
        )

        uri = sync.build_mongo_uri(args)

        self.assertEqual(
            uri,
            "mongodb://bridge-user:pa%3Ass+word@mongo.internal:27017/"
            "?authSource=admin&authMechanism=SCRAM-SHA-256&tls=true",
        )

    def test_check_connection_pings_and_reads_projects(self):
        args = self.make_args()
        project_id = "693616a50fa89c23ae8b1e99"
        client = Mock()
        database = Mock()
        database.list_collection_names.return_value = ["projects", "users"]
        database.projects.find_one.return_value = {"_id": project_id}
        client.__getitem__ = Mock(return_value=database)
        client.admin.command = Mock()

        with patch("sync.create_mongo_client", return_value=client):
            result = sync.check_connection(args)

        client.admin.command.assert_called_once_with("ping")
        database.list_collection_names.assert_called_once_with()
        database.projects.find_one.assert_called_once_with({}, {"_id": 1})
        client.close.assert_called_once_with()
        self.assertEqual(result.database_name, "sharelatex")
        self.assertEqual(result.collections, ["projects", "users"])
        self.assertEqual(result.sample_project_id, project_id)

    def test_check_git_access_with_project_access_token(self):
        args = self.make_args(
            git_remote_url="https://gitlab.example.com/group/overleaf-export.git",
            git_ssh_key_path=None,
            git_access_token="glpat-test-token",
            git_http_username="project-bot",
        )

        with patch(
            "sync.run_git_external",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="ref: refs/heads/main\tHEAD\n0123456789abcdef\tHEAD\n",
                stderr="",
            ),
        ) as run_git_external:
            result = sync.check_git_access(args)

        run_git_external.assert_called_once()
        self.assertEqual(result.auth_mode, "project-access-token")
        self.assertEqual(result.head_reference, "refs/heads/main")

    def test_check_git_access_rejects_missing_auth(self):
        args = self.make_args(git_ssh_key_path=None, git_access_token=None)
        with self.assertRaises(SystemExit):
            sync.check_git_access(args)

    def test_push_export_to_git_stages_only_export_dir(self):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            output_dir = repo_dir / "export"
            output_dir.mkdir()
            (repo_dir / ".git").mkdir()
            args = self.make_args(git_repo_dir=str(repo_dir))
            stats = sync.ExportStats(scanned=2, changed=1, unchanged=1)
            git_calls: list[tuple[list[str], bool]] = []

            def fake_run_git(repo_path, git_args, env, check=True):
                git_calls.append((git_args, check))
                if git_args[:3] == ["remote", "get-url", args.git_remote_name]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if git_args[:4] == ["diff", "--cached", "--quiet", "--"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                sync.push_export_to_git(args, output_dir, stats)

        expected = [
            ["checkout", "-B", "main"],
            ["remote", "get-url", "origin"],
            [
                "remote",
                "add",
                "origin",
                "git@gitlab.example.com:group/overleaf-export.git",
            ],
            ["add", "--all", "--force", "export"],
            ["diff", "--cached", "--quiet", "--", "export"],
            ["commit", "-m", unittest.mock.ANY],
            ["push", "origin", "main"],
        ]
        self.assertEqual([call[0] for call in git_calls], expected)

    def test_build_git_env_with_access_token_uses_http_header(self):
        args = self.make_args(
            git_access_token="glpat-test-token",
            git_http_username="project-bot",
            git_ssh_key_path=None,
        )

        env = sync.build_git_env(args)

        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.extraHeader")
        self.assertTrue(env["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic "))
        self.assertNotIn("GIT_SSH_COMMAND", env)

    def test_push_export_to_git_accepts_access_token_without_ssh_key(self):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            output_dir = repo_dir / "export"
            output_dir.mkdir()
            (repo_dir / ".git").mkdir()
            args = self.make_args(
                git_repo_dir=str(repo_dir),
                git_remote_url="https://gitlab.example.com/group/overleaf-export.git",
                git_ssh_key_path=None,
                git_access_token="glpat-test-token",
                git_http_username="oauth2",
            )
            stats = sync.ExportStats(scanned=2, changed=1, unchanged=1)
            git_calls: list[list[str]] = []

            def fake_run_git(repo_path, git_args, env, check=True):
                git_calls.append(git_args)
                if git_args[:3] == ["remote", "get-url", args.git_remote_name]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if git_args[:4] == ["diff", "--cached", "--quiet", "--"]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                sync.push_export_to_git(args, output_dir, stats)

        self.assertEqual(git_calls[-1], ["push", "origin", "main"])

    def test_access_token_requires_http_remote(self):
        args = self.make_args(
            git_remote_url="git@gitlab.example.com:group/overleaf-export.git",
            git_ssh_key_path=None,
            git_access_token="glpat-test-token",
            git_http_username="oauth2",
        )
        with self.assertRaises(SystemExit):
            sync.push_export_to_git(args, Path("C:/repo/export"), sync.ExportStats())

    def test_build_git_env_disables_interactive_prompts(self):
        args = self.make_args()

        env = sync.build_git_env(args)

        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")

    def test_write_json_serializes_nested_object_ids(self):
        payload = {
            "project_id": ObjectId("693616a50fa89c23ae8b1e99"),
            "nested": {
                "items": [ObjectId("693616a50fa89c23ae8b1e98")],
            },
        }

        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "project.json"

            changed = sync.write_json(output_path, payload, dry_run=False)

            self.assertTrue(changed)
            content = output_path.read_text(encoding="utf-8")

        self.assertIn('"project_id": "693616a50fa89c23ae8b1e99"', content)
        self.assertIn('"693616a50fa89c23ae8b1e98"', content)

    def test_ssh_key_requires_ssh_remote(self):
        args = self.make_args(
            git_remote_url="https://gitlab.example.com/group/overleaf-export.git",
            git_access_token=None,
        )
        with self.assertRaises(SystemExit):
            sync.check_git_access(args)

    def test_output_dir_must_live_inside_git_repo(self):
        args = self.make_args(git_repo_dir="C:/repo")
        with self.assertRaises(SystemExit):
            sync.ensure_output_in_repo(Path("C:/repo"), Path("C:/elsewhere/export"))


if __name__ == "__main__":
    unittest.main()
