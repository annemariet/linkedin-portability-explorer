# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python client for LinkedIn's Portability API (DMA-compliant) that uses the API for selective reading of your own activity (posts, reactions, comments), builds a knowledge graph in Neo4j, and enables GraphRAG-powered semantic search. Data is obtained via the Portability API, not by scraping.

## Core Workflow

1. **Fetch LinkedIn Data** → Extract changelog activities via LinkedIn Portability API
2. **Build Knowledge Graph** → Import entities (People, Posts, Comments) and relationships into Neo4j
3. **Index Content** → Extract post/comment text, create embeddings, build vector index
4. **Query with GraphRAG** → Semantic search combining vector retrieval and graph traversal

## Development Commands

### Setup
```bash
# Install all dependencies (uses uv for dependency management)
uv sync

# Install with dev dependencies
uv sync --all-groups
```

### Authentication Setup
```bash
# Store LinkedIn token in macOS Keychain (recommended)
uv run python scripts/setup_token.py

# Check token validity
uv run python scripts/check_token.py
```

### Running Scripts
```bash
# Fetch LinkedIn changelog data and produce CSV + legacy JSON
uv run python -m linkedin_api.extract_graph_data
uv run python -m linkedin_api.extract_graph_data --csv-only

# Build Neo4j graph (from master CSV by default, or legacy JSON)
uv run python -m linkedin_api.build_graph
uv run python -m linkedin_api.build_graph --json-file outputs/neo4j_data_*.json
uv run python -m linkedin_api.build_graph --full-rebuild

# Enrich graph with LLM-extracted entities (Phase B)
uv run python -m linkedin_api.enrich_graph --limit 5

# Index content for GraphRAG (with optional limit for testing)
uv run python -m linkedin_api.index_content
uv run python -m linkedin_api.index_content --limit 5

# Query GraphRAG (interactive mode)
uv run python -m linkedin_api.query_graphrag

# Query GraphRAG (command line)
uv run python -m linkedin_api.query_graphrag "What posts did I react to about AI?"
uv run python -m linkedin_api.query_graphrag --cypher "What are main topics in posts I commented on?"

# Verify GraphRAG indexing
uv run python -m linkedin_api.verify_indexing

# Launch Gradio web interface
uv run python -m linkedin_api.gradio_app

# Period-based pipeline (CSV → content store → report)
uv run python -m linkedin_api.run_pipeline --last 7d
uv run python -m linkedin_api.run_pipeline --skip-fetch --last 30d

# Summarize/fetch CSV only (no JSON output; data in ~/.linkedin_api/data/activities.csv)
uv run python -m linkedin_api.summarize_activity --from-cache --last 7d
uv run python -m linkedin_api.summarize_activity --last 7d

# Enrich into content store (default: master CSV)
uv run python -m linkedin_api.enrich_activities
uv run python -m linkedin_api.enrich_activities --limit 5
```

### Testing
```bash
# Run all tests
uv run pytest

# Run tests with coverage
uv run pytest --cov=linkedin_api --cov-report=html

# Run specific test file
uv run pytest tests/test_changelog_utils.py

# Run tests in linkedin_api package
uv run pytest linkedin_api/tests/
```

### Linting & Formatting
```bash
# Format code with black
uv run black .

# Check formatting without modifying
uv run black --check .

# Run flake8
uv run flake8 linkedin_api tests scripts *.py

# Run type checking with mypy
uv run mypy linkedin_api
```

### Pre-commit Hooks
```bash
# Install pre-commit hooks
uv run pre-commit install

# Run hooks manually
uv run pre-commit run --all-files
```

## Architecture

### Core Module Structure

All core scripts are designed as **dual-use modules**:
- **CLI tools**: Run directly with `uv run python -m <module>`
- **Importable modules**: Import functions/constants in other scripts

This avoids code duplication (e.g., `gradio_app.py` imports from `query_graphrag.py`).

### Key Modules

