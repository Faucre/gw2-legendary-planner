from __future__ import annotations

import json
import sqlite3
import time
import unittest
import uuid
from pathlib import Path

from gw2_legendary_planner import (
    AUTO_RECIPE_MATERIALS_FIELD,
    RECIPE_ENGINE_WARNINGS_FIELD,
    RECIPE_TREE_FIELD,
    RECIPE_UNKNOWN_STEPS_FIELD,
    build_rule_recommendations,
    build_target_status,
    make_recipe_engine_lines,
    recipe_tree_to_material_entries,
)
from recipe_engine import RecipeEngine
from reference_database import ReferenceDatabase, normalize_wiki_item_text


def insert_item(
    connection: sqlite3.Connection,
    item_id: int,
    name: str,
    flags: list[str] | None = None,
) -> None:
    now = int(time.time())
    normalized_flags = flags or []
    raw_json = json.dumps(
        {
            "id": item_id,
            "name": name,
            "flags": normalized_flags,
        },
        sort_keys=True,
    )
    connection.execute(
        """
        INSERT INTO items
            (item_id, name, type, rarity, flags_json, raw_json, source, updated_at)
        VALUES (?, ?, '', '', ?, ?, 'official_api', ?)
        """,
        (item_id, name, json.dumps(normalized_flags, sort_keys=True), raw_json, now),
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
        test_temp_root = Path.cwd() / "reports" / "test-temp"
        test_temp_root.mkdir(parents=True, exist_ok=True)
        self.root = test_temp_root / uuid.uuid4().hex
        self.root.mkdir(parents=True, exist_ok=True)
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
        pass

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
        report_lines = make_recipe_engine_lines(target, detailed=True)
        self.assertTrue(any("GW2 Wiki" in line for line in report_lines))
        self.assertTrue(any(self.source_url in line for line in report_lines))

    def test_manual_override_with_clear_wiki_recipe_expands(self) -> None:
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

        self.assertEqual(tree["final_source"]["source"], "source_step_recipe")
        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(2000, 1), (2001, 2)},
        )
        self.assertEqual(tree["unknown_manual_steps"], [])
        self.assertEqual(len(tree["expanded_source_steps"]), 1)
        self.assertTrue(
            any(
                "Expanded source-step recipe: Legendary Spear -> 2 ingredients" == event
                for event in tree["debug_events"]
            )
        )

    def test_plural_wiki_ingredient_resolves_to_singular_item_name(self) -> None:
        plural_wikitext = "\n".join(
            [
                "== Acquisition ==",
                "=== Recipe ===",
                "{{Recipe",
                "| source = Mystic Forge",
                "| ingredient1 = 38 Mystic Clovers",
                "}}",
            ]
        )
        connection = self.reference_database.connect()
        try:
            insert_item(connection, 2002, "Mystic Clover")
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                plural_wikitext,
                source_url=self.source_url,
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

        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(2002, 38)},
        )
        self.assertEqual(tree["unknown_manual_steps"], [])

    def test_account_bound_terminal_child_is_counted_and_kept_as_manual_step(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_item(connection, 4000, "Account Gift", flags=["AccountBound"])
            connection.commit()
        finally:
            connection.close()

        self.write_overrides(
            [
                {
                    "item_id": self.root_item_id,
                    "name": self.root_item_name,
                    "type": "manual",
                    "verified": True,
                    "ingredients": [
                        {
                            "item_id": 4000,
                            "name": "Account Gift",
                            "amount": 3,
                        }
                    ],
                }
            ]
        )

        engine = self.make_engine()
        tree = engine.resolve_recipe_tree(self.root_item_id)

        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(4000, 3)},
        )
        self.assertEqual(len(tree["unknown_manual_steps"]), 1)
        self.assertEqual(tree["unknown_manual_steps"][0]["item_id"], 4000)

    def test_owned_full_intermediate_item_satisfies_branch(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_item(connection, 3000, "Gift of the Homesteader", flags=["AccountBound"])
            insert_item(connection, 3001, "Mystic Clover")
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 1 Gift of the Homesteader",
                        "}}",
                    ]
                ),
                source_url=self.source_url,
            )
            insert_wiki_recipe(
                connection,
                3000,
                "Gift of the Homesteader",
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 38 Mystic Clovers",
                        "}}",
                    ]
                ),
                source_url="https://wiki.guildwars2.com/wiki/Gift_of_the_Homesteader",
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
        tree = engine.resolve_recipe_tree(self.root_item_id, item_counts={3000: 1})

        self.assertEqual(tree["raw_material_requirements"], [])
        self.assertEqual(tree["unknown_manual_steps"], [])
        self.assertEqual(tree["expanded_source_steps"], [])
        self.assertEqual(tree["satisfied_intermediates"][0]["item_id"], 3000)
        self.assertEqual(tree["satisfied_intermediates"][0]["amount"], 1)

        target = {
            RECIPE_TREE_FIELD: tree,
            RECIPE_UNKNOWN_STEPS_FIELD: [],
            RECIPE_ENGINE_WARNINGS_FIELD: [],
            AUTO_RECIPE_MATERIALS_FIELD: recipe_tree_to_material_entries(tree),
        }
        report_lines = make_recipe_engine_lines(target)
        self.assertTrue(
            any("Gift of the Homesteader: have 1, branch satisfied." in line for line in report_lines)
        )

    def test_owned_partial_intermediate_stack_expands_only_remaining_amount(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_item(connection, 3000, "Gift of the Homesteader", flags=["AccountBound"])
            insert_item(connection, 3001, "Mystic Clover")
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 2 Gift of the Homesteader",
                        "}}",
                    ]
                ),
                source_url=self.source_url,
            )
            insert_wiki_recipe(
                connection,
                3000,
                "Gift of the Homesteader",
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 38 Mystic Clovers",
                        "}}",
                    ]
                ),
                source_url="https://wiki.guildwars2.com/wiki/Gift_of_the_Homesteader",
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
        tree = engine.resolve_recipe_tree(self.root_item_id, item_counts={3000: 1})

        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(3001, 38)},
        )
        self.assertEqual(tree["satisfied_intermediates"][0]["amount"], 1)
        self.assertEqual(tree["satisfied_intermediates"][0]["remaining_amount"], 1)

    def test_no_owned_intermediate_item_expands_full_branch(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_item(connection, 3000, "Gift of the Homesteader", flags=["AccountBound"])
            insert_item(connection, 3001, "Mystic Clover")
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 2 Gift of the Homesteader",
                        "}}",
                    ]
                ),
                source_url=self.source_url,
            )
            insert_wiki_recipe(
                connection,
                3000,
                "Gift of the Homesteader",
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 38 Mystic Clovers",
                        "}}",
                    ]
                ),
                source_url="https://wiki.guildwars2.com/wiki/Gift_of_the_Homesteader",
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
        tree = engine.resolve_recipe_tree(self.root_item_id, item_counts={})

        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(3001, 76)},
        )
        self.assertEqual(tree["satisfied_intermediates"], [])

    def test_raw_material_mode_ignores_owned_intermediate_item(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_item(connection, 3000, "Gift of the Homesteader", flags=["AccountBound"])
            insert_item(connection, 3001, "Mystic Clover")
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 1 Gift of the Homesteader",
                        "}}",
                    ]
                ),
                source_url=self.source_url,
            )
            insert_wiki_recipe(
                connection,
                3000,
                "Gift of the Homesteader",
                "\n".join(
                    [
                        "== Acquisition ==",
                        "=== Recipe ===",
                        "{{Recipe",
                        "| source = Mystic Forge",
                        "| ingredient1 = 38 Mystic Clovers",
                        "}}",
                    ]
                ),
                source_url="https://wiki.guildwars2.com/wiki/Gift_of_the_Homesteader",
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
        tree = engine.resolve_recipe_tree(
            self.root_item_id,
            item_counts={3000: 1},
            use_owned_intermediates=False,
        )

        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(3001, 38)},
        )
        self.assertEqual(tree["satisfied_intermediates"], [])

    def test_manual_source_step_uses_imported_wiki_metadata(self) -> None:
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
                "achievement",
                self.source_url,
                "* Complete the related legendary collection achievement.",
                heading="Achievements",
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
        expanded_step = tree["expanded_source_steps"][0]

        self.assertEqual(tree["unknown_manual_steps"], [])
        self.assertEqual(
            {(row["item_id"], row["amount"]) for row in tree["raw_material_requirements"]},
            {(2000, 1), (2001, 2)},
        )
        self.assertEqual(expanded_step["source_url"], self.source_url)
        self.assertEqual(expanded_step["review_status"], "wiki_imported_unreviewed")
        self.assertEqual(expanded_step["source_type"], "mystic_forge")
        self.assertEqual(expanded_step["acquisition_option_count"], 1)
        self.assertIn("Gift of Frost", expanded_step["source_step_summary"])
        self.assertNotIn("{{Recipe", expanded_step["source_step_summary"])
        self.assertEqual(expanded_step["ingredient_count"], 2)

        target = {
            RECIPE_TREE_FIELD: tree,
            RECIPE_UNKNOWN_STEPS_FIELD: list(tree["unknown_manual_steps"]),
            RECIPE_ENGINE_WARNINGS_FIELD: [],
            AUTO_RECIPE_MATERIALS_FIELD: recipe_tree_to_material_entries(tree),
        }
        report_lines = make_recipe_engine_lines(target, detailed=True)

        self.assertTrue(any("Source URL:" in line for line in report_lines))
        self.assertTrue(any("Summary:" in line for line in report_lines))
        self.assertTrue(any("Source-step recipes expanded:" in line for line in report_lines))
        self.assertFalse(any("{{Recipe" in line for line in report_lines))

        detailed_lines = make_recipe_engine_lines(target, detailed=True, debug=False)
        debug_lines = make_recipe_engine_lines(target, detailed=True, debug=True)
        self.assertFalse(any("Debug:" in line for line in detailed_lines))
        self.assertTrue(any("Debug:" in line for line in debug_lines))

    def test_unparseable_source_step_recipe_shows_manual_review_message(self) -> None:
        connection = self.reference_database.connect()
        try:
            insert_wiki_recipe(
                connection,
                self.root_item_id,
                self.root_item_name,
                "{{Recipe\n| source = Mystic Forge\n}}",
                source_url=self.source_url,
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
        manual_step = tree["unknown_manual_steps"][0]

        self.assertEqual(
            manual_step["source_step_summary"],
            "Wiki recipe/source data found, but it needs manual review.",
        )
        self.assertNotIn("{{Recipe", manual_step["source_step_summary"])

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

    def test_recommendations_use_expanded_klobjarne_items(self) -> None:
        missing_items = [
            {"id": 1, "name": "Mystic Clover", "missing": 38},
            {"id": 2, "name": "Mystic Runestone", "missing": 100},
            {"id": 3, "name": "Bloodstone Shard", "missing": 1},
            {"id": 4, "name": "Gift of Research", "missing": 1},
            {"id": 5, "name": "Gift of the Mists", "missing": 1},
            {"id": 6, "name": "Nyr Hrammr", "missing": 1},
        ]

        recommendations = build_rule_recommendations(
            "Klobjarne Geirr",
            missing_items,
            [],
            None,
        )

        recommendation_text = "\n".join(recommendations)
        self.assertIn("Mystic Clover: do Wizard's Vault", recommendation_text)
        self.assertIn("Mystic Runestone: check the source-step/vendor path", recommendation_text)
        self.assertIn("Bloodstone Shard: check your Spirit Shards", recommendation_text)
        self.assertIn("Source step: Gift of Research", recommendation_text)
        self.assertIn("research notes", recommendation_text)
        self.assertIn("Source step: Gift of the Mists", recommendation_text)
        self.assertIn("Precursor step: Nyr Hrammr", recommendation_text)


if __name__ == "__main__":
    unittest.main()
