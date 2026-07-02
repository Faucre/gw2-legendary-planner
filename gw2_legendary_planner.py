"""A small Guild Wars 2 legendary goal tracker.

Run it with:

    python gw2_legendary_planner.py

The app reads your GW2 API key from a .env file, fetches wallet currencies and
account item storage from the official Guild Wars 2 API, then compares those
counts with the goals listed in legendary_goals.json.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


# Official Guild Wars 2 API base URL.
API_BASE_URL = "https://api.guildwars2.com/v2"

# Folder for reusable legendary recipe templates.
DEFAULT_TEMPLATES_DIR = Path("templates")

# Local file for remembering item name -> item_id lookups.
DEFAULT_ITEM_CACHE_PATH = Path("item_cache.json")

# Local file for a full item name -> item IDs index.
DEFAULT_ITEM_NAME_INDEX_PATH = Path("item_name_index.json")
ITEM_NAME_INDEX_VERSION = 1
ITEM_INDEX_CHUNK_SIZE = 200

# Local file for remembering Trading Post prices for a short time.
DEFAULT_PRICE_CACHE_PATH = Path("price_cache.json")
PRICE_CACHE_TTL_SECONDS = 15 * 60
GOLD_RECOMMENDATION_THRESHOLD_COPPER = 10 * 10_000

# Local file for remembering character inventory counts between runs.
DEFAULT_CHARACTER_INVENTORY_CACHE_PATH = Path("character_inventory_cache.json")
CHARACTER_INVENTORY_CACHE_VERSION = 1

# API calls use one first try plus these retries.
DEFAULT_API_TIMEOUT_SECONDS = 60
API_RETRY_DELAYS_SECONDS = (2, 5, 10)
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}

# These are the .env variable names this script will understand.
API_KEY_VARIABLES = (
    "GW2_API_KEY",
    "GW2_API_TOKEN",
    "GW2_TOKEN",
    "API_KEY",
)

# These permissions are needed for a complete scan:
# - wallet: reads account currencies such as Karma.
# - inventories: reads material storage, bank, shared inventory, and bag contents.
# - characters: lists characters so we can scan each character's bags.
# - unlocks: reads unlocked account items, including the Legendary Armory.
REQUIRED_PERMISSIONS = ("wallet", "inventories", "characters", "unlocks")

# Report modes:
# - summary is the normal clean output.
# - detailed keeps helpful empty sections and completed entries when requested.
# - debug also prints scan/API/cache progress while the app is running.
OUTPUT_MODE_SUMMARY = "summary"
OUTPUT_MODE_DETAILED = "detailed"
OUTPUT_MODE_DEBUG = "debug"

# This is set from --debug in main(). It keeps normal runs quiet while still
# making troubleshooting information easy to turn on.
DEBUG_OUTPUT = False


def debug_print(message: str) -> None:
    """Print troubleshooting details only when --debug is used."""

    if DEBUG_OUTPUT:
        print(message)


class Gw2ApiError(Exception):
    """Raised when the GW2 API cannot return the data we need."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ItemLookupError(Exception):
    """Raised when a goal material name cannot be matched to one clear item."""


def parse_env_file(env_path: Path) -> dict[str, str]:
    """Read simple KEY=value lines from a .env file.

    This intentionally keeps the parser small and beginner-friendly. It handles
    blank lines, comments, optional "export", and quoted values.
    """

    values: dict[str, str] = {}

    if not env_path.exists():
        return values

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        if line.startswith("export "):
            line = line.removeprefix("export ").strip()

        if "=" not in line:
            # If the file contains only the raw key, still allow it.
            values["GW2_API_KEY"] = line
            continue

        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip().strip('"').strip("'")

        if name:
            values[name] = value

    return values


def find_api_key(env_path: Path | None) -> str:
    """Find the API key without ever printing it."""

    # Prefer the explicit path if the user passes --env. Otherwise try common
    # filenames, including the existing API_Key.env file in this project.
    env_paths = [env_path] if env_path else [Path(".env"), Path("API_Key.env")]
    env_values: dict[str, str] = {}

    for path in env_paths:
        if path is not None:
            env_values.update(parse_env_file(path))

    # Allow keys to be found even if someone wrote API_Key instead of API_KEY.
    lower_env_values = {name.lower(): value for name, value in env_values.items()}

    for variable_name in API_KEY_VARIABLES:
        value = env_values.get(variable_name) or os.environ.get(variable_name)
        if value:
            return value

        value = lower_env_values.get(variable_name.lower())
        if value:
            return value

    raise ValueError(
        "No GW2 API key found. Add GW2_API_KEY=your-key-here to .env "
        "or pass a file with --env."
    )


def api_get(
    path: str,
    api_key: str | None = None,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_API_TIMEOUT_SECONDS,
) -> Any:
    """Call the GW2 API and return decoded JSON."""

    url = f"{API_BASE_URL}{path}"

    if params:
        url = f"{url}?{urlencode(params)}"

    headers = {
        "Accept": "application/json",
        "User-Agent": "gw2-legendary-planner/1.0",
    }

    # Send the key as an Authorization header so it is not placed in the URL.
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    total_attempts = len(API_RETRY_DELAYS_SECONDS) + 1
    total_retries = len(API_RETRY_DELAYS_SECONDS)
    last_error: Gw2ApiError | None = None

    for attempt_number in range(1, total_attempts + 1):
        request = Request(url, headers=headers)
        debug_print(f"API GET {path} (attempt {attempt_number}/{total_attempts})")

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = f"GW2 API returned HTTP {error.code} for {path}."

            if error.code in {401, 403}:
                message += (
                    " Check that your API key is valid and has wallet, inventories, "
                    "characters, and unlocks permissions."
                )

            if body:
                message += f" API message: {body}"

            last_error = Gw2ApiError(message, status_code=error.code)

            # Some HTTP errors are permanent, so retrying only wastes time.
            if error.code not in RETRYABLE_HTTP_STATUS_CODES:
                raise last_error from error
        except TimeoutError as error:
            last_error = Gw2ApiError(
                f"The GW2 API request for {path} timed out after "
                f"{timeout_seconds} seconds."
            )
        except URLError as error:
            last_error = Gw2ApiError(f"Could not reach the GW2 API for {path}: {error.reason}")

        if attempt_number < total_attempts:
            delay_seconds = API_RETRY_DELAYS_SECONDS[attempt_number - 1]
            print(
                f"Temporary API problem while requesting {path}. "
                f"Retrying in {delay_seconds} seconds "
                f"(retry {attempt_number}/{total_retries})..."
            )
            time.sleep(delay_seconds)

    raise Gw2ApiError(
        f"The GW2 API did not respond successfully for {path} after "
        f"{total_retries} retries. Please try again later. Last error: {last_error}"
    )


def fetch_token_permissions(api_key: str) -> set[str]:
    """Ask the GW2 API which permissions this key has."""

    token_info = api_get("/tokeninfo", api_key=api_key)
    permissions = token_info.get("permissions", [])
    return {str(permission) for permission in permissions}


def warn_about_missing_permissions(token_permissions: set[str]) -> None:
    """Warn the user if the key cannot support a complete scan."""

    missing_permissions = [
        permission for permission in REQUIRED_PERMISSIONS if permission not in token_permissions
    ]

    if not missing_permissions:
        return

    print("Warning: your API key is missing permissions needed for a complete scan.")
    print(f"Missing permissions: {', '.join(missing_permissions)}")
    print("The report will still run, but skipped sections may make counts incomplete.")
    print()


def fetch_wallet(api_key: str) -> dict[int, int]:
    """Fetch account wallet currencies and return {currency_id: value}."""

    wallet_rows = api_get("/account/wallet", api_key=api_key)
    return {row["id"]: row["value"] for row in wallet_rows}


def fetch_material_storage(api_key: str) -> dict[int, int]:
    """Fetch material storage and return {item_id: count}."""

    material_rows = api_get("/account/materials", api_key=api_key)
    return {row["id"]: row["count"] for row in material_rows}


def fetch_bank(api_key: str) -> list[dict[str, Any] | None]:
    """Fetch account bank slots."""

    return api_get("/account/bank", api_key=api_key)