#### `linkedin_api/graph_schema.py`
Single source of truth for the Neo4j graph schema:
- `NODE_TYPES`: All node type definitions (Person, Post, Comment, Resource, Technology, Concept, etc.)
- `RELATIONSHIP_TYPES`: All relationship type names
- `PATTERNS`: Valid (source, rel, target) triples
- `PHASE_A_RELATIONSHIP_TYPES`: Structural relationships from API data
- `get_pipeline_schema()`: Returns schema dict for `SimpleKGPipeline`

#### `linkedin_api/activity_csv.py`
Activity record model and CSV serialization:
- `ActivityRecord` dataclass with typed fields (owner, activity_type, time, etc.)
- `ActivityType` enum: POST, COMMENT, REPOST, INSTANT_REPOST, REACTION_TO_POST, REACTION_TO_COMMENT
- `append_records_csv()`: Append-only writes with dedup by `activity_urn`
- `load_records_csv()`, `filter_by_date()`, `filter_by_type()`
- `get_data_dir()`: Canonical data directory (`$LINKEDIN_DATA_DIR` or `~/.linkedin_api/data/`)

#### `linkedin_api/llm_config.py`
Configurable LLM/embedder factory:
- `create_llm()`: Returns LLM instance (OpenAI, Ollama, VertexAI, or Anthropic based on `LLM_PROVIDER`)
- `create_embedder()`: Returns embedder instance (based on `EMBEDDING_PROVIDER`)
- Supports custom base URLs (e.g., Mammouth) via `LLM_BASE_URL`

#### `linkedin_api/auth.py`
Handles LinkedIn API authentication:
- Retrieves tokens from macOS Keychain (preferred) or environment variables
- Falls back gracefully if keyring unavailable
- Builds authenticated `requests.Session` with proper headers

#### `linkedin_api/changelog_utils.py`
Shared utilities for fetching LinkedIn changelog data:
- `fetch_changelog_data()`: Paginated fetching with resource filtering
- Handles batching (default 50 items per request)
- Session management via `get_changelog_session()`

#### `linkedin_api/extract_graph_data.py`
Extracts entities and relationships from changelog data:
- `extract_activity_records()`: Returns `List[ActivityRecord]` for CSV output
- `extract_entities_and_relationships()`: Legacy path returning dicts for JSON output
- `records_to_neo4j_json()`: Converts records to legacy JSON with new relationship names
- Appends to master CSV at `~/.linkedin_api/data/activities.csv`

#### `linkedin_api/build_graph.py`
Loads graph data into Neo4j:
- `load_from_csv()`: Default path, reads master CSV
- `load_graph_data()`: Legacy JSON path (backward compat)
- Incremental loading with MERGE (default) or full rebuild with `--full-rebuild`
- Batched operations (500 items per batch)

#### `linkedin_api/enrich_graph.py`
Phase B: LLM-powered graph enrichment:
- Uses `SimpleKGPipeline` from `neo4j-graphrag` to extract entities and relationships
- Processes posts/comments that haven't been enriched yet
- Creates Technology, Concept, Resource, and other knowledge nodes
- CLI: `uv run python -m linkedin_api.enrich_graph [--limit N]`

#### `linkedin_api/index_content.py`
Indexes post/comment content for GraphRAG:
- Fetches Post/Comment nodes from Neo4j (content sourced from Portability API)
- Uses content from Neo4j; only fetches from post URLs when content is missing (e.g. legacy data)
- Splits text into chunks (500 chars, 100 char overlap)
- Generates embeddings via configurable provider (see `llm_config.py`)
- Creates Chunk nodes linked to source posts/comments
- Creates/updates vector index for retrieval
- Supports `--limit N` for quick testing

#### `linkedin_api/query_graphrag.py`
GraphRAG query interface (CLI + importable):
- **Exports**: `find_vector_index()`, `create_vector_retriever()`, `create_vector_cypher_retriever()`, configuration constants
- Two retrieval modes:
  - **Vector**: Fast semantic search over chunk embeddings
  - **Vector+Cypher**: Combines vector search with graph traversal for relationship context (includes Resource nodes)
- Interactive mode (default) or command-line mode
- Commands: `cypher` (toggle retriever), `topk <N>` (set result count), `quit/exit`

