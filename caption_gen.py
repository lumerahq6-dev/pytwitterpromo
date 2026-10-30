"""Build a high-diversity spintax caption template for the X promo fleet.

Design goal is NOT "many unique strings" — the old template already produced ~800
of those. It is many unique UNIGRAM SETS, because X's cross-account duplicate rule
(BBQDuplicateTextProd / bbq_duplicate_text_unigrams) clusters on a bag of words and
is blind to word order and emoji. The old template collapsed 773 strings to 4 bags.

Every caption therefore draws from several disjoint lexical pools, and the sentence
skeletons themselves share almost no vocabulary.

Style is copied from what actually performs in this niche on X: a first-person
micro-story with a specific beat, casual lowercase, no link (the funnel is the bio).
Deliberately suggestive rather than graphic — graphic wording raises grox's NSFW_TEXT
score, which feeds visibility filtering and account-level labels, for no extra CTR.
"""
import json, random, re, itertools, sys
from collections import Counter

# --- lexical pools (disjoint on purpose) ------------------------------------
# every entry starts with a consonant so the "a {G}" / "with a {G}" templates never
# produce "a aussie girl" — the spintax engine cannot do a/an agreement
GIRL = ["blonde", "brunette", "redhead", "latina", "college girl", "cute asian girl",
        "goth chick", "thick blonde", "shy brunette", "tattooed girl", "gym girl",
        "milf", "country girl", "scene girl", "curly haired girl", "british girl",
        "sydney girl", "dublin girl", "polish girl", "czech girl", "colombian girl",
        "sorority girl", "nurse", "waitress", "barista", "cheerleader",
        "fine art student", "law student", "bartender", "yoga girl", "dance major",
        "roommate"]

PLAT = ["omegle", "monkey", "the monkey app", "ometv", "cam roulette", "chat roulette",
        "random chat", "video roulette"]

BEAT = ["went dead quiet", "froze mid sentence", "covered her mouth", "started giggling",
        "leaned way in", "bit her lip", "did a double take", "dropped her phone",
        "went bright red", "stopped talking entirely", "whispered something",
        "looked over her shoulder", "muted herself", "sat straight up",
        "put her hand over the camera", "started shaking her head", "burst out laughing",
        "went silent for a solid ten seconds", "tilted the camera down",
        "pulled the blanket up", "grabbed her headphones"]

SETTING = ["her roommate was asleep in the next bed", "she was meant to be revising",
           "her mum was downstairs", "it was gone 3am for her", "she had work in the morning",
           "her flatmate walked past twice", "she was in a uni library booth",
           "the tv was still running behind her", "she had a lecture in an hour",
           "someone knocked halfway through", "her dog kept jumping on the bed",
           "she was on a hotel wifi somewhere", "she was hiding in a bathroom",
           "her mate was asleep on the sofa", "she had fairy lights on the wall"]

TURN = ["she still would not hit skip", "she stayed on another twenty minutes",
        "she asked me to do it again", "she never disconnected once",
        "she typed her socials in the box", "she wanted to know if i recorded it",
        "she asked if anyone else was watching", "she made me promise not to screenshot",
        "she turned the lamp off and carried on", "she said one more time then went again",
        "she kept checking the door but stayed on",
        "she said she had never done this before and did not stop",
        "she counted down from three and did it twice"]

# emoji sit before the tags, the way the posts that actually land in this niche do
EMOJI = ["\U0001f440", "\U0001f629", "\U0001f605", "\U0001f92f", "\U0001f60f",
         "\U0001f525", "\U0001f9d0", "\U0001f636", "\U0001f971", "\U0001f61c",
         "\U0001f648", "\U0001f4a4", "\U0001f31a", "\U0001f440\U0001f525",
         "\U0001f92f\U0001f60f", "\U0001f97a", "\U0001f976", "\U0001f60c"]

OPEN = ["got matched with a", "ran into a", "landed on a", "hit skip and got a",
        "next match was a", "connected to a", "got paired with a", "rolled onto a"]

CLOSE = ["absolute scenes", "still thinking about it", "wild night on there",
         "did not expect that one", "she was not shy at all", "no idea how that happened",
         "best match all week", "genuinely lost for words", "that escalated fast",
         "cannot believe she stayed"]

