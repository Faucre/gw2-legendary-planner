# Guild Wars 2 Legendary Planner

A beginner-friendly Python command-line app for tracking Guild Wars 2 legendary goals.

The app uses the official Guild Wars 2 API at `https://api.guildwars2.com/v2`. It reads your API key from a `.env` file, fetches your wallet currencies and account item storage, then compares them with the goals in `legendary_goals.json`.

## Setup

Requires Python 3.10 or newer.

1. Create or edit `.env`:

   ```env
   GW2_API_KEY=your-api-key-here
   ```

   This project also recognizes the existing `API_Key.env` filename.

2. Make sure the API key has these permissions:

   - `wallet`
   - `inventories`
   - `characters`
   - `unlocks`

3. Edit `legendary_goals.json` with the materials and currencies you want to track.

Materials can use either `item_id` or an exact `name`. If you use a name, the app prefers the local reference database and saves successful name resolutions in `item_cache.json`.

The first name lookup may take a while because the app builds `item_name_index.json` from the public item API. After that file exists, future name lookups are local and much faster.

## Run

```bash
python gw2_legendary_planner.py
```

By default, the report uses a clean summary format: scan summary, `Recommended today`, and each enabled target in priority order.

For the focused CLI Legendary Breakdown v1 view:

```bash
python gw2_legendary_planner.py --breakdown "Klobjarne Geirr"
```

To break down the first enabled target in `legendary_goals.json` priority order:

```bash
python gw2_legendary_planner.py --breakdown-priority
```

The breakdown view shows the target, status, owned/satisfied intermediates,
major components, expanded components, missing materials, manual/source steps,
recommended next actions, and warnings. It uses owned intermediate items first
unless you also pass `--raw-materials`.

To show full recipe paths in the breakdown:

```bash
python gw2_legendary_planner.py --breakdown "Klobjarne Geirr" --show-paths
```

To explain why one missing item appears:

```bash
python gw2_legendary_planner.py --explain-missing "Klobjarne Geirr" "Mystic Clover"
python gw2_legendary_planner.py --explain-missing "Klobjarne Geirr" "Orichalcum Ore"
```

Explain output includes the category, confidence label, amount still missing,
and recipe path such as `Klobjarne Geirr > Gift of the Homesteader > Mystic Clover`.

To also show completed entries:

```bash
python gw2_legendary_planner.py --show-complete
```

To show more target detail, including useful empty sections:

```bash
python gw2_legendary_planner.py --detailed
```

To show API, scan, and cache details for troubleshooting:

```bash
python gw2_legendary_planner.py --debug
```

To skip Trading Post price estimates:

```bash
python gw2_legendary_planner.py --no-prices
```

By default, the recipe planner uses owned intermediate items first. For example,
if you already have a gift needed by a legendary, the app will not also count
that gift's child ingredients. To ignore owned intermediates and fully expand
recipes into raw requirements:

```bash
python gw2_legendary_planner.py --raw-materials
```

To rebuild the item name index:

```bash
python gw2_legendary_planner.py --rebuild-item-index
```

To force a full rescan of every character inventory:

```bash
python gw2_legendary_planner.py --refresh-character-inventories
```

To save a report:

```bash
python gw2_legendary_planner.py --output reports/missing-materials.txt
```

To validate goals and templates without scanning your account inventory:

```bash
python gw2_legendary_planner.py --validate-goals
```

This checks `legendary_goals.json`, referenced templates, all template JSON files, final item resolution, name-only material entries, and whether recipe lookup appears possible for each enabled target. It does not require your GW2 API key and does not read wallet, bank, character, or inventory endpoints.

To create empty override stubs for resolved targets that have no official crafting recipe:

```bash
python gw2_legendary_planner.py --generate-missing-overrides
```

This writes safe placeholders to `data/recipe_overrides.json` with `verified: false` and an empty `ingredients` list. It does not guess legendary recipe ingredients.

To build or refresh the local public reference database:

```bash
python gw2_legendary_planner.py --setup-reference-db
python gw2_legendary_planner.py --update-reference-db
python gw2_legendary_planner.py --update-source-steps
python gw2_legendary_planner.py --reference-db-status
python gw2_legendary_planner.py --debug-recipe-source "Klobjarne Geirr"
python gw2_legendary_planner.py --debug-source-step "Gift of Janthir Wilds"
python gw2_legendary_planner.py --debug-wiki-name "Dragon's Claw (weapon)|Dragon's Claw"
```