#### `linkedin_api/gradio_app.py`
Web UI for GraphRAG queries:
- **Imports functions from `query_graphrag.py`** (no code duplication)
- Shows enrichment node counts (Resource, Technology, Concept, etc.) in stats
- Handles GCP credentials from environment variables
- Respects `$PORT` for cloud deployment (Scalingo)

#### `linkedin_api/utils/urls.py`
URL extraction and categorization utilities:
- `extract_urls_from_text()`, `is_comment_feed_url()`, `should_ignore_url()`, `resolve_redirect()`

#### `linkedin_api/utils/urns.py`
URN parsing and URL conversion:
- Extracts IDs from LinkedIn URNs
- Converts URNs to public LinkedIn URLs
- Handles various URN formats (posts, comments, people)

#### `scripts/`
Standalone and migration scripts:
- `setup_token.py`, `check_token.py` – Token setup and validation
- `migrate_comment_urns.py` – Fix Comment URN format in Neo4j
- `fix_repost_authors.py` – Fix repost CREATES/REPOSTS from re-extracted JSON
- `verify_vertex_ai.py` – Vertex AI smoke test

### Data Flow

```
LinkedIn API
    ↓ (fetch_changelog_data)
Changelog JSON
    ↓ (extract_graph_data.py)
Master CSV (~/.linkedin_api/data/activities.csv) + legacy JSON
    ↓ (build_graph.py)                              Phase A: structural
Neo4j Graph (People, Posts, Comments)
    ↓ (enrich_graph.py)                             Phase B: LLM enrichment
Neo4j Graph + Knowledge Nodes (Technology, Concept, Resource, etc.)
    ↓ (index_content.py)
Chunk Nodes + Embeddings + Vector Index
    ↓ (query_graphrag.py / gradio_app.py)
GraphRAG Queries
```

### Neo4j Graph Schema

Defined in `linkedin_api/graph_schema.py`. See that module for the complete list.

**Phase A Nodes (structural, from API data):**
- `Person`: `urn`, `person_id`
- `Post`: `urn`, `post_id`, `url`, `created_at`, `content`
- `Comment`: `urn`, `comment_id`, `url`, `created_at`, `text`
- `Chunk`: `text`, `embedding`, `chunk_index` (linked to Posts/Comments)

**Phase B Nodes (LLM-enriched):**
- `Resource`, `Technology`, `Concept`, `Process`, `Challenge`, `Benefit`, `Example`

**Relationships:**
- `Person -[:IS_AUTHOR_OF]-> Post/Comment`
- `Person -[:REACTED_TO {reaction_type, timestamp}]-> Post/Comment`
- `Comment -[:COMMENTS_ON]-> Post/Comment`
- `Person -[:REPOSTS {timestamp}]-> Post`
- `Post -[:REPOSTS]-> Post`
- `Post/Comment -[:REFERENCES]-> Resource`
- `Chunk -[:FROM_CHUNK]-> Post/Comment`
- Phase B also creates: `RELATED_TO`, `PART_OF`, `USED_IN`, `LEADS_TO`, `HAS_CHALLENGE`, `CITES`, `CREATED`

## Environment Variables

Required for API access:
```bash
LINKEDIN_ACCESS_TOKEN=your_token_here
LINKEDIN_ACCOUNT=your_email@example.com  # Used as keyring account
```

Required for Neo4j:
```bash
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_password
NEO4J_DATABASE=neo4j  # Default
```

LLM/Embedder configuration (used by enrichment, indexing, and query):
```bash
LLM_PROVIDER=openai              # openai | ollama | vertexai | anthropic
LLM_MODEL=gpt-5-nano             # Model name (OpenAI-compatible default)
LLM_SUMMARY_PROVIDER=            # Override for summarization (cheaper model; e.g. ollama)
LLM_SUMMARY_MODEL=               # Override model for summarization
LLM_REPORT_PROVIDER=             # Override for report generation (stronger model)
LLM_REPORT_MODEL=                # Override model for report
LLM_BASE_URL=                    # Custom base URL (e.g., Mammouth endpoint; omit for standard OpenAI)
LLM_API_KEY=                     # API key (for OpenAI-compatible providers)
ANTHROPIC_API_KEY=               # API key (for Anthropic provider)
EMBEDDING_PROVIDER=openai        # openai | ollama | vertexai
EMBEDDING_MODEL=text-embedding-ada-002
OLLAMA_BASE_URL=http://localhost:11434
VECTOR_INDEX_NAME=linkedin_content_index  # Default
```