# Operator decision 2026-09-07: keep the original three tags, fixed and in this
# exact order. They add 3 shared tokens to every caption, which is safe ONLY
# because the narrative body now supplies ~15-25 varying tokens per post — the
# old template had these same 3 tags and nothing else, which is what collapsed
# 773 captions into 4 unigram bags. Validated after the change: distinct unigram
# sets stayed at 100% of draws. If the narrative pools are ever trimmed back,
# re-run that check before trusting this.
TAGS = "#omegle #monkeyapp #ometv"

# --- sentence skeletons ------------------------------------------------------
# Kept structurally distinct so the shared-word backbone between any two captions
# stays small. {G}=girl {P}=platform {B}=beat {S}=setting {T}=turn {O}=open {C}=close
TEMPLATES = [
    "{O} {G} on {P} and she {B}. {S} and {T}",
    "{G} on {P} {B} the second she saw it. {T}",
    "{S}. the {G} on {P} still {B} and {T}",
    "this {G} {B} on {P} tonight. {T}, {C}",
    "{O} {G} on {P}. she {B}, then {T}",
    "{C} — {G} on {P} {B} and {T}",
    "had a {G} on {P} who {B}. {S}, and {T}",
    "{G} matched me on {P}, {B}, and {T}. {C}",
    "{S} and the {G} on {P} {B} anyway. {T}",
    "went on {P} bored, {O} {G} who {B}. {T}",
    "{G} on {P} tonight — {B}, then {T}. {C}",
    "the way this {G} {B} on {P}. {S} and {T}",
    "{O} {G} on {P} who {B} straight away. {C}",
    "not what i expected off {P} — {G} {B} and {T}",
    "{G} on {P}. {B}. {T}. {C}",
    "{S}. {G} on {P} {B} and {T} anyway",
]


def esc(s):
    # spintax is brace + pipe delimited; neither may appear in a literal
    return s.replace("{", "").replace("}", "").replace("|", "")


def alt(pool):
    return "{" + "|".join(esc(x) for x in pool) + "}"


def build():
    filled = []
    for i, t in enumerate(TEMPLATES):
        s = (t.replace("{O}", alt(OPEN)).replace("{G}", alt(GIRL))
              .replace("{P}", alt(PLAT)).replace("{B}", alt(BEAT))
              .replace("{S}", alt(SETTING)).replace("{T}", alt(TURN))
              .replace("{C}", alt(CLOSE)))
        # every 4th skeleton carries no emoji, so a quarter of posts go bare
        # without needing a blank alternative (which would leave a double space)
        tail = "" if i % 4 == 3 else " " + alt(EMOJI)
        tail += " " + TAGS
        filled.append(s + tail)
    return "{" + "|".join(filled) + "}"


# --- local reimplementation of the box's _expand_spintax, for validation -----
def expand(s):
    while True:
        m = re.search(r"\{([^{}]*)\}", s)
        if not m:
            return s
        s = s[:m.start()] + random.choice(m.group(1).split("|")) + s[m.end():]


def unigrams(t):
    return frozenset(re.findall(r"[#\w]+", t.lower()))


def theoretical():
    total = 0
    for i, t in enumerate(TEMPLATES):
        n = 1
        if i % 4 != 3:
            n *= len(EMOJI)
        for key, pool in (("{O}", OPEN), ("{G}", GIRL), ("{P}", PLAT), ("{B}", BEAT),
                          ("{S}", SETTING), ("{T}", TURN), ("{C}", CLOSE)):
            if key in t:
                n *= len(pool)
        total += n
    return total


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200000
    spin = build()
    samples = [expand(spin) for _ in range(n)]
    caps = set(samples)
    bags = set(unigrams(s) for s in samples)
    vocab = Counter()
    for s in samples:
        vocab.update(unigrams(s))
    big = Counter(unigrams(s) for s in samples).most_common(1)[0]

    print("spintax template size      : %d chars" % len(spin))
    print("THEORETICAL distinct captions: %s" % f"{theoretical():,}")
    print()
    print("drew %s samples" % f"{n:,}")
    print("  distinct captions        : %s" % f"{len(caps):,}")
    print("  DISTINCT UNIGRAM SETS    : %s" % f"{len(bags):,}")
    print("  distinct word tokens     : %s" % f"{len(vocab):,}")
    print("  largest unigram cluster  : %d posts = %.4f%% of sample"
          % (big[1], 100.0 * big[1] / n))
    print()
    print("=== 12 sample captions ===")
    for s in random.sample(samples, 12):
        print("  " + s)

    with open("spintax.txt", "w", encoding="utf-8") as f:
        f.write(spin)
