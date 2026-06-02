"""Data providers and filter logic for component lookup plugin."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple


USER_AGENT = "KiCadComponentLookup/1.0 (+https://kicad.org/)"


@dataclass
class SearchFilters:
    manufacturer: Optional[str] = None
    package: Optional[str] = None
    max_price_rub: Optional[float] = None
    min_stock: int = 0
    only_in_stock: bool = False


@dataclass
class PartResult:
    source: str
    title: str
    sku: str
    manufacturer: str
    package: str
    stock: int
    price_rub: Optional[float]
    url: str

    def to_dict(self) -> Dict[str, object]:
        return {
            "source": self.source,
            "title": self.title,
            "sku": self.sku,
            "manufacturer": self.manufacturer,
            "package": self.package,
            "stock": self.stock,
            "price_rub": self.price_rub,
            "url": self.url,
        }


class BaseProvider:
    source_name = ""
    base_url = ""

    def search(self, query: str) -> List[PartResult]:
        raise NotImplementedError

    @staticmethod
    def _http_get(url: str, extra_headers: Optional[Dict[str, str]] = None) -> str:
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/json",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.6",
            "Referer": "https://www.elitan.ru/",
        }
        if extra_headers:
            headers.update(extra_headers)
        req = urllib.request.Request(
            url,
            headers=headers,
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            body = resp.read()
            content_type = resp.headers.get("Content-Type", "")

        charset_match = re.search(r"charset=([a-zA-Z0-9_\-]+)", content_type)
        charsets = []
        if charset_match:
            charsets.append(charset_match.group(1))
        charsets.extend(["utf-8", "cp1251", "windows-1251"])
        for charset in charsets:
            try:
                return body.decode(charset)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", errors="ignore")

    @staticmethod
    def _extract_number(value: str) -> Optional[float]:
        cleaned = re.sub(r"[^0-9,\.]", "", value).replace(",", ".")
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None


class ElitanProvider(BaseProvider):
    source_name = "elitan.ru"
    base_url = "https://www.elitan.ru"

    def __init__(self) -> None:
        # Copy full browser cookie to see authorized pricing:
        #   set ELITAN_COOKIE=...
        self.auth_cookie = os.getenv("ELITAN_COOKIE", "").strip()

    def search(self, query: str) -> List[PartResult]:
        headers = {"Cookie": self.auth_cookie} if self.auth_cookie else None
        json_results = self._search_index_json(query, headers)
        if json_results:
            return _dedupe_results(json_results)

        quoted = urllib.parse.quote(query)
        search_urls = [
            f"{self.base_url}/price/index.php?find={quoted}&seenform=y",
            f"{self.base_url}/price/index.php?find={quoted}",
            f"{self.base_url}/search/?q={quoted}",
        ]

        results: List[PartResult] = []

        for url in search_urls:
            html = self._http_get(url, extra_headers=headers)

            table_results = self._extract_table_items(html)
            if table_results:
                results.extend(table_results)
                continue

            # Fallback parser for alternative page format.
            items = self._extract_json_ld_items(html)
            for item in items:
                name = item.get("name", "")
                offer = item.get("offers", {})
                price = self._extract_number(str(offer.get("price", "")))
                sku = item.get("sku", "") or item.get("mpn", "")
                availability = str(offer.get("availability", "")).lower()
                in_stock = "instock" in availability
                product_url = item.get("url") or url
                results.append(
                    PartResult(
                        source=self.source_name,
                        title=name,
                        sku=sku,
                        manufacturer=item.get("brand", {}).get("name", "") if isinstance(item.get("brand"), dict) else "",
                        package=self._guess_package(name),
                        stock=1 if in_stock else 0,
                        price_rub=price,
                        url=product_url,
                    )
                )

        return _dedupe_results(results)

    def _search_index_json(self, query: str, headers: Optional[Dict[str, str]]) -> List[PartResult]:
        time_mark = "1"
        url = (
            f"{self.base_url}/price/index_json.php?"
            f"t={time_mark}&find={urllib.parse.quote(query)}&delay=-1&mfg=all&seenform=y"
        )
        body = self._http_get(url, extra_headers=headers)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return []

        items = payload.get("items", {})
        data = items.get("data", []) if isinstance(items, dict) else []
        if not isinstance(data, list):
            return []

        results: List[PartResult] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            partname = _strip_tags(str(raw.get("partname_full") or raw.get("partname") or ""))
            sku = str(raw.get("partclear") or raw.get("artik_original") or raw.get("artik") or "")
            manufacturer = str(raw.get("namemfg") or "")
            package = str(raw.get("housing") or "")
            stock = _safe_int(raw.get("count_stock"))
            price = self._extract_number(str(raw.get("min_price") or ""))
            ntovara = str(raw.get("Ntovara") or "")
            item_url = f"{self.base_url}/price/item{ntovara}" if ntovara.isdigit() else (
                f"{self.base_url}/price/index.php?find={urllib.parse.quote(query)}"
            )

            results.append(
                PartResult(
                    source=self.source_name,
                    title=partname or sku or query,
                    sku=sku or _extract_like_part_number(partname),
                    manufacturer=manufacturer,
                    package=package or self._guess_package(partname),
                    stock=stock,
                    price_rub=price,
                    url=item_url,
                )
            )
        return results

    def _extract_table_items(self, html: str) -> List[PartResult]:
        results: List[PartResult] = []
        # New Elitan layout contains links like /price/item123456 in result rows.
        for match in re.finditer(r'href="(/price/item\d+)"', html, flags=re.I):
            href = match.group(1)
            left = max(0, match.start() - 650)
            right = min(len(html), match.end() + 1200)
            chunk = html[left:right]

            code_mfg = re.search(r">([A-Za-z0-9\-\._/]+)@([A-Za-z0-9\-\._/]+)<", chunk)
            title = f"{code_mfg.group(1)}@{code_mfg.group(2)}" if code_mfg else _strip_tags(chunk)[:120]
            sku = code_mfg.group(1) if code_mfg else _extract_like_part_number(title)
            manufacturer = code_mfg.group(2) if code_mfg else ""

            price_match = re.search(r"([0-9]+(?:[.,][0-9]+)?)\s*(?:р\.|руб)", chunk, flags=re.I)
            stock_match = re.search(r"([0-9][0-9\s]{0,12})\s*шт", chunk, flags=re.I)
            price = self._extract_number(price_match.group(1)) if price_match else None
            stock = int(re.sub(r"\s+", "", stock_match.group(1))) if stock_match else 0
            full_url = urllib.parse.urljoin(self.base_url, href)

            results.append(
                PartResult(
                    source=self.source_name,
                    title=title,
                    sku=sku,
                    manufacturer=manufacturer,
                    package=self._guess_package(title),
                    stock=stock,
                    price_rub=price,
                    url=full_url,
                )
            )
        return results

    @staticmethod
    def _extract_json_ld_items(html: str) -> Iterable[Dict[str, object]]:
        scripts = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, flags=re.S | re.I)
        for script in scripts:
            try:
                parsed = json.loads(script.strip())
            except json.JSONDecodeError:
                continue

            if isinstance(parsed, list):
                for obj in parsed:
                    if isinstance(obj, dict) and obj.get("@type") == "Product":
                        yield obj
            elif isinstance(parsed, dict):
                if parsed.get("@type") == "Product":
                    yield parsed
                graph = parsed.get("@graph")
                if isinstance(graph, list):
                    for obj in graph:
                        if isinstance(obj, dict) and obj.get("@type") == "Product":
                            yield obj

    @staticmethod
    def _guess_package(title: str) -> str:
        title_u = title.upper()
        for pkg in ("QFN", "TQFP", "SOIC", "SOT-23", "DIP", "BGA", "LQFP", "SOP"):
            if pkg in title_u:
                return pkg
        return ""


class ElectronshikProvider(BaseProvider):
    source_name = "electronshik.ru"
    base_url = "https://electronshik.ru"

    def search(self, query: str) -> List[PartResult]:
        url = f"{self.base_url}/search/?q={urllib.parse.quote(query)}"
        html = self._http_get(url)

        # Minimal HTML extraction fallback when structured data is absent.
        blocks = re.findall(
            r'<a[^>]+class="[^"]*product[^"]*"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html,
            flags=re.S | re.I,
        )
        results: List[PartResult] = []
        for href, block in blocks:
            title_match = re.search(r"<h\d[^>]*>(.*?)</h\d>", block, flags=re.S | re.I)
            price_match = re.search(r"([0-9][0-9\.,\s]*)(?:\s*руб)", block, flags=re.S | re.I)
            title = _strip_tags(title_match.group(1)) if title_match else _strip_tags(block)[:120]
            price = self._extract_number(price_match.group(1)) if price_match else None
            full_url = urllib.parse.urljoin(self.base_url, href)
            results.append(
                PartResult(
                    source=self.source_name,
                    title=title,
                    sku=_extract_like_part_number(title),
                    manufacturer="",
                    package=self._guess_package(title),
                    stock=1,
                    price_rub=price,
                    url=full_url,
                )
            )
        return results

    @staticmethod
    def _guess_package(title: str) -> str:
        return ElitanProvider._guess_package(title)


class ChipdipProvider(BaseProvider):
    source_name = "chipdip.ru"
    base_url = "https://www.chipdip.ru"

    def search(self, query: str) -> List[PartResult]:
        url = f"{self.base_url}/search?searchtext={urllib.parse.quote(query)}"
        html = self._http_get(url)
        row_re = re.compile(r'<tr[^>]+class="[^"]*\bwith-hover\b[^"]*"[^>]*>(.*?)</tr>', flags=re.S | re.I)
        blocks = row_re.findall(html)
        if not blocks:
            blocks = self._extract_product_chunks(html)

        results: List[PartResult] = []
        for block in blocks:
            link_match = re.search(r'<a[^>]+href="(/product/[^"]+)"[^>]*>(.*?)</a>', block, flags=re.S | re.I)
            if not link_match:
                continue

            href = link_match.group(1)
            title_html = link_match.group(2)
            title = _strip_tags(title_html)

            brand_match = re.search(r'<span[^>]*class="[^"]*\bitemlist_pval\b[^"]*"[^>]*>([^<]+)</span>', block, flags=re.S | re.I)
            if not brand_match:
                brand_match = re.search(r'(?:Бренд|Brand)\s*:\s*<[^>]+>([^<]+)</', block, flags=re.S | re.I)
            manufacturer = brand_match.group(1).strip() if brand_match else ""

            price_match = re.search(r'<span[^>]*id="price_\d+"[^>]*>([^<]+)</span>', block, flags=re.S | re.I)
            if not price_match:
                price_match = re.search(r'<span[^>]*class="[^"]*\bprice-main\b[^"]*"[^>]*>(.*?)</span>', block, flags=re.S | re.I)
            price = self._extract_number(_strip_tags(price_match.group(1))) if price_match else None

            stock = 0
            stock_matches = re.findall(r'([0-9][0-9\s]*)\s*шт\.?', block, flags=re.S | re.I)
            if stock_matches:
                stock = max(_safe_int(x.replace(" ", "")) for x in stock_matches)

            sku = self._extract_sku(title)
            package = self._extract_package(title)
            full_url = urllib.parse.urljoin(self.base_url, href)

            results.append(
                PartResult(
                    source=self.source_name,
                    title=title,
                    sku=sku or _extract_like_part_number(title),
                    manufacturer=manufacturer,
                    package=package,
                    stock=stock,
                    price_rub=price,
                    url=full_url,
                )
            )

        return _dedupe_results(results)

    @staticmethod
    def _extract_product_chunks(html: str) -> List[str]:
        chunks: List[str] = []
        for m in re.finditer(r'<a[^>]+href="/product/[^"]+"[^>]*>.*?</a>', html, flags=re.S | re.I):
            left = max(0, m.start() - 700)
            right = min(len(html), m.end() + 1300)
            chunks.append(html[left:right])
        return chunks

    @staticmethod
    def _extract_sku(title: str) -> str:
        head = title.split(",", 1)[0].strip()
        if head:
            return re.sub(r"\s+", "", head).upper()
        return _extract_like_part_number(title)

    @staticmethod
    def _extract_package(title: str) -> str:
        bracket = re.search(r"\[([^\]]+)\]", title)
        if bracket:
            return bracket.group(1).strip()
        return ElitanProvider._guess_package(title)


class SearchService:
    def __init__(self) -> None:
        self.providers = [ElitanProvider(), ChipdipProvider()]

    def search(self, query: str, filters: SearchFilters) -> List[PartResult]:
        filtered, _diagnostics = self.search_with_diagnostics(query, filters)
        return filtered

    def search_with_diagnostics(self, query: str, filters: SearchFilters) -> Tuple[List[PartResult], Dict[str, object]]:
        rows: List[PartResult] = []
        by_source: Dict[str, int] = {}
        errors: Dict[str, str] = {}
        for provider in self.providers:
            try:
                provider_rows = provider.search(query)
                rows.extend(provider_rows)
                by_source[provider.source_name] = len(provider_rows)
            except Exception as exc:
                # Ignore provider-level failures to keep plugin usable.
                by_source[provider.source_name] = 0
                errors[provider.source_name] = str(exc)
                continue

        filtered = [item for item in rows if _matches_filters(item, filters)]
        filtered.sort(key=lambda x: (x.price_rub is None, x.price_rub or 0))
        diagnostics = {
            "total_raw": len(rows),
            "total_filtered": len(filtered),
            "by_source": by_source,
            "errors": errors,
        }
        return filtered, diagnostics


def _matches_filters(item: PartResult, f: SearchFilters) -> bool:
    if f.manufacturer and f.manufacturer.lower() not in item.manufacturer.lower():
        return False
    if f.package and f.package.lower() not in item.package.lower():
        return False
    if f.max_price_rub is not None:
        if item.price_rub is None or item.price_rub > f.max_price_rub:
            return False
    if item.stock < f.min_stock:
        return False
    if f.only_in_stock and item.stock <= 0:
        return False
    return True


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_like_part_number(text: str) -> str:
    match = re.search(r"\b[A-Z]{1,5}[A-Z0-9\-]{2,}\b", text.upper())
    return match.group(0) if match else ""


def _safe_int(value: object, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return default


def _dedupe_results(items: List[PartResult]) -> List[PartResult]:
    out: List[PartResult] = []
    seen = set()
    for item in items:
        key = (item.source, item.url, item.sku, item.title)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out
