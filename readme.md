# overleaf-git-bridge

Small utility for exporting Overleaf Community Edition projects from MongoDB into a git-friendly folder structure and, if wanted, pushing the export to GitLab (pushing to a remote is the main intended use case, but the export can also be used standalone without git).

## What It Does

- reads project metadata from MongoDB
- exports project files and a `project.json` manifest
- skips unchanged projects on later runs
- can commit and push the export to GitLab

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
  .sync-state.json
```

## Quick Start

### 1. Install

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure

Copy `.env.example` to `.env` and set the values you need.

Minimum MongoDB settings:

- `OVERLEAF_MONGO_URI`
- or `OVERLEAF_MONGO_HOST`, `OVERLEAF_MONGO_PORT`, `OVERLEAF_MONGO_USERNAME`, `OVERLEAF_MONGO_PASSWORD`, `OVERLEAF_MONGO_AUTH_DB`
- `OVERLEAF_MONGO_DB`
- `OUTPUT_DIR`

If you want to push to GitLab, also set:

- `GIT_REPO_DIR`
- `GITLAB_REMOTE_URL`
- `GITLAB_BRANCH`

Use one Git auth method:

- HTTPS token: `GITLAB_ACCESS_TOKEN` and `GITLAB_HTTP_USERNAME`
- SSH key: `GITLAB_SSH_KEY_PATH`

### SSH key + Kubernetes secret notes

If SSH auth fails with errors like `Load key ... error in libcrypto`, the private key is usually malformed in the secret (wrong newlines, wrong key type, or encrypted key).

Recommended secret creation (preserves file contents exactly):

```powershell
kubectl create secret generic overleaf-git-bridge-ssh \
  --from-file=id_rsa=/path/to/private_key
```

Then set the chart values:

- `git.auth.mode=ssh`
- `git.auth.ssh.existingSecret.name=overleaf-git-bridge-ssh`
- `git.auth.ssh.existingSecret.key=id_rsa`

Use an **unencrypted private key** with **LF** line endings.

If your Overleaf instance stores uploaded files outside MongoDB, you may also need:

- `OVERLEAF_FILESTORE_ROOT`
- or `OVERLEAF_S3_BUCKET`

`GIT_REPO_DIR` should be a separate checkout or empty directory, not this repository.

### 3. Run an export

```powershell
python sync.py --limit 5
```

Useful variants:

- `python sync.py --include-raw` to also save the full MongoDB project document
- `python sync.py --project-id <id>` to export one project
- `python sync.py --dry-run` to preview changes without writing or pushing

### 4. Push to GitLab

```powershell
python sync.py --push
```

This will export projects, create a commit if files changed, and push to the configured branch.

## Common Commands

```powershell
python sync.py --limit 5
python sync.py --project-id 693616a50fa89c23ae8b1e99 --include-raw
python sync.py --dry-run
python sync.py --push
```

## Notes

- default database name is `sharelatex`
- exported state is tracked in `.sync-state.json`
- uploaded assets are resolved from MongoDB, the local filestore, or S3 depending on your configuration
- if an asset cannot be resolved, the exporter prints a warning and continues

## Tests

```powershell
python -m unittest discover -s tests -v
```

## Helm

A starter Helm chart is available at `helm/overleaf-git-bridge` for running the sync as a Kubernetes `CronJob`, the image can be built with `docker build -t overleaf-git-bridge .` and pushed to your registry. The chart is not published to a registry, so you need to use the local path when installing it.
