from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urlencode

from bson import ObjectId
from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.errors import PyMongoError

DEFAULT_DB_NAME = "sharelatex"
DEFAULT_OUTPUT_DIR = "."
DEFAULT_MONGO_HOST = "localhost"
DEFAULT_MONGO_PORT = 27017
DEFAULT_MONGO_AUTH_DB = "admin"
DEFAULT_CONNECT_TIMEOUT_MS = 5000
DEFAULT_GIT_BRANCH = "main"
DEFAULT_GIT_REMOTE_NAME = "origin"
DEFAULT_GIT_COMMIT_NAME = "Overleaf Git Bridge"
DEFAULT_GIT_COMMIT_EMAIL = "overleaf-git-bridge@example.invalid"
DEFAULT_GIT_HTTP_USERNAME = ""
DEFAULT_ASSET_STORE = "auto"
SYNC_STATE_FORMAT_VERSION = 1
SYNC_STATE_FILE_NAME = ".sync-state.json"
DEFAULT_ASSET_PATH_TEMPLATES = [
    "{hash}",
    "{hash_prefix2}/{hash}",
    "{hash_prefix2}/{hash_prefix2_b}/{hash}",
    "{file_id}",
    "{project_id}/{file_id}",
    "{project_id}/{name}",
]


# Prefer the project's .env file over stale terminal session variables so
# connectivity checks use the values currently saved for this workspace.
load_dotenv(override=True)


@dataclass
class ExportStats:
    """
    Class to track statistics about the export process, such as how many projects were scanned,
    how many had changes, and how many were unchanged or deleted.
    """

    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class ProjectCommitChange:
    """Describe one project's exported filesystem changes and resulting sync state."""

    project_id: str
    project_name: str
    user_id: str | None
    user_name: str
    paths: tuple[Path, ...]
    state_after: dict[str, Any]


@dataclass
class ExportResult:
    """Capture export statistics, per-project commit data, and final sync state."""

    stats: ExportStats
    project_changes: list[ProjectCommitChange]
    state_file: Path
    final_state: dict[str, Any]


@dataclass
class ConnectionCheckResult:
    """
    Basic information about the MongoDB connection and database state
    """

    database_name: str
    collections: list[str]
    sample_project_id: str | None


@dataclass
class GitCheckResult:
    """
    Basic information about the GitLab connection and repository state
    """

    remote_url: str
    auth_mode: str
    head_reference: str | None


class DateTimeEncoder(json.JSONEncoder):
    """JSON encoder that converts datetime-like Mongo values into strings."""

    def default(self, obj: Any) -> Any:
        """Serialize datetimes and ObjectIds into JSON-friendly string values."""

        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, ObjectId):
            return str(obj)
        return super().default(obj)


def env_bool(name: str, default: bool = False) -> bool:
    """
    Convert the various ways of expressing true/false in environment variables into a boolean value,
    default to false if the variable is not set or cannot be interpreted, and allow an optional default to override that.
    """

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str) -> list[str]:
    """
    Convert a semicolon-separated environment variable into a list of strings.
    """
    value = os.getenv(name)
    if not value:
        return []
    return [item.strip() for item in value.split(";") if item.strip()]


@dataclass(frozen=True)
class AssetLocator:
    """
    All the key idenifiers for a file or asset from an overleaf project
    """

    relative_path: Path
    file_id: str
    file_name: str
    file_hash: str | None


@dataclass(frozen=True)
class ProjectSyncInfo:
    """
    Information about an individual project's sync state, including its ID, name, folder, last update time, version, and trash status.
    """

    project_id: str
    project_name: str
    folder_name: str
    last_updated: str | None
    version: Any
    trashed: bool


@dataclass
class SyncPlan:
    """
    The change plan, multiple lists of project ids and file paths to change, and the next state to save after applying the plan.
    """

    changed_ids: list[str]
    cleanup_paths: list[Path]
    removed_ids: set[str]
    next_state: dict[str, Any]
    active_count: int


