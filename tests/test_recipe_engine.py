from __future__ import annotations

import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from gw2_legendary_planner import (
    AUTO_RECIPE_MATERIALS_FIELD,
    RECIPE_ENGINE_WARNINGS_FIELD,
    RECIPE_TREE_FIELD,
    RECIPE_UNKNOWN_STEPS_FIELD,
    build_target_status,
    make_recipe_engine_lines,
    recipe_tree_to_material_entries,
)
from recipe_engine import RecipeEngine
from reference_database import ReferenceDatabase, normalize_wiki_item_text


def insert_item(connection: sqlite3.Connection, item_id: int, name: str) -> None:
    now = int(time.time())
    raw_json = json.dumps(
        {
            "id": item_id,
            "name": name,
            "flags": [],
        },
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO items
            (item_id, name, type, rarity, flags_json, raw_json, source, updated_at)
        VALUES (?, ?, '', '', '[]', ?, 'official_api', ?)
        """,
        (item_id, name, raw_json, now),
    )
    connection.execute(
        """
        INSERT INTO item_names
            (normalized_name, item_id, name, source)
        VALUES (?, ?, ?, 'official_api')
        """,
        (" ".join(name.casefold().split()), item_id, name),
    )


def insert_wiki_recipe(
    connection: sqlite3.Connection,
    item_id: int,
    name: str,
    wikitext: str,
    source_url: str = "",
    review_status: str = "wiki_imported_unreviewed",
) -> None:
    now = int(time.time())
    payload = {
        "item_id": item_id,
        "name": name,
        "source_url": source_url,
        "missing": False,
        "wikitext": wikitext,
    }
    connection.execute(
        """
        INSERT INTO wiki_recipes
            (recipe_key, output_item_id, name, source_url, review_status,
             raw_json, imported_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"wiki:{item_id}",
            item_id,
            name,
            source_url,
            review_status,
            json.dumps(payload, sort_keys=True),
            now,
            now,
        ),
    )


