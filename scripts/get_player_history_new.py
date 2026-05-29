from datetime import datetime
from config import get_config
from db import insert_historic_activities
from http_client import get_json
from logger import logger

# Load configuration
config = get_config()

# config api
API_URL = "https://data-api.polymarket.com/activity"
MAX_LIMIT = 500  # max limit of the api
TABLE_NAME = config.TABLE_NAME_TRADES

def transform_activity_to_db_format(activity: dict) -> dict:
    """
    Transforms API format to database format
    
    Args:
        activity: Dictionary with API data
    
    Returns:
        Dictionary formatted for database insertion
    """
    # Converter timestamp para datetime
    
    activity_datetime = datetime.fromtimestamp(activity['timestamp'])
    
    return {
        'proxy_wallet': activity.get('proxyWallet'),
        'timestamp': activity.get('timestamp'),
        'activity_datetime': activity_datetime.isoformat(),
        'condition_id': activity.get('conditionId'),
        'type': activity.get('type'),
        'size': activity.get('size'),
        'usdc_size': activity.get('usdcSize'),
        'transaction_hash': activity.get('transactionHash'),
        'price': activity.get('price'),
        'asset': activity.get('asset'),
        'side': activity.get('side'),
        'outcome_index': activity.get('outcomeIndex'),
        'title': activity.get('title'),
        'slug': activity.get('slug'),
        'icon': activity.get('icon'),
        'event_slug': activity.get('eventSlug'),
        'outcome': activity.get('outcome'),
        'trader_name': activity.get('name'),
        'pseudonym': activity.get('pseudonym'),
        'bio': activity.get('bio'),
        'profile_image': activity.get('profileImage'),
        'profile_image_optimized': activity.get('profileImageOptimized'),
    }

def fetch_activities(user_address: str, limit: int = 500, offset: int = 0):
    """
    Fetch activities from the api
    """
    data = get_json(
        API_URL,
        params={
            "user": user_address,
            "limit": str(limit),
            "offset": str(offset),
            "sortBy": "TIMESTAMP",
            "sortDirection": "DESC",
        },
        timeout=10,
    )
    db_activities = [transform_activity_to_db_format(activity) for activity in data]
    logger.debug(f"Fetched {len(db_activities)} activities for {user_address}")
    return db_activities


def insert_activities_batch(activities: list):
    """
    Insert activities into SQLite, skipping duplicates.
    Returns only rows that were newly inserted so the bot can process them once.
    """
    if not activities:
        print("No activities to insert")
        return []

    inserted = insert_historic_activities(activities)
    skipped = len(activities) - len(inserted)
    if skipped:
        logger.debug(f"Skipped {skipped} duplicate activities")
    return inserted

if __name__ == "__main__":
    user_address = input("Enter the user address: ")
    activities = fetch_activities(user_address)
    inserted = insert_activities_batch(activities)
    print(f"Success count: {len(inserted)}")
