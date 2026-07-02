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

# Local file for remembering item name -> item_id lookups.
DEFAULT_ITEM_CACHE_PATH = Path("item_cache.json")

# Local file for remembering Trading Post prices for a short time.
DEFAULT_PRICE_CACHE_PATH = Path("price_cache.json")
PRICE_CACHE_TTL_SECONDS = 15 * 60

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


def api_get(path: str, api_key: str | None = None, params: dict[str, Any] | None = None) -> Any:
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

    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=30) as response:
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

        raise Gw2ApiError(message, status_code=error.code) from error
    except URLError as error:
        raise Gw2ApiError(f"Could not reach the GW2 API: {error.reason}") from error


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


def build_combined_item_counts(
    api_key: str,
    token_permissions: set[str],
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
    print("Scanning material storage...")
    material_storage = fetch_material_storage(api_key)
    for item_id, count in material_storage.items():
        add_item_count(item_counts, item_id, count)
    scan_summary["scanned_sources"].append(
        f"material storage ({len(material_storage):,} material entries)"
    )

    # The bank is account-wide storage. Empty bank slots are returned as null.
    print("Scanning bank...")
    bank_slots = fetch_bank(api_key)
    add_inventory_slots(item_counts, bank_slots)
    scan_summary["scanned_sources"].append(f"bank ({len(bank_slots):,} slots)")

    # Shared inventory slots are account-wide slots visible to all characters.
    print("Scanning shared inventory slots...")
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
    print("Finding characters...")
    character_names = fetch_character_names(api_key)

    for character_name in character_names:
        print(f"Scanning character inventory: {character_name}")
        character_inventory = fetch_character_inventory(api_key, character_name)
        add_character_inventory(item_counts, character_inventory)

    scan_summary["scanned_sources"].append(
        f"character inventories ({len(character_names):,} characters)"
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


def lookup_uncached_item_names(item_names: set[str]) -> dict[str, dict[str, Any]]:
    """Look up uncached material names using the public /v2/items endpoint."""

    names_by_normalized = {normalize_item_name(name): name for name in item_names}
    matches_by_name = {normalized_name: [] for normalized_name in names_by_normalized}

    print("Looking up item names from the official GW2 API...")
    item_ids = fetch_all_item_ids()

    for id_chunk in chunked(item_ids, 200):
        rows = api_get("/items", params={"ids": ",".join(str(item_id) for item_id in id_chunk)})

        for item in rows:
            normalized_name = normalize_item_name(item.get("name", ""))

            if normalized_name in matches_by_name:
                matches_by_name[normalized_name].append(item)

    resolved_items: dict[str, dict[str, Any]] = {}

    for normalized_name, typed_name in names_by_normalized.items():
        matches = matches_by_name[normalized_name]

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

    for item_id in sorted(item_ids):
        cached_price = get_cached_price(cache, item_id, now)

        if cached_price:
            price_estimates[item_id] = cached_price
        else:
            ids_to_fetch.append(item_id)

    if ids_to_fetch:
        print("Fetching Trading Post prices for missing materials...")

        for item_id in ids_to_fetch:
            price = fetch_price_for_item(item_id)
            price["fetched_at"] = now
            cache["prices_by_item_id"][str(item_id)] = price
            price_estimates[item_id] = price

        save_price_cache(cache_path, cache)

    return price_estimates


def load_goals(config_path: Path) -> list[dict[str, Any]]:
    """Load target legendary goals from the JSON config file."""

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as config_file:
        config = json.load(config_file)

    targets = config.get("targets", [])

    if not isinstance(targets, list):
        raise ValueError("legendary_goals.json must contain a list named 'targets'.")

    return [target for target in targets if target.get("enabled", True)]


def material_has_item_id(material: dict[str, Any]) -> bool:
    """Check whether a material already uses item_id or the older id shortcut."""

    return "item_id" in material or "id" in material


def resolve_goal_material_names(
    targets: list[dict[str, Any]],
    cache_path: Path,
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
        found_items = lookup_uncached_item_names(names_to_lookup)

        for typed_name, item in found_items.items():
            cache_item_lookup(cache, typed_name, item)

        save_item_cache(cache_path, cache)

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


def make_missing_lines(
    entries: list[dict[str, Any]],
    owned_counts: dict[int, int],
    api_names: dict[int, str],
    fallback_label: str,
    show_complete: bool,
) -> list[str]:
    """Build report lines for one group, such as materials or currencies."""

    lines: list[str] = []

    if not entries:
        return ["    No goals listed in this section."]

    missing_count = 0

    for entry in entries:
        entry_id = entry["id"]
        needed = entry["amount"]
        owned = owned_counts.get(entry_id, 0)
        missing = max(needed - owned, 0)
        name = entry["name"] or api_names.get(entry_id) or f"{fallback_label} {entry_id}"

        if missing > 0:
            missing_count += 1
            lines.append(f"    - {name}: missing {missing:,} (have {owned:,}, need {needed:,})")
        elif show_complete:
            lines.append(f"    - {name}: complete (have {owned:,}, need {needed:,})")

    if missing_count == 0 and not show_complete:
        lines.append("    Nothing missing here.")

    return lines


def make_missing_item_lines(
    entries: list[dict[str, Any]],
    owned_counts: dict[int, int],
    api_names: dict[int, str],
    show_complete: bool,
    price_estimates: dict[int, dict[str, Any]] | None,
) -> list[str]:
    """Build missing item lines, optionally adding Trading Post estimates."""

    lines: list[str] = []

    if not entries:
        return ["    No goals listed in this section."]

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
                line += f" - {price_text_for_missing_entry({'id': entry_id, 'missing': missing}, price_estimates)}"

            lines.append(line)
        elif show_complete:
            lines.append(f"    - {name}: complete (have {owned:,}, need {needed:,})")

    if missing_count == 0 and not show_complete:
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
    not_priced_entries = []

    for entry in missing_entries:
        price = price_estimates.get(entry["id"])

        if not price or price.get("sell_unit_price") is None:
            not_priced_entries.append(entry)
            continue

        total_copper += int(price["sell_unit_price"]) * int(entry["missing"])

    lines.append(f"  Estimated buy-now cost for priced missing items: {format_coin(total_copper)}")

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
) -> None:
    """Add a report section for configured targets already in the Legendary Armory."""

    lines.append("Already unlocked legendaries")

    if not armory_summary["scanned"]:
        lines.append(f"  Skipped: {armory_summary['skipped_reason']}")
        lines.append("")
        return

    unlocked_targets = [
        target for target in targets if is_target_unlocked(target, legendary_armory)
    ]

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
) -> str:
    """Create the final text report."""

    lines = [
        "Guild Wars 2 Legendary Planner",
        "================================",
        f"Wallet currencies fetched: {len(wallet):,}",
        f"Combined item IDs counted: {len(item_counts):,}",
    ]

    if scan_summary["scanned_sources"]:
        lines.append("Scanned item sources:")
        for source in scan_summary["scanned_sources"]:
            lines.append(f"  - {source}")

    if scan_summary["skipped_sources"]:
        lines.append("Skipped item sources:")
        for source in scan_summary["skipped_sources"]:
            lines.append(f"  - {source}")

    lines.append("")
    add_already_unlocked_section(lines, targets, legendary_armory, armory_summary, item_names)

    if not targets:
        lines.extend(
            [
                "No enabled targets found.",
                "Edit legendary_goals.json and add a target to start tracking.",
            ]
        )
        return "\n".join(lines)

    for target in targets:
        target_name = target.get("name", "Unnamed target")
        final_item_id = target_final_item_id(target)
        target_is_unlocked = is_target_unlocked(target, legendary_armory)

        if target_is_unlocked:
            final_item_name = item_names.get(final_item_id, f"Item {final_item_id}")
            lines.append(f"Target: {target_name} - complete")
            lines.append(f"  Already unlocked in Legendary Armory: {final_item_name}")
            lines.append("")
            continue

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

        lines.append(f"Target: {target_name}")
        lines.append("  Missing items from account storage and inventories:")
        lines.extend(
            make_missing_item_lines(
                material_entries,
                item_counts,
                item_names,
                show_complete,
                price_estimates,
            )
        )
        missing_material_entries = get_missing_entries(
            material_entries,
            item_counts,
            item_names,
            "Item",
        )
        add_price_summary_lines(lines, missing_material_entries, price_estimates)
        lines.append("  Missing wallet currencies:")
        lines.extend(
            make_missing_lines(
                currency_entries,
                wallet,
                currency_names,
                "Currency",
                show_complete,
            )
        )
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
        "--no-prices",
        action="store_true",
        help="Skip Trading Post price estimates.",
    )
    return parser.parse_args()


