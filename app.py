"""
Streamlit benchmark app — three tabs:

  📥 Ingest    Upload PDF leaflet, extract specs via Claude, save to DB.
  🔍 Browse    Search, view, edit, delete monitors. No tokens spent.
  📊 Generate  Pick two monitors + a view template, download Excel.

UI only. The actual operations live in core.py so they can be reused by
other front-ends or scheduled jobs.
"""

# cd benchmark_app
# venv\Scripts\python.exe -m streamlit run app.py


import os

import anthropic
import pandas as pd
import streamlit as st

# On Streamlit Cloud the Anthropic API key lives in st.secrets, not in a
# .env file — bridge it to os.environ BEFORE importing core, because core
# constructs anthropic.Anthropic() at import time and that constructor
# reads the key from the environment. Locally this no-ops (load_dotenv in
# core handles the .env path).
_secret_key = st.secrets.get("ANTHROPIC_API_KEY") if hasattr(st, "secrets") else None
if _secret_key:
    os.environ["ANTHROPIC_API_KEY"] = _secret_key

from agent import run_comparison_agent
from core import (
    DB_PATH,
    GROUP_NAMES,
    PROMPTS_DIR,
    MonitorSpec,
    block_to_xlsx,
    blocks_to_workbook,
    build_custom_template,
    comparison_to_excel,
    extract_monitor_from_pdf,
    get_conn,
    load_templates,
    search_monitors,
)


# ─── DB helpers (UI-side mutations) ────────────────────────────────

def migrate_db():
    """Idempotent schema migrations — adds columns missing on older DBs."""
    with get_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(products)")}
        if "website_url" not in cols:
            conn.execute("ALTER TABLE products ADD COLUMN website_url TEXT")


def get_pdf_bytes(product_id: int) -> bytes | None:
    """Original PDF bytes for a monitor, or None if none stored."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT source_pdf FROM products WHERE id = ?", (product_id,)
        ).fetchone()
    return bytes(row[0]) if row and row[0] else None


def get_specs(product_id: int) -> dict[str, list[dict]]:
    """Specs grouped by group_name, with spec ids — for the Browse editor."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, group_name, feature, value, sort_order FROM specs "
            "WHERE product_id = ? ORDER BY sort_order",
            (product_id,),
        ).fetchall()
    grouped: dict[str, list[dict]] = {}
    for sid, group, feature, value, _ in rows:
        grouped.setdefault(group, []).append(
            {"id": sid, "feature": feature, "value": value or ""}
        )
    return grouped


def upsert_product(brand, model, full_name, source_filename, ingested_by,
                   source_pdf: bytes | None = None,
                   website_url: str | None = None) -> tuple[int, bool]:
    """Insert or replace a product. Returns (product_id, was_existing).

    source_pdf and website_url are optional — when None on an update, the
    existing stored value is preserved (COALESCE).
    """
    with get_conn() as conn:
        cur = conn.cursor()
        existing = cur.execute(
            "SELECT id FROM products WHERE model = ?", (model,)
        ).fetchone()
        if existing:
            cur.execute(
                "UPDATE products SET brand=?, full_name=?, source_filename=?, "
                "source_pdf=COALESCE(?, source_pdf), "
                "website_url=COALESCE(?, website_url), "
                "ingested_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (brand, full_name, source_filename, source_pdf, website_url,
                 ingested_by, existing[0]),
            )
            return existing[0], True
        cur.execute(
            "INSERT INTO products (brand, model, full_name, source_filename, "
            "source_pdf, website_url, ingested_by) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (brand, model, full_name, source_filename, source_pdf, website_url, ingested_by),
        )
        return cur.lastrowid, False


def replace_specs(product_id: int, specs: list[dict]) -> None:
    with get_conn() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM specs WHERE product_id = ?", (product_id,))
        for i, item in enumerate(specs):
            cur.execute(
                "INSERT INTO specs (product_id, group_name, feature, value, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (product_id, item["group"], item["feature"], item["value"], i + 1),
            )


def update_spec_value(spec_id: int, new_value: str) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE specs SET value = ? WHERE id = ?", (new_value, spec_id))
        conn.execute(
            "UPDATE products SET updated_at = CURRENT_TIMESTAMP "
            "WHERE id = (SELECT product_id FROM specs WHERE id = ?)",
            (spec_id,),
        )


