"""
Azure AI Foundry Agent + Azure Managed Grafana (custom function tools)
--------------------------------------------------------------------------
Builds an Azure AI Foundry agent using the azure-ai-projects SDK
(PromptAgentDefinition + FunctionTool), where the "tools" are custom Python
functions that call Azure Managed Grafana's data-plane HTTP API directly.
Authentication to Grafana is via Microsoft Entra ID (no API key / service
account token needed).

This does NOT use the OpenAI SDK directly, and does NOT use the prebuilt
Azure Managed Grafana MCP catalog tool - it's a Foundry agent with your own
function tools, same shape as function-calling samples in the Foundry docs.

Capabilities exposed to the model:
  - list_dashboards        : search/list Grafana dashboards
  - get_dashboard           : fetch a dashboard's JSON model by uid
  - query_datasource        : run a query (e.g. PromQL against Azure Monitor
                              managed Prometheus) through Grafana's proxy
  - list_grafana_alerts     : fetch alert state from Grafana's alertmanager
  - query_logs              : run a KQL query against a Log Analytics
                              workspace directly (general Log Analytics
                              tables; use query_app_insights instead for
                              App Insights data - see note below)
  - query_app_insights      : run a KQL query directly against the App
                              Insights resource (resource-centric query),
                              for requests/exceptions/traces/dependencies.
                              Preferred over query_logs for App Insights
                              questions since it works whether the resource
                              is workspace-based or classic, and sidesteps
                              the "App"-prefixed table naming that
                              workspace-based queries require (e.g.
                              AppExceptions vs. exceptions)
  - list_monitor_alerts     : list currently firing/resolved alerts across
                              Azure Monitor AND Application Insights via the
                              Azure Alerts Management API (direct API call,
                              bypasses Grafana)

So this agent reaches your observability data two ways:
  - Through Grafana, for anything already wired up as a Grafana datasource
    or dashboard (query_datasource, list_dashboards, get_dashboard,
    list_grafana_alerts).
  - Directly against Azure Monitor / Log Analytics / Alerts Management APIs
    for deeper log queries and a unified alert view (query_logs,
    list_monitor_alerts) that doesn't depend on Grafana being configured
    for that resource.

Requirements:
    pip install azure-ai-projects azure-identity azure-monitor-query requests python-dotenv

Authentication:
    - To Foundry: DefaultAzureCredential (az login / managed identity / SP env vars)
    - To Grafana: DefaultAzureCredential, requesting a token for the fixed
      Azure Managed Grafana data-plane app ID: 6f2d169c-08f3-4a4c-a982-bcaf2d038c45
    - To Log Analytics (query_logs): the azure-monitor-query SDK's
      LogsQueryClient, using DefaultAzureCredential
    - To Alerts Management (list_monitor_alerts): DefaultAzureCredential
      requesting a token scoped to https://management.azure.com/.default

    IMPORTANT: the identity used needs, in addition to the Grafana role
    described below:
      - "Log Analytics Reader" (or higher) on the Log Analytics workspace,
        for query_logs
      - "Monitoring Reader" (or higher) on the subscription, for
        list_monitor_alerts

    The identity used to call Grafana (typically the same principal running
    this script, or the Foundry project's managed identity if you run this
    server-side) needs an RBAC role on the Azure Managed Grafana resource -
    Grafana Viewer, Grafana Editor, or Grafana Admin - assigned via Access
    control (IAM) on the resource.

Environment variables required (loaded from a .env file in the same folder,
see .env.example):
    FOUNDRY_PROJECT_ENDPOINT        e.g. https://<resource>.ai.azure.com/api/projects/<project>
    FOUNDRY_MODEL_DEPLOYMENT        e.g. gpt-4.1-mini (a model deployment in your project)
    AZURE_MANAGED_GRAFANA_ENDPOINT  e.g. https://my-grafana-xxxx.wcus.grafana.azure.com
    LOG_ANALYTICS_WORKSPACE_ID      the workspace (customer) ID of your Log
                                     Analytics workspace (the one your App
                                     Insights resource(s) send data to)
    AZURE_SUBSCRIPTION_ID           subscription ID that both the Log
                                     Analytics workspace and the App
                                     Insights resource live in (used for
                                     list_monitor_alerts and to resolve
                                     APPINSIGHTS_CONNECTION_STRING to a
                                     resource ID)
    APPINSIGHTS_CONNECTION_STRING   the App Insights connection string
                                     (Azure portal > your App Insights
                                     resource > Overview > Connection
                                     String). Its InstrumentationKey is used
                                     to look up the resource's full ARM ID
                                     via Azure Resource Graph at startup, so
                                     you don't have to hand-build that path
                                     yourself. The identity used needs at
                                     least "Reader" on the subscription (for
                                     the Resource Graph lookup) and
                                     "Monitoring Reader" (or higher) on the
                                     App Insights resource itself (for
                                     query_app_insights).
"""

