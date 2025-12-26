#!/usr/bin/env python3
# scripts/run_scheduler.py
#
# OSRS Herblore Profit Scheduling Runner
# - Loads prices from: https://chisel.weirdgloop.org/gazproj/gazbot/os_dump.json
# - Loads recipes from: data/RecipeCatalog.json
# - Computes profit_per_craft + gp_per_hour using the locked rules from your prompt
# - Outputs:
#   - output/latest_run.md
#   - output/latest_run.json
# - Posts to Discord (optional) via env: DISCORD_WEBHOOK_URL
#
# Notes:
# - No external dependencies (urllib only).
# - Recipes containing "__MISSING__" / "__OCR_UNCERTAIN__" in required fields are marked invalid.
# - TABLE A includes ALL recipes (even invalid), sorted by gp_per_hour desc (invalid = bottom).
# - TABLE B includes only alerts where gp_per_hour > 3,000,000 AND xp_per_hour > 250,000 (and valid).

import json
import math
import os
import time
import datetime
import urllib.request
import urllib.error
from typing import Any, Dict, List, Optional, Tuple

API_URL = "https://chisel.weirdgloop.org/gazproj/gazbot/os_dump.json"

# Locked engine constants
GE_TAX = 0.02
TAX_MULTIPLIER = 1.0 - GE_TAX  # 0.98
AMULET_BONUS_DOSES = 0.15
GOGGLES_SECONDARY_MULT = 0.9

# Alert thresholds (KANONISK)
ALERT_GP_PER_HOUR = 3_000_000
ALERT_XP_PER_HOUR = 250_000

# Discord
DISCORD_CONTENT_LIMIT = 1900  # safe margin under 2000

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_PATH = os.path.join(ROOT_DIR, "data", "RecipeCatalog.json")
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")
LATEST_MD_PATH = os.path.join(OUTPUT_DIR, "latest_run.md")
LATEST_JSON_PATH = os.path.join(OUTPUT_DIR, "latest_run.json")


def is_marker_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and (value.startswith("__MISSING__") or value.startswith("__OCR_UNCERTAIN__")):
        return True
    return False


def safe_float(value: Any) -> Optional[float]:
    if is_marker_missing(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    # accept numeric strings
    if isinstance(value, str):
        try:
            return float(value.replace(",", ""))
        except Exception:
            return None
    return None


def safe_int(value: Any) -> Optional[int]:
    f = safe_float(value)
    if f is None:
        return None
    try:
        return int(f)
    except Exception:
        return None


def http_get_json(url: str, timeout: int = 60) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": "HerbloreScheduler/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw)


def build_name_to_price(dump_obj: Any) -> Dict[str, float]:
    """
    The WeirdGloop dump schema can change. This function tries several common shapes.
    Goal: name -> price (float)
    """
    name_to_price: Dict[str, float] = {}

    if not isinstance(dump_obj, dict):
        return name_to_price

    # Common containers to try
    containers: List[Any] = []
    for k in ("items", "data", "prices"):
        v = dump_obj.get(k)
        if isinstance(v, dict):
            containers.append(v)

    # Fallback: top-level could itself be item map
    if not containers:
        containers.append(dump_obj)

    # Candidate keys for name/price
    name_keys = ("name", "item_name")
    price_keys = ("price", "ge_price", "avg", "value", "high", "low")

    for container in containers:
        if not isinstance(container, dict):
            continue
        for _, item in container.items():
            if not isinstance(item, dict):
                continue

            name = None
            for nk in name_keys:
                if isinstance(item.get(nk), str):
                    name = item.get(nk)
                    break

            price = None
            for pk in price_keys:
                pv = item.get(pk)
                if isinstance(pv, (int, float)):
                    price = float(pv)
                    break

            if isinstance(name, str) and isinstance(price, float):
                name_to_price[name] = price
                name_to_price[name.lower()] = price


    return name_to_price


def price_of(name_to_price: Dict[str, float], item_name: str) -> Optional[float]:
    if not item_name or is_marker_missing(item_name):
        return None
    # Case-insensitive match (prevents "Armadyl Brew(4)" vs "Armadyl brew(4)" failures)
    return name_to_price.get(item_name) or name_to_price.get(item_name.lower())




def price_per_dose_of_output(name_to_price: Dict[str, float], output_item_name: str) -> Optional[float]:
    # Output is always Potion(4) in API name; per prompt: Price(output(4)) / 4
    p4 = price_of(name_to_price, output_item_name)
    if p4 is None:
        return None
    return p4 / 4.0


