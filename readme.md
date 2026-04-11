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
- scan lightweight project metadata first and only fully re-export projects whose sync state changed
- write one folder per project under the configured export directory
- save a curated `project.json` manifest for each project
- reconstruct document sources from the `docs` collection into the exported project tree
- resolve uploaded `fileRefs` from Mongo history blobs, a local Overleaf filestore, or S3 when configured
- optionally save the full raw MongoDB document with `--include-raw`
- stage only the exported directory, create a commit, and push it to GitLab

Example output:

```text
gitlab-export/
  my-paper-693616a50fa89c23ae8b1e99/
    main.tex
    chapters/
      intro.tex
    assets/
      diagram.pdf
    project.json
    project.raw.json
  .sync-state.json
```

## What It Does Not Do Yet

These are the next likely milestones:

- locate where Overleaf CE stores file contents in your deployment
- detect deleted exported project folders and prune them during sync
- deepen Kubernetes deployment support beyond the included starter Helm chart

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

When using `--push`, do not point `GIT_REPO_DIR` at the same git repository that contains this tool's source code. Use a separate directory such as `gitlab-export`.

MongoDB authentication can be configured in one of two ways:

- set `OVERLEAF_MONGO_URI` with embedded credentials
- or leave `OVERLEAF_MONGO_URI` empty and set `OVERLEAF_MONGO_HOST`, `OVERLEAF_MONGO_PORT`, `OVERLEAF_MONGO_USERNAME`, `OVERLEAF_MONGO_PASSWORD`, and `OVERLEAF_MONGO_AUTH_DB`

Uploaded asset export supports three storage modes:

- `OVERLEAF_ASSET_STORE=auto`: try Mongo history blobs first, then the local filestore path, then S3 if configured
- `OVERLEAF_ASSET_STORE=filesystem`: only try `OVERLEAF_FILESTORE_ROOT`
- `OVERLEAF_ASSET_STORE=s3`: only try S3

Useful asset settings:

- `OVERLEAF_FILESTORE_ROOT`: path to the Overleaf filestore on disk if uploads are stored locally
- `OVERLEAF_ASSET_PATH_TEMPLATES`: optional semicolon-separated lookup templates such as `{hash};{hash_prefix2}/{hash};{project_id}/{file_id}`
- `OVERLEAF_S3_BUCKET`: S3 bucket for uploaded assets, or a semicolon-separated list of buckets to try in order
- `OVERLEAF_S3_PREFIX`: optional prefix within the S3 bucket
- `OVERLEAF_S3_REGION`: optional AWS region
- `OVERLEAF_S3_ENDPOINT_URL`: optional custom endpoint for S3-compatible storage
- `OVERLEAF_S3_ACCESS_KEY_ID` and `OVERLEAF_S3_SECRET_ACCESS_KEY`: optional explicit S3 credentials
- `OVERLEAF_S3_CA_BUNDLE`: optional CA bundle path for private/internal S3 TLS
- `OVERLEAF_S3_VERIFY_SSL`: set to `false` only if you must temporarily bypass TLS verification for an internal endpoint

Incremental sync state:

- by default the exporter writes `.sync-state.json` in the configured output directory
- the state file stores the last seen lightweight project metadata so unchanged projects can be skipped on the next run
- if export-affecting settings change, the exporter automatically refreshes matching projects instead of trusting the old state
- you can override the state file location with `--state-file` or `SYNC_STATE_FILE`

GitLab push settings support two authentication styles:

- HTTPS with a project access token
- SSH with a deploy key

For a project access token:

- `GITLAB_REMOTE_URL`: HTTPS remote such as `https://gitlab.example.com/group/overleaf-export.git`
- `GITLAB_ACCESS_TOKEN`: project access token with `write_repository`
- `GITLAB_HTTP_USERNAME`: the generated project access token bot username
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: separate local checkout or empty directory that will hold the export and git metadata

For a personal access token:

- `GITLAB_REMOTE_URL`: HTTPS remote such as `https://gitlab.example.com/group/overleaf-export.git`
- `GITLAB_ACCESS_TOKEN`: personal access token with `read_repository` for `--check-git` and `write_repository` for pushes
- `GITLAB_HTTP_USERNAME`: `oauth2`
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: separate local checkout or empty directory that will hold the export and git metadata

For a deploy token:

