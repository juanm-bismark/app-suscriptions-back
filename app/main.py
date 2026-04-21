from fastapi import FastAPI

from app.routers import auth, companies, me, users

app = FastAPI(title="App Suscripciones API", version="1.0.0")

app.include_router(auth.router)
app.include_router(me.router)
app.include_router(users.router)
app.include_router(companies.router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
