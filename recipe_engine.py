"""Resolve Guild Wars 2 crafting recipe trees from official API data.

The recipe engine never reads or sends the user's private account token. Normal
runs prefer the local public reference database, while curated override data
stays separate so the app can be clear about what came from ArenaNet and what
still needs human judgment.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from reference_database import DEFAULT_REFERENCE_DB_PATH, ReferenceDatabase


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
        self.manual_steps: dict[str, dict[str, Any]] = {}
        self.recipes_used: dict[int, dict[str, Any]] = {}
        self.warnings: list[str] = []

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
            output_count = max(int(override.get("output_count", 1)), 1)
            craft_count = math.ceil(amount / output_count)

            next_active_item_ids = set(active_item_ids)
            next_active_item_ids.add(item_id)

            for ingredient in ingredients:
                ingredient_item_id = ingredient.get("item_id")
                ingredient_amount = int(ingredient["amount"]) * craft_count

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
    ) -> None:
        """Aggregate a terminal raw/material requirement."""

        entry = self.raw_materials.setdefault(
            item_id,
            {
                "item_id": item_id,
                "name": self.item_name(item_id),
                "amount": 0,
                "reason": reason,
            },
        )
        entry["amount"] += amount

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
            },
        )
        entry["amount"] += amount
        entry["craft_count"] += craft_count

    def add_manual_step(
        self,
        item_id: int,
        amount: int,
        reason: str,
        item_path: list[int],
    ) -> None:
        """Aggregate unknown or manually tracked recipe gaps."""

        key = f"{item_id}:{reason}"
        path_text = self.format_item_path(item_path)
        entry = self.manual_steps.setdefault(
            key,
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
            return

        if item_has_account_bound_flags(item):
            reason = (
                "The official API lists this item as account-bound or soulbound "
                "and no recipe was found. Track this requirement manually."
            )
            self.add_manual_step(item_id, amount, reason, item_path)
            self.add_warning(f"{item_name}: {reason}")
            return

        self.add_raw_material(
            item_id,
            amount,
            "No official recipe found; treat as a terminal material.",
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
            return

        if depth >= max_depth:
            reason = (
                f"The recipe tree reached the max depth of {max_depth}. "
                "Review this branch manually."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            return

        override = self.override_for_item(item_id, item_name)

        if override:
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
            reason = (
                "The official API lists this item as account-bound or soulbound. "
                "Track this requirement manually instead of expanding it as "
                "normal crafting materials."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            return

        try:
            recipe_ids = self.recipe_ids_for_output(item_id)
        except RecipeApiError as error:
            reason = (
                "The official recipe lookup failed, so this branch needs manual "
                f"review. Details: {error}"
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            return

        if not recipe_ids:
            self.handle_no_recipe(item_id, amount, depth, current_path)
            return

        recipe_id = self.select_recipe_id(item_id, recipe_ids)

        if recipe_id is None:
            reason = (
                "Multiple official recipes exist and no valid preferred recipe "
                "is configured."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
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
            return

        if not recipe:
            reason = (
                f"Recipe {recipe_id} was selected but no recipe details were "
                "returned by the official API."
            )
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
            return

        output_count = max(int(recipe.get("output_item_count", 1)), 1)
        craft_count = math.ceil(amount / output_count)
        self.add_recipe_used(recipe, item_id, craft_count)

        if depth > 0:
            self.add_craftable_ingredient(item_id, amount, recipe_id, craft_count)

        ingredients = recipe.get("ingredients", [])

        if not isinstance(ingredients, list):
            reason = f"Recipe {recipe_id} has no readable ingredient list."
            self.add_manual_step(item_id, amount, reason, current_path)
            self.add_warning(f"{item_name}: {reason}")
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
    ) -> dict[str, Any]:
        """Resolve a full recipe tree for one output item.

        The returned structure separates official craftable branches from
        terminal raw materials and manual gaps. It never guesses when the API is
        ambiguous or incomplete.
        """

        self.raw_materials = {}
        self.craftable_ingredients = {}
        self.manual_steps = {}
        self.recipes_used = {}
        self.warnings = []

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
            "raw_material_requirements": sorted(
                self.raw_materials.values(),
                key=lambda entry: (entry["name"], entry["item_id"]),
            ),
            "unknown_manual_steps": sorted(
                self.manual_steps.values(),
                key=lambda entry: (entry["name"], entry["item_id"], entry["reason"]),
            ),
            "recipes_used": sorted(
                self.recipes_used.values(),
                key=lambda entry: (entry["output_item_name"], entry["recipe_id"]),
            ),
            "warnings": list(self.warnings),
        }


def resolve_recipe_tree(
    final_item_id: int,
    cache_path: Path = DEFAULT_RECIPE_CACHE_PATH,
    overrides_path: Path = DEFAULT_RECIPE_OVERRIDES_PATH,
    amount: int = 1,
    max_depth: int = DEFAULT_MAX_RECIPE_DEPTH,
) -> dict[str, Any]:
    """Convenience wrapper for resolving one recipe tree."""

    engine = RecipeEngine(
        cache_path=cache_path,
        overrides_path=overrides_path,
        max_depth=max_depth,
    )
    return engine.resolve_recipe_tree(final_item_id, amount=amount, max_depth=max_depth)
