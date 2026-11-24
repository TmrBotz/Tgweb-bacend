from collections import OrderedDict
from pathlib import Path
from time import time
from typing import Any, Dict, Optional
import math
import mimetypes
import secrets

from fastapi import FastAPI, Query, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, HTMLResponse, FileResponse, Response
from fastapi.templating import Jinja2Templates
from pyrogram.enums import ChatMemberStatus

from Backend import StartTime, __version__, db
from Backend.config import Telegram
from Backend.helper.custom_dl import ByteStreamer
from Backend.helper.encrypt import decode_string
from Backend.helper.exceptions import InvalidHash
from Backend.helper.pyro import get_readable_time
from Backend.logger import LOGGER
from Backend.pyrofork import StreamBot, work_loads, multi_clients


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_THUMB = BASE_DIR / "static" / "default_thumb.svg"

app = FastAPI()
class_cache = {}

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", response_model=Dict[str, Any])
async def get_bot_workloads():
    """
    Home route to list each bot's workload and total number of bots.
    """
    response = {
        "server_status": "running",
        "uptime": get_readable_time(time() - StartTime),
        "telegram_bot": "@" + StreamBot.username,
        "connected_bots": len(multi_clients),
        "loads": dict(
            ("bot" + str(c + 1), l)
            for c, (_, l) in enumerate(
                sorted(work_loads.items(), key=lambda x: x[1], reverse=True)
            )
        ),
        "version": __version__,
    }
    return response


@app.get("/is_member")
async def is_member(user_id: int, channel: int):
    try:
        member = await StreamBot.get_chat_member(channel, user_id)
        if member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            return {"is_member": True}
        return {"is_member": False}
    except Exception:
        return {"is_member": False}


@app.get("/watch/{media_id}", response_class=HTMLResponse)
async def watch(request: Request, media_id: str):
    """Simple player page for one file."""
    return templates.TemplateResponse(request, "index.html", {"id": media_id})


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

@app.get("/api/collections", response_model=dict)
async def get_collections():
    """All collections with the number of files in each."""
    try:
        return await db.list_collections()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/media", response_model=dict)
async def get_media_list(
    collection: Optional[str] = Query(None, description="Collection name, e.g. 'new videos'. Empty = all files"),
    page: int = Query(default=1, ge=1, description="Page number to return"),
    page_size: int = Query(default=20, ge=1, le=100, description="Number of files per page"),
):
    """Latest files first, optionally only from one collection."""
    try:
        return await db.list_media(collection, page, page_size)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/id/{media_id}", response_model=dict)
