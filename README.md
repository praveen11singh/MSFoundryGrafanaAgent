# MSFoundryGrafanaAgent

A Python sample that builds a **Microsoft Foundry agent** (using the `azure-ai-projects` Foundry Project SDK) with custom **function tools** that query an **Azure Managed Grafana** workspace — dashboards, datasource queries, and alerts — all authenticated with **Microsoft Entra ID** (no API keys or service account tokens required).

## How it works

1. The script defines four Python functions that call Azure Managed Grafana's data-plane REST API directly:
   - `list_dashboards` — search dashboards by title
   - `get_dashboard` — fetch a dashboard's JSON model by `uid`
   - `query_datasource` — run a PromQL/LogQL query through Grafana's `/api/ds/query` proxy
   - `list_alerts` — list current alert rules and firing state
2. Each function is registered as a `FunctionTool` on a Foundry `PromptAgentDefinition`, created via `AIProjectClient` (the Foundry Project SDK) — not the raw OpenAI SDK.
3. At runtime, the agent decides when to call a tool. The script executes the matching Python function locally, sends the result back as a `function_call_output`, and the model produces a final natural-language answer.
4. Authentication is Entra ID throughout:
   - To Foundry: `DefaultAzureCredential`
   - To Grafana: an Entra ID access token requested for Azure Managed Grafana's fixed data-plane application ID, `6f2d169c-08f3-4a4c-a982-bcaf2d038c45`

## Prerequisites

- Python 3.9+
- An Azure Managed Grafana workspace, with at least one configured datasource (e.g. Azure Monitor managed Prometheus) if you want to use `query_datasource`
- A Microsoft Foundry project with a deployed chat model that supports tool calling (e.g. `gpt-4.1-mini`, `gpt-5-mini`)
- The identity you authenticate with (your own account via `az login`, or a managed identity/service principal) must have one of these roles on the Azure Managed Grafana resource, assigned under **Access control (IAM)**:
  - Grafana Viewer
  - Grafana Editor
  - Grafana Admin

## Setup

1. Clone the repo and create a virtual environment:

   ```bash
   git clone https://github.com/praveen11singh/MSFoundryGrafanaAgent.git
   cd MSFoundryGrafanaAgent
   python -m venv .venv
   source .venv/bin/activate      # Windows: .venv\Scripts\Activate.ps1
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Create a `.env` file in the project root:

   ```dotenv
   # Format: https://<resource_name>.ai.azure.com/api/projects/<project_name>
   FOUNDRY_PROJECT_ENDPOINT=your_foundry_project_endpoint

   # Name of a model deployment in your Foundry project (e.g. gpt-4.1-mini)
   FOUNDRY_MODEL_DEPLOYMENT=your_model_deployment_name

   # e.g. https://my-grafana-xxxx.wcus.grafana.azure.com
   AZURE_MANAGED_GRAFANA_ENDPOINT=your_grafana_endpoint
   ```

4. Authenticate with Azure. For local development, the simplest option is:

   ```bash
   az login
   ```

   `DefaultAzureCredential` also supports managed identities and service principals (via `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID`) for non-interactive scenarios.

## Running

```bash
python msfoundry_azure_managed_grafana_agent.py
```

This creates a new agent version in your Foundry project and sends a sample question ("Are there any firing alerts right now, and which dashboards should I check first?"). The agent's tool calls and final answer print to the console.

To ask your own question, edit the call to `ask(...)` at the bottom of the script, or import `ask` from the module in your own code:

```python
from msfoundry_azure_managed_grafana_agent import ask

print(ask("List all dashboards with 'production' in the title."))
```

## Cleaning up

Agent versions persist in your Foundry project until deleted. Uncomment the cleanup line at the bottom of the script to delete the version after a run:

```python
project.agents.delete_version(agent_name=agent.name, agent_version=agent.version)
```

## Notes / possible extensions

- `require_approval` isn't used here since these are read-only tools; if you add tools that write or change data, consider gating them behind a manual approval step.
- Azure Managed Grafana also ships a built-in MCP endpoint (`/api/azure-mcp`) that can be added as a catalog tool directly in the Foundry portal, as an alternative to hand-written function tools. This project uses the function-tool approach for full control over exactly which Grafana operations are exposed and how results are shaped.
- `range_minutes` in `query_datasource` defaults to 60; adjust per-call as needed for longer lookback windows.

## License

N/A
