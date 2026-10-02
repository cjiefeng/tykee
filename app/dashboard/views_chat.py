"""Conversations page (§11): per-chat transcripts including tool calls, for debugging."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries
from app.dashboard.core import DashboardDeps, render


def register(router: APIRouter, deps: DashboardDeps) -> None:
    def _chat_name(chat_id: int) -> str:
        if chat_id == deps.group_id():
            return "Group"
        user = next((u for u in deps.users if u.telegram_id == chat_id), None)
        return f"DM with {user.display_name}" if user else f"Chat {chat_id}"

    @router.get("/conversations", response_class=HTMLResponse)
    async def conversations(request: Request) -> Response:
        rows = await queries.chats(deps.db)
        return render(
            request,
            deps,
            "conversations.html",
            chats=[(r["chat_id"], _chat_name(r["chat_id"]), r["n"], r["last"]) for r in rows],
        )

    @router.get("/conversations/{chat_id}", response_class=HTMLResponse)
    async def conversation(request: Request, chat_id: int, limit: int = 200) -> Response:
        names = {u.id: u.display_name for u in deps.users}
        group = deps.group_id()
        topic_names = (
            {t.thread_id: t.name for t in await deps.topics.known(group)}
            if group is not None and chat_id == group
            else {}
        )
        lines = await queries.transcript(deps.db, chat_id, names, min(max(limit, 20), 2000))
        return render(
            request,
            deps,
            "conversation.html",
            chat_id=chat_id,
            name=_chat_name(chat_id),
            lines=lines,
            topic_names=topic_names,
            limit=limit,
        )
