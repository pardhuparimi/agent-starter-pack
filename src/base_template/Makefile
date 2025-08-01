# Install dependencies using uv package manager
install:
	@command -v uv >/dev/null 2>&1 || { echo "uv is not installed. Installing uv..."; curl -LsSf https://astral.sh/uv/0.6.12/install.sh | sh; source $HOME/.local/bin/env; }
{%- if cookiecutter.settings.get("commands", {}).get("override", {}).get("install") %} 
	{{cookiecutter.settings.get("commands", {}).get("override", {}).get("install")}}
{%- else %}
	uv sync --dev{% if cookiecutter.agent_name != 'live_api' and "adk" not in cookiecutter.tags %} --extra streamlit{%- endif %} --extra jupyter{% if cookiecutter.agent_name == 'live_api' %} && (cd frontend && npm install){%- endif %}
{%- endif %}

{%- if cookiecutter.settings.get("commands", {}).get("extra", {}) %}
{%- for cmd_name, cmd_value in cookiecutter.settings.get("commands", {}).get("extra", {}).items() %}

# {{ cmd_value.get("description") }}
{{ cmd_name }}:
{%- if cmd_value is mapping %}
{%- if cmd_value.command is mapping and cookiecutter.deployment_target in cmd_value.command %}
	{{ cmd_value.command[cookiecutter.deployment_target] }}
{%- else %}
	{{ cmd_value.command if cmd_value.command is string else "" }}
{%- endif %}
{%- else %}
	{{ cmd_value }}
{%- endif %}
{%- endfor %}{%- endif %}

# Launch local dev playground
playground:
{%- if cookiecutter.settings.get("commands", {}).get("override", {}).get("playground") %}
	{{cookiecutter.settings.get("commands", {}).get("override", {}).get("playground")}}
{%- else %}
	@echo "==============================================================================="
	@echo "| 🚀 Starting your agent playground...                                        |"
	@echo "|                                                                             |"
	@echo "| 💡 Try asking: {{cookiecutter.example_question}}|"
{%- if "adk" in cookiecutter.tags %}
	@echo "|                                                                             |"
	@echo "| 🔍 IMPORTANT: Select the 'app' folder to interact with your agent.          |"
{%- endif %}
	@echo "==============================================================================="
{%- if "adk" in cookiecutter.tags %}
	uv run adk web --port 8501
{%- else %}
{%- if cookiecutter.deployment_target == 'cloud_run' %}
	uv run uvicorn app.server:app --host 0.0.0.0 --port 8000 --reload &
{%- endif %}
{%- if cookiecutter.agent_name == 'live_api' %}
	(cd frontend && PORT=8501 npm start)
{%- else %}
	{% if cookiecutter.deployment_target == 'agent_engine' %}PYTHONPATH=. {% endif %}uv run streamlit run frontend/streamlit_app.py --browser.serverAddress=localhost --server.enableCORS=false --server.enableXsrfProtection=false
{%- endif %}
{%- endif %}
{%- endif %}

# Deploy the agent remotely
{%- if cookiecutter.deployment_target == 'cloud_run' %}
# Usage: make backend [IAP=true] [PORT=8080] - Set IAP=true to enable Identity-Aware Proxy, PORT to specify container port
{%- endif %}
backend:
{%- if cookiecutter.deployment_target == 'cloud_run' %}
	PROJECT_ID=$$(gcloud config get-value project) && \
	gcloud beta run deploy {{cookiecutter.project_name}} \
		--source . \
		--memory "4Gi" \
		--project $$PROJECT_ID \
		--region "us-central1" \
		--no-allow-unauthenticated \
		--no-cpu-throttling \
		--labels "created-by=adk" \
		--set-env-vars \
		"COMMIT_SHA=$(shell git rev-parse HEAD){%- if cookiecutter.data_ingestion %}{%- if cookiecutter.datastore_type == "vertex_ai_search" %},DATA_STORE_ID={{cookiecutter.project_name}}-datastore,DATA_STORE_REGION=us{%- elif cookiecutter.datastore_type == "vertex_ai_vector_search" %},VECTOR_SEARCH_INDEX={{cookiecutter.project_name}}-vector-search,VECTOR_SEARCH_INDEX_ENDPOINT={{cookiecutter.project_name}}-vector-search-endpoint,VECTOR_SEARCH_BUCKET=$$PROJECT_ID-{{cookiecutter.project_name}}-vs{%- endif %}{%- endif %}" \
		$(if $(IAP),--iap) \
		$(if $(PORT),--port=$(PORT))
{%- elif cookiecutter.deployment_target == 'agent_engine' %}
	# Export dependencies to requirements file using uv export.
	uv export --no-hashes --no-header --no-dev --no-emit-project --no-annotate > .requirements.txt 2>/dev/null || \
	uv export --no-hashes --no-header --no-dev --no-emit-project > .requirements.txt && uv run app/agent_engine_app.py
{%- endif %}
{%- if cookiecutter.deployment_target == 'cloud_run' %}

# Launch local development server with hot-reload
local-backend:
	uv run uvicorn app.server:app --host 0.0.0.0 --port 8000 --reload
{%- endif %}
{%- if cookiecutter.deployment_target == 'cloud_run' %}
{%- if cookiecutter.agent_name == 'live_api' %}

# Start the frontend UI for development
ui:
	(cd frontend && PORT=8501 npm start)
{%- endif %}
{%- endif %}

# Set up development environment resources using Terraform
setup-dev-env:
	PROJECT_ID=$$(gcloud config get-value project) && \
	(cd deployment/terraform/dev && terraform init && terraform apply --var-file vars/env.tfvars --var dev_project_id=$$PROJECT_ID --auto-approve)

{%- if cookiecutter.data_ingestion %}

# Run the data ingestion pipeline for RAG capabilities
data-ingestion:
	PROJECT_ID=$$(gcloud config get-value project) && \
	(cd data_ingestion && uv run data_ingestion_pipeline/submit_pipeline.py \
		--project-id=$$PROJECT_ID \
		--region="us-central1" \
{%- if cookiecutter.datastore_type == "vertex_ai_search" %}
		--data-store-id="{{cookiecutter.project_name}}-datastore" \
		--data-store-region="us" \
{%- elif cookiecutter.datastore_type == "vertex_ai_vector_search" %}
		--vector-search-index="{{cookiecutter.project_name}}-vector-search" \
		--vector-search-index-endpoint="{{cookiecutter.project_name}}-vector-search-endpoint" \
		--vector-search-data-bucket-name="$$PROJECT_ID-{{cookiecutter.project_name}}-vs" \
{%- endif %}
		--service-account="{{cookiecutter.project_name}}-rag@$$PROJECT_ID.iam.gserviceaccount.com" \
		--pipeline-root="gs://$$PROJECT_ID-{{cookiecutter.project_name}}-rag" \
		--pipeline-name="data-ingestion-pipeline")
{%- endif %}

# Run unit and integration tests
test:
	uv run pytest tests/unit && uv run pytest tests/integration

# Run code quality checks (codespell, ruff, mypy)
lint:
	uv run codespell
	uv run ruff check . --diff
	uv run ruff format . --check --diff
	uv run mypy .
