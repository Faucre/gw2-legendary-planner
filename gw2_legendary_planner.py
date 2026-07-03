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

from recipe_engine import RecipeApiError, RecipeEngine
from reference_database import (
    DEFAULT_REFERENCE_DB_PATH,
    ReferenceApiError,
    ReferenceDatabase,
    WikiImportError,
    normalize_wiki_item_text,
    reference_database_status,
    setup_reference_database,
)


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

# Target keys filled by the automatic recipe engine.
AUTO_RECIPE_MATERIALS_FIELD = "auto_recipe_materials"
RECIPE_TREE_FIELD = "recipe_tree"
RECIPE_ENGINE_WARNINGS_FIELD = "recipe_engine_warnings"
RECIPE_UNKNOWN_STEPS_FIELD = "recipe_unknown_manual_steps"

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


def reference_database_if_available() -> ReferenceDatabase | None:
    """Return the local reference database when it exists."""

    reference_database = ReferenceDatabase(DEFAULT_REFERENCE_DB_PATH)

    if reference_database.exists():
        return reference_database

    return None


def fetch_item_names_from_reference(id_values: set[int]) -> dict[int, str]:
    """Fetch item names from the local reference database."""

    reference_database = reference_database_if_available()

    if not reference_database or not id_values:
        return {}

    return {
        item_id: item.get("name", f"Item {item_id}")
        for item_id, item in reference_database.items_by_id(list(id_values)).items()
    }


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
    reference_database = reference_database_if_available()
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
                if reference_database:
                    continue

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

                if cached_item:
                    resolved_material["item_id"] = int(cached_item["item_id"])
                    resolved_material["name"] = cached_item.get("name", material_name)
                elif reference_database:
                    matches = reference_database.item_name_matches(material_name)

                    if not matches:
                        raise ItemLookupError(
                            f"Could not find material '{material_name}' in "
                            f"{DEFAULT_REFERENCE_DB_PATH}. Check the spelling, use item_id, "
                            "or run --setup-reference-db."
                        )

                    if len(matches) > 1:
                        match_text = ", ".join(
                            f"{match.get('name', 'Unknown name')} (item_id {match['id']})"
                            for match in matches[:10]
                        )
                        raise ItemLookupError(
                            f"More than one item is named '{material_name}'. "
                            f"Please use item_id for that material. Matches: {match_text}"
                        )

                    matched_item = matches[0]
                    cache_item_lookup(cache, material_name, matched_item)
                    save_item_cache(cache_path, cache)
                    resolved_material["item_id"] = int(matched_item["item_id"])
                    resolved_material["name"] = matched_item.get("name", material_name)
                else:
                    raise ItemLookupError(
                        f"Could not resolve '{material_name}'. Try running again, "
                        "or use item_id for that material."
                    )

            resolved_materials.append(resolved_material)

        resolved_target["materials"] = resolved_materials
        resolved_targets.append(resolved_target)

    return resolved_targets


def add_target_recipe_warning(target: dict[str, Any], warning: str) -> None:
    """Attach a recipe/final-item warning to a target without duplicates."""

    warnings = target.setdefault(RECIPE_ENGINE_WARNINGS_FIELD, [])

    if warning not in warnings:
        warnings.append(warning)


def target_final_item_lookup_name(target: dict[str, Any]) -> tuple[str, str]:
    """Return the name to resolve for a target and where it came from."""

    final_item_name = str(target.get("final_item_name", "")).strip()

    if final_item_name:
        return final_item_name, "final_item_name"

    if target.get("auto_resolve_final_item_from_name", True) is False:
        return "", ""

    target_name = str(target.get("name", "")).strip()

    if target_name:
        return target_name, "target name"

    return "", ""


def resolve_target_final_item_names(
    targets: list[dict[str, Any]],
    cache_path: Path,
    index_path: Path,
    rebuild_item_index: bool = False,
) -> list[dict[str, Any]]:
    """Resolve final_item_name or target name to final_item_id."""

    cache = load_item_cache(cache_path)
    reference_database = reference_database_if_available()
    names_to_lookup: set[str] = set()

    for target in targets:
        if target.get("final_item_id") is not None:
            continue

        lookup_name, _lookup_source = target_final_item_lookup_name(target)

        if lookup_name and not get_cached_item(cache, lookup_name):
            if reference_database:
                continue

            names_to_lookup.add(lookup_name)

    index: dict[str, Any] | None = None

    if names_to_lookup:
        try:
            index = ensure_item_name_index(index_path, rebuild=rebuild_item_index)
        except ItemLookupError as error:
            index = None
            lookup_warning = (
                "Automatic final item lookup could not use the item name index. "
                f"Details: {error}"
            )

            for target in targets:
                lookup_name, _lookup_source = target_final_item_lookup_name(target)

                if target.get("final_item_id") is None and lookup_name:
                    add_target_recipe_warning(target, lookup_warning)

    resolved_targets: list[dict[str, Any]] = []

    for target in targets:
        resolved_target = dict(target)

        if target.get(RECIPE_ENGINE_WARNINGS_FIELD):
            resolved_target[RECIPE_ENGINE_WARNINGS_FIELD] = list(
                target.get(RECIPE_ENGINE_WARNINGS_FIELD, [])
            )

        if resolved_target.get("final_item_id") is not None:
            resolved_targets.append(resolved_target)
            continue

        lookup_name, lookup_source = target_final_item_lookup_name(resolved_target)

        if not lookup_name:
            resolved_targets.append(resolved_target)
            continue

        cached_item = get_cached_item(cache, lookup_name)

        if cached_item:
            resolved_target["final_item_id"] = int(cached_item["item_id"])
            resolved_target["final_item_name"] = cached_item.get("name", lookup_name)
            resolved_targets.append(resolved_target)
            continue

        if reference_database:
            matches = reference_database.item_name_matches(lookup_name)

            if not matches:
                if lookup_source == "target name":
                    warning = (
                        f"Could not resolve target name '{lookup_name}' as a final item "
                        f"in {DEFAULT_REFERENCE_DB_PATH}. Add final_item_name or "
                        "final_item_id, or set auto_resolve_final_item_from_name to "
                        "false for this target."
                    )
                else:
                    warning = (
                        f"Could not find a final item named '{lookup_name}' in "
                        f"{DEFAULT_REFERENCE_DB_PATH}. Check the spelling, or use "
                        "final_item_id."
                    )

                add_target_recipe_warning(resolved_target, warning)
                resolved_targets.append(resolved_target)
                continue

            if len(matches) > 1:
                match_text = ", ".join(
                    f"{match.get('name', 'Unknown name')} (item_id {match['id']})"
                    for match in matches[:10]
                )
                if lookup_source == "target name":
                    warning = (
                        f"Target name '{lookup_name}' matches more than one final item "
                        f"in {DEFAULT_REFERENCE_DB_PATH}. Add final_item_name or "
                        f"final_item_id. Matches: {match_text}"
                    )
                else:
                    warning = (
                        f"More than one final item is named '{lookup_name}' in "
                        f"{DEFAULT_REFERENCE_DB_PATH}. Use final_item_id. "
                        f"Matches: {match_text}"
                    )

                add_target_recipe_warning(resolved_target, warning)
                resolved_targets.append(resolved_target)
                continue

            matched_item = matches[0]
            cache_item_lookup(cache, lookup_name, matched_item)
            save_item_cache(cache_path, cache)
            resolved_target["final_item_id"] = int(matched_item["item_id"])
            resolved_target["final_item_name"] = matched_item.get("name", lookup_name)
            resolved_targets.append(resolved_target)
            continue

        if index is None:
            resolved_targets.append(resolved_target)
            continue

        matches = index["items_by_name"].get(normalize_item_name(lookup_name), [])

        if not matches:
            if lookup_source == "target name":
                warning = (
                    f"Could not resolve target name '{lookup_name}' as a final item. "
                    "Add final_item_name or final_item_id, or set "
                    "auto_resolve_final_item_from_name to false for this target."
                )
            else:
                warning = (
                    f"Could not find a final item named '{lookup_name}'. "
                    "Check the spelling, or use final_item_id."
                )

            add_target_recipe_warning(
                resolved_target,
                warning,
            )
            resolved_targets.append(resolved_target)
            continue

        if len(matches) > 1:
            match_text = ", ".join(
                f"{match.get('name', 'Unknown name')} (item_id {match['id']})"
                for match in matches[:10]
            )
            if lookup_source == "target name":
                warning = (
                    f"Target name '{lookup_name}' matches more than one final item. "
                    f"Add final_item_name or final_item_id. Matches: {match_text}"
                )
            else:
                warning = (
                    f"More than one final item is named '{lookup_name}'. "
                    f"Use final_item_id. Matches: {match_text}"
                )

            add_target_recipe_warning(
                resolved_target,
                warning,
            )
            resolved_targets.append(resolved_target)
            continue

        matched_item = matches[0]
        cache_item_lookup(cache, lookup_name, matched_item)
        save_item_cache(cache_path, cache)
        resolved_target["final_item_id"] = int(matched_item["item_id"])
        resolved_target["final_item_name"] = matched_item.get("name", lookup_name)
        debug_print(
            f"Resolved {lookup_source} {lookup_name} to "
            f"item_id {matched_item['item_id']}."
        )
        resolved_targets.append(resolved_target)

    return resolved_targets


def target_materials(target: dict[str, Any]) -> list[dict[str, Any]]:
    """Return manual materials plus automatic recipe-engine materials."""

    return list(target.get("materials", [])) + list(
        target.get(AUTO_RECIPE_MATERIALS_FIELD, [])
    )


def recipe_tree_to_material_entries(recipe_tree: dict[str, Any]) -> list[dict[str, Any]]:
    """Convert recipe-engine terminal materials into normal goal entries."""

    material_entries: list[dict[str, Any]] = []
    wiki_sources_used = list(recipe_tree.get("wiki_sources_used", []))
    source_hint = "Auto-resolved from the official GW2 recipe API."

    if wiki_sources_used:
        source_hint = (
            "Auto-resolved from public recipe data using the official GW2 API plus "
            "GW2 Wiki fallback data where needed."
        )

    resolution_by_id = {
        int(entry["item_id"]): entry
        for entry in recipe_tree.get("resolution_sources", [])
        if entry.get("item_id") is not None
    }

    for material in recipe_tree.get("raw_material_requirements", []):
        item_id = int(material["item_id"])
        resolution = resolution_by_id.get(item_id, {})
        data_confidence = confidence_from_resolution(resolution, material)
        material_entries.append(
            {
                "item_id": item_id,
                "name": material.get("name", f"Item {material['item_id']}"),
                "amount": int(material["amount"]),
                "source_hint": source_hint,
                "paths": list(material.get("paths", [])),
                "reason": material.get("reason", ""),
                "data_confidence": data_confidence,
                "resolution_source": resolution.get("source", ""),
            }
        )

    return material_entries


def resolve_target_recipe_data(
    targets: list[dict[str, Any]],
    recipe_engine: RecipeEngine | None = None,
    item_counts: dict[int, int] | None = None,
    use_owned_intermediates: bool = True,
) -> list[dict[str, Any]]:
    """Attach automatic recipe data and beginner-friendly warnings to targets."""

    engine = recipe_engine or RecipeEngine()
    resolved_targets: list[dict[str, Any]] = []

    for target in targets:
        resolved_target = dict(target)
        existing_warnings = list(resolved_target.get(RECIPE_ENGINE_WARNINGS_FIELD, []))
        resolved_target[AUTO_RECIPE_MATERIALS_FIELD] = []
        resolved_target[RECIPE_ENGINE_WARNINGS_FIELD] = existing_warnings
        resolved_target.setdefault(RECIPE_UNKNOWN_STEPS_FIELD, [])

        final_item_id = resolved_target.get("final_item_id")

        if final_item_id is None:
            has_manual_tracking = bool(
                resolved_target.get("materials")
                or resolved_target.get("currencies")
                or resolved_target.get("steps")
            )

            if target_recipe_status_needs_work(resolved_target) or not has_manual_tracking:
                add_target_recipe_warning(
                    resolved_target,
                    "Automatic recipe lookup skipped because final_item_id is not set. "
                    "Add final_item_id or a resolvable final_item_name before relying "
                    "on API recipe data.",
                )

            resolved_targets.append(resolved_target)
            continue

        recipe_tree = engine.resolve_recipe_tree(
            int(final_item_id),
            item_counts=item_counts,
            use_owned_intermediates=use_owned_intermediates,
        )
        resolved_target[RECIPE_TREE_FIELD] = recipe_tree
        resolved_target[AUTO_RECIPE_MATERIALS_FIELD] = recipe_tree_to_material_entries(
            recipe_tree
        )
        resolved_target[RECIPE_ENGINE_WARNINGS_FIELD] = existing_warnings + [
            warning
            for warning in recipe_tree.get("warnings", [])
            if warning not in existing_warnings
        ]
        resolved_target[RECIPE_UNKNOWN_STEPS_FIELD] = list(
            recipe_tree.get("unknown_manual_steps", [])
        )
        resolved_targets.append(resolved_target)

    return resolved_targets


def validate_template_files(templates_dir: Path) -> int:
    """Validate every JSON template file and return how many were checked."""

    template_paths = sorted(templates_dir.glob("*.json"))

    for template_path in template_paths:
        load_template(template_path.stem, templates_dir)

    return len(template_paths)