def build_parser() -> argparse.ArgumentParser:
    """
    Take in all of the configuration options via cli arguments and/or environment variables, with the cli arguments
    taking precedence over environment variables when both are provided.

    Return the built parser ready to parse the config for the sync process.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Export Overleaf project metadata from MongoDB into a git-friendly "
            "directory structure and optionally push changes to GitLab."
        )
    )
    parser.add_argument(
        "--asset-store",
        choices=["auto", "mongo", "filesystem", "s3"],
        default=os.getenv("OVERLEAF_ASSET_STORE", DEFAULT_ASSET_STORE),
        help=(
            "Where to resolve uploaded fileRefs from. `auto` tries Mongo history "
            "blobs, then a local filestore path, then S3 if configured."
        ),
    )
    parser.add_argument(
        "--filestore-root",
        default=os.getenv("OVERLEAF_FILESTORE_ROOT"),
        help=(
            "Optional local Overleaf filestore root directory. Use this when your "
            "deployment stores uploaded assets on disk rather than in MongoDB."
        ),
    )
    parser.add_argument(
        "--s3-bucket",
        default=os.getenv("OVERLEAF_S3_BUCKET"),
        help="Optional S3 bucket containing uploaded Overleaf assets.",
    )
    parser.add_argument(
        "--s3-prefix",
        default=os.getenv("OVERLEAF_S3_PREFIX", ""),
        help="Optional prefix inside the S3 bucket for uploaded assets.",
    )
    parser.add_argument(
        "--s3-region",
        default=os.getenv("OVERLEAF_S3_REGION"),
        help="Optional AWS region for the S3 bucket.",
    )
    parser.add_argument(
        "--s3-endpoint-url",
        default=os.getenv("OVERLEAF_S3_ENDPOINT_URL"),
        help="Optional custom S3 endpoint URL for compatible object stores.",
    )
    parser.add_argument(
        "--s3-access-key-id",
        default=os.getenv("OVERLEAF_S3_ACCESS_KEY_ID"),
        help="Optional S3 access key id for uploaded asset export.",
    )
    parser.add_argument(
        "--s3-secret-access-key",
        default=os.getenv("OVERLEAF_S3_SECRET_ACCESS_KEY"),
        help="Optional S3 secret access key for uploaded asset export.",
    )
    parser.add_argument(
        "--s3-ca-bundle",
        default=os.getenv("OVERLEAF_S3_CA_BUNDLE"),
        help="Optional CA bundle path for TLS verification against a private S3 endpoint.",
    )
    parser.add_argument(
        "--s3-verify-ssl",
        default=env_bool("OVERLEAF_S3_VERIFY_SSL", True),
        action=argparse.BooleanOptionalAction,
        help="Enable TLS certificate verification for S3 requests. Defaults to true.",
    )
    parser.add_argument(
        "--mongo-uri",
        default=os.getenv("OVERLEAF_MONGO_URI"),
        help="MongoDB connection string. Overrides the individual Mongo auth flags.",
    )
    parser.add_argument(
        "--mongo-host",
        default=os.getenv("OVERLEAF_MONGO_HOST", DEFAULT_MONGO_HOST),
        help=f"MongoDB host. Defaults to {DEFAULT_MONGO_HOST}.",
    )
    parser.add_argument(
        "--mongo-port",
        type=int,
        default=int(os.getenv("OVERLEAF_MONGO_PORT", str(DEFAULT_MONGO_PORT))),
        help=f"MongoDB port. Defaults to {DEFAULT_MONGO_PORT}.",
    )
    parser.add_argument(
        "--mongo-username",
        default=os.getenv("OVERLEAF_MONGO_USERNAME"),
        help="MongoDB username for authenticated connections.",
    )
    parser.add_argument(
        "--mongo-password",
        default=os.getenv("OVERLEAF_MONGO_PASSWORD"),
        help="MongoDB password for authenticated connections.",
    )
    parser.add_argument(
        "--mongo-auth-db",
        default=os.getenv("OVERLEAF_MONGO_AUTH_DB", DEFAULT_MONGO_AUTH_DB),
        help=(
            "Authentication database to use when username/password are provided. "
            f"Defaults to {DEFAULT_MONGO_AUTH_DB}."
        ),
    )
    parser.add_argument(
        "--mongo-auth-mechanism",
        default=os.getenv("OVERLEAF_MONGO_AUTH_MECHANISM"),
        help="Optional MongoDB auth mechanism, such as SCRAM-SHA-256.",
    )
    parser.add_argument(
        "--mongo-tls",
        action="store_true",
        default=env_bool("OVERLEAF_MONGO_TLS"),
        help="Enable TLS for the MongoDB connection.",
    )
    parser.add_argument(
        "--connect-timeout-ms",
        type=int,
        default=int(
            os.getenv(
                "OVERLEAF_MONGO_CONNECT_TIMEOUT_MS",
                str(DEFAULT_CONNECT_TIMEOUT_MS),
            )
        ),
        help=(
            "MongoDB server selection timeout in milliseconds. "
            f"Defaults to {DEFAULT_CONNECT_TIMEOUT_MS}."
        ),
    )
    parser.add_argument(
        "--db-name",
        default=os.getenv("OVERLEAF_MONGO_DB", DEFAULT_DB_NAME),
        help=f"MongoDB database name. Defaults to {DEFAULT_DB_NAME}.",
    )
    parser.add_argument(
        "--output-dir",
        default=os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR),
        help=(
            "Directory to write exported metadata. Defaults to the git repo "
            f"root ({DEFAULT_OUTPUT_DIR})."
        ),
    )
    parser.add_argument(
        "--state-file",
        default=os.getenv("SYNC_STATE_FILE", SYNC_STATE_FILE_NAME),
        help=(
            "Relative or absolute path for the incremental sync state file. "
            f"Defaults to {SYNC_STATE_FILE_NAME} inside the output directory."
        ),
    )
    parser.add_argument(
        "--project-id",
        action="append",
        default=[],
        help="Limit export to one or more specific project ids.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit the number of projects exported. 0 means no limit.",
    )
    parser.add_argument(
        "--include-raw",
        action="store_true",
        help="Also persist the full raw MongoDB project document for each project.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing files, committing, or pushing.",
    )
    parser.add_argument(
        "--check-connection-mongo",
        action="store_true",
        help="Ping MongoDB and verify the projects collection can be read, then exit.",
    )
    parser.add_argument(
        "--check-connection-git",
        action="store_true",
        help="Validate GitLab authentication and remote reachability, then exit.",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        default=env_bool("GITLAB_PUSH"),
        help="Commit exported changes and push them to GitLab.",
    )
    parser.add_argument(
        "--git-repo-dir",
        default=os.getenv("GIT_REPO_DIR", "."),
        help="Local git working tree to use for commits and pushes. Defaults to the current directory.",
    )
    parser.add_argument(
        "--git-remote-name",
        default=os.getenv("GIT_REMOTE_NAME", DEFAULT_GIT_REMOTE_NAME),
        help=f"Git remote name. Defaults to {DEFAULT_GIT_REMOTE_NAME}.",
    )
    parser.add_argument(
        "--git-remote-url",
        default=os.getenv("GITLAB_REMOTE_URL"),
        help="GitLab remote URL for the target repository. Use HTTPS for token auth or SSH for deploy keys.",
    )
    parser.add_argument(
        "--git-branch",
        default=os.getenv("GITLAB_BRANCH", DEFAULT_GIT_BRANCH),
        help=f"Git branch to commit and push. Defaults to {DEFAULT_GIT_BRANCH}.",
    )
    parser.add_argument(
        "--git-ssh-key-path",
        default=os.getenv("GITLAB_SSH_KEY_PATH"),
        help="Path to the SSH deploy key with write access to the GitLab repo.",
    )
    parser.add_argument(
        "--git-access-token",
        default=os.getenv("GITLAB_ACCESS_TOKEN"),
        help="GitLab project access token for HTTPS push authentication.",
    )
    parser.add_argument(
        "--git-http-username",
        default=os.getenv("GITLAB_HTTP_USERNAME", DEFAULT_GIT_HTTP_USERNAME),
        help=(
            "Username to pair with HTTPS token auth. Use `oauth2` for a personal "
            "access token, the generated bot username for a project access token, "
            "or the deploy token username when using a deploy token."
        ),
    )
    parser.add_argument(
        "--git-commit-name",
        default=os.getenv("GITLAB_COMMIT_NAME", DEFAULT_GIT_COMMIT_NAME),
        help=f"Git author/committer name. Defaults to {DEFAULT_GIT_COMMIT_NAME}.",
    )
    parser.add_argument(
        "--git-commit-email",
        default=os.getenv("GITLAB_COMMIT_EMAIL", DEFAULT_GIT_COMMIT_EMAIL),
        help=f"Git author/committer email. Defaults to {DEFAULT_GIT_COMMIT_EMAIL}.",
    )
    parser.add_argument(
        "--git-commit-message",
        default=os.getenv("GITLAB_COMMIT_MESSAGE"),
        help="Optional git commit message. A timestamped message is generated by default.",
    )
    return parser


def sanitize_name(value: str) -> str:
    """
    Make sure the project name is safe to use as a folder name by replacing unsafe characters with dashes and stripping leading/trailing dots, dashes, and underscores.

    Return a cleaned version of the name, or "unnamed-project" if the cleaned name is empty.
    """

    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "unnamed-project"


def normalize_document(value: Any) -> Any:
    """
    Take any value from a MongoDB document and convert it into a form that can be safely serialized to JSON and compared for changes,
    such as converting ObjectIds to strings and datetimes to ISO format strings.

    This will hopefully help if overleaf decides to change the format of certain fields in the future, as long as the underlying data
    can still be represented in a JSON-friendly way.
    """

    if isinstance(value, dict):
        return {key: normalize_document(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_document(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "binary") and hasattr(value, "generation_time"):
        return str(value)
    return value


def resolve_user_reference(
    raw_user_id: Any,
    users_by_id: dict[str, dict[str, Any]],
) -> tuple[str | None, str | None]:
    """
    Given a raw user ID from a project document and a mapping of user IDs to user documents, resolve the
    user reference to a string ID and display name.

    Return a tuple of (user_id, user_display_name), where user_id is the string version of the raw user ID
    or None if it cannot be resolved, and user_display_name is a human-friendly name for the user or None if it cannot be resolved.
    """

    if not raw_user_id:
        return None, None
    user_id = str(raw_user_id)
    return user_id, build_user_display_name(users_by_id.get(user_id))


def build_project_manifest(
    project: dict[str, Any],
    users_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Build a manifest of key project metadata and user-friendly information for a given Overleaf project document, including resolved user references.
    """

    if users_by_id is None:
        users_by_id = {}

    owner_ref, owner_name = resolve_user_reference(
        project.get("owner_ref"), users_by_id
    )
    last_updated_by, last_updated_by_name = resolve_user_reference(
        project.get("lastUpdatedBy"),
        users_by_id,
    )
    resolved_editor_id, resolved_editor_name = resolve_project_user_identity(
        project,
        users_by_id,
    )

    return {
        "project_id": str(project.get("_id")),
        "name": project.get("name"),
        "description": project.get("description"),
        "owner_ref": owner_ref,
        "owner_name": owner_name,
        "root_doc_id": (
            str(project.get("rootDoc_id")) if project.get("rootDoc_id") else None
        ),
        "compiler": project.get("compiler"),
        "last_updated": normalize_document(project.get("lastUpdated")),
        "last_updated_by": last_updated_by,
        "last_updated_by_name": last_updated_by_name,
        "resolved_editor_id": resolved_editor_id,
        "resolved_editor_name": resolved_editor_name,
        "version": project.get("version"),
        "trashed": project.get("trashed"),
        "deleted_docs": normalize_document(project.get("deletedDocs", [])),
        "root_folder": normalize_document(project.get("rootFolder")),
        "overleaf": normalize_document(project.get("overleaf")),
    }