- `GITLAB_REMOTE_URL`: HTTPS remote such as `https://gitlab.example.com/group/overleaf-export.git`
- `GITLAB_ACCESS_TOKEN`: deploy token secret
- `GITLAB_HTTP_USERNAME`: the deploy token username
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: separate local checkout or empty directory that will hold the export and git metadata

For an SSH deploy key:

- `GITLAB_REMOTE_URL`: SSH remote such as `git@gitlab.example.com:group/overleaf-export.git`
- `GITLAB_SSH_KEY_PATH`: absolute path to the private deploy key
- `GITLAB_BRANCH`: target branch, typically `main`
- `GIT_REPO_DIR`: separate local checkout or empty directory that will hold the export and git metadata

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

After the first run, later runs reuse the sync state file and usually only re-export projects whose `lastUpdated`, `version`, name, or trash state changed.

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
               [--state-file PATH] [--project-id ID ...] [--limit N]
               [--include-raw] [--dry-run]
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

## Container Build

Build the image locally with Docker:

```powershell
docker build -t overleaf-git-bridge:latest .
```

Or use the included PowerShell helper:

```powershell
.\scripts\build-image.ps1 -ImageName overleaf-git-bridge -Tag latest
```

The container image includes:

- Python plus the bridge dependencies from `requirements.txt`
- `git` for commit and push operations
- `openssh-client` for SSH deploy-key based GitLab pushes

The image entrypoint is:

```text
python /app/sync.py
```

## Helm Chart

A starter Helm chart is included at `helm/overleaf-git-bridge`.

The chart deploys the bridge as a Kubernetes `CronJob`, which fits the current sync model better than a long-running `Deployment` because `sync.py` performs one export run and then exits.

### Default Secret Wiring

The chart is set up to run in the same namespace as Overleaf and, by default, reads these existing secrets:

- MongoDB password from secret `overleaf-mongo-creds`, key `MONGO_ROOT_PASSWORD`
- S3 access key id from secret `overleaf-s3-creds`, key `access_key_id`
- S3 secret access key from secret `overleaf-s3-creds`, key `access_key`

You still need to set the non-secret connection details in Helm values, such as the MongoDB service hostname, S3 bucket name, and GitLab remote URL.

### GitLab Auth Secret

For HTTPS token auth, create or reuse an existing secret containing:

- `username`: GitLab HTTP username such as `oauth2`, a project bot username, or a deploy token username
- `token`: the GitLab access token or deploy token secret

Example:

```powershell
kubectl create secret generic overleaf-git-bridge-gitlab `
  --from-literal=username=oauth2 `
  --from-literal=token=REPLACE_ME
```

### Example Install

```powershell
helm upgrade --install overleaf-git-bridge .\helm\overleaf-git-bridge `
  --namespace overleaf `
  --set-string image.repository=harbor.core.tide/ctf/overleaf-git-bridge `
  --set-string image.tag=0.1 `
  --set-string s3.endpointUrl=https://s3.core.tide `
  --set-string git.remoteUrl=git@gitlab.core.tide:morgan.mcford/overleaf-sync.git `
  --set-string git.auth.mode=ssh `
  --set-string git.auth.ssh.existingSecret.name=overleaf-git-bridge-gitlab
```

If you prefer SSH auth instead of HTTPS token auth, set `git.auth.mode=ssh`, provide `git.auth.ssh.existingSecret.name`, and store the private key in that secret under the configured key name.

### Useful Helm Values

Common settings in `helm/overleaf-git-bridge/values.yaml`:

- `schedule`: Cron expression for how often to run the sync job
- `bridge.extraArgs`: extra CLI flags such as `--project-id` or `--include-raw`
- `storage.persistence.enabled`: switch from `emptyDir` to a PVC if you want persistent working storage
- `mongo.*`: MongoDB host, username, database name, and auth database settings
- `s3.*`: S3 bucket, endpoint, region, TLS, and existing secret mapping
- `git.*`: GitLab remote, branch, commit identity, auth mode, and auth secret names

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
- `.sync-state.json` is an implementation detail for incremental sync; keep it with the export if you want future runs to stay fast.
- Text documents referenced from `rootFolder.docs` are exported as regular files.
- Uploaded `fileRefs` are exported when their bytes can be resolved from Mongo history blobs, a configured filestore path, or configured S3 storage.
- If uploaded assets cannot be resolved, the exporter prints a warning so you know additional storage configuration is needed.
- Project access tokens need the `write_repository` scope for `--push` to succeed.
- Deploy keys must be granted write access in GitLab for SSH-based `--push` to succeed.
