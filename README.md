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

Materials can use either `item_id` or an exact `name`. If you use a name, the app looks it up with `/v2/items` and saves the result in `item_cache.json` so future runs do less API work.

The first name lookup may take a while because the app builds `item_name_index.json` from the public item API. After that file exists, future name lookups are local and much faster.

## Run

```bash
python gw2_legendary_planner.py
```

To also show completed entries:

```bash
python gw2_legendary_planner.py --show-complete
```

To skip Trading Post price estimates:

```bash
python gw2_legendary_planner.py --no-prices
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

The report includes a `Recommended today` section. It looks at the first incomplete enabled target first, then gives shorter secondary notes for later targets.

## Config Format

Each target can have `materials`, `currencies`, and an optional `final_item_id`.

Use `final_item_id` for the finished legendary item. If that item is already in your Legendary Armory, the app marks the target as complete.

```json
{
  "targets": [
    {
      "name": "My Legendary",
      "enabled": true,
      "final_item_id": 30684,
      "materials": [
        {
          "name": "Mystic Clover",
          "amount": 77
        },
        {
          "item_id": 19721,
          "name": "Glob of Ectoplasm",
          "amount": 250
        }
      ],
      "currencies": [
        {
          "currency_id": 2,
          "name": "Karma",
          "amount": 500000
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
- Item counts combine material storage, bank slots, shared inventory slots, and every character's bag inventory.
- Wallet, material storage, bank, and shared inventory are fetched fresh every run.
- Character bag inventories are cached in `character_inventory_cache.json` and reused when a character's `age` value has not changed.
- Name-only materials must match one item clearly. If the name is missing or matches multiple items, the app will ask you to fix the spelling or use `item_id`.
- The reusable item name index is saved in `item_name_index.json` and can resume if a run is interrupted.
- During name lookup, successful names are saved to `item_cache.json` as they complete.
- Trading Post estimates use the lowest sell price as the buy-now price.
- Price lookups are cached in `price_cache.json` for 15 minutes.
- Account-bound or otherwise unpriced missing items are shown as `not priced`.
- Daily recommendations are simple rules for now, based on missing items, currencies, and estimated gold pressure.
- Wallet currencies stay separate from item counts because the API returns them as currencies instead of item stacks.
- The script does not count trading post listings or mail yet.
