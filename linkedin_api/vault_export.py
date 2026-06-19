from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from kg_vault.catalog import CATALOG_THIRD_PARTY_PREFIX, parse_source_id_from_markdown
from kg_vault.writer import LocalVaultBackend, VaultWriteError, VaultWriter

from linkedin_api.content_store import load_content, load_metadata
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.fetch_linked_content import load_resource
from linkedin_api.vault_catalog import (
    CatalogBuild,
    activity_source_id,
    article_source_id,
    build_activity_catalog_markdown,
    build_article_catalog_markdown,
    emit_intake_record,
    scan_catalog_source_ids,
    vault_write_message,
)

logger = logging.getLogger(__name__)

VAULT_ENABLED_ENV = "LINKEDIN_VAULT_ENABLED"
VAULT_ROOT_ENV = "LUCYWORKS_VAULT_PATH"
OUTPUT_ROOT_ENV = "LINKEDIN_VAULT_OUTPUT_ROOT"


def vault_writes_enabled() -> bool:
    raw = os.environ.get(VAULT_ENABLED_ENV, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def resolve_vault_root(output_root: str | Path | None = None) -> Path:
    explicit = str(output_root).strip() if output_root is not None else ""
    if not explicit:
        explicit = os.environ.get(OUTPUT_ROOT_ENV, "").strip()
    if not explicit:
        explicit = os.environ.get(VAULT_ROOT_ENV, "").strip()
    if not explicit:
        raise VaultWriteError(
            f"Vault output root required (--output-root or {OUTPUT_ROOT_ENV} or {VAULT_ROOT_ENV})"
        )
    return Path(explicit).expanduser().resolve()


@dataclass(frozen=True)
class VaultExportReport:
    attempted: int
    written: int
    skipped_existing: int
    failed: int


def _catalog_dir(vault_root: Path) -> Path:
    return vault_root / CATALOG_THIRD_PARTY_PREFIX


def _should_skip(
    source_id: str,
    rel_path: str,
    *,
    index: dict[str, str],
    writer: VaultWriter,
) -> bool:
    existing_path = index.get(source_id)
    if existing_path:
        return True
    existing = writer.read(rel_path)
    if existing is not None and parse_source_id_from_markdown(existing) == source_id:
        index[source_id] = rel_path
        return True
    return False


def _write_build(
    writer: VaultWriter,
    build: CatalogBuild,
    *,
    index: dict[str, str],
    force_overwrite: bool,
) -> str:
    """Write one catalog file. Returns 'written', 'skipped', or 'failed'."""
    if not force_overwrite and _should_skip(
        build.source_id, build.rel_path, index=index, writer=writer
    ):
        logger.info(
            "vault_write_skipped source_id=%s reason=already_written",
            build.source_id,
        )
        return "skipped"

    try:
        already_on_disk = writer.exists(build.rel_path)
        result = writer.write(
            build.rel_path,
            build.markdown,
            message=vault_write_message(title=build.title, created=not already_on_disk),
        )
        index[build.source_id] = build.rel_path
        emit_intake_record(
            catalog_path=build.rel_path,
            source_id=build.source_id,
            producer=build.producer,
            occurred_at=datetime.now(UTC),
            tldr=build.tldr,
        )
        logger.info(
            "vault_write_ok source_id=%s path=%s created=%s",
            build.source_id,
            result.rel_path,
            result.created,
        )
        return "written"
    except VaultWriteError as exc:
        logger.warning(
            "vault_write_failed source_id=%s path=%s error=%s",
            build.source_id,
            build.rel_path,
            exc,
        )
        return "failed"
    except Exception:
        logger.exception(
            "vault_write_failed source_id=%s path=%s",
            build.source_id,
            build.rel_path,
        )
        return "failed"


def export_activities_to_vault(
    activities: list[EnrichedRecord],
    *,
    output_root: str | Path | None = None,
    writer: VaultWriter | None = None,
    force_overwrite: bool = False,
) -> VaultExportReport:
    """Write one catalog file per activity; failures do not abort the run."""
    vault_root = resolve_vault_root(output_root)
    resolved_writer = writer or VaultWriter(backend=LocalVaultBackend(vault_root))
    index = scan_catalog_source_ids(_catalog_dir(vault_root))
    occupied: set[str] = set()
    written = skipped = failed = 0
    attempted = 0

    for rec in activities:
        if not (rec.activity_id or "").strip():
            continue
        content = load_content(rec.post_urn) or rec.content or ""
        meta = load_metadata(rec.post_urn) or {}
        linked_ids = [
            article_source_id(url, resolve=False)
            for url in (meta.get("urls") or [])
            if isinstance(url, str) and url.strip()
        ]
        build = build_activity_catalog_markdown(
            rec,
            content=content,
            meta=meta,
            occupied=occupied,
            linked_article_ids=linked_ids,
        )
        attempted += 1
        outcome = _write_build(
            resolved_writer, build, index=index, force_overwrite=force_overwrite
        )
        if outcome == "written":
            written += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1

    return VaultExportReport(
        attempted=attempted,
        written=written,
        skipped_existing=skipped,
        failed=failed,
    )


def export_linked_articles_to_vault(
    urls: set[str],
    *,
    activity_ids_by_url: dict[str, list[str]] | None = None,
    output_root: str | Path | None = None,
    writer: VaultWriter | None = None,
    force_overwrite: bool = False,
) -> VaultExportReport:
    """Write catalog files for linked article URLs fetched into the resource store."""
    vault_root = resolve_vault_root(output_root)
    resolved_writer = writer or VaultWriter(backend=LocalVaultBackend(vault_root))
    index = scan_catalog_source_ids(_catalog_dir(vault_root))
    occupied: set[str] = set()
    written = skipped = failed = 0
    attempted = 0
    activity_map = activity_ids_by_url or {}

    for url in sorted(urls):
        result = load_resource(url)
        if result is None:
            continue
        related = [
            activity_source_id(aid)
            for aid in activity_map.get(url, [])
            if (aid or "").strip()
        ]
        build = build_article_catalog_markdown(
            result,
            activity_source_ids=related,
            occupied=occupied,
        )
        if build is None:
            continue
        attempted += 1
        outcome = _write_build(
            resolved_writer, build, index=index, force_overwrite=force_overwrite
        )
        if outcome == "written":
            written += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1

    return VaultExportReport(
        attempted=attempted,
        written=written,
        skipped_existing=skipped,
        failed=failed,
    )


def export_period_to_vault(
    activities: list[EnrichedRecord],
    *,
    output_root: str | Path | None = None,
    writer: VaultWriter | None = None,
    force_overwrite: bool = False,
) -> VaultExportReport:
    """Export activities and their linked articles for a pipeline period."""
    activity_report = export_activities_to_vault(
        activities,
        output_root=output_root,
        writer=writer,
        force_overwrite=force_overwrite,
    )
    urls: set[str] = set()
    activity_ids_by_url: dict[str, list[str]] = {}
    for rec in activities:
        meta = load_metadata(rec.post_urn) or {}
        for raw_url in meta.get("urls") or []:
            if not isinstance(raw_url, str) or not raw_url.strip():
                continue
            url = raw_url.strip()
            urls.add(url)
            activity_ids_by_url.setdefault(url, []).append(rec.activity_id)

    article_report = export_linked_articles_to_vault(
        urls,
        activity_ids_by_url=activity_ids_by_url,
        output_root=output_root,
        writer=writer,
        force_overwrite=force_overwrite,
    )
    return VaultExportReport(
        attempted=activity_report.attempted + article_report.attempted,
        written=activity_report.written + article_report.written,
        skipped_existing=activity_report.skipped_existing
        + article_report.skipped_existing,
        failed=activity_report.failed + article_report.failed,
    )


def run_vault_export_if_enabled(
    activities: list[EnrichedRecord],
    *,
    output_root: str | Path | None = None,
    writer: VaultWriter | None = None,
    quiet: bool = False,
) -> VaultExportReport | None:
    if output_root is None and not vault_writes_enabled():
        return None
    report = export_period_to_vault(
        activities,
        output_root=output_root,
        writer=writer,
    )
    if not quiet:
        print(
            f"Vault export: attempted={report.attempted} written={report.written} "
            f"skipped={report.skipped_existing} failed={report.failed}"
        )
    return report
