"""Resolve Guild Wars 2 crafting recipe trees from official API data.

The recipe engine never reads or sends the user's private account token. Normal
runs prefer the local public reference database, while curated override data
stays separate so the app can be clear about what came from ArenaNet and what
still needs human judgment.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from reference_database import (
    DEFAULT_REFERENCE_DB_PATH,
    ReferenceDatabase,
    normalize_wiki_item_text,
)


API_BASE_URL = "https://api.guildwars2.com/v2"

DEFAULT_RECIPE_CACHE_PATH = Path("recipe_cache.json")
DEFAULT_RECIPE_OVERRIDES_PATH = Path("data") / "recipe_overrides.json"

RECIPE_CACHE_VERSION = 1
RECIPE_OVERRIDES_VERSION = 1
DEFAULT_MAX_RECIPE_DEPTH = 20
DEFAULT_API_TIMEOUT_SECONDS = 60
API_RETRY_DELAYS_SECONDS = (2, 5, 10)
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
API_BATCH_SIZE = 200

ACCOUNT_BOUND_FLAGS = {
    "accountbound",
    "soulbindonacquire",
}

MANUAL_OVERRIDE_TYPES = {
    "manual",
    "currency",
    "achievement",
    "collection",
    "mystic_forge",
    "vendor",
    "time_gated",
    "account_bound",
}

NON_PRICEABLE_OVERRIDE_TYPES = {
    "manual",
    "currency",
    "achievement",
    "collection",
    "mystic_forge",
    "vendor",
    "time_gated",
    "account_bound",
}

WIKI_STUB_CONTINUE_OVERRIDE_TYPES = {
    "mystic_forge",
}


class RecipeApiError(Exception):
    """Raised when the public GW2 recipe API cannot return needed data."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def api_get(
    path: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = DEFAULT_API_TIMEOUT_SECONDS,
) -> Any:
    """Call a public GW2 API endpoint and return decoded JSON."""

    url = f"{API_BASE_URL}{path}"

    if params:
        url = f"{url}?{urlencode(params)}"

    headers = {
        "Accept": "application/json",
        "User-Agent": "gw2-legendary-planner/1.0",
    }

    total_attempts = len(API_RETRY_DELAYS_SECONDS) + 1
    total_retries = len(API_RETRY_DELAYS_SECONDS)
    last_error: RecipeApiError | None = None

    for attempt_number in range(1, total_attempts + 1):
        request = Request(url, headers=headers)

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            message = f"GW2 API returned HTTP {error.code} for {path}."

            if body:
                message += f" API message: {body}"

            last_error = RecipeApiError(message, status_code=error.code)

            if error.code not in RETRYABLE_HTTP_STATUS_CODES:
                raise last_error from error
        except TimeoutError as error:
            last_error = RecipeApiError(
                f"The GW2 API request for {path} timed out after "
                f"{timeout_seconds} seconds."
            )
        except URLError as error:
            last_error = RecipeApiError(
                f"Could not reach the GW2 API for {path}: {error.reason}"
            )

        if attempt_number < total_attempts:
            time.sleep(API_RETRY_DELAYS_SECONDS[attempt_number - 1])

    raise RecipeApiError(
        f"The GW2 API did not respond successfully for {path} after "
        f"{total_retries} retries. Last error: {last_error}"
    )


def chunked(values: list[int], size: int) -> list[list[int]]:
    """Split a list into smaller chunks for API calls."""

    return [values[index : index + size] for index in range(0, len(values), size)]


def empty_recipe_cache() -> dict[str, Any]:
    """Create the starting shape for recipe_cache.json."""

    return {
        "version": RECIPE_CACHE_VERSION,
        "recipe_search_by_output": {},
        "recipes_by_id": {},
        "items_by_id": {},
    }


def load_recipe_cache(cache_path: Path) -> dict[str, Any]:
    """Load cached official recipe data, resetting old cache shapes safely."""

    if not cache_path.exists():
        return empty_recipe_cache()

    with cache_path.open("r", encoding="utf-8") as cache_file:
        cache = json.load(cache_file)

    if not isinstance(cache, dict):
        return empty_recipe_cache()

    if cache.get("version") != RECIPE_CACHE_VERSION:
        return empty_recipe_cache()

    cache.setdefault("recipe_search_by_output", {})
    cache.setdefault("recipes_by_id", {})
    cache.setdefault("items_by_id", {})
    return cache


