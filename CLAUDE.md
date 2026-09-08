# Lenny

## Build & Test

| Suite | Command | Runs when |
|---|---|---|
| Lint | `cd backend && ruff check app/ tests/ && ruff format --check app/ tests/` | every PR + before push |
| Unit | `cd backend && python3 -m pytest tests/ --ignore=tests/integration -v` | every PR + before push |
| Coverage (≥70% floor) | `cd backend && python3 -m pytest tests/ --ignore=tests/integration --cov=app --cov-report=term-missing` | optional locally; CI reports |
| Integration (CI-equivalent, what `pr-checks.yml` runs) | `USE_PREBAKED=1 REQUIRE_PREBAKED=1 ./scripts/run-integration-tests.sh --ignore=tests/integration/test_golden_measures.py --ignore=tests/integration/test_connectathon_measures.py --ignore=tests/integration/test_full_workflow.py --ignore=tests/integration/test_groups_dropdown.py --ignore=tests/integration/test_full_jobs_pipeline.py --ignore=tests/integration/test_factory_reset.py` | **every PR + before push** (most-flaky-in-CI suite, ~3–5 min) |
| Full workflow only | `./scripts/run-integration-tests.sh tests/integration/test_full_workflow.py` | before merging any change to the measure pipeline / FHIR data flow / job orchestration |
| Integration (full / connectathon source-of-truth) | `./scripts/run-integration-tests.sh` (no flags — adds 600+ connectathon-measure patient tests CI skips on PRs) — or trigger weekly via Actions → Connectathon Measures | weekly automatic (Monday 03:00 UTC) + manual pre-merge for measure-engine or HAPI-bump changes |
| Frontend dev server | `cd frontend && npm start` (port 3001) | local dev only |

**Decision tree:**
- Pushing a PR? → Lint + Unit + CI-equivalent integration (no skipping).
- Touching `measure_*` / `orchestrator.py` / `fhir_client.py` / `validation.py`? → Add Full workflow + Jobs pipeline validation (see below).
- Adding measures or bumping HAPI? → Run the full integration suite before merge.
- Validating that Lenny's Jobs API produces correct numerator/denominator counts? → `USE_PREBAKED=1 ./scripts/run-integration-tests.sh tests/integration/test_full_jobs_pipeline.py` (requires prebaked images with Groups; ~30–50 min for all 11 measures). Or run the standalone script: `python scripts/validate_all_measures.py`.

`docs/testing.md` documents the four-job Connectathon Measures workflow in full.

## Recurring bug: HAPI async-indexing race

**Read this before chasing any "wrong populations" / "validation pass-rate" / "$everything returns only Patient" / "Encounter?patient= returns 0" symptom.**

### Triage rule (30 seconds)

1. Read the resource directly: `GET /{Type}/{id}` — works regardless of index state.
2. Compare to what `/{Type}?patient=...` returns.
3. Direct read shows the data and search doesn't? → it's the index, not your code.

### What's actually happening

PUT/POST 200 means the resource is durable. Search consistency is async, governed by `hibernate.search.backend.io.refresh_interval` (we have 100ms). Hibernate Search 6's default strategy commits to disk but does NOT request an index refresh — searches see stale snapshots until the next refresh tick fires, or forever if it stalls under load.

### Pitfalls (each one cost a PR)

- Reindex Condition / Observation / Procedure / MedicationRequest / MedicationAdministration — measures don't query Encounter alone.
- `/validation/upload-bundle` returns 200 before CDR indexing completes; subsequent `/jobs` runs race the index. There is no CDR-side wait.
- `$everything` is a victim of this bug, not a cause. Don't propose replacing it.
- Testing CQL operations (`Measure/$evaluate-measure`, `Group/$evaluate` per the CQL IG, etc.) against any HAPI server that doesn't have `synchronization.strategy=sync` (e.g., external sandboxes like `cloud.alphora.com`): `Type?_summary=count` warming up is **not** sufficient. The CQL engine's internal `[Resource]` retrieves hit a different index path that can lag — you'll get `quantity: 0` (or an empty member set on `Group/$evaluate`) with no error and conclude the operation is broken when it's actually a stale snapshot. Direct reads (`subject=Patient/123`) bypass the index and will work; population evaluation (no `subject`) won't. Before asserting, poll with a CQL-engine-equivalent search (e.g., `Patient?_count=1000`) until total matches what you just wrote.

