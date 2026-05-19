"""
MMD Monitor Benchmark — visual styling.

Call inject_styles() once near the top of every Streamlit page, after
st.set_page_config but before any UI elements.

NOTE ON FRAGILITY: This CSS targets Streamlit's internal class names and
data-testid attributes. These can change between Streamlit versions. If the
app suddenly looks broken after a Streamlit upgrade, the selectors below
are the first place to check. Pin your streamlit version in requirements
to avoid surprise breakage.

Tested against streamlit 1.40+. Adjust selectors if your version differs.
"""

import streamlit as st


# MMD brand palette — derived from the logo wordmark + monitor stack.
# Blue is primary; the others are reserved for status/categorical use only.
BRAND_BLUE = "#1B92E0"        # logo wordmark, primary actions
BRAND_BLUE_DARK = "#0C5A8C"   # text on tinted blue surfaces
BRAND_BLUE_TINT = "#F5FAFE"   # table header background, hover tints
BRAND_BLUE_BORDER = "#B5D4F4" # dashed borders, decorative dividers

# Keep these in reserve for badges/status, not chrome:
ACCENT_GREEN = "#7ABF3C"      # success / "new"
ACCENT_AMBER = "#F5A623"      # warning / "needs review"
ACCENT_CORAL = "#E8553A"      # error / "deprecated"


