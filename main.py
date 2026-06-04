from __future__ import annotations

import uvicorn

from app.api import create_app
from app.settings import HOST, PORT


app = create_app()


if __name__ == "__main__":
    uvicorn.run("main:app", host=HOST, port=PORT, reload=False, access_log=False)