### Structural fix (applied in PR #206; compensator removed in PR #214)

`spring.jpa.properties.hibernate.search.indexing.plan.synchronization.strategy=sync` is now set on both HAPI services in `docker-compose.yml`, `docker-compose.test.yml`, and both seeded Dockerfiles. POST/PUT blocks until the Lucene index is refreshed, eliminating the bug class. The Python-side compensator (`HAPI_SYNC_AFTER_UPLOAD` + `trigger_reindex_and_wait*`) has been removed — HS6 `synchronization.strategy=sync` is the sole mechanism.

### History

PRs #142, #155, #159, #161, #167+ each patched a slice of this same disease.

## Local-first iteration — MANDATORY pre-push checklist

> **DO NOT `git push` ANY PR until ALL the checks below pass locally** — no exceptions for "small" or "obvious" fixes. "Validate locally" means EVERY check, not just unit tests.
> CI is not a debugger. Prod is not a debugger. Reviewers' time is not a debugger.

1. **Lint** (Build & Test table, "Lint" row) — clean.
2. **Unit suite** ("Unit" row) — passes.
3. **CI-equivalent integration suite** ("Integration (CI-equivalent…)" row) — **passes against real HAPI containers**, locally first (~3–5 min). **The `USE_PREBAKED=1 REQUIRE_PREBAKED=1` prefix is not optional** — `pr-checks.yml` sets both, and without them the script silently falls back to vanilla `hapiproject/hapi` images whose seed carries only CMS122 and **no FHIR Groups at all**. Prebaked-only tests then fail with confusing 404s instead of skipping, and the run is not CI-equivalent no matter what the `--ignore` flags say. `REQUIRE_PREBAKED=1` is what turns that silent fallback into a loud failure. The full integration suite (no `--ignore` flags) runs 600+ connectathon-measure patient tests CI skips on PRs — only run those when changing the measure evaluation pipeline or before the weekly run.
4. End-to-end smoke against a local stack (`cp .env.example .env && docker compose up -d` — `.env.example` sets `COMPOSE_FILE=docker-compose.yml:docker-compose.prebaked.yml` plus the prebaked HAPI image vars so the fast path is the default; falls back to vanilla `hapiproject/hapi:v8.8.0-1` if those vars are removed) for any change touching:
   - The data flow (`fhir_client.py`, `validation.py`, `orchestrator.py`)
   - HAPI behavior or configuration
   - Bundle import / `$everything` / `$evaluate-measure` paths
   - After any wipe+push cycle in the smoke run, probe `$everything` on at least one patient — see `docs/runbooks/everything-probe.md` for the script (the shell strips `$`, so use Python).
5. **New or modified `tests/integration/` files** — run those exact files locally before pushing. The CI-equivalent suite uses `--ignore` flags and will **silently skip** any new integration test; you must run it yourself. For prebaked-only tests (check for `HAPI_PREBAKED` guard or `_require_prebaked_stack`): `USE_PREBAKED=1 ./scripts/run-integration-tests.sh <test_file>`. No exceptions — not even for the test you just wrote.
6. The "ship-or-not" gate: if steps 1–5 didn't all pass, **do not push.** Say what's blocking in the PR description instead.

If the change is documentation-only (`*.md`, no code), steps 1–4 are not required, but step 5 still applies — confirm in the PR description that no code changed.

**Reproduce on the local stack FIRST.** Don't propose code changes until you have a local repro that fails the same way as prod.

## Architecture