def delete_product(product_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


# ─── Tab: Ingest ───────────────────────────────────────────────────

def render_ingest_tab():
    st.header("Ingest a PDF leaflet")
    st.caption(
        "Upload a monitor leaflet. A preview of the specs will be "
        "shown to you before saving to the database."
    )

    if "ingest_user" not in st.session_state:
        st.session_state.ingest_user = ""
    if "ingest_url" not in st.session_state:
        st.session_state.ingest_url = ""

    st.session_state.ingest_user = st.text_input(
        "Your name (saved with the ingested data for provenance)",
        value=st.session_state.ingest_user,
        placeholder="e.g. Rachel Chen",
    )
    st.session_state.ingest_url = st.text_input(
        "Product page URL (optional — used later for marketing insights)",
        value=st.session_state.ingest_url,
        placeholder="https://www.philips.com/c-p/27B2N4500/...",
    )

    uploaded = st.file_uploader("Drop a PDF leaflet here", type="pdf")
    if uploaded is None:
        return

    st.write(f"**File:** {uploaded.name} &nbsp;·&nbsp; {uploaded.size / 1024:.1f} KB")

    if not st.session_state.ingest_user.strip():
        st.warning("Enter your name above before extracting.")
        return

    if st.button("🤖 Extract", type="primary"):
        # getvalue() returns the bytes without consuming the buffer, so we
        # can both extract specs from them and stash them for later upload.
        pdf_bytes = uploaded.getvalue()
        with st.spinner("Claude is extracting specs..."):
            try:
                extracted = extract_monitor_from_pdf(pdf_bytes)
            except ValueError as e:
                st.error(str(e))
                return
            except anthropic.AuthenticationError:
                st.error("Invalid API key. Check your .env file.")
                return
            except anthropic.APIError as e:
                st.error(f"API error: {e.message}")
                return

        st.session_state.last_extraction = {
            "extracted": extracted,
            "filename": uploaded.name,
            "pdf_bytes": pdf_bytes,
        }

    if "last_extraction" not in st.session_state:
        return

    last = st.session_state.last_extraction
    extracted: MonitorSpec = last["extracted"]

    st.success(
        f"Extracted **{extracted.brand} {extracted.model}** "
        f"({len(extracted.specs)} specs"
        + (f", {len(extracted.visual_observations)} visual observations"
           if extracted.visual_observations else "")
        + ")."
    )

    st.subheader("Preview (review before saving)")

    by_group: dict[str, list] = {}
    for s in extracted.specs:
        by_group.setdefault(s.group, []).append(s)
    for group in GROUP_NAMES:
        if group in by_group:
            with st.expander(f"{group} ({len(by_group[group])} specs)"):
                df = pd.DataFrame(
                    [{"Feature": s.feature, "Value": s.value} for s in by_group[group]]
                )
                st.dataframe(df, use_container_width=True, hide_index=True)

    if extracted.visual_observations:
        st.subheader("🔍 Visual observations to review")
        st.caption(
            "Claude spotted these physical features in the diagrams that "
            "weren't in the spec text. Tick the ones to save as specs. "
            "High-confidence items are pre-checked."
        )
        confidence_badge = {"high": "🟢", "medium": "🟡", "low": "🔴"}
        for i, obs in enumerate(extracted.visual_observations):
            col_check, col_info = st.columns([1, 14])
            with col_check:
                st.checkbox(
                    "Save",
                    value=(obs.confidence == "high"),
                    key=f"obs_check_{i}",
                    label_visibility="collapsed",
                )
            with col_info:
                st.markdown(
                    f"**{obs.feature}** {confidence_badge[obs.confidence]} "
                    f"_{obs.confidence}_ &nbsp;·&nbsp; "
                    f"would be saved under group `{obs.suggested_group}`  \n"
                    f"<small>{obs.visual_cue}</small>",
                    unsafe_allow_html=True,
                )

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, full_name FROM products WHERE model = ?",
            (extracted.model,),
        ).fetchone()

    if existing:
        st.warning(
            f"A monitor with model **{extracted.model}** already exists "
            f"({existing[1]}). Saving will REPLACE its current specs."
        )

    save_label = "Replace existing" if existing else "Save to database"
    if st.button(save_label, type="primary"):
        full_name = f"{extracted.brand} {extracted.model}"
        product_id, was_existing = upsert_product(
            brand=extracted.brand,
            model=extracted.model,
            full_name=full_name,
            source_filename=last["filename"],
            ingested_by=st.session_state.ingest_user.strip(),
            source_pdf=last["pdf_bytes"],
            website_url=st.session_state.ingest_url.strip() or None,
        )
        spec_rows = [
            {"group": s.group, "feature": s.feature, "value": s.value}
            for s in extracted.specs
        ]
        confirmed_obs = [
            obs for i, obs in enumerate(extracted.visual_observations)
            if st.session_state.get(f"obs_check_{i}", False)
        ]
        for obs in confirmed_obs:
            spec_rows.append({
                "group": obs.suggested_group,
                "feature": obs.feature,
                "value": f"Yes — from diagram: {obs.visual_cue}",
            })
        replace_specs(product_id, spec_rows)
        verb = "Updated" if was_existing else "Saved"
        extra = (
            f" ({len(confirmed_obs)} visual observation"
            f"{'s' if len(confirmed_obs) != 1 else ''} added)"
            if confirmed_obs else ""
        )
        st.success(
            f"{verb} {full_name} in the database{extra}. "
            "Switch to the Browse tab to view it."
        )
        del st.session_state.last_extraction


