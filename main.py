import json
import os
from datetime import datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.db import SessionLocal, get_db
from database.models import Conversation, Message

load_dotenv()

RODIUMAI_URL = "https://api.rodiumai.io/v1/chat/completions"
RODIUMAI_API_KEY = os.environ["RODIUMAI_API_KEY"]
PREVIEW_LENGTH = 60

# Roles the LLM understands. Custom roles stored in the DB stay out of its prompt.
LLM_ROLES = {"user", "assistant"}
NOTIFICATION_ROLE = "system-notification"
NOTE_ROLE = "note"
NOTIFICATION_EVERY = 10
NOTIFICATION_TEXT = "Notification système : Une dizaine de messages écrits."

ALLOWED_MODELS = [
    m.strip()
    for m in os.getenv(
        "ALLOWED_MODELS",
        "openai/gpt-4o-mini,anthropic/claude-sonnet-4-5-20250929",
    ).split(",")
    if m.strip()
]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", ALLOWED_MODELS[0])
if DEFAULT_MODEL not in ALLOWED_MODELS:
    ALLOWED_MODELS.insert(0, DEFAULT_MODEL)

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
SYSTEM_PROMPT = (PROMPTS_DIR / "system.md").read_text(encoding="utf-8")

CORS_ORIGINS = [
    o.strip()
    for o in os.getenv("CORS_ORIGINS", "http://localhost:5173").split(",")
    if o.strip()
]

app = FastAPI(title="Study Buddy Chatbot")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ConversationResponse(BaseModel):
    conversation_id: int


class ConversationSummary(BaseModel):
    id: int
    created_at: datetime
    preview: str | None


class ChatRequest(BaseModel):
    conversation_id: int
    message: str
    model: str | None = None


class NoteRequest(BaseModel):
    content: str = Field(min_length=1)


class NoteResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


class ModelsResponse(BaseModel):
    models: list[str]
    default: str


class MessageResponse(BaseModel):
    seq: int
    role: str
    content: str
    created_at: datetime


def resolve_model(requested: str | None) -> str:
    model = requested or DEFAULT_MODEL
    if model not in ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model}' is not allowed. Choose one of: {', '.join(ALLOWED_MODELS)}",
        )
    return model


def load_messages(db: Session, conversation_id: int) -> list[Message]:
    if db.get(Conversation, conversation_id) is None:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return list(
        db.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq)
        ).all()
    )


def build_llm_history(rows: list[Message]) -> list[dict]:
    # DB history ≠ LLM history: drop custom roles (note, system-notification) here.
    return [{"role": m.role, "content": m.content} for m in rows if m.role in LLM_ROLES]


def dialogue_count(rows: list[Message]) -> int:
    return sum(1 for m in rows if m.role in LLM_ROLES)


def sse_data(payload: dict | str) -> str:
    if isinstance(payload, str):
        return f"data: {payload}\n\n"
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/models")
def list_models() -> ModelsResponse:
    return ModelsResponse(models=ALLOWED_MODELS, default=DEFAULT_MODEL)


@app.post("/conversations", status_code=201)
def create_conversation(db: Session = Depends(get_db)) -> ConversationResponse:
    conversation = Conversation()
    db.add(conversation)
    db.commit()
    return ConversationResponse(conversation_id=conversation.id)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db)) -> list[ConversationSummary]:
    rows = db.execute(
        select(Conversation, Message.content)
        .outerjoin(
            Message,
            and_(Message.conversation_id == Conversation.id, Message.seq == 1),
        )
        .order_by(Conversation.id.desc())
    ).all()
    return [
        ConversationSummary(
            id=conversation.id,
            created_at=conversation.created_at,
            preview=content[:PREVIEW_LENGTH] if content else None,
        )
        for conversation, content in rows
    ]


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, db: Session = Depends(get_db)) -> list[MessageResponse]:
    return [
        MessageResponse(seq=m.seq, role=m.role, content=m.content, created_at=m.created_at)
        for m in load_messages(db, conversation_id)
    ]