def compute_material_cost(
    name_to_price: Dict[str, float],
    mats: List[Dict[str, Any]],
    recipe_type: str,
    n: int,
    treat_base_as_dose_scaled_from_four: bool = False,
) -> Optional[float]:
    """
    Computes sum(Price(item) * qty) with special support:
    - If a material has IsDoseScaledFromFour=true, cost is (Price(item(4))/4) * N * qty
      (used when the base is a 4-dose potion that is consumed proportionally to N)
    - For normal materials: Price(item) * qty
    """
    total = 0.0

    for m in mats:
        item = m.get("ItemName")
        qty = safe_float(m.get("Quantity"))
        if is_marker_missing(item) or qty is None:
            return None

        is_dose_scaled = bool(m.get("IsDoseScaledFromFour", False))
        # Some prompt excerpts also mention DoseScaledFromFourPerDose; treat it as IsDoseScaledFromFour.
        if bool(m.get("DoseScaledFromFourPerDose", False)):
            is_dose_scaled = True

        if is_dose_scaled:
            # Price(item(4))/4 * N * qty
            p4 = price_of(name_to_price, item)
            if p4 is None:
                return None
            per_dose = p4 / 4.0
            total += per_dose * float(n) * float(qty)
        else:
            p = price_of(name_to_price, item)
            if p is None:
                return None
            total += p * float(qty)

    return total


