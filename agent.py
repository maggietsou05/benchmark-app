"""
Agentic comparison generator with two output paths:

  - Flat (legacy / Custom view) — one table + summary, emitted via the
    `write_comparison` tool.
  - Block (R&D, B2C marketing, B2B marketing) — several blocks each with
    its own narrative + column schema, emitted via `write_block_comparison`.

The path is chosen automatically from the template's metadata: if the
YAML declares `output_blocks`, the block path runs; otherwise the flat
path runs (same as before).

Read-only tools (list_monitors, get_monitor) are available on both paths
so the agent can verify a suspicious spec or pull in a reference monitor.

URL context: when the caller supplies `urls={"philips": ..., "competitor": ...}`
and the template's `show_url_fields` is True, each URL's visible text is
appended to the user message so the LLM can compare customer-facing
positioning, not just leaflet specs.
"""

import json
from typing import Callable, Optional, Union

from pydantic import BaseModel

from core import (
    BlockContent,
    Comparison,
    ComparisonRow,
    TemplateInfo,
    _client,
    fetch_url_text,
    get_monitor_specs,
    parse_template_info,
    search_monitors,
)


# ─── Tagged result types ──────────────────────────────────────────────

class FlatAgentResult(BaseModel):
    """Legacy flat output — one table + summary. Used by Custom view."""
    kind: str = "flat"
    comparison: Comparison
    label_a: str
    label_b: str


class BlockAgentResult(BaseModel):
    """Block output — several block contents + optional narrative summary."""
    kind: str = "block"
    template_info: TemplateInfo
    blocks: list[BlockContent]
    summary_narrative: str = ""
    label_a: str
    label_b: str


AgentResult = Union[FlatAgentResult, BlockAgentResult]


# ─── Read-only tools (shared) ─────────────────────────────────────────

_READ_TOOLS = [
    {
        "name": "get_monitor",
        "description": (
            "Fetch full specifications for one monitor by model number. "
            "Use to verify a spec, look for adjacent fields when one looks "
            "missing, or pull in a reference monitor for context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "model": {
                    "type": "string",
                    "description": "Monitor model number, e.g. '27B2U6903'.",
                },
            },
            "required": ["model"],
        },
    },
    {
        "name": "list_monitors",
        "description": (
            "List monitors in the benchmark database, optionally filtered by "
            "case-insensitive substring against brand, model, or full name. "
            "Empty query returns everything. Returns summary rows only — call "
            "get_monitor for full specs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional filter; empty string returns all.",
                },
            },
            "required": [],
        },
    },
]


# ─── Path-specific writer tools ───────────────────────────────────────

_WRITE_FLAT_TOOL = {
    "name": "write_comparison",
    "description": (
        "Emit the final flat comparison. Call this exactly once when verdicts "
        "and summary are ready. Calling this ends the loop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "group": {"type": "string"},
                        "feature": {"type": "string"},
                        "philips_value": {"type": "string"},
                        "competitor_value": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["Philips", "Competitor", "Tie", "Investigate"],
                        },
                        "notes": {"type": "string"},
                    },
                    "required": [
                        "group", "feature", "philips_value",
                        "competitor_value", "verdict", "notes",
                    ],
                    "additionalProperties": False,
                },
            },
            "summary": {"type": "string"},
        },
        "required": ["rows", "summary"],
        "additionalProperties": False,
    },
}


_WRITE_BLOCK_TOOL = {
    "name": "write_block_comparison",
    "description": (
        "Emit the final block-based comparison. Call this exactly once when "
        "every block's narrative and rows are ready. Calling this ends the loop."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "blocks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "block_key": {
                            "type": "string",
                            "description": "Must match one of the block keys "
                                           "named in the system prompt.",
                        },
                        "narrative": {
                            "type": "string",
                            "description": "Plain-English paragraph for this block.",
                        },
                        "rows": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Each row is an object whose keys "
                                           "are the column keys named in the "
                                           "system prompt for this block.",
                        },
                    },
                    "required": ["block_key", "narrative", "rows"],
                },
            },
            "summary_narrative": {
                "type": "string",
                "description": "Optional priority-ordered narrative summary "
                               "(used by templates that ask for one).",
            },
        },
        "required": ["blocks"],
    },
}


# ─── Read-tool handlers ───────────────────────────────────────────────

def _tool_get_monitor(model: str) -> dict:
    try:
        return get_monitor_specs(model)
    except ValueError as e:
        return {"error": str(e)}


