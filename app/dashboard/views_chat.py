"""Conversations page (§11): per-chat transcripts including tool calls, for debugging. Chats the
account reader reads (§10.7) are listed separately: their ids can equal a bot DM's."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app.dashboard import queries
from app.dashboard.core import DashboardDeps, render
from app.db.repos import messages as messages_repo


def register(router: APIRouter, deps: DashboardDeps) -> None:
    async def _reader_labels() -> dict[int, str]:
        rows = await deps.db.read(
            lambda c: c.execute("SELECT peer_id, label FROM reader_chats").fetchall()
        )
        return {r["peer_id"]: r["label"] for r in rows}

    def _chat_name(chat_id: int, source: str, labels: dict[int, str]) -> str:
        if source == messages_repo.READER:
            return f"Reader: {labels.get(chat_id, chat_id)}"
        if chat_id == deps.group_id():
            return "Group"
        user = next((u for u in deps.users if u.telegram_id == chat_id), None)
        return f"DM with {user.display_name}" if user else f"Chat {chat_id}"

    @router.get("/conversations", response_class=HTMLResponse)
    async def conversations(request: Request) -> Response:
        rows = await queries.chats(deps.db)
        labels = await _reader_labels()
        return render(
            request,
            deps,
            "conversations.html",
            chats=[
                (
                    r["chat_id"],
                    _chat_name(r["chat_id"], r["source"], labels),
                    r["n"],
                    r["last"],
                    r["source"],
                )
                for r in rows
            ],
        )

    @router.get("/conversations/{chat_id}", response_class=HTMLResponse)
    async def conversation(
        request: Request, chat_id: int, limit: int = 200, source: str = messages_repo.BOT
    ) -> Response:
        src: messages_repo.Source = (
            messages_repo.READER if source == messages_repo.READER else messages_repo.BOT
        )
        names = {u.id: u.display_name for u in deps.users}
        group = deps.group_id()
        topic_names = (
            {t.thread_id: t.name for t in await deps.topics.known(group)}
            if group is not None and chat_id == group and src == messages_repo.BOT
            else {}
        )
        lines = await queries.transcript(
            deps.db, chat_id, names, min(max(limit, 20), 2000), source=src
        )
        return render(
            request,
            deps,
            "conversation.html",
            chat_id=chat_id,
            name=_chat_name(chat_id, src, await _reader_labels()),
            lines=lines,
            topic_names=topic_names,
            limit=limit,
            source=src,
        )
