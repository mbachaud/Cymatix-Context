"""#239 answer-pool probe — keep only words that are a SINGLE token (with a
leading space, i.e. as a continuation) in the Qwen3-4B tokenizer. A multi-token
answer would never be in_graph -> a FALSE label-0 that would poison the refit.
Prints the single-token pool. No model load, just the tokenizer (fast).
"""
from transformers import AutoTokenizer

CANDIDATES = [
    # colors
    "red", "blue", "green", "teal", "cyan", "pink", "gold", "grey", "brown",
    "black", "white", "violet", "amber", "azure", "crimson", "olive", "coral",
    "ivory", "jade", "ruby", "scarlet", "maroon", "beige", "tan",
    # metals / materials
    "quartz", "granite", "copper", "marble", "bronze", "slate", "iron", "steel",
    "brass", "zinc", "chrome", "cobalt", "nickel", "silver", "tin", "lead",
    "glass", "stone", "clay", "gravel", "concrete", "titanium",
    # animals
    "tiger", "eagle", "shark", "wolf", "lion", "bear", "hawk", "fox", "owl",
    "swan", "crane", "robin", "raven", "finch", "dove", "seal", "whale",
    "moose", "deer", "bison", "otter", "lynx", "panther", "jaguar",
    # fruits
    "mango", "peach", "cherry", "lemon", "grape", "plum", "lime", "melon",
    "apple", "berry", "fig", "date", "pear", "kiwi", "papaya", "guava",
    # elements
    "neon", "helium", "argon", "xenon", "radon", "carbon", "oxygen", "sodium",
    "boron", "krypton", "iodine", "lithium",
    # celestial / nature
    "comet", "meteor", "planet", "nebula", "galaxy", "river", "ocean",
    "forest", "desert", "canyon", "glacier", "volcano",
]


def main():
    tok = AutoTokenizer.from_pretrained("Qwen/Qwen3-4B")
    single, multi = [], []
    seen = set()
    for w in CANDIDATES:
        if w in seen:
            continue
        seen.add(w)
        ids = tok.encode(" " + w, add_special_tokens=False)
        (single if len(ids) == 1 else multi).append(w)
    print(f"SINGLE ({len(single)}): {single}")
    print(f"MULTI  ({len(multi)}): {multi}")


if __name__ == "__main__":
    main()
