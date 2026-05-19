"""
MCP server exposing the monitor benchmark as tools.

Run from Claude Desktop (or any MCP client) so Claude can call tools in an
agentic loop — e.g. "find the closest Philips response to the Dell U2725QE
and draft a comparison" calls search_monitors → get_monitor → compare_monitors
without the user clicking through tabs.

Tools are thin wrappers around core.py so the Streamlit app and the MCP
server stay in sync. No new business logic lives here.

Stdio transport (the default) is what Claude Desktop launches; do not invoke
this script manually — let the host process spawn it.
"""

from pathlib import Path

from dotenv import load_dotenv

# Load .env explicitly from this folder. Claude Desktop launches the server
# from its own working directory, so the implicit load_dotenv() in core.py
# cannot find the project's .env on its own.
HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

from mcp.server.fastmcp import FastMCP

from core import (
    compute_comparison,
    comparison_to_excel,
    get_monitor_specs,
    load_templates,
    search_monitors,
)

mcp = FastMCP("benchmark")


@mcp.tool()
def list_monitors(query: str = "") -> list[dict]:
    """List monitors in the benchmark database.

    Pass an empty query to see everything. Otherwise matches case-insensitive
    substrings against brand, model, and full name. Returns summary rows
    (id, brand, model, spec_count, etc.) — call get_monitor for full specs.
    """
    return [m.model_dump() for m in search_monitors(query)]


@mcp.tool()
def get_monitor(model: str) -> dict:
    """Full specs for one monitor, keyed by model number (e.g. "27B2U6903").

    Returns brand, model, full_name, and specs as {group: {feature: value}}.
    Raises an error if the model is not in the database.
    """
    return get_monitor_specs(model)


@mcp.tool()
def list_templates_tool() -> list[dict]:
    """Available comparison view templates (R&D, marketing, etc.).

    Each template controls the audience, tone, and which spec groups are
    included in a comparison. Use the returned `key` when calling
    compare_monitors or export_comparison_excel.
    """
    return [
        {
            "key": key,
            "name": tmpl["name"],
            "description": tmpl.get("description", ""),
            "groups": tmpl.get("groups", []) or "all groups",
        }
        for key, tmpl in load_templates().items()
    ]


@mcp.tool()
def compare_monitors(
    model_a: str, competitor_models: list[str], template: str
) -> dict:
    """Compare one Philips/OBM monitor against one or more competitors.

    `model_a` is the Philips/OBM side. `competitor_models` is a list of one
    or more competitor model numbers — every entry is compared side by
    side against the Philips monitor. `template` is the key returned by
    list_templates_tool (e.g. "rd_deepdive").

    Returns {label_a, competitor_labels, summary, rows: [...]} — no file
    written. Each row carries `philips_value`, a parallel `competitor_values`
    list (one per competitor in order), and a verdict naming the winner.
    Use export_comparison_excel if you also want the .xlsx on disk.
    """
    comparison, label_a, competitor_labels = compute_comparison(
        model_a, competitor_models, template,
    )
    return {
        "label_a": label_a,
        "competitor_labels": competitor_labels,
        "summary": comparison.summary,
        "rows": [r.model_dump() for r in comparison.rows],
    }


@mcp.tool()
def export_comparison_excel(
    model_a: str,
    competitor_models: list[str],
    template: str,
    output_path: str,
) -> str:
    """Generate the comparison and save it as an Excel workbook on disk.

    Same inputs as compare_monitors plus `output_path` — an absolute path
    where the .xlsx should be written. Returns a short status string with
    the resolved path. Overwrites any existing file at that location.
    """
    comparison, label_a, competitor_labels = compute_comparison(
        model_a, competitor_models, template,
    )
    xlsx_bytes = comparison_to_excel(comparison, label_a, competitor_labels)
    out = Path(output_path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(xlsx_bytes)
    return (
        f"Wrote {len(comparison.rows)} rows "
        f"({label_a} vs {', '.join(competitor_labels)}) to {out}"
    )


if __name__ == "__main__":
    mcp.run()
