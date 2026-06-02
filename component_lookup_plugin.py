"""KiCad action plugin for searching electronic components on Russian stores."""

from __future__ import annotations

import json
import traceback
import webbrowser
from dataclasses import dataclass, field
from typing import List, Optional

import pcbnew
import wx

from providers import SearchFilters, SearchService


@dataclass
class SearchResultRow:
    source: str
    title: str
    sku: str
    manufacturer: str
    package: str
    stock: int
    price_rub: Optional[float]
    url: str

    def part_number(self) -> str:
        if "@" in self.title:
            return self.title.split("@", 1)[0].strip()
        if self.sku:
            return self.sku
        return self.title.strip()


@dataclass
class PluginState:
    service: SearchService = field(default_factory=SearchService)
    last_query: str = ""
    last_filters: SearchFilters = field(default_factory=SearchFilters)


class ResultsFrame(wx.Frame):
    def __init__(self, parent: wx.Window, state: PluginState):
        super().__init__(parent, title="KiCad Component Lookup", size=(1100, 650))
        self.state = state
        self.rows: List[SearchResultRow] = []
        self._build_ui()

    def _build_ui(self) -> None:
        panel = wx.Panel(self)
        root = wx.BoxSizer(wx.VERTICAL)

        query_box = wx.BoxSizer(wx.HORIZONTAL)
        query_box.Add(wx.StaticText(panel, label="Part number / keyword"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.query_input = wx.TextCtrl(panel, value=self.state.last_query)
        query_box.Add(self.query_input, 1, wx.ALL | wx.EXPAND, 5)

        self.search_button = wx.Button(panel, label="Search")
        self.search_button.Bind(wx.EVT_BUTTON, self._on_search)
        query_box.Add(self.search_button, 0, wx.ALL, 5)

        self.reset_filters_button = wx.Button(panel, label="Reset filters")
        self.reset_filters_button.Bind(wx.EVT_BUTTON, self._on_reset_filters)
        query_box.Add(self.reset_filters_button, 0, wx.ALL, 5)

        root.Add(query_box, 0, wx.EXPAND)

        filters_box = wx.StaticBoxSizer(wx.VERTICAL, panel, "Filters")
        grid = wx.FlexGridSizer(cols=6, vgap=6, hgap=8)
        grid.AddGrowableCol(1, 1)
        grid.AddGrowableCol(3, 1)
        grid.AddGrowableCol(5, 1)

        self.manufacturer_input = wx.TextCtrl(panel, value=self.state.last_filters.manufacturer or "")
        self.package_input = wx.TextCtrl(panel, value=self.state.last_filters.package or "")
        self.max_price_input = wx.TextCtrl(
            panel,
            value="" if self.state.last_filters.max_price_rub is None else str(self.state.last_filters.max_price_rub),
        )
        self.min_stock_input = wx.TextCtrl(panel, value=str(self.state.last_filters.min_stock))
        self.only_in_stock = wx.CheckBox(panel, label="Only in stock")
        self.only_in_stock.SetValue(self.state.last_filters.only_in_stock)

        grid.Add(wx.StaticText(panel, label="Manufacturer"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.manufacturer_input, 1, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="Package"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.package_input, 1, wx.EXPAND)
        grid.Add(wx.StaticText(panel, label="Max price (RUB)"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.max_price_input, 1, wx.EXPAND)

        grid.Add(wx.StaticText(panel, label="Min stock"), 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(self.min_stock_input, 1, wx.EXPAND)
        grid.Add(self.only_in_stock, 0, wx.ALIGN_CENTER_VERTICAL)
        grid.Add(wx.StaticText(panel, label=""), 0)
        grid.Add(wx.StaticText(panel, label=""), 0)
        grid.Add(wx.StaticText(panel, label=""), 0)

        filters_box.Add(grid, 1, wx.ALL | wx.EXPAND, 8)
        root.Add(filters_box, 0, wx.ALL | wx.EXPAND, 5)

        self.result_list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.BORDER_SUNKEN)
        columns = [
            ("Source", 100),
            ("Title", 280),
            ("SKU", 120),
            ("Manufacturer", 130),
            ("Package", 110),
            ("Stock", 80),
            ("Price (RUB)", 100),
            ("URL", 250),
        ]
        for i, (name, width) in enumerate(columns):
            self.result_list.InsertColumn(i, name, width=width)
        self.result_list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_open_url)
        root.Add(self.result_list, 1, wx.ALL | wx.EXPAND, 5)

        assign_box = wx.StaticBoxSizer(wx.HORIZONTAL, panel, "Assign to selected PCB component(s)")
        assign_box.Add(wx.StaticText(panel, label="Target field"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.target_field_input = wx.TextCtrl(panel, value="Part_Number", size=(150, -1))
        assign_box.Add(self.target_field_input, 0, wx.ALL, 5)
        self.allow_value_fallback = wx.CheckBox(panel, label="Allow Value fallback")
        self.allow_value_fallback.SetValue(False)
        assign_box.Add(self.allow_value_fallback, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)

        # Новые элементы для цены
        assign_box.Add(wx.StaticText(panel, label="Price field"), 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)
        self.price_field_input = wx.TextCtrl(panel, value="Price", size=(150, -1))
        assign_box.Add(self.price_field_input, 0, wx.ALL, 5)
        self.assign_price_checkbox = wx.CheckBox(panel, label="Assign price")
        self.assign_price_checkbox.SetValue(True)
        assign_box.Add(self.assign_price_checkbox, 0, wx.ALL | wx.ALIGN_CENTER_VERTICAL, 5)

        self.assign_button = wx.Button(panel, label="Assign selected row")
        self.assign_button.Bind(wx.EVT_BUTTON, self._on_assign_part_number)
        assign_box.Add(self.assign_button, 0, wx.ALL, 5)

        self.create_field_button = wx.Button(panel, label="Create field on selected")
        self.create_field_button.Bind(wx.EVT_BUTTON, self._on_create_field_only)
        assign_box.Add(self.create_field_button, 0, wx.ALL, 5)
        root.Add(assign_box, 0, wx.ALL | wx.EXPAND, 5)


        self.status_label = wx.StaticText(panel, label="Ready")
        root.Add(self.status_label, 0, wx.ALL | wx.EXPAND, 5)


        panel.SetSizer(root)

    def _read_filters(self) -> SearchFilters:
        max_price = self.max_price_input.GetValue().strip()
        min_stock = self.min_stock_input.GetValue().strip()
        return SearchFilters(
            manufacturer=self.manufacturer_input.GetValue().strip() or None,
            package=self.package_input.GetValue().strip() or None,
            max_price_rub=float(max_price) if max_price else None,
            min_stock=int(min_stock) if min_stock else 0,
            only_in_stock=self.only_in_stock.GetValue(),
        )

    def _on_search(self, _event: wx.CommandEvent) -> None:
        query = self.query_input.GetValue().strip()
        if not query:
            wx.MessageBox("Please enter a part number or keyword.", "Input required", wx.OK | wx.ICON_INFORMATION)
            return

        try:
            filters = self._read_filters()
        except ValueError:
            wx.MessageBox("Max price must be a number and min stock must be an integer.", "Invalid filters", wx.OK)
            return

        self.search_button.Disable()
        self.status_label.SetLabel("Searching elitan.ru and electronshik.ru...")
        wx.YieldIfNeeded()

        try:
            results, diagnostics = self.state.service.search_with_diagnostics(query, filters)
            self.state.last_query = query
            self.state.last_filters = filters
            self.rows = [SearchResultRow(**item.to_dict()) for item in results]
            self._render_results(diagnostics)
        except Exception as exc:  # pylint: disable=broad-except
            wx.MessageBox(
                f"Search failed: {exc}\n\n{traceback.format_exc()}",
                "Plugin error",
                wx.OK | wx.ICON_ERROR,
            )
        finally:
            self.search_button.Enable()

    def _render_results(self, diagnostics: Optional[dict] = None) -> None:
        self.result_list.DeleteAllItems()
        for row in self.rows:
            idx = self.result_list.InsertItem(self.result_list.GetItemCount(), row.source)
            self.result_list.SetItem(idx, 1, row.title)
            self.result_list.SetItem(idx, 2, row.sku)
            self.result_list.SetItem(idx, 3, row.manufacturer)
            self.result_list.SetItem(idx, 4, row.package)
            self.result_list.SetItem(idx, 5, str(row.stock))
            self.result_list.SetItem(idx, 6, "" if row.price_rub is None else f"{row.price_rub:.2f}")
            self.result_list.SetItem(idx, 7, row.url)

        if diagnostics:
            by_source = diagnostics.get("by_source", {})
            source_info = ", ".join(f"{name}: {count}" for name, count in by_source.items()) or "no sources"
            self.status_label.SetLabel(
                f"Found {len(self.rows)} result(s) after filters. Raw by source -> {source_info}."
            )
        else:
            self.status_label.SetLabel(f"Found {len(self.rows)} result(s). Double-click row to open product page.")

    def _on_open_url(self, event: wx.ListEvent) -> None:
        idx = event.GetIndex()
        if 0 <= idx < len(self.rows):
            webbrowser.open(self.rows[idx].url)

    def _on_assign_part_number(self, _event: wx.CommandEvent) -> None:
        result_idx = self.result_list.GetFirstSelected()
        if result_idx < 0 or result_idx >= len(self.rows):
            wx.MessageBox("Select one row in search results first.", "Assignment", wx.OK | wx.ICON_INFORMATION)
            return

        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox("No board is open in PCB Editor.", "Assignment", wx.OK | wx.ICON_ERROR)
            return

        selected = [fp for fp in board.GetFootprints() if fp.IsSelected()]
        if not selected:
            wx.MessageBox("Select one or more footprints on PCB before assigning.", "Assignment", wx.OK | wx.ICON_INFORMATION)
            return

        row = self.rows[result_idx]
        part_number = row.part_number()
        target_field = self.target_field_input.GetValue().strip() or "Part_Number"
        allow_value_fallback = self.allow_value_fallback.GetValue()

        # Настройки для цены
        price_field_name = self.price_field_input.GetValue().strip() or "Price"
        assign_price = self.assign_price_checkbox.GetValue()
        price_str = f"{row.price_rub:.2f}" if row.price_rub is not None else ""

        updated = 0
        created_fields = 0
        method_set_property = 0
        method_set_properties = 0
        method_set_field_by_name = 0
        method_set_field_text = 0
        method_set_existing_field_text = 0
        fallback_value_updates = 0

        price_updated = 0
        price_created_fields = 0

        for fp in selected:
            # --- Part Number ---
            if _ensure_footprint_field_exists(fp, target_field):
                created_fields += 1
            assign_method = _assign_footprint_field(fp, target_field, part_number, allow_value_fallback)
            if assign_method == "set_field_by_name":
                updated += 1
                method_set_field_by_name += 1
            elif assign_method == "set_field_text":
                updated += 1
                method_set_field_text += 1
            elif assign_method == "set_existing_field_text":
                updated += 1
                method_set_existing_field_text += 1
            elif assign_method == "set_property":
                updated += 1
                method_set_property += 1
            elif assign_method == "set_properties":
                updated += 1
                method_set_properties += 1
            elif assign_method == "set_value":
                updated += 1
                fallback_value_updates += 1

            # --- Price ---
            if assign_price:
                if _ensure_footprint_field_exists(fp, price_field_name):
                    price_created_fields += 1
                # Для цены fallback на Value не используем (передаём False)
                price_method = _assign_footprint_field(fp, price_field_name, price_str, False)
                if price_method != "failed":
                    price_updated += 1

        if updated == 0 and (not assign_price or price_updated == 0):
            wx.MessageBox(
                "Could not write custom field(s) on selected footprints.\n"
                "Your KiCad API may not expose footprint custom-field setters in PCB editor.\n"
                "Enable 'Allow Value fallback' only if you want to store part number in Value.",
                "Assignment",
                wx.OK | wx.ICON_ERROR,
            )
            return

        pcbnew.Refresh()
        note = (
            f"Assigned '{part_number}' to {updated} footprint(s) in field '{target_field}'. "
            f"[Created fields: {created_fields}, "
            f"SetFieldByName: {method_set_field_by_name}, SetFieldText: {method_set_field_text}, "
            f"SetExistingField: {method_set_existing_field_text}, SetProperty: {method_set_property}, "
            f"SetProperties: {method_set_properties}, Value fallback: {fallback_value_updates}]"
        )
        if assign_price:
            note += f" Price assigned to {price_updated} footprint(s) in field '{price_field_name}' (created {price_created_fields} field(s))."
        if fallback_value_updates > 0:
            note += " For these footprints, custom fields are not available via current API."
        self.status_label.SetLabel(note)

    def _on_create_field_only(self, _event: wx.CommandEvent) -> None:
        board = pcbnew.GetBoard()
        if board is None:
            wx.MessageBox("No board is open in PCB Editor.", "Create field", wx.OK | wx.ICON_ERROR)
            return

        selected = [fp for fp in board.GetFootprints() if fp.IsSelected()]
        if not selected:
            wx.MessageBox("Select one or more footprints on PCB first.", "Create field", wx.OK | wx.ICON_INFORMATION)
            return

        target_field = self.target_field_input.GetValue().strip() or "Part_Number"
        created = 0
        already_exists = 0
        failed = 0

        for fp in selected:
            if _footprint_has_field(fp, target_field):
                already_exists += 1
                continue
            if _ensure_footprint_field_exists(fp, target_field):
                created += 1
            else:
                failed += 1

        pcbnew.Refresh()
        self.status_label.SetLabel(
            f"Field '{target_field}': created {created}, already existed {already_exists}, failed {failed}."
        )

        if failed > 0:
            wx.MessageBox(
                "Some footprints could not get a new custom field via this KiCad API.\n"
                "Those footprints can still be updated if the field already exists.",
                "Create field result",
                wx.OK | wx.ICON_INFORMATION,
            )

    def _on_reset_filters(self, _event: wx.CommandEvent) -> None:
        self.manufacturer_input.SetValue("")
        self.package_input.SetValue("")
        self.max_price_input.SetValue("")
        self.min_stock_input.SetValue("0")
        self.only_in_stock.SetValue(False)
        self.status_label.SetLabel("Filters reset. Run search again.")


class ComponentLookupPlugin(pcbnew.ActionPlugin):
    def defaults(self) -> None:
        self.name = "Component lookup (elitan/electronshik)"
        self.category = "BOM tools"
        self.description = "Search component availability and prices with filters"
        self.show_toolbar_button = True
        self.icon_file_name = ""

    def Run(self) -> None:
        # In different KiCad versions the board object may not expose UI chain
        # methods like GetScreen/GetCanvas. Resolve a parent frame via wx instead.
        parent = _resolve_kicad_parent()
        dialog = ResultsFrame(parent, PluginState())
        dialog.Centre()
        dialog.Show()
        # Keep a reference to avoid premature garbage collection.
        self._dialog = dialog


def register_plugin() -> None:
    ComponentLookupPlugin().register()


def _ensure_footprint_field_exists(footprint: pcbnew.FOOTPRINT, field_name: str) -> bool:
    if _footprint_has_field(footprint, field_name):
        return False

    # Try property map creation first (usually works for custom fields).
    try:
        if hasattr(footprint, "GetProperties") and hasattr(footprint, "SetProperties"):
            props = footprint.GetProperties()
            if props is None:
                props = {}
            if not isinstance(props, dict):
                props = dict(props)
            props[field_name] = props.get(field_name, "")
            footprint.SetProperties(props)
            return _footprint_has_field(footprint, field_name)
    except Exception:  # pylint: disable=broad-except
        pass

    # Alternative property setter path.
    try:
        if hasattr(footprint, "SetProperty"):
            footprint.SetProperty(field_name, "")
            return _footprint_has_field(footprint, field_name)
    except Exception:  # pylint: disable=broad-except
        pass

    return False


def _footprint_has_field(footprint: pcbnew.FOOTPRINT, field_name: str) -> bool:
    # Fields collection path.
    try:
        if hasattr(footprint, "GetFields"):
            fields = footprint.GetFields()
            if fields is not None:
                for fld in fields:
                    try:
                        if hasattr(fld, "GetName") and fld.GetName() == field_name:
                            return True
                    except Exception:  # pylint: disable=broad-except
                        continue
    except Exception:  # pylint: disable=broad-except
        pass

    # Property map path.
    try:
        if hasattr(footprint, "GetProperties"):
            props = footprint.GetProperties()
            if props is None:
                return False
            if isinstance(props, dict):
                return field_name in props
            return field_name in dict(props)
    except Exception:  # pylint: disable=broad-except
        pass
    return False


def _assign_footprint_field(footprint: pcbnew.FOOTPRINT, field_name: str, value: str, allow_value_fallback: bool) -> str:
    # 0) Newer KiCad builds may expose direct field-name setter.
    try:
        if hasattr(footprint, "SetFieldByName"):
            footprint.SetFieldByName(field_name, value)
            return "set_field_by_name"
    except Exception:  # pylint: disable=broad-except
        pass

    # 0.1) Some builds expose generic field text setter.
    try:
        if hasattr(footprint, "SetFieldText"):
            footprint.SetFieldText(field_name, value)
            return "set_field_text"
    except Exception:  # pylint: disable=broad-except
        pass

    # 0.2) Try field collection API if present.
    try:
        if hasattr(footprint, "GetFields"):
            fields = footprint.GetFields()
            if fields is not None:
                for fld in fields:
                    try:
                        name = fld.GetName() if hasattr(fld, "GetName") else ""
                        if name == field_name:
                            if hasattr(fld, "SetText"):
                                fld.SetText(value)
                                return "set_existing_field_text"
                    except Exception:  # pylint: disable=broad-except
                        continue
    except Exception:  # pylint: disable=broad-except
        pass

    # 1) Most direct API in KiCad builds that expose per-footprint properties.
    try:
        if hasattr(footprint, "SetProperty"):
            footprint.SetProperty(field_name, value)
            return "set_property"
    except Exception:  # pylint: disable=broad-except
        pass

    # 2) Some builds expose property map API; update map and write back.
    try:
        if hasattr(footprint, "GetProperties") and hasattr(footprint, "SetProperties"):
            props = footprint.GetProperties()
            if props is None:
                props = {}
            if not isinstance(props, dict):
                props = dict(props)
            props[field_name] = value
            footprint.SetProperties(props)
            return "set_properties"
    except Exception:  # pylint: disable=broad-except
        pass

    # 3) Optional fallback: assign footprint Value text.
    if allow_value_fallback:
        try:
            footprint.SetValue(value)
            return "set_value"
        except Exception:  # pylint: disable=broad-except
            return "failed"
    return "failed"


def _resolve_kicad_parent() -> Optional[wx.Window]:
    app = wx.GetApp()
    if not app:
        return None

    top_windows = [w for w in wx.GetTopLevelWindows() if isinstance(w, wx.Window) and w.IsShown()]
    if not top_windows:
        return None

    # Prefer currently focused window, otherwise first visible top-level window.
    focused = wx.Window.FindFocus()
    if focused:
        top = focused.GetTopLevelParent()
        if top:
            return top
    return top_windows[0]


if __name__ == "__main__":
    # Manual local test helper outside KiCad:
    sample = SearchService().search("ATmega328P", SearchFilters(min_stock=10, only_in_stock=True))
    print(json.dumps([x.to_dict() for x in sample], ensure_ascii=False, indent=2))