@app.post("/conversations/{conversation_id}/notes", status_code=201)
def add_note(
    conversation_id: int, req: NoteRequest, db: Session = Depends(get_db)
) -> NoteResponse:
    rows = load_messages(db, conversation_id)
    next_seq = rows[-1].seq + 1 if rows else 1
    content = req.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="Note content cannot be empty.")
    note = Message(
        conversation_id=conversation_id,
        seq=next_seq,
        role=NOTE_ROLE,
        content=content,
    )
    db.add(note)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail="The conversation was updated concurrently, please retry.",
        )
    db.refresh(note)
    return NoteResponse(
        seq=note.seq, role=note.role, content=note.content, created_at=note.created_at
    )


@app.post("/chat")
async def chat(req: ChatRequest, request: Request):
    model = resolve_model(req.model)
    message_text = req.message.strip()
    if not message_text:
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    with SessionLocal() as db:
        rows = load_messages(db, req.conversation_id)
        history = build_llm_history(rows)
        next_seq = rows[-1].seq + 1 if rows else 1
        prior_dialogue = dialogue_count(rows)

    user_message = {"role": "user", "content": message_text}
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history, user_message]

    async def event_stream():
        reply_parts: list[str] = []
        usage: dict | None = None
        client_disconnected = False

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
                async with client.stream(
                    "POST",
                    RODIUMAI_URL,
                    headers={
                        "Authorization": f"Bearer {RODIUMAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "max_tokens": 512,
                        "stream": True,
                    },
                ) as response:
                    if response.status_code >= 400:
                        await response.aread()
                        yield sse_event(
                            "error",
                            {"detail": f"The LLM API call failed ({response.status_code})."},
                        )
                        return

                    buffer = ""
                    async for chunk in response.aiter_text():
                        if await request.is_disconnected():
                            client_disconnected = True
                            break
                        buffer += chunk
                        while "\n" in buffer:
                            line, buffer = buffer.split("\n", 1)
                            line = line.strip()
                            if not line or not line.startswith("data:"):
                                continue
                            data = line[5:].strip()
                            if data == "[DONE]":
                                continue
                            try:
                                payload = json.loads(data)
                            except json.JSONDecodeError:
                                continue
                            if "usage" in payload and payload["usage"]:
                                usage = payload["usage"]
                            choices = payload.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                content = delta.get("content")
                                if content:
                                    reply_parts.append(content)
                            yield f"data: {data}\n\n"

        except httpx.HTTPError:
            yield sse_event("error", {"detail": "The LLM API call failed."})
            return

        if client_disconnected:
            # Stop = cancel the turn entirely: nothing is written to the DB.
            return

        reply = "".join(reply_parts).strip()
        if not reply:
            yield sse_event("error", {"detail": "Empty response from the LLM."})
            return

        notification = None
        if (prior_dialogue + 2) % NOTIFICATION_EVERY == 0:
            notification = NOTIFICATION_TEXT

        try:
            with SessionLocal() as db:
                if db.get(Conversation, req.conversation_id) is None:
                    yield sse_event("error", {"detail": "Conversation not found."})
                    return
                db.add_all(
                    [
                        Message(
                            conversation_id=req.conversation_id,
                            seq=next_seq,
                            role="user",
                            content=message_text,
                        ),
                        Message(
                            conversation_id=req.conversation_id,
                            seq=next_seq + 1,
                            role="assistant",
                            content=reply,
                        ),
                    ]
                )
                if notification:
                    db.add(
                        Message(
                            conversation_id=req.conversation_id,
                            seq=next_seq + 2,
                            role=NOTIFICATION_ROLE,
                            content=notification,
                        )
                    )
                db.commit()
        except IntegrityError:
            yield sse_event(
                "error",
                {"detail": "The conversation was updated concurrently, please retry."},
            )
            return
        except Exception:
            yield sse_event("error", {"detail": "Failed to save the conversation turn."})
            return

        meta: dict = {}
        if notification:
            meta["notification"] = notification
        if usage:
            meta["usage"] = usage
        if meta:
            yield sse_event("meta", meta)
        yield sse_data("[DONE]")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