def save_recipe_cache(cache_path: Path, cache: dict[str, Any]) -> None:
    """Save official recipe API data for future runs."""

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(cache, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def normalize_item_name(item_name: str) -> str:
    """Make item names easier to compare by ignoring case and extra spaces."""

    return " ".join(item_name.casefold().split())


def wiki_item_name_candidates(item_name: str) -> list[str]:
    """Return safe name variants for wiki ingredient text.

    The wiki sometimes writes normal plural text such as "Mystic Clovers", while
    the official item name is singular. These fallbacks only help if the local
    reference database finds exactly one matching item.
    """

    clean_name = " ".join(str(item_name).strip().split())
    candidates: list[str] = []

    if clean_name:
        candidates.append(clean_name)

    if len(clean_name) > 4 and clean_name.endswith("ies"):
        candidates.append(clean_name[:-3] + "y")
    elif len(clean_name) > 3 and clean_name.endswith("s") and not clean_name.endswith("ss"):
        candidates.append(clean_name[:-1])

    unique_candidates: list[str] = []
    for candidate in candidates:
        if candidate not in unique_candidates:
            unique_candidates.append(candidate)

    return unique_candidates


def parse_wiki_ingredient_text(ingredient_text: str) -> dict[str, Any] | None:
    """Parse a simple wiki recipe ingredient line like '1 Gift of Battle'."""

    original_text = str(ingredient_text).strip()
    clean_text = " ".join(original_text.replace("\xa0", " ").split())

    if not clean_text:
        return None

    match = re.match(r"^(?P<amount>\d[\d,]*)\s+(?P<name>.+)$", clean_text)

    if not match:
        return None

    name_details = normalize_wiki_item_text(match.group("name").strip())

    return {
        "amount": int(match.group("amount").replace(",", "")),
        "name": name_details["normalized_name"],
        "original_wiki_text": original_text,
        "original_wiki_name": match.group("name").strip(),
        "wiki_page_title": name_details["page_title"],
        "wiki_display_text": name_details["display_text"],
        "wiki_name_candidates": [
            candidate
            for name in name_details["candidates"]
            for candidate in wiki_item_name_candidates(name)
        ],
    }


def extract_wiki_recipe_templates(wikitext: str) -> list[dict[str, Any]]:
    """Extract simple {{Recipe ...}} templates from stored wiki wikitext."""

    recipe_blocks: list[str] = []
    current_block: list[str] = []
    inside_recipe = False
    brace_balance = 0

    for raw_line in wikitext.splitlines():
        line = raw_line.strip()

        if not inside_recipe and line.startswith("{{Recipe"):
            inside_recipe = True
            current_block = [line]
            brace_balance = line.count("{{") - line.count("}}")

            if brace_balance <= 0:
                recipe_blocks.append("\n".join(current_block))
                inside_recipe = False

            continue

        if not inside_recipe:
            continue

        current_block.append(line)
        brace_balance += line.count("{{") - line.count("}}")

        if brace_balance <= 0:
            recipe_blocks.append("\n".join(current_block))
            inside_recipe = False

    parsed_recipes: list[dict[str, Any]] = []

    for block in recipe_blocks:
        fields: dict[str, str] = {}

        for raw_line in block.splitlines():
            line = raw_line.strip()

            if not line.startswith("|"):
                continue

            key, separator, value = line[1:].partition("=")

            if not separator:
                continue

            fields[key.strip().casefold()] = value.strip()

        ingredients: list[dict[str, Any]] = []

        for key in sorted(fields):
            if not re.fullmatch(r"ingredient\d+", key):
                continue

            parsed_ingredient = parse_wiki_ingredient_text(fields[key])

            if parsed_ingredient:
                ingredients.append(parsed_ingredient)

        if not ingredients:
            continue

        parsed_recipes.append(
            {
                "source_hint": fields.get("source", "").strip(),
                "ingredients": ingredients,
                "raw_template": block,
            }
        )

    return parsed_recipes


def empty_recipe_overrides() -> dict[str, Any]:
    """Create an empty curated override configuration."""

    return {
        "version": RECIPE_OVERRIDES_VERSION,
        "preferred_recipes_by_output": {},
        "overrides": [],
    }


def load_recipe_overrides(overrides_path: Path) -> dict[str, Any]:
    """Load curated recipe choices and manual item stops."""

    if not overrides_path.exists():
        return empty_recipe_overrides()

    with overrides_path.open("r", encoding="utf-8") as overrides_file:
        overrides = json.load(overrides_file)

    if not isinstance(overrides, dict):
        raise ValueError(f"{overrides_path} must contain one JSON object.")

    if overrides.get("version", RECIPE_OVERRIDES_VERSION) != RECIPE_OVERRIDES_VERSION:
        raise ValueError(f"{overrides_path} uses an unsupported override version.")

    overrides.setdefault("preferred_recipes_by_output", {})
    overrides.setdefault("overrides", [])
    return overrides


def normalize_preferred_recipes(overrides: dict[str, Any]) -> dict[int, int]:
    """Return {output_item_id: preferred_recipe_id} from override JSON."""

    preferred: dict[int, int] = {}
    raw_mapping = overrides.get("preferred_recipes_by_output", {})

    if not isinstance(raw_mapping, dict):
        raise ValueError("preferred_recipes_by_output must be a JSON object.")

    for output_item_id, recipe_id in raw_mapping.items():
        try:
            preferred[int(output_item_id)] = int(recipe_id)
        except (TypeError, ValueError) as error:
            raise ValueError(
                "preferred_recipes_by_output must map item IDs to recipe IDs."
            ) from error

    return preferred


def normalize_override_entry(override: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalize one curated recipe override."""

    item_id = override.get("item_id", override.get("id"))
    item_name = str(override.get("name", "")).strip()
    override_type = str(override.get("type", "manual")).strip().casefold()

    if not item_id and not item_name:
        raise ValueError("Every recipe override needs item_id or name.")

    if override_type not in MANUAL_OVERRIDE_TYPES:
        raise ValueError(
            f"Recipe override type '{override_type}' is not supported. "
            f"Use one of: {', '.join(sorted(MANUAL_OVERRIDE_TYPES))}."
        )

    normalized: dict[str, Any] = {
        "type": override_type,
        "name": item_name,
        "source_hint": str(override.get("source_hint", "")).strip(),
        "notes": str(override.get("notes", "")).strip(),
        "verified": bool(override.get("verified", False)),
        "output_count": int(override.get("output_count", 1)),
        "ingredients": [],
    }

    if item_id:
        try:
            normalized["item_id"] = int(item_id)
        except (TypeError, ValueError) as error:
            raise ValueError("Recipe override item_id values must be integers.") from error

    ingredients = override.get("ingredients", [])

    if ingredients is None:
        ingredients = []

    if not isinstance(ingredients, list):
        raise ValueError("Recipe override ingredients must be a list.")

    for ingredient in ingredients:
        if not isinstance(ingredient, dict):
            raise ValueError("Every recipe override ingredient must be an object.")

        ingredient_item_id = ingredient.get("item_id", ingredient.get("id"))
        ingredient_name = str(ingredient.get("name", "")).strip()
        ingredient_amount = ingredient.get(
            "amount",
            ingredient.get("count", ingredient.get("quantity")),
        )

        if ingredient_amount is None:
            raise ValueError("Override ingredients need amount, count, or quantity.")

        normalized_ingredient = {
            "name": ingredient_name,
            "amount": int(ingredient_amount),
            "source_hint": str(ingredient.get("source_hint", "")).strip(),
            "notes": str(ingredient.get("notes", "")).strip(),
        }

        if ingredient_item_id:
            normalized_ingredient["item_id"] = int(ingredient_item_id)

        normalized["ingredients"].append(normalized_ingredient)

    return normalized


def normalize_recipe_overrides(
    overrides: dict[str, Any],
) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Return recipe overrides keyed by item ID and normalized item name."""

    raw_overrides = overrides.get("overrides", [])

    if not isinstance(raw_overrides, list):
        raise ValueError("recipe_overrides.json field 'overrides' must be a list.")

    overrides_by_item_id: dict[int, dict[str, Any]] = {}
    overrides_by_name: dict[str, dict[str, Any]] = {}

    for raw_override in raw_overrides:
        if not isinstance(raw_override, dict):
            raise ValueError("Every recipe override must be an object.")

        override = normalize_override_entry(raw_override)

        if "item_id" in override:
            overrides_by_item_id[int(override["item_id"])] = override

        if override.get("name"):
            overrides_by_name[normalize_item_name(override["name"])] = override

    return overrides_by_item_id, overrides_by_name


def override_reason(override: dict[str, Any]) -> str:
    """Turn one override into a readable manual-stop reason."""

    reason = f"Override type: {override['type']}."

    if override.get("source_hint"):
        reason += f" Source: {override['source_hint']}"

    if override.get("notes"):
        reason += f" Notes: {override['notes']}"

    return reason


def override_is_non_priceable(override: dict[str, Any]) -> bool:
    """Check whether this override should skip Trading Post pricing."""

    return str(override.get("type", "")).casefold() in NON_PRICEABLE_OVERRIDE_TYPES


def override_item_ids_by_type(
    override_types: set[str],
    overrides_path: Path = DEFAULT_RECIPE_OVERRIDES_PATH,
) -> set[int]:
    """Return item IDs from overrides whose type is in override_types."""

    overrides = load_recipe_overrides(overrides_path)
    overrides_by_item_id, _overrides_by_name = normalize_recipe_overrides(overrides)
    normalized_types = {override_type.casefold() for override_type in override_types}
    item_ids: set[int] = set()

    for item_id, override in overrides_by_item_id.items():
        if str(override.get("type", "")).casefold() in normalized_types:
            item_ids.add(item_id)

    return item_ids


def item_has_account_bound_flags(item: dict[str, Any]) -> bool:
    """Check official item flags for account-bound or soulbound behavior."""

    flags = item.get("flags", [])

    if not isinstance(flags, list):
        return False

    normalized_flags = {str(flag).casefold() for flag in flags}
    return bool(normalized_flags & ACCOUNT_BOUND_FLAGS)


class RecipeEngine:
    """Resolve recipe trees using official API data plus curated overrides."""

    def __init__(
        self,
        cache_path: Path = DEFAULT_RECIPE_CACHE_PATH,
        overrides_path: Path = DEFAULT_RECIPE_OVERRIDES_PATH,
        reference_db_path: Path = DEFAULT_REFERENCE_DB_PATH,
        max_depth: int = DEFAULT_MAX_RECIPE_DEPTH,
    ) -> None:
        self.cache_path = cache_path
        self.cache = load_recipe_cache(cache_path)
        self.reference_database = ReferenceDatabase(reference_db_path)
        self.using_reference_database = self.reference_database.exists()
        overrides = load_recipe_overrides(overrides_path)
        self.preferred_recipes_by_output = normalize_preferred_recipes(overrides)
        (
            self.overrides_by_item_id,
            self.overrides_by_name,
        ) = normalize_recipe_overrides(overrides)
        self.max_depth = max_depth

        self.raw_materials: dict[int, dict[str, Any]] = {}
        self.craftable_ingredients: dict[int, dict[str, Any]] = {}
        self.major_components: dict[str, dict[str, Any]] = {}
        self.manual_steps: dict[str, dict[str, Any]] = {}
        self.expanded_source_steps: dict[int, dict[str, Any]] = {}
        self.satisfied_intermediates: dict[int, dict[str, Any]] = {}
        self.owned_items_used: dict[int, int] = {}
        self.account_item_counts: dict[int, int] = {}
        self.use_owned_intermediates = True
        self.recipes_used: dict[int, dict[str, Any]] = {}
        self.resolution_log: dict[int, dict[str, Any]] = {}
        self.wiki_sources_used: dict[int, dict[str, Any]] = {}
        self.warnings: list[str] = []
        self.debug_events: list[str] = []

    def save_cache(self) -> None:
        """Persist the official API cache."""

        save_recipe_cache(self.cache_path, self.cache)

    def recipe_ids_for_output(self, item_id: int) -> list[int]:
        """Find official recipes that output one item ID."""

        if self.using_reference_database:
            return self.reference_database.recipe_ids_for_output(item_id)

        key = str(item_id)
        cached_recipe_ids = self.cache["recipe_search_by_output"].get(key)

        if cached_recipe_ids is None:
            recipe_ids = api_get("/recipes/search", params={"output": item_id})

            if not isinstance(recipe_ids, list):
                raise RecipeApiError(
                    f"The recipe search response for item {item_id} was not a list."
                )

            self.cache["recipe_search_by_output"][key] = [int(value) for value in recipe_ids]
            self.save_cache()

        return [int(value) for value in self.cache["recipe_search_by_output"][key]]

    def recipes_by_id(self, recipe_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch official recipe details by recipe ID."""

        if self.using_reference_database:
            return self.reference_database.recipes_by_id(recipe_ids)

        unique_recipe_ids = sorted({int(recipe_id) for recipe_id in recipe_ids})
        missing_recipe_ids = [
            recipe_id
            for recipe_id in unique_recipe_ids
            if str(recipe_id) not in self.cache["recipes_by_id"]
        ]

        for recipe_id_chunk in chunked(missing_recipe_ids, API_BATCH_SIZE):
            rows = api_get(
                "/recipes",
                params={"ids": ",".join(str(recipe_id) for recipe_id in recipe_id_chunk)},
            )

            if not isinstance(rows, list):
                raise RecipeApiError("The recipe details response was not a list.")

            for row in rows:
                if not isinstance(row, dict) or "id" not in row:
                    continue

                self.cache["recipes_by_id"][str(row["id"])] = row

            self.save_cache()

        recipes: dict[int, dict[str, Any]] = {}

        for recipe_id in unique_recipe_ids:
            recipe = self.cache["recipes_by_id"].get(str(recipe_id))

            if isinstance(recipe, dict):
                recipes[recipe_id] = recipe

        return recipes

    def items_by_id(self, item_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Fetch official item details by item ID."""

        if self.using_reference_database:
            return self.reference_database.items_by_id(item_ids)

        unique_item_ids = sorted({int(item_id) for item_id in item_ids})
        missing_item_ids = [
            item_id
            for item_id in unique_item_ids
            if str(item_id) not in self.cache["items_by_id"]
        ]

        for item_id_chunk in chunked(missing_item_ids, API_BATCH_SIZE):
            rows = api_get(
                "/items",
                params={"ids": ",".join(str(item_id) for item_id in item_id_chunk)},
            )

            if not isinstance(rows, list):
                raise RecipeApiError("The item details response was not a list.")

            for row in rows:
                if not isinstance(row, dict) or "id" not in row:
                    continue

                self.cache["items_by_id"][str(row["id"])] = row

            self.save_cache()

        items: dict[int, dict[str, Any]] = {}

        for item_id in unique_item_ids:
            item = self.cache["items_by_id"].get(str(item_id))

            if isinstance(item, dict):
                items[item_id] = item

        return items

    def item_name(self, item_id: int) -> str:
        """Return an item name from cache or API, falling back to Item <id>."""

        override = self.overrides_by_item_id.get(item_id)

        if override and override.get("name"):
            return override["name"]

        try:
            item = self.items_by_id([item_id]).get(item_id, {})
        except RecipeApiError:
            item = {}

        return str(item.get("name") or f"Item {item_id}")

    def override_for_item(
        self,
        item_id: int,
        item_name: str | None = None,
    ) -> dict[str, Any] | None:
        """Return a curated override by item ID or official item name."""

        override = self.overrides_by_item_id.get(item_id)

        if override:
            return override

        if not item_name and not self.overrides_by_name:
            return None

        resolved_name = item_name or self.item_name(item_id)

        if not resolved_name:
            return None

        return self.overrides_by_name.get(normalize_item_name(resolved_name))

    def is_item_non_priceable_override(
        self,
        item_id: int,
        item_name: str | None = None,
    ) -> bool:
        """Check whether an override says this item should skip TP pricing."""

        override = self.override_for_item(item_id, item_name)
        return bool(override and override_is_non_priceable(override))

    def override_decision_for_item(
        self,
        item_id: int,
        item_name: str | None = None,
    ) -> dict[str, Any]:
        """Classify how one override should affect recipe resolution."""

        override = self.override_for_item(item_id, item_name)

        if not override:
            return {
                "status": "none",
                "reason": "No matching override found.",
                "override": None,
            }

        override_type = str(override.get("type", "manual")).casefold()
        ingredients = list(override.get("ingredients", []))
        verified = bool(override.get("verified", False))

        if ingredients and verified:
            return {
                "status": "verified_ingredients",
                "reason": (
                    f"Override '{override_type}' has verified ingredients and takes priority."
                ),
                "override": override,
                "override_type": override_type,
            }

        if ingredients:
            return {
                "status": "unverified_ingredients",
                "reason": (
                    f"Override '{override_type}' includes ingredients, but verified is false, "
                    "so automatic resolution continues to safer sources."
                ),
                "override": override,
                "override_type": override_type,
            }

        if verified or override_type not in WIKI_STUB_CONTINUE_OVERRIDE_TYPES:
            return {
                "status": "manual_stop",
                "reason": (
                    f"Override '{override_type}' has no ingredients and is treated as an "
                    "intentional manual stop."
                ),
                "override": override,
                "override_type": override_type,
            }

        return {
            "status": "stub_continue",
            "reason": (
                f"Override '{override_type}' is an unverified empty stub, so automatic "
                "resolution continues to imported wiki data."
            ),
            "override": override,
            "override_type": override_type,
        }

    def official_recipe_lookup_summary(self, item_id: int) -> dict[str, Any]:
        """Summarize official recipe lookup results for one item."""

        try:
            recipe_ids = self.recipe_ids_for_output(item_id)
        except RecipeApiError as error:
            return {
                "status": "lookup_failed",
                "recipe_ids": [],
                "reason": f"Official recipe lookup failed: {error}",
                "error": str(error),
            }

        if not recipe_ids:
            return {
                "status": "no_recipe",
                "recipe_ids": [],
                "reason": "The official API has no recipe that outputs this item.",
            }

        if len(recipe_ids) == 1:
            return {
                "status": "single_recipe",
                "recipe_ids": list(recipe_ids),
                "recipe_id": recipe_ids[0],
                "reason": f"One official recipe was found: {recipe_ids[0]}.",
            }

        preferred_recipe_id = self.preferred_recipes_by_output.get(item_id)

        if preferred_recipe_id in recipe_ids:
            return {
                "status": "preferred_recipe",
                "recipe_ids": list(recipe_ids),
                "recipe_id": preferred_recipe_id,
                "reason": (
                    f"Multiple official recipes were found; preferred recipe "
                    f"{preferred_recipe_id} is configured."
                ),
            }

        recipe_text = ", ".join(str(recipe_id) for recipe_id in recipe_ids)
        return {
            "status": "multiple_recipes",
            "recipe_ids": list(recipe_ids),
            "reason": (
                "Multiple official recipes were found and no valid preferred recipe is "
                f"configured. Recipe IDs: {recipe_text}"
            ),
        }

    def resolve_wiki_ingredients(
        self,
        ingredients: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Resolve imported wiki ingredient names to item IDs when possible."""

        resolved_ingredients: list[dict[str, Any]] = []
        issues: list[str] = []

        for ingredient in ingredients:
            ingredient_name = str(ingredient.get("name", "")).strip()
            candidate_names = [
                str(candidate).strip()
                for candidate in ingredient.get("wiki_name_candidates", [ingredient_name])
                if str(candidate).strip()
            ]
            resolved_ingredient = {
                "amount": int(ingredient["amount"]),
                "name": ingredient_name or "Unnamed wiki ingredient",
                "original_wiki_text": ingredient.get("original_wiki_text", ""),
                "original_wiki_name": ingredient.get("original_wiki_name", ""),
                "wiki_page_title": ingredient.get("wiki_page_title", ""),
                "wiki_display_text": ingredient.get("wiki_display_text", ""),
            }

            if self.using_reference_database and candidate_names:
                chosen_matches: list[dict[str, Any]] = []
                ambiguous_candidates: list[str] = []

                for candidate_name in candidate_names:
                    matches = self.reference_database.item_name_matches(candidate_name)

                    if len(matches) == 1:
                        chosen_matches = matches
                        resolved_ingredient["name"] = matches[0].get("name", candidate_name)
                        break

                    if len(matches) > 1:
                        ambiguous_candidates.append(candidate_name)

                if chosen_matches:
                    resolved_ingredient["item_id"] = int(chosen_matches[0]["item_id"])
                elif ambiguous_candidates:
                    issues.append(
                        "Imported GW2 Wiki ingredient "
                        f"'{ingredient.get('original_wiki_name', ingredient_name)}' is "
                        "ambiguous in the reference database. Add a verified override before "
                        "relying on it."
                    )
                else:
                    issues.append(
                        "Imported GW2 Wiki ingredient "
                        f"'{ingredient.get('original_wiki_name', ingredient_name)}' was not "
                        "found in the reference database."
                    )

            resolved_ingredients.append(resolved_ingredient)

        return resolved_ingredients, issues

    def wiki_recipe_summary_for_item(
        self,
        item_id: int,
        item_name: str | None = None,
    ) -> dict[str, Any]:
        """Summarize imported wiki recipe data for one item."""

        resolved_name = item_name or self.item_name(item_id)
        summary: dict[str, Any] = {
            "item_id": item_id,
            "name": resolved_name,
            "rows": [],
            "row_count": 0,
            "acquisition_options": [],
            "acquisition_option_count": 0,
            "multiple_acquisition_options": False,
            "recipe_option_count": 0,
            "parsed_recipes": [],
            "parsed_recipe_count": 0,
            "selected_recipe": None,
            "resolved_ingredients": [],
            "ingredient_issues": [],
            "source_url": "",
            "review_status": "",
            "reason": "No imported wiki recipe rows found.",
        }

        if not self.using_reference_database:
            summary["reason"] = "The local reference database is not available."
            return summary

        rows = self.reference_database.wiki_recipes_for_output(item_id)
        options = self.reference_database.acquisition_options_for_item(item_id)
        parsed_recipes: list[dict[str, Any]] = []

        for row in rows:
            raw_payload = row.get("raw_json", {})
            wikitext = str(raw_payload.get("wikitext", ""))

            for parsed_recipe in extract_wiki_recipe_templates(wikitext):
                parsed_recipes.append(
                    {
                        **parsed_recipe,
                        "source_url": row["source_url"],
                        "review_status": row["review_status"],
                        "name": row.get("name") or resolved_name,
                    }
                )

        recipe_option_count = sum(
            1
            for option in options
            if str(option.get("acquisition_type", "")).casefold() == "recipe"
        )
        primary_row = rows[0] if rows else None

        summary.update(
            {
                "rows": rows,
                "row_count": len(rows),
                "acquisition_options": options,
                "acquisition_option_count": len(options),
                "multiple_acquisition_options": len(options) > 1,
                "recipe_option_count": recipe_option_count,
                "parsed_recipes": parsed_recipes,
                "parsed_recipe_count": len(parsed_recipes),
                "source_url": primary_row.get("source_url", "") if primary_row else "",
                "review_status": primary_row.get("review_status", "") if primary_row else "",
            }
        )

        if not rows:
            return summary

        if not parsed_recipes:
            summary["reason"] = (
                "Imported GW2 Wiki data exists, but no usable {{Recipe}} ingredient block "
                "was parsed from it."
            )
            return summary

        if len(parsed_recipes) > 1:
            summary["reason"] = (
                "Imported GW2 Wiki data contains multiple recipe templates, so a manual "
                "choice is still needed."
            )
            return summary

        if len(options) > 1 and recipe_option_count != 1:
            summary["reason"] = (
                "Imported GW2 Wiki data has multiple acquisition options and no single "
                "recipe option is clearly marked as the default."
            )
            return summary

        selected_recipe = parsed_recipes[0]
        resolved_ingredients, ingredient_issues = self.resolve_wiki_ingredients(
            selected_recipe["ingredients"]
        )
        ingredient_name_counts: dict[str, int] = {}

        for ingredient in resolved_ingredients:
            normalized_ingredient_name = normalize_item_name(str(ingredient.get("name", "")))

            if not normalized_ingredient_name:
                continue

            ingredient_name_counts[normalized_ingredient_name] = (
                ingredient_name_counts.get(normalized_ingredient_name, 0) + 1
            )

        duplicate_ingredient_names = sorted(
            ingredient_name
            for ingredient_name, count in ingredient_name_counts.items()
            if count > 1
        )

        if duplicate_ingredient_names:
            readable_duplicates = ", ".join(duplicate_ingredient_names)
            ingredient_issues.append(
                "Imported GW2 Wiki recipe data has duplicate ingredient names in one "
                f"recipe ({readable_duplicates}). Review this page before fully trusting "
                "the parsed quantities."
            )

        summary["selected_recipe"] = selected_recipe
        summary["resolved_ingredients"] = resolved_ingredients
        summary["ingredient_issues"] = ingredient_issues
        summary["duplicate_ingredient_names"] = duplicate_ingredient_names

        if len(options) > 1 and recipe_option_count == 1:
            summary["reason"] = (
                "Imported GW2 Wiki data has multiple acquisition options, but exactly one "
                "recipe option is clearly marked."
            )
        else:
            summary["reason"] = "One imported GW2 Wiki recipe template was found."

        return summary

    def record_resolution(
        self,
        item_id: int,
        source: str,
        reason: str,
        **metadata: Any,
    ) -> None:
        """Record which source was chosen for one item."""

        self.resolution_log[item_id] = {
            "item_id": item_id,
            "name": self.item_name(item_id),
            "source": source,
            "reason": reason,
            **metadata,
        }

    def record_wiki_source_usage(
        self,
        item_id: int,
        item_name: str,
        wiki_summary: dict[str, Any],
        used_for_recipe: bool,
        selection_reason: str,
    ) -> None:
        """Track wiki source metadata so reports can show it cleanly."""

        self.wiki_sources_used[item_id] = {
            "item_id": item_id,
            "name": item_name,
            "source": "GW2 Wiki",
            "review_status": wiki_summary.get("review_status", ""),
            "source_url": wiki_summary.get("source_url", ""),
            "acquisition_option_count": int(wiki_summary.get("acquisition_option_count", 0)),
            "multiple_acquisition_options": bool(
                wiki_summary.get("multiple_acquisition_options", False)
            ),
            "used_for_recipe": used_for_recipe,
            "selection_reason": selection_reason,
        }

    def source_step_summary_for_item(
        self,
        item_id: int,
        item_name: str | None = None,
    ) -> dict[str, Any]:
        """Return imported wiki source-step data for a manual item, if available."""

        resolved_name = item_name or self.item_name(item_id)
        empty_summary = {
            "item_id": int(item_id),
            "name": resolved_name,
            "source_url": "",
            "review_status": "not_imported",
            "source_type": "unknown",
            "source_step_summary": "",
            "source_summary": "",
            "source_parse_status": "",
            "source_parse_reason": "",
            "parsed_recipe_ingredients": [],
            "parsed_recipe_count": 0,
            "acquisition_option_count": 0,
            "wiki_page_found": False,
            "wiki_rows_found": 0,
            "raw_wiki_page_title": "",
        }

        if not self.using_reference_database:
            return empty_summary

        summary = self.reference_database.source_step_for_item(item_id)

        if not summary.get("name"):
            summary["name"] = resolved_name

        return {**empty_summary, **summary}

    def manual_step_metadata(
        self,
        item_id: int,
        item_name: str,
        source_step: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Build source-step metadata for reportable manual recipe stops."""

        metadata = source_step or self.source_step_summary_for_item(item_id, item_name)

        if metadata.get("source_step_summary") and not metadata.get("source_summary"):
            metadata["source_summary"] = metadata["source_step_summary"]

        if metadata.get("wiki_page_found") and not metadata.get("source_summary"):
            metadata["source_summary"] = (
                "Wiki page found, but structured source data could not be parsed cleanly."
            )
            metadata["source_step_summary"] = metadata["source_summary"]

        return metadata

    def apply_override(
        self,
        item_id: int,
        amount: int,
        override: dict[str, Any],
        current_path: list[int],
        active_item_ids: set[int],
        depth: int,
        max_depth: int,
    ) -> None:
        """Apply a curated override before falling back to official recipes."""

        item_name = self.item_name(item_id)
        ingredients = override.get("ingredients", [])

        if ingredients:
            remaining_amount = self.use_owned_intermediate(
                item_id,
                amount,
                current_path,
            )

            if remaining_amount <= 0:
                return

            amount = remaining_amount
            output_count = max(int(override.get("output_count", 1)), 1)
            craft_count = math.ceil(amount / output_count)

            next_active_item_ids = set(active_item_ids)
            next_active_item_ids.add(item_id)

            for ingredient in ingredients:
                ingredient_item_id = ingredient.get("item_id")
                ingredient_amount = int(ingredient["amount"]) * craft_count

                if depth == 0:
                    self.add_major_component(
                        int(ingredient_item_id) if ingredient_item_id is not None else None,
                        ingredient.get("name") or (
                            self.item_name(int(ingredient_item_id))
                            if ingredient_item_id is not None
                            else "Unnamed override ingredient"
                        ),
                        ingredient_amount,
                        "override",
                    )

                if ingredient_item_id is None:
                    ingredient_name = ingredient.get("name") or "Unnamed override ingredient"
                    reason = (
                        f"Recipe override for {item_name} includes '{ingredient_name}' "
                        "by name only. Add item_id before the app can resolve it safely."
                    )
                    self.add_manual_step(item_id, amount, reason, current_path)
                    self.add_warning(reason)
                    continue

                self.resolve_item(
                    int(ingredient_item_id),
                    ingredient_amount,
                    depth + 1,
                    current_path,
                    next_active_item_ids,
                    max_depth,
                )

            return

        reason = override_reason(override)
        self.add_manual_step(item_id, amount, reason, current_path)
        self.add_warning(
            f"{item_name}: recipe override marks this as {override['type']}; "
            "automatic API recipe expansion stopped here."
        )

    def add_warning(self, warning: str) -> None:
        """Add a warning once."""

        if warning not in self.warnings:
            self.warnings.append(warning)

    def format_item_path(self, item_path: list[int]) -> str:
        """Format a recipe path for human-readable warnings."""

        return " -> ".join(self.item_name(item_id) for item_id in item_path)

    def add_raw_material(
        self,
        item_id: int,
        amount: int,
        reason: str,
        item_path: list[int] | None = None,
    ) -> None:
        """Aggregate a terminal raw/material requirement."""

        path_text = self.format_item_path(item_path or [])
        entry = self.raw_materials.setdefault(
            item_id,
            {
                "item_id": item_id,
                "name": self.item_name(item_id),
                "amount": 0,
                "reason": reason,
                "paths": [],
            },
        )
        entry["amount"] += amount

        if path_text and path_text not in entry["paths"]:
            entry["paths"].append(path_text)

    def add_major_component(
        self,
        item_id: int | None,
        name: str,
        amount: int,
        source: str,
    ) -> None:
        """Remember a direct child of the target legendary recipe."""

        key = str(item_id) if item_id is not None else f"name:{normalize_item_name(name)}"
        entry = self.major_components.setdefault(
            key,
            {
                "item_id": item_id,
                "name": name or (f"Item {item_id}" if item_id is not None else "Unnamed component"),
                "amount": 0,
                "source": source,
            },
        )
        entry["amount"] += int(amount)

    def use_owned_intermediate(
        self,
        item_id: int,
        amount: int,
        item_path: list[int],
    ) -> int:
        """Use owned intermediate items before expanding their child ingredients."""

        if not self.use_owned_intermediates or not item_path[:-1]:
            return amount

        available = int(self.account_item_counts.get(item_id, 0)) - int(
            self.owned_items_used.get(item_id, 0)
        )

        if available <= 0:
            return amount

        used_amount = min(int(amount), available)
        remaining_amount = int(amount) - used_amount
        self.owned_items_used[item_id] = self.owned_items_used.get(item_id, 0) + used_amount

        entry = self.satisfied_intermediates.setdefault(
            item_id,
            {
                "item_id": item_id,
                "name": self.item_name(item_id),
                "amount": 0,
                "remaining_amount": 0,
                "paths": [],
            },
        )
        entry["amount"] += used_amount
        entry["remaining_amount"] += remaining_amount

        path_text = self.format_item_path(item_path)
        if path_text and path_text not in entry["paths"]:
            entry["paths"].append(path_text)

        if remaining_amount <= 0:
            self.record_resolution(
                item_id,
                "owned_intermediate_satisfied",
                "Owned intermediate item was used, so this branch was not expanded.",
                owned_amount=used_amount,
            )
        else:
            self.debug_events.append(
                f"Used owned intermediate: {self.item_name(item_id)} "
                f"{used_amount:,}/{amount:,}; expanding remaining {remaining_amount:,}"
            )

        return remaining_amount

    def add_craftable_ingredient(
        self,
        item_id: int,
        amount: int,
        recipe_id: int,
        craft_count: int,
    ) -> None:
        """Aggregate an intermediate ingredient that has a selected recipe."""

        entry = self.craftable_ingredients.setdefault(
            item_id,
            {
                "item_id": item_id,
                "name": self.item_name(item_id),
                "amount": 0,
                "recipe_id": recipe_id,
                "craft_count": 0,
                "recipe_source": "official_api",
                "review_status": "",
                "source_url": "",
            },
        )
        entry["amount"] += amount
        entry["craft_count"] += craft_count

    def apply_wiki_recipe(
        self,
        item_id: int,
        amount: int,
        wiki_summary: dict[str, Any],
        current_path: list[int],
        active_item_ids: set[int],
        depth: int,
        max_depth: int,
    ) -> None:
        """Apply one imported wiki recipe template as an unreviewed fallback."""

        item_name = self.item_name(item_id)
        resolved_ingredients = list(wiki_summary.get("resolved_ingredients", []))
        selection_reason = str(wiki_summary.get("reason", ""))
        remaining_amount = self.use_owned_intermediate(
            item_id,
            amount,
            current_path,
        )

        if remaining_amount <= 0:
            return

        amount = remaining_amount

        self.record_resolution(
            item_id,
            "wiki_recipe",
            selection_reason
            or "Imported GW2 Wiki recipe data was used because no official recipe was found.",
            source_url=wiki_summary.get("source_url", ""),
            review_status=wiki_summary.get("review_status", ""),
            acquisition_option_count=int(wiki_summary.get("acquisition_option_count", 0)),
            multiple_acquisition_options=bool(
                wiki_summary.get("multiple_acquisition_options", False)
            ),
        )
        self.record_wiki_source_usage(
            item_id,
            item_name,
            wiki_summary,
            used_for_recipe=True,
            selection_reason=selection_reason,
        )

        for issue in wiki_summary.get("ingredient_issues", []):
            self.add_warning(f"{item_name}: {issue}")

        if depth > 0:
            self.craftable_ingredients.setdefault(
                item_id,
                {
                    "item_id": item_id,
                    "name": item_name,
                    "amount": 0,
                    "recipe_id": None,
                    "craft_count": 0,
                    "recipe_source": "wiki",
                    "review_status": wiki_summary.get("review_status", ""),
                    "source_url": wiki_summary.get("source_url", ""),
                },
            )
            self.craftable_ingredients[item_id]["amount"] += amount
            self.craftable_ingredients[item_id]["craft_count"] += amount

        craft_count = max(int(amount), 1)

        next_active_item_ids = set(active_item_ids)
        next_active_item_ids.add(item_id)

        for ingredient in resolved_ingredients:
            ingredient_item_id = ingredient.get("item_id")
            ingredient_amount = int(ingredient["amount"]) * craft_count

            if depth == 0:
                self.add_major_component(
                    int(ingredient_item_id) if ingredient_item_id is not None else None,
                    ingredient.get("name") or (
                        self.item_name(int(ingredient_item_id))
                        if ingredient_item_id is not None
                        else "Unnamed wiki ingredient"
                    ),
                    ingredient_amount,
                    "wiki_recipe",
                )

            if ingredient_item_id is None:
                ingredient_name = ingredient.get("name") or "Unnamed wiki ingredient"
                reason = (
                    f"Imported GW2 Wiki recipe data for {item_name} includes "
                    f"'{ingredient_name}' by name only. Add a verified override before "
                    "the app can resolve it safely."
                )
                self.add_manual_step(item_id, amount, reason, current_path)
                self.add_warning(reason)
                continue

            self.resolve_item(
                int(ingredient_item_id),
                ingredient_amount,
                depth + 1,
                current_path,
                next_active_item_ids,
                max_depth,
            )

    def add_expanded_source_step(
        self,
        item_id: int,
        amount: int,
        wiki_summary: dict[str, Any],
        item_path: list[int] | None = None,
    ) -> None:
        """Remember an account-bound/source item that was expanded from wiki data."""

        item_name = self.item_name(item_id)
        path_text = self.format_item_path(item_path or [])
        source_step = self.manual_step_metadata(
            item_id,
            item_name,
            self.source_step_summary_for_item(item_id, item_name),
        )
        ingredient_count = len(wiki_summary.get("resolved_ingredients", []))
        entry = self.expanded_source_steps.setdefault(
            item_id,
            {
                "item_id": item_id,
                "name": item_name,
                "amount": 0,
                "ingredient_count": ingredient_count,
                "source_url": source_step.get("source_url", wiki_summary.get("source_url", "")),
                "review_status": source_step.get(
                    "review_status",
                    wiki_summary.get("review_status", ""),
                ),
                "source_type": source_step.get("source_type", "recipe"),
                "acquisition_option_count": int(
                    source_step.get(
                        "acquisition_option_count",
                        wiki_summary.get("acquisition_option_count", 0),
                    )
                ),
                "source_step_summary": source_step.get("source_step_summary", ""),
                "source_parse_status": source_step.get("source_parse_status", ""),
                "source_parse_reason": source_step.get("source_parse_reason", ""),
                "paths": [],
            },
        )
        entry["amount"] += amount
        entry["ingredient_count"] = max(int(entry["ingredient_count"]), ingredient_count)

        if path_text and path_text not in entry["paths"]:
            entry["paths"].append(path_text)

    def apply_source_step_recipe(
        self,
        item_id: int,
        amount: int,
        wiki_summary: dict[str, Any],
        current_path: list[int],
        active_item_ids: set[int],
        depth: int,
        max_depth: int,
    ) -> None:
        """Expand one manual/source step that has one clear parsed wiki recipe."""

        item_name = self.item_name(item_id)
        resolved_ingredients = list(wiki_summary.get("resolved_ingredients", []))
        ingredient_count = len(resolved_ingredients)
        selection_reason = str(wiki_summary.get("reason", ""))
        remaining_amount = self.use_owned_intermediate(
            item_id,
            amount,
            current_path,
        )

        if remaining_amount <= 0:
            return

        amount = remaining_amount

        self.add_expanded_source_step(item_id, amount, wiki_summary, current_path)
        self.record_resolution(
            item_id,
            "source_step_recipe",
            selection_reason
            or "A clear source-step wiki recipe was expanded recursively.",
            source_url=wiki_summary.get("source_url", ""),
            review_status=wiki_summary.get("review_status", ""),
            acquisition_option_count=int(wiki_summary.get("acquisition_option_count", 0)),
            ingredient_count=ingredient_count,
        )
        self.record_wiki_source_usage(
            item_id,
            item_name,
            wiki_summary,
            used_for_recipe=True,
            selection_reason=selection_reason,
        )

        debug_event = (
            f"Expanded source-step recipe: {item_name} -> {ingredient_count:,} ingredients"
        )
        self.debug_events.append(debug_event)

        for issue in wiki_summary.get("ingredient_issues", []):
            self.add_warning(f"{item_name}: {issue}")

        if depth > 0:
            self.craftable_ingredients.setdefault(
                item_id,
                {
                    "item_id": item_id,
                    "name": item_name,
                    "amount": 0,
                    "recipe_id": None,
                    "craft_count": 0,
                    "recipe_source": "source_step_wiki",
                    "review_status": wiki_summary.get("review_status", ""),
                    "source_url": wiki_summary.get("source_url", ""),
                },
            )
            self.craftable_ingredients[item_id]["amount"] += amount
            self.craftable_ingredients[item_id]["craft_count"] += amount

        craft_count = max(int(amount), 1)
        next_active_item_ids = set(active_item_ids)
        next_active_item_ids.add(item_id)

        for ingredient in resolved_ingredients:
            ingredient_item_id = ingredient.get("item_id")
            ingredient_amount = int(ingredient["amount"]) * craft_count

            if depth == 0:
                self.add_major_component(
                    int(ingredient_item_id) if ingredient_item_id is not None else None,
                    ingredient.get("name") or (
                        self.item_name(int(ingredient_item_id))
                        if ingredient_item_id is not None
                        else "Unnamed source-step ingredient"
                    ),
                    ingredient_amount,
                    "source_step_recipe",
                )

            if ingredient_item_id is None:
                ingredient_name = ingredient.get("name") or "Unnamed source-step ingredient"
                reason = (
                    f"Source-step wiki recipe data for {item_name} includes "
                    f"'{ingredient_name}' by name only. Add a verified override before "
                    "the app can resolve it safely."
                )
                self.add_manual_step(item_id, amount, reason, current_path)
                self.add_warning(reason)
                continue

            self.resolve_item(
                int(ingredient_item_id),
                ingredient_amount,
                depth + 1,
                current_path,
                next_active_item_ids,
                max_depth,
            )

    def add_manual_step(
        self,
        item_id: int,
        amount: int,
        reason: str,
        item_path: list[int],
        source_step: dict[str, Any] | None = None,
    ) -> None:
        """Aggregate unknown or manually tracked recipe gaps."""

        key = f"{item_id}:{reason}"
        path_text = self.format_item_path(item_path)
        item_name = self.item_name(item_id)
        metadata = self.manual_step_metadata(item_id, item_name, source_step)
        entry = self.manual_steps.setdefault(
            key,
            {
                "item_id": item_id,
                "name": item_name,
                "amount": 0,
                "reason": reason,
                "paths": [],
                "source_url": metadata.get("source_url", ""),
                "review_status": metadata.get("review_status", "not_imported"),
                "source_type": metadata.get("source_type", "unknown"),
                "source_step_summary": metadata.get(
                    "source_step_summary",
                    metadata.get("source_summary", ""),
                ),
                "source_summary": metadata.get(
                    "source_summary",
                    metadata.get("source_step_summary", ""),
                ),
                "source_parse_status": metadata.get("source_parse_status", ""),
                "source_parse_reason": metadata.get("source_parse_reason", ""),
                "parsed_recipe_ingredients": list(
                    metadata.get("parsed_recipe_ingredients", [])
                ),
                "parsed_recipe_count": int(metadata.get("parsed_recipe_count", 0)),
                "acquisition_option_count": int(
                    metadata.get("acquisition_option_count", 0)
                ),
                "wiki_page_found": bool(metadata.get("wiki_page_found", False)),
                "raw_wiki_page_title": metadata.get("raw_wiki_page_title", ""),
            },
        )
        entry["amount"] += amount

        for field_name in (
            "source_url",
            "review_status",
            "source_type",
            "source_step_summary",
            "source_summary",
            "source_parse_status",
            "source_parse_reason",
            "wiki_page_found",
            "raw_wiki_page_title",
        ):
            if not entry.get(field_name) and metadata.get(field_name):
                entry[field_name] = metadata[field_name]

        if not entry.get("parsed_recipe_ingredients") and metadata.get(
            "parsed_recipe_ingredients"
        ):
            entry["parsed_recipe_ingredients"] = list(
                metadata["parsed_recipe_ingredients"]
            )

        entry["parsed_recipe_count"] = max(
            int(entry.get("parsed_recipe_count", 0)),
            int(metadata.get("parsed_recipe_count", 0)),
        )

        entry["acquisition_option_count"] = max(
            int(entry.get("acquisition_option_count", 0)),
            int(metadata.get("acquisition_option_count", 0)),
        )

        if path_text and path_text not in entry["paths"]:
            entry["paths"].append(path_text)

    def add_recipe_used(
        self,
        recipe: dict[str, Any],
        output_item_id: int,
        craft_count: int,
    ) -> None:
        """Aggregate recipe usage in the resolved tree."""

        recipe_id = int(recipe["id"])
        output_count = max(int(recipe.get("output_item_count", 1)), 1)
        entry = self.recipes_used.setdefault(
            recipe_id,
            {
                "recipe_id": recipe_id,
                "output_item_id": output_item_id,
                "output_item_name": self.item_name(output_item_id),
                "output_item_count": output_count,
                "craft_count": 0,
            },
        )
        entry["craft_count"] += craft_count

    def select_recipe_id(self, item_id: int, recipe_ids: list[int]) -> int | None:
        """Choose the only recipe or a curated preferred recipe."""

        if len(recipe_ids) == 1:
            return recipe_ids[0]

        preferred_recipe_id = self.preferred_recipes_by_output.get(item_id)

        if preferred_recipe_id in recipe_ids:
            return preferred_recipe_id

        item_name = self.item_name(item_id)
        recipe_text = ", ".join(str(recipe_id) for recipe_id in recipe_ids)

        if preferred_recipe_id is None:
            warning = (
                f"{item_name} has multiple official recipes ({recipe_text}). "
                "Add a preferred recipe in data/recipe_overrides.json before the "
                "app resolves this branch."
            )
        else:
            warning = (
                f"{item_name} is configured to prefer recipe {preferred_recipe_id}, "
                f"but the official recipes are {recipe_text}."
            )

        self.add_warning(warning)
        return None

    def handle_no_recipe(
        self,
        item_id: int,
        amount: int,
        depth: int,
        item_path: list[int],
    ) -> None:
        """Safely stop when the API has no recipe for an item."""

        item_name = self.item_name(item_id)

        if depth == 0:
            reason = (
                "The official API has no recipe that outputs this target. "
                "Add curated materials or checklist steps instead of guessing."
            )
            self.add_manual_step(item_id, amount, reason, item_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "manual_recipe_gap", reason)
            return

        try:
            item = self.items_by_id([item_id]).get(item_id, {})
        except RecipeApiError as error:
            reason = (
                "No official recipe was found, and item details could not be "
                f"checked ({error}). Review this item manually."
            )
            self.add_manual_step(item_id, amount, reason, item_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "manual_recipe_gap", reason)
            return

        if item_has_account_bound_flags(item):
            reason = (
                "The official API lists this item as account-bound or soulbound "
                "and no recipe was found. Track this requirement manually."
            )
            if depth > 0:
                self.add_raw_material(
                    item_id,
                    amount,
                    "Account-bound item with no safe recipe expansion; count current "
                    "account storage toward this requirement.",
                    item_path,
                )
            self.add_manual_step(item_id, amount, reason, item_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "account_bound_manual_stop", reason)
            return

        self.add_raw_material(
            item_id,
            amount,
            "No official recipe found; treat as a terminal material.",
            item_path,
        )
        self.record_resolution(
            item_id,
            "terminal_material",
            "No official recipe or usable wiki recipe was found, so this item is treated "
            "as a terminal material.",
        )

    def resolve_item(
        self,
        item_id: int,
        amount: int,
        depth: int,
        item_path: list[int],
        active_item_ids: set[int],
        max_depth: int,
    ) -> None:
        """Resolve one item and recurse into its ingredients when safe."""

        current_path = item_path + [item_id]
        item_name = self.item_name(item_id)

        if item_id in active_item_ids:
            reason = (
                "A recipe cycle was detected, so automatic recursion stopped "
                "before looping forever."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason} Path: {self.format_item_path(current_path)}")
            self.record_resolution(item_id, "manual_cycle_stop", reason)
            return

        if depth >= max_depth:
            reason = (
                f"The recipe tree reached the max depth of {max_depth}. "
                "Review this branch manually."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "manual_max_depth_stop", reason)
            return

        override_decision = self.override_decision_for_item(item_id, item_name)
        override = override_decision.get("override")

        if override_decision["status"] == "verified_ingredients":
            self.record_resolution(
                item_id,
                "override_verified_ingredients",
                override_decision["reason"],
                override_type=override_decision.get("override_type", ""),
            )
            self.apply_override(
                item_id,
                amount,
                override,
                current_path,
                active_item_ids,
                depth,
                max_depth,
            )
            return

        if override_decision["status"] == "manual_stop":
            wiki_summary = self.wiki_recipe_summary_for_item(item_id, item_name)
            override_verified = bool((override or {}).get("verified", False))

            if not override_verified and wiki_summary.get("selected_recipe") is not None:
                self.apply_source_step_recipe(
                    item_id,
                    amount,
                    wiki_summary,
                    current_path,
                    active_item_ids,
                    depth,
                    max_depth,
                )
                return

            self.record_resolution(
                item_id,
                "override_manual_stop",
                override_decision["reason"],
                override_type=override_decision.get("override_type", ""),
            )
            self.apply_override(
                item_id,
                amount,
                override,
                current_path,
                active_item_ids,
                depth,
                max_depth,
            )
            return

        try:
            item = self.items_by_id([item_id]).get(item_id, {})
        except RecipeApiError:
            item = {}

        if item and item_has_account_bound_flags(item):
            wiki_summary = self.wiki_recipe_summary_for_item(item_id, item_name)

            if wiki_summary.get("selected_recipe") is not None:
                self.apply_source_step_recipe(
                    item_id,
                    amount,
                    wiki_summary,
                    current_path,
                    active_item_ids,
                    depth,
                    max_depth,
                )
                return

            reason = (
                "The official API lists this item as account-bound or soulbound, so "
                "automatic material expansion stops here."
            )

            if wiki_summary["row_count"] > 0:
                source_url = wiki_summary.get("source_url", "")
                review_status = wiki_summary.get("review_status", "")
                option_count = int(wiki_summary.get("acquisition_option_count", 0))
                reason += (
                    " Use the imported GW2 Wiki source data as a manual acquisition "
                    f"step. Review status: {review_status or 'n/a'}. "
                    f"Acquisition options found: {option_count}."
                )

                if source_url:
                    reason += f" Source URL: {source_url}"

                self.record_wiki_source_usage(
                    item_id,
                    item_name,
                    wiki_summary,
                    used_for_recipe=False,
                    selection_reason="Account-bound item kept as a manual source step.",
                )
            else:
                reason += (
                    " Add a verified override or imported acquisition notes so this can "
                    "be shown as a useful manual source step."
                )

            if depth > 0:
                self.add_raw_material(
                    item_id,
                    amount,
                    "Account-bound item with no safe recipe expansion; count current "
                    "account storage toward this requirement.",
                    current_path,
                )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.record_resolution(
                item_id,
                "account_bound_manual_source_step",
                reason,
                source_url=wiki_summary.get("source_url", ""),
                review_status=wiki_summary.get("review_status", ""),
                acquisition_option_count=int(
                    wiki_summary.get("acquisition_option_count", 0)
                ),
            )
            return

        official_lookup = self.official_recipe_lookup_summary(item_id)

        if official_lookup["status"] in {"lookup_failed", "no_recipe"}:
            wiki_summary = self.wiki_recipe_summary_for_item(item_id, item_name)

            if wiki_summary.get("selected_recipe") is not None:
                self.apply_wiki_recipe(
                    item_id,
                    amount,
                    wiki_summary,
                    current_path,
                    active_item_ids,
                    depth,
                    max_depth,
                )
                return

            if wiki_summary["row_count"] > 0:
                reason = (
                    f"{official_lookup['reason']} Imported GW2 Wiki data needs review "
                    f"before this branch can be expanded automatically. {wiki_summary['reason']}"
                )
                self.add_manual_step(item_id, amount, reason, current_path)
                self.add_warning(f"{item_name}: {reason}")
                self.record_resolution(
                    item_id,
                    "wiki_manual_gap",
                    reason,
                    source_url=wiki_summary.get("source_url", ""),
                    review_status=wiki_summary.get("review_status", ""),
                    acquisition_option_count=int(
                        wiki_summary.get("acquisition_option_count", 0)
                    ),
                    multiple_acquisition_options=bool(
                        wiki_summary.get("multiple_acquisition_options", False)
                    ),
                )
                self.record_wiki_source_usage(
                    item_id,
                    item_name,
                    wiki_summary,
                    used_for_recipe=False,
                    selection_reason=wiki_summary["reason"],
                )
                return

            if official_lookup["status"] == "lookup_failed":
                reason = (
                    "The official recipe lookup failed, and no imported GW2 Wiki recipe "
                    f"data was available. Review this item manually. Details: "
                    f"{official_lookup['error']}"
                )
                self.add_manual_step(item_id, amount, reason, current_path)
                self.add_warning(f"{item_name}: {reason}")
                self.record_resolution(item_id, "manual_recipe_gap", reason)
                return

            self.handle_no_recipe(item_id, amount, depth, current_path)
            return

        recipe_ids = list(official_lookup["recipe_ids"])

        recipe_id = self.select_recipe_id(item_id, recipe_ids)

        if recipe_id is None:
            reason = (
                "Multiple official recipes exist and no valid preferred recipe "
                "is configured."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.record_resolution(item_id, "manual_preferred_recipe_needed", reason)
            return

        try:
            recipe = self.recipes_by_id([recipe_id]).get(recipe_id)
        except RecipeApiError as error:
            reason = (
                f"Recipe {recipe_id} could not be fetched from the official API: "
                f"{error}"
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "manual_recipe_gap", reason)
            return

        if not recipe:
            reason = (
                f"Recipe {recipe_id} was selected but no recipe details were "
                "returned by the official API."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "manual_recipe_gap", reason)
            return

        remaining_amount = self.use_owned_intermediate(
            item_id,
            amount,
            current_path,
        )

        if remaining_amount <= 0:
            return

        amount = remaining_amount
        output_count = max(int(recipe.get("output_item_count", 1)), 1)
        craft_count = math.ceil(amount / output_count)
        self.record_resolution(
            item_id,
            "official_api",
            f"Official recipe {recipe_id} was selected.",
            recipe_id=recipe_id,
            recipe_ids=list(recipe_ids),
        )
        self.add_recipe_used(recipe, item_id, craft_count)

        if depth > 0:
            self.add_craftable_ingredient(item_id, amount, recipe_id, craft_count)

        ingredients = recipe.get("ingredients", [])

        if not isinstance(ingredients, list):
            reason = f"Recipe {recipe_id} has no readable ingredient list."
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            self.record_resolution(item_id, "manual_recipe_gap", reason)
            return

        next_active_item_ids = set(active_item_ids)
        next_active_item_ids.add(item_id)

        for ingredient in ingredients:
            if not isinstance(ingredient, dict):
                continue

            ingredient_item_id = ingredient.get("item_id")
            ingredient_count = ingredient.get("count")

            if ingredient_item_id is None or ingredient_count is None:
                reason = (
                    f"Recipe {recipe_id} contains an ingredient that is not a "
                    "normal item requirement."
                )
                self.add_manual_step(item_id, amount, reason, current_path)
                self.add_warning(f"{item_name}: {reason}")
                continue

            if depth == 0:
                self.add_major_component(
                    int(ingredient_item_id),
                    self.item_name(int(ingredient_item_id)),
                    int(ingredient_count) * craft_count,
                    "official_api",
                )

            self.resolve_item(
                int(ingredient_item_id),
                int(ingredient_count) * craft_count,
                depth + 1,
                current_path,
                next_active_item_ids,
                max_depth,
            )

    def resolve_recipe_tree(
        self,
        final_item_id: int,
        amount: int = 1,
        max_depth: int | None = None,
        item_counts: dict[int, int] | None = None,
        use_owned_intermediates: bool = True,
    ) -> dict[str, Any]:
        """Resolve a full recipe tree for one output item.

        The returned structure separates official craftable branches from
        terminal raw materials and manual gaps. It never guesses when the API is
        ambiguous or incomplete.
        """

        self.raw_materials = {}
        self.craftable_ingredients = {}
        self.major_components = {}
        self.manual_steps = {}
        self.expanded_source_steps = {}
        self.satisfied_intermediates = {}
        self.owned_items_used = {}
        self.account_item_counts = {
            int(item_id): int(count)
            for item_id, count in (item_counts or {}).items()
        }
        self.use_owned_intermediates = bool(use_owned_intermediates)
        self.recipes_used = {}
        self.resolution_log = {}
        self.wiki_sources_used = {}
        self.warnings = []
        self.debug_events = []

        normalized_final_item_id = int(final_item_id)
        normalized_amount = int(amount)
        resolved_max_depth = self.max_depth if max_depth is None else int(max_depth)

        self.resolve_item(
            normalized_final_item_id,
            normalized_amount,
            depth=0,
            item_path=[],
            active_item_ids=set(),
            max_depth=resolved_max_depth,
        )

        return {
            "final_item_id": normalized_final_item_id,
            "final_item_name": self.item_name(normalized_final_item_id),
            "amount": normalized_amount,
            "max_depth": resolved_max_depth,
            "craftable_ingredients": sorted(
                self.craftable_ingredients.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "major_components": sorted(
                self.major_components.values(),
                key=lambda entry: (entry["name"], entry.get("item_id") or 0),
            ),
            "raw_material_requirements": sorted(
                self.raw_materials.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "unknown_manual_steps": sorted(
                self.manual_steps.values(),
                key=lambda entry: (entry["name"], entry["item_id"], entry["reason"]),
            ),
            "expanded_source_steps": sorted(
                self.expanded_source_steps.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "satisfied_intermediates": sorted(
                self.satisfied_intermediates.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "recipes_used": sorted(
                self.recipes_used.values(),
                key=lambda entry: (entry["output_item_name"], entry["recipe_id"]),
            ),
            "final_source": dict(self.resolution_log.get(normalized_final_item_id, {})),
            "resolution_sources": sorted(
                self.resolution_log.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "wiki_sources_used": sorted(
                self.wiki_sources_used.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "warnings": list(self.warnings),
            "debug_events": list(self.debug_events),
        }

    def debug_recipe_source(
        self,
        item_id: int,
        item_name: str | None = None,
    ) -> dict[str, Any]:
        """Return a detailed recipe-source decision summary for one item."""

        resolved_name = item_name or self.item_name(item_id)
        override_summary = self.override_decision_for_item(item_id, resolved_name)
        official_summary = self.official_recipe_lookup_summary(item_id)
        wiki_summary = self.wiki_recipe_summary_for_item(item_id, resolved_name)
        recipe_tree = self.resolve_recipe_tree(item_id)

        return {
            "final_item_id": int(item_id),
            "final_item_name": recipe_tree.get("final_item_name", resolved_name),
            "override_lookup": override_summary,
            "official_recipe_lookup": official_summary,
            "wiki_recipe_rows": wiki_summary.get("rows", []),
            "wiki_recipe_row_count": int(wiki_summary.get("row_count", 0)),
            "acquisition_options": wiki_summary.get("acquisition_options", []),
            "acquisition_option_count": int(wiki_summary.get("acquisition_option_count", 0)),
            "wiki_summary": wiki_summary,
            "chosen_source": recipe_tree.get("final_source", {}),
        }


def resolve_recipe_tree(
    final_item_id: int,
    cache_path: Path = DEFAULT_RECIPE_CACHE_PATH,
    overrides_path: Path = DEFAULT_RECIPE_OVERRIDES_PATH,
    amount: int = 1,
    max_depth: int = DEFAULT_MAX_RECIPE_DEPTH,
    item_counts: dict[int, int] | None = None,
    use_owned_intermediates: bool = True,
) -> dict[str, Any]:
    """Convenience wrapper for resolving one recipe tree."""

    engine = RecipeEngine(
        cache_path=cache_path,
        overrides_path=overrides_path,
        max_depth=max_depth,
    )
    return engine.resolve_recipe_tree(
        final_item_id,
        amount=amount,
        max_depth=max_depth,
        item_counts=item_counts,
        use_owned_intermediates=use_owned_intermediates,
    )
