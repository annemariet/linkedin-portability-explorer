"""Gradio UI for LinkedIn pipeline: fetch, enrich, summarize, and report."""

from __future__ import annotations

import html
import logging
import time
from typing import Callable, cast

import gradio as gr

from linkedin_api.gradio_keepalive import (
    KEEPALIVE_TICK,
    _stream_with_keepalive,
    normalize_report_markdown,
)
from linkedin_api.llm_config import (
    get_default_provider_model,
    resolve_mammouth_chat_model,
)
from linkedin_api.llm_models import fetch_all_provider_models, fetch_models_for_provider
from linkedin_api.pipeline_report import (
    CONTENT_LEVEL_CHOICES,
    CONTENT_LEVEL_MINIMAL,
    CONTENT_LEVEL_SUMMARY,
    PipelineCancelledError,
    REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    REPORT_MAX_POSTS_MINIMAL,
    REPORT_MODE_CHOICES,
    REPORT_MODE_PER_CATEGORY,
    REPORT_MODE_SINGLE_PASS,
    ReportComplete,
    ReportProgress,
    _default_max_posts,
    _get_posts_for_period,
    _load_report_cache,
    _load_report_prompt_debug,
    _resolve_max_posts,
    _save_report_cache,
    build_report_signature,
    generate_report_events,
)
from linkedin_api.activity_csv import get_default_csv_path
from linkedin_api.run_pipeline import run_pipeline_ui_streaming
from linkedin_api.period import parse_period

logger = logging.getLogger(__name__)

RunControl = dict[str, bool]


def _parse_optional_int(val, default=None):
    if val in (None, "", float("nan")):
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


PIPELINE_HINT_TEXT = "Click Get latest news report to refresh data and get a summary."
MIN_PROGRESS_VISIBILITY_SECONDS = 0.6
PERIOD_SYNTAX = "e.g. 1d, 7d, 14d, 30d, 1w, 2w, 1m"


def _report_cache_status_label(source: str) -> str:
    """User-visible status when report is reused from cache."""
    if source == "disk":
        return "Report loaded from cache (disk)"
    return "Report loaded from cache (session)"


def _render_pipeline_status(
    step_label: str | None = None,
    stage_progress: tuple[int, float] | None = None,
) -> str:
    """
    Render status below the run button: hint when idle, stage label + 5-segment bar while running.
    stage_progress: (stage_index 0-4, progress_in_stage 0-1). Bar splits into 5 segments.
    """
    if step_label is None or stage_progress is None:
        return (
            '<div style="color: #6b7280; margin: 0.25rem 0 0.5rem 0;">'
            f"{html.escape(PIPELINE_HINT_TEXT)}"
            "</div>"
        )
    stage_idx, prog = stage_progress
    stage_idx = max(0, min(4, stage_idx))
    prog = max(0.0, min(1.0, prog))
    segments: list[float] = []
    for i in range(5):
        if i < stage_idx:
            segments.append(1.0)
        elif i == stage_idx:
            segments.append(prog)
        else:
            segments.append(0.0)
    pct = [int(round(s * 100)) for s in segments]
    segment_css = (
        "flex: 1; min-width: 0; height: 100%; background: #e5e7eb; "
        "overflow: hidden; display: flex;"
    )
    fill_css = "height: 100%; background: #f97316; transition: width 200ms ease;"
    return (
        f'<div style="margin: 0.25rem 0; color: #111827;">{html.escape(step_label)}</div>'
        '<div style="display: flex; width: 100%; height: 10px; gap: 2px; '
        'border-radius: 4px; overflow: hidden;">'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[0]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[1]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[2]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[3]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[4]}%;"></div></div>'
        "</div>"
    )


