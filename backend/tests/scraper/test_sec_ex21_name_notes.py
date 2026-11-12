"""What a row writes after a company's name that is no part of it (findings
item 80): another name it bears or bore, the filing's shorthand, a stake note,
a tax id — and a row that is only a note. The rows are the shapes measured
over 4,384 cached exhibits of 7k filings, written as the filers write them."""
from app.scraper.sec_ex21 import parse_exhibit


def _rows(*rows: tuple[str, str], registrant: str = "Example Holdings, Inc.") -> dict[str, dict]:
    body = "".join(f"<tr><td>{n}</td><td>{j}</td></tr>" for n, j in rows)
    html = ("<html><body><table><tr><td>Name of Subsidiary</td><td>Jurisdiction of Organization</td></tr>"
            f"{body}</table></body></html>")
    return {e["name"]: e for e in parse_exhibit(html, registrant)}


def test_a_former_name_goes_to_former_names():
    got = _rows(("ACRES Realty Funding, Inc. (F/K/A RCC Real Estate, Inc.)", "Delaware"),
                ("Odeon Cinemas Group Limited (fka AMC (UK) Acquisition Limited)", "United Kingdom"),
                ("Hypotheca Capital, LLC (formerly known as The New York Mortgage Company, LLC)", "Delaware"),
                ("Old AGI, Inc. f/k/a AIRO Group, Inc.", "Delaware"))
    assert got["ACRES Realty Funding, Inc."]["former_names"] == ["RCC Real Estate, Inc."]
    assert got["Odeon Cinemas Group Limited"]["former_names"] == ["AMC (UK) Acquisition Limited"]
    assert got["Hypotheca Capital, LLC"]["former_names"] == ["The New York Mortgage Company, LLC"]
    assert got["Old AGI, Inc."]["former_names"] == ["AIRO Group, Inc."]
    assert "aliases" not in got["Old AGI, Inc."]


def test_a_trade_name_goes_to_aliases():
    got = _rows(("Ameren Illinois Company (d/b/a Ameren Illinois)", "Illinois"),
                ("Fin Technologies, Inc. d/b/a MANTL", "Delaware"),
                ("ArcBest II, Inc. (d/b/a ArcBest and d/b/a U-Pack)", "Delaware"),
                ("Merchants Bank of Indiana (also d/b/a Merchants Mortgage)", "Indiana"))
    assert got["Ameren Illinois Company"]["aliases"] == ["Ameren Illinois"]
    assert got["Fin Technologies, Inc."]["aliases"] == ["MANTL"]
    assert got["ArcBest II, Inc."]["aliases"] == ["ArcBest", "U-Pack"]
    assert got["Merchants Bank of Indiana"]["aliases"] == ["Merchants Mortgage"]
    assert "former_names" not in got["Ameren Illinois Company"]


def test_the_filings_shorthand_and_a_tax_id_are_no_part_of_the_name():
    got = _rows(("Gemma Power Systems, LLC (“GPS”)", "Connecticut"),
                ('ACM Research (Chengdu), Inc. ("ACM Chengdu")', "China"),
                ("LucidDx Labs Inc. (87-41661458)", "Delaware"),
                # quotes inside the name, no bracket: part of the name
                ("UAB “1stdibs.com Lithuania”", "Lithuania"))
    assert set(got) == {"Gemma Power Systems, LLC", "ACM Research (Chengdu), Inc.", "LucidDx Labs Inc.",
                        "UAB “1stdibs.com Lithuania”"}
    assert "aliases" not in got["Gemma Power Systems, LLC"]


def test_a_stake_note_gives_the_stake_and_the_holder_it_names():
    got = _rows(("Roadrunner Gas Transmission Holdings, LLC", "Delaware"),
                ("Roadrunner Gas Transmission, LLC (100% owned by Roadrunner Gas Transmission Holdings, LLC)",
                 "Delaware"),
                ("Pilot Knob Pellet Co. (50% owned)", "Missouri"),
                ("TR Domain Point LLC (96.5% owned by Registrant)", "Texas"),
                ("Fiberprep Inc. (31.05% owned by Roadrunner Gas Transmission Holdings, LLC and "
                 "68.95% owned by Example Holdings, Inc.)", "Delaware"),
                ("Sekopac d.o.o. (majority owned by unrelated third party)", "Slovenia"))
    rr = got["Roadrunner Gas Transmission, LLC"]
    assert rr["stake_percent"] == 100.0
    assert rr["parent"] == "Roadrunner Gas Transmission Holdings, LLC" and rr["parent_basis"] == "column"
    assert got["Pilot Knob Pellet Co."]["stake_percent"] == 50.0 and "parent" not in got["Pilot Knob Pellet Co."]
    # "owned by Registrant" is the filer: no parent, directly under it
    tr = got["TR Domain Point LLC"]
    assert tr["stake_percent"] == 96.5 and tr.get("parent") is None and tr["parent_basis"] == "column"
    # two holders: neither is THE parent, and neither stake is the row's
    assert "parent" not in got["Fiberprep Inc."] and "stake_percent" not in got["Fiberprep Inc."]
    assert "stake_percent" not in got["Sekopac d.o.o."]


