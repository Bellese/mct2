"""FastAPI application entry point."""

import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded

from app.config import parse_allowed_origins
from app.config import settings as app_settings
from app.db import engine
from app.limiter import limiter
from app.models import Base
from app.routes import groups, health, jobs, measures, results, settings, validation
from app.services.bundle_loader import load_connectathon_bundles
from app.services.worker import request_shutdown, worker_loop

# ---------------------------------------------------------------------------
# Structured JSON logging
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    """Minimal structured JSON log formatter."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        from datetime import datetime, timezone

        log_entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge structlog-style extra keys
        _EXTRA_KEYS = (
            "job_id",
            "batch_id",
            "patient_id",
            "url",
            "count",
            "error",
            "cdr_url",
            "status_code",
            "latency_ms",
            "hint",
            "matched",
            "null_remaining",
            "null_job_count",
            "legacy_rows_seen",
            "encrypted_now",
            "event",
            "action",
            "cdr_id",
            "cdr_name",
            "mcs_id",
            "mcs_name",
            "mcs_url",
        )
        for key in _EXTRA_KEYS:
            val = getattr(record, key, None)
            if val is not None:
                log_entry[key] = val
        if record.exc_info and record.exc_info[1]:
            log_entry["exception"] = self.formatException(record.exc_info)
        return _json.dumps(log_entry)


handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logging.basicConfig(level=logging.INFO, handlers=[handler])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan: create tables + start worker
# ---------------------------------------------------------------------------


async def _run_schema_migrations(conn) -> None:
    """Add new columns/enum values to existing tables. Idempotent (IF NOT EXISTS).

    Must run outside a transaction for ALTER TYPE ADD VALUE (Postgres < 12
    requires autocommit for that statement). SQLite (used in tests) skips all
    ALTER statements — create_all handles new columns on fresh in-memory DBs.
    """
    from sqlalchemy import text

    if conn.dialect.name == "postgresql":
        # Check if tables already exist (skip migrations on a fresh DB — create_all handles those)
        result = await conn.execute(text("SELECT to_regtype('authtype')"))
        authtype_exists = result.scalar() is not None
        if not authtype_exists:
            return

        # AuthType enum: add 'smart' value if not present
        await conn.execute(text("ALTER TYPE authtype ADD VALUE IF NOT EXISTS 'smart'"))
        result = await conn.execute(text("SELECT to_regtype('validationstatus')"))
        validationstatus_exists = result.scalar() is not None
        if validationstatus_exists:
            await conn.execute(text("ALTER TYPE validationstatus ADD VALUE IF NOT EXISTS 'cancelled'"))

        # Add new columns to cdr_configs (idempotent)
        for stmt in [
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS name VARCHAR(512)",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS is_default BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS is_read_only BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS request_timeout_seconds INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE cdr_configs ADD COLUMN IF NOT EXISTS max_bundle_entries INTEGER",
        ]:
            await conn.execute(text(stmt))

        # Issue #396: is_read_only moved up from CDRConfig to ConnectionConfigMixin,
        # so mcs_configs gains the column too. Wrapped in a table-existence guard
        # (same pattern as the bundle_uploads ALTER below) because a DB old enough
        # to predate the MCS table gets the column from create_all instead — that
        # runs after this function.
        await conn.execute(
            text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'mcs_configs') THEN
                    ALTER TABLE mcs_configs ADD COLUMN IF NOT EXISTS is_read_only BOOLEAN NOT NULL DEFAULT FALSE;
                END IF;
            END
            $$;
            """)
        )

        # Issue #392: wipe_before_job gates the destructive full wipe at job start.
        # DEFAULT FALSE is the safe direction for existing rows — an attendee who
        # already added a shared connectathon MCS stops nuking it on the next job
        # without touching their config. The seeded local engine is restored to
        # TRUE so local behavior is unchanged.
        #
        # Both statements are inside a column-absence guard rather than using
        # ADD COLUMN IF NOT EXISTS, so the backfill runs EXACTLY ONCE. With an
        # unguarded UPDATE, every restart would re-assert TRUE on the default row
        # and silently undo a user who had turned the full wipe off.
        await conn.execute(
            text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'mcs_configs')
                   AND NOT EXISTS (
                       SELECT FROM information_schema.columns
                       WHERE table_name = 'mcs_configs' AND column_name = 'wipe_before_job'
                   ) THEN
                    -- IF NOT EXISTS on the ALTER as well as the guard: the guard
                    -- makes the BACKFILL one-shot, and this keeps the ALTER itself
                    -- idempotent if two processes ever pass the guard concurrently
                    -- (the loser would otherwise abort startup on "column already
                    -- exists").
                    ALTER TABLE mcs_configs
                        ADD COLUMN IF NOT EXISTS wipe_before_job BOOLEAN NOT NULL DEFAULT FALSE;
                    UPDATE mcs_configs SET wipe_before_job = TRUE WHERE is_default = TRUE;
                END IF;
            END
            $$;
            """)
        )

        # Normalize legacy CDR rows before adding unique indexes. Production may
        # already contain duplicate names or multiple active/default rows created
        # before these constraints existed.
        await conn.execute(
            text("""
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        ORDER BY is_active DESC, is_default DESC, id ASC
                    ) AS rn
                FROM cdr_configs
                WHERE cdr_url = 'http://hapi-fhir-cdr:8080/fhir'
                  AND (name IS NULL OR btrim(name) = '')
            )
            UPDATE cdr_configs AS c
            SET
                name = CASE
                    WHEN ranked.rn = 1 THEN 'Local CDR'
                    ELSE 'Local CDR (migrated ' || c.id::text || ')'
                END,
                is_default = CASE
                    WHEN ranked.rn = 1 THEN TRUE
                    ELSE FALSE
                END
            FROM ranked
            WHERE c.id = ranked.id
            """)
        )
        await conn.execute(
            text("""
            UPDATE cdr_configs
            SET name = 'CDR Connection ' || id::text
            WHERE name IS NULL OR btrim(name) = ''
            """)
        )
        await conn.execute(
            text("""
            WITH ranked AS (
                SELECT
                    id,
                    name,
                    ROW_NUMBER() OVER (
                        PARTITION BY name
                        ORDER BY is_default DESC, is_active DESC, id ASC
                    ) AS rn
                FROM cdr_configs
                WHERE name IS NOT NULL
            )
            UPDATE cdr_configs AS c
            SET name = left(c.name, 480) || ' (' || c.id::text || ')'
            FROM ranked
            WHERE c.id = ranked.id
              AND ranked.rn > 1
            """)
        )

        # Unique constraint on name (required for ON CONFLICT (name) seed upsert)
        await conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_cdr_configs_name ON cdr_configs (name)"))

        # Add new columns to jobs
        for stmt in [
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_name VARCHAR(512)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_read_only BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_auth_type VARCHAR(32)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_auth_credentials JSONB",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS delete_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mcs_id INTEGER REFERENCES mcs_configs(id) ON DELETE SET NULL",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mcs_url VARCHAR(1024)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mcs_name VARCHAR(512)",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mcs_auth_type VARCHAR(32)",
            # Issue #392. FALSE on existing rows = scoped wipe, the safe default.
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS mcs_wipe_before_job BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workflow VARCHAR(32) NOT NULL DEFAULT 'direct_load'",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS submit_data_mode VARCHAR(32)",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS delete_requested BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS started_at TIMESTAMPTZ",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_id INTEGER "
            "REFERENCES mcs_configs(id) ON DELETE SET NULL",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_url VARCHAR(1024)",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_name VARCHAR(512)",
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_auth_type VARCHAR(32)",
            # Issue #397. FALSE on existing rows = scoped wipe, the safe default.
            "ALTER TABLE validation_runs ADD COLUMN IF NOT EXISTS mcs_wipe_before_job BOOLEAN NOT NULL DEFAULT FALSE",
        ]:
            await conn.execute(text(stmt))

        # Backfill MCS snapshot for jobs created before #12. Existing rows
        # ran against `MEASURE_ENGINE_URL` (the only MCS Lenny knew at the
        # time), so populate them with that value + the seeded
        # `Local Measure Engine` name. If the env var is unset, leaves NULL
        # and logs a warning — UI displays "(unknown)" for those rows.
        env_measure_url = app_settings.MEASURE_ENGINE_URL or ""
        if env_measure_url:
            await conn.execute(
                text("UPDATE jobs SET   mcs_url = :url,   mcs_name = 'Local Measure Engine' WHERE mcs_url IS NULL"),
                {"url": env_measure_url},
            )
        else:
            logger.warning(
                "MEASURE_ENGINE_URL is unset — skipping mcs_url backfill on existing jobs",
                extra={"event": "mcs_backfill_skipped"},
            )

        # Add warning_message to bundle_uploads if the table exists
        await conn.execute(
            text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'bundle_uploads') THEN
                    ALTER TABLE bundle_uploads ADD COLUMN IF NOT EXISTS warning_message TEXT;
                END IF;
            END
            $$;
            """)
        )

        # Keep only one active row before enforcing the partial unique index.
        await conn.execute(
            text("""
            WITH ranked AS (
                SELECT
                    id,
                    ROW_NUMBER() OVER (
                        ORDER BY is_default DESC, id ASC
                    ) AS rn
                FROM cdr_configs
                WHERE is_active = TRUE
            )
            UPDATE cdr_configs AS c
            SET is_active = FALSE
            FROM ranked
            WHERE c.id = ranked.id
              AND ranked.rn > 1
            """)
        )

        # Enforce at most one active CDR row (partial unique index — Postgres only)
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_cdr ON cdr_configs (is_active) WHERE is_active = TRUE"
            )
        )

        # Update existing row that matches the default URL but has no name yet
        await conn.execute(
            text("""
            UPDATE cdr_configs
            SET name = 'Local CDR', is_default = TRUE, is_read_only = FALSE
            WHERE cdr_url = 'http://hapi-fhir-cdr:8080/fhir'
              AND (name IS NULL OR name = '')
        """)
        )

        # Dedup measure_results before adding the unique index — the batch-retry
        # loop can create duplicate rows. Keep the newest row per (job_id, patient_id).
        # Guard so the DELETE only runs once (before the index is created).
        index_exists = (
            await conn.execute(
                text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname = 'uq_measure_results_job_patient')")
            )
        ).scalar()
        if not index_exists:
            await conn.execute(
                text("""
                DELETE FROM measure_results a USING measure_results b
                WHERE a.id < b.id
                  AND a.job_id = b.job_id
                  AND a.patient_id = b.patient_id
                """)
            )
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_measure_results_job_patient"
                " ON measure_results (job_id, patient_id)"
            )
        )

        # Error context columns added for #74/#75/#76 observability
        for stmt in [
            "ALTER TABLE measure_results ADD COLUMN IF NOT EXISTS error_details JSONB",
            "ALTER TABLE measure_results ADD COLUMN IF NOT EXISTS error_phase VARCHAR(32)",
            "ALTER TABLE measure_results ADD COLUMN IF NOT EXISTS evaluated_resources JSONB",
            "ALTER TABLE bundle_uploads ADD COLUMN IF NOT EXISTS error_details JSONB",
            "ALTER TABLE validation_results ADD COLUMN IF NOT EXISTS error_details JSONB",
        ]:
            await conn.execute(text(stmt))

        # Issue #219: replace per-job cdr_auth_credentials snapshot with a live FK.
        # Uses IF NOT EXISTS / IF EXISTS guards — idempotent across concurrent startups.
        await conn.execute(
            text(
                "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS cdr_id INTEGER REFERENCES cdr_configs(id) ON DELETE SET NULL"
            )
        )
        await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_jobs_cdr_id ON jobs(cdr_id)"))
        # Backfill cdr_id for historical jobs where exactly one CDR matches url+name.
        # Ambiguous matches (cdr_configs.name has a unique constraint so this is rare) stay NULL.
        result = await conn.execute(
            text("""
            UPDATE jobs j
            SET cdr_id = c.id
            FROM cdr_configs c
            WHERE j.cdr_id IS NULL
              AND c.cdr_url = j.cdr_url
              AND COALESCE(c.name, '') = COALESCE(j.cdr_name, '')
              AND (
                SELECT COUNT(*) FROM cdr_configs c2
                WHERE c2.cdr_url = j.cdr_url
                  AND COALESCE(c2.name, '') = COALESCE(j.cdr_name, '')
              ) = 1
            """)
        )
        matched = result.rowcount
        # Count how many jobs were left with NULL (ambiguous or no match).
        skipped_result = await conn.execute(text("SELECT COUNT(*) FROM jobs WHERE cdr_id IS NULL"))
        skipped = skipped_result.scalar() or 0
        logger.info("cdr_id_backfill", extra={"matched": matched, "null_remaining": skipped})
        if skipped:
            logger.warning(
                "Some jobs have no cdr_id — CDR config deleted or name mismatch",
                extra={"null_job_count": skipped},
            )

        # Drop the old plaintext credential snapshot column.
        await conn.execute(text("ALTER TABLE jobs DROP COLUMN IF EXISTS cdr_auth_credentials"))

        # Preserve validation_enabled=true for existing deployments that relied
        # on the previous default. The in-code default flipped to "false" so that
        # fresh installs start with the Validation tab hidden; existing installs
        # that never explicitly set this key would otherwise lose the tab on
        # upgrade. ON CONFLICT (key) DO NOTHING preserves any explicit admin choice.
        await conn.execute(
            text("""
            DO $$
            BEGIN
                IF EXISTS (SELECT FROM information_schema.tables WHERE table_name = 'app_settings') THEN
                    INSERT INTO app_settings (key, value)
                    VALUES ('validation_enabled', 'true')
                    ON CONFLICT (key) DO NOTHING;
                END IF;
            END
            $$;
            """)
        )


