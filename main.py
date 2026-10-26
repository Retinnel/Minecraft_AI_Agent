from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pathlib import Path
from api import logs, settings
from api import advice
from api import analytics
from api import challenges
from api import campaign
from api import player_state
from api import wiki
from api import compare
from api.llm_config_manager import get_llm_config

app = FastAPI(title="AI Assistant Admin")

# Initialize LLM config
llm_config = get_llm_config()
print(f"[LLM Config] Loaded model: {llm_config.model_type}")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    print("[VALIDATION ERROR]", request.method, request.url)
    print(exc.errors())
    try:
        print("[VALIDATION ERROR BODY]", exc.body)
    except Exception:
        pass
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

# Подключение роутеров
app.include_router(logs.router)
app.include_router(settings.router)
app.include_router(advice.router)
app.include_router(analytics.router)
app.include_router(challenges.router)
app.include_router(campaign.router)
app.include_router(player_state.router)
app.include_router(wiki.router)
app.include_router(compare.router)

# Статические файлы
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/frontend", StaticFiles(directory="frontend"), name="frontend")

BASE_DIR = Path(__file__).resolve().parent


@app.get("/admin")
async def admin_logs_page():
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/admin/settings")
async def admin_settings_page():
    return FileResponse(BASE_DIR / "static" / "settings.html")

@app.get("/admin/responses")
async def admin_responses_page():
    return FileResponse(BASE_DIR / "static" / "responses.html")

@app.get("/admin/analytics")
async def admin_analytics_page():
    return FileResponse(BASE_DIR / "static" / "analytics.html")

@app.get("/admin/player-state")
async def admin_player_state_page():
    return FileResponse(BASE_DIR / "static" / "player_state.html")


@app.get("/admin/challenges")
async def admin_challenges_page():
    return FileResponse(BASE_DIR / "static" / "challenges.html")

@app.get("/admin/wiki")
async def admin_wiki_page():
    return FileResponse(BASE_DIR / "static" / "wiki.html")

@app.get("/compare")
async def compare_page():
    """Страница сравнения конфигураций — демонстрационный материал к защите."""
    return FileResponse(BASE_DIR / "static" / "compare.html")


@app.get("/dashboard")
async def dashboard_page():
    return FileResponse(BASE_DIR / "static" / "dashboard.html")

@app.get("/steam")
async def steam_crafting():
    return FileResponse(BASE_DIR / "static" / "steam.html")

@app.get("/api/recipes")
async def get_recipes():
    """Get all crafting recipes from JSON database"""
    from api.minecraft_recipes import get_all_recipes
    recipes = get_all_recipes()
    return {"recipes": recipes}

@app.get("/")
async def root():
    return FileResponse(BASE_DIR / "static" / "dashboard.html")