5 Docker services (frontend :3001, backend :8000, db, hapi-fhir-cdr, hapi-fhir-measure). Local dev (per `.env.example`) and CI use `docker-compose.prebaked.yml` (bundles + IGs baked into the image, PR #199).

**Production does NOT use the prebaked overlay** — `deploy-prod.sh` runs `docker-compose.yml` + `docker-compose.prod.yml` only, so the `cdrdata`/`measuredata` volumes mount over `/data/hapi` and shadow any baked H2 store. Prod data is what the `seed` service loaded into those volumes; it persists across redeploys.

Prod's `/opt/leonard/.env` does **not** pin `HAPI_CDR_IMAGE`/`HAPI_MEASURE_IMAGE` — it falls through to the compose default, `hapiproject/hapi:v8.8.0-1`, pulled from Docker Hub. Prod is reproducible from this repository. (History: from PR #261 to issue #407, that file pinned a renamed-away GHCR package name that 403'd, silently, for months — see `docs/decisions.md` ADR-015. `scripts/check-pinned-images.sh` now fails the deploy loudly if any future pin can't be pulled.) See `docs/deploy.md`.

Full service map, data flow, HAPI configuration, and environment variables in `docs/architecture.md`. **End-to-end prod CI/CD pipeline, GHCR's role, and the inventory of everything outside this repo: `docs/deploy.md`.**

## Code Conventions

- **Commits:** conventional commits (`feat:`, `fix:`, `chore:`, `docs:`, `test:`)
- **Python:** 3.10+, `X | None` union syntax OK, type hints required
- **React:** plain JavaScript (not TypeScript), PascalCase components, co-located CSS Modules (`Foo.module.css`)
- **Config:** all values via environment variables (`backend/app/config.py`) — never hardcoded
- **PRs:** use `.github/pull_request_template.md` sections (`gh pr create` does not auto-populate — build the body explicitly)

## Workflow

Branches: `feature/*`, `fix/*`, or `chore/*` off `main`, merged via PR. Always work in a git worktree (`git worktree add ../lenny-<branch> -b <branch> origin/main`) — never commit directly on the current branch.
Work items: GitHub Issues on the [project board](https://github.com/orgs/Bellese/projects/33/views/3).

| Phase | Command | Toolkit |
|-------|---------|---------|
| Ideate | `/office-hours` | gstack |
| Plan | `/brainstorming` then `/writing-plans` | superpowers |
| Build | `/subagent-driven-development` | superpowers |
| Review | `/review` | gstack |
| Ship | `/ship` | gstack |
| Verify | `/qa` + `/browse` | gstack |

Shortcuts: bug fixes start at Build (use `/investigate` for root cause); small tasks skip Ideate and Plan; spikes are Ideate only. See `docs/workflow.md` for full details.

## AWS

**Always export `AWS_PROFILE=leonard` before any AWS CLI call.** Using any other profile/account is a bug — Claude has gotten this wrong before. Verify with `aws sts get-caller-identity` if unsure.

- Region: `us-east-1`
- Prod runs on a single t3.large EC2 instance (tagged `leonard`; look up with `aws ec2 describe-instances --query 'Reservations[].Instances[].[InstanceId,InstanceType,State.Name]' --output text`).
- Live: `https://lenny.bellese.dev` (UI), `https://api.lenny.bellese.dev` (API)

## Deploy Configuration (configured by /setup-deploy)

- **Platform:** AWS EC2 (single t3.large, instance `i-0f00585639d2f3ef1`, tagged `leonard`, us-east-1) — deploys via GitHub Actions OIDC role assumption + SSM Run Command (`leonard-deploy` document). No platform-specific deploy CLI (Fly/Render/Vercel/Netlify) involved.
- **Project type:** Web app (frontend on `lenny.bellese.dev`) + backend API (on `api.lenny.bellese.dev`)
- **Production URLs:**
  - UI: `https://lenny.bellese.dev`
  - API: `https://api.lenny.bellese.dev`
  - Post-deploy health check: `https://api.lenny.bellese.dev/health` (returns JSON with `database`, `measure_engine`, `cdr` connection state)
- **Deploy workflow:** `.github/workflows/deploy.yml`
- **Trigger:** `push` to `main` (auto-deploy on every merge) + manual `workflow_dispatch`. Concurrency group `deploy-production`, `cancel-in-progress: false` — deploys are serialized.
- **Deploy mechanism:** GitHub Actions → AWS OIDC role `arn:aws:iam::439475769170:role/leonard-github-deploy` → `aws ssm send-command --document-name leonard-deploy --instance-ids i-0f00585639d2f3ef1`. Poll loop with 64 × 15s (~16 min budget) checking `aws ssm get-command-invocation` status.
- **Built-in workflow health check:** workflow polls `https://api.lenny.bellese.dev/health` 24 × 5s (~2 min budget) immediately after the SSM command succeeds. Deploy fails if the URL doesn't respond within that window. Note: `/health` returns HTTP 200 even when degraded (e.g. HAPI disconnected) — a green health check proves the backend answered, not that HAPI is up. Read the JSON body's `.status`/`.cdr.status`/`.measure_engine.status` fields to actually confirm health.
- **Pinned-image guard:** `scripts/check-pinned-images.sh` runs before `deploy-prod.sh`'s `docker compose pull`. Any `HAPI_*_IMAGE` pin in `.env` that differs from the compose default is deliberate — the guard pulls it explicitly and fails the deploy (exit 1) if that pull fails, rather than letting the blanket `--ignore-pull-failures` swallow it. Unpinned images are unaffected — a registry blip on those still can't block a deploy.
- **Merge method:** squash. Branch protection requires linear history, so merge commits are rejected on `main` (squash and rebase both work).
- **Branch protection on `main`** (verified 2026-08-19). All six `pr-checks.yml` checks are required:
  `Lint`, `Unit Tests + Coverage`, `Integration Tests`, `Frontend Build`, `Config Validation`, `Script Security Lint`.
  - `strict: true` — a PR must be up to date with `main` and re-pass CI before merging. This is the mechanism behind the CI-parity claim below: the commit that deploys is the commit that was tested.
  - `enforce_admins: true` — admins cannot merge past a red check either.
  - No review approval required. Status checks are the gate; a solo maintainer can still merge.
  - Force pushes and branch deletion on `main` are blocked.

  Protection was absent until 2026-08-19 despite this file describing it, so anything merged before that date did **not** pass through this gate. `deploy.yml:23-27` also skips re-running tests on the strength of that guarantee, so the deploy pipeline's central assumption was unfounded for the same period. Both are now true; if protection is ever removed, that comment and this section become wrong together.
- **Pre-merge hooks:** the `pr-checks.yml` workflow gates merges via the branch protection above. The Deploy workflow does NOT re-run tests (deliberate — see in-line comment in `deploy.yml:23-27`); CI parity comes from `strict: true` requiring the PR to be rebased onto current `main` and green before it can merge.

### Deploy status command (for `/land-and-deploy`)

The Deploy workflow IS the deploy mechanism, so poll the workflow run via `gh`:

```bash
# Get the latest deploy.yml run on main (by head_sha after the merge commit lands)
env -u GH_TOKEN gh run list --repo Bellese/Lenny --workflow deploy.yml --branch main --limit 1 \
  --json status,conclusion,headSha,databaseId,url,createdAt
# Poll until status == "completed" and conclusion == "success"
```

### Post-deploy verification (for `/land-and-deploy` canary)

```bash
curl -fsS https://api.lenny.bellese.dev/health | jq '.status, .database.status, .measure_engine.status, .cdr.status'
# Expect all == "healthy" / "connected"
curl -fsS -o /dev/null -w "%{http_code}" https://lenny.bellese.dev    # expect 200
```

### Staging

No staging environment configured. All merges to `main` deploy straight to production. Branch protection is the only safety gate: all six `pr-checks.yml` checks must pass, and `strict: true` forces the PR to be up to date with `main` first — so production runs the commit CI actually tested. There is no second chance after merge; the deploy fires immediately.

## Do NOT

- Hardcode URLs or credentials — use environment variables
- Use Python 3.9-style `Optional[X]` — `X | None` is preferred
- Modify HAPI FHIR H2 storage paths without reading `docs/architecture.md`
- Modify `TODOS.md` — it is frozen 2026-04-27. Open a GitHub Issue for any new work item.

## External toolkit commands

These are gstack / superpowers slash commands — **not** harness-loadable skills. Don't try to invoke them via the Skill tool; surface the right one when the user's intent matches and let them run it. (The Workflow table above maps phases to commands; this list maps intents.)

- Product ideas / brainstorming → `/office-hours`
- Strategy / scope → `/plan-ceo-review`
- Architecture → `/plan-eng-review`
- Design system / plan review → `/design-consultation` or `/plan-design-review`
- Full review pipeline → `/autoplan`
- Bugs / errors → `/investigate`
- QA / testing site behavior → `/qa` or `/qa-only`
- Code review / diff check → `/review`
- Visual polish → `/design-review`
- Ship / deploy / PR → `/ship` or `/land-and-deploy`
- Save progress → `/context-save`
- Resume context → `/context-restore`