async def seed_default_connections() -> None:
    """Seed Local <Kind> rows for each connection-management kind.

    Idempotent: skips a kind if a row with `is_default=True` already exists in
    its table (the seed has already run). For each missing seed, inserts a
    row keyed off the env-var URL (`DEFAULT_CDR_URL` for CDR,
    `MEASURE_ENGINE_URL` for MCS). Uses SQLAlchemy ORM, so it runs identically
    on Postgres and SQLite — replaces the earlier raw-SQL CDR seed that
    hardcoded the URL.

    Future kinds (TS, MR, MRR) add a single tuple to `_KIND_SEEDS` below.
    """
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import settings as app_config
    from app.models.config import CDRConfig
    from app.models.connection_base import AuthType
    from app.models.mcs_config import MCSConfig

    # (model, default-name, url-attribute, env-derived URL, kind-specific
    # extra kwargs the model accepts).
    _KIND_SEEDS = [
        (CDRConfig, "Local CDR", "cdr_url", app_config.DEFAULT_CDR_URL, {"is_read_only": False}),
        # wipe_before_job=True: the seeded row is Lenny's own measure-engine
        # container, the one place a full wipe between jobs is correct (#392).
        # Every user-created MCS defaults to the scoped wipe instead.
        (MCSConfig, "Local Measure Engine", "mcs_url", app_config.MEASURE_ENGINE_URL, {"wipe_before_job": True}),
    ]

    async with AsyncSession(engine) as session:
        for model, default_name, url_attr, default_url, extra_kwargs in _KIND_SEEDS:
            existing = await session.execute(select(model.id).where(model.is_default.is_(True)).limit(1))
            if existing.scalar_one_or_none() is not None:
                continue  # Already seeded.
            cfg = model(
                name=default_name,
                auth_type=AuthType.none,
                is_active=True,
                is_default=True,
                **{url_attr: default_url},
                **extra_kwargs,
            )
            session.add(cfg)
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: create DB tables and launch background worker.
    Shutdown: signal worker to stop.
    """
    from sqlalchemy import text

    # Run schema migrations outside a transaction (required for ALTER TYPE ADD VALUE in Postgres)
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        await _run_schema_migrations(conn)
    # Create any missing tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # Enforce at most one active row per kind on fresh DBs (partial unique
    # indexes). `__table_args__` on each subclass declares the index so
    # `create_all` above generates it for both Postgres and SQLite. The raw-
    # SQL below is belt-and-suspenders for existing-DB upgrades where the
    # model declaration didn't yet exist when the table was created.
    async with engine.connect() as conn:
        await conn.execution_options(isolation_level="AUTOCOMMIT")
        if conn.dialect.name == "postgresql":
            where_clause = "is_active = TRUE"
        else:
            where_clause = "is_active = 1"
        for table_name, index_name in [
            ("cdr_configs", "idx_one_active_cdr"),
            ("mcs_configs", "idx_one_active_mcs"),
        ]:
            await conn.execute(
                text(f"CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table_name} (is_active) WHERE {where_clause}")
            )
    # Seed built-in Local <Kind> rows across all connection kinds. Reads URLs
    # from env vars (DEFAULT_CDR_URL, MEASURE_ENGINE_URL) — replaces the
    # earlier raw-SQL CDR seed that hardcoded the URL in violation of the
    # CLAUDE.md "no hardcoded URLs" rule.
    await seed_default_connections()
    logger.info("Database tables created")

    # Encrypt any plaintext cdr_configs.auth_credentials rows left from before #219.
    # Idempotent: rows already wrapped in {v, ct} envelope are skipped by the TypeDecorator.
    if engine.dialect.name == "postgresql":
        try:
            from sqlalchemy.ext.asyncio import AsyncSession
            from sqlalchemy.orm.attributes import flag_modified

            from app.models.config import CDRConfig
            from app.services.credential_crypto import self_check

            if self_check():
                async with AsyncSession(engine) as session:
                    # Only fetch rows that lack the {v, ct} envelope — legacy plaintext.
                    result = await session.execute(
                        text(
                            "SELECT id FROM cdr_configs"
                            " WHERE auth_credentials IS NOT NULL"
                            " AND NOT (auth_credentials::jsonb ? 'v')"
                        )
                    )
                    ids = [row[0] for row in result.fetchall()]
                    encrypted_count = 0
                    for cdr_id in ids:
                        cfg = await session.get(CDRConfig, cdr_id)
                        if cfg is not None and cfg.auth_credentials is not None:
                            # flag_modified forces SQLAlchemy to mark the attribute dirty so
                            # EncryptedJSON.process_bind_param fires on flush and encrypts the value.
                            flag_modified(cfg, "auth_credentials")
                            await session.flush()
                            encrypted_count += 1
                    await session.commit()
                logger.info(
                    "credentials_encrypted",
                    extra={"legacy_rows_seen": len(ids), "encrypted_now": encrypted_count},
                )
        except Exception:
            logger.exception("Credential encryption backfill failed — continuing startup")

    # Reconcile stale admin_operations: mark any row stuck in running/pending for
    # >10 min as failed. asyncio.create_task does not survive restarts; a crashed
    # container leaves those rows in running state indefinitely otherwise.
    # Also purge rows older than 30 days to keep the table bounded.
    try:
        from sqlalchemy import text as _text

        async with engine.connect() as _conn:
            await _conn.execute(
                _text("""
                UPDATE admin_operations
                SET status = 'failed',
                    error = 'Interrupted by backend restart',
                    completed_at = NOW()
                WHERE status IN ('running', 'pending')
                  AND started_at < NOW() - INTERVAL '10 minutes'
                """)
            )
            await _conn.execute(_text("DELETE FROM admin_operations WHERE started_at < NOW() - INTERVAL '30 days'"))
            await _conn.commit()
        logger.info("Admin operations reconciled")
    except Exception:
        logger.exception("Admin operations reconcile failed — continuing startup")

    # Load connectathon bundles at startup (no-op if directory missing)
    try:
        summary = await load_connectathon_bundles()
        logger.info("Startup bundle load complete", extra=summary)
    except Exception:
        logger.exception("Startup bundle load failed — continuing startup")

    # Start background worker
    worker_task = asyncio.create_task(worker_loop())
    logger.info("Background worker started")

    yield

    # Shutdown
    request_shutdown()
    worker_task.cancel()
    try:
        await worker_task
    except asyncio.CancelledError:
        pass
    logger.info("Background worker stopped")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Lenny — Measure Calculation Tool",
    description="Healthcare quality measure calculation orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)


def _rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "throttled",
                    "diagnostics": f"Rate limit exceeded: {exc.detail}",
                }
            ],
        },
    )


app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS — restricted in production via ALLOWED_ORIGINS env var; defaults to wildcard for local dev
origins = parse_allowed_origins(app_settings.ALLOWED_ORIGINS)
# allow_credentials requires an explicit origin list; wildcard + credentials is invalid per spec
allow_credentials = bool(origins) and origins != ["*"]
if not allow_credentials:
    logger.warning(
        "CORS: ALLOWED_ORIGINS is wildcard — allow_credentials disabled. "
        "Set ALLOWED_ORIGINS to specific origins in production."
    )
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(groups.router)
app.include_router(health.router)
app.include_router(jobs.router)
app.include_router(measures.router)
app.include_router(results.router)
app.include_router(settings.router)
app.include_router(validation.router)
