#!/usr/bin/env python3
"""Gradio UI: LinkedIn pipeline (collect → enrich → summarize) and activity report."""

import logging
import os

import dotenv

dotenv.load_dotenv()

from linkedin_api.gradio_pipeline_ui import create_pipeline_interface

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    demo = create_pipeline_interface()
    port = int(os.getenv("PORT", 7860))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(
        "LLM_PROVIDER=%s (embedding: %s)",
        os.getenv("LLM_PROVIDER", "<unset>"),
        os.getenv("EMBEDDING_PROVIDER", "<unset>"),
    )
    logger.info("Starting Gradio app on %s:%s", host, port)
    demo.queue(default_concurrency_limit=1)
    demo.launch(server_name=host, server_port=port, share=False, show_error=True)


if __name__ == "__main__":
    main()