def insert_acquisition_option(
    connection: sqlite3.Connection,
    item_id: int,
    acquisition_type: str,
    source_url: str,
    text: str,
    heading: str = "Recipe",
    review_status: str = "wiki_imported_unreviewed",
) -> None:
    now = int(time.time())
    payload = {
        "item_id": item_id,
        "source_url": source_url,
        "acquisition_type": acquisition_type,
        "heading": heading,
        "text": text,
    }
    connection.execute(
        """
        INSERT INTO acquisition_options
            (item_id, acquisition_type, source, source_url, review_status,
             raw_json, imported_at, updated_at)
        VALUES (?, ?, 'wiki', ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            acquisition_type,
            source_url,
            review_status,
            json.dumps(payload, sort_keys=True),
            now,
            now,
        ),
    )


class RecipeEngineWikiFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.reference_db_path = self.root / "planner_reference.sqlite"
        self.overrides_path = self.root / "recipe_overrides.json"
        self.reference_database = ReferenceDatabase(self.reference_db_path)
        self.reference_database.initialize_schema()

        self.root_item_id = 1000
        self.root_item_name = "Legendary Spear"
        self.source_url = "https://wiki.guildwars2.com/wiki/Legendary_Spear"
        self.wikitext = "\n".join(
            [
                "== Acquisition ==",
                "=== Recipe ===",
                "{{Recipe",
                "| source = Mystic Forge",
                "| ingredient1 = 1 Gift of Frost",
                "| ingredient2 = 2 Gift of Fire",
                "}}",
            ]
        )

        connection = self.reference_database.connect()
        try:
            insert_item(connection, self.root_item_id, self.root_item_name)
            insert_item(connection, 2000, "Gift of Frost")
            insert_item(connection, 2001, "Gift of Fire")
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def make_engine(self) -> RecipeEngine:
        return RecipeEngine(
            cache_path=self.root / "recipe_cache.json",
            overrides_path=self.overrides_path,
            reference_db_path=self.reference_db_path,
        )

    def write_overrides(self, overrides: list[dict[str, object]]) -> None:
        self.overrides_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "preferred_recipes_by_output": {},
                    "overrides": overrides,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_unverified_empty_mystic_forge_override_uses_wiki_recipe(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                self.wikitext,
                source_url=self.source_url,
            )
            insert_acquisition_option(
                connection,
                self.root_item_id,
                "recipe",
                self.source_url,
                "{{Recipe",
            )
            connection.commit()
        finally:
            connection.close()

        self.write_overrides(
            [
                {
                    "item_id": self.root_item_id,
                    "name": self.root_item_name,
                    "type": "mystic_forge",
                    "verified": False,
                    "ingredients": [],
                }
            ]
        )

        engine = self.make_engine()
        tree = engine.resolve_recipe_tree(self.root_item_id)
        debug_data = engine.debug_recipe_source(self.root_item_id, self.root_item_name)

        self.assertEqual(tree["final_source"]["source"], "wiki_recipe")
        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(2000, 1), (2001, 2)},
        )
        self.assertEqual(tree["unknown_manual_steps"], [])
        self.assertEqual(debug_data["override_lookup"]["status"], "stub_continue")
        self.assertEqual(debug_data["official_recipe_lookup"]["status"], "no_recipe")
        self.assertEqual(debug_data["chosen_source"]["source"], "wiki_recipe")

        target = {
            RECIPE_TREE_FIELD: tree,
            AUTO_RECIPE_MATERIALS_FIELD: recipe_tree_to_material_entries(tree),
            RECIPE_ENGINE_WARNINGS_FIELD: [],
            RECIPE_UNKNOWN_STEPS_FIELD: list(tree["unknown_manual_steps"]),
        }
        report_lines = make_recipe_engine_lines(target)
        self.assertTrue(any("GW2 Wiki" in line for line in report_lines))
        self.assertTrue(any(self.source_url in line for line in report_lines))

    def test_manual_override_without_ingredients_still_stops(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                self.wikitext,
                source_url=self.source_url,
            )
            insert_acquisition_option(
                connection,
                self.root_item_id,
                "recipe",
                self.source_url,
                "{{Recipe",
            )
            connection.commit()
        finally:
            connection.close()

        self.write_overrides(
            [
                {
                    "item_id": self.root_item_id,
                    "name": self.root_item_name,
                    "type": "manual",
                    "verified": False,
                    "ingredients": [],
                }
            ]
        )

        engine = self.make_engine()
        tree = engine.resolve_recipe_tree(self.root_item_id)

        self.assertEqual(tree["final_source"]["source"], "override_manual_stop")
        self.assertEqual(len(tree["unknown_manual_steps"]), 1)
        self.assertEqual(tree["wiki_sources_used"], [])

    def test_multiple_wiki_recipe_options_warn_instead_of_picking(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                self.wikitext,
                source_url=self.source_url,
            )
            insert_acquisition_option(
                connection,
                self.root_item_id,
                "recipe",
                self.source_url,
                "{{Recipe A",
            )
            insert_acquisition_option(
                connection,
                self.root_item_id,
                "recipe",
                self.source_url,
                "{{Recipe B",
            )
            connection.commit()
        finally:
            connection.close()

        self.write_overrides(
            [
                {
                    "item_id": self.root_item_id,
                    "name": self.root_item_name,
                    "type": "mystic_forge",
                    "verified": False,
                    "ingredients": [],
                }
            ]
        )

        engine = self.make_engine()
        tree = engine.resolve_recipe_tree(self.root_item_id)

        self.assertEqual(tree["final_source"]["source"], "wiki_manual_gap")
        self.assertEqual(len(tree["unknown_manual_steps"]), 1)
        self.assertTrue(
            any(
                "multiple acquisition options" in warning.casefold()
                for warning in tree["warnings"]
            )
        )

    def test_wiki_link_display_name_is_preferred_with_page_fallback(self) -> None:
        name_details = normalize_wiki_item_text(
            "[[Dragon's Claw (weapon)|Dragon's Claw]]"
        )

        self.assertEqual(name_details["normalized_name"], "Dragon's Claw")
        self.assertEqual(name_details["page_title"], "Dragon's Claw (weapon)")
        self.assertEqual(
            name_details["candidates"],
            ["Dragon's Claw", "Dragon's Claw (weapon)"],
        )

    def test_partially_resolved_status_when_wiki_materials_and_manual_steps_exist(self) -> None:
        target = {
            "name": self.root_item_name,
            "final_item_id": self.root_item_id,
            "final_item_name": self.root_item_name,
            "recipe_status": "needs_verification",
            AUTO_RECIPE_MATERIALS_FIELD: [
                {
                    "item_id": 2000,
                    "name": "Gift of Frost",
                    "amount": 1,
                }
            ],
            RECIPE_TREE_FIELD: {
                "wiki_sources_used": [
                    {
                        "item_id": self.root_item_id,
                        "name": self.root_item_name,
                    }
                ],
            },
            RECIPE_UNKNOWN_STEPS_FIELD: [
                {
                    "item_id": 2001,
                    "name": "Gift of Fire",
                    "amount": 1,
                    "reason": "Needs manual source data.",
                }
            ],
            RECIPE_ENGINE_WARNINGS_FIELD: [],
            "steps": [
                {
                    "name": "TODO: Verify final_item_id for Legendary Spear",
                    "complete": False,
                    "notes": "Old template setup step.",
                }
            ],
        }

        status = build_target_status(
            target,
            wallet={},
            item_counts={},
            legendary_armory={},
            item_names={},
            currency_names={},
        )

        self.assertEqual(status["status_label"], "Partially resolved")
        self.assertEqual(status["steps"], [])


if __name__ == "__main__":
    unittest.main()
