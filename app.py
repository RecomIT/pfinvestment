"""Vercel entry point: Vercel loads the `app` object from this file (app.py at the project root).

The data lives in the Neon Postgres database connected to the Vercel project (DATABASE_URL).
If the database is not connected yet, every page explains what to do instead of crashing.
"""
import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from web.cloud import create_cloud_app, database_url

SETUP_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Setup needed</title>
<style>body{font:15px/1.5 'Segoe UI',system-ui,sans-serif;max-width:640px;margin:60px auto;padding:0 20px;color:#1d2433}
h1{font-size:20px}code{background:#eef0f4;padding:1px 5px;border-radius:3px}</style></head><body>
<h1>Bond Amortization: the database is not connected yet</h1>
<p>{reason}</p>
<ol><li>In the Vercel dashboard open this project &rarr; <b>Storage</b> &rarr; <b>Create Database</b> &rarr; <b>Neon</b>
(region <b>Singapore</b>) and connect it to this project.</li>
<li>Open <b>Deployments</b> &rarr; the latest deployment &rarr; <b>&hellip;</b> &rarr; <b>Redeploy</b>.</li></ol>
<p>See <code>DEPLOY.md</code> for the full steps.</p></body></html>"""


def _setup_app(reason: str) -> FastAPI:
    fallback = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @fallback.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def setup_needed(path: str):
        return HTMLResponse(SETUP_PAGE.replace("{reason}", reason), status_code=503,
                            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})

    return fallback


def _build_app() -> FastAPI:
    if not database_url():
        return _setup_app("The DATABASE_URL setting is missing.")
    try:
        return create_cloud_app()
    except Exception as exc:                                   # wrong password, database paused, ...
        logging.getLogger("bond_app").exception("could not connect to the database")
        return _setup_app("The app could not connect to the database (" + type(exc).__name__ +
                          "). Check that the Neon database is connected and running.")


# Vercel looks for this exact top-level line: "app = ..." (not inside an if/try block).
app = _build_app()