def _parse_fraction(line: str, prefix: str) -> tuple[int, int] | None:
    """Extract (done, total) from lines like 'Enriching 3/10…' or 'Summarizing batch 2/5…'."""
    if not line.startswith(prefix) or "/" not in line:
        return None
    try:
        done_str, total_str = line.removeprefix(prefix).rstrip("…").split("/")
        return int(done_str), int(total_str)
    except (ValueError, IndexError):
        return None


def _status_from_pipeline_line(line: str) -> tuple[tuple[int, float], str] | None:
    """
    Map pipeline stream lines to (stage_index, progress_in_stage), label.
    Stages: 0=fetching, 1=enriching, 2=fetch linked URLs, 3=summarizing, 4=preparing report.
    """
    if line.startswith("Starting pipeline"):
        return (0, 0.0), "fetching…"
    if "Collected" in line:
        return (0, 1.0), "fetching…"
    frac = _parse_fraction(line, "Enriching ")
    if frac is not None:
        done, total = frac
        p = (done / total) if total > 0 else 1.0
        return (1, p), f"enriching [{done}/{total}]…"
    if "Enriched" in line:
        return (1, 1.0), "enriching…"
    frac = _parse_fraction(line, "Fetching linked URLs ")
    if frac is not None:
        done, total = frac
        p = (done / total) if total > 0 else 1.0
        return (2, p), f"fetching linked URLs [{done}/{total}]…"
    if "Fetched" in line and "URL" in line:
        return (2, 1.0), "fetching linked URLs…"
    frac = _parse_fraction(line, "Summarizing batch ")
    if frac is not None:
        done, total = frac
        p = (done / total) if total > 0 else 1.0
        return (3, p), f"summarizing [{done}/{total}]…"
    if "Summarized" in line:
        return (3, 1.0), "summarizing…"
    if "✅ Done" in line:
        return (4, 0.0), "preparing report…"
    if line.startswith("❌"):
        return (4, 1.0), "Failed."
    return None