# ─── Tab: Browse ───────────────────────────────────────────────────

def render_browse_tab():
    st.header("Browse monitors")
    st.caption("Search, view, edit, or delete monitors in the database.")

    all_monitors = search_monitors("")
    if not all_monitors:
        st.info("No monitors in the database yet. Use the Ingest tab to add one.")
        return

    c1, c2 = st.columns([1, 2])
    with c1:
        brands = ["All"] + sorted({m.brand for m in all_monitors})
        brand_filter = st.selectbox("Brand", brands, key="browse_brand")
    with c2:
        model_query = st.text_input(
            "Model search", placeholder="Type to filter…", key="browse_model"
        )

    monitors = search_monitors(model_query)
    if brand_filter != "All":
        monitors = [m for m in monitors if m.brand == brand_filter]

    if not monitors:
        st.info("No monitors match your filters.")
        return

    display_df = pd.DataFrame([
        {"Brand": m.brand, "Model": m.model, "Specs": m.spec_count,
         "Last updated": m.updated_at}
        for m in monitors
    ])

    event = st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        on_select="rerun",
        selection_mode="single-row",
    )

    if not event.selection.rows:
        st.info("Click a row above to view, edit, or delete a monitor.")
        return

    selected = monitors[event.selection.rows[0]]
    product_id = selected.id

    st.divider()
    st.subheader(selected.full_name)

    col_l, col_r = st.columns(2)
    with col_l:
        st.write(f"**Source:** {selected.source_filename or '(no source recorded)'}")
        st.write(f"**Ingested by:** {selected.ingested_by or '(unknown)'}")
    with col_r:
        st.write(f"**Ingested at:** {selected.ingested_at}")
        st.write(f"**Last updated:** {selected.updated_at}")

    button_l, button_m, button_r = st.columns(3)
    edit_mode = button_l.toggle("Edit values", key=f"edit_{product_id}")
    with button_m:
        if selected.has_pdf:
            pdf_bytes = get_pdf_bytes(product_id)
            download_name = (
                selected.source_filename
                or f"{selected.brand}_{selected.model}.pdf".replace(" ", "_")
            )
            st.download_button(
                "📥 Download PDF",
                data=pdf_bytes or b"",
                file_name=download_name,
                mime="application/pdf",
                key=f"dl_{product_id}",
            )
        else:
            st.button(
                "📥 Download PDF",
                disabled=True,
                help="No PDF stored for this monitor (was seeded from Excel, not ingested via PDF upload).",
                key=f"dl_disabled_{product_id}",
            )
    if button_r.button("🗑 Delete monitor", key=f"del_{product_id}"):
        st.session_state[f"confirm_del_{product_id}"] = True

    if st.session_state.get(f"confirm_del_{product_id}"):
        st.warning(
            f"Are you sure you want to delete **{selected.full_name}**? "
            "This cannot be undone."
        )
        c1, c2 = st.columns(2)
        if c1.button("Yes, delete it", type="primary", key=f"del_yes_{product_id}"):
            delete_product(product_id)
            del st.session_state[f"confirm_del_{product_id}"]
            st.success(f"Deleted {selected.full_name}.")
            st.rerun()
        if c2.button("Cancel", key=f"del_no_{product_id}"):
            del st.session_state[f"confirm_del_{product_id}"]
            st.rerun()
        return

    grouped = get_specs(product_id)
    if not grouped:
        st.info("This monitor has no specs in the database.")
        return

    # "Not on Philips Leaflet" is a benchmarking catch-all from the source Excel —
    # not useful when browsing a single monitor's specs, so hide it here.
    # Specs in that bucket still exist in the DB and are visible in Generate.
    BROWSE_HIDDEN_GROUPS = {"Not on Philips Leaflet"}

    for group in GROUP_NAMES:
        if group in BROWSE_HIDDEN_GROUPS:
            continue
        if group not in grouped:
            continue
        items = grouped[group]
        with st.expander(f"{group} ({len(items)} specs)"):
            if edit_mode:
                df_edit = pd.DataFrame(
                    [{"Feature": i["feature"], "Value": i["value"], "_id": i["id"]}
                     for i in items]
                )
                edited = st.data_editor(
                    df_edit,
                    use_container_width=True,
                    hide_index=True,
                    disabled=["Feature", "_id"],
                    column_config={"_id": None},
                    key=f"editor_{product_id}_{group}",
                )
                for orig, new in zip(items, edited.to_dict("records")):
                    if orig["value"] != new["Value"]:
                        update_spec_value(orig["id"], new["Value"])
                        st.toast(f"Saved: {orig['feature']} → {new['Value']}")
            else:
                df_view = pd.DataFrame(
                    [{"Feature": i["feature"], "Value": i["value"]} for i in items]
                )
                st.dataframe(df_view, use_container_width=True, hide_index=True)


