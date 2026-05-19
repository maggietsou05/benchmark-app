# Benchmark App

A Streamlit app that compares two monitors using the Claude API and generates
role-specific comparison tables (R&D, Marketing, etc.). Spec data lives in a
local SQLite database. View templates live as YAML files in `prompts/`.

## First-time setup

You only do these steps once.

1. **Install Python 3.11 or later** from https://python.org. Confirm:

   ```
   python --version
   ```

2. **Create a virtual environment and install dependencies.** From inside
   the `benchmark_app/` folder:

   ```
   python -m venv venv
   venv\Scripts\activate          (Windows)
   source venv/bin/activate       (macOS / Linux)
   pip install -r requirements.txt
   ```

3. **Set your API key.** Copy `.env.example` to `.env` and paste your key:

   ```
   copy .env.example .env         (Windows)
   cp .env.example .env           (macOS / Linux)
   ```

   Then open `.env` and replace the placeholder with your real key.
   Get a key at https://console.anthropic.com → API Keys.

4. **Seed the database.** This reads
   `../example/Benchmark leaflets pilot ClaudeAI.xlsx` and creates a fresh
   `benchmark.db` with three monitors (Philips 27B2U6903, Dell U2725QE,
   and a Lenovo P27Q-40 placeholder):

   ```
   python seed_db.py
   ```

   Re-run this any time you want to rebuild the DB from scratch.

## Running the app

```
streamlit run app.py
```

Your browser opens at http://localhost:8501. Pick two monitors, pick a view
template, click **Generate**.

## Using from Claude Desktop (MCP)

`benchmark_mcp.py` exposes the same operations as a Model Context Protocol
server, so you can chat with Claude Desktop and have it call benchmark tools
in a loop — *"find the closest Philips response to the Dell U2725QE and
draft an R&D comparison"* — instead of clicking through the Streamlit tabs.
This path bills against your Claude subscription, not the API.

Tools exposed: `list_monitors`, `get_monitor`, `list_templates_tool`,
`compare_monitors`, `export_comparison_excel`.

1. **Install the new dependency** (re-run inside the activated venv):

   ```
   pip install -r requirements.txt
   ```

2. **Register the server with Claude Desktop.** Open the config file at
   `%APPDATA%\Claude\claude_desktop_config.json` on Windows
   (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS)
   and add a `benchmark` entry under `mcpServers`. Use absolute paths to the
   venv Python and to `benchmark_mcp.py`:

   ```json
   {
     "mcpServers": {
       "benchmark": {
         "command": "C:\\Users\\maggie.tsou\\OneDrive - TPV Technology Limited\\Desktop\\leaflet\\benchmark_app\\venv\\Scripts\\python.exe",
         "args": ["C:\\Users\\maggie.tsou\\OneDrive - TPV Technology Limited\\Desktop\\leaflet\\benchmark_app\\benchmark_mcp.py"]
       }
     }
   }
   ```

   Backslashes must be doubled in JSON. If the file already has other servers,
   add `"benchmark": {...}` alongside them inside `mcpServers`.

3. **Restart Claude Desktop.** After relaunch you should see the benchmark
   tools listed in the connectors / tools menu.

4. **Verify it works.** Try a prompt like:

   > List the monitors in the benchmark database and tell me which two
   > would make the most interesting R&D comparison.

   Claude should call `list_monitors` and then reason about the result.

## Project layout

```
benchmark_app/
├── app.py                      Streamlit UI
├── core.py                     Pure operations — used by app.py and the MCP server
├── benchmark_mcp.py            MCP server for Claude Desktop / Agent SDK clients
├── seed_db.py                  Builds benchmark.db from the Excel file
├── benchmark.db                SQLite DB (generated, gitignored)
├── prompts/
│   ├── rd_deepdive.yaml        View template — R&D engineering audience
│   └── marketing_onepager.yaml View template — non-technical marketing audience
├── requirements.txt
├── .env                        YOUR API key — DO NOT SHARE OR COMMIT
└── .env.example                Template for .env
```

## Adding a new view

1. Copy any YAML file in `prompts/` (e.g. `marketing_onepager.yaml`)
2. Edit `name`, `description`, `groups`, and `system_prompt`
3. Save with a new filename — the app picks it up on next reload

The `groups` list controls which spec groups Claude sees. Drop the `groups`
key entirely to include every group in the database.

## Cost

Both the ingest call (PDF → specs) and the comparison "Generate" click
use `claude-sonnet-4-6`, configured in `core.py`. Rough costs:

- **Ingest a leaflet**: ~$0.05–0.15, depending on page count and image size
- **Generate a comparison**: ~$0.05–0.20, depending on template size and spec count

Ingest is a one-time cost per monitor; comparisons are repeated. Set a
usage cap in the Anthropic console to limit blast radius.

## Sharing with a tester

The tester needs to repeat the **First-time setup** steps on their own
machine:

1. Python 3.11+ installed
2. The `benchmark_app/` folder copied to their machine
3. Their own `venv` and `pip install -r requirements.txt`
4. Their own `.env` file (you can share your API key with them privately —
   put a usage cap on it first)
5. `python seed_db.py` to build their local DB
6. `streamlit run app.py` to run

Each tester runs their own copy locally. There is no shared server yet.

## Troubleshooting

- **"No database at benchmark.db"** — run `python seed_db.py` from inside
  `benchmark_app/`
- **"Invalid API key"** — check `.env`. Key should start with `sk-ant-api03-`
- **Excel locked** — close the Excel file before running `seed_db.py`
- **Module not found** — make sure your virtual env is activated
  (you should see `(venv)` in your terminal prompt)
