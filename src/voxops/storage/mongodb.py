from bson import BSON
from pymongo import MongoClient
from pymongo.database import Database

from voxops.core.errors import AppError
from voxops.core.settings import Settings


# Open the configured MongoDB database without creating a local storage directory.
# The caller closes the client when its application or script stops.
def database_for(settings: Settings) -> Database:
    return MongoClient(
        settings.mongo_uri.get_secret_value(),
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000,
        socketTimeoutMS=10000,
        connect=False,
    )[settings.mongo_database]


# Refuse a record that cannot fit in one MongoDB document before sending it.
# Generated file bytes live separately, so they do not enlarge run records.
def bounded_document(value: dict) -> dict:
    if len(BSON.encode(value)) >= 16 * 1024 * 1024:
        raise AppError("record_too_large", "Record exceeds the MongoDB document limit.", 409)
    return value