# ─── Tab: Generate ─────────────────────────────────────────────────

# Templates are read fresh from disk every render — they are 3 small YAML
# files (sub-millisecond) and not caching them avoids the stale-dropdown
# bug where a deleted template still shows up until session restart.


def _safe_filename(s: str) -> str:
    return s.replace(" ", "_").replace("/", "-")


def _resolve_header_labels(headers, label_a, label_b):
    """Substitute generic 'Philips' / 'Competitor' headers with monitor names
    for in-app preview tables. Matches core._resolve_headers behaviour."""
    out = []
    for h in headers:
        h = h.replace("{philips}", label_a).replace("{competitor}", label_b)
        if h == "Philips":
            h = label_a
        elif h == "Competitor":
            h = label_b
        out.append(h)
    return out


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _render_flat_result(result, template_name: str, file_slug: str) -> None:
    """Single-workbook download for flat (legacy / Custom view) comparisons."""
    xlsx_bytes = comparison_to_excel(result.comparison, result.label_a, result.label_b)
    st.success(f"{template_name} generated.")
    safe_name = _safe_filename(
        f"benchmark_{result.label_a}_vs_{result.label_b}_{file_slug}.xlsx"
    )
    st.download_button(
        "📥 Download as Excel",
        data=xlsx_bytes,
        file_name=safe_name,
        mime=_XLSX_MIME,
        key="dl_flat",
    )
    if result.comparison.summary:
        st.markdown("### Summary")
        st.write(result.comparison.summary)


