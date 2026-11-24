from datetime import datetime
from typing import List
from pydantic import BaseModel, Field


class MediaSchema(BaseModel):
    """One document = one file posted in an AUTH_CHANNEL."""
    title: str = Field(..., description="Caption of the file (only @usernames and links removed)")
    collections: List[str] = Field(..., description="Collections this file belongs to")
    id: str = Field(..., description="Encoded {chat_id, msg_id, hash} used by /dl and /thumb")
    size: str = Field(..., description="Readable size, e.g. 1.37GB")
    file_name: str = Field("video.mkv", description="Original Telegram file name")
    mime_type: str = Field("", description="Telegram mime type")
    file_unique_id: str = Field(..., description="Telegram file_unique_id, used to avoid duplicates")
    has_thumb: bool = Field(False, description="Telegram has a thumbnail for this file")
    added_on: datetime = Field(default_factory=datetime.utcnow, description="When the file was added")