def create_pipeline_interface():
    """Pipeline tab: single button runs collect → enrich → summarize → report."""
    with gr.Blocks(
        title="Pipeline",
        css=(
            "#report-output { min-height: 24em; overflow-y: auto; }"
            "#pipeline-status { min-height: 2.8em; }"
        ),
    ) as block:
        gr.Markdown(
            "# Pipeline\nOne run: fetch/enrich/summarize (using caches when possible), then generate report."
        )
        with gr.Row():
            period = gr.Dropdown(
                choices=["1d", "2d", "7d", "14d", "30d", "1w", "2w", "1m"],
                value="7d",
                label="Period",
                info=PERIOD_SYNTAX,
            )
            from_cache = gr.Checkbox(
                value=False,
                label="Skip fetch (use cached data only)",
                info="No LinkedIn API call; use only previously fetched data.",
            )
            limit = gr.Number(value=None, label="Limit (optional)", precision=0)
            report_mode = gr.Dropdown(
                choices=REPORT_MODE_CHOICES,
                value=REPORT_MODE_SINGLE_PASS,
                label="Report mode",
            )
            content_level = gr.Dropdown(
                choices=CONTENT_LEVEL_CHOICES,
                value=CONTENT_LEVEL_MINIMAL,
                label="Content level",
            )
            max_posts_report = gr.Number(
                value=REPORT_MAX_POSTS_MINIMAL,
                label="Max posts (report)",
                minimum=1,
                maximum=500,
                precision=0,
                info="Defaults: 100 (minimal), 50 (summary), 20 (full).",
            )
            max_full_post_chars = gr.Number(
                value=REPORT_MAX_FULL_POST_CHARS_DEFAULT,
                label="Max post content length",
                minimum=100,
                maximum=10000,
                precision=0,
                info="Chars per post when Full. Ignored for Minimal/Summary.",
            )

        def suggest_max_posts(content_lvl: str):
            return _default_max_posts(content_lvl or CONTENT_LEVEL_SUMMARY)

        content_level.change(
            fn=suggest_max_posts,
            inputs=[content_level],
            outputs=[max_posts_report],
        )
        with gr.Accordion("Model selection", open=False):
            sp, sm = get_default_provider_model("summary")
            rp, rm = get_default_provider_model("report")
            provider_choices = ["ollama", "anthropic", "mammouth"]

            models_by_provider = fetch_all_provider_models()
            for prov in (sp, rp):
                if not models_by_provider.get(prov, []):
                    models_by_provider[prov] = fetch_models_for_provider(prov) or []

            def _choice_ids(choices: list[tuple[str, str]]) -> list[str]:
                """Extract model ids from (label, model_id) choices."""
                return [c[1] for c in choices]

            def _resolve_model_value(
                stage: str,
                provider: str,
                default_model: str,
                choices: list[tuple[str, str]],
            ) -> str:
                ids = _choice_ids(choices)
                if default_model in ids:
                    return default_model
                fallback = ids[0] if ids else ""
                if default_model:
                    logger.warning(
                        "Configured %s model %r is not in the %s provider list; "
                        "using %r instead. Check LLM_*_MODEL / LLM_MODEL and provider.",
                        stage,
                        default_model,
                        provider,
                        fallback,
                    )
                return fallback

            def _choices_for(
                d: dict, provider: str, default_model: str, stage: str
            ) -> tuple[list[tuple[str, str]], str]:
                models = d.get(provider, [])
                choices = models if models else [(default_model, default_model)]
                value = _resolve_model_value(stage, provider, default_model, choices)
                return choices, value

            s_choices, s_val = _choices_for(models_by_provider, sp, sm, "summary")
            r_choices, r_val = _choices_for(models_by_provider, rp, rm, "report")

            with gr.Row():
                with gr.Column():
                    gr.Markdown("**Summary** (categorization & short summary)")
                    summary_provider = gr.Dropdown(
                        choices=provider_choices,
                        value=sp,
                        label="Provider",
                    )
                    summary_model = gr.Dropdown(
                        choices=s_choices,
                        value=s_val,
                        label="Model",
                    )
                with gr.Column():
                    gr.Markdown("**Report** (batch summaries & final report)")
                    report_provider = gr.Dropdown(
                        choices=provider_choices,
                        value=rp,
                        label="Provider",
                    )
                    report_model = gr.Dropdown(
                        choices=r_choices,
                        value=r_val,
                        label="Model",
                    )

            def refresh_summary_models(provider):
                choices = fetch_models_for_provider(provider) or [(sm, sm)]
                value = _resolve_model_value("summary", provider, sm, choices)
                return gr.update(choices=choices, value=value)

            def refresh_report_models(provider):
                choices = fetch_models_for_provider(provider) or [(rm, rm)]
                value = _resolve_model_value("report", provider, rm, choices)
                return gr.update(choices=choices, value=value)

            summary_provider.change(
                refresh_summary_models,
                inputs=[summary_provider],
                outputs=[summary_model],
            )
            report_provider.change(
                refresh_report_models,
                inputs=[report_provider],
                outputs=[report_model],
            )
        run_ctrl = gr.State({"cancel": False})
        with gr.Row():
            run_btn = gr.Button("Get latest news report", variant="primary")
            stop_btn = gr.Button("Stop", variant="stop", interactive=False)
        pipeline_status = gr.HTML(
            value=_render_pipeline_status(), elem_id="pipeline-status"
        )
        report_output = gr.Markdown(
            value="Report will appear here after the pipeline run.",
            label="Report",
            elem_id="report-output",
            # Self-generated LLM output; default sanitizer strips <https://…> autolinks.
            sanitize_html=False,
        )
        with gr.Accordion("Debug: Last report prompt", open=False):
            prompt_debug_btn = gr.Button("View last prompt", size="sm")
            prompt_debug_output = gr.Markdown(
                value=_load_report_prompt_debug(),
                label="Prompt",
                elem_id="prompt-debug-output",
            )
        report_cache_state = gr.State(value=None)  # (report_text, signature) or None

        def load_prompt_debug(cache_state):
            sig = cache_state[1] if cache_state else None
            return _load_report_prompt_debug(sig)

        prompt_debug_btn.click(
            fn=load_prompt_debug,
            inputs=[report_cache_state],
            outputs=[prompt_debug_output],
        )

        def _pipeline_run_outputs(
            status_html: str,
            report,
            cache_val,
            *,
            running: bool,
            prompt_debug=None,
        ):
            if prompt_debug is None:
                prompt_debug = gr.update()
            return (
                status_html,
                report,
                cache_val,
                gr.update(interactive=not running),
                gr.update(interactive=running),
                prompt_debug,
            )

        def _stopped_run_outputs(cache_val):
            return _pipeline_run_outputs(
                _render_pipeline_status("Stopped.", (4, 1.0)),
                gr.update(
                    value="_Run stopped. Change model/settings above, then run again._"
                ),
                cache_val,
                running=False,
            )

        def prepare_run(cache, ctrl: RunControl):
            ctrl["cancel"] = False
            return _pipeline_run_outputs(
                _render_pipeline_status("fetching…", (0, 0.0)),
                gr.update(value="_Running pipeline… report will appear when ready._"),
                cache,
                running=True,
            )

        def stop_run(ctrl: RunControl, cache):
            ctrl["cancel"] = True
            logger.info("Pipeline stop requested")
            stopped = _stopped_run_outputs(cache)
            return (ctrl, *stopped)

        def _resolve_runtime_model(
            provider: str | None, model: str | None, stage: str
        ) -> str | None:
            """Re-fetch provider models and fall back if the selected id is unavailable."""
            if not provider or not model:
                return model
            choices = fetch_models_for_provider(provider) or []
            ids = [mid for _, mid in choices]
            if model in ids:
                resolved = model
            else:
                fallback = ids[0] if ids else model
                logger.warning(
                    "Selected %s model %r is not in the current %s list; using %r",
                    stage,
                    model,
                    provider,
                    fallback,
                )
                resolved = fallback
            if provider == "mammouth":
                resolved = resolve_mammouth_chat_model(resolved, quiet=False)
            return resolved

        def run_all(
            last: str,
            from_cache: bool,
            lim,
            mode: str,
            content_lvl: str,
            max_posts_val,
            max_full_chars_val,
            sum_prov,
            sum_mod,
            rep_prov,
            rep_mod,
            cache,
            ctrl: RunControl,
        ):
            logger.info(
                "Pipeline & report started: last=%s from_cache=%s limit=%s",
                last,
                from_cache,
                lim,
            )
            last_clean = (last or "").strip()
            if parse_period(last_clean) is None:
                err = f"Invalid period '{last}'. {PERIOD_SYNTAX}"
                yield _pipeline_run_outputs(
                    _render_pipeline_status("Invalid period", (0, 0.0)),
                    err,
                    cache,
                    running=False,
                )
                return
            started_at = time.monotonic()

            def _ensure_min_progress_visibility() -> None:
                elapsed = time.monotonic() - started_at
                remaining = MIN_PROGRESS_VISIBILITY_SECONDS - elapsed
                if remaining > 0:
                    time.sleep(remaining)

            lim_int = _parse_optional_int(lim)

            stage_progress: tuple[int, float] = (0, 0.0)
            step_label = "fetching…"

            def _is_stopped() -> bool:
                return bool(ctrl.get("cancel"))

            def _pipeline_keepalive_outputs():
                return _pipeline_run_outputs(
                    _render_pipeline_status(step_label, stage_progress),
                    gr.update(),
                    cache,
                    running=True,
                )

            def _check_user_stop() -> bool:
                if _is_stopped():
                    logger.info("Pipeline run cancelled by user")
                    return True
                return False

            sum_mod = _resolve_runtime_model(sum_prov, sum_mod, "summary")
            rep_mod = _resolve_runtime_model(rep_prov, rep_mod, "report")

            try:
                pipeline = run_pipeline_ui_streaming(
                    last=last_clean,
                    from_cache=from_cache,
                    limit=lim_int,
                    summary_provider=sum_prov or None,
                    summary_model=sum_mod or None,
                    should_cancel=_is_stopped,
                )
                for chunk in _stream_with_keepalive(
                    pipeline,
                    lambda: KEEPALIVE_TICK,
                    should_stop=_is_stopped,
                ):
                    if chunk is KEEPALIVE_TICK:
                        if _check_user_stop():
                            yield _stopped_run_outputs(cache)
                            return
                        yield _pipeline_keepalive_outputs()
                        continue
                    last = chunk.strip().split("\n")[-1] if chunk.strip() else ""
                    status_update = _status_from_pipeline_line(last)
                    if status_update is not None:
                        stage_progress, step_label = status_update
                    if last.startswith("❌"):
                        error_text = last
                        if error_text.startswith("❌ "):
                            error_text = error_text[2:].strip()
                        if not error_text:
                            error_text = "Pipeline failed."
                        _ensure_min_progress_visibility()
                        yield _pipeline_run_outputs(
                            _render_pipeline_status(step_label, stage_progress),
                            f"⚠️ {error_text}",
                            cache,
                            running=False,
                        )
                        return
                    if _check_user_stop():
                        yield _stopped_run_outputs(cache)
                        return
                    yield _pipeline_keepalive_outputs()
            except Exception as e:
                if _is_stopped():
                    yield _stopped_run_outputs(cache)
                    return
                logger.exception("Pipeline failed")
                err_msg = str(e)[:200]
                _ensure_min_progress_visibility()
                yield _pipeline_run_outputs(
                    _render_pipeline_status("Failed.", (4, 1.0)),
                    f"⚠️ Pipeline failed: {err_msg}",
                    cache,
                    running=False,
                )
                return

            if _check_user_stop():
                yield _stopped_run_outputs(cache)
                return

            stage_progress, step_label = (4, 0.0), "preparing report…"
            yield _pipeline_run_outputs(
                _render_pipeline_status(step_label, stage_progress),
                gr.update(value="_Generating report…_"),
                cache,
                running=True,
            )
            report_mode_val = mode or REPORT_MODE_PER_CATEGORY
            content_level_val = content_lvl or CONTENT_LEVEL_SUMMARY
            max_posts_int = _parse_optional_int(max_posts_val)
            max_full_chars_int = _parse_optional_int(
                max_full_chars_val, REPORT_MAX_FULL_POST_CHARS_DEFAULT
            )
            max_full_chars_int = max(
                100,
                min(10000, max_full_chars_int or REPORT_MAX_FULL_POST_CHARS_DEFAULT),
            )
            max_posts_resolved = _resolve_max_posts(max_posts_int, content_level_val)
            logger.info(
                "Report mode: raw=%r → %s; content: raw=%r → %s; max_posts=%s; max_full=%s",
                mode,
                report_mode_val,
                content_lvl,
                content_level_val,
                max_posts_resolved,
                max_full_chars_int,
            )
            csv_path = get_default_csv_path()
            metas, period_dates = _get_posts_for_period(
                last_clean, max_posts_resolved, csv_path=csv_path
            )
            result: str | None = None
            cache_out = cache
            report_cache_source: str | None = None
            if not metas:
                result = (
                    "No summarized posts found. Run the pipeline first "
                    "(collect → enrich → summarize)."
                )
                cache_out = None
            else:
                sig = build_report_signature(
                    metas,
                    report_mode=report_mode_val,
                    content_level=content_level_val,
                    max_posts=max_posts_int,
                    max_full_post_chars=max_full_chars_int,
                    report_provider=rep_prov or None,
                    report_model=rep_mod or None,
                    period=last_clean,
                )
                disk = _load_report_cache(sig)
                if disk is not None:
                    result = disk[0]
                    report_cache_source = "disk"
                    logger.info("Report cache hit (disk)")
                    cache_out = (result, sig)
                elif cache is not None and cache[1] == sig:
                    result = cache[0]
                    report_cache_source = "session"
                    logger.info("Report cache hit (session)")
                    cache_out = cache
                else:
                    report_label = "preparing report…"
                    report_frac = 0.0
                    try:
                        report_stream = generate_report_events(
                            report_mode=report_mode_val,
                            content_level=content_level_val,
                            max_posts=max_posts_int,
                            max_full_post_chars=max_full_chars_int,
                            report_provider=rep_prov or None,
                            report_model=rep_mod or None,
                            period=last_clean,
                            activities_csv_path=csv_path,
                            metas=metas,
                            period_dates=period_dates,
                            signature=sig,
                            should_cancel=lambda: bool(ctrl.get("cancel")),
                        )
                        for event in _stream_with_keepalive(
                            report_stream,
                            cast(
                                "Callable[[], ReportProgress | ReportComplete]",
                                lambda: KEEPALIVE_TICK,
                            ),
                            should_stop=_is_stopped,
                        ):
                            if event is KEEPALIVE_TICK:
                                if _check_user_stop():
                                    yield _stopped_run_outputs(cache)
                                    return
                                yield _pipeline_run_outputs(
                                    _render_pipeline_status(
                                        report_label, (4, report_frac)
                                    ),
                                    gr.update(value="_Generating report…_"),
                                    cache,
                                    running=True,
                                )
                                continue
                            if isinstance(event, ReportProgress):
                                report_label = event.label
                                report_frac = event.frac
                                logger.info("Report generation: %s", report_label)
                                yield _pipeline_run_outputs(
                                    _render_pipeline_status(
                                        report_label, (4, report_frac)
                                    ),
                                    gr.update(value="_Generating report…_"),
                                    cache,
                                    running=True,
                                )
                            elif isinstance(event, ReportComplete):
                                result = event.text
                                if event.signature:
                                    sig = event.signature
                    except PipelineCancelledError:
                        yield _stopped_run_outputs(cache)
                        return
                    if result is not None and sig is not None:
                        cache_out = (result, sig)
                        _save_report_cache(result, sig)
                    else:
                        cache_out = None
            display_result = normalize_report_markdown(result or "")
            if report_cache_source:
                status_html = _render_pipeline_status(
                    _report_cache_status_label(report_cache_source),
                    (4, 1.0),
                )
            else:
                status_html = _render_pipeline_status(None, None)
            yield _pipeline_run_outputs(
                status_html,
                display_result,
                cache_out,
                running=False,
            )

        run_event = run_btn.click(
            fn=prepare_run,
            inputs=[report_cache_state, run_ctrl],
            outputs=[
                pipeline_status,
                report_output,
                report_cache_state,
                run_btn,
                stop_btn,
                prompt_debug_output,
            ],
            queue=False,
        ).then(
            fn=run_all,
            inputs=[
                period,
                from_cache,
                limit,
                report_mode,
                content_level,
                max_posts_report,
                max_full_post_chars,
                summary_provider,
                summary_model,
                report_provider,
                report_model,
                report_cache_state,
                run_ctrl,
            ],
            outputs=[
                pipeline_status,
                report_output,
                report_cache_state,
                run_btn,
                stop_btn,
                prompt_debug_output,
            ],
            show_progress="hidden",
        )
        stop_btn.click(
            fn=stop_run,
            inputs=[run_ctrl, report_cache_state],
            outputs=[
                run_ctrl,
                pipeline_status,
                report_output,
                report_cache_state,
                run_btn,
                stop_btn,
                prompt_debug_output,
            ],
            cancels=[run_event],
            queue=False,
        )
    return block