def build_project_sync_info(project: dict[str, Any]) -> ProjectSyncInfo:
    """
    Extract the key metadata fields from a project document and return a ProjectSyncInfo object
    that can be used to track the sync state of this project, including a sanitized folder name for
    storing the project's files and metadata on disk.
    """

    project_id = str(project.get("_id"))
    project_name = project.get("name") or project_id
    return ProjectSyncInfo(
        project_id=project_id,
        project_name=project_name,
        folder_name=f"{sanitize_name(project_name)}-{project_id}",
        last_updated=normalize_document(project.get("lastUpdated")),
        version=normalize_document(project.get("version")),
        trashed=bool(project.get("trashed")),
    )


def build_project_state_record(
    project: ProjectSyncInfo,
    resolved_editor_id: str | None = None,
    resolved_editor_name: str | None = None,
) -> dict[str, Any]:
    """Build the sync-state record persisted for a single exported project."""

    record = {
        "project_name": project.project_name,
        "folder_name": project.folder_name,
        "last_updated": project.last_updated,
        "version": project.version,
        "trashed": project.trashed,
    }
    if resolved_editor_id is not None or resolved_editor_name is not None:
        record["resolved_editor_id"] = resolved_editor_id
        record["resolved_editor_name"] = normalize_user_name(resolved_editor_name)
    return record


def build_project_state_signature(
    record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Extract the subset of state fields that determine whether a project changed."""

    if not isinstance(record, dict):
        return None
    return {
        "project_name": record.get("project_name"),
        "folder_name": record.get("folder_name"),
        "last_updated": record.get("last_updated"),
        "version": record.get("version"),
        "trashed": record.get("trashed"),
    }


def normalize_user_name(value: str | None) -> str:
    """
    Clean up a user name string by stripping whitespace and
    returning "unknown user" if the result is empty or the input is None.
    """

    cleaned = (value or "").strip()
    return cleaned or "unknown user"


def build_user_display_name(user: dict[str, Any] | None) -> str:
    """
    Build a display name for a user, prioritizing full name, then email, and finally user ID.
    If no valid information is available, return "unknown user".
    """
    if not user:
        return "unknown user"

    first_name = str(user.get("first_name") or "").strip()
    last_name = str(user.get("last_name") or "").strip()
    full_name = " ".join(part for part in [first_name, last_name] if part)
    if full_name:
        return full_name

    email = str(user.get("email") or "").strip()
    if email:
        return email

    return str(user.get("_id") or "unknown user")


def coerce_mongo_id(value: Any) -> str | ObjectId:
    """Convert valid ObjectId strings into ObjectId instances for Mongo queries."""

    if isinstance(value, ObjectId):
        return value
    if isinstance(value, str) and ObjectId.is_valid(value):
        return ObjectId(value)
    return str(value)


def fetch_users_by_ids(database: Any, user_ids: set[str]) -> dict[str, dict[str, Any]]:
    """
    Run a single query to fetch all user documents for the given set of user IDs and return a mapping of stringified user ID to user document,
    which can be used to resolve user references in project documents.
    """

    if not user_ids:
        return {}

    try:
        cursor = database.users.find(
            {
                "_id": {
                    "$in": [coerce_mongo_id(user_id) for user_id in sorted(user_ids)]
                }
            },
            {"_id": 1, "first_name": 1, "last_name": 1, "email": 1},
        )
        return {str(user["_id"]): user for user in cursor}
    except PyMongoError as exc:
        raise SystemExit(f"MongoDB export failed while loading users: {exc}") from exc


def resolve_project_user_identity(
    project: dict[str, Any],
    users_by_id: dict[str, dict[str, Any]],
) -> tuple[str | None, str]:
    """Resolve the most relevant user attached to a project for commit attribution."""

    for field_name in ("lastUpdatedBy", "owner_ref"):
        raw_user_id = project.get(field_name)
        if not raw_user_id:
            continue
        user_id = str(raw_user_id)
        return user_id, build_user_display_name(users_by_id.get(user_id))
    return None, "unknown user"


def resolve_saved_project_user_identity(
    record: dict[str, Any],
) -> tuple[str | None, str]:
    """Read the stored editor identity back out of a saved sync-state record."""

    if not isinstance(record, dict):
        return None, "unknown user"
    return (
        record.get("resolved_editor_id"),
        normalize_user_name(record.get("resolved_editor_name")),
    )


def clone_sync_state(sync_state: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of sync state so mutations do not leak across snapshots."""

    return copy.deepcopy(sync_state)


def build_sync_config_fingerprint(args: argparse.Namespace) -> str:
    """Hash export-affecting settings so config changes can trigger a refresh."""

    payload = {
        "format_version": SYNC_STATE_FORMAT_VERSION,
        "include_raw": bool(args.include_raw),
        "asset_store": args.asset_store,
        "filestore_root": args.filestore_root or "",
        "asset_templates": build_asset_templates(),
        "s3_bucket": args.s3_bucket or "",
        "s3_prefix": args.s3_prefix or "",
        "s3_region": args.s3_region or "",
        "s3_endpoint_url": args.s3_endpoint_url or "",
        "s3_access_key_id": args.s3_access_key_id or "",
        "s3_secret_access_key": args.s3_secret_access_key or "",
        "s3_ca_bundle": args.s3_ca_bundle or "",
        "s3_verify_ssl": bool(args.s3_verify_ssl),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_sync_state(path: Path) -> dict[str, Any]:
    """Load the incremental sync-state file, falling back to an empty default state."""

    default_state = {
        "format_version": SYNC_STATE_FORMAT_VERSION,
        "config_fingerprint": "",
        "projects": {},
    }
    if not path.exists():
        return default_state

    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"warning: ignoring unreadable sync state at {path}: {exc}")
        return default_state

    if not isinstance(loaded, dict):
        return default_state

    projects = loaded.get("projects")
    if not isinstance(projects, dict):
        projects = {}

    return {
        "format_version": loaded.get("format_version", SYNC_STATE_FORMAT_VERSION),
        "config_fingerprint": str(loaded.get("config_fingerprint", "")),
        "projects": projects,
    }


def dedupe_paths(paths: list[Path]) -> list[Path]:
    """Preserve path order while removing duplicate filesystem targets."""

    seen: set[str] = set()
    unique_paths: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique_paths.append(path)
    return unique_paths


def remove_path(path: Path, dry_run: bool) -> bool:
    """
    Remove a file or directory at the given path if it exists.
    If dry_run is true, just return whether the path exists without actually removing it.
    """

    if not path.exists():
        return False
    if dry_run:
        return True
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return True


def prepare_target_dir(path: Path, dry_run: bool) -> None:
    """
    Prepare the target directory by removing it if it exists (via remove_path) and then creating it.
    """

    remove_path(path, dry_run)
    if not dry_run:
        path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str, dry_run: bool) -> bool:
    """
    Write the given text content to the specified path, but only if the content has changed from what's already on disk.

    Return false if the file already exists with the same content,
    true if the file was changed or created.
    """

    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def write_bytes(path: Path, content: bytes, dry_run: bool) -> bool:
    """
    Write the given binary content to the specified path, but only if the content has changed from what's already on disk.
    Very similar to write_text but for bytes, and without encoding considerations.

    Return false if the file already exists with the same content,
    true if the file was changed or created.
    """

    if path.exists() and path.read_bytes() == content:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return True


def write_json(path: Path, payload: dict[str, Any] | list[Any], dry_run: bool) -> bool:
    """
    Write the given JSON payload to the specified path, but only if the content has changed from what's already on disk.

    Return false if the file already exists with the same content,
    true if the file was changed or created.
    """

    normalized_payload = normalize_document(payload)
    content = (
        json.dumps(normalized_payload, indent=2, sort_keys=True, cls=DateTimeEncoder)
        + "\n"
    )
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    if dry_run:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def iter_project_doc_refs(
    folders: list[dict[str, Any]],
    parent_parts: tuple[str, ...] = (),
):
    """
    For all the folders and subfolders in the given list, yield tuples of (folder_parts, doc_name, doc_id)
    for each document reference found, where:

    - folder_parts is a tuple of folder names leading to the document, excluding "rootFolder"
    - doc_name is the name of the document
    - doc_id is the unique identifier of the document
    """

    for folder in folders:
        folder_name = folder.get("name")
        folder_parts = parent_parts
        if folder_name and folder_name != "rootFolder":
            folder_parts = (*parent_parts, folder_name)

        for doc_ref in folder.get("docs", []):
            doc_name = doc_ref.get("name")
            doc_id = doc_ref.get("_id")
            if doc_name and doc_id:
                yield folder_parts, doc_name, doc_id

        child_folders = folder.get("folders", [])
        if child_folders:
            yield from iter_project_doc_refs(child_folders, folder_parts)


