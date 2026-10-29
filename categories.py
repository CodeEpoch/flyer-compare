"""Keyword food-category classifier for flyer items.

Flipp leaves item categories blank, so we look at the item's head noun first
(the last meaningful word: "apple JUICE", "chocolate MILK", "hot dog BUN"),
then walk left through the other words.
"""

CATEGORIES = {
    "Fruits": """apple banana orange mandarin clementine tangerine grape strawberry blueberry raspberry
        blackberry berry cherry pear peach plum nectarine mango pineapple watermelon melon cantaloupe
        honeydew kiwi lemon lime grapefruit avocado pomegranate papaya persimmon fig coconut guava lychee
        longan durian dragonfruit passionfruit apricot cranberry pomelo plantain jackfruit rambutan
        mangosteen kumquat fruit""",
    "Vegetables": """chili seaweed vegetable veggie lettuce romaine spinach kale cabbage broccoli cauliflower carrot celery
        cucumber tomato pepper onion garlic potato yam squash zucchini pumpkin eggplant mushroom corn pea
        asparagus beet radish turnip parsnip leek scallion ginger choy lan sprout herb cilantro parsley
        basil salad okra artichoke arugula chard rapini kohlrabi taro lotus daikon shallot jalapeno
        edamame green greens""",
    "Meat & Poultry": """omasum beef steak roast pork chop rib tenderloin loin sirloin striploin brisket chicken
        breast thigh drumstick wing turkey lamb veal goat duck sausage bacon ham wiener burger patty
        meatball mince oxtail kebab meat cutlet shank""",
    "Seafood": """snail bass fish salmon shrimp prawn tilapia cod haddock tuna crab lobster scallop mussel clam oyster
        squid octopus basa trout halibut pollock sole mackerel sardine anchovy seafood catfish snapper
        eel roe""",
    "Deli & Prepared": """beancurd falafel catering deli salami pepperoni prosciutto mortadella hummus dip sushi rotisserie meal
        sandwich samosa platter quiche kimchi tofu""",
    "Dairy & Eggs": """milk cheese yogurt yogourt butter cream egg margarine kefir cheddar mozzarella
        parmesan bocconcini feta brie ghee paneer creamer milkshake""",
    "Bakery": """bread bun roll bagel muffin croissant cake pie tart donut doughnut baguette ciabatta loaf
        tortilla pita naan cupcake brownie danish scone pastry cheesecake strudel crumpet""",
    "Snacks & Candy": """chestnut puffy cruncher chip crisp cracker cookie popcorn pretzel peanut almond cashew pistachio walnut
        nut chocolate candy gum gummy gummie licorice bar snack jerky granola nacho cheesie puff wafer
        biscuit treat mix""",
    "Beverages": """beer wine cider juice water soda pop cola coke pepsi ale drink beverage coffee tea kombucha lemonade
        espresso latte smoothie""",
    "Frozen": """frozen pizza fries waffle novelty popsicle gelato sorbet nugget pierogi perogy perogie
        entree lasagna dumpling""",
    "Pantry": """lasagne shell rice pasta noodle spaghetti macaroni penne sauce ketchup mustard mayonnaise mayo dressing
        oil vinegar flour sugar salt spice seasoning cereal oatmeal oat soup broth stock bean lentil
        chickpea honey jam spread syrup baking yeast condiment salsa pickle olive paste ramen vermicelli
        couscous quinoa grain seed nutella gravy sriracha curry masala marinade kd dinner""",
    "Household": """swiffer cloth duster wok roaster container butane detergent laundry dishwasher cleaner bleach towel tissue toilet napkin garbage trash
        foil battery bulb sponge softener freshener plate cutlery pod tab""",
    "Personal Care": """hair colour beauty cosmetic shampoo conditioner toothpaste toothbrush deodorant lotion wash soap razor cleanser
        moisturizer sunscreen vitamin supplement makeup mouthwash floss tampon""",
    "Baby": """diaper wipe baby formula infant toddler""",
    "Pet": """pet cat dog litter kibble""",
    "Flowers": """flower tulip rose bouquet plant orchid mum""",
}

# Multi-word heads checked against the END of the name ("... ice cream")
PHRASES = {
    "ice cream": "Frozen", "sour cream": "Dairy & Eggs", "cream cheese": "Dairy & Eggs",
    "peanut butter": "Pantry", "nut butter": "Pantry", "hot dog": "Meat & Poultry",
    "paper towel": "Household", "bath tissue": "Household", "dish soap": "Household",
    "cat food": "Pet", "dog food": "Pet", "pet food": "Pet", "cat treat": "Pet", "dog treat": "Pet",
    "baby food": "Baby", "energy bar": "Snacks & Candy", "protein bar": "Snacks & Candy",
    "sweet potato": "Vegetables", "green bean": "Vegetables", "body wash": "Personal Care",
    "spring roll": "Frozen", "fish stick": "Frozen", "fish ball": "Seafood", "crab stick": "Seafood", "egg roll": "Frozen", "cold cut": "Deli & Prepared",
}

ORDER = list(CATEGORIES) + ["Other"]


def build(singular):
    """Index keywords with the same singularizer the tokenizer uses."""
    words = {}
    for cat, text in CATEGORIES.items():
        for w in text.split():
            # index "cookie" and "cookies" -> "cooky", the way names get tokenized
            words.setdefault(singular(w), cat)
            words.setdefault(singular(w + "s"), cat)
    phrases = {" ".join(singular(w) for w in p.split()): c for p, c in PHRASES.items()}
    return words, sorted(phrases.items(), key=lambda kv: -len(kv[0]))


def classify(tokens, index):
    words, phrases = index
    joined = " ".join(tokens)
    for phrase, cat in phrases:
        if joined.endswith(phrase):
            return cat
    for w in reversed(tokens):
        if w in words:
            return words[w]
    return "Other"