def test_a_note_row_is_no_company_and_one_name_is_one_row():
    got = _rows(("LucidDx Labs Inc.", "Delaware"),
                ("Wholly-Owned Subsidiary of Lucid Diagnostics Inc.", "Delaware"),
                ("Majority-owned subsidiary of HCI Group, Inc.", "Florida"),
                ("(fka DE 201 Santa Monica, LLC)", "Delaware"),
                ("LucidDx Labs Inc. (87-41661458)", "Delaware"))
    assert list(got) == ["LucidDx Labs Inc."]


def test_a_stake_the_column_states_wins_over_the_note():
    html = ("<html><body><table><tr><td>Name of Subsidiary</td><td>Jurisdiction</td><td>Ownership</td></tr>"
            "<tr><td>Yellowstone Pipe Line Company (40.0% interest)</td><td>Delaware</td><td>46%</td></tr>"
            "<tr><td>Northwest Pipe Line Company</td><td>Delaware</td><td>100%</td></tr>"
            "</table></body></html>")
    got = {e["name"]: e for e in parse_exhibit(html, "Example Holdings, Inc.")}
    assert got["Yellowstone Pipe Line Company"]["stake_percent"] == 46.0


def test_two_spellings_of_one_row_stay_one_row():
    def rows(*cells):
        return parse_exhibit(
            "<html><body><table><tr><td>Name of Subsidiary</td><td>Jurisdiction of Organization</td></tr>"
            + "".join(f"<tr><td>{n}</td><td>{j}</td></tr>" for n, j in cells) + "</table></body></html>",
            "Example Holdings, Inc.")
    got = rows(("BREX Parker MF DST (f/k/a BNTR Parker Propco LLC)", "Delaware"),
               ("BREX Parker MF DST", "Delaware"), ("BREX Parker MF DST", "Ireland"))
    # one per place, as a list repeated across pages is
    assert [(e["name"], e["jurisdiction"]) for e in got] == [("BREX Parker MF DST", "Delaware"),
                                                            ("BREX Parker MF DST", "Ireland")]


def test_a_place_in_brackets_after_the_legal_form_is_the_rows_jurisdiction():
    # findings item 112: Quaker Houghton (Pennsylvania) lists "Quaker Chemical
    # Corporation (PA)" and "… (DE)" under "United States"; kept in the name, the
    # Delaware company became a node beside GLEIF's US-DE record, and the filer
    # itself a subsidiary of itself
    got = parse_exhibit(
        "<html><body><table><tr><td>Name</td><td>Jurisdiction of Incorporation</td></tr>"
        "<tr><td>Quaker Chemical Corporation (PA)</td><td>United States</td></tr>"
        "<tr><td>Quaker Chemical Corporation (DE)</td><td>United States</td></tr>"
        "<tr><td>Quaker Houghton Brazil Holdings Ltda. (Brazil)</td><td>Brazil</td></tr>"
        "<tr><td>LR Port Imperial South BB, LLC (Delaware)</td><td>DE</td></tr>"
        "</table></body></html>", ["QUAKER CHEMICAL CORP"], home="US-PA")
    assert [(e["name"], e["jurisdiction"]) for e in got] == [
        ("Quaker Chemical Corporation", "DE"),            # the PA one is the filer's own row
        ("Quaker Houghton Brazil Holdings Ltda.", "Brazil"),
        ("LR Port Imperial South BB, LLC", "DE")]          # the column already says the state


def test_a_bracket_that_is_part_of_the_name_or_disagrees_stays():
    got = _rows(("Alibaba Information Port (Wulanchabu) Co., Ltd.", "China"),
                ("Foo Services GmbH (UK)", "Germany"),           # another country than the column's
                ("Acme Holdings (Delaware)", "Delaware"),          # no legal form before it
                ("Bar Trading Limited (Group Treasury)", "Ireland"))  # no place in it
    assert set(got) == {"Alibaba Information Port (Wulanchabu) Co., Ltd.", "Foo Services GmbH (UK)",
                        "Acme Holdings (Delaware)", "Bar Trading Limited (Group Treasury)"}


def test_one_name_in_two_states_is_two_rows():
    # Lennar lists "Lennar Title, Inc." in Alabama and in California: two companies,
    # one row until the list key weighed the state (findings item 112)
    got = parse_exhibit(
        "<html><body><table><tr><td>Name of Subsidiary</td><td>State</td></tr>"
        "<tr><td>Lennar Title, Inc.</td><td>AL</td></tr>"
        "<tr><td>Lennar Title, Inc.</td><td>CA</td></tr>"
        "<tr><td>Lennar Title, Inc.</td><td>CA</td></tr>"
        "<tr><td>Rutenberg Homes, Inc. (Florida)</td><td>FL</td></tr>"
        "</table></body></html>", "Lennar Corp")
    assert [(e["name"], e["jurisdiction"]) for e in got] == [
        ("Lennar Title, Inc.", "AL"), ("Lennar Title, Inc.", "CA"), ("Rutenberg Homes, Inc.", "FL")]