Normal planner runs use `data/planner_reference.sqlite` when it exists. Setup/update commands fetch public item and recipe data, build lookup tables, apply `data/recipe_overrides.json`, then import targeted wiki data only for configured targets that still have no official recipe or verified override ingredients. `--update-source-steps` refreshes just the targeted wiki source-step pages for current manual/account-bound recipe gaps. Account-specific caches stay separate.

The report includes a `Recommended today` section. It looks at the first incomplete enabled target first, separates gameplay recommendations from configuration/TODO recommendations, then gives shorter secondary notes for later targets.

## Recipe Engine

The app includes a conservative recipe engine in `recipe_engine.py`.

When a target has a verified `final_item_id`, normal runs ask the local reference database which recipes output that item. The setup/update commands populate that database from the official Guild Wars 2 API:

- `/v2/recipes/search?output=<item_id>`
- `/v2/recipes?ids=...`

If `final_item_id` is missing but `final_item_name` is present, the app resolves that name through the existing item lookup cache. If both are missing, the app tries the target's `name` as a fallback final item name unless `auto_resolve_final_item_from_name` is set to `false`. The name must match exactly one item. Missing, ambiguous, or misspelled final item names become warnings instead of silent guesses.

The engine recursively follows craftable ingredients and adds terminal raw/material requirements to the target automatically. Official recipe and item data belongs in `data/planner_reference.sqlite`; `recipe_cache.json` is legacy fallback cache data.

The recipe engine does not use your account API key. It only reads public API data.

Wiki imports are stored as source material, not trusted recipe totals. Imported wiki rows and acquisition options are marked `wiki_imported_unreviewed`.

Normal runs now use imported wiki recipe data as a fallback only when:

- no official recipe exists or the official recipe lookup fails
- no verified override ingredients are available first
- the imported wiki data points to one clear recipe choice

Verified override ingredients still win first. Manual/account-bound/time-gated style overrides with no ingredients still stop automatic expansion immediately. Unverified empty `mystic_forge` override stubs are treated as review placeholders instead of hard stops, so they no longer block imported wiki data.

If imported wiki data has multiple acquisition options and no single recipe option is clearly marked, the planner refuses to guess and adds a warning/manual recipe gap instead.

Manual/account-bound recipe gaps are shown as source steps. When source-step wiki data has been imported, each step can show the item name, amount needed, source URL, review status, short source summary, and acquisition-option count. If the wiki page exists but is not structured clearly enough, the app reports `wiki page found, manual review needed` instead of expanding uncertain achievement or collection requirements.

Automatic resolution stops and adds a warning instead of guessing when:

- no recipe exists for the final target item
- multiple recipes exist and no preferred recipe is configured
- an item is configured as manual
- an item appears account-bound or soulbound
- an override says the item comes from currency, achievements, collections, Mystic Forge, vendors, or time gates
- a recipe cycle is detected
- the max recipe depth is reached
- the public API cannot return the needed recipe data

Use `data/recipe_overrides.json` for curated choices that the official API cannot decide safely:

```json
{
  "preferred_recipes_by_output": {
    "12345": 67890
  },
  "overrides": [
    {
      "item_id": 24680,
      "name": "Example Account-Bound Item",
      "type": "account_bound",
      "source_hint": "Earned through an achievement or collection.",
      "notes": "Track this manually instead of using Trading Post pricing."
    },
    {
      "name": "Example Mystic Forge Gift",
      "type": "mystic_forge",
      "source_hint": "Mystic Forge recipe verified manually.",
      "ingredients": [
        {
          "item_id": 19721,
          "name": "Glob of Ectoplasm",
          "amount": 250
        }
      ]
    }
  ]
}
```

Do not use override data to guess legendary requirements. Add curated entries only after verifying them. `item_id` is preferred over `name` in overrides because names can be ambiguous.

## Config Format

Each target can have `materials`, `currencies`, manual `steps`, and an optional `final_item_id` or `final_item_name`.

Use `final_item_id` for the finished legendary item. If that item is already in your Legendary Armory, the app marks the target as complete.

`final_item_id` is the most explicit option. `final_item_name` is supported when you prefer readable config:

```json
{
  "targets": [
    {
      "name": "Aurene Longbow",
      "enabled": true,
      "final_item_name": "Aurene's Flight"
    }
  ]
}
```

When `final_item_id` is present, `final_item_name` resolves to one exact item, or the target `name` resolves cleanly as a fallback, the recipe engine will try to resolve craftable requirements automatically. Any handwritten `materials`, `currencies`, or `steps` on the target are still kept and are added alongside API-resolved material data.

