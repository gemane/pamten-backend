"""The /scraper/geocode endpoint must be gated by GEOCODING_ENABLED (env)."""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from app.config import settings
from app.scraper import router as scraper_router


def test_geocode_endpoint_refuses_when_disabled():
    with patch.object(settings, "GEOCODING_ENABLED", False):
        with pytest.raises(HTTPException) as exc:
            scraper_router.geocode_backfill_run(limit=None, _={"role": "admin"})
    assert exc.value.status_code == 403


def test_geocode_endpoint_runs_backfill_when_enabled():
    with patch.object(settings, "GEOCODING_ENABLED", True), \
         patch("app.scraper.geocode_backfill.backfill",
               return_value={"geocoded": 3, "entities_geocoded": 3}) as bf:
        out = scraper_router.geocode_backfill_run(limit=5, _={"role": "contributor"})
    bf.assert_called_once_with(limit=5)
    assert out["status"] == "ok"
    assert out["geocoded"] == 3
