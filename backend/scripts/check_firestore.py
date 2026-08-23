"""Manual Firestore connectivity check — run directly, not via pytest."""

import asyncio
from google.cloud import firestore
from google.oauth2 import service_account

SERVICE_ACCOUNT_PATH = "firebase-service-account.json"


async def check_firestore_connection() -> None:
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_PATH
    )

    db = firestore.AsyncClient(
        project="notesinsight-cfa98",
        credentials=credentials,
    )

    print("Testing Firestore connection...")

    async for collection in db.collections():
        print(collection.id)

    print("Firestore connection successful!")


if __name__ == "__main__":
    asyncio.run(check_firestore_connection())