def fetch_shared_inventory(api_key: str) -> list[dict[str, Any] | None]:
    """Fetch shared inventory slots."""

    return api_get("/account/inventory", api_key=api_key)


def fetch_character_names(api_key: str) -> list[str]:
    """Fetch the names of every character on the account."""

    return api_get("/characters", api_key=api_key)


def fetch_character_details(api_key: str) -> list[dict[str, Any]]:
    """Fetch character details, including age/playtime seconds."""

    return api_get("/characters", api_key=api_key, params={"ids": "all"})


def fetch_character_inventory(api_key: str, character_name: str) -> dict[str, Any]:
    """Fetch one character's inventory bags."""

    safe_character_name = quote(character_name, safe="")
    return api_get(f"/characters/{safe_character_name}/inventory", api_key=api_key)


def fetch_legendary_armory(api_key: str) -> dict[int, int]:
    """Fetch Legendary Armory unlocks and return {item_id: unlocked_count}."""

    armory_rows = api_get("/account/legendaryarmory", api_key=api_key)
    unlocked_legendaries: dict[int, int] = {}

    for row in armory_rows:
        if not row:
            continue

        item_id = row.get("id")
        count = row.get("count", 1)

        if item_id is None:
            continue

        unlocked_legendaries[int(item_id)] = unlocked_legendaries.get(int(item_id), 0) + int(count)

    return unlocked_legendaries


def add_item_count(item_counts: dict[int, int], item_id: int, count: int) -> None:
    """Add an item stack to the combined item count dictionary."""

    item_counts[item_id] = item_counts.get(item_id, 0) + count


def add_item_stack(item_counts: dict[int, int], stack: dict[str, Any] | None) -> None:
    """Add one item slot to item_counts, ignoring empty slots safely."""

    if not stack:
        return

    item_id = stack.get("id")
    count = stack.get("count", 1)

    if item_id is None:
        return

    add_item_count(item_counts, int(item_id), int(count))


def add_inventory_slots(
    item_counts: dict[int, int],
    slots: list[dict[str, Any] | None],
) -> None:
    """Add every non-empty item stack from a list of inventory-like slots."""

    for slot in slots:
        add_item_stack(item_counts, slot)


def add_character_inventory(
    item_counts: dict[int, int],
    character_inventory: dict[str, Any],
) -> None:
    """Add all item stacks from one character's bags."""

    for bag in character_inventory.get("bags", []):
        if not bag:
            continue

        add_inventory_slots(item_counts, bag.get("inventory", []))


def merge_item_counts(
    item_counts: dict[int, int],
    extra_counts: dict[int, int],
) -> None:
    """Add one item count dictionary into another."""

    for item_id, count in extra_counts.items():
        add_item_count(item_counts, int(item_id), int(count))


def character_inventory_to_counts(character_inventory: dict[str, Any]) -> dict[int, int]:
    """Convert one character's inventory response into {item_id: count}."""

    item_counts: dict[int, int] = {}
    add_character_inventory(item_counts, character_inventory)
    return item_counts


def empty_character_inventory_cache() -> dict[str, Any]:
    """Create the starting shape for character_inventory_cache.json."""

    return {
        "version": CHARACTER_INVENTORY_CACHE_VERSION,
        "characters": {},
    }


def load_character_inventory_cache(cache_path: Path) -> dict[str, Any]:
    """Load cached character inventory counts."""

    if not cache_path.exists():
        return empty_character_inventory_cache()

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Could not read {cache_path}. Delete the file and run again, "
            "or fix it so it is valid JSON."
        ) from error

    if not isinstance(cache, dict):
        return empty_character_inventory_cache()

    if cache.get("version") != CHARACTER_INVENTORY_CACHE_VERSION:
        return empty_character_inventory_cache()

    if not isinstance(cache.get("characters"), dict):
        cache["characters"] = {}

    return cache


def save_character_inventory_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    """Save cached character inventory counts."""

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ValueError(f"Could not save character inventory cache at {cache_path}.") from error


def cache_entry_to_item_counts(cache_entry: dict[str, Any]) -> dict[int, int]:
    """Convert cached JSON item counts back to integer item IDs."""

    raw_counts = cache_entry.get("inventory_item_counts", {})

    if not isinstance(raw_counts, dict):
        return {}

    return {int(item_id): int(count) for item_id, count in raw_counts.items()}


def make_character_cache_entry(
    character_name: str,
    character_age: int,
    inventory_item_counts: dict[int, int],
) -> dict[str, Any]:
    """Create one character cache entry."""

    return {
        "name": character_name,
        "age": int(character_age),
        "inventory_item_counts": {
            str(item_id): int(count)
            for item_id, count in sorted(inventory_item_counts.items())
        },
        "scan_timestamp": int(time.time()),
    }


def build_character_inventory_counts(
    api_key: str,
    cache_path: Path,
    force_refresh: bool = False,
) -> tuple[dict[int, int], dict[str, int]]:
    """Use cached character inventories unless a character's age changed."""

    character_counts: dict[int, int] = {}
    cache = load_character_inventory_cache(cache_path)
    new_cache = empty_character_inventory_cache()
    details = fetch_character_details(api_key)
    current_names = set()
    cached_count = 0
    refreshed_count = 0

    for character in details:
        character_name = str(character.get("name", "")).strip()

        if not character_name:
            continue

        current_names.add(character_name)
        character_age = int(character.get("age", 0))
        cached_entry = cache["characters"].get(character_name)
        can_use_cache = (
            not force_refresh
            and cached_entry is not None
            and int(cached_entry.get("age", -1)) == character_age
            and isinstance(cached_entry.get("inventory_item_counts"), dict)
        )

        if can_use_cache:
            inventory_counts = cache_entry_to_item_counts(cached_entry)
            cached_count += 1
        else:
            debug_print(f"Refreshing character inventory: {character_name}")
            character_inventory = fetch_character_inventory(api_key, character_name)
            inventory_counts = character_inventory_to_counts(character_inventory)
            refreshed_count += 1

        merge_item_counts(character_counts, inventory_counts)
        new_cache["characters"][character_name] = make_character_cache_entry(
            character_name,
            character_age,
            inventory_counts,
        )
        save_character_inventory_cache(cache_path, new_cache)

    removed_count = len(set(cache.get("characters", {})) - current_names)
    save_character_inventory_cache(cache_path, new_cache)

    return character_counts, {
        "total": len(current_names),
        "cached": cached_count,
        "refreshed": refreshed_count,
        "removed": removed_count,
    }


def build_combined_item_counts(
    api_key: str,
    token_permissions: set[str],
    character_cache_path: Path,
    refresh_character_inventories: bool = False,
) -> tuple[dict[int, int], dict[str, list[str]]]:
    """Build one {item_id: count} dictionary from all item storage locations."""

    item_counts: dict[int, int] = {}
    scan_summary = {
        "scanned_sources": [],
        "skipped_sources": [],
    }

    if "inventories" not in token_permissions:
        scan_summary["skipped_sources"].append(
            "material storage, bank, and shared inventory (missing inventories permission)"
        )
        scan_summary["skipped_sources"].append(
            "character inventories (missing inventories permission)"
        )
        return item_counts, scan_summary

    # Material storage is the account-wide crafting material vault.
    debug_print("Scanning material storage...")
    material_storage = fetch_material_storage(api_key)
    for item_id, count in material_storage.items():
        add_item_count(item_counts, item_id, count)
    scan_summary["scanned_sources"].append(
        f"material storage ({len(material_storage):,} material entries)"
    )

    # The bank is account-wide storage. Empty bank slots are returned as null.
    debug_print("Scanning bank...")
    bank_slots = fetch_bank(api_key)
    add_inventory_slots(item_counts, bank_slots)
    scan_summary["scanned_sources"].append(f"bank ({len(bank_slots):,} slots)")

    # Shared inventory slots are account-wide slots visible to all characters.
    debug_print("Scanning shared inventory slots...")
    shared_inventory_slots = fetch_shared_inventory(api_key)
    add_inventory_slots(item_counts, shared_inventory_slots)
    scan_summary["scanned_sources"].append(
        f"shared inventory ({len(shared_inventory_slots):,} slots)"
    )

    if "characters" not in token_permissions:
        scan_summary["skipped_sources"].append(
            "character inventories (missing characters permission)"
        )
        return item_counts, scan_summary

    # Character inventories are the bags carried by each individual character.
    debug_print("Checking character inventory cache...")
    character_counts, character_summary = build_character_inventory_counts(
        api_key,
        character_cache_path,
        force_refresh=refresh_character_inventories,
    )
    merge_item_counts(item_counts, character_counts)

    cache_message = (
        f"Using cached inventories for {character_summary['cached']:,} characters; "
        f"refreshed {character_summary['refreshed']:,} characters."
    )
    debug_print(cache_message)

    if character_summary["removed"]:
        debug_print(
            f"Removed {character_summary['removed']:,} old character cache entries "
            "for deleted or renamed characters."
        )

    scan_summary["scanned_sources"].append(
        f"character inventories ({cache_message})"
    )

    return item_counts, scan_summary


