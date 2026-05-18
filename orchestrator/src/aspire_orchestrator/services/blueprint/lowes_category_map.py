from __future__ import annotations

LOWES_CATEGORY_URLS: dict[str, str] = {
    "appliance_dishwasher_builtin": "https://www.lowes.com/pl/dishwashers/built-in-dishwashers/4294857924",
    "appliance_dishwasher_portable": "https://www.lowes.com/pl/dishwashers/portable-dishwashers/4294857923",
    "appliance_refrigerator_standard": "https://www.lowes.com/pl/refrigerators/french-door-refrigerators/4294857849",
    "appliance_range_electric": "https://www.lowes.com/pl/ranges/electric-ranges/4294857899",
    "appliance_range_gas": "https://www.lowes.com/pl/ranges/gas-ranges/4294857898",
    "appliance_microwave_otr": "https://www.lowes.com/pl/microwaves/over-the-range-microwaves/4294857909",
    "appliance_washer_topload": "https://www.lowes.com/pl/washers/top-load-washers/4294857873",
    "appliance_washer_frontload": "https://www.lowes.com/pl/washers/front-load-washers/4294857874",
    "appliance_dryer_electric": "https://www.lowes.com/pl/dryers/electric-dryers/4294857881",
    "appliance_dryer_gas": "https://www.lowes.com/pl/dryers/gas-dryers/4294857880",
    "fixture_water_heater_tank_gas": "https://www.lowes.com/pl/water-heaters/gas-tank-water-heaters/4294608594",
    "fixture_water_heater_tankless": "https://www.lowes.com/pl/water-heaters/tankless-water-heaters/4294608595",
    "flooring_hardwood": "https://www.lowes.com/pl/flooring/hardwood-flooring/4294408849",
    "flooring_laminate": "https://www.lowes.com/pl/flooring/laminate-flooring/4294408853",
    "flooring_vinyl_plank": "https://www.lowes.com/pl/flooring/vinyl-plank-flooring/4294408855",
    "flooring_tile_ceramic": "https://www.lowes.com/pl/tile/ceramic-tile/4294609137",
    "flooring_tile_porcelain": "https://www.lowes.com/pl/tile/porcelain-tile/4294609138",
    "lumber_dimensional_softwood": "https://www.lowes.com/pl/dimensional-lumber/4294609171",
    "lumber_plywood_sheathing": "https://www.lowes.com/pl/plywood/structural-plywood-sheathing/4294608755",
    "lumber_osb_sheathing": "https://www.lowes.com/pl/oriented-strand-board-osb/4294608754",
    "drywall_gwb_standard": "https://www.lowes.com/pl/drywall/4294609083",
    "drywall_gwb_moisture_resistant": "https://www.lowes.com/pl/drywall/moisture-resistant-drywall/4294609084",
    "roofing_asphalt_shingles": "https://www.lowes.com/pl/roofing/asphalt-shingles/4294608681",
    "roofing_underlayment": "https://www.lowes.com/pl/roofing/roofing-underlayment/4294608682",
    "door_interior_prehung": "https://www.lowes.com/pl/interior-doors/prehung-interior-doors/4294608507",
    "door_exterior_entry": "https://www.lowes.com/pl/exterior-doors/entry-doors/4294608508",
    "window_double_hung": "https://www.lowes.com/pl/windows/double-hung-windows/4294608540",
    "insulation_batt_fiberglass": "https://www.lowes.com/pl/insulation/fiberglass-batt-insulation/4294608609",
    "insulation_rigid_foam": "https://www.lowes.com/pl/insulation/rigid-foam-insulation/4294608610",
    "paint_interior_latex": "https://www.lowes.com/pl/paint/interior-paint/4294608805",
    "paint_exterior": "https://www.lowes.com/pl/paint/exterior-paint/4294608806",
}

LOWES_CATEGORY_ALIASES: dict[str, str] = {
    "dishwasher": "appliance_dishwasher_builtin",
    "built-in dishwasher": "appliance_dishwasher_builtin",
    "builtin dishwasher": "appliance_dishwasher_builtin",
    "refrigerator": "appliance_refrigerator_standard",
    "fridge": "appliance_refrigerator_standard",
    "electric range": "appliance_range_electric",
    "gas range": "appliance_range_gas",
    "oven": "appliance_range_electric",
    "microwave": "appliance_microwave_otr",
    "washer": "appliance_washer_topload",
    "washing machine": "appliance_washer_frontload",
    "dryer": "appliance_dryer_electric",
    "water heater": "fixture_water_heater_tank_gas",
    "hot water heater": "fixture_water_heater_tank_gas",
    "tankless water heater": "fixture_water_heater_tankless",
    "hardwood floor": "flooring_hardwood",
    "hardwood flooring": "flooring_hardwood",
    "laminate floor": "flooring_laminate",
    "vinyl plank": "flooring_vinyl_plank",
    "lvp": "flooring_vinyl_plank",
    "ceramic tile": "flooring_tile_ceramic",
    "porcelain tile": "flooring_tile_porcelain",
    "2x4": "lumber_dimensional_softwood",
    "2x6": "lumber_dimensional_softwood",
    "dimensional lumber": "lumber_dimensional_softwood",
    "framing lumber": "lumber_dimensional_softwood",
    "plywood": "lumber_plywood_sheathing",
    "osb": "lumber_osb_sheathing",
    "drywall": "drywall_gwb_standard",
    "sheetrock": "drywall_gwb_standard",
    "gwb": "drywall_gwb_standard",
    "moisture resistant drywall": "drywall_gwb_moisture_resistant",
    "greenboard": "drywall_gwb_moisture_resistant",
    "shingles": "roofing_asphalt_shingles",
    "roofing shingles": "roofing_asphalt_shingles",
    "roofing felt": "roofing_underlayment",
    "interior door": "door_interior_prehung",
    "exterior door": "door_exterior_entry",
    "entry door": "door_exterior_entry",
    "double hung window": "window_double_hung",
    "window": "window_double_hung",
    "fiberglass insulation": "insulation_batt_fiberglass",
    "batt insulation": "insulation_batt_fiberglass",
    "rigid foam": "insulation_rigid_foam",
    "interior paint": "paint_interior_latex",
    "exterior paint": "paint_exterior",
    "paint": "paint_interior_latex",
}


def resolve_lowes_url(line_item: str) -> str | None:
    """Return Lowes category URL for line_item, or None. Law 3: never raises."""
    if not line_item:
        return None
    needle = line_item.strip().lower()
    if needle in LOWES_CATEGORY_URLS:
        return LOWES_CATEGORY_URLS[needle]
    if needle in LOWES_CATEGORY_ALIASES:
        return LOWES_CATEGORY_URLS.get(LOWES_CATEGORY_ALIASES[needle])
    best_alias: str | None = None
    best_len = 0
    for alias, slug in LOWES_CATEGORY_ALIASES.items():
        if alias in needle and len(alias) > best_len:
            best_alias = slug
            best_len = len(alias)
    if best_alias:
        return LOWES_CATEGORY_URLS.get(best_alias)
    return None