def iter_project_file_refs(
    folders: list[dict[str, Any]],
    parent_parts: tuple[str, ...] = (),
):
    """
    For all the folders and subfolders in the given list, yield AssetLocator objects for each file
    reference found, where the relative_path is constructed from the folder hierarchy and file name,
    and the file_id, file_name, and file_hash are taken from the file reference.
    """

    for folder in folders:
        folder_name = folder.get("name")
        folder_parts = parent_parts
        if folder_name and folder_name != "rootFolder":
            folder_parts = (*parent_parts, folder_name)

        for file_ref in folder.get("fileRefs", []):
            file_name = file_ref.get("name")
            file_id = file_ref.get("_id")
            if file_name and file_id:
                yield AssetLocator(
                    relative_path=Path(*folder_parts, file_name),
                    file_id=str(file_id),
                    file_name=file_name,
                    file_hash=file_ref.get("hash"),
                )

        child_folders = folder.get("folders", [])
        if child_folders:
            yield from iter_project_file_refs(child_folders, folder_parts)


def render_doc_content(lines: list[str]) -> str:
    """
    Render the content of a document from its list of lines by stripping trailing newlines from each line and joining them with a single newline character,
    and ensuring the final content ends with a newline if there are any lines at all.

    return the rendered document content as a single string, or an empty string if there are no lines.
    """

    if not lines:
        return ""
    return "\n".join(line.rstrip("\n") for line in lines) + "\n"


def fetch_project_docs(
    database: Any,
    root_folders: list[dict[str, Any]],
) -> dict[str, tuple[Path, str]]:
    """
    Given a list of root folders from a project document, fetch the content of all referenced documents from the database
    and return a mapping of relative file path to a tuple of (relative_path, rendered_content),
    """

    doc_refs = list(iter_project_doc_refs(root_folders))
    if not doc_refs:
        return {}

    doc_ids = [doc_id for _, _, doc_id in doc_refs]
    docs_by_id = {
        str(doc["_id"]): doc
        for doc in database.docs.find({"_id": {"$in": doc_ids}}, {"_id": 1, "lines": 1})
    }

    project_docs: dict[str, tuple[Path, str]] = {}
    for folder_parts, doc_name, doc_id in doc_refs:
        doc = docs_by_id.get(str(doc_id))
        if doc is None:
            continue
        relative_path = Path(*folder_parts, doc_name)
        project_docs[str(relative_path)] = (
            relative_path,
            render_doc_content(doc.get("lines", [])),
        )
    return project_docs


def export_project_sources(
    database: Any,
    project: dict[str, Any],
    target_dir: Path,
    dry_run: bool,
) -> bool:
    """
    For a given project document, fetch the content of all its referenced documents and write them to the target directory,
    preserving the folder structure and only writing files that have changed from what's already on disk (via write_text).
    """

    changed = False
    for relative_path, content in fetch_project_docs(
        database, project.get("rootFolder", [])
    ).values():
        changed |= write_text(target_dir / relative_path, content, dry_run)
    return changed


def build_asset_context(project_id: str, locator: AssetLocator) -> dict[str, str]:
    """Prepare template variables used to resolve an uploaded asset's storage path."""

    file_hash = locator.file_hash or ""
    return {
        "project_id": project_id,
        "file_id": locator.file_id,
        "name": locator.file_name,
        "hash": file_hash,
        "hash_prefix2": file_hash[:2],
        "hash_prefix2_b": file_hash[2:4],
        "hash_prefix3": file_hash[:3],
    }


def build_asset_templates() -> list[str]:
    """Return asset path templates from the environment or the built-in defaults."""

    templates = env_list("OVERLEAF_ASSET_PATH_TEMPLATES")
    if templates:
        return templates
    return DEFAULT_ASSET_PATH_TEMPLATES


def build_s3_buckets(args: argparse.Namespace) -> list[str]:
    """Split the configured S3 bucket list into trimmed bucket names."""

    if not args.s3_bucket:
        return []
    return [
        bucket.strip() for bucket in str(args.s3_bucket).split(";") if bucket.strip()
    ]


def extract_hash_from_s3_key(key: str) -> str | None:
    """Infer a 40-character asset hash from the trailing path segments of an S3 key."""

    parts = key.split("/")
    if len(parts) < 2:
        return None
    prefix = parts[-2]
    suffix = parts[-1]
    candidate = f"{prefix}{suffix}"
    if (
        len(prefix) == 2
        and len(candidate) == 40
        and all(ch in "0123456789abcdef" for ch in candidate.lower())
    ):
        return candidate.lower()
    return None


def build_s3_hash_index(
    client: Any,
    bucket: str,
) -> dict[str, str]:
    """Index bucket objects by inferred asset hash for fallback S3 lookups."""

    paginator = client.get_paginator("list_objects_v2")
    index: dict[str, str] = {}
    for page in paginator.paginate(Bucket=bucket):
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not key:
                continue
            asset_hash = extract_hash_from_s3_key(key)
            if asset_hash:
                index.setdefault(asset_hash, key)
    return index


def render_asset_template(template: str, context: dict[str, str]) -> str:
    """Render a storage path template and suppress missing-placeholder failures."""

    try:
        return template.format(**context).strip("/\\")
    except KeyError:
        return ""


def resolve_asset_from_mongo(database: Any, asset_hash: str | None) -> bytes | None:
    """Load an uploaded asset payload from MongoDB history blobs by content hash."""

    if not asset_hash:
        return None

    prefix = asset_hash[:3]
    projection = {f"blobs.{prefix}": 1}
    for doc in database.projectHistoryBlobs.find(
        {f"blobs.{prefix}": {"$exists": True}},
        projection,
    ):
        bucket = doc.get("blobs", {}).get(prefix, [])
        for item in bucket:
            if item.get("h") != asset_hash:
                continue
            payload = item.get("b")
            if payload is None:
                return None
            return bytes(payload)
    return None


def resolve_asset_from_filestore(
    args: argparse.Namespace,
    context: dict[str, str],
) -> bytes | None:
    """Load an uploaded asset from the local Overleaf filestore if configured."""

    if not args.filestore_root:
        return None

    filestore_root = Path(args.filestore_root).expanduser()
    for template in build_asset_templates():
        rendered = render_asset_template(template, context)
        if not rendered:
            continue
        candidate_path = filestore_root / Path(rendered)
        if candidate_path.is_file():
            return candidate_path.read_bytes()
    return None


def create_s3_client(args: argparse.Namespace):
    """Create an S3 client configured for the export settings and TLS options."""

    try:
        import boto3  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            "S3 asset export requires boto3. Install dependencies with `pip install -r requirements.txt`."
        ) from exc

    session = boto3.session.Session(
        aws_access_key_id=args.s3_access_key_id or None,
        aws_secret_access_key=args.s3_secret_access_key or None,
        region_name=args.s3_region or None,
    )
    verify: bool | str = True
    if args.s3_ca_bundle:
        verify = args.s3_ca_bundle
    elif not args.s3_verify_ssl:
        verify = False
    return session.client(
        "s3",
        endpoint_url=args.s3_endpoint_url or None,
        verify=verify,
    )


