# linkedin-portability

Python client for the **LinkedIn Member Data Portability API**: fetch your post-related activities, enrich a local content store, fetch linked URLs, and optionally summarize with an LLM.

## Features

- Fetch post-related activities (posts, comments, reactions, reposts) via the Portability API
- Append-only `activities.csv` cache under `~/.linkedin_api/data/`
- Content store (Markdown + metadata sidecars) for enrichment and summaries
- Fetch and cache linked article/page content
- Optional LLM summarization and Gradio UI for pipeline + activity reports

## Install

```bash
uv sync                    # core: fetch, CSV, content store, URL fetch
uv sync --extra llm        # + OpenAI/Mammouth, Anthropic, Ollama
uv sync --extra ui         # + Gradio app (includes llm extra)
```

**Package name:** PyPI distribution is **`linkedin-portability`** (import as `linkedin_api`).
Do not install PyPI **`linkedin-api-client`** — that is LinkedIn’s unrelated official SDK.

## Library API

```python
from linkedin_api import PipelineOptions, run_pipeline, parse_period
from linkedin_api import extract_activity_records, get_all_post_activities
from linkedin_api import load_content, load_metadata, resolve_redirect, strip_utm_params

opts = PipelineOptions(last="7d", from_cache=False)
activities, stats = run_pipeline(opts)
```

## CLI entry points

| Command | Description |
|---------|-------------|
| `linkedin-pipeline` | collect → enrich → fetch linked URLs → summarize |
| `linkedin-gradio` | Gradio UI (pipeline + activity report) |

## Data flow

```
LinkedIn API → activities.csv → pipeline (collect → enrich → fetch URLs → summarize)
                                      ↓
                              content_store/ + resources/
```

Downstream consumers (e.g. private vault export in amai-lab) should depend on the public `linkedin_api` API — not private `_` helpers.

## Setup (auth)

As a library dependency:

```bash
pip install "linkedin-portability @ git+https://github.com/annemariet/linkedin-portability-explorer.git"
```

### 2. Get LinkedIn Access Token

