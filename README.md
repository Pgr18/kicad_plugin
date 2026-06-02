# KiCad Component Lookup Plugin

Python Action Plugin for KiCad that searches:

- `elitan.ru`
- `chipdip.ru`

and applies filters (manufacturer, package, max price, min stock, in-stock only).

## Features

- Search by part number or free text.
- Pulls component cards from both stores.
- Unified results table with source, title, SKU, stock, price, and URL.
- Double-click result to open product page in browser.
- Graceful fallback if one provider fails.

## Files

- `__init__.py` - plugin package entry point for KiCad.
- `component_lookup_plugin.py` - ActionPlugin UI and command flow.
- `providers.py` - site scraping/parsing and filter logic.

## Install

1. Find your KiCad user plugin directory:
   - Windows (typical): `%APPDATA%\kicad\8.0\scripting\plugins\`
2. Copy this whole folder (`kicad_plugin`) into that plugins directory.
3. Restart KiCad.
4. Open PCB Editor -> `Tools` -> `External Plugins` -> `Component lookup (elitan/electronshik)`.

## Usage

1. Enter part number (example: `ATMEGA328P-AU`) or keyword.
2. Optional filters:
   - Manufacturer
   - Package
   - Max price (RUB)
   - Min stock
   - Only in stock
3. Click **Search**.
4. Double-click a row to open product page.
5. To assign part number to PCB components:
   - Select one result row.
   - Select one or more footprints on PCB.
   - Optional: click **Create field on selected** to create the field first.
   - Click **Assign selected row**.
   - Plugin writes value to footprint property `Part_Number` (or custom field name you set).

## Notes

- Store markup can change; parsing in `providers.py` is written with fallbacks.
- If one source is temporarily unavailable, the plugin still shows results from the other source.
- Network requests use Python stdlib (`urllib`), so no external pip dependencies are required.
- Elitan can require authorization for full prices/stock. You can pass your logged-in browser cookie:
  - PowerShell: `$env:ELITAN_COOKIE="your_cookie_header_here"`
  - Then start KiCad from the same shell session.

