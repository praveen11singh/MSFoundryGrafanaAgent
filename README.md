# MSFoundryGrafanaAgent

A Python sample that builds a **Microsoft Foundry agent** (using the `azure-ai-projects` Foundry Project SDK) with custom **function tools** for observability: Azure Managed Grafana dashboards/alerts, plus direct Azure Monitor Log Analytics and Application Insights queries and alerts. Authentication is Microsoft Entra ID throughout — no API keys or service account tokens.

## What it does

The agent has access to seven tools:

**Through Azure Managed Grafana:**
- `list_dashboards` — search dashboards by title
- `get_dashboard` — fetch a dashboard's JSON model by `uid`
- `query_datasource` — run a query (e.g. PromQL against Azure Monitor managed Prometheus) through Grafana's `/api/ds/query` proxy
- `list_grafana_alerts` — list Grafana's own alert rules and firing state

**Directly against Azure Monitor / Application Insights (bypasses Grafana):**
- `query_app_insights` — run a KQL query directly against the App Insights resource (requests, exceptions, traces, dependencies). **Preferred for App Insights questions** — resource-centric queries work regardless of whether the resource is workspace-based or classic, and avoid the `App`-prefixed table-naming quirk of workspace queries (e.g. `AppExceptions` vs. `exceptions`).
- `query_logs` — run a KQL query against a Log Analytics workspace directly, for non-App-Insights tables.
- `list_monitor_alerts` — list currently firing/resolved alerts across **both** Azure Monitor and Application Insights, via the Azure Alerts Management API.

At runtime, the agent decides which tool(s) to call, the script executes the matching Python function locally, and the results feed back into the model to produce a final answer. The `ask()` loop runs verbosely by default — it prints each tool call and result — which is useful for diagnosing auth/permission issues.

## Prerequisites

- Python 3.9+
- An Azure Managed Grafana workspace (with a datasource configured if you want to use `query_datasource`)
- An Application Insights resource
- A Microsoft Foundry project with a deployed chat model that supports tool calling (e.g. `gpt-4.1-mini`, `gpt-5-mini`)
- The identity you authenticate with (your own account via `az login`, or a managed identity/service principal) needs:
  - **Grafana Viewer** (or Editor/Admin) on the Azure Managed Grafana resource, under **Access control (IAM)**
  - **Reader** on the subscription (used to resolve the App Insights resource ID from its connection string via Azure Resource Graph)
  - **Monitoring Reader** (or higher) on the App Insights resource, for `query_app_insights` and `list_monitor_alerts`
  - **Log Analytics Reader** (or higher) on the Log Analytics workspace, for `query_logs`

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

3. Create a `.env` file in the project root (see `.env.example`):

   ```dotenv
   # Format: https://<resource_name>.ai.azure.com/api/projects/<project_name>
   FOUNDRY_PROJECT_ENDPOINT=your_foundry_project_endpoint

   # Name of a model deployment in your Foundry project (e.g. gpt-4.1-mini)
   FOUNDRY_MODEL_DEPLOYMENT=your_model_deployment_name

   # e.g. https://my-grafana-xxxx.wcus.grafana.azure.com
   AZURE_MANAGED_GRAFANA_ENDPOINT=your_grafana_endpoint

   # Workspace (customer) ID of your Log Analytics workspace
   LOG_ANALYTICS_WORKSPACE_ID=your_log_analytics_workspace_id

   # Subscription ID the Log Analytics workspace and App Insights resource live in
   AZURE_SUBSCRIPTION_ID=your_azure_subscription_id

   # Azure portal > your App Insights resource > Overview > Connection String
   APPINSIGHTS_CONNECTION_STRING=your_app_insights_connection_string
   ```

   > **Note:** don't commit a `.env` with real values to source control. Add `.env` to `.gitignore` and keep only `.env.example` (with placeholders) checked in.

4. Authenticate with Azure. For local development, the simplest option is:

   ```bash
   az login
   ```

   `DefaultAzureCredential` also supports managed identities and service principals (via `AZURE_CLIENT_ID` / `AZURE_CLIENT_SECRET` / `AZURE_TENANT_ID`) for non-interactive scenarios.

## Running

```bash
python msfoundry_azure_managed_grafana_agent.py
```

On startup, the script:
1. Parses `APPINSIGHTS_CONNECTION_STRING` and resolves the App Insights resource's full ARM ID via Azure Resource Graph (prints the resolved ID).
2. Creates a new agent version in your Foundry project.
3. Sends a sample question exercising both Grafana and direct Azure Monitor tools, printing each tool call/result as it happens, followed by the final answer.

To ask your own question, edit the call at the bottom of the script, or import `ask` from the module:

```python
from msfoundry_azure_managed_grafana_agent import ask

print(ask("Any exceptions in the last hour, and any dashboards I should check?"))
```

## Troubleshooting

- **`ItemNotFoundError` / "could not find a resource with that InstrumentationKey"** — double-check `APPINSIGHTS_CONNECTION_STRING` is the full string (not just a GUID), and that `AZURE_SUBSCRIPTION_ID` matches the subscription the App Insights resource actually lives in.
- **"table not found" errors from `query_logs`** — workspace-based Application Insights tables use an `App` prefix (`AppExceptions`, not `exceptions`). Prefer `query_app_insights` for App Insights data instead.
- **"Reached max_turns without a final answer"** — check the printed `[turn N] calling ...` / `-> ...` lines for a repeated error (commonly a missing RBAC role) that's causing the model to retry.
- **401/403 errors** — verify the RBAC roles listed in Prerequisites are assigned to the identity `DefaultAzureCredential` resolves to.

## Cleaning up

Agent versions persist in your Foundry project until deleted. Uncomment the cleanup line at the bottom of the script to delete the version after a run:

```python
project.agents.delete_version(agent_name=agent.name, agent_version=agent.version)
```

## Notes / possible extensions

- Azure Managed Grafana also ships a built-in MCP endpoint (`/api/azure-mcp`) that can be added as a catalog tool directly in the Foundry portal, as an alternative to hand-written function tools. This project uses the function-tool approach for full control over exactly which operations are exposed and how results are shaped.
- These are all read-only tools, so `require_approval`/manual approval isn't used; if you add tools that write or change data, consider gating them behind an approval step.
- A small standalone utility, `push_test_error.py` (not required to run the agent), can push a test exception to Application Insights and a test annotation to Grafana, useful for verifying `query_app_insights` and Grafana connectivity end to end.

## License

Free source