def resolve_asset_from_s3(
    args: argparse.Namespace,
    context: dict[str, str],
    state: dict[str, Any],
) -> bytes | None:
    """Fetch an uploaded asset from S3 using template paths and hash-based fallback lookup."""

    buckets = build_s3_buckets(args)
    if not buckets:
        return None

    if "s3_client" not in state:
        state["s3_client"] = create_s3_client(args)

    client = state["s3_client"]
    prefix = args.s3_prefix.strip("/")
    for bucket in buckets:
        for template in build_asset_templates():
            rendered = render_asset_template(template, context)
            if not rendered:
                continue
            key = f"{prefix}/{rendered}" if prefix else rendered
            try:
                response = client.get_object(Bucket=bucket, Key=key)
            except Exception as exc:
                code = getattr(exc, "response", {}).get("Error", {}).get("Code")
                if code in {"NoSuchKey", "404", "NotFound", "NoSuchBucket"}:
                    continue
                raise SystemExit(
                    f"S3 asset download failed for bucket {bucket} key {key}: {exc}"
                ) from exc
            return response["Body"].read()

        asset_hash = context.get("hash")
        if asset_hash:
            if "s3_hash_index" not in state:
                state["s3_hash_index"] = {}
            hash_index = state["s3_hash_index"]
            if bucket not in hash_index:
                hash_index[bucket] = build_s3_hash_index(client, bucket)
            indexed_key = hash_index[bucket].get(asset_hash.lower())
            if indexed_key:
                try:
                    response = client.get_object(Bucket=bucket, Key=indexed_key)
                except Exception as exc:
                    raise SystemExit(
                        f"S3 asset download failed for bucket {bucket} key {indexed_key}: {exc}"
                    ) from exc
                return response["Body"].read()
    return None


def resolve_asset_bytes(
    args: argparse.Namespace,
    database: Any,
    project_id: str,
    locator: AssetLocator,
    state: dict[str, Any],
) -> bytes | None:
    """Resolve uploaded asset bytes from the configured backing store sequence."""

    context = build_asset_context(project_id, locator)
    stores = {
        "mongo": ["mongo"],
        "filesystem": ["filesystem"],
        "s3": ["s3"],
        "auto": ["mongo", "filesystem", "s3"],
    }[args.asset_store]

    for store in stores:
        if store == "mongo":
            payload = resolve_asset_from_mongo(database, locator.file_hash)
        elif store == "filesystem":
            payload = resolve_asset_from_filestore(args, context)
        else:
            payload = resolve_asset_from_s3(args, context, state)
        if payload is not None:
            return payload
    return None


def export_project_assets(
    args: argparse.Namespace,
    database: Any,
    project: dict[str, Any],
    target_dir: Path,
    dry_run: bool,
    state: dict[str, Any],
) -> tuple[bool, list[dict[str, Any]]]:
    """Export uploaded assets for a project and report any unresolved file references."""

    changed = False
    unresolved_assets: list[dict[str, Any]] = []
    project_id = str(project["_id"])

    for locator in iter_project_file_refs(project.get("rootFolder", [])):
        payload = resolve_asset_bytes(args, database, project_id, locator, state)
        if payload is None:
            unresolved_assets.append(
                {
                    "path": str(locator.relative_path),
                    "file_id": locator.file_id,
                    "hash": locator.file_hash,
                }
            )
            continue
        changed |= write_bytes(target_dir / locator.relative_path, payload, dry_run)

    return changed, unresolved_assets


def parse_project_ids(values: list[str]) -> list[str | ObjectId]:
    """Parse project id filters into Mongo-ready string or ObjectId values."""

    parsed: list[str | ObjectId] = []
    for value in values:
        parsed.append(ObjectId(value) if ObjectId.is_valid(value) else value)
    return parsed


def build_mongo_uri(args: argparse.Namespace) -> str:
    """Construct a MongoDB connection URI from CLI and environment settings."""

    if args.mongo_uri:
        return args.mongo_uri

    if bool(args.mongo_username) != bool(args.mongo_password):
        raise SystemExit(
            "MongoDB username and password must be provided together when using credential flags."
        )

    auth_prefix = ""
    query_params: dict[str, str] = {}
    if args.mongo_username and args.mongo_password:
        auth_prefix = (
            f"{quote_plus(args.mongo_username)}:{quote_plus(args.mongo_password)}@"
        )
        query_params["authSource"] = args.mongo_auth_db

    if args.mongo_auth_mechanism:
        query_params["authMechanism"] = args.mongo_auth_mechanism
    if args.mongo_tls:
        query_params["tls"] = "true"

    query_string = urlencode(query_params)
    uri = f"mongodb://{auth_prefix}{args.mongo_host}:{args.mongo_port}/"
    return f"{uri}?{query_string}" if query_string else uri


def create_mongo_client(args: argparse.Namespace) -> MongoClient:
    """Create a MongoClient using the resolved URI and timeout settings."""

    return MongoClient(
        build_mongo_uri(args),
        serverSelectionTimeoutMS=args.connect_timeout_ms,
    )


def check_connection(args: argparse.Namespace) -> ConnectionCheckResult:
    """Ping MongoDB and return basic database details for a connectivity check."""

    client = create_mongo_client(args)
    try:
        database = client[args.db_name]
        client.admin.command("ping")
        collection_names = sorted(database.list_collection_names())
        sample_project = database.projects.find_one({}, {"_id": 1})
        sample_project_id = None
        if sample_project and sample_project.get("_id") is not None:
            sample_project_id = str(sample_project["_id"])
        return ConnectionCheckResult(
            database_name=args.db_name,
            collections=collection_names,
            sample_project_id=sample_project_id,
        )
    except PyMongoError as exc:
        raise SystemExit(f"MongoDB connection check failed: {exc}") from exc
    finally:
        client.close()


def build_projects_query(args: argparse.Namespace) -> dict[str, Any]:
    """Build the MongoDB query used to limit the project export scope."""

    query: dict[str, Any] = {}
    if args.project_id:
        query["_id"] = {"$in": parse_project_ids(args.project_id)}
    return query


def build_projects_cursor(
    database: Any,
    args: argparse.Namespace,
    projection: dict[str, Any] | None = None,
):
    """Return the projects cursor sorted by most recently updated first."""

    cursor = database.projects.find(build_projects_query(args), projection).sort(
        "lastUpdated", -1
    )
    if args.limit:
        cursor = cursor.limit(args.limit)
    return cursor


def collect_project_metadata(
    database: Any,
    args: argparse.Namespace,
) -> list[ProjectSyncInfo]:
    """Fetch lightweight project metadata used to compute the sync plan."""

    try:
        cursor = build_projects_cursor(
            database,
            args,
            {
                "_id": 1,
                "name": 1,
                "lastUpdated": 1,
                "version": 1,
                "trashed": 1,
            },
        )
        return [build_project_sync_info(project) for project in cursor]
    except PyMongoError as exc:
        raise SystemExit(
            f"MongoDB export failed while scanning metadata: {exc}"
        ) from exc


def fetch_projects_by_ids(
    database: Any,
    project_ids: list[str],
) -> list[dict[str, Any]]:
    """Load full project documents and preserve the caller's requested id order."""

    if not project_ids:
        return []

    try:
        cursor = database.projects.find(
            {"_id": {"$in": parse_project_ids(project_ids)}}
        )
        projects_by_id = {str(project["_id"]): project for project in cursor}
    except PyMongoError as exc:
        raise SystemExit(
            f"MongoDB export failed while loading projects: {exc}"
        ) from exc

    return [
        projects_by_id[project_id]
        for project_id in project_ids
        if project_id in projects_by_id
    ]


def is_partial_sync(args: argparse.Namespace) -> bool:
    """Return whether the current run targets only a subset of projects."""

    return bool(args.project_id) or bool(args.limit)


def resolve_state_file_path(args: argparse.Namespace, output_dir: Path) -> Path:
    """Resolve the state file path relative to the chosen output directory."""

    state_file = Path(args.state_file)
    if state_file.is_absolute():
        return state_file
    return output_dir / state_file