def chunked(values: list[int], size: int) -> list[list[int]]:
    """Split a list into smaller lists for API calls."""

    return [values[index : index + size] for index in range(0, len(values), size)]


def fetch_names(path: str, id_values: set[int]) -> dict[int, str]:
    """Fetch item or currency names from public GW2 API endpoints."""

    names: dict[int, str] = {}

    for id_chunk in chunked(sorted(id_values), 200):
        rows = api_get(path, params={"ids": ",".join(str(value) for value in id_chunk)})

        for row in rows:
            names[row["id"]] = row.get("name", f"ID {row['id']}")

    return names


def format_coin(copper: int) -> str:
    """Turn a copper amount into a readable gold/silver/copper string."""

    gold = copper // 10_000
    silver = (copper % 10_000) // 100
    copper_left = copper % 100
    parts = []

    if gold:
        parts.append(f"{gold:,}g")

    if silver:
        parts.append(f"{silver}s")

    if copper_left or not parts:
        parts.append(f"{copper_left}c")

    return " ".join(parts)


def format_goal_amount(entry_id: int, amount: int, fallback_label: str) -> str:
    """Format item counts normally and account coin as gold/silver/copper."""

    if fallback_label == "Currency" and entry_id == 1:
        return format_coin(amount)

    return f"{amount:,}"


def normalize_item_name(item_name: str) -> str:
    """Make item names easier to compare by ignoring case and extra spaces."""

    return " ".join(item_name.casefold().split())


def empty_item_cache() -> dict[str, Any]:
    """Create the starting shape for item_cache.json."""

    return {"items_by_name": {}}


def load_item_cache(cache_path: Path) -> dict[str, Any]:
    """Load cached item name lookups from item_cache.json."""

    if not cache_path.exists():
        return empty_item_cache()

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Could not read {cache_path}. Delete the file and run again, "
            "or fix it so it is valid JSON."
        ) from error

    if not isinstance(cache, dict):
        return empty_item_cache()

    if not isinstance(cache.get("items_by_name"), dict):
        cache["items_by_name"] = {}

    return cache


def save_item_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    """Save item name lookups so future runs do less API work."""

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ValueError(f"Could not save item cache at {cache_path}.") from error


def cache_item_lookup(cache: dict[str, Any], typed_name: str, item: dict[str, Any]) -> None:
    """Remember one successful item name lookup."""

    official_name = item.get("name", typed_name)
    cache_entry = {
        "item_id": int(item["id"]),
        "name": official_name,
    }

    cache["items_by_name"][normalize_item_name(typed_name)] = cache_entry
    cache["items_by_name"][normalize_item_name(official_name)] = cache_entry


def get_cached_item(cache: dict[str, Any], item_name: str) -> dict[str, Any] | None:
    """Return a cached item if this name has already been resolved."""

    cached_item = cache["items_by_name"].get(normalize_item_name(item_name))

    if not cached_item:
        return None

    if "item_id" not in cached_item:
        return None

    return cached_item


def fetch_all_item_ids() -> list[int]:
    """Fetch every item ID from /v2/items."""

    return api_get("/items")


def empty_item_name_index() -> dict[str, Any]:
    """Create the starting shape for item_name_index.json."""

    return {
        "version": ITEM_NAME_INDEX_VERSION,
        "complete": False,
        "item_ids": [],
        "next_index": 0,
        "items_by_name": {},
    }


def load_item_name_index(index_path: Path) -> dict[str, Any]:
    """Load the reusable item name index."""

    if not index_path.exists():
        return empty_item_name_index()

    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Could not read {index_path}. Delete the file and run again, "
            "or fix it so it is valid JSON."
        ) from error

    if not isinstance(index, dict):
        return empty_item_name_index()

    if index.get("version") != ITEM_NAME_INDEX_VERSION:
        return empty_item_name_index()

    if not isinstance(index.get("items_by_name"), dict):
        index["items_by_name"] = {}

    if not isinstance(index.get("item_ids"), list):
        index["item_ids"] = []

    index["next_index"] = int(index.get("next_index", 0))
    index["complete"] = bool(index.get("complete", False))
    return index