def compute_recipe(
    name_to_price: Dict[str, float],
    recipe: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Returns a dict with computed fields and validity.
    """
    recipe_name = recipe.get("RecipeName", "")
    output_item_name = recipe.get("OutputItemName", "")
    recipe_type = recipe.get("RecipeType", "")
    n = safe_int(recipe.get("N"))
    goggles_allowed = bool(recipe.get("GogglesAllowed", False))

    xp_per_craft = safe_float(recipe.get("XP_per_craft"))
    xp_per_hour = safe_float(recipe.get("XP_per_hour"))

    base_mats = recipe.get("BaseMaterials") or []
    sec_mats = recipe.get("SecondaryMaterials") or []

    invalid_reasons: List[str] = []

    if not isinstance(base_mats, list) or not isinstance(sec_mats, list):
        invalid_reasons.append("Invalid materials list shape")
    if is_marker_missing(output_item_name):
        invalid_reasons.append("Missing OutputItemName")
    if not isinstance(base_mats, list) or not isinstance(sec_mats, list):
        invalid_reasons.append("Invalid materials list shape")
    if is_marker_missing(output_item_name):
        invalid_reasons.append("Missing OutputItemName")
    if n is None or n <= 0:
        invalid_reasons.append("Missing/invalid N")
    if xp_per_craft is None or xp_per_craft <= 0:
        invalid_reasons.append("Missing/invalid XP_per_craft")
    if xp_per_hour is None or xp_per_hour <= 0:
        invalid_reasons.append("Missing/invalid XP_per_hour")
    if n is None or n <= 0:
        invalid_reasons.append("Missing/invalid N")
    if xp_per_craft is None or xp_per_craft <= 0:
        invalid_reasons.append("Missing/invalid XP_per_craft")
    if xp_per_hour is None or xp_per_hour <= 0:
        invalid_reasons.append("Missing/invalid XP_per_hour")
        # Early price existence check (gives explicit missing item names)
    if not invalid_reasons:
        for m in base_mats:
            item = m.get("ItemName")
            if is_marker_missing(item):
                invalid_reasons.append("Missing base material ItemName")
                break
            if item.lower() not in name_to_price:
                invalid_reasons.append(f"Missing price for base material: '{item}'")
                break
    
    if not invalid_reasons:
        for m in sec_mats:
            item = m.get("ItemName")
            if is_marker_missing(item):
                invalid_reasons.append("Missing secondary material ItemName")
                break
            if item.lower() not in name_to_price:
                invalid_reasons.append(f"Missing price for secondary material: '{item}'")
                break


    # Expected doses (locked)
    expected_doses = None
    if n is not None:
        expected_doses = float(n) + AMULET_BONUS_DOSES

    # Costs
    base_cost = None
    secondary_cost = None
    total_cost = None

    if not invalid_reasons:
        base_cost = compute_material_cost(name_to_price, base_mats, recipe_type, n)
        if base_cost is None:
            invalid_reasons.append("Missing price for base materials")

        secondary_cost = compute_material_cost(name_to_price, sec_mats, recipe_type, n)
        if secondary_cost is None:
            invalid_reasons.append("Missing price for secondary materials")

        if not invalid_reasons and base_cost is not None and secondary_cost is not None:
            sec_mult = GOGGLES_SECONDARY_MULT if goggles_allowed else 1.0
            total_cost = base_cost + secondary_cost * sec_mult

    # Revenue
    price_per_dose = None
    revenue_before_tax = None
    revenue_after_tax = None
    if not invalid_reasons:
        price_per_dose = price_per_dose_of_output(name_to_price, output_item_name)
        if price_per_dose is None:
            invalid_reasons.append("Missing price for output item")
        else:
            revenue_before_tax = price_per_dose * expected_doses
            revenue_after_tax = revenue_before_tax * TAX_MULTIPLIER

    # Profit + rates
    profit_per_craft = None
    crafts_per_hour = None
    gp_per_hour = None

    if not invalid_reasons:
        profit_per_craft = revenue_after_tax - total_cost
        crafts_per_hour = xp_per_hour / xp_per_craft
        gp_per_hour = profit_per_craft * crafts_per_hour

    valid = len(invalid_reasons) == 0

    # Alerts (KANONISK: AND)
    is_alert = False
    if valid and gp_per_hour is not None and xp_per_hour is not None:
        is_alert = (gp_per_hour > ALERT_GP_PER_HOUR) and (xp_per_hour > ALERT_XP_PER_HOUR)

    return {
        "RecipeName": recipe_name,
        "OutputItemName": output_item_name,
        "RecipeType": recipe_type,
        "N": n,
        "XP_per_craft": xp_per_craft,
        "XP_per_hour": xp_per_hour,
        "crafts_per_hour": crafts_per_hour,
        "profit_per_craft": profit_per_craft,
        "gp_per_hour": gp_per_hour,
        "valid": valid,
        "invalid_reasons": invalid_reasons,
        "GogglesAllowed": goggles_allowed,
    }


def fmt_num(v: Optional[float], decimals: int = 2) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "N/A"
    if decimals == 0:
        return f"{int(round(v)):,}"
    return f"{v:,.{decimals}f}"


def markdown_table(rows: List[Dict[str, Any]], columns: List[Tuple[str, str]], decimals_map: Dict[str, int]) -> str:
    """
    columns: list of (field, header)
    decimals_map: field -> decimals
    """
    header = "| " + " | ".join([h for _, h in columns]) + " |\n"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |\n"

    lines = []
    for r in rows:
        vals = []
        for field, _ in columns:
            v = r.get(field)
            if field in ("RecipeName", "OutputItemName"):
                vals.append(str(v) if v is not None else "")
            elif field == "N":
                vals.append(str(v) if v is not None else "N/A")
            elif field in ("valid",):
                vals.append("true" if v else "false")
            else:
                dec = decimals_map.get(field, 2)
                vals.append(fmt_num(v, dec))
        lines.append("| " + " | ".join(vals) + " |")

    return header + sep + "\n".join(lines) + "\n"


def discord_post(webhook_url: str, content: str) -> None:
    chunks = [content[i:i + DISCORD_CONTENT_LIMIT] for i in range(0, len(content), DISCORD_CONTENT_LIMIT)]
    if not chunks:
        chunks = ["(empty)"]

    for chunk in chunks:
        payload = json.dumps({"content": chunk}).encode("utf-8")
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            # Simple 429 handling
            if e.code == 429:
                retry_after = 2
                try:
                    body = e.read().decode("utf-8")
                    j = json.loads(body)
                    retry_after = max(1, int(math.ceil(float(j.get("retry_after", 2)))))
                except Exception:
                    pass
                time.sleep(retry_after)
                with urllib.request.urlopen(req, timeout=20) as resp:
                    resp.read()
            else:
                # Do not crash the whole scheduler on Discord errors (e.g. 403 Forbidden).
                try:
                    body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    body = ""
                print(f"[Discord] HTTP {e.code} {e.reason}. Body: {body}")
                return



def main() -> int:
    run_ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")

    # Load recipe catalog
    if not os.path.exists(DATA_PATH):
        raise FileNotFoundError(f"Missing RecipeCatalog.json at: {DATA_PATH}")

    with open(DATA_PATH, "r", encoding="utf-8") as f:
        catalog = json.load(f)

    recipes = catalog.get("recipes")
    if not isinstance(recipes, list):
        raise ValueError("RecipeCatalog.json must contain top-level key: recipes[]")

    # Enforce Surge potion removal (prompt: must not be included)
    recipes = [r for r in recipes if str(r.get("RecipeName", "")).strip().lower() != "surge potion"]

    # Load prices
    dump = http_get_json(API_URL, timeout=90)
    name_to_price = build_name_to_price(dump)

    # Compute
    computed = [compute_recipe(name_to_price, r) for r in recipes]

    # Sort TABLE A by gp_per_hour desc, invalid bottom
    def sort_key(r: Dict[str, Any]) -> float:
        v = r.get("gp_per_hour")
        if r.get("valid") and isinstance(v, (int, float)):
            return float(v)
        return float("-inf")

    table_a = sorted(computed, key=sort_key, reverse=True)

    # TABLE B alerts (KANONISK AND), only valid
    table_b = [r for r in table_a if r.get("valid") and r.get("gp_per_hour") is not None and r.get("XP_per_hour") is not None
               and (r["gp_per_hour"] > ALERT_GP_PER_HOUR and r["XP_per_hour"] > ALERT_XP_PER_HOUR)]

    # Build markdown output
    cols = [
        ("RecipeName", "RecipeName"),
        ("OutputItemName", "OutputItemName"),
        ("N", "N"),
        ("XP_per_craft", "XP/craft"),
        ("XP_per_hour", "XP/h"),
        ("crafts_per_hour", "crafts/h"),
        ("profit_per_craft", "profit/craft"),
        ("gp_per_hour", "gp/h"),
    ]
    decimals = {
        "XP_per_craft": 2,
        "XP_per_hour": 0,
        "crafts_per_hour": 2,
        "profit_per_craft": 2,
        "gp_per_hour": 0,
    }

    md_parts: List[str] = []
    md_parts.append(f"# Herblore Scheduling Run\n\n- Timestamp (UTC): **{run_ts}**\n- Price source: `{API_URL}`\n")
    md_parts.append("## TABLE A — All recipes (sorted by gp_per_hour desc)\n")
    md_parts.append(markdown_table(table_a, cols, decimals))

    md_parts.append("## TABLE B — Alerts (gp_per_hour > 3M AND xp_per_hour > 250k)\n")
    if not table_b:
        md_parts.append("No alerts this run.\n")
    else:
        md_parts.append(markdown_table(table_b, cols, decimals))

    # Append invalid summary (useful for fixing catalog)
    invalid = [r for r in table_a if not r.get("valid")]
    if invalid:
        md_parts.append("## Invalid recipes (blocked by __MISSING__/__OCR_UNCERTAIN__ or missing prices)\n")
        for r in invalid:
            reasons = ", ".join(r.get("invalid_reasons") or [])
            md_parts.append(f"- **{r.get('RecipeName','')}** → {reasons}\n")

    latest_md = "".join(md_parts)

    # Write outputs
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(LATEST_MD_PATH, "w", encoding="utf-8") as f:
        f.write(latest_md)

    latest_json = {
        "timestamp_utc": run_ts,
        "price_source": API_URL,
        "table_a": table_a,
        "table_b": table_b,
    }
    with open(LATEST_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(latest_json, f, ensure_ascii=False, indent=2)

    # Print to stdout for Actions logs
    print(latest_md)

    # Discord post (optional)
    webhook = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if webhook:
        # Post a compact message: header + Alerts table (or "No alerts")
        compact: List[str] = []
        compact.append(f"**Herblore Scheduling Run** (UTC {run_ts})\n")
        if not table_b:
            compact.append("**TABLE B — Alerts:** No alerts this run.\n")
        else:
            compact.append("**TABLE B — Alerts:**\n")
            # Keep alert table only to avoid hitting 2000 chars
            compact.append(markdown_table(table_b, cols, decimals))

        # Also include top 10 from TABLE A for quick glance
        compact.append("**Top 10 (TABLE A):**\n")
        top10 = table_a[:10]
        # Make a tiny inline list
        for r in top10:
            gp = fmt_num(r.get("gp_per_hour"), 0)
            xp = fmt_num(r.get("XP_per_hour"), 0)
            name = r.get("RecipeName", "")
            compact.append(f"- {name}: gp/h {gp}, xp/h {xp}\n")

        discord_post(webhook, "".join(compact))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