def validation_item_lookup(
    item_name: str,
    cache: dict[str, Any],
    cache_path: Path,
    index: dict[str, Any] | None,
    label: str,
    reference_database: ReferenceDatabase | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Resolve one item name for validation without guessing."""

    warnings: list[str] = []
    clean_item_name = item_name.strip()

    if not clean_item_name:
        return None, [f"{label} is missing a name."]

    cached_item = get_cached_item(cache, clean_item_name)

    if cached_item:
        return cached_item, warnings

    if reference_database:
        matches = reference_database.item_name_matches(clean_item_name)

        if not matches:
            return None, [
                f"{label} '{clean_item_name}' was not found in "
                f"{DEFAULT_REFERENCE_DB_PATH}."
            ]

        if len(matches) > 1:
            match_text = ", ".join(
                f"{match.get('name', 'Unknown name')} (item_id {match['id']})"
                for match in matches[:10]
            )
            return None, [
                f"{label} '{clean_item_name}' is ambiguous. Use item_id. "
                f"Matches: {match_text}"
            ]

        matched_item = matches[0]
        cache_item_lookup(cache, clean_item_name, matched_item)
        save_item_cache(cache_path, cache)
        return matched_item, warnings

    if index is None:
        return None, [
            f"{label} '{clean_item_name}' could not be checked because "
            "item_name_index.json is not available."
        ]

    matches = index["items_by_name"].get(normalize_item_name(clean_item_name), [])

    if not matches:
        return None, [f"{label} '{clean_item_name}' was not found in the GW2 item index."]

    if len(matches) > 1:
        match_text = ", ".join(
            f"{match.get('name', 'Unknown name')} (item_id {match['id']})"
            for match in matches[:10]
        )
        return None, [
            f"{label} '{clean_item_name}' is ambiguous. Use item_id. "
            f"Matches: {match_text}"
        ]

    matched_item = matches[0]
    cache_item_lookup(cache, clean_item_name, matched_item)
    save_item_cache(cache_path, cache)
    return matched_item, warnings


def validate_recipe_lookup(
    recipe_engine: RecipeEngine,
    final_item_id: int | None,
    final_item_name: str,
) -> tuple[str, list[str]]:
    """Check whether a final item can be resolved through recipes or overrides."""

    if final_item_id is None:
        return "no (no final item resolved)", []

    override_summary = recipe_engine.override_decision_for_item(
        final_item_id,
        final_item_name,
    )

    if override_summary["status"] == "verified_ingredients":
        override_type = override_summary.get("override_type", "manual")
        return f"yes (verified {override_type} override ingredients)", []

    if override_summary["status"] == "manual_stop":
        override_type = override_summary.get("override_type", "manual")
        return f"manual ({override_type} override)", []

    official_lookup = recipe_engine.official_recipe_lookup_summary(final_item_id)

    if official_lookup["status"] == "single_recipe":
        return f"yes (official recipe {official_lookup['recipe_id']})", []

    if official_lookup["status"] == "preferred_recipe":
        return (
            f"yes (preferred recipe {official_lookup['recipe_id']} from "
            f"{len(official_lookup['recipe_ids'])} official recipes)",
            [],
        )

    if official_lookup["status"] == "multiple_recipes":
        return (
            "no (multiple recipes need a preferred override)",
            [official_lookup["reason"]],
        )

    wiki_summary = recipe_engine.wiki_recipe_summary_for_item(
        final_item_id,
        final_item_name,
    )

    if wiki_summary.get("selected_recipe") is not None:
        warnings = [
            "Using imported GW2 Wiki recipe data as an unreviewed fallback.",
        ]

        if wiki_summary.get("source_url"):
            warnings.append(f"Source URL: {wiki_summary['source_url']}")

        return "yes (GW2 Wiki imported recipe fallback)", warnings

    if wiki_summary["row_count"] > 0:
        warnings = [wiki_summary["reason"]]

        if wiki_summary.get("source_url"):
            warnings.append(f"Source URL: {wiki_summary['source_url']}")

        return "review (GW2 Wiki imported source needs verification)", warnings

    if official_lookup["status"] == "lookup_failed":
        return "unknown (recipe API lookup failed)", [official_lookup["reason"]]

    return "Needs legendary override data", [
        "No official crafting recipe or usable imported wiki recipe was found."
    ]


def build_debug_recipe_source_report(
    item_name: str,
    rebuild_item_index: bool = False,
) -> str:
    """Build a focused report that explains which recipe source would be used."""

    cache = load_item_cache(DEFAULT_ITEM_CACHE_PATH)
    reference_database = reference_database_if_available()
    index: dict[str, Any] | None = None

    if not reference_database:
        index = ensure_item_name_index(
            DEFAULT_ITEM_NAME_INDEX_PATH,
            rebuild=rebuild_item_index,
        )

    resolved_item, warnings = validation_item_lookup(
        item_name,
        cache,
        DEFAULT_ITEM_CACHE_PATH,
        index,
        "Item",
        reference_database=reference_database,
    )

    lines = [
        "Recipe Source Debug",
        f"Requested item: {item_name}",
    ]

    if warnings:
        lines.append("Lookup warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    if not resolved_item:
        lines.append("Could not resolve the requested item to one final item ID.")
        return "\n".join(lines)

    final_item_id = int(resolved_item["item_id"])
    final_item_name = str(resolved_item.get("name", item_name))
    recipe_engine = RecipeEngine()
    debug_data = recipe_engine.debug_recipe_source(final_item_id, final_item_name)
    override_lookup = debug_data["override_lookup"]
    official_lookup = debug_data["official_recipe_lookup"]
    wiki_summary = debug_data["wiki_summary"]
    chosen_source = debug_data["chosen_source"]

    lines.extend(
        [
            f"Final item: {final_item_name} (item_id {final_item_id})",
            "",
            "Official API recipe lookup:",
            f"  - status: {official_lookup['status']}",
            f"  - reason: {official_lookup['reason']}",
        ]
    )

    if official_lookup.get("recipe_ids"):
        recipe_text = ", ".join(str(recipe_id) for recipe_id in official_lookup["recipe_ids"])
        lines.append(f"  - recipe_ids: {recipe_text}")

    override = override_lookup.get("override")
    lines.extend(
        [
            "",
            "Override lookup:",
            f"  - status: {override_lookup['status']}",
            f"  - reason: {override_lookup['reason']}",
        ]
    )

    if override:
        lines.append(f"  - type: {override.get('type', 'manual')}")
        lines.append(f"  - verified: {bool(override.get('verified', False))}")
        lines.append(f"  - ingredient_count: {len(override.get('ingredients', []))}")

    lines.extend(
        [
            "",
            "Imported wiki reference data:",
            f"  - wiki_recipes rows found: {debug_data['wiki_recipe_row_count']}",
            f"  - acquisition_options found: {debug_data['acquisition_option_count']}",
            f"  - multiple acquisition options: "
            f"{'yes' if wiki_summary.get('multiple_acquisition_options') else 'no'}",
            f"  - review_status: {wiki_summary.get('review_status') or 'n/a'}",
            f"  - source_url: {wiki_summary.get('source_url') or 'n/a'}",
            f"  - reason: {wiki_summary.get('reason', 'No wiki data found.')}",
        ]
    )

    lines.extend(
        [
            "",
            "Chosen source:",
            f"  - source: {chosen_source.get('source', 'unknown')}",
            f"  - reason: {chosen_source.get('reason', 'No final source decision was recorded.')}",
        ]
    )

    return "\n".join(lines)


def build_debug_wiki_name_report(wiki_text: str) -> str:
    """Show how one wiki item-name snippet is normalized and resolved."""

    name_details = normalize_wiki_item_text(wiki_text)
    reference_database = reference_database_if_available()
    lines = [
        "Wiki Name Debug",
        f"Original text: {name_details['original_text']}",
        f"Stripped text: {name_details['stripped_text'] or 'n/a'}",
        f"Display text: {name_details['display_text'] or 'n/a'}",
        f"Page title fallback: {name_details['page_title'] or 'n/a'}",
        f"Preferred normalized name: {name_details['normalized_name'] or 'n/a'}",
        "Resolution candidates:",
    ]

    if not name_details["candidates"]:
        lines.append("  - none")
    else:
        for candidate in name_details["candidates"]:
            lines.append(f"  - {candidate}")

    if not reference_database:
        lines.append("")
        lines.append(f"Reference database not found at {DEFAULT_REFERENCE_DB_PATH}.")
        return "\n".join(lines)

    selected_match: dict[str, Any] | None = None
    selected_candidate = ""
    lines.append("")
    lines.append("Reference database matches:")

    for candidate in name_details["candidates"]:
        matches = reference_database.item_name_matches(candidate)

        if len(matches) == 1 and selected_match is None:
            selected_match = matches[0]
            selected_candidate = candidate

        if not matches:
            lines.append(f"  - {candidate}: no exact match")
            continue

        match_text = ", ".join(
            f"{match.get('name', 'Unknown name')} (item_id {match['item_id']})"
            for match in matches[:10]
        )
        suffix = "" if len(matches) <= 10 else f" plus {len(matches) - 10} more"
        lines.append(f"  - {candidate}: {match_text}{suffix}")

    lines.append("")

    if selected_match:
        lines.append(
            "Chosen resolution: "
            f"{selected_match.get('name', selected_candidate)} "
            f"(item_id {selected_match['item_id']}) from candidate '{selected_candidate}'"
        )
    else:
        lines.append("Chosen resolution: none; add item_id or a verified override if needed.")

    return "\n".join(lines)


def build_validate_goals_report(
    config_path: Path,
    rebuild_item_index: bool = False,
) -> str:
    """Build a fast validation report for goals and templates."""

    template_count = validate_template_files(DEFAULT_TEMPLATES_DIR)
    targets = load_goals(config_path)
    cache = load_item_cache(DEFAULT_ITEM_CACHE_PATH)
    reference_database = reference_database_if_available()
    names_to_check: set[str] = set()

    for target in targets:
        if target.get("final_item_id") is None:
            lookup_name, _lookup_source = target_final_item_lookup_name(target)

            if lookup_name and not get_cached_item(cache, lookup_name):
                if reference_database:
                    continue

                names_to_check.add(lookup_name)

        for material in target.get("materials", []):
            if material_has_item_id(material):
                continue

            material_name = str(material.get("name", "")).strip()

            if material_name and not get_cached_item(cache, material_name):
                if reference_database:
                    continue

                names_to_check.add(material_name)

    index: dict[str, Any] | None = None
    index_warning = ""

    if names_to_check:
        try:
            index = ensure_item_name_index(
                DEFAULT_ITEM_NAME_INDEX_PATH,
                rebuild=rebuild_item_index,
            )
        except ItemLookupError as error:
            index_warning = (
                "Item name index could not be loaded or built. "
                f"Name checks may be incomplete. Details: {error}"
            )

    recipe_engine = RecipeEngine()
    lines = [
        "Guild Wars 2 Legendary Planner Goal Validation",
        "================================================",
        f"Config: {config_path}",
        f"Template files checked: {template_count:,}",
        f"Enabled targets: {len(targets):,}",
    ]

    if index_warning:
        lines.append(f"Warning: {index_warning}")

    lines.append("")

    if not targets:
        lines.append("No enabled targets found.")
        return "\n".join(lines)

    total_warnings = 0

    for target_number, target in enumerate(targets, start=1):
        target_name = str(target.get("name", "Unnamed target"))
        final_item_id = target_final_item_id(target)
        configured_final_item_name = str(target.get("final_item_name", "")).strip()
        lookup_name, lookup_source = target_final_item_lookup_name(target)
        target_warnings: list[str] = []
        resolved_final_item_name = configured_final_item_name
        final_resolution = "no final item configured"

        if final_item_id is not None:
            final_resolution = "yes (explicit final_item_id)"
        elif lookup_name:
            label = (
                "fallback target name"
                if lookup_source == "target name"
                else "final_item_name"
            )
            resolved_item, lookup_warnings = validation_item_lookup(
                lookup_name,
                cache,
                DEFAULT_ITEM_CACHE_PATH,
                index,
                label,
                reference_database,
            )
            target_warnings.extend(lookup_warnings)

            if resolved_item:
                final_item_id = int(resolved_item["item_id"])
                resolved_final_item_name = resolved_item.get("name", lookup_name)
                final_resolution = f"yes ({label})"
            else:
                final_resolution = f"no ({label} did not resolve)"
        elif target.get("auto_resolve_final_item_from_name", True) is False:
            final_resolution = "skipped (fallback disabled)"

        material_name_warnings: list[str] = []

        for material in target.get("materials", []):
            if material_has_item_id(material):
                continue

            material_name = str(material.get("name", "")).strip()

            if not material_name:
                material_name_warnings.append("A material is missing both item_id and name.")
                continue

            _material_item, lookup_warnings = validation_item_lookup(
                material_name,
                cache,
                DEFAULT_ITEM_CACHE_PATH,
                index,
                "material name",
                reference_database,
            )
            material_name_warnings.extend(lookup_warnings)

        target_warnings.extend(material_name_warnings)
        recipe_lookup, recipe_warnings = validate_recipe_lookup(
            recipe_engine,
            final_item_id,
            resolved_final_item_name,
        )
        target_warnings.extend(recipe_warnings)
        total_warnings += len(target_warnings)

        lines.append(f"Target {target_number}: {target_name}")
        lines.append(
            "  final_item_id: "
            f"{final_item_id if final_item_id is not None else 'not set'}"
        )
        lines.append(
            "  final_item_name: "
            f"{resolved_final_item_name if resolved_final_item_name else 'not set'}"
        )
        lines.append(f"  Final item resolution: {final_resolution}")
        lines.append(f"  Recipe lookup: {recipe_lookup}")

        if target_warnings:
            lines.append("  Warnings:")
            for warning in target_warnings:
                lines.append(f"    - {warning}")

        lines.append("")

    lines.append(f"Validation warnings: {total_warnings:,}")
    return "\n".join(lines).rstrip()


def resolve_targets_for_override_generation(
    config_path: Path,
    rebuild_item_index: bool = False,
) -> list[dict[str, Any]]:
    """Load targets and resolve final item IDs without account scanning."""

    targets = load_goals(config_path)
    return resolve_target_final_item_names(
        targets,
        DEFAULT_ITEM_CACHE_PATH,
        DEFAULT_ITEM_NAME_INDEX_PATH,
        rebuild_item_index=rebuild_item_index,
    )


def resolve_targets_for_reference_update(
    config_path: Path,
    rebuild_item_index: bool = False,
) -> list[dict[str, Any]]:
    """Load targets and resolve names needed for targeted reference imports."""

    targets = load_goals(config_path)
    targets = resolve_goal_material_names(
        targets,
        DEFAULT_ITEM_CACHE_PATH,
        DEFAULT_ITEM_NAME_INDEX_PATH,
        rebuild_item_index=rebuild_item_index,
    )
    return resolve_target_final_item_names(
        targets,
        DEFAULT_ITEM_CACHE_PATH,
        DEFAULT_ITEM_NAME_INDEX_PATH,
        rebuild_item_index=rebuild_item_index,
    )


def load_override_database(overrides_path: Path) -> dict[str, Any]:
    """Load recipe_overrides.json, creating the default shape if needed."""

    if not overrides_path.exists():
        return {
            "version": 1,
            "instructions": [
                "Use this file for manual or special-case recipe data the official GW2 API cannot fully resolve.",
                "final_item_id in a target is still the most explicit way to identify a legendary.",
                "Overrides may use item_id or name. item_id is preferred because it cannot be ambiguous.",
                "Supported types: manual, currency, achievement, collection, mystic_forge, vendor, time_gated, account_bound.",
                "Add ingredients only after manually verifying them. Ingredients should use item_id when possible.",
            ],
            "preferred_recipes_by_output": {},
            "overrides": [],
        }

    with overrides_path.open("r", encoding="utf-8") as overrides_file:
        overrides = json.load(overrides_file)

    if not isinstance(overrides, dict):
        raise ValueError(f"{overrides_path} must contain one JSON object.")

    if not isinstance(overrides.get("overrides", []), list):
        raise ValueError(f"{overrides_path} field 'overrides' must be a list.")

    overrides.setdefault("version", 1)
    overrides.setdefault("preferred_recipes_by_output", {})
    overrides.setdefault("overrides", [])
    return overrides


def save_override_database(overrides_path: Path, overrides: dict[str, Any]) -> None:
    """Save recipe_overrides.json."""

    overrides_path.parent.mkdir(parents=True, exist_ok=True)
    overrides_path.write_text(
        json.dumps(overrides, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def override_database_has_item(overrides: dict[str, Any], item_id: int, name: str) -> bool:
    """Check whether an override already exists for this item ID or name."""

    normalized_name = normalize_item_name(name) if name else ""

    for override in overrides.get("overrides", []):
        if not isinstance(override, dict):
            continue

        override_item_id = override.get("item_id", override.get("id"))

        if override_item_id is not None and int(override_item_id) == item_id:
            return True

        override_name = str(override.get("name", "")).strip()

        if normalized_name and normalize_item_name(override_name) == normalized_name:
            return True

    return False


def target_missing_override_stub(
    recipe_engine: RecipeEngine,
    overrides: dict[str, Any],
    target: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return an override stub when a resolved final item needs one."""

    final_item_id = target_final_item_id(target)
    warnings = list(target.get(RECIPE_ENGINE_WARNINGS_FIELD, []))

    if final_item_id is None:
        return None, warnings

    final_item_name = str(
        target.get("final_item_name") or target.get("name") or f"Item {final_item_id}"
    ).strip()

    if recipe_engine.override_for_item(final_item_id, final_item_name):
        return None, warnings

    if override_database_has_item(overrides, final_item_id, final_item_name):
        return None, warnings

    try:
        recipe_ids = recipe_engine.recipe_ids_for_output(final_item_id)
    except RecipeApiError as error:
        warnings.append(f"Skipped override generation because recipe lookup failed: {error}")
        return None, warnings

    if recipe_ids:
        return None, warnings

    return (
        {
            "item_id": final_item_id,
            "name": final_item_name,
            "type": "mystic_forge",
            "verified": False,
            "ingredients": [],
            "notes": "Fill this with verified legendary recipe ingredients.",
        },
        warnings,
    )


def target_needs_wiki_import(
    recipe_engine: RecipeEngine,
    target: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Return a wiki import item for an unresolved configured target."""

    final_item_id = target_final_item_id(target)
    warnings = list(target.get(RECIPE_ENGINE_WARNINGS_FIELD, []))

    if final_item_id is None:
        return None, warnings

    final_item_name = str(
        target.get("final_item_name") or target.get("name") or f"Item {final_item_id}"
    ).strip()

    try:
        recipe_ids = recipe_engine.recipe_ids_for_output(final_item_id)
    except RecipeApiError as error:
        warnings.append(f"Skipped wiki import because recipe lookup failed: {error}")
        return None, warnings

    if recipe_ids:
        return None, warnings

    override = recipe_engine.override_for_item(final_item_id, final_item_name)

    if override and override.get("verified") and override.get("ingredients"):
        return None, warnings

    return {"item_id": final_item_id, "name": final_item_name}, warnings


def collect_wiki_import_items_for_targets(
    targets: list[dict[str, Any]],
    recipe_engine: RecipeEngine,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collect unique unresolved target and configured ingredient items."""

    items_by_id: dict[int, dict[str, Any]] = {}
    warnings: list[str] = []

    for target in targets:
        item, target_warnings = target_needs_wiki_import(recipe_engine, target)
        warnings.extend(target_warnings)

        if item is None:
            pass
        else:
            items_by_id[int(item["item_id"])] = item

        final_item_id = target_final_item_id(target)

        if final_item_id is not None:
            try:
                recipe_tree = recipe_engine.resolve_recipe_tree(final_item_id)
            except RecipeApiError as error:
                warnings.append(
                    f"Skipped wiki import for recipe-tree gaps in "
                    f"{target.get('name', 'Unnamed target')} because recipe lookup "
                    f"failed: {error}"
                )
            else:
                for manual_step in recipe_tree.get("unknown_manual_steps", []):
                    manual_item_id = manual_step.get("item_id")

                    if manual_item_id is None:
                        continue

                    manual_item_id = int(manual_item_id)
                    manual_item_name = str(
                        manual_step.get("name") or f"Item {manual_item_id}"
                    )
                    override = recipe_engine.override_for_item(
                        manual_item_id,
                        manual_item_name,
                    )

                    if override and override.get("verified") and override.get("ingredients"):
                        continue

                    items_by_id[manual_item_id] = {
                        "item_id": manual_item_id,
                        "name": manual_item_name,
                    }

        target_name = str(target.get("name", "Unnamed target"))
        material_entries = normalize_goal_entries(
            target_name,
            target.get("materials", []),
            ("item_id", "id"),
            "materials",
        )

        for material in material_entries:
            material_id = int(material["id"])
            material_name = str(material.get("name") or f"Item {material_id}")

            try:
                recipe_ids = recipe_engine.recipe_ids_for_output(material_id)
            except RecipeApiError as error:
                warnings.append(
                    f"Skipped wiki import for {material_name} because recipe lookup failed: {error}"
                )
                continue

            if recipe_ids:
                continue

            override = recipe_engine.override_for_item(material_id, material_name)

            if override and override.get("verified") and override.get("ingredients"):
                continue

            items_by_id[material_id] = {
                "item_id": material_id,
                "name": material_name,
            }

    return list(items_by_id.values()), warnings


def build_generate_missing_overrides_report(
    config_path: Path,
    rebuild_item_index: bool = False,
) -> str:
    """Create missing legendary override stubs for resolved targets."""

    validate_template_files(DEFAULT_TEMPLATES_DIR)
    targets = resolve_targets_for_override_generation(
        config_path,
        rebuild_item_index=rebuild_item_index,
    )
    overrides_path = Path("data") / "recipe_overrides.json"
    overrides = load_override_database(overrides_path)
    recipe_engine = RecipeEngine()
    created_stubs: list[dict[str, Any]] = []
    warnings: list[str] = []

    for target in targets:
        stub, target_warnings = target_missing_override_stub(
            recipe_engine,
            overrides,
            target,
        )
        warnings.extend(target_warnings)

        if stub is None:
            continue

        overrides["overrides"].append(stub)
        created_stubs.append(stub)

    if created_stubs:
        save_override_database(overrides_path, overrides)

    lines = [
        "Guild Wars 2 Legendary Planner Missing Override Generator",
        "===========================================================",
        f"Config: {config_path}",
        f"Override file: {overrides_path}",
        f"Stubs created: {len(created_stubs):,}",
    ]

    if created_stubs:
        lines.append("")
        lines.append("Created override stubs:")

        for stub in created_stubs:
            lines.append(f"  - {stub['name']} (item_id {stub['item_id']})")
    else:
        lines.append("")
        lines.append("No missing override stubs needed.")

    if warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines).rstrip()


def build_update_source_steps_report(
    config_path: Path,
    rebuild_item_index: bool = False,
) -> str:
    """Import targeted GW2 Wiki pages for current manual/source step items."""

    validate_template_files(DEFAULT_TEMPLATES_DIR)
    targets = resolve_targets_for_reference_update(
        config_path,
        rebuild_item_index=rebuild_item_index,
    )

    # Build the engine before creating a missing DB so it can still fall back to
    # the legacy public API cache when the reference DB has not been set up yet.
    recipe_engine = RecipeEngine()
    wiki_items, wiki_warnings = collect_wiki_import_items_for_targets(
        targets,
        recipe_engine,
    )
    database = ReferenceDatabase(DEFAULT_REFERENCE_DB_PATH)
    database.initialize_schema()
    database.apply_overrides(Path("data") / "recipe_overrides.json")
    wiki_counts = database.import_wiki_recipes_for_items(wiki_items)

    lines = [
        "Guild Wars 2 Planner Source Step Update",
        "========================================",
        f"Database: {DEFAULT_REFERENCE_DB_PATH}",
        f"Source-step items checked: {len(wiki_items):,}",
        f"Targeted wiki pages imported: {wiki_counts['wiki_pages']:,}",
        f"Wiki acquisition options imported: {wiki_counts['acquisition_options']:,}",
    ]

    if wiki_items:
        lines.append("")
        lines.append("Imported source-step pages:")

        for item in sorted(wiki_items, key=lambda row: row["name"]):
            lines.append(f"  - {item['name']} (item_id {item['item_id']})")
    else:
        lines.append("")
        lines.append("No source-step wiki pages needed for the current goals.")

    if wiki_warnings:
        lines.append("")
        lines.append("Warnings:")
        for warning in wiki_warnings:
            lines.append(f"  - {warning}")

    return "\n".join(lines).rstrip()


def build_debug_source_step_report(
    item_name: str,
    rebuild_item_index: bool = False,
) -> str:
    """Show imported source-step data for one account-bound/manual item."""

    cache = load_item_cache(DEFAULT_ITEM_CACHE_PATH)
    reference_database = reference_database_if_available()
    index: dict[str, Any] | None = None

    if not reference_database:
        index = ensure_item_name_index(
            DEFAULT_ITEM_NAME_INDEX_PATH,
            rebuild=rebuild_item_index,
        )

    resolved_item, warnings = validation_item_lookup(
        item_name,
        cache,
        DEFAULT_ITEM_CACHE_PATH,
        index,
        "Source-step item",
        reference_database=reference_database,
    )

    lines = [
        "Source Step Debug",
        f"Requested item: {item_name}",
    ]

    if warnings:
        lines.append("Lookup warnings:")
        for warning in warnings:
            lines.append(f"  - {warning}")

    if not resolved_item:
        lines.append("Could not resolve the requested item to one item ID.")
        return "\n".join(lines)

    item_id = int(resolved_item["item_id"])
    resolved_name = str(resolved_item.get("name", item_name))
    recipe_engine = RecipeEngine()
    source_step = recipe_engine.source_step_summary_for_item(item_id, resolved_name)
    wiki_summary = recipe_engine.wiki_recipe_summary_for_item(item_id, resolved_name)

    lines.extend(
        [
            f"Item: {resolved_name} (item_id {item_id})",
            f"Raw wiki page title: {source_step.get('raw_wiki_page_title') or 'n/a'}",
            f"Wiki rows found: {source_step.get('wiki_rows_found', 0):,}",
            f"Wiki page found: {'yes' if source_step.get('wiki_page_found') else 'no'}",
            f"Detected source type: {source_step.get('source_type') or 'unknown'}",
            f"Source URL: {source_step.get('source_url') or 'n/a'}",
            f"Review status: {source_step.get('review_status') or 'n/a'}",
            "Acquisition options found: "
            f"{source_step.get('acquisition_option_count', 0):,}",
            f"Source-step parse status: {source_step.get('source_parse_status') or 'n/a'}",
            f"Source-step parse reason: {source_step.get('source_parse_reason') or 'n/a'}",
            "Final normal-report summary: "
            f"{source_step.get('source_step_summary') or source_step.get('source_summary') or 'not imported yet'}",
            "",
            "Recipe-like wiki data:",
            f"  - parsed recipes: {wiki_summary.get('parsed_recipe_count', 0):,}",
            f"  - selected for auto-expansion: "
            f"{'yes' if wiki_summary.get('selected_recipe') else 'no'}",
            f"  - reason: {wiki_summary.get('reason', 'n/a')}",
        ]
    )

    parsed_ingredients = list(source_step.get("parsed_recipe_ingredients", []))

    lines.append("")
    lines.append("Parsed source-step recipe ingredients:")

    if parsed_ingredients:
        for ingredient in parsed_ingredients:
            lines.append(f"  - {ingredient['amount']:,} {ingredient['name']}")
    else:
        lines.append("  - none")

    if not source_step.get("wiki_rows_found"):
        lines.append("")
        lines.append(
            "Run python gw2_legendary_planner.py --update-source-steps "
            "to import targeted wiki source-step pages."
        )

    return "\n".join(lines).rstrip()


def build_reference_db_setup_report(
    config_path: Path,
    update: bool = False,
    rebuild_item_index: bool = False,
) -> str:
    """Build or refresh the public reference database and report the result."""

    action = "Update" if update else "Setup"
    counts = setup_reference_database(
        DEFAULT_REFERENCE_DB_PATH,
        Path("data") / "recipe_overrides.json",
    )
    targets = resolve_targets_for_reference_update(
        config_path,
        rebuild_item_index=rebuild_item_index,
    )
    recipe_engine = RecipeEngine()
    wiki_items, wiki_warnings = collect_wiki_import_items_for_targets(
        targets,
        recipe_engine,
    )
    database = ReferenceDatabase(DEFAULT_REFERENCE_DB_PATH)
    wiki_counts = database.import_wiki_recipes_for_items(wiki_items)
    return "\n".join(
        [
            f"Guild Wars 2 Planner Reference Database {action}",
            "=" * (48 + len(action)),
            f"Database: {DEFAULT_REFERENCE_DB_PATH}",
            f"Items imported: {counts['items']:,}",
            f"Official recipes imported: {counts['recipes']:,}",
            f"Recipe overrides applied: {counts['overrides']:,}",
            f"Targeted wiki pages imported: {wiki_counts['wiki_pages']:,}",
            f"Wiki acquisition options imported: {wiki_counts['acquisition_options']:,}",
        ]
        + (
            ["", "Warnings:"]
            + [f"  - {warning}" for warning in wiki_warnings]
            if wiki_warnings
            else []
        )
    )


def build_reference_db_status_report() -> str:
    """Build a readable reference database status report."""

    status = reference_database_status(DEFAULT_REFERENCE_DB_PATH)
    lines = [
        "Guild Wars 2 Planner Reference Database Status",
        "================================================",
        f"Database: {status.get('path', DEFAULT_REFERENCE_DB_PATH)}",
    ]

    if status.get("exists") != "true":
        lines.extend(
            [
                "Status: missing",
                "Run python gw2_legendary_planner.py --setup-reference-db to create it.",
            ]
        )
        return "\n".join(lines)

    lines.append("Status: ready")

    for key in (
        "schema_version",
        "items_rows",
        "item_names_rows",
        "recipes_rows",
        "recipe_ingredients_rows",
        "recipe_outputs_rows",
        "wiki_recipes_rows",
        "acquisition_options_rows",
        "recipe_overrides_rows",
        "items_updated_at",
        "recipes_updated_at",
        "overrides_updated_at",
        "wiki_recipes_updated_at",
        "wiki_recipe_import_count",
        "wiki_acquisition_option_count",
    ):
        if key in status:
            lines.append(f"{key}: {status[key]}")

    return "\n".join(lines)


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
                "paths": list(entry.get("paths", [])),
                "reason": entry.get("reason", ""),
                "data_confidence": entry.get("data_confidence", ""),
                "resolution_source": entry.get("resolution_source", ""),
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
            target_materials(target),
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
                    "paths": list(entry.get("paths", [])),
                    "reason": entry.get("reason", ""),
                    "data_confidence": entry.get("data_confidence", ""),
                    "resolution_source": entry.get("resolution_source", ""),
                }
            )

    return missing_entries


