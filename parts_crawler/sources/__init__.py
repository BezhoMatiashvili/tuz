"""Source adapter registry."""

from __future__ import annotations

from parts_crawler.sources.base import SourceAdapter
from parts_crawler.sources.catcar import CatCarAdapter
from parts_crawler.sources.mann import MannAdapter
from parts_crawler.sources.mann_api import MannApiAdapter
from parts_crawler.sources.ngk import NgkAdapter
from parts_crawler.sources.partsouq import PartsouqAdapter
from parts_crawler.sources.realoem import RealOemAdapter
from parts_crawler.sources.rockauto import RockAutoAdapter

ADAPTERS: dict[str, type[SourceAdapter]] = {
    adapter.name: adapter for adapter in (
        RockAutoAdapter, MannAdapter, MannApiAdapter, NgkAdapter, CatCarAdapter,
        RealOemAdapter, PartsouqAdapter,
    )
}


def get_adapters(selection: str) -> list[SourceAdapter]:
    """Adapters by name; "all" means every source that can be crawled automatically.

    Sources filled only from operator-recorded HAR files are left out of "all": a run
    would stop at their bot protection on the first request.
    """
    names = [name for name, adapter in ADAPTERS.items() if not adapter.manual_capture] \
        if selection in {"", "all"} else [
            name.strip() for name in selection.split(",") if name.strip()]
    unknown = [name for name in names if name not in ADAPTERS]
    if unknown:
        raise SystemExit(f"Unknown source(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(ADAPTERS)}")
    return [ADAPTERS[name]() for name in names]
