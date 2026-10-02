"""Memory page (§11): vault tree, hybrid search, view/edit/delete/pin notes, and the inbox."""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, Response

from app.brain import notes as nt
from app.brain.notes import PathError
from app.brain.store import NoteError
from app.dashboard import queries
from app.dashboard.core import DashboardDeps, back, render


def _note_url(path: str) -> str:
    return f"/memory/note?path={quote(path, safe='/')}"


def register(router: APIRouter, deps: DashboardDeps) -> None:
    @router.get("/memory", response_class=HTMLResponse)
    async def memory(request: Request, q: str = "") -> Response:
        # The admin sees everyone's notes (§12), so search across all owners.
        hits = (
            await deps.memory.search(q, asker=deps.users[0].slug, scope="both", k=12) if q else []
        )
        return render(
            request,
            deps,
            "memory.html",
            notes=await queries.notes(deps.db),
            q=q,
            hits=hits,
            pending=await queries.inbox(deps.db, "pending"),
        )

    @router.get("/memory/note", response_class=HTMLResponse)
    async def note(request: Request, path: str) -> Response:
        try:
            rel = nt.normalise_rel(path)
        except PathError as e:
            return back(request, "/memory", str(e), "error")
        abs_path = nt.resolve_in_vault(deps.store.root, rel)
        text = abs_path.read_text(encoding="utf-8") if abs_path.is_file() else None
        if text is None:
            return back(request, "/memory", f"No note at {rel}.", "error")
        try:
            parsed = nt.parse(text)
        except Exception:  # broken frontmatter: still show the raw text so it can be fixed
            parsed = None
        return render(request, deps, "note.html", path=rel, text=text, note=parsed)

    @router.post("/memory/note")
    async def save_note(request: Request, path: str = Form(...), text: str = Form(...)) -> Response:
        try:
            rel = await deps.store.write_raw(path, text)
        except (PathError, NoteError) as e:
            return back(request, _note_url(path), str(e), "error")
        return back(request, _note_url(rel), "Saved and reindexed.")

    @router.post("/memory/note/pin")
    async def pin(request: Request, path: str = Form(...), pinned: str = Form("")) -> Response:
        try:
            await deps.store.set_pinned(path, pinned == "on")
        except (PathError, NoteError) as e:
            return back(request, "/memory", str(e), "error")
        return back(request, _note_url(path), "Pinned." if pinned == "on" else "Unpinned.")

    @router.post("/memory/note/delete")
    async def delete(request: Request, path: str = Form(...)) -> Response:
        try:
            removed = await deps.store.delete(path)
        except PathError as e:
            return back(request, "/memory", str(e), "error")
        return back(request, "/memory", f"Deleted {path}." if removed else "Nothing to delete.")

    @router.get("/memory/inbox", response_class=HTMLResponse)
    async def inbox(request: Request, status: str = "pending") -> Response:
        if status not in ("pending", "approved", "rejected"):
            status = "pending"
        return render(
            request, deps, "inbox.html", items=await queries.inbox(deps.db, status), status=status
        )

    @router.post("/memory/inbox/{item_id}")
    async def decide(request: Request, item_id: int, action: str = Form(...)) -> Response:
        admin = next((u for u in deps.users if u.is_admin), None)
        try:
            item = await deps.memory.decide(
                item_id, approve=action == "approve", user_id=admin.id if admin else None
            )
        except Exception as e:
            return back(request, "/memory/inbox", f"Couldn't apply: {e}", "error")
        if request.headers.get("hx-request"):
            return render(request, deps, "_inbox_done.html", item=item)
        return back(request, "/memory/inbox", f"Item {item_id}: {item.status}.")
