from __future__ import annotations

import argparse
import base64
import json
import os
import re
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
DEFAULT_OUTPUT_DIR = "export"
DEFAULT_MONGO_HOST = "localhost"
DEFAULT_MONGO_PORT = 27017
DEFAULT_MONGO_AUTH_DB = "admin"
DEFAULT_CONNECT_TIMEOUT_MS = 5000
DEFAULT_GIT_BRANCH = "main"
DEFAULT_GIT_REMOTE_NAME = "origin"
DEFAULT_GIT_COMMIT_NAME = "Overleaf Git Bridge"
DEFAULT_GIT_COMMIT_EMAIL = "overleaf-git-bridge@example.invalid"
DEFAULT_GIT_HTTP_USERNAME = ""


# Prefer the project's .env file over stale terminal session variables so
# connectivity checks use the values currently saved for this workspace.
load_dotenv(override=True)


@dataclass
class ExportStats:
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0


@dataclass
class ConnectionCheckResult:
    database_name: str
    collections: list[str]
    sample_project_id: str | None


@dataclass
class GitCheckResult:
    remote_url: str
    auth_mode: str
    head_reference: str | None


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, ObjectId):
            return str(obj)
        return super().default(obj)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Export Overleaf project metadata from MongoDB into a git-friendly "
            "directory structure and optionally push changes to GitLab."
        )
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
        help=f"Directory to write exported metadata. Defaults to {DEFAULT_OUTPUT_DIR}.",
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
        "--check-connection",
        action="store_true",
        help="Ping MongoDB and verify the projects collection can be read, then exit.",
    )
    parser.add_argument(
        "--check-git",
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
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    cleaned = cleaned.strip("-._")
    return cleaned or "unnamed-project"


def normalize_document(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: normalize_document(item) for key, item in value.items()}
    if isinstance(value, list):
        return [normalize_document(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "binary") and hasattr(value, "generation_time"):
        return str(value)
    return value


def build_project_manifest(project: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": str(project.get("_id")),
        "name": project.get("name"),
        "description": project.get("description"),
        "owner_ref": (
            str(project.get("owner_ref")) if project.get("owner_ref") else None
        ),
        "root_doc_id": (
            str(project.get("rootDoc_id")) if project.get("rootDoc_id") else None
        ),
        "compiler": project.get("compiler"),
        "last_updated": normalize_document(project.get("lastUpdated")),
        "last_updated_by": (
            str(project.get("lastUpdatedBy")) if project.get("lastUpdatedBy") else None
        ),
        "version": project.get("version"),
        "trashed": project.get("trashed"),
        "deleted_docs": normalize_document(project.get("deletedDocs", [])),
        "root_folder": normalize_document(project.get("rootFolder")),
        "overleaf": normalize_document(project.get("overleaf")),
    }


def write_json(path: Path, payload: dict[str, Any] | list[Any], dry_run: bool) -> bool:
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


def parse_project_ids(values: list[str]) -> list[str | ObjectId]:
    parsed: list[str | ObjectId] = []
    for value in values:
        parsed.append(ObjectId(value) if ObjectId.is_valid(value) else value)
    return parsed


def build_mongo_uri(args: argparse.Namespace) -> str:
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
    return MongoClient(
        build_mongo_uri(args),
        serverSelectionTimeoutMS=args.connect_timeout_ms,
    )


def check_connection(args: argparse.Namespace) -> ConnectionCheckResult:
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


def iter_projects(args: argparse.Namespace):
    client = create_mongo_client(args)
    database = client[args.db_name]
    query: dict[str, Any] = {}
    if args.project_id:
        query["_id"] = {"$in": parse_project_ids(args.project_id)}

    cursor = database.projects.find(query).sort("lastUpdated", -1)
    if args.limit:
        cursor = cursor.limit(args.limit)

    try:
        for project in cursor:
            yield project
    except PyMongoError as exc:
        raise SystemExit(f"MongoDB export failed: {exc}") from exc
    finally:
        client.close()


def resolve_git_repo_dir(args: argparse.Namespace) -> Path:
    return Path(args.git_repo_dir).resolve()


def resolve_output_dir(args: argparse.Namespace) -> Path:
    output_dir = Path(args.output_dir)
    if output_dir.is_absolute():
        return output_dir
    return resolve_git_repo_dir(args) / output_dir


def export_projects(args: argparse.Namespace, output_dir: Path) -> ExportStats:
    stats = ExportStats()

    for project in iter_projects(args):
        stats.scanned += 1
        project_id = str(project["_id"])
        project_name = sanitize_name(project.get("name") or project_id)
        target_dir = output_dir / f"{project_name}-{project_id}"

        changed = False
        manifest = build_project_manifest(project)
        changed |= write_json(target_dir / "project.json", manifest, args.dry_run)

        if args.include_raw:
            raw_project = normalize_document(project)
            changed |= write_json(
                target_dir / "project.raw.json", raw_project, args.dry_run
            )

        if changed:
            stats.changed += 1
            print(f"updated {target_dir}")
        else:
            stats.unchanged += 1
            print(f"unchanged {target_dir}")

    return stats


def resolve_git_remote_url(args: argparse.Namespace) -> str:
    if not args.git_remote_url:
        raise SystemExit(
            "Git remote URL is required. Provide --git-remote-url or set GITLAB_REMOTE_URL."
        )
    return args.git_remote_url


def resolve_git_auth_mode(args: argparse.Namespace, remote_url: str) -> str:
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
    for line in stdout.splitlines():
        if line.startswith("ref: ") and line.endswith("\tHEAD"):
            return line.split("\t", 1)[0].replace("ref: ", "", 1)
    for line in stdout.splitlines():
        if line.endswith("\tHEAD"):
            return "HEAD"
    return None


def check_git_access(args: argparse.Namespace) -> GitCheckResult:
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
    count = int(env.get("GIT_CONFIG_COUNT", "0"))
    env[f"GIT_CONFIG_KEY_{count}"] = key
    env[f"GIT_CONFIG_VALUE_{count}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + 1)


def uses_http_remote(remote_url: str) -> bool:
    return remote_url.startswith(("https://", "http://"))


def build_git_http_auth_header(args: argparse.Namespace) -> str:
    if not args.git_http_username.strip():
        raise SystemExit(
            "--git-http-username must be set when using --git-access-token. "
            "Use `oauth2` for a personal access token, the generated bot username "
            "for a project access token, or the deploy token username."
        )

    credentials = f"{args.git_http_username}:{args.git_access_token}".encode("utf-8")
    encoded = base64.b64encode(credentials).decode("ascii")
    return f"Authorization: Basic {encoded}"


def build_git_env(args: argparse.Namespace) -> dict[str, str]:
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
    try:
        return output_dir.resolve().relative_to(repo_dir.resolve())
    except ValueError as exc:
        raise SystemExit(
            f"Output directory {output_dir} must be inside git repo directory {repo_dir} when --push is used."
        ) from exc


def ensure_git_repo(
    args: argparse.Namespace, repo_dir: Path, env: dict[str, str]
) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        run_git(repo_dir, ["init"], env)

    run_git(repo_dir, ["checkout", "-B", args.git_branch], env)

    if args.git_remote_url:
        remote_check = run_git(
            repo_dir,
            ["remote", "get-url", args.git_remote_name],
            env,
            check=False,
        )
        if remote_check.returncode == 0:
            run_git(
                repo_dir,
                ["remote", "set-url", args.git_remote_name, args.git_remote_url],
                env,
            )
        else:
            run_git(
                repo_dir,
                ["remote", "add", args.git_remote_name, args.git_remote_url],
                env,
            )


def build_commit_message(stats: ExportStats) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return (
        f"Sync Overleaf export ({stats.changed} changed, {stats.scanned} scanned) "
        f"at {timestamp}"
    )


def push_export_to_git(
    args: argparse.Namespace, output_dir: Path, stats: ExportStats
) -> None:
    remote_url = resolve_git_remote_url(args)
    resolve_git_auth_mode(args, remote_url)

    repo_dir = resolve_git_repo_dir(args)
    env = build_git_env(args)
    ensure_git_repo(args, repo_dir, env)
    relative_output_dir = ensure_output_in_repo(repo_dir, output_dir)

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

    commit_message = args.git_commit_message or build_commit_message(stats)
    run_git(repo_dir, ["commit", "-m", commit_message], env)
    run_git(repo_dir, ["push", args.git_remote_name, args.git_branch], env)
    print(
        f"pushed exported changes from {relative_output_dir} to "
        f"{args.git_remote_name}/{args.git_branch}"
    )


def main() -> int:
    args = build_parser().parse_args()

    if args.check_connection:
        result = check_connection(args)
        print(f"connected to MongoDB database '{result.database_name}'")
        print(f"discovered {len(result.collections)} collections")
        if result.sample_project_id:
            print(f"sample project id: {result.sample_project_id}")
        else:
            print("projects collection is reachable but currently empty")
        return 0

    if args.check_git:
        result = check_git_access(args)
        print(f"connected to Git remote '{result.remote_url}'")
        print(f"authentication mode: {result.auth_mode}")
        if result.head_reference:
            print(f"remote HEAD resolves to: {result.head_reference}")
        else:
            print("remote is reachable but did not advertise a HEAD reference")
        return 0

    output_dir = resolve_output_dir(args)
    stats = export_projects(args, output_dir)
    print(
        "scan complete: "
        f"{stats.scanned} scanned, {stats.changed} changed, {stats.unchanged} unchanged"
    )

    if args.push and args.dry_run:
        print("skipping git push because --dry-run was used")
    elif args.push:
        push_export_to_git(args, output_dir, stats)

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        raise SystemExit(130)