import os
import json
import time
from datetime import timedelta
import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import PromptAgentDefinition, Tool, FunctionTool
from azure.monitor.query import LogsQueryClient, LogsQueryStatus
from openai.types.responses.response_input_param import FunctionCallOutput, ResponseInputParam

# Load variables from a .env file (if present) into the environment
load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
MODEL_DEPLOYMENT = os.environ["FOUNDRY_MODEL_DEPLOYMENT"]
GRAFANA_ENDPOINT = os.environ["AZURE_MANAGED_GRAFANA_ENDPOINT"].rstrip("/")
LOG_ANALYTICS_WORKSPACE_ID = os.environ["LOG_ANALYTICS_WORKSPACE_ID"]
AZURE_SUBSCRIPTION_ID = os.environ["AZURE_SUBSCRIPTION_ID"]
# Application Insights connection string, e.g.:
# InstrumentationKey=xxxx;IngestionEndpoint=https://...;ApplicationId=xxxx
APPINSIGHTS_CONNECTION_STRING = os.environ["APPINSIGHTS_CONNECTION_STRING"]


def _parse_connection_string(connection_string: str) -> dict:
    """Parse an App Insights connection string ('key1=value1;key2=value2;...') into a dict."""
    parts = {}
    for segment in connection_string.split(";"):
        segment = segment.strip()
        if not segment or "=" not in segment:
            continue
        key, _, value = segment.partition("=")
        parts[key.strip()] = value.strip()
    return parts