def _render_block_result(result, template_name: str, file_slug: str) -> None:
    """Block-based render — per-block narrative + downloads driven by template
    metadata (workbook vs per-block)."""
    info = result.template_info
    specs = info.output_blocks or []
    contents_by_key = {c.block_key: c for c in result.blocks}

    st.success(f"{template_name} generated.")

    if info.download_mode == "workbook":
        # R&D — one workbook for everything, downloadable up top
        xlsx_bytes = blocks_to_workbook(
            specs=specs,
            contents=result.blocks,
            summary_narrative=result.summary_narrative,
            label_a=result.label_a,
            label_b=result.label_b,
        )
        safe_name = _safe_filename(
            f"benchmark_{result.label_a}_vs_{result.label_b}_{file_slug}.xlsx"
        )
        st.download_button(
            "📥 Download workbook (all blocks)",
            data=xlsx_bytes,
            file_name=safe_name,
            mime=_XLSX_MIME,
            key="dl_workbook",
        )

    # Render every block's narrative in-app. For per_block mode, a download
    # button rides with each block; for workbook mode, it does not (the
    # top-of-page button already covers everything).
    for spec in specs:
        st.divider()
        st.subheader(spec.name)
        content = contents_by_key.get(spec.key)
        if content is None:
            st.info("No content returned for this block.")
            continue
        if content.narrative:
            st.write(content.narrative)

        if info.download_mode == "per_block":
            single_bytes = block_to_xlsx(
                spec, content, result.label_a, result.label_b
            )
            safe_block_name = _safe_filename(
                f"benchmark_{result.label_a}_vs_{result.label_b}"
                f"_{file_slug}_{spec.key}.xlsx"
            )
            st.download_button(
                f"📥 Download {spec.name} (Excel)",
                data=single_bytes,
                file_name=safe_block_name,
                mime=_XLSX_MIME,
                key=f"dl_block_{spec.key}",
            )

        if content.rows:
            resolved = _resolve_header_labels(
                spec.headers, result.label_a, result.label_b
            )
            preview_rows = [
                {h: row.get(c, "") for c, h in zip(spec.columns, resolved)}
                for row in content.rows[:10]
            ]
            st.dataframe(
                pd.DataFrame(preview_rows),
                use_container_width=True,
                hide_index=True,
            )
            if len(content.rows) > 10:
                st.caption(
                    f"Showing first 10 of {len(content.rows)} rows — "
                    "download the Excel for the full table."
                )
        else:
            st.caption("No rows in this block.")

    if info.include_summary_narrative and result.summary_narrative:
        st.divider()
        st.subheader("Recommended for next launch")
        st.write(result.summary_narrative)


