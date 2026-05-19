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

URL context: when the caller supplies `urls={"philips": ..., "competitors":
["...", "..."]}` and the template's `show_url_fields` is True, each URL's
visible text is appended to the user message so the LLM can compare
customer-facing positioning, not just leaflet specs.

Multi-competitor: every entry point accepts ONE Philips monitor plus a
LIST of competitor models (length ≥ 1). Block-tool rows are dict-shaped,
so the agent emits per-competitor values under indexed keys (e.g.
`competitor_value_1`, `competitor_value_2`, ...). The flat tool emits a
`competitor_values` array, one entry per competitor in order.
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
    competitor_labels: list[str]


class BlockAgentResult(BaseModel):
    """Block output — several block contents + optional narrative summary."""
    kind: str = "block"
    template_info: TemplateInfo
    blocks: list[BlockContent]
    summary_narrative: str = ""
    label_a: str
    competitor_labels: list[str]


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
        "and summary are ready. Calling this ends the loop. "
        "`competitor_values` must list one entry per competitor in the SAME "
        "order the competitors appear in the user message."
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
                        "competitor_values": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "One value per competitor, parallel "
                                           "to the competitor order in the "
                                           "user message.",
                        },
                        "verdict": {
                            "type": "string",
                            "description": "'Philips' / 'Tie' / 'Investigate' "
                                           "OR the full_name of the single "
                                           "competitor that beats everyone "
                                           "else (including Philips).",
                        },
                        "notes": {"type": "string"},
                    },
                    "required": [
                        "group", "feature", "philips_value",
                        "competitor_values", "verdict", "notes",
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
    competitor_labels: list[str],
    on_step: Optional[Callable[[str, dict], None]] = None,
) -> str:
    """Append product-page text to the user message for marketing/custom views.

    `urls` shape: {"philips": "<url>", "competitors": ["<url1>", "<url2>", ...]}.
    The competitor list is parallel to competitor_labels; missing/empty
    entries are tolerated.

    Emits a synthetic `fetch_url` on_step event per side so the Streamlit
    UI can show fetch progress (success with byte count, or failure with
    reason).
    """
    if not urls:
        return ""
    competitor_urls = urls.get("competitors") or []
    sides = [("Philips/OBM", label_a, urls.get("philips") or "")]
    for idx, c_label in enumerate(competitor_labels):
        side_label = (
            f"Competitor {idx + 1}" if len(competitor_labels) > 1 else "Competitor"
        )
        url = competitor_urls[idx] if idx < len(competitor_urls) else ""
        sides.append((side_label, c_label, url or ""))

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


def _format_competitor_blocks(
    competitor_labels: list[str],
    competitor_models: list[str],
    competitor_specs: list[dict],
) -> str:
    """One competitor's spec block per entry, numbered 1..N.

    Numbering matches the indices the agent uses in row keys
    (competitor_value_1 etc.) and in the competitor_values list.
    """
    return "\n\n".join(
        f"Competitor {idx} ({label}, model {model}):\n```json\n"
        f"{json.dumps(specs, indent=2, ensure_ascii=False)}\n```"
        for idx, (label, model, specs) in enumerate(
            zip(competitor_labels, competitor_models, competitor_specs), start=1,
        )
    )


def _build_flat_user_message(
    label_a: str, competitor_labels: list[str],
    model_a: str, competitor_models: list[str],
    specs_a: dict, competitor_specs: list[dict],
    url_context: str,
) -> str:
    competitor_blocks = _format_competitor_blocks(
        competitor_labels, competitor_models, competitor_specs,
    )
    n = len(competitor_labels)
    competitor_list_line = ", ".join(
        f"'{label}'" for label in competitor_labels
    )
    return (
        f"Compare ONE Philips/OBM monitor against {n} competitor monitor(s) "
        f"and call write_comparison with the final result.\n\n"
        f"Philips/OBM ({label_a}, model {model_a}):\n```json\n"
        f"{json.dumps(specs_a, indent=2, ensure_ascii=False)}\n```\n\n"
        f"{competitor_blocks}\n"
        f"{url_context}\n\n"
        "Read-only tools (list_monitors, get_monitor) are available if you "
        "need to verify a spec or pull in a reference monitor. Most cases "
        "need no tool calls.\n\n"
        "Row shape:\n"
        f"- `competitor_values` is a list of exactly {n} string(s), in the "
        "SAME order as the competitors above (Competitor 1, Competitor 2, "
        "...).\n"
        "- Verdict scheme:\n"
        "  • 'Philips' — Philips beats every competitor on this spec\n"
        f"  • One of: {competitor_list_line} — that competitor beats every "
        "other monitor (including Philips)\n"
        "  • 'Tie' — at least two monitors lead and are equivalent\n"
        "  • 'Investigate' — subjective, marketing-only, or any side did "
        "not list the value (data gap)\n\n"
        "When ready, call write_comparison(rows=[...], summary='...')."
    )


