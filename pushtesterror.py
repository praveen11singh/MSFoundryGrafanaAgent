"""
push_test_error.py
---------------------------------------------------------------------------
Standalone utility (separate from the agent) that generates a test error in
both Application Insights and Azure Managed Grafana, so you can verify the
agent's query_logs / list_monitor_alerts / Grafana tools actually pick
something up end to end.

What it does:
  - log_error_to_app_insights(): raises and logs a test exception, exported
    to Application Insights via OpenTelemetry. Shows up in the "exceptions"
    table of the connected Log Analytics workspace within a minute or two.
  - log_error_to_grafana(): posts an annotation (a marked event/error) to
    Azure Managed Grafana via its Annotations API, authenticated with the
    same Entra ID token used by the main agent.

Requirements:
    pip install azure-monitor-opentelemetry azure-identity requests python-dotenv

Environment variables required (.env, alongside the main agent's values):
    APPLICATIONINSIGHTS_CONNECTION_STRING   connection string of your App
                                             Insights resource (Overview >
                                             Connection String)
    AZURE_MANAGED_GRAFANA_ENDPOINT          e.g. https://my-grafana-xxxx.wcus.grafana.azure.com
"""

import os
import time
import logging
import requests
from dotenv import load_dotenv
from azure.identity import DefaultAzureCredential
from azure.monitor.opentelemetry import configure_azure_monitor

load_dotenv()

APPINSIGHTS_CONNECTION_STRING = os.environ["APPLICATIONINSIGHTS_CONNECTION_STRING"]
GRAFANA_ENDPOINT = os.environ["AZURE_MANAGED_GRAFANA_ENDPOINT"].rstrip("/")

# Fixed Azure AD application (resource) ID for Azure Managed Grafana's data plane
GRAFANA_AAD_SCOPE = "6f2d169c-08f3-4a4c-a982-bcaf2d038c45/.default"

credential = DefaultAzureCredential()

logger = logging.getLogger("push_test_error")


def log_error_to_app_insights(message: str = "Test error from MSFoundryGrafanaAgent") -> None:
    """Raise and log a test exception, exported to Application Insights."""
    configure_azure_monitor(connection_string=APPINSIGHTS_CONNECTION_STRING)

    try:
        raise RuntimeError(message)
    except RuntimeError:
        logger.exception(message)

    print(f"Sent test exception to Application Insights: {message!r}")
    print("Note: it can take 1-2 minutes to appear in the Log Analytics workspace.")


def log_error_to_grafana(message: str = "Test error from MSFoundryGrafanaAgent") -> None:
    """Post an annotation marking a test error event on the Grafana timeline."""
    token = credential.get_token(GRAFANA_AAD_SCOPE).token

    payload = {
        "text": message,
        "tags": ["error", "test"],
        "time": int(time.time() * 1000),
    }
    resp = requests.post(
        f"{GRAFANA_ENDPOINT}/api/annotations",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    print(f"Created Grafana annotation: {resp.json()}")


if __name__ == "__main__":
    log_error_to_app_insights()
    log_error_to_grafana()