def build_sync_plan(
    args: argparse.Namespace,
    output_dir: Path,
    project_metadata: list[ProjectSyncInfo],
    sync_state: dict[str, Any],
    config_fingerprint: str,
) -> SyncPlan:
    """Compute which projects must be exported, removed, or left untouched."""

    existing_projects = sync_state.get("projects")
    if not isinstance(existing_projects, dict):
        existing_projects = {}

    partial_sync = is_partial_sync(args)
    next_projects = dict(existing_projects) if partial_sync else {}
    force_refresh = sync_state.get("config_fingerprint") != config_fingerprint

    changed_ids: list[str] = []
    cleanup_paths: list[Path] = []
    removed_ids: set[str] = set()
    seen_ids: set[str] = set()
    active_count = 0

    for project in project_metadata:
        seen_ids.add(project.project_id)
        target_dir = output_dir / project.folder_name
        previous = existing_projects.get(project.project_id)
        previous_folder = None
        if isinstance(previous, dict):
            previous_folder = previous.get("folder_name")

        if previous_folder and previous_folder != project.folder_name:
            cleanup_paths.append(output_dir / str(previous_folder))

        if project.trashed:
            next_projects.pop(project.project_id, None)
            if previous_folder:
                cleanup_paths.append(output_dir / str(previous_folder))
                removed_ids.add(project.project_id)
            elif target_dir.exists():
                cleanup_paths.append(target_dir)
                removed_ids.add(project.project_id)
            continue

        active_count += 1
        record = build_project_state_record(project)
        next_projects[project.project_id] = record
        if (
            force_refresh
            or build_project_state_signature(previous)
            != build_project_state_signature(record)
            or not target_dir.exists()
        ):
            changed_ids.append(project.project_id)

    if partial_sync:
        stale_ids = {
            project_id
            for project_id in args.project_id
            if project_id in existing_projects and project_id not in seen_ids
        }
    else:
        stale_ids = set(existing_projects) - seen_ids

    for project_id in stale_ids:
        previous = existing_projects.get(project_id)
        if isinstance(previous, dict):
            previous_folder = previous.get("folder_name")
            if previous_folder:
                cleanup_paths.append(output_dir / str(previous_folder))
        next_projects.pop(project_id, None)
        removed_ids.add(project_id)

    next_state_fingerprint = (
        config_fingerprint
        if not partial_sync
        else str(sync_state.get("config_fingerprint", ""))
    )
    next_state = {
        "format_version": SYNC_STATE_FORMAT_VERSION,
        "config_fingerprint": next_state_fingerprint,
        "projects": next_projects,
    }
    return SyncPlan(
        changed_ids=changed_ids,
        cleanup_paths=dedupe_paths(cleanup_paths),
        removed_ids=removed_ids,
        next_state=next_state,
        active_count=active_count,
    )


def iter_projects(args: argparse.Namespace):
    """Yield project documents from MongoDB while managing the client lifecycle."""

    client = create_mongo_client(args)
    database = client[args.db_name]

    try:
        for project in build_projects_cursor(database, args):
            yield project
    except PyMongoError as exc:
        raise SystemExit(f"MongoDB export failed: {exc}") from exc
    finally:
        client.close()


def resolve_git_repo_dir(args: argparse.Namespace) -> Path:
    """Resolve the configured git working tree path to an absolute directory."""

    return Path(args.git_repo_dir).resolve()


def resolve_output_dir(args: argparse.Namespace) -> Path:
    """Resolve the export output directory relative to the git repo when needed."""

    output_dir = Path(args.output_dir)
    if output_dir.is_absolute():
        return output_dir
    return resolve_git_repo_dir(args) / output_dir


def ensure_safe_push_target(repo_dir: Path) -> None:
    """Block pushes that would target the source repository containing this script."""

    script_repo_dir = Path(__file__).resolve().parent
    if repo_dir.resolve() == script_repo_dir:
        raise SystemExit(
            "Refusing to push exports from the source code repository that contains "
            "sync.py. Set GIT_REPO_DIR to a separate checkout or empty directory "
            "for the export repository."
        )


def export_projects(args: argparse.Namespace, output_dir: Path) -> ExportResult:
    """Export changed projects, remove stale ones, and update incremental sync state."""

    stats = ExportStats()
    client = create_mongo_client(args)
    database = client[args.db_name]
    state: dict[str, Any] = {}
    project_changes: list[ProjectCommitChange] = []
    output_dir.mkdir(parents=True, exist_ok=True)
    state_file = resolve_state_file_path(args, output_dir)
    current_state = load_sync_state(state_file)

    try:
        project_metadata = collect_project_metadata(database, args)
        stats.scanned = len(project_metadata)
        sync_state = clone_sync_state(current_state)
        config_fingerprint = build_sync_config_fingerprint(args)
        sync_plan = build_sync_plan(
            args,
            output_dir,
            project_metadata,
            sync_state,
            config_fingerprint,
        )

        current_state["format_version"] = SYNC_STATE_FORMAT_VERSION
        current_state["config_fingerprint"] = sync_plan.next_state["config_fingerprint"]
        current_state["projects"] = dict(current_state.get("projects", {}))

        projects = fetch_projects_by_ids(database, sync_plan.changed_ids)
        changed_user_ids = {
            str(raw_user_id)
            for project in projects
            for raw_user_id in [project.get("lastUpdatedBy"), project.get("owner_ref")]
            if raw_user_id
        }
        users_by_id = fetch_users_by_ids(database, changed_user_ids)
        exported_ids: set[str] = set()
        existing_projects = sync_state.get("projects", {})
        for project in projects:
            project_id = str(project["_id"])
            project_info = build_project_sync_info(project)
            target_dir = output_dir / project_info.folder_name
            previous_record = existing_projects.get(project_id)
            previous_folder_name = None
            if isinstance(previous_record, dict):
                previous_folder_name = previous_record.get("folder_name")
            change_paths: list[Path] = []
            if (
                previous_folder_name
                and previous_folder_name != project_info.folder_name
            ):
                old_target_dir = output_dir / str(previous_folder_name)
                if remove_path(old_target_dir, args.dry_run):
                    print(f"removed {old_target_dir}")
                change_paths.append(old_target_dir)

            exported_ids.add(project_id)
            prepare_target_dir(target_dir, args.dry_run)
            change_paths.append(target_dir)

            changed = False
            user_id, user_name = resolve_project_user_identity(project, users_by_id)
            manifest = build_project_manifest(project, users_by_id)
            changed |= write_json(target_dir / "project.json", manifest, args.dry_run)
            changed |= export_project_sources(
                database, project, target_dir, args.dry_run
            )
            asset_changed, unresolved_assets = export_project_assets(
                args,
                database,
                project,
                target_dir,
                args.dry_run,
                state,
            )
            changed |= asset_changed

            if args.include_raw:
                raw_project = normalize_document(project)
                changed |= write_json(
                    target_dir / "project.raw.json", raw_project, args.dry_run
                )

            if unresolved_assets:
                print(
                    f"warning: could not resolve {len(unresolved_assets)} uploaded assets in {target_dir}; "
                    "configure OVERLEAF_FILESTORE_ROOT or S3 settings if this deployment stores assets externally"
                )

            current_state["projects"][project_id] = build_project_state_record(
                project_info,
                resolved_editor_id=user_id,
                resolved_editor_name=user_name,
            )

            if not changed:
                print(f"unchanged {target_dir}")
                continue

            stats.changed += 1
            print(f"updated {target_dir}")
            project_changes.append(
                ProjectCommitChange(
                    project_id=project_id,
                    project_name=project_info.project_name,
                    user_id=user_id,
                    user_name=user_name,
                    paths=tuple(dedupe_paths(change_paths)),
                    state_after=clone_sync_state(current_state),
                )
            )

        missing_ids = [
            project_id
            for project_id in sync_plan.changed_ids
            if project_id not in exported_ids
        ]
        for project_id in missing_ids:
            previous = sync_plan.next_state["projects"].pop(project_id, None)
            removed_path = None
            if isinstance(previous, dict) and previous.get("folder_name"):
                removed_path = output_dir / str(previous["folder_name"])
            if removed_path and remove_path(removed_path, args.dry_run):
                print(f"removed {removed_path}")
            sync_plan.removed_ids.add(project_id)
            previous_saved_record = existing_projects.get(project_id, previous)
            user_id, user_name = resolve_saved_project_user_identity(
                previous_saved_record
            )
            current_state["projects"].pop(project_id, None)
            project_changes.append(
                ProjectCommitChange(
                    project_id=project_id,
                    project_name=project_id,
                    user_id=user_id,
                    user_name=user_name,
                    paths=tuple(path for path in [removed_path] if path is not None),
                    state_after=clone_sync_state(current_state),
                )
            )
            print(
                f"warning: project {project_id} disappeared before it could be exported; removed it from sync state"
            )

        removed_project_ids = [
            project_id
            for project_id in sorted(sync_plan.removed_ids)
            if project_id not in missing_ids
        ]
        existing_projects = sync_state.get("projects", {})
        for project_id in removed_project_ids:
            previous_record = existing_projects.get(project_id)
            if not isinstance(previous_record, dict):
                continue
            removed_path = None
            folder_name = previous_record.get("folder_name")
            if folder_name:
                removed_path = output_dir / str(folder_name)
                if remove_path(removed_path, args.dry_run):
                    print(f"removed {removed_path}")
            user_id, user_name = resolve_saved_project_user_identity(previous_record)
            current_state["projects"].pop(project_id, None)
            project_changes.append(
                ProjectCommitChange(
                    project_id=project_id,
                    project_name=str(previous_record.get("project_name") or project_id),
                    user_id=user_id,
                    user_name=user_name,
                    paths=tuple(path for path in [removed_path] if path is not None),
                    state_after=clone_sync_state(current_state),
                )
            )

        stats.unchanged = max(sync_plan.active_count - stats.changed, 0)
        stats.deleted = len(sync_plan.removed_ids)
        write_json(state_file, current_state, args.dry_run)
    finally:
        client.close()

    return ExportResult(
        stats=stats,
        project_changes=project_changes,
        state_file=state_file,
        final_state=current_state,
    )


