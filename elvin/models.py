"""Database schema (PostgreSQL). Every row that belongs to a person carries
owner/user ids so every query can be scoped - nothing is shared across accounts
or across projects."""
from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _ts(nullable: bool = False, default: bool = True) -> Mapped[Any]:
    return mapped_column(DateTime(timezone=True), nullable=nullable, default=utcnow if default else None)


# --------------------------------------------------------------------------- installation-wide settings
class AppSetting(Base):
    """Small key/value overrides an admin can change in the app (e.g. the
    gateway address after the gateway PC got a new IP)."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[dt.datetime] = _ts()


# --------------------------------------------------------------------------- accounts
class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # lower-cased
    username: Mapped[str] = mapped_column(String(32))
    username_key: Mapped[str] = mapped_column(String(32), unique=True, index=True)  # lower-cased
    password_hash: Mapped[str] = mapped_column(String(255))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    theme: Mapped[str] = mapped_column(String(10), default="system")
    versioning_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    ai_team: Mapped[str] = mapped_column(String(32), default="gemma_qwen")
    ai_effort: Mapped[str] = mapped_column(String(10), default="medium")  # low | medium | high | max
    model_prefs: Mapped[dict] = mapped_column(JSONB, default=dict)        # per-role model choices
    created_at: Mapped[dt.datetime] = _ts()
    last_login_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    password_changed_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    # One login per account: only the newest session's token hash is valid.
    session_token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_started_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    session_device: Mapped[str | None] = mapped_column(String(120), nullable=True)
    session_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    current_project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[dt.datetime] = _ts()
    expires_at: Mapped[dt.datetime] = _ts(default=False)
    used_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    request_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    ts: Mapped[dt.datetime] = _ts()
    event: Mapped[str] = mapped_column(String(60))
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)


# --------------------------------------------------------------------------- projects
class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("owner_id", "slug", name="uq_project_owner_slug"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    slug: Mapped[str] = mapped_column(String(80))
    description: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(20), default="new")  # new | upload | clone | import
    repo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    github_repo: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = _ts()
    last_opened_at: Mapped[dt.datetime] = _ts()
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | missing
    integrity: Mapped[dict] = mapped_column(JSONB, default=dict)
    integrity_checked_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)


class ProjectFile(Base):
    """Last known state of every tracked file - used for integrity checks."""

    __tablename__ = "project_files"
    __table_args__ = (UniqueConstraint("project_id", "path", name="uq_project_file"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    mtime_ns: Mapped[int] = mapped_column(BigInteger)


class Snapshot(Base):
    """A timestamped version of a project (content-addressed blobs on disk,
    manifest here and in a local JSON copy, hash-chained per project)."""

    __tablename__ = "snapshots"
    __table_args__ = (Index("ix_snapshots_project_created", "project_id", "created_at"),)
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    created_at: Mapped[dt.datetime] = _ts()
    kind: Mapped[str] = mapped_column(String(20))  # task_start | step | task_end | manual | restore | partial | external | checkpoint | initial
    label: Mapped[str] = mapped_column(String(200), default="")
    created_by: Mapped[str] = mapped_column(String(10), default="system")  # user | elvin | system
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    manifest: Mapped[dict] = mapped_column(JSONB, default=dict)  # {path: [sha256, size]}
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    prev_chain: Mapped[str] = mapped_column(String(64), default="")
    chain_hash: Mapped[str] = mapped_column(String(64), default="")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    verify_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


# --------------------------------------------------------------------------- Elvin chat / memory / tasks
class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_project_id_id", "project_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(10))  # user | elvin | system
    content: Mapped[str] = mapped_column(Text)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = _ts()


class Memory(Base):
    __tablename__ = "memories"
    __table_args__ = (UniqueConstraint("project_id", "content_key", name="uq_memory_key"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(String(400))
    content_key: Mapped[str] = mapped_column(String(64))
    source: Mapped[str] = mapped_column(String(10), default="auto")  # auto | user
    created_at: Mapped[dt.datetime] = _ts()


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_task_idempotency"),
        Index("ix_tasks_user_status", "user_id", "status"),
        Index("ix_tasks_project_created", "project_id", "created_at"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))  # code | design | bughunt | vulnscan | loadtest | push | clone | ghstats
    status: Mapped[str] = mapped_column(String(20), default="queued")
    title: Mapped[str] = mapped_column(String(200), default="")
    prompt: Mapped[str] = mapped_column(Text, default="")
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    team: Mapped[str] = mapped_column(String(32), default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[dt.datetime] = _ts()
    started_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    finished_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    end_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    retry_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    result: Mapped[dict] = mapped_column(JSONB, default=dict)


class TaskEvent(Base):
    __tablename__ = "task_events"
    __table_args__ = (
        Index("ix_task_events_user_id", "user_id", "id"),
        Index("ix_task_events_task_id", "task_id", "id"),
    )
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ts: Mapped[dt.datetime] = _ts()
    type: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSONB, default=dict)


class ApprovalBatch(Base):
    """A set of changes Elvin wants to make. Nothing is written or run until the
    owner approves it; the content stays server-side and is pinned by sha256."""

    __tablename__ = "approval_batches"
    id: Mapped[uuid.UUID] = _uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    step_no: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")
    actions: Mapped[list] = mapped_column(JSONB, default=list)
    decision: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = _ts()
    decided_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)


class CommandRun(Base):
    """A command the user ran themselves (editor or Elvin chat) in the sandbox.
    Elvin sees the latest one, so it can check and fix what failed."""

    __tablename__ = "command_runs"
    __table_args__ = (Index("ix_command_runs_project_id", "project_id", "id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source: Mapped[str] = mapped_column(String(10), default="editor")  # editor | elvin
    command: Mapped[str] = mapped_column(String(500))
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timed_out: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = _ts()


class Attachment(Base):
    __tablename__ = "attachments"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="image")  # image | screenshot
    filename: Mapped[str] = mapped_column(String(200), default="")
    mime: Mapped[str] = mapped_column(String(50), default="image/png")
    size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="")
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = _ts()


class UsageRecord(Base):
    """One AI call (successful or not) - powers the Usage page and the
    data-driven suggestions for effort / model / team choices."""

    __tablename__ = "usage_records"
    __table_args__ = (Index("ix_usage_user_created", "user_id", "created_at"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    feature: Mapped[str] = mapped_column(String(20), default="")   # elvin | design | bughunt | clone | vulns | editor | memory
    provider: Mapped[str] = mapped_column(String(20))
    model: Mapped[str] = mapped_column(String(80), default="")
    role: Mapped[str] = mapped_column(String(20), default="")
    effort: Mapped[str] = mapped_column(String(10), default="")
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    error_kind: Mapped[str] = mapped_column(String(20), default="")
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    elapsed_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[dt.datetime] = _ts()


# --------------------------------------------------------------------------- security tooling
class LibraryCheck(Base):
    __tablename__ = "library_checks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    ecosystem: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(214))
    version: Mapped[str] = mapped_column(String(64), default="")
    verdict: Mapped[str] = mapped_column(String(20))  # go | go_with_version | blocked
    recommended: Mapped[str] = mapped_column(String(300), default="")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[dt.datetime] = _ts()


class VulnScan(Base):
    __tablename__ = "vuln_scans"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    status: Mapped[str] = mapped_column(String(20), default="running")
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class VulnFinding(Base):
    __tablename__ = "vuln_findings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("vuln_scans.id", ondelete="CASCADE"), index=True)
    ecosystem: Mapped[str] = mapped_column(String(20))
    package: Mapped[str] = mapped_column(String(214))
    version: Mapped[str] = mapped_column(String(64), default="")
    manifest: Mapped[str] = mapped_column(String(300), default="")
    vuln_ids: Mapped[list] = mapped_column(JSONB, default=list)
    severity: Mapped[str] = mapped_column(String(10), default="UNKNOWN")
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    fixed_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recommendation: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict] = mapped_column(JSONB, default=dict)


class BugHuntRun(Base):
    __tablename__ = "bughunt_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    status: Mapped[str] = mapped_column(String(20), default="running")
    provider: Mapped[str] = mapped_column(String(20))
    folder: Mapped[str] = mapped_column(String(500), default="")
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    byte_count: Mapped[int] = mapped_column(BigInteger, default=0)
    finding_count: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BugFinding(Base):
    __tablename__ = "bug_findings"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("bughunt_runs.id", ondelete="CASCADE"), index=True)
    severity: Mapped[str] = mapped_column(String(10))
    category: Mapped[str] = mapped_column(String(60), default="")
    cwe: Mapped[str] = mapped_column(String(20), default="")
    title: Mapped[str] = mapped_column(String(300))
    file: Mapped[str] = mapped_column(String(500), default="")
    line: Mapped[int | None] = mapped_column(Integer, nullable=True)
    description: Mapped[str] = mapped_column(Text, default="")
    recommendation: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(10), default="ai")  # ai | static
    provider: Mapped[str] = mapped_column(String(20), default="")


class ScanResult(Base):
    """ClamAV scan of something entering Elvin (upload, clone, image)."""

    __tablename__ = "scan_results"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    context: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))  # clean | infected | unavailable | error
    scanned: Mapped[int] = mapped_column(Integer, default=0)
    infected: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[dt.datetime] = _ts()


# --------------------------------------------------------------------------- load testing
class LoadTestConfig(Base):
    __tablename__ = "loadtest_configs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    host: Mapped[str] = mapped_column(String(300))
    users: Mapped[int] = mapped_column(Integer, default=10)
    spawn_rate: Mapped[int] = mapped_column(Integer, default=2)
    duration_s: Mapped[int] = mapped_column(Integer, default=60)
    endpoints: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = _ts()


class LoadTestRun(Base):
    __tablename__ = "loadtest_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[dt.datetime] = _ts()
    finished_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    total_requests: Mapped[int] = mapped_column(BigInteger, default=0)
    total_failures: Mapped[int] = mapped_column(BigInteger, default=0)
    avg_ms: Mapped[float] = mapped_column(Float, default=0)
    p50_ms: Mapped[float] = mapped_column(Float, default=0)
    p95_ms: Mapped[float] = mapped_column(Float, default=0)
    p99_ms: Mapped[float] = mapped_column(Float, default=0)
    max_ms: Mapped[float] = mapped_column(Float, default=0)
    rps: Mapped[float] = mapped_column(Float, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary: Mapped[dict] = mapped_column(JSONB, default=dict)


class LoadTestSample(Base):
    __tablename__ = "loadtest_samples"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loadtest_runs.id", ondelete="CASCADE"), index=True)
    t: Mapped[float] = mapped_column(Float)
    users: Mapped[int] = mapped_column(Integer, default=0)
    rps: Mapped[float] = mapped_column(Float, default=0)
    fail_rps: Mapped[float] = mapped_column(Float, default=0)
    p50: Mapped[float] = mapped_column(Float, default=0)
    p95: Mapped[float] = mapped_column(Float, default=0)
    avg: Mapped[float] = mapped_column(Float, default=0)
    total_requests: Mapped[int] = mapped_column(BigInteger, default=0)
    total_failures: Mapped[int] = mapped_column(BigInteger, default=0)


class LoadTestEndpointStat(Base):
    __tablename__ = "loadtest_endpoint_stats"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("loadtest_runs.id", ondelete="CASCADE"), index=True)
    method: Mapped[str] = mapped_column(String(10))
    name: Mapped[str] = mapped_column(String(300))
    requests: Mapped[int] = mapped_column(BigInteger, default=0)
    failures: Mapped[int] = mapped_column(BigInteger, default=0)
    median: Mapped[float] = mapped_column(Float, default=0)
    avg: Mapped[float] = mapped_column(Float, default=0)
    min: Mapped[float] = mapped_column(Float, default=0)
    max: Mapped[float] = mapped_column(Float, default=0)
    p90: Mapped[float] = mapped_column(Float, default=0)
    p95: Mapped[float] = mapped_column(Float, default=0)
    p99: Mapped[float] = mapped_column(Float, default=0)
    rps: Mapped[float] = mapped_column(Float, default=0)
    fail_rps: Mapped[float] = mapped_column(Float, default=0)
    avg_size: Mapped[float] = mapped_column(Float, default=0)


# --------------------------------------------------------------------------- GitHub
class GitHubAccount(Base):
    __tablename__ = "github_accounts"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    login: Mapped[str] = mapped_column(String(100))
    gh_id: Mapped[int] = mapped_column(BigInteger, default=0)
    token_enc: Mapped[str] = mapped_column(Text)
    scopes: Mapped[str] = mapped_column(String(500), default="")
    connected_at: Mapped[dt.datetime] = _ts()


class GitHubStats(Base):
    __tablename__ = "github_stats"
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    data: Mapped[dict] = mapped_column(JSONB, default=dict)
    suggestions: Mapped[list] = mapped_column(JSONB, default=list)
    fetched_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class CloneRequest(Base):
    __tablename__ = "clone_requests"
    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(24), default="inspecting")
    owner: Mapped[str] = mapped_column(String(100), default="")
    repo: Mapped[str] = mapped_column(String(100), default="")
    commit_sha: Mapped[str] = mapped_column(String(64), default="")
    readme: Mapped[str] = mapped_column(Text, default="")
    license_spdx: Mapped[str] = mapped_column(String(60), default="")
    license_name: Mapped[str] = mapped_column(String(120), default="")
    terms: Mapped[list] = mapped_column(JSONB, default=list)
    simplified: Mapped[list] = mapped_column(JSONB, default=list)
    summary: Mapped[str] = mapped_column(Text, default="")
    scan: Mapped[dict] = mapped_column(JSONB, default=dict)
    file_count: Mapped[int] = mapped_column(Integer, default=0)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    project_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    agreed_at: Mapped[dt.datetime | None] = _ts(nullable=True, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = _ts()
    updated_at: Mapped[dt.datetime] = _ts()
