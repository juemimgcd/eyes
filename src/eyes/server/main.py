import uvicorn

from eyes.server.api.app import create_app
from eyes.server.config import Settings


def main():
    settings = Settings()
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, proxy_headers=False)