For targets that are intentionally not one final item, such as armor sets or rune/sigil sets, disable fallback lookup:

```json
{
  "targets": [
    {
      "name": "Obsidian Armor set",
      "enabled": true,
      "auto_resolve_final_item_from_name": false
    }
  ]
}
```

Use `steps` for manual checklist items that are not API materials or wallet currencies.

You can also reference a recipe template from the `templates/` folder. Template names are written without `.json`:

```json
{
  "targets": [
    {
      "template": "klobjarne_geirr",
      "enabled": true
    },
    {
      "template": "aurene_longbow",
      "enabled": true,
      "steps": [
        {
          "name": "Personal reminder: check alt account materials",
          "complete": false
        }
      ]
    }
  ]
}
```

Targets without `template` still work as fully manual targets:

```json
{
  "targets": [
    {
      "name": "My Legendary",
      "enabled": true,
      "final_item_id": 30684,
      "steps": [
        {
          "name": "Finish time-gated daily materials",
          "complete": false,
          "notes": "Update manually when the cooldowns are done."
        },
        {
          "name": "Craft final gift",
          "complete": false
        }
      ],
      "materials": [
        {
          "name": "Mystic Clover",
          "amount": 77
        },
        {
          "item_id": 19721,
          "name": "Glob of Ectoplasm",
          "amount": 250
        },
        {
          "name": "Obsidian Shard",
          "amount": 250,
          "source_hint": "Buy with Karma from vendors, or obtain from map currencies/reward tracks."
        }
      ],
      "currencies": [
        {
          "currency_id": 2,
          "name": "Karma",
          "amount": 500000,
          "source_hint": "Earn from events, daily Wizard's Vault objectives, and account boosts."
        }
      ]
    }
  ]
}
```

## Notes

- The script does not print your API key.
- The script sends the API key in an Authorization header, not in the URL.
- API calls use a 60-second timeout and retry temporary failures before giving a friendly error.
- Before scanning, the script checks `/v2/tokeninfo` and warns if the key is missing needed permissions.
- Legendary Armory unlocks come from `/v2/account/legendaryarmory`.
- Targets with `final_item_id` are marked complete when that item is already unlocked.
- Target status labels are `Complete`, `In progress`, `Partially resolved`, `Needs manual source data`, `Needs final item data`, or `Needs recipe data`.
- Targets with TODO recipe/checklist steps but no material or currency data are marked `Needs recipe data`.
- Normal output hides empty material/currency sections; use `--detailed` when you want to audit every section.
- Default recipe planning uses owned intermediate items first; use `--raw-materials` when you want full base-material expansion.
- Recipe templates live in `templates/`. If a template has TODO notes, verify the recipe before relying on exact quantities.
- Template targets can add extra `steps`, `materials`, or `currencies` in `legendary_goals.json`.
- Public item and official recipe API data is stored in `data/planner_reference.sqlite`.
- `recipe_cache.json` is legacy fallback cache data.
- Resolved item names, including `final_item_name`, are cached in `item_cache.json`.
- Curated recipe choices and manual stops live in `data/recipe_overrides.json`.
- Recipe engine warnings mean the app refused to guess. Verify the item, add a preferred recipe, or add an override entry.
- Manual `steps` are shown in the report but are not counted as item IDs or wallet currencies.
- Completed manual steps are shown only when you run with `--show-complete`.
- Optional `source_hint` text is shown under missing materials or currencies.
- Item counts combine material storage, bank slots, shared inventory slots, and every character's bag inventory.
- Wallet, material storage, bank, and shared inventory are fetched fresh every run.
- Character bag inventories are cached in `character_inventory_cache.json` and reused when a character's `age` value has not changed.
- Name-only materials must match one item clearly. If the name is missing or matches multiple items, the app will ask you to fix the spelling or use `item_id`.
- The reusable item name index is saved in `item_name_index.json` and can resume if a run is interrupted.
- During name lookup, successful names are saved to `item_cache.json` as they complete.
- Trading Post estimates use the lowest sell price as the buy-now price.
- Price lookups are cached in `price_cache.json` for 15 minutes.
- Account-bound or otherwise unpriced missing items are shown as `not priced`.
- Coin values are shown as gold/silver/copper, such as `123g 45s 67c`.
- Daily recommendations are simple rules for now, based on missing items, currencies, and estimated gold pressure.
- Wallet currencies stay separate from item counts because the API returns them as currencies instead of item stacks.
- The script does not count trading post listings or mail yet.