def main() -> int:
    """Program entry point."""

    args = parse_args()

    try:
        api_key = find_api_key(Path(args.env) if args.env else None)

        print("Checking API key permissions...")
        token_permissions = fetch_token_permissions(api_key)
        warn_about_missing_permissions(token_permissions)

        targets = load_goals(Path(args.config))
        targets = resolve_goal_material_names(targets, DEFAULT_ITEM_CACHE_PATH)

        legendary_armory: dict[int, int] = {}
        armory_summary: dict[str, Any] = {
            "scanned": False,
            "skipped_reason": "missing unlocks permission",
        }

        if "unlocks" in token_permissions:
            print("Checking Legendary Armory unlocks...")
            legendary_armory = fetch_legendary_armory(api_key)
            armory_summary = {
                "scanned": True,
                "skipped_reason": "",
            }
        else:
            print("Skipping Legendary Armory because the API key is missing unlocks permission.")

        wallet: dict[int, int] = {}

        if "wallet" in token_permissions:
            print("Fetching wallet currencies from the official GW2 API...")
            wallet = fetch_wallet(api_key)
        else:
            print("Skipping wallet currencies because the API key is missing wallet permission.")

        item_counts, scan_summary = build_combined_item_counts(api_key, token_permissions)

        item_ids, currency_ids = collect_goal_ids(targets)
        price_estimates = None

        if args.no_prices:
            print("Skipping Trading Post prices because --no-prices was used.")
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
        )

        print()
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