def _tool_list_monitors(query: str = "") -> list[dict]:
    return [m.model_dump() for m in search_monitors(query)]


_READ_HANDLERS = {
    "get_monitor": _tool_get_monitor,
    "list_monitors": _tool_list_monitors,
}


# ─── User-message builders ────────────────────────────────────────────

def _filter_specs_to_groups(specs: dict, groups: list[str] | None) -> dict:
    if not groups:
        return specs
    return {g: specs.get(g, {}) for g in groups if specs.get(g)}


def _format_url_context(
    urls: dict | None,
    label_a: str,
    label_b: str,
    on_step: Optional[Callable[[str, dict], None]] = None,
) -> str:
    """Append product-page text to the user message for marketing/custom views.

    Emits a synthetic `fetch_url` on_step event per side so the Streamlit
    UI can show fetch progress (success with byte count, or failure with
    reason).
    """
    if not urls:
        return ""
    sides = [
        ("Philips/OBM", label_a, urls.get("philips") or ""),
        ("Competitor", label_b, urls.get("competitor") or ""),
    ]
    if not any(u for _, _, u in sides):
        return ""

    parts = ["", "=== Product-page text (use for use-case and positioning analysis) ==="]
    for side_label, brand_label, url in sides:
        if not url:
            if on_step:
                on_step("fetch_url", {
                    "side": side_label, "brand": brand_label, "url": "",
                    "ok": False, "reason": "no URL provided",
                })
            parts.append(
                f"\n[{side_label} — {brand_label}] No URL provided. "
                "Skip URL-derived rows or use-case analysis for this side."
            )
            continue
        try:
            text = fetch_url_text(url)
        except ValueError as e:
            if on_step:
                on_step("fetch_url", {
                    "side": side_label, "brand": brand_label, "url": url,
                    "ok": False, "reason": str(e),
                })
            parts.append(
                f"\n[{side_label} — {brand_label}] {url}\n"
                f"Could not fetch page ({e}). "
                "Skip URL-derived rows for this side."
            )
            continue
        if on_step:
            on_step("fetch_url", {
                "side": side_label, "brand": brand_label, "url": url,
                "ok": True, "chars": len(text),
            })
        parts.append(
            f"\n[{side_label} — {brand_label}] {url}\n"
            f"```\n{text}\n```"
        )
    return "\n".join(parts)


def _build_flat_user_message(
    label_a: str, label_b: str,
    model_a: str, model_b: str,
    specs_a: dict, specs_b: dict,
    url_context: str,
) -> str:
    return (
        f"Compare these two monitors and call write_comparison with the "
        f"final result.\n\n"
        f"Philips/OBM ({label_a}, model {model_a}):\n```json\n"
        f"{json.dumps(specs_a, indent=2, ensure_ascii=False)}\n```\n\n"
        f"Competitor ({label_b}, model {model_b}):\n```json\n"
        f"{json.dumps(specs_b, indent=2, ensure_ascii=False)}\n```\n"
        f"{url_context}\n\n"
        "Read-only tools (list_monitors, get_monitor) are available if you "
        "need to verify a spec or pull in a reference monitor. Most cases "
        "need no tool calls.\n\n"
        "Verdict scheme:\n"
        "- 'Philips' if Philips/OBM is better\n"
        "- 'Competitor' if the competitor is better\n"
        "- 'Tie' if equivalent\n"
        "- 'Investigate' if subjective, marketing-only, or one side did "
        "not list the value (data gap)\n\n"
        "When ready, call write_comparison(rows=[...], summary='...')."
    )


def _build_block_user_message(
    template_info: TemplateInfo,
    label_a: str, label_b: str,
    model_a: str, model_b: str,
    specs_a: dict, specs_b: dict,
    url_context: str,
) -> str:
    blocks_summary = "\n".join(
        f"  - block_key='{b.key}' → columns: {b.columns}"
        for b in (template_info.output_blocks or [])
    )
    summary_line = (
        "Also include a `summary_narrative` — see the system prompt for "
        "what to put there.\n\n"
        if template_info.include_summary_narrative else
        "Leave `summary_narrative` empty (this template does not use it).\n\n"
    )
    return (
        f"Compare these two monitors and call write_block_comparison with "
        f"every block your template defines.\n\n"
        f"Philips/OBM ({label_a}, model {model_a}):\n```json\n"
        f"{json.dumps(specs_a, indent=2, ensure_ascii=False)}\n```\n\n"
        f"Competitor ({label_b}, model {model_b}):\n```json\n"
        f"{json.dumps(specs_b, indent=2, ensure_ascii=False)}\n```\n"
        f"{url_context}\n\n"
        "Blocks expected (use these exact block_key values and column "
        "keys inside each row):\n"
        f"{blocks_summary}\n\n"
        + summary_line
        + "Read-only tools (list_monitors, get_monitor) are available if "
        "you need to verify a spec. Most cases need no tool calls.\n\n"
        "When ready, call write_block_comparison(blocks=[...], "
        "summary_narrative='...')."
    )