def render_generate_tab():
    st.header("Generate comparison")
    st.caption(
        "Pick two monitors and a view. Marketing and custom views can pull in "
        "each side's product-page URL to enrich the analysis."
    )

    monitors = search_monitors("")
    if len(monitors) < 2:
        st.info("You need at least 2 monitors in the database to compare.")
        return

    templates = load_templates()
    if not templates:
        st.error(f"No prompt templates found in {PROMPTS_DIR.name}/.")
        return

    labels = [m.full_name for m in monitors]
    model_lookup = {m.full_name: m.model for m in monitors}
    url_lookup = {m.full_name: (m.website_url or "") for m in monitors}

    c1, c2 = st.columns(2)
    with c1:
        a_label = st.selectbox("Philips / OBM monitor", labels, index=0, key="gen_a")
    with c2:
        b_label = st.selectbox(
            "Competitor monitor", labels, index=min(1, len(labels) - 1), key="gen_b"
        )

    CUSTOM_KEY = "__custom__"
    template_options = list(templates.keys()) + [CUSTOM_KEY]

    def _template_label(k: str) -> str:
        if k == CUSTOM_KEY:
            return " Custom view (define your own)"
        return templates[k]["name"]

    template_key = st.selectbox("View", template_options, format_func=_template_label)

    # Decide before submit whether the URL block should be shown. Custom
    # view always shows URLs (they are optional). Built-in views opt in
    # via the YAML's show_url_fields flag.
    if template_key == CUSTOM_KEY:
        show_url_fields = True
        template_preview = None
    else:
        template_preview = templates[template_key]
        show_url_fields = bool(template_preview.get("show_url_fields", False))

    if template_key == CUSTOM_KEY:
        st.markdown("**Define your custom view**")
        audience = st.text_input(
            "Audience",
            placeholder="Who reads this? e.g. sales team, product team",
            key="custom_audience",
        )
        goal = st.text_area(
            "Goal",
            placeholder="What decision should this comparison enable? e.g. 'Help sales decide whether to lead with the Philips over the Dell in mid-market refresh tenders'",
            height=80,
            key="custom_goal",
        )
        other_details = st.text_area(
            "Other details (optional)",
            placeholder="Anything else — what to emphasize, what to avoid, tone, format preferences...",
            height=80,
            key="custom_other",
        )
    else:
        with st.expander("Template details"):
            st.write(
                f"**{template_preview['name']}** — "
                f"{template_preview.get('description', '')}"
            )
            if template_preview.get("groups"):
                st.write("Spec groups included: " + ", ".join(template_preview["groups"]))
            else:
                st.write("Spec groups included: all groups in the database")

    # URL inputs — only shown when the chosen view uses them. Prefilled from
    # any URL captured at ingest time so the user does not retype.
    philips_url = ""
    competitor_url = ""
    if show_url_fields:
        st.markdown(
            "**Product page URLs** &nbsp;·&nbsp; "
            "<small>optional",
            unsafe_allow_html=True,
        )
        u1, u2 = st.columns(2)
        with u1:
            philips_url = st.text_input(
                f"{a_label} URL",
                value=url_lookup.get(a_label, ""),
                key="gen_url_a",
                placeholder="https://www.philips.com/...",
            )
        with u2:
            competitor_url = st.text_input(
                f"{b_label} URL",
                value=url_lookup.get(b_label, ""),
                key="gen_url_b",
                placeholder="https://...",
            )

    # Run the agent only when Generate is clicked. The result is stashed in
    # session_state so download-button clicks (which trigger a Streamlit
    # rerun) do NOT wipe the rendered output — the page stays on the last
    # generated comparison until the user clicks Generate again.
    if st.button("Generate", type="primary", key="gen_btn"):
        if template_key == CUSTOM_KEY:
            if not audience.strip() or not goal.strip():
                st.error("Fill in **Audience** and **Goal** to generate a custom view.")
                return
            template_arg: str | dict = build_custom_template(audience, goal, other_details)
            template_name = template_arg["name"]
            file_slug = "custom"
        else:
            template_arg = template_key
            template_name = templates[template_key]["name"]
            file_slug = template_key

        urls = None
        if show_url_fields and (philips_url.strip() or competitor_url.strip()):
            urls = {
                "philips": philips_url.strip(),
                "competitor": competitor_url.strip(),
            }

        with st.status("Asking Claude...", expanded=True) as status:
            def on_step(tool_name: str, tool_input: dict) -> None:
                if tool_name == "fetch_url":
                    side = tool_input.get("side", "?")
                    if tool_input.get("ok"):
                        chars = tool_input.get("chars", 0)
                        status.write(
                            f"→ Fetched {side} URL ({chars / 1024:.1f} KB of text)"
                        )
                    else:
                        reason = tool_input.get("reason", "unknown error")
                        status.write(f"→ Skipped {side} URL — {reason}")
                elif tool_name == "get_monitor":
                    status.write(f"→ Looking up `{tool_input.get('model', '?')}`")
                elif tool_name == "list_monitors":
                    q = tool_input.get("query", "") or "(all)"
                    status.write(f"→ Searching monitors for `{q}`")
                elif tool_name == "write_comparison":
                    n = len(tool_input.get("rows", []))
                    status.write(f"→ Committing comparison ({n} rows)")
                elif tool_name == "write_block_comparison":
                    n = len(tool_input.get("blocks", []))
                    status.write(f"→ Committing {n} blocks")
                else:
                    status.write(f"→ {tool_name}({tool_input})")

            try:
                result = run_comparison_agent(
                    model_a=model_lookup[a_label],
                    model_b=model_lookup[b_label],
                    template=template_arg,
                    on_step=on_step,
                    urls=urls,
                )
            except ValueError as e:
                status.update(label="Failed", state="error")
                st.error(str(e))
                return
            except anthropic.AuthenticationError:
                status.update(label="Failed", state="error")
                st.error("Invalid API key. Check your .env file.")
                return
            except anthropic.APIError as e:
                status.update(label="Failed", state="error")
                st.error(f"API error: {e.message}")
                return
            except RuntimeError as e:
                status.update(label="Failed", state="error")
                st.error(str(e))
                return

            status.update(label="Comparison ready", state="complete")

        st.session_state["gen_result"] = {
            "result": result,
            "template_name": template_name,
            "file_slug": file_slug,
        }

    # Always render the stashed result if one exists. This is what survives
    # download-button reruns and tab switches within the session.
    stored = st.session_state.get("gen_result")
    if stored:
        stashed_result = stored["result"]
        if stashed_result.kind == "flat":
            _render_flat_result(
                stashed_result, stored["template_name"], stored["file_slug"]
            )
        else:
            _render_block_result(
                stashed_result, stored["template_name"], stored["file_slug"]
            )


# ─── Main ──────────────────────────────────────────────────────────

st.set_page_config(page_title="Monitor Benchmark", layout="wide")
st.title("Monitor Benchmark")

if not DB_PATH.exists():
    st.error(f"No database at {DB_PATH.name}. Run `python seed_db.py` first.")
    st.stop()

migrate_db()

tabs = st.tabs(["📥 Ingest", "🔍 Browse", "📊 Generate"])
with tabs[0]:
    render_ingest_tab()
with tabs[1]:
    render_browse_tab()
with tabs[2]:
    render_generate_tab()