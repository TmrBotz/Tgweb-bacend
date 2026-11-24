from os import getenv, path
from dotenv import load_dotenv
from Backend import LOGGER
from Backend.helper.titles import parse_collections


load_dotenv(path.join(path.dirname(path.dirname(__file__)), "config.env"))


class Telegram:
    API_ID = int(getenv("API_ID", "0"))
    API_HASH = getenv("API_HASH", "")
    BOT_TOKEN = getenv("BOT_TOKEN", "")
    PORT = int(getenv("PORT", "8000"))
    BASE_URL = getenv("BASE_URL", "0.0.0.0").rstrip('/')
    AUTH_CHANNEL = [channel.strip() for channel in (getenv("AUTH_CHANNEL") or "").split(",") if channel.strip()]
    DATABASE = getenv("DATABASE", "").split(", ")
    UPSTREAM_REPO = getenv("UPSTREAM_REPO", "")
    UPSTREAM_BRANCH = getenv("UPSTREAM_BRANCH", "main")
    MULTI_CLIENT = getenv("MULTI_CLIENT", "False").lower() == "true"
    OWNER_ID = int(getenv("OWNER_ID", "5422223708"))

    # Collections: "name: keyword1, keyword2 | other name: keyword | tv: re:S\d+E\d+"
    COLLECTIONS = parse_collections(getenv("COLLECTIONS", ""))
    # Files that match no collection go here
    DEFAULT_COLLECTION = (getenv("DEFAULT_COLLECTION") or "new videos").strip() or "new videos"


LOGGER.info(
    "Collections loaded: "
    + (", ".join(c.name for c in Telegram.COLLECTIONS) or "none")
    + f" | default: {Telegram.DEFAULT_COLLECTION}"
)