def collect_missing_item_ids(
    targets: list[dict[str, Any]],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    recipe_engine: RecipeEngine | None = None,
) -> set[int]:
    """Collect item IDs that are missing from at least one enabled target."""

    missing_item_ids: set[int] = set()

    for target in targets:
        if is_target_unlocked(target, legendary_armory):
            continue

        target_name = target.get("name", "Unnamed target")
        material_entries = normalize_goal_entries(
            target_name,
            target_materials(target),
            ("item_id", "id"),
            "materials",
        )

        for entry in material_entries:
            if item_counts.get(entry["id"], 0) < entry["amount"]:
                if recipe_engine and recipe_engine.is_item_non_priceable_override(
                    entry["id"],
                    entry.get("name"),
                ):
                    debug_print(
                        f"Skipping Trading Post price for override item {entry['id']}."
                    )
                    continue

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


def confidence_from_resolution(
    resolution: dict[str, Any],
    entry: dict[str, Any] | None = None,
) -> str:
    """Turn recipe-source metadata into a short trust label."""

    source = str(resolution.get("source", "")).strip()
    review_status = str(resolution.get("review_status", "")).strip()

    if source == "override_verified_ingredients":
        return "manual_override_verified"

    if source in {"wiki_recipe", "source_step_recipe"}:
        if "manual_review_needed" in review_status:
            return "manual_review_needed"

        return review_status or "wiki_imported_unreviewed"

    if source in {
        "wiki_manual_gap",
        "manual_recipe_gap",
        "manual_preferred_recipe_needed",
        "account_bound_manual_source_step",
        "account_bound_manual_stop",
    }:
        return "manual_review_needed"

    if source == "manual_cycle_stop" or source == "manual_max_depth_stop":
        return "ambiguous"

    if source == "official_api" or source == "terminal_material":
        return "official_api"

    if entry and "account-bound" in str(entry.get("reason", "")).casefold():
        return "manual_review_needed"

    return source or "official_api"


