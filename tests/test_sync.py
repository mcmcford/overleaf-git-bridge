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
            "output_dir": ".",
            "state_file": ".sync-state.json",
            "asset_store": "auto",
            "filestore_root": None,
            "s3_bucket": None,
            "s3_prefix": "",
            "s3_region": None,
            "s3_endpoint_url": None,
            "s3_access_key_id": None,
            "s3_secret_access_key": None,
            "s3_ca_bundle": None,
            "s3_verify_ssl": True,
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

    def test_render_doc_content_reconstructs_text_file(self):
        content = sync.render_doc_content(["line 1", "line 2\n", "line 3"])

        self.assertEqual(content, "line 1\nline 2\nline 3\n")

    def test_fetch_project_docs_returns_nested_paths_and_content(self):
        database = Mock()
        doc_a_id = ObjectId("693616a50fa89c23ae8b1e91")
        doc_b_id = ObjectId("693616a50fa89c23ae8b1e92")
        database.docs.find.return_value = [
            {"_id": doc_a_id, "lines": ["root line"]},
            {"_id": doc_b_id, "lines": ["nested line"]},
        ]
        root_folders = [
            {
                "name": "rootFolder",
                "docs": [{"_id": doc_a_id, "name": "main.tex"}],
                "folders": [
                    {
                        "name": "chapters",
                        "docs": [{"_id": doc_b_id, "name": "intro.tex"}],
                        "folders": [],
                    }
                ],
            }
        ]

        docs = sync.fetch_project_docs(database, root_folders)

        self.assertEqual(docs["main.tex"][0], Path("main.tex"))
        self.assertEqual(docs["main.tex"][1], "root line\n")
        self.assertEqual(docs["chapters\\intro.tex"][0], Path("chapters", "intro.tex"))
        self.assertEqual(docs["chapters\\intro.tex"][1], "nested line\n")

    def test_iter_project_file_refs_returns_nested_assets(self):
        root_folders = [
            {
                "name": "rootFolder",
                "fileRefs": [
                    {
                        "_id": ObjectId("693616a50fa89c23ae8b1e94"),
                        "name": "logo.png",
                        "hash": "abc123",
                    }
                ],
                "folders": [
                    {
                        "name": "assets",
                        "fileRefs": [
                            {
                                "_id": ObjectId("693616a50fa89c23ae8b1e95"),
                                "name": "diagram.pdf",
                                "hash": "def456",
                            }
                        ],
                        "folders": [],
                    }
                ],
            }
        ]

        locators = list(sync.iter_project_file_refs(root_folders))

        self.assertEqual(locators[0].relative_path, Path("logo.png"))
        self.assertEqual(locators[0].file_hash, "abc123")
        self.assertEqual(locators[1].relative_path, Path("assets", "diagram.pdf"))
        self.assertEqual(locators[1].file_hash, "def456")

    def test_resolve_asset_from_mongo_reads_prefix_bucket(self):
        database = Mock()
        database.projectHistoryBlobs.find.return_value = [
            {
                "blobs": {
                    "abc": [
                        {"h": "abc123", "b": b"payload", "s": 7},
                    ]
                }
            }
        ]

        payload = sync.resolve_asset_from_mongo(database, "abc123")

        self.assertEqual(payload, b"payload")

    def test_resolve_asset_from_filestore_reads_configured_path(self):
        with TemporaryDirectory() as temp_dir:
            filestore_root = Path(temp_dir)
            asset_path = filestore_root / "abc123"
            asset_path.write_bytes(b"asset-bytes")
            args = self.make_args(filestore_root=str(filestore_root))

            payload = sync.resolve_asset_from_filestore(
                args,
                {
                    "hash": "abc123",
                    "file_id": "file-1",
                    "project_id": "proj-1",
                    "name": "logo.png",
                    "hash_prefix2": "ab",
                    "hash_prefix2_b": "c1",
                    "hash_prefix3": "abc",
                },
            )

            self.assertEqual(payload, b"asset-bytes")

    def test_build_s3_buckets_splits_semicolon_list(self):
        args = self.make_args(s3_bucket="bucket-a; bucket-b ;bucket-c")

        buckets = sync.build_s3_buckets(args)

        self.assertEqual(buckets, ["bucket-a", "bucket-b", "bucket-c"])

    def test_extract_hash_from_s3_key_reads_hash_suffix(self):
        asset_hash = sync.extract_hash_from_s3_key(
            "0a2/b75/3b75102305d6cd5396/ad/7d8eab89f5acbf7dea1dd36463c75066cc9540"
        )

        self.assertEqual(asset_hash, "ad7d8eab89f5acbf7dea1dd36463c75066cc9540")

    def test_resolve_asset_from_s3_tries_multiple_buckets(self):
        args = self.make_args(s3_bucket="bucket-a;bucket-b")
        body = Mock()
        body.read.return_value = b"from-s3"
        client = Mock()

        class FakeNotFound(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "NoSuchKey"}}

        client.get_object.side_effect = [
            FakeNotFound(),
            {"Body": body},
        ]

        with patch("sync.build_asset_templates", return_value=["{hash}"]), patch(
            "sync.build_s3_hash_index", return_value={}
        ):
            payload = sync.resolve_asset_from_s3(
                args,
                {
                    "hash": "abc123",
                    "file_id": "file-1",
                    "project_id": "proj-1",
                    "name": "logo.png",
                    "hash_prefix2": "ab",
                    "hash_prefix2_b": "c1",
                    "hash_prefix3": "abc",
                },
                {"s3_client": client},
            )

        self.assertEqual(payload, b"from-s3")
        self.assertEqual(
            client.get_object.call_args_list[0].kwargs["Bucket"], "bucket-a"
        )
        self.assertEqual(
            client.get_object.call_args_list[1].kwargs["Bucket"], "bucket-b"
        )

    def test_resolve_asset_from_s3_uses_indexed_hash_key(self):
        args = self.make_args(s3_bucket="bucket-a")
        body = Mock()
        body.read.return_value = b"indexed"
        client = Mock()

        class FakeNotFound(Exception):
            def __init__(self):
                self.response = {"Error": {"Code": "NoSuchKey"}}

        client.get_object.side_effect = [
            FakeNotFound(),
            {"Body": body},
        ]

        with patch("sync.build_asset_templates", return_value=["{hash}"]), patch(
            "sync.build_s3_hash_index",
            return_value={
                "abc123abc123abc123abc123abc123abc123abcd": "prefix/ab/c123abc123abc123abc123abc123abc123abcd"
            },
        ):
            payload = sync.resolve_asset_from_s3(
                args,
                {
                    "hash": "abc123abc123abc123abc123abc123abc123abcd",
                    "file_id": "file-1",
                    "project_id": "proj-1",
                    "name": "logo.png",
                    "hash_prefix2": "ab",
                    "hash_prefix2_b": "c1",
                    "hash_prefix3": "abc",
                },
                {"s3_client": client},
            )

        self.assertEqual(payload, b"indexed")
        self.assertEqual(
            client.get_object.call_args_list[1].kwargs["Key"],
            "prefix/ab/c123abc123abc123abc123abc123abc123abcd",
        )

    def test_export_project_sources_writes_doc_files(self):
        database = Mock()
        doc_id = ObjectId("693616a50fa89c23ae8b1e93")
        database.docs.find.return_value = [{"_id": doc_id, "lines": ["hello"]}]
        project = {
            "rootFolder": [
                {
                    "name": "rootFolder",
                    "docs": [{"_id": doc_id, "name": "main.tex"}],
                    "folders": [],
                }
            ]
        }

        with TemporaryDirectory() as temp_dir:
            target_dir = Path(temp_dir)

            changed = sync.export_project_sources(
                database,
                project,
                target_dir,
                dry_run=False,
            )

            self.assertTrue(changed)
            self.assertEqual(
                (target_dir / "main.tex").read_text(encoding="utf-8"),
                "hello\n",
            )

    def test_export_project_assets_writes_binary_files(self):
        database = Mock()
        project = {
            "_id": ObjectId("693616a50fa89c23ae8b1e96"),
            "rootFolder": [
                {
                    "name": "rootFolder",
                    "fileRefs": [
                        {
                            "_id": ObjectId("693616a50fa89c23ae8b1e97"),
                            "name": "logo.png",
                            "hash": "abc123",
                        }
                    ],
                    "folders": [],
                }
            ],
        }

        with TemporaryDirectory() as temp_dir:
            filestore_root = Path(temp_dir) / "filestore"
            filestore_root.mkdir()
            (filestore_root / "abc123").write_bytes(b"png-bytes")
            output_dir = Path(temp_dir) / "project"
            args = self.make_args(
                asset_store="filesystem", filestore_root=str(filestore_root)
            )

            changed, unresolved = sync.export_project_assets(
                args,
                database,
                project,
                output_dir,
                dry_run=False,
                state={},
            )

            self.assertTrue(changed)
            self.assertEqual(unresolved, [])
            self.assertEqual((output_dir / "logo.png").read_bytes(), b"png-bytes")

    def test_build_sync_plan_skips_unchanged_project_and_prunes_removed_export(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            unchanged = sync.ProjectSyncInfo(
                project_id="proj-1",
                project_name="Stable Project",
                folder_name="stable-project-proj-1",
                last_updated="2026-04-01T00:00:00",
                version=4,
                trashed=False,
            )
            removed_folder = output_dir / "old-project-proj-2"
            removed_folder.mkdir()
            (output_dir / unchanged.folder_name).mkdir()
            sync_state = {
                "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                "config_fingerprint": "fingerprint-a",
                "projects": {
                    unchanged.project_id: sync.build_project_state_record(unchanged),
                    "proj-2": {
                        "project_name": "Old Project",
                        "folder_name": removed_folder.name,
                        "last_updated": "2026-03-31T00:00:00",
                        "version": 1,
                        "trashed": False,
                    },
                },
            }

            plan = sync.build_sync_plan(
                self.make_args(),
                output_dir,
                [unchanged],
                sync_state,
                "fingerprint-a",
            )

        self.assertEqual(plan.changed_ids, [])
        self.assertEqual(plan.active_count, 1)
        self.assertEqual(plan.removed_ids, {"proj-2"})
        self.assertEqual(plan.cleanup_paths, [removed_folder])
        self.assertEqual(
            plan.next_state["projects"],
            {unchanged.project_id: sync.build_project_state_record(unchanged)},
        )

    def test_build_sync_plan_marks_renamed_project_changed_and_prunes_old_folder(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            project = sync.ProjectSyncInfo(
                project_id="proj-1",
                project_name="Renamed Project",
                folder_name="renamed-project-proj-1",
                last_updated="2026-04-01T00:00:00",
                version=2,
                trashed=False,
            )
            old_folder = output_dir / "old-name-proj-1"
            old_folder.mkdir()
            sync_state = {
                "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                "config_fingerprint": "fingerprint-a",
                "projects": {
                    "proj-1": {
                        "project_name": "Old Name",
                        "folder_name": old_folder.name,
                        "last_updated": "2026-03-31T00:00:00",
                        "version": 1,
                        "trashed": False,
                    }
                },
            }

            plan = sync.build_sync_plan(
                self.make_args(),
                output_dir,
                [project],
                sync_state,
                "fingerprint-a",
            )

        self.assertEqual(plan.changed_ids, ["proj-1"])
        self.assertEqual(plan.cleanup_paths, [old_folder])
        self.assertEqual(plan.removed_ids, set())

    def test_build_sync_plan_forces_refresh_when_config_changes(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            project = sync.ProjectSyncInfo(
                project_id="proj-1",
                project_name="Stable Project",
                folder_name="stable-project-proj-1",
                last_updated="2026-04-01T00:00:00",
                version=4,
                trashed=False,
            )
            (output_dir / project.folder_name).mkdir()
            sync_state = {
                "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                "config_fingerprint": "fingerprint-old",
                "projects": {
                    project.project_id: sync.build_project_state_record(project)
                },
            }

            plan = sync.build_sync_plan(
                self.make_args(include_raw=True),
                output_dir,
                [project],
                sync_state,
                "fingerprint-new",
            )

        self.assertEqual(plan.changed_ids, ["proj-1"])
        self.assertEqual(
            plan.next_state["config_fingerprint"],
            "fingerprint-new",
        )

    def test_resolve_project_user_identity_prefers_last_updated_by(self):
        project = {
            "lastUpdatedBy": ObjectId("693610e0919e86a942250c2f"),
            "owner_ref": ObjectId("6936110a919e86a942250c3b"),
        }
        users_by_id = {
            "693610e0919e86a942250c2f": {
                "_id": ObjectId("693610e0919e86a942250c2f"),
                "first_name": "Morgan",
                "last_name": "Lee",
                "email": "morgan@example.com",
            },
            "6936110a919e86a942250c3b": {
                "_id": ObjectId("6936110a919e86a942250c3b"),
                "first_name": "Owner",
                "last_name": "User",
                "email": "owner@example.com",
            },
        }

        user_id, user_name = sync.resolve_project_user_identity(project, users_by_id)

        self.assertEqual(user_id, "693610e0919e86a942250c2f")
        self.assertEqual(user_name, "Morgan Lee")

    def test_build_project_manifest_includes_resolved_user_names(self):
        project = {
            "_id": ObjectId("693616a50fa89c23ae8b1e99"),
            "name": "Example Project",
            "description": "Demo",
            "owner_ref": ObjectId("6936110a919e86a942250c3b"),
            "rootDoc_id": ObjectId("693616a50fa89c23ae8b1e91"),
            "compiler": "pdflatex",
            "lastUpdated": "2026-04-01T00:00:00Z",
            "lastUpdatedBy": ObjectId("693610e0919e86a942250c2f"),
            "version": 3,
            "trashed": False,
            "deletedDocs": [],
            "rootFolder": [],
            "overleaf": {},
        }
        users_by_id = {
            "693610e0919e86a942250c2f": {
                "_id": ObjectId("693610e0919e86a942250c2f"),
                "first_name": "Morgan",
                "last_name": "Lee",
            },
            "6936110a919e86a942250c3b": {
                "_id": ObjectId("6936110a919e86a942250c3b"),
                "email": "owner@example.com",
            },
        }

        manifest = sync.build_project_manifest(project, users_by_id)

        self.assertEqual(manifest["last_updated_by_name"], "Morgan Lee")
        self.assertEqual(manifest["owner_name"], "owner@example.com")
        self.assertEqual(manifest["resolved_editor_id"], "693610e0919e86a942250c2f")
        self.assertEqual(manifest["resolved_editor_name"], "Morgan Lee")

    def test_build_sync_plan_ignores_saved_editor_metadata_for_change_detection(self):
        with TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            project = sync.ProjectSyncInfo(
                project_id="proj-1",
                project_name="Stable Project",
                folder_name="stable-project-proj-1",
                last_updated="2026-04-01T00:00:00",
                version=4,
                trashed=False,
            )
            (output_dir / project.folder_name).mkdir()
            sync_state = {
                "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                "config_fingerprint": "fingerprint-a",
                "projects": {
                    project.project_id: {
                        **sync.build_project_state_record(project),
                        "resolved_editor_id": "user-1",
                        "resolved_editor_name": "Morgan Lee",
                    }
                },
            }

            plan = sync.build_sync_plan(
                self.make_args(),
                output_dir,
                [project],
                sync_state,
                "fingerprint-a",
            )

        self.assertEqual(plan.changed_ids, [])

    def test_resolve_saved_project_user_identity_reads_state_values(self):
        user_id, user_name = sync.resolve_saved_project_user_identity(
            {
                "resolved_editor_id": "693610e0919e86a942250c2f",
                "resolved_editor_name": "Morgan Lee",
            }
        )

        self.assertEqual(user_id, "693610e0919e86a942250c2f")
        self.assertEqual(user_name, "Morgan Lee")

    def test_push_export_to_git_commits_each_project_separately(self):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            output_dir = repo_dir / "export"
            project_a_dir = output_dir / "project-a-proj-a"
            project_b_dir = output_dir / "project-b-proj-b"
            project_a_dir.mkdir(parents=True)
            project_b_dir.mkdir(parents=True)
            state_file = output_dir / ".sync-state.json"
            state_file.write_text("{}\n", encoding="utf-8")
            (repo_dir / ".git").mkdir()
            args = self.make_args(git_repo_dir=str(repo_dir))
            export_result = sync.ExportResult(
                stats=sync.ExportStats(scanned=2, changed=2, unchanged=0),
                project_changes=[
                    sync.ProjectCommitChange(
                        project_id="proj-a",
                        project_name="Project A",
                        user_id="user-a",
                        user_name="Morgan Lee",
                        paths=(project_a_dir,),
                        state_after={
                            "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                            "config_fingerprint": "fingerprint-a",
                            "projects": {"proj-a": {"folder_name": project_a_dir.name}},
                        },
                    ),
                    sync.ProjectCommitChange(
                        project_id="proj-b",
                        project_name="Project B",
                        user_id="user-b",
                        user_name="Jamie Smith",
                        paths=(project_b_dir,),
                        state_after={
                            "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                            "config_fingerprint": "fingerprint-a",
                            "projects": {
                                "proj-a": {"folder_name": project_a_dir.name},
                                "proj-b": {"folder_name": project_b_dir.name},
                            },
                        },
                    ),
                ],
                state_file=state_file,
                final_state={
                    "format_version": sync.SYNC_STATE_FORMAT_VERSION,
                    "config_fingerprint": "fingerprint-a",
                    "projects": {
                        "proj-a": {"folder_name": project_a_dir.name},
                        "proj-b": {"folder_name": project_b_dir.name},
                    },
                },
            )
            git_calls: list[list[str]] = []
            diff_results = iter([1, 1, 0])

            def fake_run_git(repo_path, git_args, env, check=True):
                git_calls.append(git_args)
                if git_args[:3] == ["remote", "get-url", args.git_remote_name]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if git_args[:3] == ["remote", "add", args.git_remote_name]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args[:3] == ["diff", "--cached", "--quiet"]:
                    return SimpleNamespace(
                        returncode=next(diff_results), stdout="", stderr=""
                    )
                if git_args[:3] == ["diff", "--quiet", "--"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                sync.push_export_to_git(args, output_dir, export_result)

        commit_calls = [call for call in git_calls if call[:1] == ["commit"]]
        self.assertEqual(
            commit_calls,
            [
                ["commit", "-m", 'Sync project "Project A" by Morgan Lee'],
                ["commit", "-m", 'Sync project "Project B" by Jamie Smith'],
            ],
        )
        add_calls = [call for call in git_calls if call[:1] == ["add"]]
        self.assertEqual(
            add_calls,
            [
                [
                    "add",
                    "--all",
                    "--force",
                    "--",
                    str(Path("export", "project-a-proj-a")),
                    str(Path("export", ".sync-state.json")),
                ],
                [
                    "add",
                    "--all",
                    "--force",
                    "--",
                    str(Path("export", "project-b-proj-b")),
                    str(Path("export", ".sync-state.json")),
                ],
                [
                    "add",
                    "--all",
                    "--force",
                    "--",
                    str(Path("export", ".sync-state.json")),
                ],
            ],
        )
        self.assertEqual(git_calls[-1], ["push", "origin", "main"])

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

    def test_push_export_to_git_rejects_source_repo_as_target(self):
        repo_dir = Path(sync.__file__).resolve().parent
        output_dir = repo_dir / "export"
        args = self.make_args(git_repo_dir=str(repo_dir))

        with self.assertRaises(SystemExit):
            sync.push_export_to_git(args, output_dir, sync.ExportStats())

    def test_ensure_git_repo_refuses_to_overwrite_existing_remote(self):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            (repo_dir / ".git").mkdir()
            args = self.make_args(git_repo_dir=str(repo_dir))
            env = {}

            def fake_run_git(repo_path, git_args, env, check=True):
                if git_args[:3] == ["remote", "get-url", args.git_remote_name]:
                    return SimpleNamespace(
                        returncode=0,
                        stdout="https://example.com/other-repo.git\n",
                        stderr="",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                with self.assertRaises(SystemExit):
                    sync.ensure_git_repo(args, repo_dir, env)

    def test_sync_git_repo_before_export_checks_out_remote_branch(self):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            args = self.make_args(git_repo_dir=str(repo_dir))
            env = {}
            git_calls: list[list[str]] = []

            def fake_run_git(repo_path, git_args, env, check=True):
                git_calls.append(git_args)
                if git_args == ["status", "--porcelain"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args == ["fetch", args.git_remote_name]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args == [
                    "rev-parse",
                    "--verify",
                    f"refs/remotes/{args.git_remote_name}/{args.git_branch}",
                ]:
                    return SimpleNamespace(returncode=0, stdout="abc123\n", stderr="")
                if git_args == [
                    "checkout",
                    "-B",
                    args.git_branch,
                    f"{args.git_remote_name}/{args.git_branch}",
                ]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                sync.sync_git_repo_before_export(args, repo_dir, env)

        self.assertEqual(
            git_calls,
            [
                ["status", "--porcelain"],
                ["fetch", "origin"],
                ["rev-parse", "--verify", "refs/remotes/origin/main"],
                ["checkout", "-B", "main", "origin/main"],
            ],
        )

    def test_sync_git_repo_before_export_initializes_local_branch_when_remote_missing(
        self,
    ):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            args = self.make_args(git_repo_dir=str(repo_dir))
            env = {}
            git_calls: list[list[str]] = []

            def fake_run_git(repo_path, git_args, env, check=True):
                git_calls.append(git_args)
                if git_args == ["status", "--porcelain"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args == ["fetch", args.git_remote_name]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args == [
                    "rev-parse",
                    "--verify",
                    f"refs/remotes/{args.git_remote_name}/{args.git_branch}",
                ]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if git_args == ["checkout", "-B", args.git_branch]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                sync.sync_git_repo_before_export(args, repo_dir, env)

        self.assertEqual(
            git_calls,
            [
                ["status", "--porcelain"],
                ["fetch", "origin"],
                ["rev-parse", "--verify", "refs/remotes/origin/main"],
                ["checkout", "-B", "main"],
            ],
        )

    def test_prepare_git_repo_for_export_rejects_dirty_repo(self):
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            args = self.make_args(
                git_repo_dir=str(repo_dir),
                git_remote_url="https://gitlab.example.com/group/overleaf-export.git",
                git_ssh_key_path=None,
                git_access_token="glpat-test-token",
                git_http_username="project-bot",
            )

            def fake_run_git(repo_path, git_args, env, check=True):
                if git_args == ["init"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args[:3] == ["remote", "get-url", args.git_remote_name]:
                    return SimpleNamespace(returncode=1, stdout="", stderr="")
                if git_args[:3] == ["remote", "add", args.git_remote_name]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if git_args == ["status", "--porcelain"]:
                    return SimpleNamespace(
                        returncode=0, stdout=" M export/project.json\n", stderr=""
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch("sync.run_git", side_effect=fake_run_git):
                with self.assertRaises(SystemExit):
                    sync.prepare_git_repo_for_export(args)

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

    def test_resolve_output_dir_defaults_to_git_repo_root(self):
        args = self.make_args(git_repo_dir="C:/repo", output_dir=".")

        output_dir = sync.resolve_output_dir(args)

        self.assertEqual(output_dir, Path("C:/repo"))


if __name__ == "__main__":
    unittest.main()
