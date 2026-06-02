"""
Seed script – populates the database with example data.
Run with: python seed.py
Make sure your .env file is configured before running.
"""

import httpx
import json

BASE_URL = "http://localhost:8000"


def post(path, data):
    r = httpx.post(f"{BASE_URL}{path}", json=data)
    r.raise_for_status()
    return r.json()


def main():
    print("🌱 Seeding database...")

    # --- Sources ---
    print("\n📰 Creating sources...")
    nyt = post("/sources/", {
        "name": "New York Times",
        "url": "https://nytimes.com",
        "credibility_score": 95,
        "type": "news"
    })
    wikipedia = post("/sources/", {
        "name": "Wikipedia",
        "url": "https://wikipedia.org",
        "credibility_score": 75,
        "type": "wikipedia"
    })
    print(f"  ✓ {nyt['name']} (id: {nyt['id']})")
    print(f"  ✓ {wikipedia['name']} (id: {wikipedia['id']})")

    # --- Entities ---
    print("\n🏢 Creating entities...")
    ab_inbev = post("/entities/", {
        "name": "AB InBev",
        "type": "company",
        "country": "BE",
        "founded": 2008,
        "revenue": 57786000000,
        "description": "Anheuser-Busch InBev, world's largest beer company."
    })
    corona = post("/entities/", {
        "name": "Corona",
        "type": "brand",
        "country": "MX",
        "founded": 1925,
        "description": "Mexican beer brand owned by AB InBev outside Mexico."
    })
    modelo = post("/entities/", {
        "name": "Grupo Modelo",
        "type": "company",
        "country": "MX",
        "founded": 1925,
        "description": "Mexican brewery, maker of Corona."
    })
    print(f"  ✓ {ab_inbev['name']} (id: {ab_inbev['id']})")
    print(f"  ✓ {corona['name']} (id: {corona['id']})")
    print(f"  ✓ {modelo['name']} (id: {modelo['id']})")

    # --- Locations ---
    print("\n📍 Creating locations...")
    leuven = post("/locations/", {
        "city": "Leuven",
        "country": "BE",
        "country_full": "Belgium",
        "region": "Europe",
        "latitude": 50.8798,
        "longitude": 4.7005
    })
    mexico_city = post("/locations/", {
        "city": "Mexico City",
        "country": "MX",
        "country_full": "Mexico",
        "region": "Latin America",
        "latitude": 19.4326,
        "longitude": -99.1332
    })
    print(f"  ✓ {leuven['city']} (id: {leuven['id']})")
    print(f"  ✓ {mexico_city['city']} (id: {mexico_city['id']})")

    # --- Set HQ locations ---
    print("\n🗺️  Setting headquarters...")
    httpx.post(f"{BASE_URL}/locations/{ab_inbev['id']}/headquartered-in/{leuven['id']}").raise_for_status()
    httpx.post(f"{BASE_URL}/locations/{modelo['id']}/headquartered-in/{mexico_city['id']}").raise_for_status()
    print(f"  ✓ AB InBev → Leuven")
    print(f"  ✓ Grupo Modelo → Mexico City")

    # --- Persons ---
    print("\n👤 Creating persons...")
    michel = post("/persons/", {
        "first_name": "Michel",
        "last_name": "Doukeris",
        "nationality": "BR",
        "description": "CEO of AB InBev since 2021.",
        "wikipedia_url": "https://en.wikipedia.org/wiki/Michel_Doukeris"
    })
    print(f"  ✓ {michel['full_name']} (id: {michel['id']})")

    # --- Relationships ---
    print("\n🔗 Creating ownership relationships...")
    post("/relationships/owns", {
        "owner_id": ab_inbev["id"],
        "owned_id": modelo["id"],
        "stake_percent": 100.0,
        "ownership_type": "full",
        "since": "2013-06-04",
        "until": None,
        "value_usd": 20100000000,
        "source_id": wikipedia["id"],
        "credibility_score": 80
    })
    post("/relationships/owns", {
        "owner_id": modelo["id"],
        "owned_id": corona["id"],
        "stake_percent": 100.0,
        "ownership_type": "full",
        "since": "1925-01-01",
        "until": None,
        "source_id": wikipedia["id"],
        "credibility_score": 80
    })
    print(f"  ✓ AB InBev → Grupo Modelo (100%)")
    print(f"  ✓ Grupo Modelo → Corona (100%)")

    # --- Role ---
    print("\n👔 Creating roles...")
    post("/relationships/roles", {
        "person_id": michel["id"],
        "entity_id": ab_inbev["id"],
        "role": "CEO",
        "since": "2021-07-01",
        "until": None,
        "source_id": nyt["id"],
        "credibility_score": 95
    })
    print(f"  ✓ Michel Doukeris → CEO of AB InBev")

    print("\n✅ Seed complete! Visit http://localhost:8000/docs to explore the API.")
    print(f"\n🔍 Try: GET /search/entity/{ab_inbev['id']}/full-profile")


if __name__ == "__main__":
    main()