def _resolve_appinsights_resource_id(instrumentation_key: str) -> str:
    """
    Look up the full ARM resource ID of the App Insights component whose
    InstrumentationKey matches the one in the connection string, using the
    Azure Resource Graph API (a single Entra ID-authenticated REST call -
    no extra SDK package needed).
    """
    query = (
        "resources "
        "| where type =~ 'microsoft.insights/components' "
        f"| where tostring(properties.InstrumentationKey) == '{instrumentation_key}' "
        "| project id"
    )
    resp = requests.post(
        "https://management.azure.com/providers/Microsoft.ResourceGraph/resources",
        headers={
            "Authorization": f"Bearer {credential.get_token(ARM_SCOPE).token}",
            "Content-Type": "application/json",
        },
        params={"api-version": "2021-03-01"},
        json={"subscriptions": [AZURE_SUBSCRIPTION_ID], "query": query},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json().get("data", [])

    if not rows:
        raise ValueError(
            "Could not find an Application Insights resource in subscription "
            f"{AZURE_SUBSCRIPTION_ID} with InstrumentationKey "
            f"{instrumentation_key}. Check that APPINSIGHTS_CONNECTION_STRING "
            "is correct and that AZURE_SUBSCRIPTION_ID matches the "
            "subscription the App Insights resource actually lives in, and "
            "that the identity running this script has at least Reader on "
            "that subscription/resource."
        )
    if len(rows) > 1:
        raise ValueError(
            "Found multiple Application Insights resources with the same "
            f"InstrumentationKey ({instrumentation_key}) - this shouldn't "
            "normally happen. Resource IDs found: "
            + ", ".join(r["id"] for r in rows)
        )
    return rows[0]["id"]

# Fixed Azure AD application (resource) ID for Azure Managed Grafana's data plane
GRAFANA_AAD_SCOPE = "6f2d169c-08f3-4a4c-a982-bcaf2d038c45/.default"
# Azure Resource Manager scope, used for the Alerts Management API
ARM_SCOPE = "https://management.azure.com/.default"

credential = DefaultAzureCredential()
logs_client = LogsQueryClient(credential)

# Resolve the App Insights ARM resource ID once at startup from the
# connection string's InstrumentationKey, via Azure Resource Graph.
_appinsights_ikey = _parse_connection_string(APPINSIGHTS_CONNECTION_STRING).get("InstrumentationKey")
if not _appinsights_ikey:
    raise ValueError(
        "Could not find 'InstrumentationKey' in APPINSIGHTS_CONNECTION_STRING. "
        "Make sure you copied the full connection string (Azure portal > your "
        "App Insights resource > Overview > Connection String), not just a "
        "single value."
    )

APPINSIGHTS_RESOURCE_ID = _resolve_appinsights_resource_id(_appinsights_ikey)
print(f"Resolved Application Insights resource: {APPINSIGHTS_RESOURCE_ID}")

# ---------------------------------------------------------------------------
# Foundry project client
# ---------------------------------------------------------------------------

project = AIProjectClient(
    endpoint=PROJECT_ENDPOINT,
    credential=credential,
)
openai = project.get_openai_client()

# ---------------------------------------------------------------------------
# Grafana token caching helper
# ---------------------------------------------------------------------------

_token_cache = {"token": None, "expires_on": 0}


def _get_grafana_token() -> str:
    """Return a cached Entra ID access token for Azure Managed Grafana, refreshing when needed."""
    now = time.time()
    if _token_cache["token"] is None or now > _token_cache["expires_on"] - 60:
        token = credential.get_token(GRAFANA_AAD_SCOPE)
        _token_cache["token"] = token.token
        _token_cache["expires_on"] = token.expires_on
    return _token_cache["token"]


def _grafana_headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_grafana_token()}",
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Grafana API helper functions (the actual local functions the agent calls)
# ---------------------------------------------------------------------------


def list_dashboards(query: str = "") -> str:
    """Search Azure Managed Grafana dashboards by title."""
    resp = requests.get(
        f"{GRAFANA_ENDPOINT}/api/search",
        headers=_grafana_headers(),
        params={"query": query, "type": "dash-db"},
        timeout=30,
    )
    resp.raise_for_status()
    results = [
        {"title": d["title"], "uid": d["uid"], "url": d["url"]}
        for d in resp.json()
    ]
    return json.dumps(results)


