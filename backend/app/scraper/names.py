"""Name-matching helpers shared by the proxy-statement writer.

Pure functions: generate reversed-name / nickname / article-and-punctuation
variants used to match EDGAR and proxy names against DB node names.
"""
import re as _re


_NICKNAMES: dict[str, str] = {
    "larry":  "lawrence",
    "bill":   "william",
    "bob":    "robert",
    "dick":   "richard",
    "chuck":  "charles",
    "jim":    "james",
    "mike":   "michael",
    "ted":    "edward",
    "tom":    "thomas",
    "ken":    "kenneth",
    "jeff":   "jeffrey",
    "steve":  "steven",
    "dave":   "david",
    "andy":   "andrew",
    "tony":   "anthony",
    "joe":    "joseph",
    "jack":   "john",
    "alex":   "alexander",
    "liz":    "elizabeth",
    "beth":   "elizabeth",
    "kate":   "katherine",
    "sue":    "susan",
    "jen":    "jennifer",
    "sam":    "samuel",
    "matt":   "matthew",
    "dan":    "daniel",
    "tim":    "timothy",
    "pat":    "patricia",
    "chris":  "christopher",
    "nick":   "nicholas",
}


def _name_words(name: str) -> list[str]:
    """Strip punctuation and split into words."""
    return name.replace(".", "").replace(",", "").strip().split()


def _person_name_variants(name: str) -> list[str]:
    """
    Generate name candidates to match SEC reversed-name format and nicknames.

    SEC filings store names as "Last First [Mid]" (no periods).
    Proxy statements use "First [Mid.] Last".

    Examples:
      "Sergey Brin"       → [..., "Brin Sergey"]
      "Warren E. Buffett" → [..., "Buffett Warren E", "Buffett Warren"]
      "Larry Page"        → [..., "Page Larry", "Lawrence Page", "Page Lawrence"]
    """
    variants: list[str] = [name]
    clean = name.replace(".", "").replace(",", "").strip()
    words = clean.split()

    if len(words) == 2:
        variants.append(f"{words[1]} {words[0]}")
    elif len(words) == 3:
        variants.append(f"{words[2]} {words[0]} {words[1]}")
        variants.append(f"{words[2]} {words[0]}")
        variants.append(f"{words[0]} {words[2]}")

    if clean != name:
        variants.append(clean)

    # Nickname expansion on the first word
    if words:
        nick = words[0].lower()
        formal = _NICKNAMES.get(nick)
        if formal:
            formal_cap = formal.capitalize()
            exp_words = [formal_cap] + words[1:]
            variants.append(" ".join(exp_words))
            if len(exp_words) == 2:
                variants.append(f"{exp_words[1]} {exp_words[0]}")
            elif len(exp_words) == 3:
                variants.append(f"{exp_words[2]} {exp_words[0]} {exp_words[1]}")
                variants.append(f"{exp_words[2]} {exp_words[0]}")

    return list(dict.fromkeys(variants))


def _entity_name_variants(name: str) -> list[str]:
    """
    Generate entity name candidates that differ only in articles/punctuation.

    Handles cases like:
      "The Vanguard Group, Inc." → also tries "Vanguard Group, Inc.", "Vanguard Group Inc"
      "BlackRock, Inc."          → also tries "BlackRock Inc", "Blackrock Inc."
    """
    variants: list[str] = [name]
    # Strip leading "The "
    no_the = _re.sub(r"^the\s+", "", name.strip(), flags=_re.IGNORECASE)
    if no_the != name:
        variants.append(no_the)
    # Strip commas and periods
    for base in [name, no_the]:
        no_punct = base.replace(",", "").replace(".", "")
        variants.append(no_punct)
    return list(dict.fromkeys(variants))


def _is_reordering(proxy_name: str, db_name: str) -> bool:
    """True when proxy and DB name contain exactly the same words in a different order."""
    pw = sorted(_name_words(proxy_name.lower()))
    dw = sorted(_name_words(db_name.lower()))
    return pw == dw and proxy_name.replace(".", "").replace(",", "").strip() != \
           db_name.replace(".", "").replace(",", "").strip()