def missing_item_by_name(
    missing_items: list[dict[str, Any]],
    item_name: str,
) -> dict[str, Any] | None:
    """Find one missing item by exact normalized name."""

    for item in missing_items:
        if is_named_item(item, item_name):
            return item

    return None


def is_provisioners_token(missing_entry: dict[str, Any]) -> bool:
    """Check whether a missing item looks like a Provisioner's Token."""

    normalized_name = normalize_item_name(missing_entry["name"])
    return "provisioner" in normalized_name and "token" in normalized_name


def is_precursor_step(missing_entry: dict[str, Any]) -> bool:
    """Check for known precursor-style items in expanded legendary trees."""

    normalized_name = normalize_item_name(missing_entry["name"])
    return "precursor" in normalized_name or normalized_name in {"nyr hrammr"}


def item_category(entry: dict[str, Any], manual_step: bool = False) -> str:
    """Categorize a missing entry for breakdown output."""

    normalized_name = normalize_item_name(str(entry.get("name", "")))
    source_type = normalize_item_name(str(entry.get("source_type", "")))
    confidence = normalize_item_name(str(entry.get("data_confidence", "")))

    if is_precursor_step({"name": entry.get("name", ""), "missing": entry.get("missing", 0)}):
        return "precursor"

    if source_type in {"achievement", "collection"}:
        return "achievement/collection step"

    if confidence == "ambiguous" or "manual_review" in confidence or "manual review" in confidence:
        if manual_step:
            return "ambiguous/manual review"

    if normalized_name in {"mystic runestone", "bloodstone shard", "eldritch scroll"}:
        return "vendor item"

    if normalized_name in {"gift of research", "gift of the mists"}:
        return "account-bound source step"

    if "provisioner" in normalized_name and "token" in normalized_name:
        return "currency-like item"

    if manual_step:
        if source_type in {"vendor", "mystic forge", "mystic_forge"}:
            return "vendor item" if normalized_name in {"mystic runestone", "bloodstone shard"} else "account-bound source step"

        return "account-bound source step"

    if "gift of" in normalized_name:
        return "craftable component"

    return "normal material"


def format_recipe_path(path_text: str) -> str:
    """Format stored recipe paths for CLI output."""

    return " > ".join(part.strip() for part in str(path_text).split("->") if part.strip())


def important_path_entry(entry: dict[str, Any]) -> bool:
    """Decide whether normal breakdown output should show a Used in line."""

    normalized_name = normalize_item_name(str(entry.get("name", "")))
    category = str(entry.get("category", ""))
    important_names = {
        "mystic clover",
        "mystic runestone",
        "bloodstone shard",
        "gift of research",
        "gift of the mists",
        "nyr hrammr",
        "orichalcum ore",
    }
    return normalized_name in important_names or category in {
        "precursor",
        "vendor item",
        "account-bound source step",
        "achievement/collection step",
        "ambiguous/manual review",
    }


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

    if missing_item_by_name(missing_items, "Mystic Runestone"):
        recommendations.append(
            "Mystic Runestone: check the source-step/vendor path before treating it "
            "like a generic material."
        )

    if missing_item_by_name(missing_items, "Bloodstone Shard"):
        recommendations.append(
            "Bloodstone Shard: check your Spirit Shards and Mystic Forge vendor access."
        )

    if missing_item_by_name(missing_items, "Gift of Research"):
        recommendations.append(
            "Source step: Gift of Research needs research notes; review that branch before buying."
        )

    if missing_item_by_name(missing_items, "Gift of the Mists"):
        recommendations.append("Source step: Gift of the Mists is still needed; review that branch.")

    if any(is_named_item(item, "Gift of Battle") for item in missing_items):
        recommendations.append("Gift of Battle: put today's play time into WvW reward track progress.")

    precursor_items = [item for item in missing_items if is_precursor_step(item)]

    for precursor in precursor_items[:1]:
        recommendations.append(
            f"Precursor step: {precursor['name']} is still needed for final assembly."
        )

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


def target_has_verified_final_item(target: dict[str, Any]) -> bool:
    """Check whether the target already has a resolved final item identity."""

    final_item_id = target_final_item_id(target)
    final_item_name = str(target.get("final_item_name", "")).strip()
    return final_item_id is not None and bool(final_item_name)


def step_is_stale_final_item_todo(step: dict[str, Any]) -> bool:
    """Detect old checklist items that only ask us to verify final_item_id."""

    step_text = normalize_item_name(f"{step['name']} {step.get('notes', '')}")
    return "final_item_id" in step_text or "final item id" in step_text