For VertexAI provider:
```bash
GOOGLE_APPLICATION_CREDENTIALS=/path/to/credentials.json
VERTEX_PROJECT=your-gcp-project
VERTEX_LOCATION=[REDACTED] # pragma: allowlist secret
```

Data directory:
```bash
LINKEDIN_DATA_DIR=~/.linkedin_api/data  # Default; master CSV lives here
```

Report/prompt cache (Gradio pipeline):
```bash
REPORT_CACHE_MAX_ENTRIES=100  # Max cached reports and prompts; evict by hits (default 100)
```

Linked content fetch (fetch_linked_content, resolve_redirect):
```bash
REQUESTS_SSL_VERIFY=true  # Set to false to skip SSL verification for sites with cert issues (use with caution)
LINKEDIN_EXTRACTOR=tavily  # tavily (default) | httpx; falls back to httpx if TAVILY_API_KEY missing
TAVILY_API_KEY=          # Tavily Extract API key (prefer keyring; service name TAVILY_API_KEY)
TAVILY_EXTRACT_DEPTH=advanced  # basic|advanced — advanced handles JS-rendered/Cloudflare-gated pages
```

For deployment (Scalingo/cloud):
```bash
PORT=7860  # Overrides default Gradio port
```

## Token Management

LinkedIn access tokens expire every ~60 days. When you see:
```json
{"status":401,"serviceErrorCode":65602,"code":"EXPIRED_ACCESS_TOKEN","message":"The token used in the request has expired"}
```

Recreate token at https://www.linkedin.com/developers/tools/oauth and update via:
```bash
uv run python scripts/setup_token.py
```

## Important Notes

### LinkedIn Portability API
- Uses `r_dma_portability_self_serve` OAuth scope
- Subject to rate limits
- Only accesses user's own content (DMA-compliant)
- API reference: https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/

### Code Style
- Python 3.12+ required
- Black formatter (line length: 88)
- Flake8 linting (ignores E203, W503)
- Type hints preferred (mypy configured but non-blocking)
- Pre-commit hooks enforce formatting and conventional commits
- **Commits:** Use conventional commits with gitmoji (e.g. ✨ feat, 🐛 fix, ♻️ refactor). See https://gitmoji.dev/ and `.cursor/skills/github-use/SKILL.md`

### Module Design Principles
1. **Guard main execution**: All scripts use `if __name__ == "__main__":`
2. **Export reusable functions**: Make functions importable
3. **No code duplication**: Import from existing modules (see `gradio_app.py` ← `query_graphrag.py`)
4. **Configuration constants**: Export at module level for importers
5. **Clean entry points**: Define `main()` functions for CLI usage

### Neo4j Operations
- `build_graph.py` uses incremental MERGE by default; `--full-rebuild` clears first
- Batched operations (500 nodes/relationships per batch)
- Standard Cypher queries (no custom APOC procedures required for loading)
- Vector indexes require Neo4j 5.0+ with vector search support

### GraphRAG Limitations
- Content extraction from LinkedIn URLs requires public accessibility
- Content is primarily from the Portability API; URL fetch is only a fallback and may not work for all posts (basic HTML parsing)
- Embedding generation fails fast (no silent errors)
- Vector index dimensions must match embedding model (depends on provider)

## Deployment

The project supports Scalingo deployment via Gradio:
- `Procfile`: `web: uv run python -m linkedin_api.gradio_app`
- `runtime.txt`: Specifies Python 3.12
- See `SCALINGO_DEPLOYMENT.md` for detailed deployment instructions

## Testing Strategy

- Unit tests in `tests/` and `linkedin_api/tests/`
- Integration tests marked with `@pytest.mark.integration`
- Test configuration in `pyproject.toml`
- Coverage configured to omit test files and examples
- CI/CD via GitHub Actions (`.github/workflows/python-package.yml`)

## Vertex AI

Use `uv run python scripts/verify_vertex_ai.py` to smoke-test Vertex AI connectivity.