def save_item_name_index(index_path: Path, index: dict[str, Any]) -> None:
    """Save item_name_index.json so interrupted builds can resume."""

    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text(
            json.dumps(index, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ValueError(f"Could not save item name index at {index_path}.") from error


def add_item_to_name_index(index: dict[str, Any], item: dict[str, Any]) -> None:
    """Add one item row to the name index, keeping duplicates visible."""

    item_id = item.get("id")
    item_name = item.get("name")

    if item_id is None or not item_name:
        return

    normalized_name = normalize_item_name(str(item_name))
    entry = {
        "id": int(item_id),
        "item_id": int(item_id),
        "name": str(item_name),
    }
    matches = index["items_by_name"].setdefault(normalized_name, [])

    if not any(existing.get("item_id") == entry["item_id"] for existing in matches):
        matches.append(entry)


def ensure_item_name_index(
    index_path: Path,
    rebuild: bool = False,
) -> dict[str, Any]:
    """Build or load item_name_index.json for fast future name lookups."""

    if rebuild:
        print(f"Rebuilding {index_path} from the official GW2 item list...")
        index = empty_item_name_index()
        save_item_name_index(index_path, index)
    else:
        index = load_item_name_index(index_path)

    if index["complete"]:
        debug_print(f"Using {index_path} for fast item name lookup.")
        return index

    if not index["item_ids"]:
        print("Building item name index from the official GW2 API.")
        print("The first run may take a while, but future name lookups will be much faster.")

        try:
            index["item_ids"] = fetch_all_item_ids()
        except Gw2ApiError as error:
            raise ItemLookupError(
                "Could not get the GW2 item list while building the item name index. "
                "Please try again in a few minutes. "
                f"API error: {error}"
            ) from error

        index["next_index"] = 0
        save_item_name_index(index_path, index)
    else:
        print(f"Resuming item name index build from {index_path}.")

    item_ids = [int(item_id) for item_id in index["item_ids"]]
    total_items = len(item_ids)

    while index["next_index"] < total_items:
        start_index = int(index["next_index"])
        end_index = min(start_index + ITEM_INDEX_CHUNK_SIZE, total_items)
        id_chunk = item_ids[start_index:end_index]

        try:
            rows = api_get(
                "/items",
                params={"ids": ",".join(str(item_id) for item_id in id_chunk)},
            )
        except Gw2ApiError as error:
            save_item_name_index(index_path, index)
            raise ItemLookupError(
                f"Item name index build paused at {start_index:,}/{total_items:,} item IDs. "
                f"Progress was saved to {index_path}. Run the app again to continue. "
                f"API error: {error}"
            ) from error

        for item in rows:
            add_item_to_name_index(index, item)

        index["next_index"] = end_index
        save_item_name_index(index_path, index)

        chunk_number = (end_index + ITEM_INDEX_CHUNK_SIZE - 1) // ITEM_INDEX_CHUNK_SIZE
        total_chunks = (total_items + ITEM_INDEX_CHUNK_SIZE - 1) // ITEM_INDEX_CHUNK_SIZE

        if chunk_number == 1 or chunk_number == total_chunks or chunk_number % 10 == 0:
            print(f"Item name index progress: {end_index:,}/{total_items:,} item IDs.")

    index["complete"] = True
    save_item_name_index(index_path, index)
    print(f"Item name index ready: {index_path}")
    return index


def lookup_uncached_item_names(
    item_names: set[str],
    cache: dict[str, Any],
    cache_path: Path,
    index_path: Path,
    rebuild_index: bool = False,
) -> dict[str, dict[str, Any]]:
    """Look up uncached material names using item_name_index.json."""

    resolved_items: dict[str, dict[str, Any]] = {}
    total_names = len(item_names)
    index = ensure_item_name_index(index_path, rebuild=rebuild_index)

    print(f"Need to look up {total_names} item name(s).")

    for name_number, typed_name in enumerate(sorted(item_names, key=normalize_item_name), start=1):
        normalized_target_name = normalize_item_name(typed_name)
        matches = index["items_by_name"].get(normalized_target_name, [])

        print(f"Item lookup {name_number}/{total_names}: {typed_name}")

        if not matches:
            raise ItemLookupError(
                f"Could not find an item named '{typed_name}'. Check the spelling in "
                "legendary_goals.json, or use item_id for that material."
            )

        if len(matches) > 1:
            match_text = ", ".join(
                f"{match.get('name', 'Unknown name')} (item_id {match['id']})"
                for match in matches[:10]
            )
            raise ItemLookupError(
                f"More than one item is named '{typed_name}'. Please use item_id "
                f"for that material. Matches: {match_text}"
            )

        resolved_items[typed_name] = matches[0]
        cache_item_lookup(cache, typed_name, matches[0])
        save_item_cache(cache_path, cache)
        debug_print(f"Saved {typed_name} as item_id {matches[0]['id']} in {cache_path}.")

    return resolved_items


def empty_price_cache() -> dict[str, Any]:
    """Create the starting shape for price_cache.json."""

    return {"prices_by_item_id": {}}


def load_price_cache(cache_path: Path) -> dict[str, Any]:
    """Load cached Trading Post prices from price_cache.json."""

    if not cache_path.exists():
        return empty_price_cache()

    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Could not read {cache_path}. Delete the file and run again, "
            "or fix it so it is valid JSON."
        ) from error

    if not isinstance(cache, dict):
        return empty_price_cache()

    if not isinstance(cache.get("prices_by_item_id"), dict):
        cache["prices_by_item_id"] = {}

    return cache


def save_price_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    """Save Trading Post prices so repeated runs do less API work."""

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(cache, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        raise ValueError(f"Could not save price cache at {cache_path}.") from error


def get_cached_price(
    cache: dict[str, Any],
    item_id: int,
    now: float,
) -> dict[str, Any] | None:
    """Return a fresh cached price if it is less than 15 minutes old."""

    cached_price = cache["prices_by_item_id"].get(str(item_id))

    if not cached_price:
        return None

    fetched_at = float(cached_price.get("fetched_at", 0))

    if now - fetched_at > PRICE_CACHE_TTL_SECONDS:
        return None

    return cached_price


def sell_unit_price_from_row(price_row: dict[str, Any]) -> int | None:
    """Read the lowest sell price from one /v2/commerce/prices row."""

    sells = price_row.get("sells") or {}
    sell_unit_price = sells.get("unit_price")
    sell_quantity = sells.get("quantity", 0)

    if sell_unit_price is None or int(sell_unit_price) <= 0 or int(sell_quantity) <= 0:
        return None

    return int(sell_unit_price)


def fetch_price_for_item(item_id: int) -> dict[str, Any]:
    """Fetch one Trading Post buy-now estimate from /v2/commerce/prices."""

    try:
        rows = api_get("/commerce/prices", params={"ids": str(item_id)})
    except Gw2ApiError as error:
        if error.status_code == 404:
            return {
                "item_id": item_id,
                "sell_unit_price": None,
                "not_priced_reason": "not listed on the Trading Post",
            }

        raise

    if not rows:
        return {
            "item_id": item_id,
            "sell_unit_price": None,
            "not_priced_reason": "not listed on the Trading Post",
        }

    sell_unit_price = sell_unit_price_from_row(rows[0])

    if sell_unit_price is None:
        return {
            "item_id": item_id,
            "sell_unit_price": None,
            "not_priced_reason": "no sell listings",
        }

    return {
        "item_id": item_id,
        "sell_unit_price": sell_unit_price,
        "not_priced_reason": None,
    }


def get_price_estimates(
    item_ids: set[int],
    cache_path: Path,
) -> dict[int, dict[str, Any]]:
    """Get current-ish Trading Post prices, using a 15-minute local cache."""

    price_estimates: dict[int, dict[str, Any]] = {}

    if not item_ids:
        return price_estimates

    cache = load_price_cache(cache_path)
    now = time.time()
    ids_to_fetch: list[int] = []
    cached_price_count = 0

    for item_id in sorted(item_ids):
        cached_price = get_cached_price(cache, item_id, now)

        if cached_price:
            cached_price_count += 1
            price_estimates[item_id] = cached_price
        else:
            ids_to_fetch.append(item_id)

    debug_print(
        f"Trading Post price cache: {cached_price_count:,} cached, "
        f"{len(ids_to_fetch):,} to fetch."
    )

    if ids_to_fetch:
        debug_print("Fetching Trading Post prices for missing materials...")

        for item_id in ids_to_fetch:
            price = fetch_price_for_item(item_id)
            price["fetched_at"] = now
            cache["prices_by_item_id"][str(item_id)] = price
            price_estimates[item_id] = price

        save_price_cache(cache_path, cache)

    return price_estimates


def template_path_from_reference(template_reference: str, templates_dir: Path) -> Path:
    """Convert a simple template name into a templates/*.json path."""

    template_name = str(template_reference).strip()

    if not template_name:
        raise ValueError("A target has an empty template reference.")

    if "/" in template_name or "\\" in template_name:
        raise ValueError(
            f"Template reference '{template_name}' should be a simple name, "
            "like 'klobjarne_geirr'."
        )

    if not template_name.endswith(".json"):
        template_name = f"{template_name}.json"

    return templates_dir / template_name


def load_template(template_reference: str, templates_dir: Path) -> dict[str, Any]:
    """Load one recipe template JSON file."""

    template_path = template_path_from_reference(template_reference, templates_dir)

    if not template_path.exists():
        raise FileNotFoundError(f"Template file not found: {template_path}")

    with template_path.open("r", encoding="utf-8") as template_file:
        template = json.load(template_file)

    if not isinstance(template, dict):
        raise ValueError(f"Template {template_path} must contain one JSON object.")

    return template


def merge_template_target(template: dict[str, Any], target: dict[str, Any]) -> dict[str, Any]:
    """Merge one template with one target from legendary_goals.json."""

    merged_target = dict(template)

    # List fields are additive by default. This lets a template provide the base
    # recipe while legendary_goals.json adds personal notes or extra reminders.
    for list_field in ("steps", "materials", "currencies"):
        merged_target[list_field] = list(template.get(list_field, [])) + list(
            target.get(list_field, [])
        )

    for key, value in target.items():
        if key in {"steps", "materials", "currencies"}:
            continue

        merged_target[key] = value

    return merged_target


def expand_goal_templates(
    targets: list[dict[str, Any]],
    templates_dir: Path,
) -> list[dict[str, Any]]:
    """Replace template references with full target data."""

    expanded_targets: list[dict[str, Any]] = []

    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("Every target in legendary_goals.json must be an object.")

        template_reference = target.get("template")

        if not template_reference:
            expanded_targets.append(target)
            continue

        template = load_template(str(template_reference), templates_dir)
        expanded_targets.append(merge_template_target(template, target))

    return expanded_targets


def load_goals(config_path: Path) -> list[dict[str, Any]]:
    """Load target legendary goals from the JSON config file."""

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    targets = config.get("targets", [])

    if not isinstance(targets, list):
        raise ValueError("legendary_goals.json must contain a list named 'targets'.")

    targets = expand_goal_templates(targets, DEFAULT_TEMPLATES_DIR)
    return [target for target in targets if target.get("enabled", True)]


def material_has_item_id(material: dict[str, Any]) -> bool:
    """Check whether a material already uses item_id or the older id shortcut."""

    return "item_id" in material or "id" in material


def resolve_goal_material_names(
    targets: list[dict[str, Any]],
    cache_path: Path,
    index_path: Path,
    rebuild_item_index: bool = False,
) -> list[dict[str, Any]]:
    """Fill in item_id for materials that were written with name only."""

    cache = load_item_cache(cache_path)
    names_to_lookup: set[str] = set()

    for target in targets:
        target_name = target.get("name", "Unnamed target")

        for material in target.get("materials", []):
            if material_has_item_id(material):
                continue

            material_name = material.get("name")

            if not material_name:
                raise ValueError(
                    f"Target '{target_name}' has a material without item_id or name."
                )

            if not get_cached_item(cache, material_name):
                names_to_lookup.add(material_name)

    if names_to_lookup:
        lookup_uncached_item_names(
            names_to_lookup,
            cache,
            cache_path,
            index_path,
            rebuild_index=rebuild_item_index,
        )
    elif rebuild_item_index:
        ensure_item_name_index(index_path, rebuild=True)

    resolved_targets: list[dict[str, Any]] = []

    for target in targets:
        resolved_target = dict(target)
        resolved_materials = []

        for material in target.get("materials", []):
            resolved_material = dict(material)

            if not material_has_item_id(resolved_material):
                material_name = str(resolved_material["name"])
                cached_item = get_cached_item(cache, material_name)

                if not cached_item:
                    raise ItemLookupError(
                        f"Could not resolve '{material_name}'. Try running again, "
                        "or use item_id for that material."
                    )

                resolved_material["item_id"] = int(cached_item["item_id"])
                resolved_material["name"] = cached_item.get("name", material_name)

            resolved_materials.append(resolved_material)

        resolved_target["materials"] = resolved_materials
        resolved_targets.append(resolved_target)

    return resolved_targets


def normalize_goal_entries(
    target_name: str,
    entries: list[dict[str, Any]],
    id_keys: tuple[str, ...],
    section_name: str,
) -> list[dict[str, Any]]:
    """Make goal entries easier to work with and validate the required fields."""

    normalized_entries: list[dict[str, Any]] = []

    for entry in entries:
        entry_id = None

        for id_key in id_keys:
            if id_key in entry:
                entry_id = entry[id_key]
                break

        amount = entry.get("amount", entry.get("count", entry.get("quantity")))

        if entry_id is None or amount is None:
            raise ValueError(
                f"Target '{target_name}' has a {section_name} entry missing an id or amount."
            )

        normalized_entries.append(
            {
                "id": int(entry_id),
                "amount": int(amount),
                "name": entry.get("name"),
                "source_hint": str(entry.get("source_hint", "")).strip(),
            }
        )

    return normalized_entries


def collect_goal_ids(targets: list[dict[str, Any]]) -> tuple[set[int], set[int]]:
    """Collect all item and currency IDs listed in enabled goals."""

    item_ids: set[int] = set()
    currency_ids: set[int] = set()

    for target in targets:
        target_name = target.get("name", "Unnamed target")

        material_entries = normalize_goal_entries(
            target_name,
            target.get("materials", []),
            ("item_id", "id"),
            "materials",
        )
        currency_entries = normalize_goal_entries(
            target_name,
            target.get("currencies", []),
            ("currency_id", "id"),
            "currencies",
        )

        item_ids.update(entry["id"] for entry in material_entries)
        currency_ids.update(entry["id"] for entry in currency_entries)

        final_item_id = target.get("final_item_id")

        if final_item_id is not None:
            item_ids.add(int(final_item_id))

    return item_ids, currency_ids


def target_final_item_id(target: dict[str, Any]) -> int | None:
    """Return a target's optional final legendary item ID."""

    final_item_id = target.get("final_item_id")

    if final_item_id is None:
        return None

    return int(final_item_id)


def is_target_unlocked(
    target: dict[str, Any],
    legendary_armory: dict[int, int],
) -> bool:
    """Check whether this target's final legendary is already unlocked."""

    final_item_id = target_final_item_id(target)

    if final_item_id is None:
        return False

    return legendary_armory.get(final_item_id, 0) > 0


def get_missing_entries(
    entries: list[dict[str, Any]],
    owned_counts: dict[int, int],
    api_names: dict[int, str],
    fallback_label: str,
) -> list[dict[str, Any]]:
    """Return the entries that are still missing after account counts are checked."""

    missing_entries: list[dict[str, Any]] = []

    for entry in entries:
        entry_id = entry["id"]
        needed = entry["amount"]
        owned = owned_counts.get(entry_id, 0)
        missing = max(needed - owned, 0)
        name = entry["name"] or api_names.get(entry_id) or f"{fallback_label} {entry_id}"

        if missing > 0:
            missing_entries.append(
                {
                    "id": entry_id,
                    "name": name,
                    "needed": needed,
                    "owned": owned,
                    "missing": missing,
                    "source_hint": entry.get("source_hint", ""),
                }
            )

    return missing_entries


def collect_missing_item_ids(
    targets: list[dict[str, Any]],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
) -> set[int]:
    """Collect item IDs that are missing from at least one enabled target."""

    missing_item_ids: set[int] = set()

    for target in targets:
        if is_target_unlocked(target, legendary_armory):
            continue

        target_name = target.get("name", "Unnamed target")
        material_entries = normalize_goal_entries(
            target_name,
            target.get("materials", []),
            ("item_id", "id"),
            "materials",
        )

        for entry in material_entries:
            if item_counts.get(entry["id"], 0) < entry["amount"]:
                missing_item_ids.add(entry["id"])

    return missing_item_ids


def price_text_for_missing_entry(
    entry: dict[str, Any],
    price_estimates: dict[int, dict[str, Any]],
) -> str:
    """Return a short price note for one missing item line."""

    price = price_estimates.get(entry["id"])

    if not price or price.get("sell_unit_price") is None:
        return "not priced"

    total_price = int(price["sell_unit_price"]) * int(entry["missing"])
    return f"est. {format_coin(total_price)}"


def is_named_item(missing_entry: dict[str, Any], item_name: str) -> bool:
    """Check a missing item by name, ignoring case and extra spaces."""

    return normalize_item_name(missing_entry["name"]) == normalize_item_name(item_name)


def is_provisioners_token(missing_entry: dict[str, Any]) -> bool:
    """Check whether a missing item looks like a Provisioner's Token."""

    normalized_name = normalize_item_name(missing_entry["name"])
    return "provisioner" in normalized_name and "token" in normalized_name


def is_obsidian_armor_essence(
    target_name: str,
    missing_entry: dict[str, Any],
) -> bool:
    """Check whether a missing item looks like an Obsidian armor essence."""

    normalized_target = normalize_item_name(target_name)
    normalized_name = normalize_item_name(missing_entry["name"])

    if "essence" not in normalized_name:
        return False

    essence_words = ("despair", "greed", "triumph", "kryptis")
    return "obsidian armor" in normalized_target or any(
        word in normalized_name for word in essence_words
    )


def priced_missing_total(
    missing_items: list[dict[str, Any]],
    price_estimates: dict[int, dict[str, Any]] | None,
) -> int:
    """Add up the Trading Post estimate for priced missing items."""

    if price_estimates is None:
        return 0

    total_copper = 0

    for item in missing_items:
        price = price_estimates.get(item["id"])

        if not price or price.get("sell_unit_price") is None:
            continue

        total_copper += int(price["sell_unit_price"]) * int(item["missing"])

    return total_copper


def is_gold_currency(missing_currency: dict[str, Any]) -> bool:
    """Check whether a missing wallet currency is account coin/gold."""

    normalized_name = normalize_item_name(missing_currency["name"])
    return missing_currency["id"] == 1 or "coin" in normalized_name or "gold" in normalized_name


def build_rule_recommendations(
    target_name: str,
    missing_items: list[dict[str, Any]],
    missing_currencies: list[dict[str, Any]],
    price_estimates: dict[int, dict[str, Any]] | None,
) -> list[str]:
    """Build simple rule-based recommendations from missing items and currencies."""

    recommendations: list[str] = []

    if any(is_named_item(item, "Mystic Clover") for item in missing_items):
        recommendations.append(
            "Mystic Clover: do Wizard's Vault objectives, work WvW reward tracks, "
            "and check weekly vendor sources."
        )

    if any(is_named_item(item, "Gift of Battle") for item in missing_items):
        recommendations.append("Gift of Battle: put today's play time into WvW reward track progress.")

    if any(is_provisioners_token(item) for item in missing_items):
        recommendations.append(
            "Provisioner's Token: buy the easy daily Provisioner's Token options first."
        )

    if any(is_obsidian_armor_essence(target_name, item) for item in missing_items):
        recommendations.append(
            "Obsidian armor essences: run Convergences and do rift hunting for essence progress."
        )

    gold_currency_missing = any(is_gold_currency(currency) for currency in missing_currencies)
    priced_total = priced_missing_total(missing_items, price_estimates)

    if gold_currency_missing or priced_total >= GOLD_RECOMMENDATION_THRESHOLD_COPPER:
        recommendations.append(
            "Gold pressure: choose low-burnout profit sources like daily Wizard's Vault "
            "objectives, quick strikes/fractals/metas you enjoy, gathering, and selling "
            "surplus materials before buying missing items."
        )

    if missing_currencies and not gold_currency_missing:
        currency_names = ", ".join(currency["name"] for currency in missing_currencies[:3])
        recommendations.append(
            f"Currency gap: prioritize activities that award {currency_names} before spending gold."
        )

    return recommendations


def target_recipe_status_needs_work(target: dict[str, Any]) -> bool:
    """Check whether a template says its recipe data still needs verification."""

    recipe_status = str(target.get("recipe_status", "")).strip().casefold()
    return recipe_status not in {"", "complete", "verified"}


def step_is_configuration_work(step: dict[str, Any]) -> bool:
    """Guess whether a checklist step is about setup/recipe data instead of play."""

    step_text = normalize_item_name(f"{step['name']} {step.get('notes', '')}")
    setup_words = (
        "todo",
        "verify",
        "recipe",
        "final item id",
        "final_item_id",
        "configuration",
        "template",
        "exact material",
    )
    return any(word in step_text for word in setup_words)


def split_incomplete_steps(
    steps: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate incomplete checklist steps into setup work and play/checklist work."""

    configuration_steps: list[dict[str, Any]] = []
    gameplay_steps: list[dict[str, Any]] = []

    for step in steps:
        if step["complete"]:
            continue

        if step_is_configuration_work(step):
            configuration_steps.append(step)
        else:
            gameplay_steps.append(step)

    return configuration_steps, gameplay_steps


def target_needs_recipe_data(
    target: dict[str, Any],
    material_entries: list[dict[str, Any]],
    currency_entries: list[dict[str, Any]],
    configuration_steps: list[dict[str, Any]],
) -> bool:
    """Decide whether a target still needs recipe/configuration data."""

    if target_recipe_status_needs_work(target):
        return True

    has_goal_entries = bool(material_entries or currency_entries)

    if not has_goal_entries and configuration_steps:
        return True

    # A target with no API-tracked requirements and no armory unlock cannot be
    # proven complete, so treat it as needing recipe data.
    if not has_goal_entries and not target.get("steps"):
        return True

    return False


def target_status_label(
    is_unlocked: bool,
    needs_recipe_data: bool,
    missing_items: list[dict[str, Any]],
    missing_currencies: list[dict[str, Any]],
    incomplete_steps: list[dict[str, Any]],
) -> str:
    """Return the short status label shown beside a target."""

    if is_unlocked:
        return "Complete"

    if needs_recipe_data:
        return "Needs recipe data"

    if missing_items or missing_currencies:
        return "In progress"

    if incomplete_steps:
        return "Needs manual checklist work"

    return "Complete"


def build_target_status(
    target: dict[str, Any],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
) -> dict[str, Any]:
    """Collect missing item/currency details for one target."""

    target_name = target.get("name", "Unnamed target")
    material_entries = normalize_goal_entries(
        target_name,
        target.get("materials", []),
        ("item_id", "id"),
        "materials",
    )
    currency_entries = normalize_goal_entries(
        target_name,
        target.get("currencies", []),
        ("currency_id", "id"),
        "currencies",
    )
    steps = normalize_steps(target_name, target.get("steps", []))
    incomplete_steps = [step for step in steps if not step["complete"]]
    configuration_steps, gameplay_steps = split_incomplete_steps(steps)
    missing_items = get_missing_entries(
        material_entries,
        item_counts,
        item_names,
        "Item",
    )
    missing_currencies = get_missing_entries(
        currency_entries,
        wallet,
        currency_names,
        "Currency",
    )
    is_unlocked = is_target_unlocked(target, legendary_armory)
    needs_recipe_data = target_needs_recipe_data(
        target,
        material_entries,
        currency_entries,
        configuration_steps,
    )

    return {
        "target": target,
        "name": target_name,
        "is_unlocked": is_unlocked,
        "material_entries": material_entries,
        "currency_entries": currency_entries,
        "steps": steps,
        "incomplete_steps": incomplete_steps,
        "configuration_steps": configuration_steps,
        "gameplay_steps": gameplay_steps,
        "needs_recipe_data": needs_recipe_data,
        "missing_items": missing_items,
        "missing_currencies": missing_currencies,
        "status_label": target_status_label(
            is_unlocked,
            needs_recipe_data,
            missing_items,
            missing_currencies,
            incomplete_steps,
        ),
    }


def normalize_steps(target_name: str, steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and simplify manual checklist steps."""

    normalized_steps: list[dict[str, Any]] = []

    if not isinstance(steps, list):
        raise ValueError(f"Target '{target_name}' has steps, but steps must be a list.")

    for step in steps:
        if not isinstance(step, dict):
            raise ValueError(f"Target '{target_name}' has a step that is not an object.")

        step_name = str(step.get("name", "")).strip()

        if not step_name:
            raise ValueError(f"Target '{target_name}' has a step without a name.")

        normalized_steps.append(
            {
                "name": step_name,
                "complete": bool(step.get("complete", False)),
                "notes": str(step.get("notes", "")).strip(),
            }
        )

    return normalized_steps


def make_step_lines(
    target_name: str,
    steps: list[dict[str, Any]],
    show_complete: bool,
    detailed: bool = False,
) -> list[str]:
    """Build report lines for manual checklist steps."""

    if not steps:
        if detailed:
            return ["    No manual steps listed."]

        return []

    normalized_steps = normalize_steps(target_name, steps)
    visible_steps = [
        step for step in normalized_steps if show_complete or not step["complete"]
    ]

    if not visible_steps:
        if detailed:
            return ["    No incomplete manual steps."]

        return []

    lines: list[str] = []

    for step in visible_steps:
        status = "complete" if step["complete"] else "incomplete"
        line = f"    - {step['name']}: {status}"

        if step["notes"]:
            line += f" - {step['notes']}"

        lines.append(line)

    return lines


def make_missing_lines(
    entries: list[dict[str, Any]],
    owned_counts: dict[int, int],
    api_names: dict[int, str],
    fallback_label: str,
    show_complete: bool,
    detailed: bool = False,
) -> list[str]:
    """Build report lines for one group, such as materials or currencies."""

    lines: list[str] = []

    if not entries:
        if detailed:
            return ["    No goals listed in this section."]

        return []

    missing_count = 0

    for entry in entries:
        entry_id = entry["id"]
        needed = entry["amount"]
        owned = owned_counts.get(entry_id, 0)
        missing = max(needed - owned, 0)
        name = entry["name"] or api_names.get(entry_id) or f"{fallback_label} {entry_id}"

        if missing > 0:
            missing_count += 1
            missing_text = format_goal_amount(entry_id, missing, fallback_label)
            owned_text = format_goal_amount(entry_id, owned, fallback_label)
            needed_text = format_goal_amount(entry_id, needed, fallback_label)
            lines.append(
                f"    - {name}: missing {missing_text} "
                f"(have {owned_text}, need {needed_text})"
            )
            add_source_hint_line(lines, entry)
        elif show_complete:
            owned_text = format_goal_amount(entry_id, owned, fallback_label)
            needed_text = format_goal_amount(entry_id, needed, fallback_label)
            lines.append(f"    - {name}: complete (have {owned_text}, need {needed_text})")

    if missing_count == 0 and not show_complete and detailed:
        lines.append("    Nothing missing here.")

    return lines


def add_source_hint_line(lines: list[str], entry: dict[str, Any]) -> None:
    """Add a clean source hint line when a goal entry provides one."""

    source_hint = str(entry.get("source_hint", "")).strip()

    if source_hint:
        lines.append(f"      Source hint: {source_hint}")


def make_missing_item_lines(
    entries: list[dict[str, Any]],
    owned_counts: dict[int, int],
    api_names: dict[int, str],
    show_complete: bool,
    price_estimates: dict[int, dict[str, Any]] | None,
    detailed: bool = False,
) -> list[str]:
    """Build missing item lines, optionally adding Trading Post estimates."""

    lines: list[str] = []

    if not entries:
        if detailed:
            return ["    No goals listed in this section."]

        return []

    missing_count = 0

    for entry in entries:
        entry_id = entry["id"]
        needed = entry["amount"]
        owned = owned_counts.get(entry_id, 0)
        missing = max(needed - owned, 0)
        name = entry["name"] or api_names.get(entry_id) or f"Item {entry_id}"

        if missing > 0:
            missing_count += 1
            line = f"    - {name}: missing {missing:,} (have {owned:,}, need {needed:,})"

            if price_estimates is not None:
                price_text = price_text_for_missing_entry(
                    {"id": entry_id, "missing": missing},
                    price_estimates,
                )

                if price_text != "not priced":
                    line += f" - {price_text}"

            lines.append(line)
            add_source_hint_line(lines, entry)
        elif show_complete:
            lines.append(f"    - {name}: complete (have {owned:,}, need {needed:,})")

    if missing_count == 0 and not show_complete and detailed:
        lines.append("    Nothing missing here.")

    return lines


def add_price_summary_lines(
    lines: list[str],
    missing_entries: list[dict[str, Any]],
    price_estimates: dict[int, dict[str, Any]] | None,
) -> None:
    """Add per-target Trading Post total and not-priced notes."""

    if price_estimates is None:
        return

    total_copper = 0
    priced_count = 0
    not_priced_entries = []

    for entry in missing_entries:
        price = price_estimates.get(entry["id"])

        if not price or price.get("sell_unit_price") is None:
            not_priced_entries.append(entry)
            continue

        priced_count += 1
        total_copper += int(price["sell_unit_price"]) * int(entry["missing"])

    if priced_count:
        lines.append(
            "  Estimated buy-now cost for priced missing items: "
            f"{format_coin(total_copper)}"
        )

    if not_priced_entries:
        lines.append("  Not priced:")

        for entry in not_priced_entries:
            lines.append(f"    - {entry['name']}: missing {entry['missing']:,}")


def add_already_unlocked_section(
    lines: list[str],
    targets: list[dict[str, Any]],
    legendary_armory: dict[int, int],
    armory_summary: dict[str, Any],
    item_names: dict[int, str],
    detailed: bool = False,
) -> None:
    """Add a report section for configured targets already in the Legendary Armory."""

    unlocked_targets = [
        target for target in targets if is_target_unlocked(target, legendary_armory)
    ]

    if not detailed and not unlocked_targets:
        return

    lines.append("Already unlocked legendaries")

    if not armory_summary["scanned"]:
        lines.append(f"  Skipped: {armory_summary['skipped_reason']}")
        lines.append("")
        return

    if not unlocked_targets:
        lines.append("  No configured targets with final_item_id are unlocked yet.")
        lines.append("")
        return

    for target in unlocked_targets:
        final_item_id = target_final_item_id(target)
        item_name = item_names.get(final_item_id, f"Item {final_item_id}")
        unlocked_count = legendary_armory.get(final_item_id, 0)
        count_text = f" ({unlocked_count} unlocked)" if unlocked_count > 1 else ""
        lines.append(f"  - {target.get('name', 'Unnamed target')}: {item_name}{count_text}")

    lines.append("")


def step_recommendation_text(step: dict[str, Any]) -> str:
    """Turn one incomplete manual step into a short recommendation."""

    recommendation = f"Checklist: {step['name']}."

    if step.get("notes"):
        recommendation += f" {step['notes']}"

    return recommendation


def build_gameplay_recommendations(
    target_status: dict[str, Any],
    price_estimates: dict[int, dict[str, Any]] | None,
) -> list[str]:
    """Build recommendations that point to actual in-game progress."""

    recommendations = build_rule_recommendations(
        target_status["name"],
        target_status["missing_items"],
        target_status["missing_currencies"],
        price_estimates,
    )

    for step in target_status["gameplay_steps"]:
        recommendations.append(step_recommendation_text(step))

    if not recommendations and (
        target_status["missing_items"] or target_status["missing_currencies"]
    ):
        biggest_missing = sorted(
            target_status["missing_items"] + target_status["missing_currencies"],
            key=lambda entry: entry["missing"],
            reverse=True,
        )[0]
        recommendations.append(
            f"No special daily rule matched. Work on {biggest_missing['name']} first."
        )

    return recommendations


def build_configuration_recommendations(target_status: dict[str, Any]) -> list[str]:
    """Build recommendations for recipe/template TODOs and setup work."""

    recommendations: list[str] = []

    if target_recipe_status_needs_work(target_status["target"]):
        recommendations.append(
            f"Recipe data: verify {target_status['name']}'s template, then fill in "
            "exact materials, currencies, and final_item_id where known."
        )

    for step in target_status["configuration_steps"]:
        recommendations.append(step_recommendation_text(step))

    # Keep repeated template TODOs from making the recommendation block noisy.
    unique_recommendations: list[str] = []
    for recommendation in recommendations:
        if recommendation not in unique_recommendations:
            unique_recommendations.append(recommendation)

    return unique_recommendations


def add_recommended_today_section(
    lines: list[str],
    targets: list[dict[str, Any]],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    price_estimates: dict[int, dict[str, Any]] | None,
    detailed: bool = False,
) -> None:
    """Add focused recommendations for the first incomplete target, plus future notes."""

    target_statuses = [
        build_target_status(
            target,
            wallet,
            item_counts,
            legendary_armory,
            item_names,
            currency_names,
        )
        for target in targets
    ]
    incomplete_indexes = [
        index
        for index, status in enumerate(target_statuses)
        if not status["is_unlocked"] and status["status_label"] != "Complete"
    ]

    lines.append("Recommended today")

    if not incomplete_indexes:
        lines.append("  No gameplay or configuration recommendations today.")
        lines.append("")
        return

    main_index = incomplete_indexes[0]
    main_status = target_statuses[main_index]
    main_gameplay = build_gameplay_recommendations(main_status, price_estimates)
    main_configuration = build_configuration_recommendations(main_status)

    lines.append(f"  Main target: {main_status['name']} [{main_status['status_label']}]")

    if main_gameplay:
        lines.append("  Gameplay:")
        for recommendation in main_gameplay:
            lines.append(f"    - {recommendation}")
    elif detailed:
        lines.append("  Gameplay: no gameplay recommendations for this target.")

    if main_configuration:
        lines.append("  Configuration:")
        for recommendation in main_configuration:
            lines.append(f"    - {recommendation}")
    elif detailed:
        lines.append("  Configuration: no configuration recommendations for this target.")

    future_gameplay_lines = []
    future_configuration_lines = []
    future_recommendation_limit = 2 if detailed else 1

    for future_index in incomplete_indexes[1:]:
        future_status = target_statuses[future_index]
        future_gameplay = build_gameplay_recommendations(future_status, price_estimates)
        future_configuration = build_configuration_recommendations(future_status)

        if future_gameplay:
            future_gameplay_lines.append(
                f"    - {future_status['name']}: "
                f"{'; '.join(future_gameplay[:future_recommendation_limit])}"
            )

        if future_configuration:
            future_configuration_lines.append(
                f"    - {future_status['name']}: "
                f"{'; '.join(future_configuration[:future_recommendation_limit])}"
            )

    if future_gameplay_lines:
        lines.append("  Future gameplay:")
        lines.extend(future_gameplay_lines)
    elif detailed:
        lines.append("  Future gameplay: none from today's rules.")

    if future_configuration_lines:
        lines.append("  Future configuration:")
        lines.extend(future_configuration_lines)
    elif detailed:
        lines.append("  Future configuration: none.")

    lines.append("")


def build_report(
    targets: list[dict[str, Any]],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    scan_summary: dict[str, list[str]],
    legendary_armory: dict[int, int],
    armory_summary: dict[str, Any],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    show_complete: bool,
    price_estimates: dict[int, dict[str, Any]] | None,
    output_mode: str = OUTPUT_MODE_SUMMARY,
) -> str:
    """Create the final text report."""

    detailed = output_mode in {OUTPUT_MODE_DETAILED, OUTPUT_MODE_DEBUG}
    character_cache_summary = next(
        (
            source.removeprefix("character inventories (").removesuffix(")")
            for source in scan_summary["scanned_sources"]
            if source.startswith("character inventories (")
        ),
        "",
    )

    lines = [
        "Guild Wars 2 Legendary Planner",
        "================================",
        f"Scan summary: {len(wallet):,} wallet currencies, {len(item_counts):,} item IDs counted.",
    ]

    if character_cache_summary:
        lines.append(f"Character inventories: {character_cache_summary}")

    if scan_summary["skipped_sources"]:
        lines.append(f"Skipped: {'; '.join(scan_summary['skipped_sources'])}")

    if detailed and scan_summary["scanned_sources"]:
        lines.append("Scanned item sources:")
        for source in scan_summary["scanned_sources"]:
            lines.append(f"  - {source}")

    if detailed and scan_summary["skipped_sources"]:
        lines.append("Skipped item sources:")
        for source in scan_summary["skipped_sources"]:
            lines.append(f"  - {source}")

    lines.append("")
    add_already_unlocked_section(
        lines,
        targets,
        legendary_armory,
        armory_summary,
        item_names,
        detailed=detailed,
    )

    if not targets:
        lines.extend(
            [
                "No enabled targets found.",
                "Edit legendary_goals.json and add a target to start tracking.",
            ]
        )
        return "\n".join(lines)

    add_recommended_today_section(
        lines,
        targets,
        wallet,
        item_counts,
        legendary_armory,
        item_names,
        currency_names,
        price_estimates,
        detailed=detailed,
    )

    for target_number, target in enumerate(targets, start=1):
        target_status = build_target_status(
            target,
            wallet,
            item_counts,
            legendary_armory,
            item_names,
            currency_names,
        )
        target_name = target_status["name"]
        final_item_id = target_final_item_id(target)

        lines.append(
            f"Target {target_number}: {target_name} [{target_status['status_label']}]"
        )

        if target_status["is_unlocked"]:
            final_item_name = item_names.get(final_item_id, f"Item {final_item_id}")
            lines.append(f"  Already unlocked in Legendary Armory: {final_item_name}")
            step_lines = make_step_lines(
                target_name,
                target.get("steps", []),
                show_complete,
                detailed=detailed,
            )

            if step_lines:
                lines.append("  Manual checklist steps:")
                lines.extend(step_lines)

            lines.append("")
            continue

        step_lines = make_step_lines(
            target_name,
            target.get("steps", []),
            show_complete,
            detailed=detailed,
        )

        if step_lines:
            lines.append("  Manual checklist steps:")
            lines.extend(step_lines)

        item_lines = make_missing_item_lines(
            target_status["material_entries"],
            item_counts,
            item_names,
            show_complete,
            price_estimates,
            detailed=detailed,
        )

        if item_lines:
            lines.append("  Missing items from account storage and inventories:")
            lines.extend(item_lines)

        add_price_summary_lines(
            lines,
            target_status["missing_items"],
            price_estimates,
        )

        currency_lines = make_missing_lines(
            target_status["currency_entries"],
            wallet,
            currency_names,
            "Currency",
            show_complete,
            detailed=detailed,
        )

        if currency_lines:
            lines.append("  Missing wallet currencies:")
            lines.extend(currency_lines)

        if detailed and not step_lines and not item_lines and not currency_lines:
            lines.append("  No visible details for this target.")

        lines.append("")

    return "\n".join(lines).rstrip()


def parse_args() -> argparse.Namespace:
    """Read command-line options."""

    parser = argparse.ArgumentParser(
        description="Track Guild Wars 2 legendary goals from wallet and item storage."
    )
    parser.add_argument(
        "--config",
        default="legendary_goals.json",
        help="Path to your legendary goals JSON file.",
    )
    parser.add_argument(
        "--env",
        default=None,
        help="Path to your .env file. Defaults to .env, then API_Key.env.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional path for saving the report as a text file.",
    )
    parser.add_argument(
        "--show-complete",
        action="store_true",
        help="Also show goals that are already complete.",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Show fuller target details, including useful empty sections.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show API, scan, and cache details for troubleshooting.",
    )
    parser.add_argument(
        "--no-prices",
        action="store_true",
        help="Skip Trading Post price estimates.",
    )
    parser.add_argument(
        "--rebuild-item-index",
        action="store_true",
        help="Force rebuilding item_name_index.json before resolving item names.",
    )
    parser.add_argument(
        "--refresh-character-inventories",
        action="store_true",
        help="Force a fresh scan of every character inventory.",
    )
    return parser.parse_args()


def main() -> int:
    """Program entry point."""

    global DEBUG_OUTPUT

    args = parse_args()
    output_mode = OUTPUT_MODE_SUMMARY

    if args.detailed:
        output_mode = OUTPUT_MODE_DETAILED

    if args.debug:
        output_mode = OUTPUT_MODE_DEBUG

    DEBUG_OUTPUT = output_mode == OUTPUT_MODE_DEBUG

    try:
        api_key = find_api_key(Path(args.env) if args.env else None)

        debug_print("Checking API key permissions...")
        token_permissions = fetch_token_permissions(api_key)
        warn_about_missing_permissions(token_permissions)

        targets = load_goals(Path(args.config))
        targets = resolve_goal_material_names(
            targets,
            DEFAULT_ITEM_CACHE_PATH,
            DEFAULT_ITEM_NAME_INDEX_PATH,
            rebuild_item_index=args.rebuild_item_index,
        )

        legendary_armory: dict[int, int] = {}
        armory_summary: dict[str, Any] = {
            "scanned": False,
            "skipped_reason": "missing unlocks permission",
        }

        if "unlocks" in token_permissions:
            debug_print("Checking Legendary Armory unlocks...")
            legendary_armory = fetch_legendary_armory(api_key)
            armory_summary = {
                "scanned": True,
                "skipped_reason": "",
            }
        else:
            debug_print("Skipping Legendary Armory because the API key is missing unlocks permission.")

        wallet: dict[int, int] = {}

        if "wallet" in token_permissions:
            debug_print("Fetching wallet currencies from the official GW2 API...")
            wallet = fetch_wallet(api_key)
        else:
            debug_print("Skipping wallet currencies because the API key is missing wallet permission.")

        item_counts, scan_summary = build_combined_item_counts(
            api_key,
            token_permissions,
            DEFAULT_CHARACTER_INVENTORY_CACHE_PATH,
            refresh_character_inventories=args.refresh_character_inventories,
        )

        item_ids, currency_ids = collect_goal_ids(targets)
        price_estimates = None

        if args.no_prices:
            debug_print("Skipping Trading Post prices because --no-prices was used.")
        else:
            missing_item_ids = collect_missing_item_ids(targets, item_counts, legendary_armory)
            price_estimates = get_price_estimates(missing_item_ids, DEFAULT_PRICE_CACHE_PATH)

        item_names = fetch_names("/items", item_ids) if item_ids else {}
        currency_names = fetch_names("/currencies", currency_ids) if currency_ids else {}

        report = build_report(
            targets,
            wallet,
            item_counts,
            scan_summary,
            legendary_armory,
            armory_summary,
            item_names,
            currency_names,
            args.show_complete,
            price_estimates,
            output_mode=output_mode,
        )

        print(report)

        if args.output:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(report + "\n", encoding="utf-8")
            print()
            print(f"Report saved to {output_path}")

        return 0
    except (
        FileNotFoundError,
        ValueError,
        json.JSONDecodeError,
        Gw2ApiError,
        ItemLookupError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
