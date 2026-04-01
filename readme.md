# overleaf-git-bridge

Starter project for exporting Overleaf Community Edition project metadata from MongoDB into a git-friendly on-disk structure and pushing the resulting export to GitLab.

The current version focuses on the parts we can validate quickly:

- authenticate to MongoDB with either a full URI or explicit username/password settings
- verify connectivity before a full sync
- enumerate project documents from the `projects` collection
- export stable JSON snapshots for each project
- commit and push the exported output to GitLab using either a project access token over HTTPS or an SSH deploy key

That gives you a safe foundation before tackling the harder part: reconstructing full project files from the document/blob model and any S3-backed assets.

## What This Does Today

Running `sync.py` can:

- ping MongoDB and confirm the configured database is readable
- validate GitLab authentication and remote reachability before a push
- write one folder per project under the configured export directory
- save a curated `project.json` manifest for each project
- optionally save the full raw MongoDB document with `--include-raw`
- stage only the exported directory, create a commit, and push it to GitLab

Example output:

```text
export/
  my-paper-693616a50fa89c23ae8b1e99/
    project.json
    project.raw.json
```

## What It Does Not Do Yet

These are the next likely milestones:

- resolve document trees into actual `.tex`, `.bib`, image, and support files
- locate where Overleaf CE stores file contents in your deployment
- fetch blob data from S3 if your installation offloads storage there
- detect deleted exported project folders and prune them during sync
- package the sync process into a container for Kubernetes

## Quick Start

### 1. Create a virtual environment

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure MongoDB and GitLab settings

Copy `.env.example` into `.env` and fill in the values you need.

MongoDB authentication can be configured in one of two ways:

- set `OVERLEAF_MONGO_URI` with embedded credentials
- or leave `OVERLEAF_MONGO_URI` empty and set `OVERLEAF_MONGO_HOST`, `OVERLEAF_MONGO_PORT`, `OVERLEAF_MONGO_USERNAME`, `OVERLEAF_MONGO_PASSWORD`, and `OVERLEAF_MONGO_AUTH_DB`

GitLab push settings support two authentication styles:

- HTTPS with a project access token
- SSH with a deploy key

For a project access token:

- `GITLAB_REMOTE_URL`: HTTPS remote such as `https://gitlab.example.com/group/overleaf-export.git`
- `GITLAB_ACCESS_TOKEN`: project access token with `write_repository`
- `GITLAB_HTTP_USERNAME`: the generated project access token bot username
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: local checkout that will hold the export and git metadata

For a personal access token:

- `GITLAB_REMOTE_URL`: HTTPS remote such as `https://gitlab.example.com/group/overleaf-export.git`
- `GITLAB_ACCESS_TOKEN`: personal access token with `read_repository` for `--check-git` and `write_repository` for pushes
- `GITLAB_HTTP_USERNAME`: `oauth2`
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: local checkout that will hold the export and git metadata

For a deploy token:

- `GITLAB_REMOTE_URL`: HTTPS remote such as `https://gitlab.example.com/group/overleaf-export.git`
- `GITLAB_ACCESS_TOKEN`: deploy token secret
- `GITLAB_HTTP_USERNAME`: the deploy token username
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: local checkout that will hold the export and git metadata

For an SSH deploy key:

- `GITLAB_REMOTE_URL`: SSH remote such as `git@gitlab.example.com:group/overleaf-export.git`
- `GITLAB_SSH_KEY_PATH`: absolute path to the private deploy key
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: local checkout that will hold the export and git metadata

### 3. Confirm MongoDB connectivity

```powershell
python sync.py --check-connection
```

A successful check reports:

- the database name reached
- how many collections were discovered
- one sample project id, if the `projects` collection is readable and non-empty

### 4. Run an export

```powershell
python sync.py --limit 5 --include-raw
```

### 5. Confirm GitLab connectivity

```powershell
python sync.py --check-git
```

A successful check reports:

- the remote URL reached
- the authentication mode in use
- the remote `HEAD` branch when the server advertises one

### 6. Export and push to GitLab

```powershell
python sync.py --include-raw --push
```

When `--push` is enabled, the script:

- ensures the configured git working tree exists
- initializes the repository if needed
- configures the remote URL
- stages only the export directory
- commits if exported files changed
- pushes to the configured branch using either HTTPS token auth or SSH key auth
- does not fall back to interactive Git credential prompts, so failed auth surfaces as a normal error

## CLI Reference

```text
python sync.py [--mongo-uri URI] [--mongo-host HOST] [--mongo-port PORT]
               [--mongo-username USER] [--mongo-password PASSWORD]
               [--mongo-auth-db DB] [--mongo-auth-mechanism NAME] [--mongo-tls]
               [--connect-timeout-ms MS] [--db-name NAME] [--output-dir DIR]
               [--project-id ID ...] [--limit N] [--include-raw] [--dry-run]
               [--check-connection] [--check-git] [--push] [--git-repo-dir DIR]
               [--git-remote-name NAME] [--git-remote-url URL]
               [--git-branch BRANCH] [--git-ssh-key-path PATH]
               [--git-access-token TOKEN] [--git-http-username USER]
               [--git-commit-name NAME] [--git-commit-email EMAIL]
               [--git-commit-message MESSAGE]
```

## Testing

Run the automated tests with:

```powershell
python -m unittest discover -s tests -v
```

The test suite covers:

- MongoDB URI construction with username/password authentication
- the connection-check workflow
- the Git remote/authentication check workflow
- GitLab staging and push command flow for both SSH and HTTPS token auth
- safety checks that prevent pushing exports from outside the configured git repo

## Suggested Investigation Path

Once the metadata export looks correct, the next high-value step is to inspect how a project's file tree is represented across collections. A good approach is:

1. Export one known project with `--project-id ... --include-raw`.
2. Inspect `root_folder`, `root_doc_id`, and related ids in the raw JSON.
3. Query neighboring collections such as document, file, or chunk/blob collections in MongoDB.
4. Verify whether your Overleaf deployment stores binary assets in MongoDB, filesystem storage, or S3.
5. Extend `sync.py` to reconstruct a checkoutable working tree from those relationships.

## Notes

- The default database name is `sharelatex`, which is common in Overleaf CE deployments, but your environment may differ.
- `project.json` is intentionally a curated snapshot so diffs stay readable in git.
- `project.raw.json` is meant for reverse engineering and may be noisy.
- Project access tokens need the `write_repository` scope for `--push` to succeed.
- Deploy keys must be granted write access in GitLab for SSH-based `--push` to succeed.