def get_dashboard(uid: str) -> str:
    """Fetch a dashboard's JSON model by its uid."""
    resp = requests.get(
        f"{GRAFANA_ENDPOINT}/api/dashboards/uid/{uid}",
        headers=_grafana_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    return json.dumps(resp.json())


def query_datasource(datasource_uid: str, expr: str, range_minutes: int = 60) -> str:
    """
    Run a query against a datasource through Grafana's unified query endpoint.
    Works with e.g. Azure Monitor managed Prometheus (PromQL) or Loki (LogQL).
    """
    now_ms = int(time.time() * 1000)
    from_ms = now_ms - range_minutes * 60 * 1000

    payload = {
        "queries": [
            {
                "refId": "A",
                "datasource": {"uid": datasource_uid},
                "expr": expr,
                "instant": False,
            }
        ],
        "from": str(from_ms),
        "to": str(now_ms),
    }
    resp = requests.post(
        f"{GRAFANA_ENDPOINT}/api/ds/query",
        headers=_grafana_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    return json.dumps(resp.json())


def list_grafana_alerts() -> str:
    """Fetch current alert rules and their state from Grafana's alertmanager."""
    resp = requests.get(
        f"{GRAFANA_ENDPOINT}/api/alertmanager/grafana/api/v2/alerts",
        headers=_grafana_headers(),
        timeout=30,
    )
    resp.raise_for_status()
    alerts = [
        {
            "name": a.get("labels", {}).get("alertname"),
            "state": a.get("status", {}).get("state"),
            "startsAt": a.get("startsAt"),
        }
        for a in resp.json()
    ]
    return json.dumps(alerts)


# ---------------------------------------------------------------------------
# Direct Azure Monitor / Application Insights functions (bypass Grafana)
# ---------------------------------------------------------------------------


def _tables_to_rows(response) -> tuple:
    """Convert a LogsQueryResult/PartialResult into a flat list of row dicts."""
    if response.status == LogsQueryStatus.PARTIAL:
        tables = response.partial_data
        error = str(response.partial_error)
    elif response.status == LogsQueryStatus.SUCCESS:
        tables = response.tables
        error = None
    else:
        return None, f"Unexpected query status: {response.status}"

    rows = []
    for table in tables:
        for row in table.rows:
            rows.append(dict(zip(table.columns, row)))
    return rows, error


def query_logs(kql: str, timespan_hours: int = 24) -> str:
    """
    Run a KQL query directly against the configured Log Analytics workspace.
    Use this for general Log Analytics tables. If your Application Insights
    resource is workspace-based and this workspace ID is the one it's
    connected to, App Insights tables (requests, exceptions, traces, etc.)
    are queryable here too. If this fails with a "table not found" style
    error, use query_app_insights instead - it targets the App Insights
    resource directly and works regardless of workspace configuration.
    """
    response = logs_client.query_workspace(
        workspace_id=LOG_ANALYTICS_WORKSPACE_ID,
        query=kql,
        timespan=timedelta(hours=timespan_hours),
    )
    rows, error = _tables_to_rows(response)
    if rows is None:
        return json.dumps({"error": error})
    return json.dumps({"rows": rows, "error": error}, default=str)


def query_app_insights(kql: str, timespan_hours: int = 24) -> str:
    """
    Run a KQL query directly against the Application Insights resource
    (resource-centric query), bypassing any Log Analytics workspace
    configuration. Use this for requests/exceptions/traces/dependencies
    queries - it works whether the App Insights resource is workspace-based
    or classic, and doesn't depend on LOG_ANALYTICS_WORKSPACE_ID being
    correct.
    """
    response = logs_client.query_resource(
        resource_id=APPINSIGHTS_RESOURCE_ID,
        query=kql,
        timespan=timedelta(hours=timespan_hours),
    )
    rows, error = _tables_to_rows(response)
    if rows is None:
        return json.dumps({"error": error})
    return json.dumps({"rows": rows, "error": error}, default=str)


def _get_arm_token() -> str:
    return credential.get_token(ARM_SCOPE).token


def list_monitor_alerts(time_range: str = "1d") -> str:
    """
    List current alerts across Azure Monitor and Application Insights for the
    configured subscription, using the Azure Alerts Management API directly
    (does not go through Grafana). time_range examples: "1h", "1d", "7d".
    """
    resp = requests.get(
        f"https://management.azure.com/subscriptions/{AZURE_SUBSCRIPTION_ID}"
        f"/providers/Microsoft.AlertsManagement/alerts",
        headers={
            "Authorization": f"Bearer {_get_arm_token()}",
            "Content-Type": "application/json",
        },
        params={"api-version": "2019-03-01", "timeRange": time_range},
        timeout=30,
    )
    resp.raise_for_status()
    alerts = []
    for a in resp.json().get("value", []):
        essentials = a.get("properties", {}).get("essentials", {})
        alerts.append(
            {
                "name": a.get("name"),
                "severity": essentials.get("severity"),
                "monitorCondition": essentials.get("monitorCondition"),
                "monitorService": essentials.get("monitorService"),
                "targetResource": essentials.get("targetResourceName"),
                "startDateTime": essentials.get("startDateTime"),
            }
        )
    return json.dumps(alerts)


LOCAL_FUNCTIONS = {
    "list_dashboards": list_dashboards,
    "get_dashboard": get_dashboard,
    "query_datasource": query_datasource,
    "list_grafana_alerts": list_grafana_alerts,
    "query_logs": query_logs,
    "query_app_insights": query_app_insights,
    "list_monitor_alerts": list_monitor_alerts,
}

# ---------------------------------------------------------------------------
# Function tool declarations for the Foundry agent
# ---------------------------------------------------------------------------

tools: list[Tool] = [
    FunctionTool(
        name="list_dashboards",
        description="Search Azure Managed Grafana dashboards by title keyword. Returns title, uid, url.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text, empty string for all"}
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        strict=True,
    ),
    FunctionTool(
        name="get_dashboard",
        description="Fetch a dashboard's full JSON model by its uid.",
        parameters={
            "type": "object",
            "properties": {"uid": {"type": "string"}},
            "required": ["uid"],
            "additionalProperties": False,
        },
        strict=True,
    ),
    FunctionTool(
        name="query_datasource",
        description=(
            "Run a metrics/logs query (e.g. PromQL against Azure Monitor managed "
            "Prometheus, or LogQL) against a Grafana datasource and return the "
            "raw result frames."
        ),
        parameters={
            "type": "object",
            "properties": {
                "datasource_uid": {"type": "string"},
                "expr": {"type": "string", "description": "PromQL/LogQL expression"},
                "range_minutes": {
                    "type": "integer",
                    "description": "How many minutes back to query, default 60",
                },
            },
            "required": ["datasource_uid", "expr", "range_minutes"],
            "additionalProperties": False,
        },
        strict=True,
    ),
    FunctionTool(
        name="list_grafana_alerts",
        description="List current alert rules in Azure Managed Grafana and whether they are firing.",
        parameters={
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
        strict=True,
    ),
    FunctionTool(
        name="query_logs",
        description=(
            "Run a KQL query directly against the Log Analytics workspace. "
            "Use this for Application Insights data (tables like requests, "
            "exceptions, traces, dependencies, customEvents) or any other "
            "Log Analytics table, when you need deeper querying than what's "
            "available through Grafana dashboards. If this returns a table-"
            "not-found style error, use query_app_insights instead."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kql": {"type": "string", "description": "The KQL query to run"},
                "timespan_hours": {
                    "type": "integer",
                    "description": "How many hours back to query, default 24",
                },
            },
            "required": ["kql", "timespan_hours"],
            "additionalProperties": False,
        },
        strict=True,
    ),
    FunctionTool(
        name="query_app_insights",
        description=(
            "Run a KQL query directly against the Application Insights "
            "resource (requests, exceptions, traces, dependencies, "
            "customEvents). Prefer this over query_logs for App Insights "
            "questions - it targets the App Insights resource directly and "
            "works whether it's workspace-based or classic. "
            "Example query to list recent exceptions: "
            "\"exceptions | where timestamp > ago(1h) | project timestamp, "
            "operation_Name, problemId, outerMessage | order by timestamp "
            "desc | take 20\". Always reference real column names "
            "(timestamp, operation_Name, problemId, outerMessage, "
            "innermostMessage, type, method) - do not invent aggregate "
            "columns like 'occurrences' that don't exist in the schema."
        ),
        parameters={
            "type": "object",
            "properties": {
                "kql": {"type": "string", "description": "The KQL query to run"},
                "timespan_hours": {
                    "type": "integer",
                    "description": "How many hours back to query, default 24",
                },
            },
            "required": ["kql", "timespan_hours"],
            "additionalProperties": False,
        },
        strict=True,
    ),
    FunctionTool(
        name="list_monitor_alerts",
        description=(
            "List currently firing or recently resolved alerts across Azure "
            "Monitor and Application Insights for the subscription, using "
            "the Azure Alerts Management API directly (not through Grafana)."
        ),
        parameters={
            "type": "object",
            "properties": {
                "time_range": {
                    "type": "string",
                    "description": "Lookback window, e.g. '1h', '1d', '7d'. Default '1d'.",
                }
            },
            "required": ["time_range"],
            "additionalProperties": False,
        },
        strict=True,
    ),
]

SYSTEM_INSTRUCTIONS = (
    "You are an observability assistant with access to a live Azure Managed "
    "Grafana workspace, as well as direct access to Azure Monitor Log "
    "Analytics and Application Insights, and the Azure Monitor Alerts "
    "Management API. Use Grafana tools for dashboards and anything already "
    "wired up as a Grafana datasource. For Application Insights questions "
    "(requests, exceptions, traces, dependencies), prefer query_app_insights "
    "- it queries the App Insights resource directly and works regardless "
    "of workspace configuration. Use query_logs only for general Log "
    "Analytics tables outside App Insights. Use list_monitor_alerts for a "
    "subscription-wide view of firing alerts across Azure Monitor and "
    "Application Insights, and list_grafana_alerts for Grafana's own alert "
    "rules. Be concise and cite dashboard names/uids, table names, or alert "
    "names when relevant."
)

# ---------------------------------------------------------------------------
# Create the agent
# ---------------------------------------------------------------------------

agent = project.agents.create_version(
    agent_name="GrafanaFunctionToolAgent",
    definition=PromptAgentDefinition(
        model=MODEL_DEPLOYMENT,
        instructions=SYSTEM_INSTRUCTIONS,
        tools=tools,
    ),
)
print(f"Agent created (id: {agent.id}, name: {agent.name}, version: {agent.version})")


def ask(question: str, max_turns: int = 10, verbose: bool = True) -> str:
    """Send a question to the agent, executing any requested function calls locally."""
    conversation = openai.conversations.create()

    response = openai.responses.create(
        input=question,
        conversation=conversation.id,
        extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
    )

    for turn in range(1, max_turns + 1):
        function_calls = [item for item in response.output if item.type == "function_call"]
        if not function_calls:
            return response.output_text

        input_list: ResponseInputParam = []
        for item in function_calls:
            fn = LOCAL_FUNCTIONS.get(item.name)
            args = json.loads(item.arguments or "{}")

            if verbose:
                print(f"[turn {turn}] calling {item.name}({args})")

            try:
                result = fn(**args) if fn else json.dumps({"error": f"Unknown tool {item.name}"})
            except Exception as exc:
                result = json.dumps({"error": str(exc)})

            if verbose:
                preview = result if len(result) <= 500 else result[:500] + "...(truncated)"
                print(f"[turn {turn}] {item.name} ->  {preview}")

            input_list.append(
                FunctionCallOutput(
                    type="function_call_output",
                    call_id=item.call_id,
                    output=result,
                )
            )

        response = openai.responses.create(
            input=input_list,
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": agent.name, "type": "agent_reference"}},
        )

    # Hit max_turns: show whatever text the model has produced so far, plus
    # a note, instead of silently discarding it.
    partial_text = response.output_text
    if partial_text:
        return f"[Reached max_turns={max_turns} - last partial response below]\n{partial_text}"
    return (
        f"Reached max_turns={max_turns} without a final answer, and the model "
        "produced no text output either. Re-run with verbose=True (default) "
        "and check the printed tool calls/results above for repeated errors - "
        "common causes are missing RBAC roles (Log Analytics Reader / "
        "Monitoring Reader / Grafana Viewer) or an invalid KQL query."
    )


if __name__ == "__main__":
    answer = ask(
        "Are there any firing alerts right now across Azure Monitor and "
        "Application Insights, and are there any exceptions in the last "
        "hour I should know about?"
    )
    print("\nResponse:", answer)

    # Clean up: delete the agent version when you're done experimenting
    # project.agents.delete_version(agent_name=agent.name, agent_version=agent.version)