def resolve_git_remote_url(args: argparse.Namespace) -> str:
    """Return the configured git remote URL or exit with a clear error."""

    if not args.git_remote_url:
        raise SystemExit(
            "Git remote URL is required. Provide --git-remote-url or set GITLAB_REMOTE_URL."
        )
    return args.git_remote_url


def resolve_git_auth_mode(args: argparse.Namespace, remote_url: str) -> str:
    """Validate git auth settings and report which authentication mode will be used."""

    if args.git_access_token:
        if not uses_http_remote(remote_url):
            raise SystemExit(
                "--git-access-token requires an HTTP(S) git remote URL, for example https://gitlab.example.com/group/project.git."
            )
        return "project-access-token"

    if args.git_ssh_key_path:
        if uses_http_remote(remote_url):
            raise SystemExit(
                "--git-ssh-key-path requires an SSH git remote URL, for example git@gitlab.example.com:group/project.git."
            )
        return "ssh-key"

    raise SystemExit(
        "Git authentication is required. Provide either --git-access-token for HTTPS or --git-ssh-key-path for SSH."
    )


def run_git_external(
    git_args: list[str],
    env: dict[str, str],
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command outside the export repo and optionally fail on non-zero exit."""

    result = subprocess.run(
        ["git", *git_args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(git_args[:2])} failed with code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def parse_ls_remote_head(stdout: str) -> str | None:
    """Extract the symbolic HEAD reference from `git ls-remote --symref` output."""

    for line in stdout.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            return line.split("\t", 1)[0].replace("ref: ", "", 1)
    for line in stdout.splitlines():
        if line.endswith("\tHEAD"):
            return "HEAD"
    return None


def check_git_access(args: argparse.Namespace) -> GitCheckResult:
    """Verify remote git access and report the detected HEAD reference."""

    remote_url = resolve_git_remote_url(args)
    auth_mode = resolve_git_auth_mode(args, remote_url)
    env = build_git_env(args)
    result = run_git_external(["ls-remote", "--symref", remote_url, "HEAD"], env)
    return GitCheckResult(
        remote_url=remote_url,
        auth_mode=auth_mode,
        head_reference=parse_ls_remote_head(result.stdout),
    )


def add_git_config_env(env: dict[str, str], key: str, value: str) -> None:
    """Append an in-memory git config override via `GIT_CONFIG_COUNT` variables."""

    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 1)


def uses_http_remote(remote_url: str) -> bool:
    """Return whether a git remote URL uses HTTP or HTTPS transport."""

    return remote_url.startswith(("https://", "http://"))


def build_git_http_auth_header(args: argparse.Namespace) -> str:
    """Build the HTTP Basic auth header Git uses for token-based pushes."""

    if not args.git_http_username.strip():
        raise SystemExit(
            "--git-http-username must be set when using --git-access-token. "
            "Use `oauth2` for a personal access token, the generated bot username "
            "for a project access token, or the deploy token username."
        )

    credentials = f"{args.git_http_username}:{args.git_access_token}".encode("utf-8")
    encoded = base64.b64encode(credentials).decode("ascii")
    return f"Authorization: Basic {encoded}"


def validate_git_ssh_private_key(ssh_key_path: Path) -> None:
    """Validate SSH private key basics so auth failures are easier to diagnose."""

    if not ssh_key_path.is_file():
        raise SystemExit(
            f"SSH key file not found at {ssh_key_path}. "
            "Set --git-ssh-key-path to a mounted private key file."
        )

    try:
        key_text = ssh_key_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"Unable to read SSH key file {ssh_key_path}: {exc}") from exc

    if "\r" in key_text:
        raise SystemExit(
            f"SSH key file {ssh_key_path} contains Windows (CRLF) line endings. "
            "Use LF line endings when creating the Kubernetes secret."
        )

    key_text = key_text.strip()
    if not key_text:
        raise SystemExit(f"SSH key file {ssh_key_path} is empty.")

    supported_headers = (
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "-----BEGIN RSA PRIVATE KEY-----",
        "-----BEGIN EC PRIVATE KEY-----",
        "-----BEGIN DSA PRIVATE KEY-----",
        "-----BEGIN PRIVATE KEY-----",
    )
    if not key_text.startswith(supported_headers):
        raise SystemExit(
            f"SSH key file {ssh_key_path} does not look like a private key. "
            "If this key is in a Kubernetes Secret, create it from a file (for example `kubectl create secret generic ... --from-file=id_rsa=...`) "
            "so newlines are preserved."
        )

    encrypted_headers = (
        "-----BEGIN ENCRYPTED PRIVATE KEY-----",
        "Proc-Type: 4,ENCRYPTED",
    )
    if any(marker in key_text for marker in encrypted_headers):
        raise SystemExit(
            f"SSH key file {ssh_key_path} appears to be encrypted. "
            "Use an unencrypted deploy key for non-interactive sync jobs."
        )


def build_git_env(args: argparse.Namespace) -> dict[str, str]:
    """Prepare a non-interactive git environment with author and auth settings."""

    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": args.git_commit_name,
            "GIT_AUTHOR_EMAIL": args.git_commit_email,
            "GIT_COMMITTER_NAME": args.git_commit_name,
            "GIT_COMMITTER_EMAIL": args.git_commit_email,
            # Keep automation non-interactive so Git Credential Manager does not
            # pop a login prompt when token auth is missing or rejected.
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }
    )

    if args.git_access_token:
        add_git_config_env(env, "http.extraHeader", build_git_http_auth_header(args))

    if args.git_ssh_key_path and not args.git_access_token:
        ssh_key_path = Path(args.git_ssh_key_path).expanduser().resolve()
        validate_git_ssh_private_key(ssh_key_path)
        env["GIT_SSH_COMMAND"] = (
            f'ssh -i "{ssh_key_path}" -o IdentitiesOnly=yes '
            "-o StrictHostKeyChecking=accept-new"
        )
    return env


def run_git(
    repo_dir: Path,
    git_args: list[str],
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command inside the export repository and optionally enforce success."""

    result = subprocess.run(
        ["git", *git_args],
        cwd=repo_dir,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise SystemExit(
            f"git {' '.join(git_args)} failed with code {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def ensure_output_in_repo(repo_dir: Path, output_dir: Path) -> Path:
    """Confirm the export output lives inside the git repo and return its relative path."""

    try:
        return output_dir.resolve().relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise SystemExit(
            f"Output directory {output_dir} must be inside git repo directory {repo_dir} when --push is used."
        ) from exc


def ensure_git_repo(
    args: argparse.Namespace, repo_dir: Path, env: dict[str, str]
) -> None:
    """Initialize the export repo if needed and validate or add the configured remote."""

    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        run_git(repo_dir, ["init"], env)

    if args.git_remote_url:
        remote_check = run_git(
            repo_dir,
            ["remote", "get-url", args.git_remote_name],
            env,
            check=False,
        )
        if remote_check.returncode == 0:
            existing_remote_url = remote_check.stdout.strip()
            if existing_remote_url != args.git_remote_url:
                raise SystemExit(
                    f"Git remote '{args.git_remote_name}' in {repo_dir} already "
                    f"points to {existing_remote_url}. Refusing to overwrite it; "
                    "choose a different GIT_REPO_DIR or remote name."
                )
        else:
            run_git(
                repo_dir,
                ["remote", "add", args.git_remote_name, args.git_remote_url],
                env,
            )


def ensure_clean_git_worktree(repo_dir: Path, env: dict[str, str]) -> None:
    """Require a clean git worktree before syncing from or pushing to the remote."""

    status = run_git(repo_dir, ["status", "--porcelain"], env)
    if status.stdout.strip():
        raise SystemExit(
            f"Git repo {repo_dir} has uncommitted changes. Commit or clean them "
            "before running a pushed sync."
        )


def sync_git_repo_before_export(
    args: argparse.Namespace, repo_dir: Path, env: dict[str, str]
) -> None:
    """Fetch the remote and check out the target branch before exporting."""

    ensure_clean_git_worktree(repo_dir, env)
    run_git(repo_dir, ["fetch", args.git_remote_name], env)

    remote_branch_ref = f"refs/remotes/{args.git_remote_name}/{args.git_branch}"
    remote_branch = run_git(
        repo_dir,
        ["rev-parse", "--verify", remote_branch_ref],
        env,
        check=False,
    )
    if remote_branch.returncode == 0:
        run_git(
            repo_dir,
            [
                "checkout",
                "-B",
                args.git_branch,
                f"{args.git_remote_name}/{args.git_branch}",
            ],
            env,
        )
        return

    run_git(repo_dir, ["checkout", "-B", args.git_branch], env)


def prepare_git_repo_for_export(args: argparse.Namespace) -> None:
    """Validate git settings and fast-forward or create the target export branch."""

    remote_url = resolve_git_remote_url(args)
    resolve_git_auth_mode(args, remote_url)

    repo_dir = resolve_git_repo_dir(args)
    ensure_safe_push_target(repo_dir)
    env = build_git_env(args)
    ensure_git_repo(args, repo_dir, env)
    sync_git_repo_before_export(args, repo_dir, env)


def build_commit_message(stats: ExportStats) -> str:
    """Generate the default summary commit message for a sync run."""

    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"Sync Overleaf export ({stats.changed} changed, {stats.scanned} scanned) "
        f"at {timestamp}"
    )


def build_project_commit_message(
    change: ProjectCommitChange,
    base_message: str | None = None,
) -> str:
    """Generate a per-project commit message, optionally prefixed by a custom base message."""

    user_name = normalize_user_name(change.user_name)
    if base_message:
        return f"{base_message}: {change.project_name} by {user_name}"
    return f'Sync project "{change.project_name}" by {user_name}'


def stage_paths_for_commit(
    repo_dir: Path,
    env: dict[str, str],
    paths: list[Path],
) -> list[str]:
    """Stage the given paths and return their repo-relative, de-duplicated names."""

    relative_paths = [str(ensure_output_in_repo(repo_dir, path)) for path in paths]
    unique_relative_paths = list(dict.fromkeys(relative_paths))
    run_git(repo_dir, ["add", "--all", "--force", "--", *unique_relative_paths], env)
    return unique_relative_paths


def has_staged_changes(
    repo_dir: Path,
    env: dict[str, str],
    relative_paths: list[str],
) -> bool:
    """Return whether the staged diff includes changes for the requested paths."""

    staged_diff = run_git(
        repo_dir,
        ["diff", "--cached", "--quiet", "--", *relative_paths],
        env,
        check=False,
    )
    if staged_diff.returncode == 0:
        return False
    if staged_diff.returncode != 1:
        raise SystemExit(
            "Unable to determine whether exported files changed after staging."
        )
    return True


def push_export_to_git(
    args: argparse.Namespace,
    output_dir: Path,
    export_result: ExportResult | ExportStats,
) -> None:
    """Commit exported changes and push them to the configured git remote."""

    remote_url = resolve_git_remote_url(args)
    resolve_git_auth_mode(args, remote_url)

    repo_dir = resolve_git_repo_dir(args)
    ensure_safe_push_target(repo_dir)
    env = build_git_env(args)
    ensure_git_repo(args, repo_dir, env)
    relative_output_dir = ensure_output_in_repo(repo_dir, output_dir)

    if isinstance(export_result, ExportStats):
        run_git(repo_dir, ["add", "--all", "--force", str(relative_output_dir)], env)
        staged_diff = run_git(
            repo_dir,
            ["diff", "--cached", "--quiet", "--", str(relative_output_dir)],
            env,
            check=False,
        )
        if staged_diff.returncode == 0:
            print("no exported file changes to commit")
            return
        if staged_diff.returncode != 1:
            raise SystemExit(
                "Unable to determine whether exported files changed after staging."
            )

        commit_message = args.git_commit_message or build_commit_message(export_result)
        run_git(repo_dir, ["commit", "-m", commit_message], env)
        run_git(repo_dir, ["push", args.git_remote_name, args.git_branch], env)
        print(
            f"pushed exported changes from {relative_output_dir} to "
            f"{args.git_remote_name}/{args.git_branch}"
        )
        return

    state_file = export_result.state_file
    final_state = export_result.final_state
    commits_created = 0

    for change in export_result.project_changes:
        write_json(state_file, change.state_after, dry_run=False)
        paths_to_stage = [*change.paths, state_file]
        relative_paths = stage_paths_for_commit(repo_dir, env, paths_to_stage)
        if not has_staged_changes(repo_dir, env, relative_paths):
            continue
        commit_message = build_project_commit_message(
            change,
            base_message=args.git_commit_message,
        )
        run_git(repo_dir, ["commit", "-m", commit_message], env)
        commits_created += 1

    write_json(state_file, final_state, dry_run=False)
    remaining_paths = stage_paths_for_commit(repo_dir, env, [state_file])
    if has_staged_changes(repo_dir, env, remaining_paths):
        commit_message = args.git_commit_message or build_commit_message(
            export_result.stats
        )
        run_git(repo_dir, ["commit", "-m", commit_message], env)
        commits_created += 1

    if commits_created == 0:
        print("no exported file changes to commit")
        return

    run_git(repo_dir, ["push", args.git_remote_name, args.git_branch], env)
    print(
        f"pushed exported changes from {relative_output_dir} to "
        f"{args.git_remote_name}/{args.git_branch}"
    )


def main() -> int:
    """Parse CLI arguments, run the requested sync action, and report the result."""

    args = build_parser().parse_args()

    check_mongo = bool(
        getattr(args, "check_connection_mongo", False)
        or getattr(args, "check_connection", False)
    )
    check_git = bool(
        getattr(args, "check_connection_git", False)
        or getattr(args, "check_git", False)
    )

    if check_mongo:
        result = check_connection(args)
        print(f"connected to MongoDB database '{result.database_name}'")
        print(f"discovered {len(result.collections)} collections")
        if result.sample_project_id:
            print(f"sample project id: {result.sample_project_id}")
        else:
            print("projects collection is reachable but currently empty")
        return 0

    if check_git:
        result = check_git_access(args)
        print(f"connected to Git remote '{result.remote_url}'")
        print(f"authentication mode: {result.auth_mode}")
        if result.head_reference:
            print(f"remote HEAD resolves to: {result.head_reference}")
        else:
            print("remote is reachable but did not advertise a HEAD reference")
        return 0

    if args.push and not args.dry_run:
        prepare_git_repo_for_export(args)

    output_dir = resolve_output_dir(args)
    export_result = export_projects(args, output_dir)
    stats = export_result.stats
    print(
        "scan complete: "
        f"{stats.scanned} scanned, {stats.changed} changed, {stats.unchanged} unchanged, {stats.deleted} removed"
    )

    if args.push and args.dry_run:
        print("skipping git push because --dry-run was used")
    elif args.push:
        push_export_to_git(args, output_dir, export_result)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
