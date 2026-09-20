# Worktree tests

Run commands from the repository root after installing `backend/requirements.txt`
into `.venv`. Git must be installed on the test host. The deployed API itself
does not need Git: production repository operations run in the confined helper.

```sh
PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -v
```

The suite uses the standard library's `unittest`, temporary SQLite databases,
and disposable repositories. It includes:

- Unit tests for request validation, all four harness mount/cwd configurations,
  volume ownership labels, nonempty storage refusal, and guarded stale-mount repair.
- Service tests for idempotency, concurrent reservations, suspended writers,
  stopped-session recovery, launch failure/cancellation, and interrupted removal.
- Real Git tests for independent checkouts, dirty-source acknowledgement, changed
  base commits, ignored/untracked-file retention, opt-in author identity copying,
  branch export conflicts, safe retries, and locked missing worktrees.
- SQLite migration coverage preserving pre-worktree sessions, plus HTTP API tests
  for asynchronous launches, prepare-only operations, lifecycle conflicts, and
  explicit deletion of retained branch history.

## Docker integration

```sh
docker build -t agentcc-git-helper:dev images/git-helper
docker build -f backend/tests/docker/Dockerfile -t agentcc-worktree-test:dev .
PYTHONPATH=backend AGENTCC_DOCKER_TESTS=1 .venv/bin/python -m unittest discover -s backend/tests -p test_worktree_docker.py -v
```

These tests require access to the local Docker daemon. They create UUID-named
test volumes, containers, and networks, then remove those resources. The host
storage variant creates UUID-named test directories under the Docker daemon's
`/tmp`; it never uses configured production workspace roots or metadata.

The fixture uses the production bootstrap, real Git, both session users, and
tmux. A small local HTTP server stands in for code-server; the harness is a
fixed `sleep` process. It verifies actual Docker mounts, coder/agent permissions,
independent task contents, paused-writer exclusion, continued files after session
deletion, and export/removal. It does not exercise provider authentication,
paid agent calls, or the actual VS Code browser interface.

## Browser end-to-end tests

```sh
cd frontend
npm ci
npx playwright install --with-deps chromium
npm run test:e2e
```

Playwright starts temporary FastAPI/SQLite/Git fixtures on `127.0.0.1:19090` and
Vite on `127.0.0.1:5179`. Both ports must be free. It uses a fake container runtime
and the real application routes; no API responses are mocked in the browser.
It covers launch, continuation, archive/restore, export confirmation, checkout
removal, explicit parent-history deletion, dirty-source acknowledgement, and the non-Git fallback. Failure traces
are saved under `frontend/test-results/` (ignored by Git).

Some restricted execution sandboxes block the asynchronous communication used
by FastAPI's TestClient or local browser servers. Run these tests in a normal
local shell if the sandbox prevents them from starting.
