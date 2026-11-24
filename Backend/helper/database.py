import re
from datetime import datetime
from typing import List, Optional
from urllib.parse import quote

from bson import ObjectId
from bson.errors import InvalidId
import motor.motor_asyncio
from pydantic import ValidationError

from Backend.logger import LOGGER
from Backend.config import Telegram
from Backend.helper.encrypt import encode_string
from Backend.helper.modal import MediaSchema


def _is_object_id(value: str) -> bool:
    """24 hex chars = Mongo _id (used in the short bot deep-link)."""
    return isinstance(value, str) and len(value) == 24 and re.fullmatch(r"[0-9a-fA-F]{24}", value) is not None


class Database:
    def __init__(self, connection_uri: str = Telegram.DATABASE, db_name: str = "projectS"):
        self._conn = None
        self.db = None
        self.media_collection = None
        self.deploy_config = None
        self.connection_uri = connection_uri
        self.db_name = db_name

    async def connect(self):
        """Establish a connection to the database."""
        try:
            if self._conn is not None:
                self._conn.close()

            self._conn = motor.motor_asyncio.AsyncIOMotorClient(self.connection_uri)
            self.db = self._conn[self.db_name]

            # One collection for every file
            self.media_collection = self.db["media"]
            self.deploy_config = self.db["deploy_config"]

            LOGGER.info("Database connection established")
        except Exception as e:
            LOGGER.error(f"Error connecting to the database: {e}")
            self._conn = None
            self.db = None
            return

        # Indexes (unique file_unique_id stops duplicates)
        try:
            await self.media_collection.create_index("file_unique_id", unique=True)
            await self.media_collection.create_index("id")
            await self.media_collection.create_index("collections")
            await self.media_collection.create_index([("added_on", -1)])
        except Exception as e:
            LOGGER.warning(f"Could not create indexes: {e}")

    async def disconnect(self):
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            LOGGER.info("Database connection closed")
        self._conn = None
        self.db = None
        self.media_collection = None

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    @staticmethod
    def to_public(doc: dict) -> dict:
        """Shape of one item in every API response."""
        media_id = doc["id"]
        name = quote(doc.get("file_name") or "video.mkv")
        added_on = doc.get("added_on")
        return {
            "id": media_id,
            "title": doc.get("title", ""),
            "collections": doc.get("collections", []),
            "size": doc.get("size", ""),
            "file_name": doc.get("file_name", ""),
            "added_on": added_on.isoformat() if isinstance(added_on, datetime) else added_on,
            "thumbnail": f"/thumb/{media_id}",
            "stream": f"/dl/{media_id}/{name}",
            "watch": f"/watch/{media_id}",
            # use as  https://t.me/<bot>?start=<bot_param>
            "bot_param": f"file_{doc['_id']}" if "_id" in doc else None,
        }

    @staticmethod
    def resolve_collection(name: str) -> str:
        """Case-insensitive lookup of a collection name from the config."""
        wanted = name.strip().lower()
        for col in Telegram.COLLECTIONS:
            if col.name.lower() == wanted:
                return col.name
        if Telegram.DEFAULT_COLLECTION.lower() == wanted:
            return Telegram.DEFAULT_COLLECTION
        return name.strip()

    @staticmethod
    def _id_filter(identifier: str) -> dict:
        """Accept the long encoded id or the short Mongo _id."""
        if _is_object_id(identifier):
            try:
                return {"_id": ObjectId(identifier)}
            except InvalidId:
                pass
        return {"id": identifier}

    # ------------------------------------------------------------------
    # write
    # ------------------------------------------------------------------
    async def insert_media(
        self,
        title: str,
        collections: List[str],
        hash: str,
        channel: int,
        msg_id: int,
        size: str,
        file_name: str,
        mime_type: str,
        file_unique_id: str,
        has_thumb: bool,
    ) -> Optional[dict]:
        """
        Save one file. Returns {"id": <encoded id>, "created": bool}
        (created=False -> same file was already saved, nothing changed).
        """
        if self.media_collection is None:
            LOGGER.error("Database is not connected. Did you call db.connect()?")
            return None

        encoded_id = await encode_string({"chat_id": channel, "msg_id": msg_id, "hash": hash})

        try:
            doc = MediaSchema(
                title=title,
                collections=collections,
                id=encoded_id,
                size=size,
                file_name=file_name or "video.mkv",
                mime_type=mime_type or "",
                file_unique_id=file_unique_id,
                has_thumb=has_thumb,
            ).model_dump()
        except ValidationError as e:
            LOGGER.error(f"Validation error: {e}")
            return None

        on_insert = {k: v for k, v in doc.items() if k != "file_unique_id"}
        result = await self.media_collection.update_one(
            {"file_unique_id": file_unique_id},
            {"$setOnInsert": on_insert},
            upsert=True,
        )
        return {"id": encoded_id, "created": result.upserted_id is not None}

    async def delete_media(self, identifier: str) -> bool:
        result = await self.media_collection.delete_one(self._id_filter(identifier))
        if result.deleted_count > 0:
            LOGGER.info(f"Media {identifier} deleted successfully.")
            return True
        LOGGER.info(f"No media found for {identifier}.")
        return False

    # ------------------------------------------------------------------
    # read
    # ------------------------------------------------------------------
    async def get_media(self, identifier: str) -> Optional[dict]:
        """Return one raw document by encoded id or by short Mongo _id."""
        return await self.media_collection.find_one(self._id_filter(identifier))

    async def list_media(self, collection: Optional[str], page: int, page_size: int) -> dict:
        query = {"collections": self.resolve_collection(collection)} if collection else {}
        skip = (page - 1) * page_size

        total = await self.media_collection.count_documents(query)
        cursor = (
            self.media_collection.find(query)
            .sort([("added_on", -1), ("_id", -1)])
            .skip(skip)
            .limit(page_size)
        )
        items = [self.to_public(d) for d in await cursor.to_list(length=page_size)]
        return {"total_count": total, "media": items}

    async def search_documents(
        self, query: str, page: int, page_size: int, collection: Optional[str] = None
    ) -> dict:
        words = [w for w in query.split() if w][:20]
        if not words:
            return {"total_count": 0, "results": []}

        # every word must be present in the title (any order)
        mongo_query: dict = {"$and": [{"title": {"$regex": re.escape(w), "$options": "i"}} for w in words]}
        if collection:
            mongo_query["$and"].append({"collections": self.resolve_collection(collection)})

        skip = (page - 1) * page_size
        total = await self.media_collection.count_documents(mongo_query)
        cursor = (
            self.media_collection.find(mongo_query)
            .sort([("added_on", -1), ("_id", -1)])
            .skip(skip)
            .limit(page_size)
        )
        items = [self.to_public(d) for d in await cursor.to_list(length=page_size)]
        return {"total_count": total, "results": items}

    async def list_collections(self) -> dict:
        """Every collection with its file count (configured ones first, even if empty)."""
        pipeline = [
            {"$unwind": "$collections"},
            {"$group": {"_id": "$collections", "count": {"$sum": 1}}},
        ]
        rows = await self.media_collection.aggregate(pipeline).to_list(None)
        counts = {r["_id"]: r["count"] for r in rows}

        names = [c.name for c in Telegram.COLLECTIONS]
        if Telegram.DEFAULT_COLLECTION not in names:
            names.append(Telegram.DEFAULT_COLLECTION)
        # collections that exist in DB but were later removed from config
        names += sorted(n for n in counts if n not in names)

        total = await self.media_collection.count_documents({})
        return {
            "total_count": total,
            "collections": [{"name": n, "count": counts.get(n, 0)} for n in names],
        }
