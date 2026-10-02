"""Registry of supported wastewater data sources."""

from __future__ import annotations

from .base import LEVELS, PATHOGENS, RawSeries, Signal, Source, SourceInfo
from .canada import Canada
from .germany import Germany
from .netherlands import Netherlands
from .new_zealand import NewZealand
from .scotland import Scotland
from .usa import UnitedStates

# Order here is the order countries appear in the site's picker.
SOURCES: dict[str, type[Source]] = {
    cls.info.id: cls
    for cls in (Scotland, UnitedStates, Canada, Germany, Netherlands, NewZealand)
}

DEFAULT_COUNTRY = "scotland"

__all__ = ["LEVELS", "PATHOGENS", "RawSeries", "Signal", "Source", "SourceInfo", "SOURCES", "DEFAULT_COUNTRY"]