def inject_styles() -> None:
    """Inject all custom CSS for the Monitor Benchmark app."""
    st.markdown(
        f"""
        <style>
        /* ==========================================================
           BASE — typography and page surface
           ========================================================== */

        /* Strip Streamlit's default top padding for a tighter header */
        .block-container {{
            padding-top: 2rem;
            padding-bottom: 4rem;
            max-width: 1100px;
        }}

        /* H1 used for page titles ("Ingest a PDF leaflet", etc.) */
        h1 {{
            font-weight: 500 !important;
            font-size: 1.75rem !important;
            letter-spacing: -0.01em;
            margin-bottom: 0.25rem !important;
        }}

        h2 {{
            font-weight: 500 !important;
            font-size: 1.25rem !important;
        }}

        /* Body text muted by default — pairs with the editorial direction */
        .stMarkdown p {{
            color: #4a4a4a;
            line-height: 1.6;
        }}

        /* ==========================================================
           TABS — the Ingest / Browse / Generate row
           ========================================================== */
        /* Streamlit's tabs render as: div[data-baseweb="tab-list"] containing
           button[data-baseweb="tab"] elements. The active tab gets
           aria-selected="true". We restyle the underline to MMD blue. */

        div[data-baseweb="tab-list"] {{
            gap: 28px;
            border-bottom: 0.5px solid rgba(0,0,0,0.08);
        }}

        button[data-baseweb="tab"] {{
            font-size: 13px !important;
            font-weight: 400 !important;
            color: #6a6a6a !important;
            padding: 8px 0 12px !important;
        }}

        button[data-baseweb="tab"][aria-selected="true"] {{
            color: {BRAND_BLUE_DARK} !important;
            font-weight: 500 !important;
        }}

        /* The underline is a separate element; Streamlit renders it as a
           highlight bar inside the tab list. This selector is the most
           fragile part of the file — if the underline color doesn't change
           after a Streamlit upgrade, inspect the DOM and update. */
        div[data-baseweb="tab-highlight"] {{
            background-color: {BRAND_BLUE} !important;
            height: 1.5px !important;
        }}

        /* ==========================================================
           BUTTONS — primary actions (Upload, Save, etc.)
           ========================================================== */
        /* Streamlit's primary button uses kind="primary"; we restyle that
           to MMD blue. Secondary buttons stay neutral. */

        button[kind="primary"] {{
            background-color: {BRAND_BLUE} !important;
            border-color: {BRAND_BLUE} !important;
            color: white !important;
            font-weight: 500 !important;
            font-size: 13px !important;
        }}

        button[kind="primary"]:hover {{
            background-color: {BRAND_BLUE_DARK} !important;
            border-color: {BRAND_BLUE_DARK} !important;
        }}

        /* ==========================================================
           FILE UPLOADER — the dashed drop zone
           ========================================================== */
        /* This is one of the cleanest Streamlit components to restyle:
           the outer wrapper has a stable data-testid. */

        [data-testid="stFileUploader"] section {{
            border: 1px dashed {BRAND_BLUE_BORDER} !important;
            background-color: white !important;
            border-radius: 8px !important;
            padding: 1.25rem !important;
        }}

        [data-testid="stFileUploader"] section:hover {{
            border-color: {BRAND_BLUE} !important;
            background-color: {BRAND_BLUE_TINT} !important;
        }}

        /* The upload button inside the dropzone */
        [data-testid="stFileUploader"] button {{
            background-color: {BRAND_BLUE} !important;
            color: white !important;
            border: none !important;
            font-size: 13px !important;
        }}

        /* ==========================================================
           INPUTS — text fields, selects
           ========================================================== */

        [data-testid="stTextInput"] input,
        [data-testid="stSelectbox"] > div > div {{
            border-radius: 6px !important;
            border-color: rgba(0,0,0,0.12) !important;
            font-size: 14px !important;
        }}

        [data-testid="stTextInput"] input:focus {{
            border-color: {BRAND_BLUE} !important;
            box-shadow: 0 0 0 2px {BRAND_BLUE_TINT} !important;
        }}

        /* Field labels — smaller and muted, matching the mockup */
        [data-testid="stWidgetLabel"] {{
            font-size: 12px !important;
            color: #6a6a6a !important;
            font-weight: 400 !important;
            letter-spacing: 0.01em;
        }}

        /* ==========================================================
           DATAFRAME — the monitor table
           ========================================================== */
        /* CAVEAT: st.dataframe is a React component (Glide Data Grid).
           You can only restyle the outer container. Cell-level styling
           (e.g. monospace for the Model column, right-alignment for
           numbers) is NOT possible from CSS.

           If you need that level of control, switch to st.markdown with
           an HTML table — see render_table() helper at the bottom of
           this file. The tradeoff is you lose Streamlit's built-in
           sorting and filtering. */

        [data-testid="stDataFrame"] {{
            border: 0.5px solid rgba(0,0,0,0.08) !important;
            border-radius: 8px !important;
            overflow: hidden;
        }}
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_header(active_tab: str = "ingest") -> None:
    """
    Render the MMD branded header with logo lockup.

    Use this above your tabs on every page. The 'active_tab' arg is unused
    here (Streamlit's own tabs handle the underline) — kept for symmetry
    if you later switch to custom nav.
    """
    st.markdown(
        f"""
        <div style="display: flex; align-items: center; gap: 12px;
                    padding-bottom: 1rem;
                    border-bottom: 0.5px solid rgba(0,0,0,0.08);
                    margin-bottom: 1.5rem;">
            <div style="width: 32px; height: 32px; border-radius: 7px;
                        background: {BRAND_BLUE}; display: flex;
                        align-items: center; justify-content: center;
                        color: white; font-weight: 500; font-size: 13px;">
                MB
            </div>
            <div>
                <p style="font-size: 11px; color: #999; margin: 0;
                          letter-spacing: 0.06em; text-transform: uppercase;">
                    MMD Europe
                </p>
                <h2 style="margin: 0; font-size: 16px; font-weight: 500;">
                    Monitor benchmark
                </h2>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_table_html(df) -> None:
    """
    Optional: render a pandas DataFrame as styled HTML instead of using
    st.dataframe. Use this when you need cell-level styling (monospace
    SKUs, right-aligned numbers, formatted dates).

    Tradeoff: loses sorting, search, and pagination. Best for tables
    under ~50 rows that you've already filtered server-side.
    """
    rows_html = ""
    for _, row in df.iterrows():
        rows_html += f"""
        <tr style="border-top: 0.5px solid rgba(0,0,0,0.06);">
            <td style="padding: 12px 16px;">{row.get('brand', '')}</td>
            <td style="padding: 12px 16px; font-family: ui-monospace,
                       SFMono-Regular, monospace; font-size: 12px;">
                {row.get('model', '')}
            </td>
            <td style="padding: 12px 16px; text-align: right;
                       font-variant-numeric: tabular-nums;">
                {row.get('specs', '')}
            </td>
            <td style="padding: 12px 16px; color: #6a6a6a;
                       font-variant-numeric: tabular-nums;">
                {row.get('last_updated', '')}
            </td>
        </tr>
        """

    st.markdown(
        f"""
        <div style="background: white;
                    border: 0.5px solid rgba(0,0,0,0.08);
                    border-radius: 8px; overflow: hidden;">
        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
            <thead>
                <tr style="background: {BRAND_BLUE_TINT};">
                    <th style="text-align: left; padding: 10px 16px;
                               font-weight: 500; font-size: 11px;
                               color: {BRAND_BLUE_DARK};
                               letter-spacing: 0.04em;
                               text-transform: uppercase;">Brand</th>
                    <th style="text-align: left; padding: 10px 16px;
                               font-weight: 500; font-size: 11px;
                               color: {BRAND_BLUE_DARK};
                               letter-spacing: 0.04em;
                               text-transform: uppercase;">Model</th>
                    <th style="text-align: right; padding: 10px 16px;
                               font-weight: 500; font-size: 11px;
                               color: {BRAND_BLUE_DARK};
                               letter-spacing: 0.04em;
                               text-transform: uppercase;">Specs</th>
                    <th style="text-align: left; padding: 10px 16px;
                               font-weight: 500; font-size: 11px;
                               color: {BRAND_BLUE_DARK};
                               letter-spacing: 0.04em;
                               text-transform: uppercase;">Last updated</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
        </table>
        </div>
        """,
        unsafe_allow_html=True,
    )