def hide_stale_final_item_steps(
    target: dict[str, Any],
    steps: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hide final-item verification TODOs after the final item is resolved."""

    if not target_has_verified_final_item(target):
        return steps

    return [step for step in steps if not step_is_stale_final_item_todo(step)]


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

    if target.get(RECIPE_ENGINE_WARNINGS_FIELD) or target.get(RECIPE_UNKNOWN_STEPS_FIELD):
        return True

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
    target: dict[str, Any],
    is_unlocked: bool,
    needs_recipe_data: bool,
    missing_items: list[dict[str, Any]],
    missing_currencies: list[dict[str, Any]],
    incomplete_steps: list[dict[str, Any]],
) -> str:
    """Return the short status label shown beside a target."""

    if is_unlocked:
        return "Complete"

    if target_final_item_id(target) is None:
        return "Needs final item data"

    recipe_tree = target.get(RECIPE_TREE_FIELD, {})
    has_auto_materials = bool(target.get(AUTO_RECIPE_MATERIALS_FIELD, []))
    has_manual_source_steps = bool(target.get(RECIPE_UNKNOWN_STEPS_FIELD, []))
    used_wiki_fallback = bool(recipe_tree.get("wiki_sources_used", []))

    if has_manual_source_steps and (has_auto_materials or used_wiki_fallback):
        return "Partially resolved"

    if has_manual_source_steps:
        return "Needs manual source data"

    if missing_items or missing_currencies:
        return "In progress"

    if incomplete_steps:
        return "Needs manual source data"

    if needs_recipe_data:
        return "Needs recipe data"

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
        target_materials(target),
        ("item_id", "id"),
        "materials",
    )
    currency_entries = normalize_goal_entries(
        target_name,
        target.get("currencies", []),
        ("currency_id", "id"),
        "currencies",
    )
    steps = hide_stale_final_item_steps(
        target,
        normalize_steps(target_name, target.get("steps", [])),
    )
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
        "recipe_engine_warnings": list(target.get(RECIPE_ENGINE_WARNINGS_FIELD, [])),
        "recipe_unknown_manual_steps": list(target.get(RECIPE_UNKNOWN_STEPS_FIELD, [])),
        "needs_recipe_data": needs_recipe_data,
        "missing_items": missing_items,
        "missing_currencies": missing_currencies,
        "status_label": target_status_label(
            target,
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


def make_recipe_engine_lines(
    target: dict[str, Any],
    detailed: bool = False,
    debug: bool = False,
) -> list[str]:
    """Build report lines for automatic recipe resolution."""

    recipe_tree = target.get(RECIPE_TREE_FIELD)
    warnings = list(target.get(RECIPE_ENGINE_WARNINGS_FIELD, []))
    manual_steps = list(target.get(RECIPE_UNKNOWN_STEPS_FIELD, []))
    auto_materials = list(target.get(AUTO_RECIPE_MATERIALS_FIELD, []))
    expanded_source_steps = list(recipe_tree.get("expanded_source_steps", [])) if recipe_tree else []
    satisfied_intermediates = (
        list(recipe_tree.get("satisfied_intermediates", [])) if recipe_tree else []
    )

    if not recipe_tree and not warnings and not manual_steps:
        if detailed:
            return ["    Automatic recipe lookup did not run."]

        return []

    lines: list[str] = []

    if recipe_tree:
        recipes_used = recipe_tree.get("recipes_used", [])
        craftable_ingredients = recipe_tree.get("craftable_ingredients", [])
        wiki_sources = recipe_tree.get("wiki_sources_used", [])
        lines.append(
            f"    Official API recipes used: {len(recipes_used):,}; "
            f"terminal materials added: {len(auto_materials):,}; "
            f"craftable ingredient types found: {len(craftable_ingredients):,}."
        )

        if detailed and wiki_sources:
            lines.append("    GW2 Wiki fallback data used:")
            for wiki_source in wiki_sources:
                option_count = int(wiki_source.get("acquisition_option_count", 0))
                lines.append(
                    f"      - {wiki_source['name']}: source {wiki_source['source']}; "
                    f"review_status {wiki_source['review_status'] or 'n/a'}; "
                    f"acquisition options found {option_count:,}."
                )
                if wiki_source.get("multiple_acquisition_options"):
                    lines.append("        Multiple acquisition options need review.")
                if wiki_source.get("source_url"):
                    lines.append(f"        Source URL: {wiki_source['source_url']}")

        if detailed and craftable_ingredients:
            lines.append("    Craftable ingredients in the resolved tree:")
            for ingredient in craftable_ingredients:
                if ingredient.get("recipe_source") == "wiki":
                    lines.append(
                        f"      - {ingredient['name']}: {ingredient['amount']:,} "
                        "via GW2 Wiki recipe data"
                    )
                elif ingredient.get("recipe_source") == "source_step_wiki":
                    lines.append(
                        f"      - {ingredient['name']}: {ingredient['amount']:,} "
                        "via expanded source-step wiki recipe"
                    )
                else:
                    lines.append(
                        f"      - {ingredient['name']}: {ingredient['amount']:,} "
                        f"via recipe {ingredient['recipe_id']}"
                    )

    if expanded_source_steps:
        lines.append("    Source-step recipes expanded:")
        for source_step in expanded_source_steps:
            lines.append(
                f"      - {source_step['name']}: {source_step['amount']:,} needed; "
                f"expanded {source_step['ingredient_count']:,} ingredients."
            )
            if source_step.get("source_step_summary"):
                lines.append(f"        Summary: {source_step['source_step_summary']}")
            if source_step.get("source_url"):
                lines.append(f"        Source URL: {source_step['source_url']}")

    if satisfied_intermediates:
        lines.append("    Satisfied intermediate items:")
        for intermediate in satisfied_intermediates:
            used_amount = int(intermediate.get("amount", 0))
            remaining_amount = int(intermediate.get("remaining_amount", 0))

            if remaining_amount:
                lines.append(
                    f"      - {intermediate['name']}: have {used_amount:,}; "
                    f"expanded remaining {remaining_amount:,}."
                )
            else:
                lines.append(
                    f"      - {intermediate['name']}: have {used_amount:,}, "
                    "branch satisfied."
                )

    if debug and recipe_tree and recipe_tree.get("debug_events"):
        lines.append("    Debug:")
        for event in recipe_tree["debug_events"]:
            lines.append(f"      - {event}")

    if manual_steps:
        lines.append("    Manual/source steps still needed:")
        for manual_step in manual_steps:
            lines.append(
                f"      - {manual_step['name']}: {manual_step['amount']:,} needed."
            )
            source_url = str(manual_step.get("source_url", "")).strip()
            review_status = str(manual_step.get("review_status", "")).strip()
            source_type = str(manual_step.get("source_type", "unknown")).strip()
            source_summary = str(
                manual_step.get("source_step_summary")
                or manual_step.get("source_summary", "")
            ).strip()
            acquisition_option_count = int(
                manual_step.get("acquisition_option_count", 0)
            )

            if source_url or manual_step.get("wiki_page_found"):
                lines.append("        Source: GW2 Wiki")

            if source_type and source_type != "unknown":
                lines.append(f"        Type: {source_type}")

            if source_summary:
                lines.append(f"        Summary: {source_summary}")
            elif manual_step.get("wiki_page_found"):
                lines.append(
                    "        Summary: Wiki page found, but structured source data "
                    "could not be parsed cleanly."
                )

            if acquisition_option_count:
                lines.append(
                    f"        Acquisition options found: {acquisition_option_count:,}"
                )

            if review_status and review_status != "not_imported":
                lines.append(f"        Review status: {review_status}")

            if source_url:
                lines.append(f"        Source URL: {source_url}")

            if detailed and manual_step.get("reason"):
                lines.append(f"        Notes: {manual_step['reason']}")

    if warnings:
        lines.append("    Warnings:")
        for warning in warnings:
            lines.append(f"      - {warning}")

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
        biggest_missing_entries = sorted(
            target_status["missing_items"] + target_status["missing_currencies"],
            key=lambda entry: entry["missing"],
            reverse=True,
        )[:3]
        missing_names = ", ".join(entry["name"] for entry in biggest_missing_entries)
        recommendations.append(
            "Next recipe branch: work the highest-priority missing entries now showing "
            f"in the tree: {missing_names}. If one is account-bound, use its source-step "
            "notes; otherwise gather, craft, or buy the normal materials as needed."
        )

    return recommendations


def build_configuration_recommendations(target_status: dict[str, Any]) -> list[str]:
    """Build recommendations for recipe/template TODOs and setup work."""

    recommendations: list[str] = []

    if target_recipe_status_needs_work(target_status["target"]):
        if target_has_verified_final_item(target_status["target"]):
            recommendations.append(
                f"Recipe data: review {target_status['name']}'s remaining source steps, "
                "then fill in exact materials and currencies where known."
            )
        else:
            recommendations.append(
                f"Recipe data: verify {target_status['name']}'s final item, then fill in "
                "exact materials and currencies where known."
            )

    for warning in target_status["recipe_engine_warnings"][:2]:
        recommendations.append(f"Recipe engine: {warning}")

    for manual_step in target_status["recipe_unknown_manual_steps"][:2]:
        recommendations.append(
            f"Manual/source step: review {manual_step['name']} "
            f"({manual_step['amount']:,} needed). {manual_step['reason']}"
        )

    for step in target_status["configuration_steps"]:
        recommendations.append(step_recommendation_text(step))

    # Keep repeated template TODOs from making the recommendation block noisy.
    unique_recommendations: list[str] = []
    for recommendation in recommendations:
        if recommendation not in unique_recommendations:
            unique_recommendations.append(recommendation)

    return unique_recommendations


def component_status_label(component: dict[str, Any]) -> str:
    """Return a short status label for one major component."""

    if component["satisfied"]:
        return "owned"

    if component["is_precursor"]:
        return "missing precursor"

    if component["manual_step"]:
        return "source/manual step"

    if component["expanded"]:
        return "expanded"

    return "missing"


def build_major_components(
    recipe_tree: dict[str, Any],
    item_counts: dict[int, int],
) -> list[dict[str, Any]]:
    """Build user-facing major components from direct recipe children."""

    expanded_ids = {
        int(entry["item_id"])
        for entry in recipe_tree.get("expanded_source_steps", [])
        if entry.get("item_id") is not None
    }
    craftable_ids = {
        int(entry["item_id"])
        for entry in recipe_tree.get("craftable_ingredients", [])
        if entry.get("item_id") is not None
    }
    manual_ids = {
        int(entry["item_id"])
        for entry in recipe_tree.get("unknown_manual_steps", [])
        if entry.get("item_id") is not None
    }
    satisfied_by_id = {
        int(entry["item_id"]): entry
        for entry in recipe_tree.get("satisfied_intermediates", [])
        if entry.get("item_id") is not None
    }

    components: list[dict[str, Any]] = []
    final_item_name = str(recipe_tree.get("final_item_name", "Target"))

    for component in recipe_tree.get("major_components", []):
        item_id = component.get("item_id")
        item_id_int = int(item_id) if item_id is not None else None
        needed = int(component.get("amount", 0))
        owned = item_counts.get(item_id_int, 0) if item_id_int is not None else 0
        satisfied_entry = satisfied_by_id.get(item_id_int) if item_id_int is not None else None
        satisfied_amount = int(satisfied_entry.get("amount", 0)) if satisfied_entry else 0
        missing = max(needed - max(owned, satisfied_amount), 0)
        display_entry = {
            "id": item_id_int or 0,
            "name": component.get("name", "Unnamed component"),
            "missing": missing,
        }
        branch = {
            "item_id": item_id_int,
            "name": component.get("name", "Unnamed component"),
            "needed": needed,
            "owned": owned,
            "satisfied_amount": satisfied_amount,
            "missing": missing,
            "satisfied": satisfied_amount >= needed or (owned >= needed and needed > 0),
            "expanded": item_id_int in expanded_ids or item_id_int in craftable_ids,
            "manual_step": item_id_int in manual_ids,
            "is_precursor": is_precursor_step(display_entry),
            "category": item_category(display_entry),
            "data_confidence": confidence_from_resolution(
                {"source": component.get("source", "")}
            ),
            "paths": [f"{final_item_name} -> {component.get('name', 'Unnamed component')}"],
        }
        branch["status"] = component_status_label(branch)
        components.append(branch)

    return components


def build_ranked_breakdown_recommendations(
    target_status: dict[str, Any],
    terminal_missing_materials: list[dict[str, Any]],
    source_steps: list[dict[str, Any]],
    major_components: list[dict[str, Any]],
    price_estimates: dict[int, dict[str, Any]] | None,
) -> list[str]:
    """Rank next actions for a focused target breakdown."""

    recommendations: list[str] = []

    missing_major_precursors = [
        component for component in major_components if component["missing"] > 0 and component["is_precursor"]
    ]

    for component in missing_major_precursors[:1]:
        recommendations.append(
            f"Precursor step: focus {component['name']} first; it blocks final assembly."
        )

    all_missing_items = terminal_missing_materials + source_steps

    if missing_item_by_name(all_missing_items, "Mystic Clover"):
        recommendations.append(
            "Mystic Clover: do Wizard's Vault objectives, WvW reward tracks, and weekly vendor sources."
        )

    if missing_item_by_name(all_missing_items, "Mystic Runestone"):
        recommendations.append(
            "Mystic Runestone: check the vendor/source path and plan the purchase cost."
        )

    janthir_names = [
        item["name"]
        for item in terminal_missing_materials
        if any(
            word in normalize_item_name(item["name"])
            for word in (
                "janthir",
                "lowland",
                "mursaat",
                "honey flower",
                "titan",
                "homestead",
            )
        )
    ][:3]

    if janthir_names:
        recommendations.append(
            "Janthir materials: route map gathering/currency progress for "
            f"{', '.join(janthir_names)}."
        )

    if missing_item_by_name(all_missing_items, "Bloodstone Shard"):
        recommendations.append(
            "Bloodstone Shard: check Spirit Shards and Mystic Forge vendor access."
        )

    source_step_names = [
        step["name"]
        for step in source_steps
        if step.get("category") in {
            "account-bound source step",
            "achievement/collection step",
            "ambiguous/manual review",
        }
    ][:3]

    if source_step_names:
        recommendations.append(
            "Source-gated gifts: review acquisition paths for "
            f"{', '.join(source_step_names)}."
        )

    existing_recommendation_keys = {
        recommendation.split(":", 1)[0].casefold()
        for recommendation in recommendations
        if ":" in recommendation
    }

    for recommendation in build_gameplay_recommendations(target_status, price_estimates):
        recommendation_key = (
            recommendation.split(":", 1)[0].casefold()
            if ":" in recommendation
            else recommendation.casefold()
        )

        if recommendation not in recommendations and recommendation_key not in existing_recommendation_keys:
            recommendations.append(recommendation)
            existing_recommendation_keys.add(recommendation_key)

    return recommendations


def build_target_breakdown(
    target: dict[str, Any],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    price_estimates: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create a focused planning breakdown for one target."""

    target_status = build_target_status(
        target,
        wallet,
        item_counts,
        legendary_armory,
        item_names,
        currency_names,
    )
    recipe_tree = target.get(RECIPE_TREE_FIELD, {})
    major_branches = build_major_components(recipe_tree, item_counts)
    manual_steps = list(target_status["recipe_unknown_manual_steps"])
    manual_item_ids = {
        int(step["item_id"])
        for step in manual_steps
        if step.get("item_id") is not None
    }
    terminal_missing_materials = [
        {
            **item,
            "category": item_category(item),
            "data_confidence": item.get("data_confidence") or "official_api",
        }
        for item in target_status["missing_items"]
        if int(item["id"]) not in manual_item_ids
    ]
    missing_by_id = {int(item["id"]): item for item in target_status["missing_items"]}
    source_steps: list[dict[str, Any]] = []
    source_step_ids: set[int] = set()

    for step in manual_steps:
        item_id = int(step["item_id"]) if step.get("item_id") is not None else None
        missing_entry = missing_by_id.get(item_id) if item_id is not None else None
        needed = int(step.get("amount", 0))
        owned = item_counts.get(item_id, 0) if item_id is not None else 0
        missing = (
            missing_entry.get("missing", max(needed - owned, 0))
            if missing_entry
            else max(needed - owned, 0)
        )

        if missing <= 0:
            continue

        if item_id is not None:
            source_step_ids.add(item_id)

        source_steps.append(
            {
                **step,
                "needed": needed,
                "owned": missing_entry.get("owned", owned) if missing_entry else owned,
                "missing": missing,
                "category": item_category(step, manual_step=True),
                "data_confidence": confidence_from_resolution(
                    {
                        "source": "account_bound_manual_source_step",
                        "review_status": step.get("review_status", ""),
                    },
                    step,
                ),
            }
        )

    important_source_step_names = {
        "gift of research",
        "gift of the mists",
    }

    for step in recipe_tree.get("expanded_source_steps", []):
        item_id = int(step["item_id"]) if step.get("item_id") is not None else None
        normalized_step_name = normalize_item_name(str(step.get("name", "")))

        if normalized_step_name not in important_source_step_names:
            continue

        if item_id is not None and item_id in source_step_ids:
            continue

        needed = int(step.get("amount", 0))
        owned = item_counts.get(item_id, 0) if item_id is not None else 0
        missing = max(needed - owned, 0)

        if missing <= 0:
            continue

        if item_id is not None:
            source_step_ids.add(item_id)

        source_steps.append(
            {
                **step,
                "needed": needed,
                "owned": owned,
                "missing": missing,
                "reason": "Expanded source-step component; review its acquisition path.",
                "category": item_category(step, manual_step=True),
                "data_confidence": step.get("review_status") or "wiki_imported_unreviewed",
            }
        )

    recommendations = build_ranked_breakdown_recommendations(
        target_status,
        terminal_missing_materials,
        source_steps,
        major_branches,
        price_estimates,
    )
    recommendations.extend(build_configuration_recommendations(target_status)[:2])

    source_step_names = {normalize_item_name(str(step.get("name", ""))) for step in source_steps}

    if "gift of research" in source_step_names:
        recommendations.append(
            "Source step: Gift of Research needs research notes; review that branch before buying."
        )

    if "gift of the mists" in source_step_names:
        recommendations.append("Source step: Gift of the Mists is still needed; review that branch.")

    unique_recommendations: list[str] = []

    for recommendation in recommendations:
        if recommendation not in unique_recommendations:
            unique_recommendations.append(recommendation)

    final_item_id = target_final_item_id(target)
    owned_final = []

    if final_item_id is not None and is_target_unlocked(target, legendary_armory):
        owned_final.append(
            {
                "item_id": final_item_id,
                "name": item_names.get(
                    final_item_id,
                    target.get("final_item_name", target_status["name"]),
                ),
                "source": "Legendary Armory",
            }
        )

    return {
        "target": {
            "name": target_status["name"],
            "final_item_id": final_item_id,
            "final_item_name": target.get("final_item_name", target_status["name"]),
        },
        "status": target_status["status_label"],
        "owned_final_items": owned_final,
        "owned_intermediate_items": list(recipe_tree.get("satisfied_intermediates", [])),
        "major_branches": major_branches,
        "expanded_ingredients": list(recipe_tree.get("expanded_source_steps", [])),
        "terminal_missing_materials": terminal_missing_materials,
        "account_bound_manual_source_steps": source_steps,
        "warnings": list(target_status["recipe_engine_warnings"]),
        "recommendations": unique_recommendations,
    }


def format_breakdown_entry_amount(entry: dict[str, Any]) -> str:
    """Format have/need/missing counts for one breakdown line."""

    owned = int(entry.get("owned", 0))
    needed = int(entry.get("needed", entry.get("amount", 0)))
    missing = int(entry.get("missing", max(needed - owned, 0)))
    return f"have {owned:,}, need {needed:,}, missing {missing:,}"


def breakdown_entry_tag(entry: dict[str, Any]) -> str:
    """Return a compact category/confidence tag for one breakdown line."""

    category = str(entry.get("category", "")).strip()
    confidence = str(entry.get("data_confidence", "")).strip()
    bits = [bit for bit in (category, confidence) if bit]

    if not bits:
        return ""

    return f" [{'; '.join(bits)}]"


def add_breakdown_entry_detail_lines(
    lines: list[str],
    entry: dict[str, Any],
    show_paths: bool = False,
) -> None:
    """Add category, confidence, and path details for one breakdown entry."""

    paths = [format_recipe_path(path) for path in entry.get("paths", []) if str(path).strip()]

    if not paths:
        return

    if show_paths:
        lines.append("    Used in:")

        for path in paths:
            lines.append(f"      - {path}")
    elif important_path_entry(entry):
        lines.append(f"    Used in: {paths[0]}")


def format_target_breakdown(
    breakdown: dict[str, Any],
    show_paths: bool = False,
) -> str:
    """Turn a target breakdown dictionary into readable CLI text."""

    target = breakdown["target"]
    lines = [
        "Legendary Breakdown",
        "===================",
        f"Target: {target['name']}",
    ]

    if target.get("final_item_id"):
        lines.append(
            f"Final item: {target.get('final_item_name') or target['name']} "
            f"(item_id {target['final_item_id']})"
        )

    lines.extend(["", "Status", f"  - {breakdown['status']}"])
    lines.extend(["", "Already owned / satisfied"])

    owned_lines = []

    for item in breakdown["owned_final_items"]:
        owned_lines.append(f"  - {item['name']}: already unlocked via {item['source']}.")

    for item in breakdown["owned_intermediate_items"]:
        owned_lines.append(
            f"  - {item['name']}: have {int(item['amount']):,}, branch satisfied."
        )

    lines.extend(owned_lines or ["  - None found for this target yet."])
    lines.extend(["", "Major components"])

    if breakdown["major_branches"]:
        for component in breakdown["major_branches"]:
            lines.append(
                f"  - {component['name']}: {component['status']}{breakdown_entry_tag(component)} "
                f"({format_breakdown_entry_amount(component)})"
            )
            add_breakdown_entry_detail_lines(lines, component, show_paths=show_paths)
    else:
        lines.append("  - No major components were resolved yet.")

    expanded_by_id = {
        int(component["item_id"]): component
        for component in breakdown["expanded_ingredients"]
        if component.get("item_id") is not None
    }
    expanded_major_branches = [
        component
        for component in breakdown["major_branches"]
        if component["expanded"] and component.get("item_id") in expanded_by_id
    ]

    if expanded_major_branches:
        lines.extend(["", "Expanded components"])

        for branch in expanded_major_branches:
            expanded_component = expanded_by_id[int(branch["item_id"])]
            lines.append(
                f"  - {branch['name']}: expanded into "
                f"{int(expanded_component.get('ingredient_count', 0)):,} child ingredients."
            )

        nested_count = max(len(breakdown["expanded_ingredients"]) - len(expanded_major_branches), 0)

        if nested_count:
            lines.append(f"  - {nested_count:,} nested components also expanded.")

    lines.extend(["", "Missing materials"])

    if breakdown["terminal_missing_materials"]:
        for item in breakdown["terminal_missing_materials"]:
            lines.append(
                f"  - {item['name']}{breakdown_entry_tag(item)}: "
                f"{format_breakdown_entry_amount(item)}"
            )
            add_breakdown_entry_detail_lines(lines, item, show_paths=show_paths)
    else:
        lines.append("  - No terminal materials missing after owned items were counted.")

    lines.extend(["", "Manual/source steps"])

    if breakdown["account_bound_manual_source_steps"]:
        for step in breakdown["account_bound_manual_source_steps"]:
            lines.append(
                f"  - {step['name']}{breakdown_entry_tag(step)}: "
                f"{format_breakdown_entry_amount(step)}"
            )
            add_breakdown_entry_detail_lines(lines, step, show_paths=show_paths)
            source_summary = str(
                step.get("source_step_summary") or step.get("source_summary") or ""
            ).strip()

            if source_summary:
                lines.append(f"    Summary: {source_summary}")

            if step.get("source_url"):
                lines.append(f"    Source URL: {step['source_url']}")
    else:
        lines.append("  - No manual/source steps currently blocking this target.")

    lines.extend(["", "Recommended next actions"])

    if breakdown["recommendations"]:
        for recommendation in breakdown["recommendations"][:6]:
            lines.append(f"  - {recommendation}")
    else:
        lines.append(
            "  - No specific recommendation matched yet; work the first missing major component."
        )

    lines.extend(["", "Warnings / needs review"])

    if breakdown["warnings"]:
        for warning in breakdown["warnings"][:8]:
            lines.append(f"  - {warning}")
    else:
        lines.append("  - No recipe warnings for this breakdown.")

    return "\n".join(lines).rstrip()


def select_breakdown_target(
    targets: list[dict[str, Any]],
    target_name: str | None = None,
    use_priority: bool = False,
) -> dict[str, Any]:
    """Choose the target for a focused breakdown command."""

    enabled_targets = [target for target in targets if target.get("enabled", True)]

    if use_priority:
        if not enabled_targets:
            raise ValueError("No enabled targets were found in legendary_goals.json.")

        return enabled_targets[0]

    if not target_name:
        raise ValueError("Pass --breakdown \"Target Name\" or use --breakdown-priority.")

    normalized_requested_name = normalize_item_name(target_name)
    matches = [
        target
        for target in enabled_targets
        if normalize_item_name(str(target.get("name", ""))) == normalized_requested_name
        or normalize_item_name(str(target.get("final_item_name", "")))
        == normalized_requested_name
    ]

    if len(matches) == 1:
        return matches[0]

    if not matches:
        available_names = ", ".join(
            str(target.get("name", "Unnamed target")) for target in enabled_targets
        )
        raise ValueError(
            f"No enabled target named '{target_name}' was found. "
            f"Enabled targets: {available_names or 'none'}."
        )

    raise ValueError(f"More than one enabled target matched '{target_name}'.")


def build_breakdown_report(
    targets: list[dict[str, Any]],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    target_name: str | None = None,
    use_priority: bool = False,
    show_paths: bool = False,
) -> str:
    """Build the focused CLI Legendary Breakdown v1 output."""

    target = select_breakdown_target(
        targets,
        target_name=target_name,
        use_priority=use_priority,
    )
    breakdown = build_target_breakdown(
        target,
        wallet,
        item_counts,
        legendary_armory,
        item_names,
        currency_names,
        price_estimates=None,
    )
    return format_target_breakdown(breakdown, show_paths=show_paths)


def breakdown_entries_for_explain(breakdown: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Return all explainable missing entries with their section names."""

    entries: list[tuple[str, dict[str, Any]]] = []

    for entry in breakdown.get("major_branches", []):
        if int(entry.get("missing", 0)) > 0:
            entries.append(("Major component", entry))

    for entry in breakdown.get("terminal_missing_materials", []):
        entries.append(("Missing material", entry))

    for entry in breakdown.get("account_bound_manual_source_steps", []):
        entries.append(("Manual/source step", entry))

    return entries


def format_explain_missing_report(
    breakdown: dict[str, Any],
    item_name: str,
) -> str:
    """Explain why one missing item appears in a target breakdown."""

    normalized_requested_name = normalize_item_name(item_name)
    matches = [
        (section, entry)
        for section, entry in breakdown_entries_for_explain(breakdown)
        if normalize_item_name(str(entry.get("name", ""))) == normalized_requested_name
    ]
    target_name = breakdown["target"]["name"]

    lines = [
        "Missing Requirement Explanation",
        "===============================",
        f"Target: {target_name}",
        f"Requested item: {item_name}",
    ]

    if not matches:
        lines.append("")
        lines.append("No missing entry with that exact name was found in this breakdown.")
        return "\n".join(lines).rstrip()

    for section, entry in matches:
        lines.extend(
            [
                "",
                f"Section: {section}",
                f"Item: {entry['name']}",
                f"Amount: {format_breakdown_entry_amount(entry)}",
                f"Category: {entry.get('category', 'unknown')}",
                f"Data confidence: {entry.get('data_confidence', 'unknown')}",
            ]
        )

        paths = [
            format_recipe_path(path)
            for path in entry.get("paths", [])
            if str(path).strip()
        ]

        if paths:
            lines.append("Why it is needed:")

            for path in paths:
                lines.append(f"  - {path}")
        else:
            lines.append("Why it is needed: no path was recorded for this entry yet.")

        source_summary = str(
            entry.get("source_step_summary") or entry.get("source_summary") or ""
        ).strip()

        if source_summary:
            lines.append(f"Source summary: {source_summary}")

        if entry.get("source_url"):
            lines.append(f"Source URL: {entry['source_url']}")

        if entry.get("reason"):
            lines.append(f"Reason: {entry['reason']}")

    return "\n".join(lines).rstrip()


def build_explain_missing_report(
    targets: list[dict[str, Any]],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    target_name: str,
    item_name: str,
) -> str:
    """Build the --explain-missing report for one target and item."""

    target = select_breakdown_target(targets, target_name=target_name)
    breakdown = build_target_breakdown(
        target,
        wallet,
        item_counts,
        legendary_armory,
        item_names,
        currency_names,
        price_estimates=None,
    )
    return format_explain_missing_report(breakdown, item_name)


def acquisition_option(
    label: str,
    source_type: str,
    *,
    source_url: str = "",
    costs: list[dict[str, Any]] | None = None,
    currencies: list[str] | None = None,
    time_gate: str = "",
    confidence: str = "manual_review_needed",
    review_status: str = "manual_review_needed",
    notes: str = "",
) -> dict[str, Any]:
    """Create one structured acquisition option for future CLI/GUI use."""

    return {
        "label": label,
        "source_type": source_type,
        "source_url": source_url,
        "costs": costs or [],
        "currencies": currencies or [],
        "time_gate": time_gate,
        "confidence": confidence,
        "review_status": review_status,
        "notes": notes,
    }


def acquisition_options_for_entry(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """Return practical acquisition options for one missing requirement."""

    name = str(entry.get("name", ""))
    normalized_name = normalize_item_name(name)
    category = str(entry.get("category", ""))
    source_url = str(entry.get("source_url", ""))
    confidence = str(entry.get("data_confidence", "manual_review_needed"))

    if normalized_name == "mystic clover":
        return [
            acquisition_option(
                "Wizard's Vault objectives and rewards",
                "currency_purchase",
                source_url=source_url,
                time_gate="daily/weekly",
                confidence=confidence,
                notes="Use Astral Acclaim rewards when Mystic Clovers are available.",
            ),
            acquisition_option(
                "WvW reward tracks",
                "weekly",
                source_url=source_url,
                time_gate="repeatable",
                confidence=confidence,
                notes="Good low-planning source while also progressing Gift of Battle style goals.",
            ),
            acquisition_option(
                "Weekly or limited vendors",
                "weekly",
                source_url=source_url,
                time_gate="weekly",
                confidence=confidence,
                notes="Check current weekly vendor availability before committing currencies.",
            ),
        ]

    if normalized_name == "mystic runestone":
        return [
            acquisition_option(
                "Vendor/source purchase",
                "vendor",
                source_url=source_url,
                confidence="manual_review_needed",
                notes="Planner knows this is vendor/source-like, but exact cost is not verified here.",
            )
        ]

    if normalized_name in {"bloodstone shard", "eldritch scroll"}:
        return [
            acquisition_option(
                "Mystic Forge vendor",
                "vendor",
                source_url=source_url,
                currencies=["Spirit Shard"],
                confidence="manual_review_needed",
                notes="Vendor-like Spirit Shard item; verify exact cost before spending.",
            )
        ]

    if normalized_name == "hydrocatalytic reagent":
        return [
            acquisition_option(
                "Vendor/source purchase",
                "vendor",
                source_url=source_url,
                confidence="manual_review_needed",
                notes="Cost/source should be verified before relying on this quantity.",
            )
        ]

    janthir_words = (
        "janthir",
        "lowland",
        "mursaat",
        "honey flower",
        "titan",
        "homestead",
        "charged titan ore",
        "rotted titan amber",
    )

    if any(word in normalized_name for word in janthir_words):
        return [
            acquisition_option(
                "Janthir map farming or map currency route",
                "farm",
                source_url=source_url,
                confidence=confidence,
                notes="Group this with other Janthir materials to reduce travel and context switching.",
            )
        ]

    if category == "precursor":
        return [
            acquisition_option(
                "Precursor acquisition",
                "manual_review",
                source_url=source_url,
                confidence=confidence,
                notes="Choose the precursor path before investing heavily in final assembly.",
            )
        ]

    if category == "normal material":
        return [
            acquisition_option(
                "Gather, craft, or buy",
                "farm",
                source_url=source_url,
                confidence=confidence or "official_api",
                notes="Use account stock first, then decide between gathering/crafting and Trading Post.",
            ),
            acquisition_option(
                "Trading Post buy",
                "trading_post",
                source_url=source_url,
                confidence="manual_review_needed",
                notes="Price lookup is separate from this action-plan view.",
            ),
        ]

    if category == "craftable component":
        return [
            acquisition_option(
                "Craft from expanded child ingredients",
                "craft",
                source_url=source_url,
                confidence=confidence,
                notes="Follow the expanded component branch and stop if you already own the component.",
            )
        ]

    if category == "achievement/collection step":
        return [
            acquisition_option(
                "Achievement or collection",
                "achievement",
                source_url=source_url,
                confidence="manual_review_needed",
                notes="Planner should not guess achievement or collection details.",
            )
        ]

    return [
        acquisition_option(
            "Manual source review",
            "manual_review",
            source_url=source_url,
            confidence=confidence or "manual_review_needed",
            review_status=str(entry.get("review_status", "manual_review_needed")),
            notes="Review the source page or add a verified override before trusting this path.",
        )
    ]


def entry_with_acquisition_options(entry: dict[str, Any]) -> dict[str, Any]:
    """Copy one missing entry and attach structured acquisition options."""

    return {
        **entry,
        "acquisition_options": acquisition_options_for_entry(entry),
    }


def action_bucket_name(entry: dict[str, Any]) -> str:
    """Choose the main action-plan bucket for one missing entry."""

    normalized_name = normalize_item_name(str(entry.get("name", "")))
    category = str(entry.get("category", ""))
    confidence = str(entry.get("data_confidence", ""))

    if category == "precursor":
        return "priority_blockers"

    if "ambiguous" in confidence or "manual_review" in confidence and category == "ambiguous/manual review":
        return "manual_review"

    if normalized_name in {"mystic clover", "gift of battle"}:
        return "do_today"

    if category in {"account-bound source step", "achievement/collection step"}:
        if normalized_name in {"gift of research", "gift of the mists"}:
            return "priority_blockers"

        return "manual_review"

    if category == "vendor item":
        return "buy_vendor"

    if category == "craftable component":
        return "crafting"

    janthir_words = (
        "janthir",
        "lowland",
        "mursaat",
        "honey flower",
        "titan",
        "homestead",
        "charged titan ore",
        "rotted titan amber",
    )

    if any(word in normalized_name for word in janthir_words):
        return "farm_gather"

    if category == "normal material":
        return "farm_gather"

    return "manual_review"


ACTION_BUCKET_TITLES = {
    "priority_blockers": "Priority blockers",
    "do_today": "Do today / do soon",
    "buy_vendor": "Buy/vendor list",
    "farm_gather": "Farm/gather list",
    "crafting": "Crafting list",
    "manual_review": "Manual review needed",
}


CONFIDENCE_STRENGTH = {
    "manual_override_verified": 4,
    "official_api": 3,
    "wiki_imported_unreviewed": 2,
    "manual_review_needed": 1,
    "ambiguous": 0,
}


def confidence_rank(confidence: str) -> int:
    """Return a simple trust rank for choosing the best merged entry."""

    return CONFIDENCE_STRENGTH.get(normalize_item_name(confidence), -1)


def action_entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    """Use item id when available, then fall back to normalized item name."""

    item_id = entry.get("id", entry.get("item_id"))

    if item_id is not None:
        return ("id", str(item_id))

    return ("name", normalize_item_name(str(entry.get("name", ""))))


def unique_nonempty_text(values: list[Any]) -> list[str]:
    """Keep useful text values in original order without duplicates."""

    seen: set[str] = set()
    unique_values: list[str] = []

    for value in values:
        text = str(value).strip()

        if not text or text in seen:
            continue

        seen.add(text)
        unique_values.append(text)

    return unique_values


def merge_action_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge duplicate action entries while preserving paths and source details."""

    merged_by_key: dict[tuple[str, str], dict[str, Any]] = {}

    for entry in entries:
        key = action_entry_key(entry)
        current = merged_by_key.get(key)

        if current is None:
            merged_by_key[key] = dict(entry)
            merged_by_key[key]["paths"] = unique_nonempty_text(list(entry.get("paths", [])))
            merged_by_key[key]["secondary_sources"] = []
            continue

        current_confidence = str(current.get("data_confidence", ""))
        new_confidence = str(entry.get("data_confidence", ""))

        if confidence_rank(new_confidence) > confidence_rank(current_confidence):
            for field in (
                "category",
                "data_confidence",
                "resolution_source",
                "source_url",
                "review_status",
                "source_type",
                "source_step_summary",
            ):
                if entry.get(field):
                    current[field] = entry[field]

        current["owned"] = max(int(current.get("owned", 0)), int(entry.get("owned", 0)))
        current["needed"] = max(
            int(current.get("needed", current.get("amount", 0))),
            int(entry.get("needed", entry.get("amount", 0))),
        )
        current["amount"] = max(
            int(current.get("amount", current.get("needed", 0))),
            int(entry.get("amount", entry.get("needed", 0))),
        )
        current["missing"] = max(
            int(current.get("missing", 0)),
            int(entry.get("missing", 0)),
        )
        current["paths"] = unique_nonempty_text(
            list(current.get("paths", [])) + list(entry.get("paths", []))
        )

        secondary_sources = list(current.get("secondary_sources", []))
        secondary_sources.append(
            {
                "category": entry.get("category", ""),
                "data_confidence": entry.get("data_confidence", ""),
                "resolution_source": entry.get("resolution_source", ""),
                "source_url": entry.get("source_url", ""),
            }
        )
        current["secondary_sources"] = [
            source
            for index, source in enumerate(secondary_sources)
            if source not in secondary_sources[:index]
        ]

    return list(merged_by_key.values())


def sort_action_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Put the most useful action-plan entries first."""

    priority_names = {
        "nyr hrammr": 0,
        "mystic clover": 1,
        "mystic runestone": 2,
        "bloodstone shard": 3,
        "eldritch scroll": 4,
        "gift of research": 5,
        "gift of the mists": 6,
    }

    def sort_key(entry: dict[str, Any]) -> tuple[int, int, str]:
        normalized_name = normalize_item_name(str(entry.get("name", "")))
        priority = priority_names.get(normalized_name, 50)
        missing = int(entry.get("missing", 0))
        return (priority, -missing, normalized_name)

    return sorted(entries, key=sort_key)


def manual_review_group_name(entry: dict[str, Any]) -> str:
    """Group noisy manual-review items into player-friendly buckets."""

    normalized_name = normalize_item_name(str(entry.get("name", "")))
    name = str(entry.get("name", ""))
    category = str(entry.get("category", ""))

    trophy_words = ("blood", "bones", "claws", "dust", "fangs", "scales", "totems", "venom")

    if normalized_name.startswith("gift of ") and any(word in normalized_name for word in trophy_words):
        return "Trophy gifts"

    if any(word in normalized_name for word in ("janthir", "lowland", "ursus", "syntri")):
        return "Janthir gifts"

    if any(word in normalized_name for word in ("mists", "glory", "war", "battle")):
        return "Mists/PvP/WvW gifts"

    if normalized_name in {"bloodstone shard", "eldritch scroll", "mystic runestone"}:
        return "Vendor/source items"

    if "research" in normalized_name or "hydrocatalytic" in normalized_name:
        return "Research/currency items"

    if category == "achievement/collection step":
        return "Achievement/collection steps"

    if name.startswith("Gift of "):
        return "Other gifts"

    return "Other manual review"


def summarize_entry_names(entries: list[dict[str, Any]], limit: int = 6) -> str:
    """Return a compact comma-separated name summary."""

    names = [str(entry.get("name", "Unnamed item")) for entry in entries]

    if len(names) <= limit:
        return ", ".join(names)

    return f"{', '.join(names[:limit])}, and {len(names) - limit} more"


def build_action_plan_from_breakdown(breakdown: dict[str, Any]) -> dict[str, Any]:
    """Convert a target breakdown into strategy buckets."""

    buckets = {bucket_name: [] for bucket_name in ACTION_BUCKET_TITLES}

    entries: list[dict[str, Any]] = []

    for component in breakdown.get("major_branches", []):
        if int(component.get("missing", 0)) > 0 and component.get("category") == "precursor":
            entries.append(component)

    entries.extend(breakdown.get("terminal_missing_materials", []))
    entries.extend(breakdown.get("account_bound_manual_source_steps", []))

    for entry in merge_action_entries(entries):
        entry_with_options = entry_with_acquisition_options(entry)
        buckets[action_bucket_name(entry_with_options)].append(entry_with_options)

    for bucket_name, bucket_entries in buckets.items():
        buckets[bucket_name] = sort_action_entries(bucket_entries)

    warnings = list(breakdown.get("warnings", []))
    suspicious_warnings = [
        warning
        for warning in warnings
        if "duplicate ingredient" in warning.casefold()
        or "manual review" in warning.casefold()
        or "ambiguous" in warning.casefold()
    ]

    return {
        "target": breakdown["target"],
        "status": breakdown["status"],
        "buckets": buckets,
        "warnings": warnings,
        "suspicious_warnings": suspicious_warnings,
        "recommendations": list(breakdown.get("recommendations", [])),
    }


def format_action_plan_entry(
    entry: dict[str, Any],
    show_paths: bool = False,
    detailed: bool = True,
) -> list[str]:
    """Format one action-plan entry."""

    lines = [
        f"  - {entry['name']}: {format_breakdown_entry_amount(entry)} "
        f"[{entry.get('category', 'unknown')}; {entry.get('data_confidence', 'unknown')}]"
    ]

    options = list(entry.get("acquisition_options", []))

    if options:
        option_labels = ", ".join(option["label"] for option in options[:3])
        lines.append(f"    Options: {option_labels}")

        option_details = options[:2] if detailed else []

        for option in option_details:
            details = []

            if option.get("time_gate"):
                details.append(f"time gate: {option['time_gate']}")

            if option.get("currencies"):
                details.append(f"currencies: {', '.join(option['currencies'])}")

            if option.get("costs"):
                cost_text = ", ".join(str(cost) for cost in option["costs"])
                details.append(f"costs: {cost_text}")

            if option.get("confidence"):
                details.append(f"confidence: {option['confidence']}")

            if option.get("notes"):
                details.append(option["notes"])

            if details:
                lines.append(f"    - {option['label']}: {'; '.join(details)}")

    paths = [format_recipe_path(path) for path in entry.get("paths", []) if str(path).strip()]

    if show_paths and paths:
        lines.append("    Used in:")

        for path in paths:
            lines.append(f"      - {path}")
    elif paths and important_path_entry(entry):
        lines.append(f"    Used in: {paths[0]}")

    secondary_sources = list(entry.get("secondary_sources", []))

    if detailed and secondary_sources:
        lines.append("    Secondary sources:")

        for source in secondary_sources[:4]:
            source_bits = [
                str(source.get("category", "")).strip(),
                str(source.get("data_confidence", "")).strip(),
                str(source.get("resolution_source", "")).strip(),
            ]
            source_text = "; ".join(bit for bit in source_bits if bit)

            if source_text:
                lines.append(f"      - {source_text}")

    return lines


def format_group_summary(
    title: str,
    entries: list[dict[str, Any]],
    limit: int = 5,
) -> list[str]:
    """Format a concise grouped summary section."""

    lines = [title]

    if not entries:
        lines.append("  - None right now.")
        return lines

    shown_entries = entries[:limit]

    for entry in shown_entries:
        lines.extend(format_action_plan_entry(entry, detailed=False))

    if len(entries) > limit:
        lines.append(f"  - {len(entries) - limit} more hidden; use --detailed to see all.")

    return lines


def format_buy_vendor_summary(entries: list[dict[str, Any]]) -> list[str]:
    """Format vendor-like items as one compact shopping/source list."""

    lines = ["Buy/vendor summary"]

    if not entries:
        lines.append("  - No vendor-like items found yet.")
        return lines

    lines.append(f"  - {summarize_entry_names(entries)}")

    spirit_shard_entries = [
        entry
        for entry in entries
        if normalize_item_name(str(entry.get("name", ""))) in {"bloodstone shard", "eldritch scroll"}
    ]

    if spirit_shard_entries:
        lines.append("    Spirit Shard vendor-like items are marked unverified until costs are reviewed.")

    runestone_entries = [
        entry
        for entry in entries
        if normalize_item_name(str(entry.get("name", ""))) == "mystic runestone"
    ]

    if runestone_entries:
        lines.append("    Mystic Runestone: check the vendor/source plan before treating it as a generic material.")

    return lines


def format_farm_gather_summary(entries: list[dict[str, Any]]) -> list[str]:
    """Format farm/gather work without dumping every material line."""

    lines = ["Farm/gather summary"]

    if not entries:
        lines.append("  - No farm/gather group found yet.")
        return lines

    janthir_entries = [
        entry
        for entry in entries
        if any(
            word in normalize_item_name(str(entry.get("name", "")))
            for word in ("janthir", "lowland", "mursaat", "honey flower", "titan", "rotted titan amber")
        )
    ]
    other_entries = [entry for entry in entries if entry not in janthir_entries]

    if janthir_entries:
        lines.append(f"  - Janthir materials: {summarize_entry_names(janthir_entries)}")

    if other_entries:
        lines.append(f"  - Other materials: {summarize_entry_names(other_entries)}")

    return lines


def format_manual_review_summary(entries: list[dict[str, Any]]) -> list[str]:
    """Collapse manual-review noise into grouped counts for the default action plan."""

    lines = ["Manual review summary"]

    if not entries:
        lines.append("  - No manual review items found yet.")
        return lines

    grouped_entries: dict[str, list[dict[str, Any]]] = {}

    for entry in entries:
        grouped_entries.setdefault(manual_review_group_name(entry), []).append(entry)

    total_count = sum(len(group) for group in grouped_entries.values())
    lines.append(f"  - {total_count} item(s) need review. Use --detailed to see every entry.")

    for group_name in sorted(grouped_entries):
        group_entries = sort_action_entries(grouped_entries[group_name])
        lines.append(f"  - {group_name}: {summarize_entry_names(group_entries)}")

    return lines


def format_action_plan_detailed(
    action_plan: dict[str, Any],
    show_paths: bool = False,
) -> str:
    """Turn a full action plan into readable CLI output."""

    target = action_plan["target"]
    lines = [
        "Legendary Action Plan",
        "=====================",
        f"Target: {target['name']}",
        f"Status: {action_plan['status']}",
        "",
    ]

    for bucket_name, title in ACTION_BUCKET_TITLES.items():
        entries = list(action_plan["buckets"].get(bucket_name, []))
        lines.append(title)

        if entries:
            for entry in entries:
                lines.extend(
                    format_action_plan_entry(
                        entry,
                        show_paths=show_paths,
                        detailed=True,
                    )
                )
        else:
            lines.append("  - None right now.")

        lines.append("")

    if action_plan["suspicious_warnings"]:
        lines.append("Trust / review flags")

        for warning in action_plan["suspicious_warnings"][:8]:
            lines.append(f"  - {warning}")

        lines.append("")

    lines.append("Recommended next actions")

    if action_plan["recommendations"]:
        for recommendation in action_plan["recommendations"][:6]:
            lines.append(f"  - {recommendation}")
    else:
        lines.append("  - No ranked action found yet; start with priority blockers.")

    return "\n".join(lines).rstrip()


def format_action_plan_summary(action_plan: dict[str, Any]) -> str:
    """Turn an action plan into a concise player-facing next-actions screen."""

    target = action_plan["target"]
    buckets = action_plan["buckets"]
    lines = [
        "Legendary Action Plan",
        "=====================",
        f"Target: {target['name']}",
        f"Status: {action_plan['status']}",
        "",
    ]

    lines.extend(format_group_summary("Priority blockers", buckets.get("priority_blockers", []), limit=5))
    lines.append("")
    lines.extend(format_group_summary("Do today / do soon", buckets.get("do_today", []), limit=5))
    lines.append("")
    lines.extend(format_buy_vendor_summary(buckets.get("buy_vendor", [])))
    lines.append("")
    lines.extend(format_farm_gather_summary(buckets.get("farm_gather", [])))
    lines.append("")
    lines.extend(format_manual_review_summary(buckets.get("manual_review", [])))
    lines.append("")

    if action_plan["suspicious_warnings"]:
        warning_count = len(action_plan["suspicious_warnings"])
        lines.append("Trust / review flags")
        lines.append(f"  - {warning_count} warning(s). Use --detailed to review them.")
        lines.append("")

    lines.append("Recommended next actions")

    if action_plan["recommendations"]:
        for recommendation in action_plan["recommendations"][:5]:
            lines.append(f"  - {recommendation}")
    else:
        lines.append("  - Start with the priority blockers above.")

    return "\n".join(lines).rstrip()


def format_action_plan(
    action_plan: dict[str, Any],
    show_paths: bool = False,
    detailed: bool = False,
) -> str:
    """Turn an action plan into either summary or detailed CLI output."""

    if detailed or show_paths:
        return format_action_plan_detailed(action_plan, show_paths=show_paths)

    return format_action_plan_summary(action_plan)


def build_action_plan_report(
    targets: list[dict[str, Any]],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    target_name: str | None = None,
    use_priority: bool = False,
    show_paths: bool = False,
    detailed: bool = False,
) -> str:
    """Build the focused CLI Action Plan v1 output."""

    target = select_breakdown_target(
        targets,
        target_name=target_name,
        use_priority=use_priority,
    )
    breakdown = build_target_breakdown(
        target,
        wallet,
        item_counts,
        legendary_armory,
        item_names,
        currency_names,
        price_estimates=None,
    )
    return format_action_plan(
        build_action_plan_from_breakdown(breakdown),
        show_paths=show_paths,
        detailed=detailed,
    )


def format_acquisition_options_report(
    breakdown: dict[str, Any],
    item_name: str,
) -> str:
    """Show acquisition options for one missing entry."""

    normalized_requested_name = normalize_item_name(item_name)
    matches = [
        entry
        for _section, entry in breakdown_entries_for_explain(breakdown)
        if normalize_item_name(str(entry.get("name", ""))) == normalized_requested_name
    ]
    lines = [
        "Acquisition Options",
        "===================",
        f"Target: {breakdown['target']['name']}",
        f"Requested item: {item_name}",
    ]

    if not matches:
        lines.append("")
        lines.append("No missing entry with that exact name was found in this breakdown.")
        return "\n".join(lines).rstrip()

    for entry in matches:
        entry_with_options = entry_with_acquisition_options(entry)
        lines.extend(
            [
                "",
                f"Item: {entry_with_options['name']}",
                f"Amount: {format_breakdown_entry_amount(entry_with_options)}",
                f"Category: {entry_with_options.get('category', 'unknown')}",
                f"Confidence: {entry_with_options.get('data_confidence', 'unknown')}",
            ]
        )

        for option in entry_with_options["acquisition_options"]:
            lines.append(f"  - {option['label']} [{option['source_type']}]")

            if option.get("time_gate"):
                lines.append(f"    Time gate: {option['time_gate']}")

            if option.get("currencies"):
                lines.append(f"    Currencies: {', '.join(option['currencies'])}")

            if option.get("costs"):
                lines.append(f"    Costs: {option['costs']}")
            elif option["source_type"] in {"vendor", "currency_purchase"}:
                lines.append("    Costs: unknown or unverified")

            if option.get("source_url"):
                lines.append(f"    Source URL: {option['source_url']}")

            if option.get("notes"):
                lines.append(f"    Notes: {option['notes']}")

            lines.append(f"    Confidence: {option.get('confidence', 'manual_review_needed')}")

    return "\n".join(lines).rstrip()


def build_show_options_report(
    targets: list[dict[str, Any]],
    wallet: dict[int, int],
    item_counts: dict[int, int],
    legendary_armory: dict[int, int],
    item_names: dict[int, str],
    currency_names: dict[int, str],
    target_name: str,
    item_name: str,
) -> str:
    """Build the --show-options report."""

    target = select_breakdown_target(targets, target_name=target_name)
    breakdown = build_target_breakdown(
        target,
        wallet,
        item_counts,
        legendary_armory,
        item_names,
        currency_names,
        price_estimates=None,
    )
    return format_acquisition_options_report(breakdown, item_name)


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
    debug = output_mode == OUTPUT_MODE_DEBUG
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

        recipe_engine_lines = make_recipe_engine_lines(
            target,
            detailed=detailed,
            debug=debug,
        )

        if recipe_engine_lines:
            lines.append("  Recipe engine:")
            lines.extend(recipe_engine_lines)

        if target_status["is_unlocked"]:
            final_item_name = item_names.get(final_item_id, f"Item {final_item_id}")
            lines.append(f"  Already unlocked in Legendary Armory: {final_item_name}")
            step_lines = make_step_lines(
                target_name,
                target_status["steps"],
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
            target_status["steps"],
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
        "--breakdown",
        default=None,
        help="Show a focused legendary breakdown for one enabled target name.",
    )
    parser.add_argument(
        "--breakdown-priority",
        action="store_true",
        help="Show a focused breakdown for the first enabled target in priority order.",
    )
    parser.add_argument(
        "--action-plan",
        default=None,
        help="Show a practical acquisition action plan for one enabled target name.",
    )
    parser.add_argument(
        "--action-plan-priority",
        action="store_true",
        help="Show an action plan for the first enabled target in priority order.",
    )
    parser.add_argument(
        "--show-options",
        nargs=2,
        metavar=("TARGET", "ITEM"),
        help="Show acquisition options for one missing item in one target.",
    )
    parser.add_argument(
        "--show-paths",
        action="store_true",
        help="Show full recipe paths in breakdown output.",
    )
    parser.add_argument(
        "--explain-missing",
        nargs=2,
        metavar=("TARGET", "ITEM"),
        help="Explain why one missing item is needed for one target.",
    )
    parser.add_argument(
        "--validate-goals",
        action="store_true",
        help="Validate goal/template configuration without scanning the account.",
    )
    parser.add_argument(
        "--generate-missing-overrides",
        action="store_true",
        help="Create recipe override stubs for resolved targets missing official recipes.",
    )
    parser.add_argument(
        "--setup-reference-db",
        action="store_true",
        help="Build the local public reference database from official API data.",
    )
    parser.add_argument(
        "--update-reference-db",
        action="store_true",
        help="Refresh the local public reference database from official API data.",
    )
    parser.add_argument(
        "--update-source-steps",
        action="store_true",
        help="Import targeted GW2 Wiki source-step pages for manual recipe items.",
    )
    parser.add_argument(
        "--reference-db-status",
        action="store_true",
        help="Show local public reference database status.",
    )
    parser.add_argument(
        "--debug-recipe-source",
        default=None,
        help="Explain which recipe source the engine would choose for one item name.",
    )
    parser.add_argument(
        "--debug-source-step",
        default=None,
        help="Show imported source-step data for one account-bound/manual item.",
    )
    parser.add_argument(
        "--debug-wiki-name",
        default=None,
        help="Show how one wiki item-name snippet is normalized and resolved.",
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
        "--summary",
        action="store_true",
        help="Use concise summary output. This is the default for action plans.",
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
        "--raw-materials",
        action="store_true",
        help=(
            "Fully expand recipe trees and ignore owned intermediate items. "
            "Default mode uses owned intermediates first."
        ),
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
        if args.validate_goals:
            validation_report = build_validate_goals_report(
                Path(args.config),
                rebuild_item_index=args.rebuild_item_index,
            )
            print(validation_report)
            return 0

        if args.generate_missing_overrides:
            override_report = build_generate_missing_overrides_report(
                Path(args.config),
                rebuild_item_index=args.rebuild_item_index,
            )
            print(override_report)
            return 0

        if args.setup_reference_db:
            print(
                build_reference_db_setup_report(
                    Path(args.config),
                    update=False,
                    rebuild_item_index=args.rebuild_item_index,
                )
            )
            return 0

        if args.update_reference_db:
            print(
                build_reference_db_setup_report(
                    Path(args.config),
                    update=True,
                    rebuild_item_index=args.rebuild_item_index,
                )
            )
            return 0

        if args.update_source_steps:
            print(
                build_update_source_steps_report(
                    Path(args.config),
                    rebuild_item_index=args.rebuild_item_index,
                )
            )
            return 0

        if args.reference_db_status:
            print(build_reference_db_status_report())
            return 0

        if args.debug_recipe_source:
            print(
                build_debug_recipe_source_report(
                    args.debug_recipe_source,
                    rebuild_item_index=args.rebuild_item_index,
                )
            )
            return 0

        if args.debug_source_step:
            print(
                build_debug_source_step_report(
                    args.debug_source_step,
                    rebuild_item_index=args.rebuild_item_index,
                )
            )
            return 0

        if args.debug_wiki_name:
            print(build_debug_wiki_name_report(args.debug_wiki_name))
            return 0

        if not DEFAULT_REFERENCE_DB_PATH.exists():
            print(
                f"Warning: local reference database not found at {DEFAULT_REFERENCE_DB_PATH}."
            )
            print(
                "Normal runs will use legacy public API caches for now. "
                "Run python gw2_legendary_planner.py --setup-reference-db to build "
                "the local reference database."
            )
            print()

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
        targets = resolve_target_final_item_names(
            targets,
            DEFAULT_ITEM_CACHE_PATH,
            DEFAULT_ITEM_NAME_INDEX_PATH,
            rebuild_item_index=args.rebuild_item_index,
        )
        recipe_engine = RecipeEngine()

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

        targets = resolve_target_recipe_data(
            targets,
            recipe_engine,
            item_counts=item_counts,
            use_owned_intermediates=not args.raw_materials,
        )

        item_ids, currency_ids = collect_goal_ids(targets)
        item_names = fetch_item_names_from_reference(item_ids) if item_ids else {}

        if item_ids - set(item_names):
            missing_name_ids = item_ids - set(item_names)
            fetched_item_names = fetch_names("/items", missing_name_ids)
            item_names.update(fetched_item_names)

        currency_names = fetch_names("/currencies", currency_ids) if currency_ids else {}

        if args.explain_missing:
            target_name, missing_item_name = args.explain_missing
            report = build_explain_missing_report(
                targets,
                wallet,
                item_counts,
                legendary_armory,
                item_names,
                currency_names,
                target_name,
                missing_item_name,
            )
            print(report)

            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report + "\n", encoding="utf-8")
                print()
                print(f"Report saved to {output_path}")

            return 0

        if args.show_options:
            target_name, missing_item_name = args.show_options
            report = build_show_options_report(
                targets,
                wallet,
                item_counts,
                legendary_armory,
                item_names,
                currency_names,
                target_name,
                missing_item_name,
            )
            print(report)

            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report + "\n", encoding="utf-8")
                print()
                print(f"Report saved to {output_path}")

            return 0

        if args.action_plan or args.action_plan_priority:
            action_plan_detailed = (
                output_mode in {OUTPUT_MODE_DETAILED, OUTPUT_MODE_DEBUG} or args.show_paths
            ) and not args.summary
            report = build_action_plan_report(
                targets,
                wallet,
                item_counts,
                legendary_armory,
                item_names,
                currency_names,
                target_name=args.action_plan,
                use_priority=args.action_plan_priority,
                show_paths=args.show_paths,
                detailed=action_plan_detailed,
            )
            print(report)

            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report + "\n", encoding="utf-8")
                print()
                print(f"Report saved to {output_path}")

            return 0

        if args.breakdown or args.breakdown_priority:
            report = build_breakdown_report(
                targets,
                wallet,
                item_counts,
                legendary_armory,
                item_names,
                currency_names,
                target_name=args.breakdown,
                use_priority=args.breakdown_priority,
                show_paths=args.show_paths,
            )
            print(report)

            if args.output:
                output_path = Path(args.output)
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_text(report + "\n", encoding="utf-8")
                print()
                print(f"Report saved to {output_path}")

            return 0

        price_estimates = None

        if args.no_prices:
            debug_print("Skipping Trading Post prices because --no-prices was used.")
        else:
            missing_item_ids = collect_missing_item_ids(
                targets,
                item_counts,
                legendary_armory,
                recipe_engine,
            )
            price_estimates = get_price_estimates(missing_item_ids, DEFAULT_PRICE_CACHE_PATH)

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
        ReferenceApiError,
        WikiImportError,
    ) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