def _build_block_user_message(
    template_info: TemplateInfo,
    label_a: str, competitor_labels: list[str],
    model_a: str, competitor_models: list[str],
    specs_a: dict, competitor_specs: list[dict],
    url_context: str,
) -> str:
    competitor_blocks_text = _format_competitor_blocks(
        competitor_labels, competitor_models, competitor_specs,
    )
    blocks_summary_lines = []
    for b in (template_info.output_blocks or []):
        expanded = []
        for col in b.columns:
            if col == "competitor" or col.startswith("competitor_"):
                expanded.extend(
                    f"{col}_{i}" for i in range(1, len(competitor_labels) + 1)
                )
            else:
                expanded.append(col)
        blocks_summary_lines.append(
            f"  - block_key='{b.key}' → row keys: {expanded}"
        )
    blocks_summary = "\n".join(blocks_summary_lines)

    summary_line = (
        "Also include a `summary_narrative` — see the system prompt for "
        "what to put there.\n\n"
        if template_info.include_summary_narrative else
        "Leave `summary_narrative` empty (this template does not use it).\n\n"
    )
    n = len(competitor_labels)
    competitor_list_line = ", ".join(
        f"'{label}'" for label in competitor_labels
    )
    return (
        f"Compare ONE Philips/OBM monitor against {n} competitor monitor(s) "
        f"and call write_block_comparison with every block your template "
        f"defines.\n\n"
        f"Philips/OBM ({label_a}, model {model_a}):\n```json\n"
        f"{json.dumps(specs_a, indent=2, ensure_ascii=False)}\n```\n\n"
        f"{competitor_blocks_text}\n"
        f"{url_context}\n\n"
        "Blocks expected. Each row must use the exact keys listed (note "
        "the per-competitor columns are SPLIT into one key per competitor, "
        "numbered to match the competitor order above):\n"
        f"{blocks_summary}\n\n"
        + summary_line
        + "Verdict / winner cells (when present):\n"
        "  • 'Philips' — Philips beats every competitor\n"
        f"  • One of: {competitor_list_line} — that competitor beats every "
        "other monitor (including Philips)\n"
        "  • 'Tie' or 'Investigate' as usual\n\n"
        "Read-only tools (list_monitors, get_monitor) are available if "
        "you need to verify a spec. Most cases need no tool calls.\n\n"
        "When ready, call write_block_comparison(blocks=[...], "
        "summary_narrative='...')."
    )


# ─── The agent loop ───────────────────────────────────────────────────

MAX_TURNS = 8


def run_comparison_agent(
    model_a: str,
    competitor_models: list[str],
    template: str | dict,
    on_step: Optional[Callable[[str, dict], None]] = None,
    urls: Optional[dict] = None,
) -> AgentResult:
    """Run the comparison agent and return either a flat or block result.

    `urls` is an optional dict like
    {"philips": "...", "competitors": ["...", "..."]}; each entry may be
    blank. URLs are only used when the resolved template's show_url_fields
    is True. The competitor URL list is parallel to competitor_models —
    same length, same order.

    Returns FlatAgentResult or BlockAgentResult — caller branches on .kind.

    Raises ValueError on bad inputs (unknown monitor/template, Philips also
    listed as competitor, duplicate competitors). Raises RuntimeError if
    MAX_TURNS is exceeded without a write call.
    """
    if not competitor_models:
        raise ValueError("Pick at least one competitor.")
    if model_a in competitor_models:
        raise ValueError("Philips model also appears in the competitor list.")
    if len(set(competitor_models)) != len(competitor_models):
        raise ValueError("Pick distinct competitors — duplicates are not allowed.")

    info = parse_template_info(template)

    a_record = get_monitor_specs(model_a)
    label_a = a_record["full_name"]
    specs_a = _filter_specs_to_groups(a_record["specs"], info.groups)

    competitor_labels: list[str] = []
    competitor_specs: list[dict] = []
    for cm in competitor_models:
        rec = get_monitor_specs(cm)
        competitor_labels.append(rec["full_name"])
        competitor_specs.append(_filter_specs_to_groups(rec["specs"], info.groups))

    url_context = ""
    if info.show_url_fields and urls:
        url_context = _format_url_context(
            urls, label_a, competitor_labels, on_step=on_step,
        )

    is_block = info.output_blocks is not None
    if is_block:
        user_msg = _build_block_user_message(
            info, label_a, competitor_labels, model_a, competitor_models,
            specs_a, competitor_specs, url_context,
        )
        tools = _READ_TOOLS + [_WRITE_BLOCK_TOOL]
    else:
        user_msg = _build_flat_user_message(
            label_a, competitor_labels, model_a, competitor_models,
            specs_a, competitor_specs, url_context,
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
                normalized_rows = []
                expected_n = len(competitor_labels)
                for r in tu.input["rows"]:
                    values = list(r.get("competitor_values") or [])
                    # Pad/truncate so the row always has one value per
                    # competitor. The agent should already conform; this
                    # is a defensive fallback so a sloppy row does not
                    # blow up Pydantic.
                    if len(values) < expected_n:
                        values = values + [""] * (expected_n - len(values))
                    elif len(values) > expected_n:
                        values = values[:expected_n]
                    normalized_rows.append({
                        **r,
                        "competitor_values": [
                            "" if v is None else str(v) for v in values
                        ],
                    })
                comp = Comparison(
                    rows=[ComparisonRow(**r) for r in normalized_rows],
                    summary=tu.input["summary"],
                )
                final = FlatAgentResult(
                    comparison=comp,
                    label_a=label_a,
                    competitor_labels=competitor_labels,
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
                    label_a=label_a,
                    competitor_labels=competitor_labels,
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
