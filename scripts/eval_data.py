"""Fixture for the §6.9 int8 vs fp32 evaluation: realistic notes and queries with known answers."""

NOTES: dict[str, str] = {
    "people/jack.md": "# Jack\n\n## Constraints\n\nAllergic to peanuts. Hates coriander (cilantro).\n\n## Preferences\n\nLikes spicy food but not numbing mala spice.",
    "people/partner.md": "# Partner\n\n## Constraints\n\nVegetarian on Mondays. No raw fish.\n\n## Preferences\n\nLoves desserts, especially durian.",
    "shared/household.md": "# Household\n\nWeekday dinner budget about $30 for two. We have an air fryer and a rice cooker, no oven.",
    "shared/places/tiong-bahru-pau.md": "# Tiong Bahru Pau\n\nGood char siew pau near the Tiong Bahru market. We liked it on a Sunday morning.",
    "shared/places/ah-hock-laksa.md": "# Ah Hock Laksa\n\nKatong laksa stall, very rich coconut broth. Long queue at lunch, go before 11am.",
    "shared/places/ramen-keisuke.md": "# Ramen Keisuke\n\nTonkotsu ramen in Tanjong Pagar. Free-flow boiled eggs. Jack's favourite ramen.",
    "shared/places/sichuan-kitchen.md": "# Sichuan Kitchen\n\nVery mala, numbing spice. Partner liked it, Jack found it too numbing.",
    "shared/places/hawker-maxwell.md": "# Maxwell Food Centre\n\nHawker centre with Tian Tian chicken rice. Good for a cheap weekday dinner.",
    "shared/places/omakase-sushi.md": "# Sushi Omakase\n\nExpensive omakase for anniversaries. Mostly raw fish, so not for Partner.",
    "shared/topics/movies.md": "# Movies\n\nWe both like sci-fi and slow thrillers. Partner dislikes horror. Watched Arrival twice.",
    "shared/topics/weekend.md": "# Weekend activities\n\nWe enjoy MacRitchie walks, board game cafes and the Botanic Gardens on cooler days.",
    "shared/topics/takeaway.md": "# Takeaway\n\nOn weekdays we usually tapao (takeaway) from the coffee shop downstairs.",
    "shared/topics/drinks.md": "# Drinks\n\nBubble tea: Jack orders brown sugar milk tea, less sweet. Partner prefers fruit tea.",
    "memories/jack/coffee.md": "# Coffee\n\nJack drinks kopi-o kosong (black coffee, no sugar) every morning.",
    "memories/jack/gym.md": "# Gym\n\nJack goes to the gym on Tuesday and Thursday evenings, so dinner is late on those days.",
    "memories/partner/spicy.md": "# Spice\n\nPartner can't take very spicy food lately (stomach), prefers mild dishes this month.",
    "memories/partner/books.md": "# Books\n\nPartner is reading Japanese mystery novels, likes Keigo Higashino.",
    "memories/partner/chinese-note.md": "# 甜品\n\n她很喜欢吃榴莲和芒果糯米饭，周末常去甜品店。",
    "memories/jack/chinese-note.md": "# 早餐\n\n他早上喜欢吃咸豆浆和油条，不喜欢太甜的东西。",
    "shared/topics/cooking.md": "# Cooking at home\n\nEasy home dinners: air-fryer chicken wings, tomato egg stir-fry, instant ramen upgraded with egg.",
}

# (query, expected path) — phrased the way Claude would call search_memory, incl. local terms.
QUERIES: list[tuple[str, str]] = [
    ("Jack food allergy peanut", "people/jack.md"),
    ("what does Jack dislike herbs coriander cilantro", "people/jack.md"),
    ("that bun place we liked in Tiong Bahru", "shared/places/tiong-bahru-pau.md"),
    ("good laksa stall Katong", "shared/places/ah-hock-laksa.md"),
    ("ramen in Tanjong Pagar", "shared/places/ramen-keisuke.md"),
    ("numbing mala spicy restaurant", "shared/places/sichuan-kitchen.md"),
    ("cheap hawker dinner chicken rice", "shared/places/hawker-maxwell.md"),
    ("movie genres we like", "shared/topics/movies.md"),
    ("things to do outdoors on the weekend", "shared/topics/weekend.md"),
    ("takeaway food tapao weekdays", "shared/topics/takeaway.md"),
    ("bubble tea order", "shared/topics/drinks.md"),
    ("Jack's morning coffee kopi", "memories/jack/coffee.md"),
    ("想吃甜的 榴莲 dessert", "memories/partner/chinese-note.md"),
    ("想吃辣的 spicy food preference", "memories/partner/spicy.md"),
    ("what can we cook at home with an air fryer", "shared/topics/cooking.md"),
]