1. Go to [LinkedIn Developers](https://www.linkedin.com/developers/)
2. Create a new app
3. Get OAuth 2.0 access token with `r_dma_portability_self_serve` scope
4. Get token from: https://www.linkedin.com/developers/tools/oauth?clientId=78bwhum7gz6t9t

**Note:** Tokens expire every ~60 days. You'll need to regenerate them.

### 3. Configure Access Token

**Recommended: Store in Keychain (macOS)**

```bash
uv run python scripts/setup_token.py
```

This securely stores your token in macOS Keychain.

**Alternative: Environment Variable**

```bash
export LINKEDIN_ACCESS_TOKEN=your_access_token_here
```

### 4. Configure Neo4j

Set environment variables (or use defaults):

```bash
export NEO4J_URI=neo4j://localhost:7687
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=your_password
export NEO4J_DATABASE=neo4j
```

Or create a `.env` file:

```bash
NEO4J_URI=neo4j://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=your_password
NEO4J_DATABASE=neo4j
```

### 5. Optional: API-only mode (no URL fetching)

To use only Portability API and Neo4j data, without any HTTP requests to post URLs:

- **`USE_API_CONTENT_ONLY=1`** – Use only content from the Portability API/Neo4j for indexing and resource extraction. Never fetch post URLs for content.
- **`ENABLE_AUTHOR_ENRICHMENT=0`** – Disable author name/profile enrichment (no HTTP requests to LinkedIn post pages). Build graph and `enrich_profiles` will skip author fetch.

By default, content is taken from the API first; URL fetch is only used when content is missing (e.g. legacy data). Author enrichment is enabled by default.

## Building the Graph

### Step-by-Step Workflow

#### Step 1: Extract Graph Data

Fetch LinkedIn changelog data and extract entities/relationships:

```bash
uv run python -m linkedin_api.extract_graph_data
```

**What it does:**
- Fetches post-related activities from LinkedIn API (posts, comments, reactions)
- Extracts entities: Posts, People, Comments, Reactions
- Extracts relationships: CREATES, REPOSTS, REACTS_TO, COMMENTS_ON, etc.
- Saves to `neo4j_data_YYYYMMDD_HHMMSS.json`

**Output:** Master CSV (`~/.linkedin_api/data/activities.csv`) and optionally `neo4j_data_*.json`.

#### Step 2: Build Graph in Neo4j

Load from CSV (default) or JSON:

```bash
uv run python -m linkedin_api.build_graph
# or from legacy JSON:
uv run python -m linkedin_api.build_graph --json-file outputs/neo4j_data_*.json
```

**What it does:**
1. Loads nodes and relationships from JSON into Neo4j
2. Optionally enriches Post nodes with author information (name, profile URL); set `ENABLE_AUTHOR_ENRICHMENT=0` to skip
3. Extracts external resources (articles, videos, GitHub repos) from post/comment content (API content used first; set `USE_API_CONTENT_ONLY=1` to never fetch post URLs)
4. Creates Resource nodes and REFERENCES relationships

**Options:**
- Default: Incremental loading (merges new data with existing graph, preserves author info, resources)
- `--full-rebuild`: Deletes all data and recreates graph from scratch

**Example with incremental update (default):**

```bash
uv run python -m linkedin_api.build_graph
```

**Example with full rebuild:**

```bash
uv run python -m linkedin_api.build_graph --full-rebuild
```

#### Step 3: Index content (for GraphRAG)

```bash
uv run python -m linkedin_api.index_content
```

#### Step 4: Query the Graph

```bash
uv run python -m linkedin_api.query_graphrag
```

## Scripts Overview

### Main Scripts (CLI pipeline)

- **`extract_graph_data.py`** – Fetch and extract to CSV + JSON
- **`build_graph.py`** – Load CSV/JSON into Neo4j, enrich (author, resources)
- **`query_graphrag.py`** – GraphRAG semantic search
- **`index_content.py`** – Chunk and embed for GraphRAG (run before querying)
- **`enrich_graph.py`** – Optional LLM enrichment (Technology, Concept nodes)
- **`gradio_app.py`** – Web UI: Pipeline tab (report) + GraphRAG tab
- **`analyze_activity.py`** – Activity analysis (exploration)

### Utility Modules

- **`enrich_profiles.py`** - Extract author profiles from LinkedIn URLs
- **`extract_resources.py`** - Extract external resources from content
- **`utils/`** - Shared utilities (auth, changelog fetching, URN conversion, etc.)

## Usage Examples

### CLI: GraphRAG workflow

```bash
# 1. Extract data
uv run python -m linkedin_api.extract_graph_data

# 2. Build graph (from CSV by default)
uv run python -m linkedin_api.build_graph

# 3. Index content (required for GraphRAG)
uv run python -m linkedin_api.index_content

# 4. Query
uv run python -m linkedin_api.query_graphrag
```

### UI: Report workflow

```bash
uv run python -m linkedin_api.gradio_app
```

Then use the **Pipeline** tab: set period (e.g. 7d), click "Get latest news report". Uses `activities.csv` and `content_store`; no Neo4j. The **GraphRAG query** tab requires the CLI pipeline to have been run.

### Incremental graph update

```bash
uv run python -m linkedin_api.extract_graph_data
uv run python -m linkedin_api.build_graph   # merges by default
```

### Explore Activity Data

```bash
# Analyze all LinkedIn activity (not just posts)
uv run python -m linkedin_api.analyze_activity
```

### Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `setup_token.py` | Store LinkedIn token in keyring |
| `check_token.py` | Validate token without exposing it |
| `migrate_comment_urns.py` | Fix Comment URN format in Neo4j (`--dry-run` supported) |
| `fix_repost_authors.py` | Fix repost authors from re-extracted JSON |
| `verify_vertex_ai.py` | Smoke-test Vertex AI connectivity |
| `urn_to_url_example.py` | Example: URN to URL conversion |
| `validate_urn_urls.py` | Validate URN-to-URL conversions (HTTP) |

Run with `uv run python scripts/<name>.py`.

## Graph Schema

### Nodes

- **Post**: LinkedIn posts (original, reposts)
  - Properties: `urn`, `post_id`, `url`, `content`, `type`, `timestamp`
- **Person**: People (authors, actors)
  - Properties: `urn`, `person_id`, `name`, `profile_url`
- **Comment**: Comments on posts
  - Properties: `urn`, `text`, `timestamp`
- **Reaction**: Reactions/likes
  - Properties: `urn`, `reaction_type`, `timestamp`
- **Resource**: External resources (articles, videos, repos)
  - Properties: `url`, `type`, `title`

### Relationships

- `Person CREATES Post`
- `Person REPOSTS Post`
- `Person REACTS_TO Post`
- `Person CREATES Comment`
- `Comment COMMENTS_ON Post` (top-level)
- `Comment COMMENTS_ON Comment` (replies)
- `Post REFERENCES Resource`
- `Comment REFERENCES Resource`

## Validating the Graph

Use these Cypher queries in the AuraDB UI (or Neo4j Browser) to visualize and validate the graph structure. These queries return nodes and relationships that can be visualized in the graph view.

### Complete Subgraph: Posts with Authors, Resources, and Comments

```cypher
// Visualize a connected subgraph showing posts, authors, resources, and comments
MATCH (person:Person)-[creates:CREATES]->(post:Post)
OPTIONAL MATCH (post)-[ref:REFERENCES]->(resource:Resource)
OPTIONAL MATCH (comment:Comment)-[comments:COMMENTS_ON]->(post)
OPTIONAL MATCH (comment)-[commentRef:REFERENCES]->(resource)
WITH person, post, resource, comment, creates, ref, comments, commentRef
LIMIT 30
RETURN person, post, resource, comment, creates, ref, comments, commentRef
```

### Rich Post Subgraph: Author → Post → Resources → Comments

```cypher
// Show posts with all their connections: authors, resources, and comments
MATCH (person:Person)-[creates:CREATES]->(post:Post)-[ref:REFERENCES]->(resource:Resource)
OPTIONAL MATCH (comment:Comment)-[comments:COMMENTS_ON]->(post)
OPTIONAL MATCH (comment)-[commentRef:REFERENCES]->(resource)
WITH person, post, resource, comment, creates, ref, comments, commentRef
LIMIT 20
RETURN person, post, resource, comment, creates, ref, comments, commentRef
```

### Multi-Relationship Subgraph: All Connection Types

```cypher
// Visualize a subgraph with multiple relationship types
MATCH (person:Person)-[r1:CREATES|REPOSTS]->(post:Post)
OPTIONAL MATCH (post)-[r2:REFERENCES]->(resource:Resource)
OPTIONAL MATCH (comment:Comment)-[r3:COMMENTS_ON]->(post)
OPTIONAL MATCH (person2:Person)-[r4:CREATES]->(comment)
OPTIONAL MATCH (comment)-[r5:REFERENCES]->(resource)
WITH person, post, resource, comment, person2, r1, r2, r3, r4, r5
LIMIT 15
RETURN person, post, resource, comment, person2, r1, r2, r3, r4, r5
```

### Posts with Resources and Comment Threads

```cypher
// Show posts that have both resources and comment threads
MATCH (post:Post)-[ref:REFERENCES]->(resource:Resource)
OPTIONAL MATCH (comment:Comment)-[comments:COMMENTS_ON]->(post)
OPTIONAL MATCH (reply:Comment)-[replies:COMMENTS_ON]->(comment)
OPTIONAL MATCH (person:Person)-[creates:CREATES]->(post)
OPTIONAL MATCH (person2:Person)-[createsComment:CREATES]->(comment)
WITH post, resource, comment, reply, person, person2, ref, comments, replies, creates, createsComment
LIMIT 15
RETURN post, resource, comment, reply, person, person2, ref, comments, replies, creates, createsComment
```

### Most Connected Subgraph: Posts with Multiple Resources

```cypher
// Find posts that reference multiple resources and show their full context
MATCH (post:Post)-[ref:REFERENCES]->(resource:Resource)
WITH post, collect({resource: resource, ref: ref}) as resourceData
WHERE size(resourceData) > 1
WITH post, resourceData[0..3] as topResources
UNWIND topResources as rd
OPTIONAL MATCH (person:Person)-[creates:CREATES]->(post)
OPTIONAL MATCH (comment:Comment)-[comments:COMMENTS_ON]->(post)
WITH post, rd.resource as resource, rd.ref as ref, person, comment, creates, comments
LIMIT 12
RETURN post, resource, person, comment, ref, creates, comments
```

## Troubleshooting

### Token Expired

If you see `401 Unauthorized` or `EXPIRED_ACCESS_TOKEN`:

1. Get a new token from: https://www.linkedin.com/developers/tools/oauth?clientId=78bwhum7gz6t9t
2. Update it: `uv run python scripts/setup_token.py`

### Neo4j Connection Issues

```bash
# Check LinkedIn token
uv run python scripts/check_token.py

# Verify Neo4j is running
# Check NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD environment variables
```

### Rate Limiting

LinkedIn API has rate limits. If you hit them:
- Wait a few minutes and retry
- The scripts handle pagination automatically

## API References

- [LinkedIn Member Data Portability API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-changelog-api?view=li-dma-data-portability-2025-11&tabs=http)
- [LinkedIn DMA Portability API Terms](https://www.linkedin.com/legal/l/portability-api-terms)

## Development Notes

### Changelog Data Structure

Each element from the API contains:
- `resourceName`: Type of resource (e.g., "ugcPosts", "socialActions/likes")
- `methodName`: Action (e.g., "CREATE")
- `activity`: Detailed activity data
- `actor`: Person who performed the action

### Resource Types

- `ugcPosts` / `ugcPost`: Posts
- `socialActions/likes`: Reactions
- `socialActions/comments`: Comments
- `messages`: DMs (not imported to graph)
- `invitations`: Connection invites (not imported to graph)