# ─── The agent loop ───────────────────────────────────────────────────

MAX_TURNS = 8


def run_comparison_agent(
    model_a: str,
    model_b: str,
    template: str | dict,
    on_step: Optional[Callable[[str, dict], None]] = None,
    urls: Optional[dict] = None,
) -> AgentResult:
    """Run the comparison agent and return either a flat or block result.

    `urls` is an optional dict like {"philips": "...", "competitor": "..."};
    each value may be an empty string. URLs are only used when the resolved
    template's show_url_fields is True.

    Returns FlatAgentResult or BlockAgentResult — caller branches on .kind.

    Raises ValueError on bad inputs (unknown monitors/template, same model
    twice). Raises RuntimeError if MAX_TURNS is exceeded without a write call.
    """
    if model_a == model_b:
        raise ValueError("Pick two different monitors.")

    info = parse_template_info(template)

    a_record = get_monitor_specs(model_a)
    b_record = get_monitor_specs(model_b)
    label_a = a_record["full_name"]
    label_b = b_record["full_name"]
    specs_a = _filter_specs_to_groups(a_record["specs"], info.groups)
    specs_b = _filter_specs_to_groups(b_record["specs"], info.groups)

    url_context = ""
    if info.show_url_fields and urls:
        url_context = _format_url_context(urls, label_a, label_b, on_step=on_step)

    is_block = info.output_blocks is not None
    if is_block:
        user_msg = _build_block_user_message(
            info, label_a, label_b, model_a, model_b, specs_a, specs_b, url_context,
        )
        tools = _READ_TOOLS + [_WRITE_BLOCK_TOOL]
    else:
        user_msg = _build_flat_user_message(
            label_a, label_b, model_a, model_b, specs_a, specs_b, url_context,
        )
        tools = _READ_TOOLS + [_WRITE_FLAT_TOOL]

    messages: list[dict] = [{"role": "user", "content": user_msg}]

    for _turn in range(MAX_TURNS):
        response = _client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=16000,
            system=[{
                "type": "text",
                "text": info.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=tools,
            messages=messages,
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        if not tool_uses:
            raise RuntimeError(
                "Claude stopped without calling the write tool "
                f"(stop_reason={response.stop_reason!r}). Usually fixable by "
                "retrying or tightening the template's system prompt."
            )

        messages.append({"role": "assistant", "content": response.content})

        tool_results: list[dict] = []
        final: AgentResult | None = None

        for tu in tool_uses:
            if on_step:
                on_step(tu.name, tu.input)

            if tu.name == "write_comparison":
                comp = Comparison(
                    rows=[ComparisonRow(**r) for r in tu.input["rows"]],
                    summary=tu.input["summary"],
                )
                final = FlatAgentResult(
                    comparison=comp, label_a=label_a, label_b=label_b,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "Comparison received.",
                })

            elif tu.name == "write_block_comparison":
                blocks = [
                    BlockContent(
                        block_key=b["block_key"],
                        narrative=b.get("narrative", "") or "",
                        rows=[
                            {
                                str(k): ("" if v is None else str(v))
                                for k, v in (r or {}).items()
                            }
                            for r in b.get("rows", [])
                        ],
                    )
                    for b in tu.input["blocks"]
                ]
                final = BlockAgentResult(
                    template_info=info,
                    blocks=blocks,
                    summary_narrative=tu.input.get("summary_narrative", "") or "",
                    label_a=label_a, label_b=label_b,
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": "Block comparison received.",
                })

            elif tu.name in _READ_HANDLERS:
                try:
                    result = _READ_HANDLERS[tu.name](**tu.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": json.dumps(result, ensure_ascii=False),
                    })
                except Exception as e:
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.id,
                        "content": f"Error: {e}",
                        "is_error": True,
                    })

            else:
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": f"Unknown tool: {tu.name}",
                    "is_error": True,
                })

        if final is not None:
            return final

        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(
        f"Agent exceeded {MAX_TURNS} turns without calling a write tool."
    )
