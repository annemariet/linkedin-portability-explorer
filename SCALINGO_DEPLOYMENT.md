# Deploying LinkedIn GraphRAG to Scalingo

## Overview

This application is now deployable to Scalingo as a web application with a Gradio UI.

## Prerequisites

- Scalingo account
- Scalingo CLI installed
- Neo4j database (accessible from Scalingo)
- Google Cloud project with Vertex AI enabled
- Indexed LinkedIn content (run `index_content.py` first)

## Files for Deployment

- `Procfile` - Defines how Scalingo starts the web app
- `requirements.txt` - Python dependencies
- `runtime.txt` - Python version
- `linkedin_api/gradio_app.py` - Gradio web interface

## Deployment Steps

### 1. Create Scalingo App

```bash
cd ~/temp-linkedin-repo
scalingo create my-linkedin-graphrag
```

### 2. Configure Environment Variables

Set these on Scalingo (via dashboard or CLI):

```bash
# Neo4j Configuration
scalingo --app my-linkedin-graphrag env-set NEO4J_URI="neo4j://your-host:7687"
scalingo --app my-linkedin-graphrag env-set NEO4J_USERNAME="neo4j"
scalingo --app my-linkedin-graphrag env-set NEO4J_PASSWORD="your-password"
scalingo --app my-linkedin-graphrag env-set NEO4J_DATABASE="neo4j"

# Vertex AI Configuration
scalingo --app my-linkedin-graphrag env-set EMBEDDING_MODEL="textembedding-gecko@002"
scalingo --app my-linkedin-graphrag env-set LLM_MODEL="gemini-1.5-pro"
scalingo --app my-linkedin-graphrag env-set VECTOR_INDEX_NAME="linkedin_content_index"
scalingo --app my-linkedin-graphrag env-set VERTEX_PROJECT="your-gcp-project-id"
scalingo --app my-linkedin-graphrag env-set VERTEX_LOCATION="europe-west9"

# Google Cloud credentials
# Option 1: Service Account Key (simpler, but not recommended by GCP)
scalingo --app my-linkedin-graphrag env-set GOOGLE_APPLICATION_CREDENTIALS_JSON="$(cat path/to/your-service-account-key.json)"

# Option 2: Workload Identity Federation (recommended, but requires setup)
# If you've configured Workload Identity Federation, set the credential config file path:
# scalingo --app my-linkedin-graphrag env-set GOOGLE_APPLICATION_CREDENTIALS="/path/to/workload-identity-credential-config.json"
```

**Important**: 
- The Gradio app automatically writes JSON credentials from `GOOGLE_APPLICATION_CREDENTIALS_JSON` to a secure temp file at runtime.
- **File Security**: The credentials file is created with `0600` permissions (read/write for owner only, no access for group/others) using `tempfile.mkstemp()` to prevent unauthorized access.
- **Security Note**: Google Cloud recommends avoiding service account keys when possible. For Scalingo deployments, service account keys are a necessary compromise unless you set up Workload Identity Federation (which requires Scalingo to support OIDC). Consider using Google Cloud Run instead, which supports attached service accounts without keys.

### 3. Choose Deployment Method

#### Option A: With uv (Development Consistency)
Keep the default Procfile:
```
web: uv run python -m linkedin_api.gradio_app
```

**Pros:** Uses same tool as local development
**Cons:** Requires `uv` to be available in Scalingo environment

#### Option B: Standard Python (Recommended for Scalingo)
Use the alternative Procfile:
```bash
cp Procfile.standard Procfile
git add Procfile
git commit -m "Use standard Python for Scalingo"
```

**Pros:** Works out-of-the-box with Scalingo's Python buildpack
**Cons:** Relies on `requirements.txt` instead of `uv.lock`

### 4. Deploy

```bash
git push scalingo main
```

### 5. Scale the App

```bash
# Start with 1 container (M size recommended for AI workloads)
scalingo --app my-linkedin-graphrag scale web:1:M
```

### 6. Open Your App

```bash
scalingo --app my-linkedin-graphrag open
```

## Important Considerations

### Neo4j Connectivity

- Ensure your Neo4j instance is accessible from Scalingo's network
- Consider using Neo4j AuraDB for cloud-hosted Neo4j
- Configure firewall rules if using self-hosted Neo4j

### Vertex AI Authentication

The app supports two authentication methods:

**Option 1: Service Account Key (Current Implementation)**
- Set `GOOGLE_APPLICATION_CREDENTIALS_JSON` with your service account JSON content
- App writes it to a temp file at startup
- **Note**: Google Cloud recommends avoiding service account keys when possible

**Option 2: Workload Identity Federation (Recommended)**
- Configure Workload Identity Federation in Google Cloud
- Set `GOOGLE_APPLICATION_CREDENTIALS` to point to the credential configuration file
- Requires Scalingo to support OIDC (may not be available)
- See: https://cloud.google.com/iam/docs/workload-identity-federation

**Alternative: Use Google Cloud Run**
- Cloud Run supports attached service accounts (no keys needed)
- Better security posture than service account keys
- Consider migrating if security is a priority


## Process Types

- `web` - Gradio UI (must be scaled to at least 1)
- You can add `worker` or other process types in the Procfile if needed

**Note:** The project uses `uv` for dependency management. If Scalingo doesn't have `uv` pre-installed, use the standard Procfile (Option B above).

## Cost Considerations

- Scalingo charges per container size and uptime
- Vertex AI charges per API call (embeddings + LLM)
- Neo4j charges depend on your hosting choice
- Start with 1 M container and scale based on usage

## Testing Locally

Before deploying, test the Gradio app locally:

```bash
# In temp-linkedin-repo
uv run python -m linkedin_api.gradio_app
```

Visit `http://localhost:7860` to test the interface.

## Monitoring

- Use Scalingo dashboard to monitor logs: `scalingo --app my-linkedin-graphrag logs -f`
- Check app metrics in Scalingo dashboard
- Monitor Vertex AI usage in Google Cloud Console

## Troubleshooting

### App Won't Start

Check logs:
```bash
scalingo --app my-linkedin-graphrag logs --lines 100
```

Common issues:
- Missing environment variables
- Neo4j connection failure
- Vertex AI authentication issues
- Missing vector index (run `index_content.py` first)

### Port Binding

The app automatically uses `$PORT` environment variable (set by Scalingo). No manual configuration needed.

### Memory Issues

If the app crashes with memory errors, scale to a larger container:
```bash
scalingo --app my-linkedin-graphrag scale web:1:L
```

## Alternative: Worker-Only Deployment

If you don't want the web UI, you can deploy as a worker:

```procfile
worker: uv run python -m linkedin_api.query_graphrag
```

Then scale:
```bash
scalingo --app my-linkedin-graphrag scale web:0
scalingo --app my-linkedin-graphrag scale worker:1:M
```

However, this requires modifying `query_graphrag.py` to run continuously (e.g., polling a queue).

## Deployment Options Summary

### Option 1: With uv (Recommended for Consistency)
Keep the Procfile as is: `web: uv run python -m linkedin_api.gradio_app`

**Pros:** Uses same tool as development
**Cons:** Requires `uv` in Scalingo environment

### Option 2: Standard Python (Easier Deployment)
Change Procfile to: `web: python -m linkedin_api.gradio_app`

**Pros:** Works out-of-the-box with Scalingo's Python buildpack
**Cons:** Relies on `requirements.txt` instead of `uv.lock`

For simplicity on Scalingo, **Option 2 is recommended** unless you need exact `uv.lock` dependency resolution.
