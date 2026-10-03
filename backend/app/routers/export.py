"""The spreadsheet export: a company's ownership as an .ods, one sheet per
chapter — built on the server so it holds everything, not what a browser
happened to load. See app/export_ods.py."""
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Query, Response

from app.export_ods import MIME, ExportOptions, build_workbook

router = APIRouter(prefix="/export", tags=["Export"])


@router.get("/entity/{entity_id:path}", response_class=Response,
            responses={200: {"content": {MIME: {}}, "description": "The .ods file"},
                       404: {"description": "Entity not found"}})
def export_entity(
    entity_id: str,
    as_of: Annotated[str | None, Query(
        pattern=r"^\d{4}-\d{2}-\d{2}$", max_length=10,
        description="Owners, subsidiaries and roles as they stood on this date (ISO, inclusive). "
                    "Omitted: the present.")] = None,
    all_levels: Annotated[bool, Query(
        description="The whole tree below the company (with Level and Parent columns) "
                    "instead of its direct subsidiaries.")] = False,
    min_stake: Annotated[float, Query(
        ge=0, le=100, description="Leave out holdings whose STATED stake is below this percent; "
                                  "unstated stakes always stay (the graph's own rule).")] = 0.0,
    min_stake_exclusive: Annotated[bool, Query(
        description="Strictly above min_stake instead of at least.")] = False,
    link: Annotated[str | None, Query(
        max_length=500, pattern=r"^https?://",
        description="The live graph's URL, written into the Overview sheet.")] = None,
):
    """One company as an OpenDocument spreadsheet: Overview, Owners, Subsidiaries,
    Roles, Timeline, Sources, Claims — every row, not the profile's per-page cap.
    The same readers as the profile, the tree and the history, so the sheets say
    what the panel says."""
    filename, data = build_workbook(entity_id, ExportOptions(
        as_of=as_of, all_levels=all_levels, min_stake=min_stake,
        min_stake_exclusive=min_stake_exclusive, link=link))
    ascii_name = filename.encode("ascii", "replace").decode().replace('"', "'")
    return Response(content=data, media_type=MIME, headers={
        "Content-Disposition": f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}",
        "Cache-Control": "no-store",
    })
