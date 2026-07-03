"""SQLite reference database for public Guild Wars 2 planner data.

This database is for public, account-independent data only. It does not read or
store API keys, wallet contents, character inventories, or other account data.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.guildwars2.com/v2"
WIKI_API_URL = "https://wiki.guildwars2.com/api.php"
WIKI_BASE_URL = "https://wiki.guildwars2.com/wiki"
DEFAULT_REFERENCE_DB_PATH = Path("data") / "planner_reference.sqlite"
DEFAULT_RECIPE_OVERRIDES_PATH = Path("data") / "recipe_overrides.json"
API_BATCH_SIZE = 200
API_TIMEOUT_SECONDS = 60
RETRYABLE_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
API_RETRY_DELAYS_SECONDS = (2, 5, 10)
SCHEMA_VERSION = 1


class ReferenceApiError(Exception):
    """Raised when a public GW2 API import cannot complete."""


class WikiImportError(Exception):
    """Raised when a targeted wiki import cannot complete."""


def normalize_item_name(item_name: str) -> str:
    """Make item names easier to compare by ignoring case and extra spaces."""

    return " ".join(item_name.casefold().split())


def normalize_wiki_item_text(wiki_text: str) -> dict[str, Any]:
    """Strip common wiki markup and return item-name resolution candidates."""

    original_text = str(wiki_text).strip()
    text = original_text.replace("\xa0", " ")
    page_title = ""
    display_text = ""

    def replace_link(match: re.Match[str]) -> str:
        nonlocal page_title, display_text
        link_page = match.group("page").strip()
        link_display = (match.group("display") or link_page).strip()

        if not page_title:
            page_title = link_page.split("#", 1)[0].strip()

        if not display_text:
            display_text = link_display

        return link_display

    text = re.sub(
        r"\[\[(?P<page>[^\]|]+)(?:\|(?P<display>[^\]]+))?\]\]",
        replace_link,
        text,
    )

    if not page_title and "|" in text and "{{" not in text and "}}" not in text:
        left, _separator, right = text.partition("|")
        page_title = left.strip()
        display_text = right.strip()
        text = display_text or page_title

    def replace_template(match: re.Match[str]) -> str:
        parts = [part.strip() for part in match.group(1).split("|")]

        if len(parts) >= 3:
            return parts[2]

        if len(parts) >= 2:
            return parts[1]

        return ""

    text = re.sub(r"\{\{([^{}]+)\}\}", replace_template, text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("'''", "").replace("''", "")
    text = text.replace("[[", "").replace("]]", "")
    text = " ".join(text.split()).strip()

    normalized_name = display_text or text or page_title
    candidates: list[str] = []

    for candidate in (normalized_name, page_title):
        candidate = " ".join(candidate.split()).strip()

        if candidate and candidate not in candidates:
            candidates.append(candidate)

    return {
        "original_text": original_text,
        "normalized_name": normalized_name,
        "page_title": page_title,
        "display_text": display_text,
        "stripped_text": text,
        "candidates": candidates,
    }


def chunked(values: list[int], size: int) -> list[list[int]]:
    """Split a list into smaller chunks for API calls."""

    return [values[index : index + size] for index in range(0, len(values), size)]


def public_api_get(
    path: str,
    params: dict[str, Any] | None = None,
    timeout_seconds: int = API_TIMEOUT_SECONDS,
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
    last_error: Exception | None = None

    for attempt_number in range(1, total_attempts + 1):
        request = Request(url, headers=headers)

        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            last_error = ReferenceApiError(
                f"GW2 API returned HTTP {error.code} for {path}. {body}".strip()
            )

            if error.code not in RETRYABLE_HTTP_STATUS_CODES:
                raise last_error from error
        except (TimeoutError, URLError) as error:
            last_error = error

        if attempt_number < total_attempts:
            time.sleep(API_RETRY_DELAYS_SECONDS[attempt_number - 1])

    raise ReferenceApiError(
        f"The GW2 API did not respond successfully for {path}. Last error: {last_error}"
    )


def wiki_api_get(
    params: dict[str, Any],
    timeout_seconds: int = API_TIMEOUT_SECONDS,
) -> Any:
    """Call the public Guild Wars 2 Wiki API and return decoded JSON."""

    url = f"{WIKI_API_URL}?{urlencode(params)}"
    headers = {
        "Accept": "application/json",
        "User-Agent": "gw2-legendary-planner/1.0",
    }
    request = Request(url, headers=headers)

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, TimeoutError, URLError) as error:
        raise WikiImportError(f"Could not import wiki page data: {error}") from error


def wiki_page_url(title: str) -> str:
    """Return a human-readable wiki URL for a page title."""

    return f"{WIKI_BASE_URL}/{title.replace(' ', '_')}"


def extract_wiki_text(page: dict[str, Any]) -> str:
    """Extract wikitext from a MediaWiki API page row."""

    revisions = page.get("revisions", [])

    if not revisions:
        return ""

    revision = revisions[0]
    slots = revision.get("slots")

    if isinstance(slots, dict):
        main_slot = slots.get("main", {})
        return str(main_slot.get("content", ""))

    return str(revision.get("content", revision.get("*", "")))


def acquisition_type_from_heading(heading: str) -> str | None:
    """Map a wiki heading to a broad acquisition option type."""

    normalized_heading = normalize_item_name(heading)
    mapping = {
        "acquisition": "acquisition",
        "recipe": "recipe",
        "recipes": "recipe",
        "mystic forge": "mystic_forge",
        "sold by": "vendor",
        "vendor": "vendor",
        "contained in": "container",
        "rewarded by": "reward",
        "achievements": "achievement",
        "collection": "collection",
        "collections": "collection",
    }

    return mapping.get(normalized_heading)


def normal_report_source_type(acquisition_type: str | None) -> str:
    """Map imported wiki option types to the small set shown in normal reports."""

    normalized_type = str(acquisition_type or "").casefold()

    if normalized_type in {"recipe", "vendor", "achievement", "collection", "mystic_forge"}:
        return normalized_type

    if normalized_type in {"reward", "container", "acquisition"}:
        return normalized_type

    return "unknown"


def extract_acquisition_options_from_wikitext(wikitext: str) -> list[dict[str, Any]]:
    """Extract lightweight acquisition snippets from common wiki sections."""

    options: list[dict[str, Any]] = []
    current_type: str | None = None
    current_heading = ""

    for raw_line in wikitext.splitlines():
        line = raw_line.strip()

        if line.startswith("==") and line.endswith("=="):
            heading = line.strip("=").strip()
            current_type = acquisition_type_from_heading(heading)
            current_heading = heading
            continue

        if not current_type:
            continue

        if line.startswith("{{Recipe"):
            continue

        if line.startswith("*") or line.startswith("#"):
            normalized_line = normalize_wiki_item_text(line)

            if normalized_line["stripped_text"].startswith("{{"):
                continue

            options.append(
                {
                    "acquisition_type": current_type,
                    "heading": current_heading,
                    "text": line,
                    "normalized_text": normalized_line["stripped_text"],
                }
            )

    return options


def extract_template_blocks(wikitext: str, template_name: str) -> list[str]:
    """Extract top-level wiki template blocks by name."""

    blocks: list[str] = []
    current_block: list[str] = []
    inside_template = False
    brace_balance = 0
    template_start = "{{" + template_name.casefold()

    for raw_line in wikitext.splitlines():
        line = raw_line.strip()
        lower_line = line.casefold()
        next_character = lower_line[len(template_start) : len(template_start) + 1]
        is_requested_template = (
            lower_line.startswith(template_start)
            and next_character in {"", "|", "}"}
        )

        if not inside_template and is_requested_template:
            inside_template = True
            current_block = [line]
            brace_balance = line.count("{{") - line.count("}}")

            if brace_balance <= 0:
                blocks.append("\n".join(current_block))
                inside_template = False

            continue

        if not inside_template:
            continue

        current_block.append(line)
        brace_balance += line.count("{{") - line.count("}}")

        if brace_balance <= 0:
            blocks.append("\n".join(current_block))
            inside_template = False

    return blocks


def parse_template_fields(template_block: str) -> dict[str, str]:
    """Parse simple | key = value lines from one wiki template block."""

    fields: dict[str, str] = {}

    for raw_line in template_block.splitlines():
        line = raw_line.strip()

        if not line.startswith("|"):
            continue

        key, separator, value = line[1:].partition("=")

        if not separator:
            continue

        fields[key.strip().casefold()] = value.strip()

    return fields


def parse_source_step_ingredient(ingredient_text: str) -> dict[str, Any] | None:
    """Parse a simple wiki recipe ingredient like '250 Glob of Ectoplasm'."""

    original_text = str(ingredient_text).strip()
    clean_text = " ".join(original_text.replace("\xa0", " ").split())

    if not clean_text:
        return None

    match = re.match(r"^(?P<amount>\d[\d,]*)\s+(?P<name>.+)$", clean_text)

    if not match:
        return None

    name_details = normalize_wiki_item_text(match.group("name").strip())
    item_name = name_details["normalized_name"] or name_details["stripped_text"]

    if not item_name or item_name.startswith("{{"):
        return None

    return {
        "amount": int(match.group("amount").replace(",", "")),
        "name": item_name,
        "original_text": original_text,
    }


def parse_source_step_recipe_blocks(wikitext: str) -> list[dict[str, Any]]:
    """Parse wiki {{Recipe}} blocks into source-step ingredient summaries."""

    parsed_recipes: list[dict[str, Any]] = []

    for block in extract_template_blocks(wikitext, "Recipe"):
        fields = parse_template_fields(block)
        ingredients: list[dict[str, Any]] = []

        for key in sorted(fields):
            if not re.fullmatch(r"ingredient\d+", key):
                continue

            ingredient = parse_source_step_ingredient(fields[key])

            if ingredient:
                ingredients.append(ingredient)

        parsed_recipes.append(
            {
                "source_hint": fields.get("source", "").strip(),
                "ingredients": ingredients,
                "ingredient_count": len(ingredients),
                "raw_template": block,
            }
        )

    return parsed_recipes


def summarize_recipe_ingredients(ingredients: list[dict[str, Any]]) -> str:
    """Return a compact ingredient summary suitable for terminal output."""

    if not ingredients:
        return ""

    parts = [
        f"{ingredient['amount']:,} {ingredient['name']}"
        for ingredient in ingredients[:8]
    ]

    if len(ingredients) > 8:
        parts.append(f"and {len(ingredients) - 8:,} more")

    return "Requires: " + ", ".join(parts)


def source_step_data_from_wikitext(
    wikitext: str,
    options: list[dict[str, Any]],
    missing: bool,
) -> dict[str, Any]:
    """Build normal-report source-step fields from one wiki page."""

    if missing:
        return {
            "source_type": "unknown",
            "source_step_summary": "Wiki page was not found; manual review needed.",
            "parse_status": "page_missing",
            "parse_reason": "The wiki API reported this page as missing.",
            "parsed_recipe_ingredients": [],
            "parsed_recipe_count": 0,
        }

    parsed_recipes = parse_source_step_recipe_blocks(wikitext)

    if len(parsed_recipes) == 1 and parsed_recipes[0]["ingredients"]:
        source_hint = normalize_item_name(parsed_recipes[0].get("source_hint", ""))
        source_type = "mystic_forge" if "mystic forge" in source_hint else "recipe"
        return {
            "source_type": source_type,
            "source_step_summary": summarize_recipe_ingredients(
                parsed_recipes[0]["ingredients"]
            ),
            "parse_status": "parsed_recipe",
            "parse_reason": "One structured {{Recipe}} block with ingredients was parsed.",
            "parsed_recipe_ingredients": parsed_recipes[0]["ingredients"],
            "parsed_recipe_count": 1,
        }

    if parsed_recipes:
        return {
            "source_type": "recipe",
            "source_step_summary": "Wiki recipe/source data found, but it needs manual review.",
            "parse_status": "manual_review_needed",
            "parse_reason": (
                "Recipe template data exists, but it did not contain one safely "
                "parseable ingredient list."
            ),
            "parsed_recipe_ingredients": [],
            "parsed_recipe_count": len(parsed_recipes),
        }

    source_type = "unknown"

    if options:
        source_type = normal_report_source_type(options[0].get("acquisition_type"))

    summary = source_summary_from_acquisition_options(options)

    if summary:
        return {
            "source_type": source_type,
            "source_step_summary": summary,
            "parse_status": "parsed_acquisition_option",
            "parse_reason": "A wiki acquisition option was parsed.",
            "parsed_recipe_ingredients": [],
            "parsed_recipe_count": 0,
        }

    return {
        "source_type": source_type,
        "source_step_summary": "Wiki page found, but structured source data could not be parsed cleanly.",
        "parse_status": "manual_review_needed",
        "parse_reason": "No structured recipe or acquisition option was parsed.",
        "parsed_recipe_ingredients": [],
        "parsed_recipe_count": 0,
    }


def clean_wiki_summary_text(wiki_text: str) -> str:
    """Turn a short wiki snippet into plain-ish terminal text."""

    text = normalize_wiki_item_text(wiki_text)["stripped_text"]
    text = re.sub(r"^\*+\s*", "", text)
    text = re.sub(r"^#+\s*", "", text)
    text = re.sub(r"\s+", " ", text).strip()

    if text.startswith("{{") or text.endswith("}}"):
        return ""

    return text


def looks_like_raw_wiki_markup(text: str) -> bool:
    """Return True for raw template snippets that should not be in reports."""

    stripped_text = str(text).strip()
    return stripped_text.startswith("{{") or "{{Recipe" in stripped_text


def source_summary_from_acquisition_options(options: list[dict[str, Any]]) -> str:
    """Choose a short, cautious source summary from parsed wiki options."""

    if not options:
        return ""

    preferred_types = (
        "recipe",
        "mystic_forge",
        "vendor",
        "achievement",
        "collection",
        "reward",
        "acquisition",
    )

    for preferred_type in preferred_types:
        for option in options:
            if option.get("acquisition_type") != preferred_type:
                continue

            text = clean_wiki_summary_text(
                option.get("normalized_text") or option.get("text") or ""
            )

            if text:
                return text[:240]

    text = clean_wiki_summary_text(options[0].get("normalized_text") or options[0].get("text") or "")
    return text[:240]


class ReferenceDatabase:
    """Read and write the local public reference database."""

    def __init__(self, db_path: Path = DEFAULT_REFERENCE_DB_PATH) -> None:
        self.db_path = db_path

    def exists(self) -> bool:
        """Return whether the SQLite database file already exists."""

        return self.db_path.exists()

    def connect(self) -> sqlite3.Connection:
        """Open a SQLite connection with rows addressable by name."""

        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def open_connection(self) -> Any:
        """Open a SQLite connection that always closes after use."""

        connection = self.connect()

        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize_schema(self) -> None:
        """Create tables for public reference data."""

        self.db_path.parent.mkdir(parents=True, exist_ok=True)

        with self.open_connection() as connection:
            connection.executescript(
                """
                PRAGMA foreign_keys = ON;

                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS items (
                    item_id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    type TEXT,
                    rarity TEXT,
                    flags_json TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    source TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS item_names (
                    normalized_name TEXT NOT NULL,
                    item_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (normalized_name, item_id)
                );

                CREATE TABLE IF NOT EXISTS recipes (
                    recipe_id INTEGER PRIMARY KEY,
                    output_item_id INTEGER,
                    output_item_count INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    recipe_type TEXT,
                    raw_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recipe_ingredients (
                    recipe_id INTEGER NOT NULL,
                    item_id INTEGER NOT NULL,
                    count INTEGER NOT NULL,
                    PRIMARY KEY (recipe_id, item_id)
                );

                CREATE TABLE IF NOT EXISTS recipe_outputs (
                    output_item_id INTEGER NOT NULL,
                    recipe_id INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    PRIMARY KEY (output_item_id, recipe_id, source)
                );

                CREATE TABLE IF NOT EXISTS wiki_recipes (
                    recipe_key TEXT PRIMARY KEY,
                    output_item_id INTEGER,
                    name TEXT,
                    source_url TEXT NOT NULL,
                    review_status TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    imported_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS acquisition_options (
                    option_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    acquisition_type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    review_status TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    imported_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS recipe_overrides (
                    override_key TEXT PRIMARY KEY,
                    item_id INTEGER,
                    name TEXT,
                    override_type TEXT,
                    verified INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                """
            )
            self.ensure_column(connection, "wiki_recipes", "name", "TEXT")
            self.ensure_column(connection, "wiki_recipes", "source_url", "TEXT NOT NULL DEFAULT ''")
            self.ensure_column(
                connection,
                "wiki_recipes",
                "review_status",
                "TEXT NOT NULL DEFAULT 'wiki_imported_unreviewed'",
            )
            self.ensure_column(connection, "wiki_recipes", "imported_at", "INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(connection, "wiki_recipes", "updated_at", "INTEGER NOT NULL DEFAULT 0")
            self.ensure_column(connection, "acquisition_options", "source_url", "TEXT")
            self.ensure_column(
                connection,
                "acquisition_options",
                "review_status",
                "TEXT NOT NULL DEFAULT 'wiki_imported_unreviewed'",
            )
            self.ensure_column(
                connection,
                "acquisition_options",
                "imported_at",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self.ensure_column(
                connection,
                "acquisition_options",
                "updated_at",
                "INTEGER NOT NULL DEFAULT 0",
            )
            self.set_metadata(connection, "schema_version", str(SCHEMA_VERSION))

    def ensure_column(
        self,
        connection: sqlite3.Connection,
        table_name: str,
        column_name: str,
        column_sql: str,
    ) -> None:
        """Add a column to an existing table when needed."""

        rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
        existing_columns = {row["name"] for row in rows}

        if column_name in existing_columns:
            return

        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")

    def set_metadata(self, connection: sqlite3.Connection, key: str, value: str) -> None:
        """Set one metadata value."""

        connection.execute(
            """
            INSERT INTO metadata (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, int(time.time())),
        )

    def import_items(self) -> int:
        """Fetch all public item details into the database."""

        self.initialize_schema()
        item_ids = [int(item_id) for item_id in public_api_get("/items")]
        imported_count = 0
        now = int(time.time())

        with self.open_connection() as connection:
            for item_id_chunk in chunked(item_ids, API_BATCH_SIZE):
                rows = public_api_get(
                    "/items",
                    params={"ids": ",".join(str(item_id) for item_id in item_id_chunk)},
                )

                for row in rows:
                    item_id = int(row["id"])
                    name = str(row.get("name", f"Item {item_id}"))
                    connection.execute(
                        """
                        INSERT INTO items
                            (item_id, name, type, rarity, flags_json, raw_json, source, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, 'official_api', ?)
                        ON CONFLICT(item_id) DO UPDATE SET
                            name = excluded.name,
                            type = excluded.type,
                            rarity = excluded.rarity,
                            flags_json = excluded.flags_json,
                            raw_json = excluded.raw_json,
                            source = excluded.source,
                            updated_at = excluded.updated_at
                        """,
                        (
                            item_id,
                            name,
                            row.get("type"),
                            row.get("rarity"),
                            json.dumps(row.get("flags", []), sort_keys=True),
                            json.dumps(row, sort_keys=True),
                            now,
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO item_names
                            (normalized_name, item_id, name, source)
                        VALUES (?, ?, ?, 'official_api')
                        """,
                        (normalize_item_name(name), item_id, name),
                    )
                    imported_count += 1

            self.set_metadata(connection, "items_updated_at", str(now))
            self.set_metadata(connection, "item_count", str(imported_count))

        return imported_count

    def import_recipes(self) -> int:
        """Fetch all public recipe details into the database."""

        self.initialize_schema()
        recipe_ids = [int(recipe_id) for recipe_id in public_api_get("/recipes")]
        imported_count = 0
        now = int(time.time())

        with self.open_connection() as connection:
            for recipe_id_chunk in chunked(recipe_ids, API_BATCH_SIZE):
                rows = public_api_get(
                    "/recipes",
                    params={"ids": ",".join(str(recipe_id) for recipe_id in recipe_id_chunk)},
                )

                for row in rows:
                    recipe_id = int(row["id"])
                    output_item_id = row.get("output_item_id")
                    output_item_count = int(row.get("output_item_count", 1))
                    connection.execute(
                        """
                        INSERT INTO recipes
                            (recipe_id, output_item_id, output_item_count, source,
                             recipe_type, raw_json, updated_at)
                        VALUES (?, ?, ?, 'official_api', ?, ?, ?)
                        ON CONFLICT(recipe_id) DO UPDATE SET
                            output_item_id = excluded.output_item_id,
                            output_item_count = excluded.output_item_count,
                            source = excluded.source,
                            recipe_type = excluded.recipe_type,
                            raw_json = excluded.raw_json,
                            updated_at = excluded.updated_at
                        """,
                        (
                            recipe_id,
                            output_item_id,
                            output_item_count,
                            row.get("type"),
                            json.dumps(row, sort_keys=True),
                            now,
                        ),
                    )
                    connection.execute(
                        "DELETE FROM recipe_ingredients WHERE recipe_id = ?",
                        (recipe_id,),
                    )

                    for ingredient in row.get("ingredients", []):
                        if "item_id" not in ingredient or "count" not in ingredient:
                            continue

                        connection.execute(
                            """
                            INSERT OR REPLACE INTO recipe_ingredients
                                (recipe_id, item_id, count)
                            VALUES (?, ?, ?)
                            """,
                            (
                                recipe_id,
                                int(ingredient["item_id"]),
                                int(ingredient["count"]),
                            ),
                        )

                    if output_item_id is not None:
                        connection.execute(
                            """
                            INSERT OR REPLACE INTO recipe_outputs
                                (output_item_id, recipe_id, source)
                            VALUES (?, ?, 'official_api')
                            """,
                            (int(output_item_id), recipe_id),
                        )

                    imported_count += 1

            self.set_metadata(connection, "recipes_updated_at", str(now))
            self.set_metadata(connection, "recipe_count", str(imported_count))

        return imported_count

    def apply_overrides(self, overrides_path: Path = DEFAULT_RECIPE_OVERRIDES_PATH) -> int:
        """Store curated override metadata in the reference database."""

        self.initialize_schema()

        if not overrides_path.exists():
            overrides = {"overrides": []}
        else:
            overrides = json.loads(overrides_path.read_text(encoding="utf-8"))

        override_rows = overrides.get("overrides", [])

        if not isinstance(override_rows, list):
            raise ValueError(f"{overrides_path} field 'overrides' must be a list.")

        now = int(time.time())
        applied_count = 0

        with self.open_connection() as connection:
            connection.execute("DELETE FROM recipe_overrides")

            for index, override in enumerate(override_rows, start=1):
                if not isinstance(override, dict):
                    continue

                item_id = override.get("item_id", override.get("id"))
                name = str(override.get("name", "")).strip()
                override_key = str(item_id or normalize_item_name(name) or index)
                connection.execute(
                    """
                    INSERT OR REPLACE INTO recipe_overrides
                        (override_key, item_id, name, override_type, verified, raw_json, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        override_key,
                        int(item_id) if item_id is not None else None,
                        name or None,
                        override.get("type"),
                        1 if override.get("verified", False) else 0,
                        json.dumps(override, sort_keys=True),
                        now,
                    ),
                )
                applied_count += 1

            self.set_metadata(connection, "overrides_updated_at", str(now))
            self.set_metadata(connection, "override_count", str(applied_count))

        return applied_count

    def import_wiki_recipes_for_items(self, items: list[dict[str, Any]]) -> dict[str, int]:
        """Import wiki recipe/acquisition data for specific unresolved items only."""

        self.initialize_schema()
        imported_pages = 0
        acquisition_options = 0
        now = int(time.time())

        with self.open_connection() as connection:
            for item in items:
                item_id = int(item["item_id"])
                name = str(item.get("name") or f"Item {item_id}")
                page_title = name
                source_url = wiki_page_url(page_title)
                response = wiki_api_get(
                    {
                        "action": "query",
                        "prop": "revisions",
                        "titles": page_title,
                        "rvprop": "content",
                        "rvslots": "main",
                        "format": "json",
                        "formatversion": "2",
                    }
                )
                pages = response.get("query", {}).get("pages", [])
                page = pages[0] if pages else {}
                missing = bool(page.get("missing"))
                wikitext = "" if missing else extract_wiki_text(page)
                options = extract_acquisition_options_from_wikitext(wikitext)
                source_step_data = source_step_data_from_wikitext(
                    wikitext,
                    options,
                    missing,
                )
                source_step_review_status = (
                    "wiki_page_missing_manual_review_needed"
                    if missing
                    else "wiki_page_found_manual_review_needed"
                )
                raw_payload = {
                    "item_id": item_id,
                    "name": name,
                    "source_url": source_url,
                    "page_title": page.get("title", page_title),
                    "missing": missing,
                    "wikitext": wikitext,
                    "acquisition_options": options,
                    "source_step_summary": source_step_data["source_step_summary"],
                    "source_summary": source_step_data["source_step_summary"],
                    "source_type": source_step_data["source_type"],
                    "source_parse_status": source_step_data["parse_status"],
                    "source_parse_reason": source_step_data["parse_reason"],
                    "parsed_recipe_ingredients": source_step_data[
                        "parsed_recipe_ingredients"
                    ],
                    "parsed_recipe_count": source_step_data["parsed_recipe_count"],
                    "source_step_review_status": source_step_review_status,
                }
                recipe_key = f"wiki:{item_id}"

                connection.execute(
                    """
                    INSERT INTO wiki_recipes
                        (recipe_key, output_item_id, name, source_url, review_status,
                         raw_json, imported_at, updated_at)
                    VALUES (?, ?, ?, ?, 'wiki_imported_unreviewed', ?, ?, ?)
                    ON CONFLICT(recipe_key) DO UPDATE SET
                        output_item_id = excluded.output_item_id,
                        name = excluded.name,
                        source_url = excluded.source_url,
                        review_status = excluded.review_status,
                        raw_json = excluded.raw_json,
                        imported_at = excluded.imported_at,
                        updated_at = excluded.updated_at
                    """,
                    (
                        recipe_key,
                        item_id,
                        name,
                        source_url,
                        json.dumps(raw_payload, sort_keys=True),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    DELETE FROM acquisition_options
                    WHERE item_id = ? AND source = 'wiki'
                    """,
                    (item_id,),
                )

                for option in options:
                    option_payload = {
                        "item_id": item_id,
                        "name": name,
                        "source_url": source_url,
                        **option,
                    }
                    connection.execute(
                        """
                        INSERT INTO acquisition_options
                            (item_id, acquisition_type, source, source_url, review_status,
                             raw_json, imported_at, updated_at)
                        VALUES (?, ?, 'wiki', ?, 'wiki_imported_unreviewed', ?, ?, ?)
                        """,
                        (
                            item_id,
                            option["acquisition_type"],
                            source_url,
                            json.dumps(option_payload, sort_keys=True),
                            now,
                            now,
                        ),
                    )
                    acquisition_options += 1

                imported_pages += 1

            self.set_metadata(connection, "wiki_recipes_updated_at", str(now))
            self.set_metadata(connection, "wiki_recipe_import_count", str(imported_pages))
            self.set_metadata(connection, "wiki_acquisition_option_count", str(acquisition_options))

        return {
            "wiki_pages": imported_pages,
            "acquisition_options": acquisition_options,
        }

    def item_name_matches(self, item_name: str) -> list[dict[str, Any]]:
        """Return item-name matches from the local reference database."""

        if not self.exists():
            return []

        with self.open_connection() as connection:
            rows = connection.execute(
                """
                SELECT item_id, name
                FROM item_names
                WHERE normalized_name = ?
                ORDER BY item_id
                """,
                (normalize_item_name(item_name),),
            ).fetchall()

        return [
            {"id": int(row["item_id"]), "item_id": int(row["item_id"]), "name": row["name"]}
            for row in rows
        ]

    def items_by_id(self, item_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Return item rows as official-style dictionaries."""

        if not self.exists() or not item_ids:
            return {}

        unique_item_ids = sorted({int(item_id) for item_id in item_ids})
        items: dict[int, dict[str, Any]] = {}

        with self.open_connection() as connection:
            for item_id_chunk in chunked(unique_item_ids, 500):
                placeholders = ",".join("?" for _item_id in item_id_chunk)
                rows = connection.execute(
                    f"SELECT item_id, raw_json FROM items WHERE item_id IN ({placeholders})",
                    item_id_chunk,
                ).fetchall()

                for row in rows:
                    items[int(row["item_id"])] = json.loads(row["raw_json"])

        return items

    def recipe_ids_for_output(self, item_id: int) -> list[int]:
        """Return official recipe IDs that output one item."""

        if not self.exists():
            return []

        with self.open_connection() as connection:
            rows = connection.execute(
                """
                SELECT recipe_id
                FROM recipe_outputs
                WHERE output_item_id = ? AND source = 'official_api'
                ORDER BY recipe_id
                """,
                (int(item_id),),
            ).fetchall()

        return [int(row["recipe_id"]) for row in rows]

    def recipes_by_id(self, recipe_ids: list[int]) -> dict[int, dict[str, Any]]:
        """Return official recipe rows as API-style dictionaries."""

        if not self.exists() or not recipe_ids:
            return {}

        unique_recipe_ids = sorted({int(recipe_id) for recipe_id in recipe_ids})
        recipes: dict[int, dict[str, Any]] = {}

        with self.open_connection() as connection:
            for recipe_id_chunk in chunked(unique_recipe_ids, 500):
                placeholders = ",".join("?" for _recipe_id in recipe_id_chunk)
                rows = connection.execute(
                    f"SELECT recipe_id, raw_json FROM recipes WHERE recipe_id IN ({placeholders})",
                    recipe_id_chunk,
                ).fetchall()

                for row in rows:
                    recipes[int(row["recipe_id"])] = json.loads(row["raw_json"])

        return recipes

    def wiki_recipes_for_output(self, item_id: int) -> list[dict[str, Any]]:
        """Return imported wiki recipe rows for one output item."""

        if not self.exists():
            return []

        with self.open_connection() as connection:
            rows = connection.execute(
                """
                SELECT recipe_key, output_item_id, name, source_url, review_status, raw_json
                FROM wiki_recipes
                WHERE output_item_id = ?
                ORDER BY recipe_key
                """,
                (int(item_id),),
            ).fetchall()

        recipes: list[dict[str, Any]] = []

        for row in rows:
            payload = json.loads(row["raw_json"])
            recipes.append(
                {
                    "recipe_key": row["recipe_key"],
                    "output_item_id": int(row["output_item_id"]),
                    "name": row["name"],
                    "source_url": row["source_url"],
                    "review_status": row["review_status"],
                    "raw_json": payload,
                }
            )

        return recipes

    def acquisition_options_for_item(self, item_id: int) -> list[dict[str, Any]]:
        """Return imported acquisition-option rows for one item."""

        if not self.exists():
            return []

        with self.open_connection() as connection:
            rows = connection.execute(
                """
                SELECT option_id, item_id, acquisition_type, source, source_url,
                       review_status, raw_json
                FROM acquisition_options
                WHERE item_id = ?
                ORDER BY option_id
                """,
                (int(item_id),),
            ).fetchall()

        options: list[dict[str, Any]] = []

        for row in rows:
            payload = json.loads(row["raw_json"])
            options.append(
                {
                    "option_id": int(row["option_id"]),
                    "item_id": int(row["item_id"]),
                    "acquisition_type": row["acquisition_type"],
                    "source": row["source"],
                    "source_url": row["source_url"],
                    "review_status": row["review_status"],
                    "raw_json": payload,
                }
            )

        return options

    def source_step_for_item(self, item_id: int) -> dict[str, Any]:
        """Return imported wiki source-step data for one manual item."""

        rows = self.wiki_recipes_for_output(item_id)
        options = self.acquisition_options_for_item(item_id)
        primary_row = rows[0] if rows else {}
        raw_payload = primary_row.get("raw_json", {}) if primary_row else {}
        source_url = (
            primary_row.get("source_url")
            or raw_payload.get("source_url")
            or wiki_page_url(str(raw_payload.get("name") or f"Item {item_id}"))
        )
        source_step_summary = str(
            raw_payload.get("source_step_summary")
            or raw_payload.get("source_summary", "")
        ).strip()

        if looks_like_raw_wiki_markup(source_step_summary):
            source_step_summary = ""

        source_type = str(raw_payload.get("source_type", "")).strip() or "unknown"
        parse_status = str(raw_payload.get("source_parse_status", "")).strip()
        parse_reason = str(raw_payload.get("source_parse_reason", "")).strip()
        parsed_recipe_ingredients = list(raw_payload.get("parsed_recipe_ingredients", []))
        parsed_recipe_count = int(raw_payload.get("parsed_recipe_count", 0))

        if not source_step_summary and raw_payload.get("wikitext") is not None:
            source_step_data = source_step_data_from_wikitext(
                str(raw_payload.get("wikitext", "")),
                [
                    option.get("raw_json", {})
                    for option in options
                    if isinstance(option.get("raw_json", {}), dict)
                ],
                bool(raw_payload.get("missing", False)),
            )
            source_step_summary = source_step_data["source_step_summary"]
            source_type = source_step_data["source_type"]
            parse_status = source_step_data["parse_status"]
            parse_reason = source_step_data["parse_reason"]
            parsed_recipe_ingredients = source_step_data["parsed_recipe_ingredients"]
            parsed_recipe_count = int(source_step_data["parsed_recipe_count"])

        if not source_step_summary:
            option_payloads = [
                option.get("raw_json", {})
                for option in options
                if isinstance(option.get("raw_json", {}), dict)
            ]
            source_step_summary = source_summary_from_acquisition_options(option_payloads)

        page_found = bool(rows and not raw_payload.get("missing", False))
        review_status = str(
            raw_payload.get("source_step_review_status")
            or primary_row.get("review_status", "")
            or "not_imported"
        )
        page_title = str(raw_payload.get("page_title", "")).strip()

        if page_found and not source_step_summary:
            source_step_summary = (
                "Wiki page found, but structured source data could not be parsed cleanly."
            )

        if not parse_status and page_found:
            parse_status = "manual_review_needed"

        if not parse_reason and page_found:
            parse_reason = "No structured source-step parser result was stored."

        return {
            "item_id": int(item_id),
            "name": primary_row.get("name") or raw_payload.get("name") or f"Item {item_id}",
            "source_url": source_url if rows else "",
            "review_status": review_status,
            "source_type": source_type,
            "source_step_summary": source_step_summary,
            "source_summary": source_step_summary,
            "source_parse_status": parse_status,
            "source_parse_reason": parse_reason,
            "parsed_recipe_ingredients": parsed_recipe_ingredients,
            "parsed_recipe_count": parsed_recipe_count,
            "acquisition_option_count": len(options),
            "wiki_page_found": page_found,
            "wiki_rows_found": len(rows),
            "raw_wiki_page_title": page_title,
        }

    def status(self) -> dict[str, str]:
        """Return metadata and table counts for display."""

        if not self.exists():
            return {"exists": "false", "path": str(self.db_path)}

        with self.open_connection() as connection:
            metadata_rows = connection.execute(
                "SELECT key, value FROM metadata ORDER BY key"
            ).fetchall()
            status = {row["key"]: row["value"] for row in metadata_rows}

            for table_name in (
                "items",
                "item_names",
                "recipes",
                "recipe_ingredients",
                "recipe_outputs",
                "wiki_recipes",
                "acquisition_options",
                "recipe_overrides",
            ):
                row = connection.execute(f"SELECT COUNT(*) AS count FROM {table_name}").fetchone()
                status[f"{table_name}_rows"] = str(row["count"])

        status["exists"] = "true"
        status["path"] = str(self.db_path)
        return status


def setup_reference_database(
    db_path: Path = DEFAULT_REFERENCE_DB_PATH,
    overrides_path: Path = DEFAULT_RECIPE_OVERRIDES_PATH,
    wiki_items: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Build or refresh the local public reference database."""

    database = ReferenceDatabase(db_path)
    database.initialize_schema()
    item_count = database.import_items()
    recipe_count = database.import_recipes()
    override_count = database.apply_overrides(overrides_path)
    wiki_counts = database.import_wiki_recipes_for_items(wiki_items or [])
    return {
        "items": item_count,
        "recipes": recipe_count,
        "overrides": override_count,
        "wiki_pages": wiki_counts["wiki_pages"],
        "acquisition_options": wiki_counts["acquisition_options"],
    }


def reference_database_status(
    db_path: Path = DEFAULT_REFERENCE_DB_PATH,
) -> dict[str, str]:
    """Return status for the reference database."""

    return ReferenceDatabase(db_path).status()