async def get_media_details(media_id: str):
    """Details of one file by its id."""
    doc = await db.get_media(media_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Requested details not found")
    return db.to_public(doc)


@app.get("/api/search/", response_model=dict)
async def search_documents_endpoint(
    query: str = Query(..., description="Search query string"),
    collection: Optional[str] = Query(None, description="Search only inside this collection"),
    page: int = Query(default=1, ge=1, description="Page number to return"),
    page_size: int = Query(default=10, ge=1, le=100, description="Number of files per page"),
):
    """Search titles. Every word of the query must be present in the title."""
    try:
        return await db.search_documents(query=query, page=page, page_size=page_size, collection=collection)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# ----------------------------------------------------------------------
# Thumbnail  (taken from the Telegram media itself, default image if none)
# ----------------------------------------------------------------------
THUMB_CACHE_MAX = 500
thumb_cache: "OrderedDict[str, bytes]" = OrderedDict()
THUMB_HEADERS = {"Cache-Control": "public, max-age=86400"}


def _default_thumb() -> FileResponse:
    return FileResponse(
        DEFAULT_THUMB,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.get("/thumb/{media_id}")
async def thumbnail(media_id: str):
    cached = thumb_cache.get(media_id)
    if cached is not None:
        thumb_cache.move_to_end(media_id)
        return Response(content=cached, media_type="image/jpeg", headers=THUMB_HEADERS)

    try:
        data = await decode_string(media_id)
        chat_id = int(f"-100{data['chat_id']}")
        msg_id = int(data["msg_id"])
        secure_hash = data["hash"]
    except Exception:
        return _default_thumb()

    try:
        index = min(work_loads, key=work_loads.get)
        client = multi_clients[index]

        message = await client.get_messages(chat_id, msg_id)
        media = message.video or message.document
        if not media or not getattr(media, "thumbs", None) or media.file_unique_id[:6] != secure_hash:
            return _default_thumb()

        thumb = max(media.thumbs, key=lambda t: t.file_size or 0)
        buf = await client.download_media(thumb.file_id, in_memory=True)
        content = bytes(buf.getbuffer()) if hasattr(buf, "getbuffer") else None
        if not content:
            return _default_thumb()
    except Exception as e:
        LOGGER.debug(f"Thumbnail error for {media_id[:12]}...: {e}")
        return _default_thumb()

    thumb_cache[media_id] = content
    if len(thumb_cache) > THUMB_CACHE_MAX:
        thumb_cache.popitem(last=False)
    return Response(content=content, media_type="image/jpeg", headers=THUMB_HEADERS)


# ----------------------------------------------------------------------
# Streaming  (unchanged logic)
# ----------------------------------------------------------------------

@app.get('/dl/{id}/{name}')
async def stream_handler(request: Request, id: str, name: str):
    try:
        decoded_data = await decode_string(id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid id")
    if not decoded_data.get('msg_id') or not decoded_data.get('hash'):
        raise HTTPException(status_code=400, detail="Missing id or hash")
    chat_id = f"-100{decoded_data['chat_id']}"
    return await media_streamer(request, int(chat_id), int(decoded_data['msg_id']), decoded_data['hash'])


async def media_streamer(request: Request, chat_id: int, id: int, secure_hash: str):
    range_header = request.headers.get("Range", 0)
    index = min(work_loads, key=work_loads.get)
    faster_client = multi_clients[index]
    if Telegram.MULTI_CLIENT:
        LOGGER.debug(f"Client {index} is now serving {request.client.host}")
    if faster_client in class_cache:
        tg_connect = class_cache[faster_client]
        LOGGER.debug(f"Using cached ByteStreamer object for client {index}")
    else:
        LOGGER.debug(f"Creating new ByteStreamer object for client {index}")
        tg_connect = ByteStreamer(faster_client)
        class_cache[faster_client] = tg_connect
    LOGGER.debug("before calling get_file_properties")
    file_id = await tg_connect.get_file_properties(chat_id=chat_id, message_id=id)
    LOGGER.debug("after calling get_file_properties")
    if file_id.unique_id[:6] != secure_hash:
        LOGGER.debug(f"Invalid hash for message with ID {id}")
        raise InvalidHash
    file_size = file_id.file_size
    if range_header:
        from_bytes, until_bytes = range_header.replace("bytes=", "").split("-")
        from_bytes = int(from_bytes)
        until_bytes = int(until_bytes) if until_bytes else file_size - 1
    else:
        from_bytes = 0
        until_bytes = file_size - 1
    if (until_bytes > file_size) or (from_bytes < 0) or (until_bytes < from_bytes):
        return StreamingResponse(
            content=(f"416: Range not satisfiable",),
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"},
        )
    chunk_size = 1024 * 1024
    until_bytes = min(until_bytes, file_size - 1)

    offset = from_bytes - (from_bytes % chunk_size)
    first_part_cut = from_bytes - offset
    last_part_cut = until_bytes % chunk_size + 1

    req_length = until_bytes - from_bytes + 1
    part_count = math.ceil(until_bytes / chunk_size) - math.floor(offset / chunk_size)
    body = tg_connect.yield_file(
        file_id, index, offset, first_part_cut, last_part_cut, part_count, chunk_size
    )
    mime_type = file_id.mime_type
    file_name = file_id.file_name
    disposition = "inline"

    if mime_type:
        if not file_name:
            try:
                file_name = f"{secrets.token_hex(2)}.{mime_type.split('/')[1]}"
            except (IndexError, AttributeError):
                file_name = f"{secrets.token_hex(2)}.unknown"
    else:
        if file_name:
            mime_type = mimetypes.guess_type(file_name)[0]
        else:
            mime_type = "application/octet-stream"
            file_name = f"{secrets.token_hex(2)}.unknown"

    LOGGER.info(f"{mime_type}, {file_name}, {disposition}")
    return StreamingResponse(
        status_code=206 if range_header else 200,
        content=body,
        headers={
            "Content-Type": f"{mime_type}",
            "Content-Range": f"bytes {from_bytes}-{until_bytes}/{file_size}",
            "Content-Length": str(req_length),
            "Content-Disposition": f'{disposition}; filename="{file_name}"',
            "Accept-Ranges": "bytes",
        },
    )
