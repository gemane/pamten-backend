"""The dated states of a living company (app.legal_events, findings item 85d):
GLEIF's liquidation / bankruptcy / insolvency / arrangement events as stored
entries and as timeline rows. The cases are dev's records of 2026-11-08."""
from app import legal_events


def _w(v):
    return {"$": v}


def _ev(kind, status, effective=None, recorded=None):
    ev = {"@group_type": "STANDALONE", "@event_status": status, "LegalEntityEventType": _w(kind)}
    if effective:
        ev["LegalEntityEventEffectiveDate"] = _w(f"{effective}T00:00:00+01:00")
    if recorded:
        ev["LegalEntityEventRecordedDate"] = _w(f"{recorded}T10:15:00Z")
    return ev


def _entity(*events):
    return {"LegalName": _w("X"), "LegalEntityEvents": {"LegalEntityEvent": list(events)}}


class TestEntries:
    def test_diamond_transmission_is_in_liquidation(self):
        # 2138003BFF7ZPQZA6B02: ACTIVE, LAPSED, one LIQUIDATION in progress
        assert legal_events.from_record(_entity(_ev("LIQUIDATION", "IN_PROGRESS", "2025-04-28", "2025-04-28"))) == [
            "LIQUIDATION|IN_PROGRESS|2025-04-28|2025-04-28"]

    def test_petrotel_s_insolvency_has_no_effective_day_only_the_recorded_one(self):
        # 7872006AZAFVTVIQ3G32: 2,761 liquidations and 169 bankruptcies in progress are undated this way
        assert legal_events.from_record(_entity(_ev("INSOLVENCY", "IN_PROGRESS", None, "2026-10-12"))) == [
            "INSOLVENCY|IN_PROGRESS||2026-10-12"]

    def test_cannae_s_arrangement_completed_and_the_other_events_stay_out(self):
        # 549300OGRF1ICI7D2V77: a rename, a form change and the VOLUNTARY_ARRANGEMENT
        got = legal_events.from_record(_entity(_ev("CHANGE_LEGAL_ADDRESS", "COMPLETED", "2024-06-20", "2024-06-24"),
                                               _ev("CHANGE_LEGAL_FORM", "COMPLETED", "2024-06-20", "2024-06-24"),
                                               _ev("VOLUNTARY_ARRANGEMENT", "COMPLETED", "2024-06-20", "2025-05-28")))
        assert got == ["VOLUNTARY_ARRANGEMENT|COMPLETED|2024-06-20|2025-05-28"]

    def test_an_end_event_is_not_a_state(self):
        # the ends are the node's dissolved_on (gleif_lei_cdf._end_of), never a state
        got = legal_events.from_record(_entity(_ev("MERGERS_AND_ACQUISITIONS", "COMPLETED", "2023-04-22", "2024-01-17"),
                                               _ev("DISSOLUTION", "COMPLETED", "2024-01-01", "2024-01-02"),
                                               _ev("BANKRUPTCY", "COMPLETED", "2023-01-01", "2023-01-05")))
        assert got == ["BANKRUPTCY|COMPLETED|2023-01-01|2023-01-05"]

    def test_withdrawn_is_kept_and_a_repeat_is_not_and_the_order_is_by_day(self):
        got = legal_events.from_record(_entity(_ev("LIQUIDATION", "WITHDRAWN_CANCELLED", "2022-05-01", "2022-05-03"),
                                               _ev("BANKRUPTCY", "IN_PROGRESS", None, None),
                                               _ev("LIQUIDATION", "IN_PROGRESS", "2021-01-01", "2021-01-02"),
                                               _ev("LIQUIDATION", "IN_PROGRESS", "2021-01-01", "2021-01-02")))
        assert got == ["LIQUIDATION|IN_PROGRESS|2021-01-01|2021-01-02",
                       "LIQUIDATION|WITHDRAWN_CANCELLED|2022-05-01|2022-05-03",
                       "BANKRUPTCY|IN_PROGRESS||"]

    def test_a_status_outside_the_vocabulary_or_a_dict_shaped_single_event(self):
        assert legal_events.entry("LIQUIDATION", "PENDING", "2021-01-01", None) is None
        one = {"LegalName": _w("X"), "LegalEntityEvents": {"LegalEntityEvent": _ev("BANKRUPTCY", "COMPLETED", "2020-02-02", None)}}
        assert legal_events.from_record(one) == ["BANKRUPTCY|COMPLETED|2020-02-02|"]
        assert legal_events.from_record({"LegalName": _w("X")}) == []


class TestParseAndTimeline:
    def test_parse_round_trips_in_the_apis_spelling(self):
        assert legal_events.parse("LIQUIDATION|IN_PROGRESS|2025-04-28|2025-04-28") == {
            "event": "liquidation", "status": "in_progress", "effective": "2025-04-28", "recorded": "2025-04-28"}
        assert legal_events.parse("INSOLVENCY|IN_PROGRESS||2026-10-12")["effective"] is None
        assert legal_events.parse("garbage") is None and legal_events.parse("A|B|C|D") is None
        assert legal_events.parse("LIQUIDATION|IN_PROGRESS|28/04/2025|") == {
            "event": "liquidation", "status": "in_progress", "effective": None, "recorded": None}

    def test_timeline_rows_date_by_the_effective_day_else_the_recorded_one(self):
        rows = legal_events.timeline_events(["LIQUIDATION|IN_PROGRESS|2025-04-28|2025-04-28",
                                             "INSOLVENCY|IN_PROGRESS||2026-10-12",
                                             "BANKRUPTCY|IN_PROGRESS||",            # no day at all: no row
                                             "VOLUNTARY_ARRANGEMENT|COMPLETED|2024-06-20|2025-05-28",
                                             "nonsense"])
        assert rows == [
            {"kind": "legal_event", "event": "liquidation", "status": "in_progress", "since": "2025-04-28",
             "dated_by": "effective", "until": None, "active": True},
            {"kind": "legal_event", "event": "insolvency", "status": "in_progress", "since": "2026-10-12",
             "dated_by": "recorded", "until": None, "active": True},
            {"kind": "legal_event", "event": "voluntary_arrangement", "status": "completed", "since": "2024-06-20",
             "dated_by": "effective", "until": None, "active": False},
        ]
        assert legal_events.timeline_events(None) == []
