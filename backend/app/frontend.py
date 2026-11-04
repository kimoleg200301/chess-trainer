"""Serve client-side routes when the API also hosts the built frontend."""

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class SPAStaticFiles(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        parts = path.replace("\\", "/").split("/")
        # Missing assets and API endpoints must retain their real 404 responses.
        # In particular, never turn a failed traversal or hidden-file lookup into
        # an application page. StaticFiles still validates every file it serves.
        app_route = parts[0] not in {"api", "assets", "pieces"} and not any("." in part for part in parts)
        try:
            response = await super().get_response(path, scope)
        except HTTPException as exc:
            if exc.status_code != 404 or not app_route:
                raise
        else:
            if response.status_code != 404 or not app_route:
                return response
        return await super().get_response("index.html", scope)
