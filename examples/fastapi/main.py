from fastapi import FastAPI

from bootstrap import bootstrap
from presentation.routes import router

app = FastAPI()
app.include_router(router)
bootstrap(app)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
