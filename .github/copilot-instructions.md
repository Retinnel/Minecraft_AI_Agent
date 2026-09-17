# Copilot Instructions

## Build, run, and validation commands

### Python backend

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Windows helpers already exist:

```powershell
.\start_server.bat
```

### Fabric mod

```powershell
Set-Location mod\minecraft-mod
.\gradlew.bat runClient
.\gradlew.bat build
```

Windows helper:

```powershell
.\start_minecraft_client.bat
```

### Tests and lint

Python unit tests are checked in:

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q --ignore=tests/test_ui_smoke.py
```

There is still no lint configuration and no Java/Fabric test suite checked in. Runtime validation is also manual by running the backend and mod together, then checking:

- `http://127.0.0.1:8000/admin`
- `http://127.0.0.1:8000/admin/responses`
- `http://127.0.0.1:8000/admin/wiki`
- `http://127.0.0.1:8000/admin/analytics`

Runtime logs are written to `logs\server.log` and `logs\minecraft_client.log` by the batch launchers.

## High-level architecture

- `main.py` creates the FastAPI app, registers all routers from `api\`, serves admin HTML from `static\`, and serves browser JavaScript from `frontend\`.
- The backend is built around shared module-level state rather than a database:
  - `api\logs.py` owns the in-memory `logs_db`
  - `api\settings.py` owns `current_settings` backed by `settings.json`
  - `api\advice.py` owns the singleton `groq_client`, RP response history, and WebSocket subscribers
  - `api\analytics.py` and `api\wiki.py` import those shared objects instead of creating their own copies
- `llm_config.json` is the source of truth for the active model, provider (`groq` or `ollama`), model parameters, and prompt templates. The `/api/rp/models/switch` endpoint persists changes back into that file and recreates the client.
- The Fabric side is client-only. `mod\minecraft-mod\src\client\java\com\example\ExampleModClient.java` loads config, creates `AssistantRuntime`, registers the client tick loop, and forwards player chat into the assistant flow.
- `AssistantRuntime` is the gameplay bridge: it emits structured events such as `startup`, `state`, `low_health`, `hunger_low`, `night`, `near_hostile`, and `death`, polls `/api/settings/`, polls `/api/rp/`, and shows responses in Minecraft chat.
- `HttpAssistantClient` is the only HTTP layer inside the mod. It hardcodes the backend contract used by the runtime: `POST /api/logs/`, `GET /api/settings/`, `GET /api/rp/`, and `POST /api/rp/chat`.
- The admin UI is plain HTML + CSS + vanilla JS. There is no frontend bundler. Each page in `static\` directly loads a matching script from `frontend\js\`, and live updates come from WebSockets (`/api/logs/ws`, `/api/rp/ws`).
- Item and block resolution for `/give` is now backed by a local structured catalog in `data\minecraft_catalog.json` through `api\minecraft_catalog.py`. Keep item knowledge there rather than expanding hardcoded alias maps inside `api\advice.py`.

## Key conventions

- Preserve the shared-state pattern when changing backend behavior. If you create a second `GroqClient`, a second log store, or a second settings object, other routers will not see the same state.
- Keep player-facing assistant text in Russian and short. Prompt edits belong in `llm_prompts.json`, and the placeholders `{context}` and `{player_message}` must stay intact.
- LLM context is intentionally capped:
  - RP tips and chat replies use only the last 5 logs
  - Analytics uses aggregated counters plus a reduced timeline
  Avoid sending the full `logs_db` to the model unless you are intentionally redesigning that behavior.
- Route spelling matters. The mod and browser code hardcode the current API paths, including trailing slashes on `/api/logs/`, `/api/settings/`, and `/api/rp/`, while `/api/rp/chat`, `/api/rp/models/*`, and `/api/wiki/craft` do not use a trailing slash.
- The backend persists configuration to files, not a database. `settings.json` and `llm_config.json` survive restarts; `logs_db` and response history do not.
- Server and mod timing use different units. `analysis_interval` in `settings.json` is milliseconds, but the Fabric runtime converts it into Minecraft ticks inside `AssistantRuntime`.
- `api\advice.py` contains behavior-critical throttling and deduplication (`_processed_log_ids`, `_min_interval_seconds`, `_has_situation_changed()`). Treat that as product logic, not incidental cleanup code.
- For `/give` requests, prefer deterministic catalog lookup over LLM-only resolution. The LLM can help with intent parsing, but canonical item/block ids should come from the local catalog whenever possible.
- If you add or rename admin pages, update the navigation links in each static HTML page manually. There is no shared layout or template partial for the admin UI.
- If you change provider support or add a model, update both `llm_config.json` and `api\groq_client.py`; model switching depends on both sides matching.
