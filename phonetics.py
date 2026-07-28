"""
Callsign extraction from speech-to-text output.

The hard problem this file solves: Whisper returns ordinary English prose, and a
spoken callsign arrives as words — "mike mike three november delta hotel" — or
occasionally already spelled out ("MM3NDH"), or as some mixture of the two.

Two things make naive matching useless:

  1. Amateur operators routinely ignore the NATO alphabet. "Germany Four Radio
     Sugar" is a perfectly ordinary way to send G4RS on the air.
  2. Many phonetic words are also ordinary English. "for" is 4, "to" is 2, "one"
     is 1, "oh" is 0, "king" is K. A transcript of someone saying "for you to
     read" trivially yields the token run 4-U-2-R.

So mappings are split into two tiers. STRICT words are unambiguous on-air
phonetics that essentially never appear in conversational English ("foxtrot",
"niner", "zulu"). LOOSE words are the ambiguous ones. A run of tokens is only
promoted to a candidate if it carries enough strict evidence, or if it directly
follows a cue phrase like "this is" / "cq" / "de", which is where callsigns
actually live.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Phonetic vocabulary
# ---------------------------------------------------------------------------

# Unambiguous on-air phonetics — these words are vanishingly rare in ordinary
# speech, so a match is strong evidence we are inside a callsign.
STRICT_LETTERS: Dict[str, str] = {
    "alpha": "A", "alfa": "A",
    "bravo": "B",
    "charlie": "C", "charley": "C",
    "delta": "D",
    "foxtrot": "F",
    "golf": "G",
    "hotel": "H",
    "india": "I",
    "juliet": "J", "juliett": "J", "juliette": "J", "joliet": "J",
    "kilo": "K",
    "lima": "L",
    "november": "N",
    "oscar": "O",
    "papa": "P",
    "quebec": "Q",
    "romeo": "R",
    "sierra": "S",
    "tango": "T",
    "uniform": "U",
    "victor": "V",
    "whiskey": "W", "whisky": "W",
    "xray": "X", "x-ray": "X",
    "yankee": "Y",
    "zulu": "Z",
}

# Ambiguous phonetics: real English words, common ASR mishearings, and the
# non-standard "geographic"/first-name phonetics hams use constantly instead
# of strict NATO. Deliberately generous — false positives are guarded against
# by the evidence gate (2 strict chars, or a cue phrase, or a long run) and
# the mandatory embedded-digit shape check, not by keeping this list short.
# See test_bare_digits_and_letters_in_ordinary_speech and the rest of the
# false-positive corpus in test_phonetics.py, which every addition here must
# keep passing.
LOOSE_LETTERS: Dict[str, str] = {
    "america": "A", "american": "A", "amsterdam": "A", "able": "A",
    "argentina": "A", "africa": "A", "australia": "A", "adam": "A",
    "alfred": "A", "andrew": "A",
    "boston": "B", "baker": "B", "bravo!": "B", "brazil": "B",
    "belgium": "B", "benjamin": "B", "barcelona": "B", "billy": "B",
    "canada": "C", "california": "C", "chicago": "C", "columbia": "C",
    "colorado": "C", "china": "C", "charlie!": "C",
    "denmark": "D", "david": "D", "dog": "D", "dublin": "D",
    "denver": "D", "douglas": "D",
    "echo": "E", "england": "E", "easy": "E", "edward": "E",
    "egypt": "E", "europe": "E", "edinburgh": "E", "eddie": "E",
    "florida": "F", "fox": "F", "foxx": "F", "france": "F", "frank": "F",
    "finland": "F", "freddie": "F", "fred": "F",
    "germany": "G", "george": "G", "guatemala": "G", "greece": "G",
    "glasgow": "G", "gibraltar": "G", "golf!": "G",
    "gulf": "G",  # common ASR mishearing of "Golf" (observed live)
    "henry": "H", "honolulu": "H", "havana": "H", "holland": "H",
    "harry": "H", "houston": "H", "hotel!": "H",
    "italy": "I", "india!": "I", "item": "I", "ireland": "I",
    "iceland": "I", "indonesia": "I", "ivan": "I",
    "julia": "J", "julie": "J", "giulia": "J",  # common ASR mishearings of "Juliet" (observed live)
    "japan": "J", "john": "J", "jupiter": "J", "jamaica": "J",
    "johannesburg": "J", "jack": "J",
    "king": "K", "kilowatt": "K", "kentucky": "K", "kenya": "K",
    "korea": "K", "kansas": "K", "kilo!": "K",
    "london": "L", "lincoln": "L", "love": "L", "liverpool": "L",
    "lithuania": "L", "larry": "L",
    "mike": "M", "mic": "M", "michael": "M", "mexico": "M", "mary": "M",
    "madrid": "M", "moscow": "M", "montreal": "M", "malta": "M",
    "norway": "N", "nancy": "N", "nov": "N", "nigeria": "N",
    "nairobi": "N", "norman": "N",
    "ontario": "O", "ocean": "O", "oboe": "O", "oslo": "O",
    "oxford": "O", "ottawa": "O", "oscar!": "O",
    "portugal": "P", "peter": "P", "pacific": "P", "paris": "P",
    "panama": "P", "poland": "P", "papa!": "P",
    "queen": "Q", "quebec!": "Q", "quito": "Q",
    "radio": "R", "rome": "R", "russia": "R", "richard": "R",
    "romeo!": "R",
    # "roger" deliberately excluded: on the air it is almost always the
    # acknowledgment word, not a phonetic stand-in for R, and it sits directly
    # after callsigns constantly ("...Zulu Alpha Kilo, Roger?"). Mapping it
    # let it get absorbed as a trailing character, corrupting the run
    # (observed live: "Golf Mike 6 and Z A K Roger?" → GM6ZAKR instead of the
    # real GM6ZAK). Leaving it unmapped makes the run end cleanly before it.
    "sugar": "S", "santiago": "S", "spain": "S", "sweden": "S",
    "sydney": "S", "scotland": "S", "samuel": "S", "sierra!": "S",
    "texas": "T", "tokyo": "T", "toronto": "T", "turkey": "T",
    "tennessee": "T", "thomas": "T",
    "union": "U", "united": "U", "uncle": "U", "uruguay": "U", "utah": "U",
    "venezuela": "V", "victoria": "V", "vienna": "V", "virginia": "V",
    "vancouver": "V",
    "washington": "W", "william": "W", "wales": "W", "wisconsin": "W",
    "warsaw": "W", "walter": "W",
    "xylophone": "X",
    "extra": "X",  # observed live — a very common ASR mishearing of "x-ray"
    "yokohama": "Y", "yellow": "Y", "young": "Y", "york": "Y",
    "yugoslavia": "Y",
    "zanzibar": "Z", "zed": "Z", "zebra": "Z", "zambia": "Z",
    "zimbabwe": "Z", "zurich": "Z",
}

# Digit words. Aviation/on-air forms ("niner", "fife", "fower", "tree") are
# strict; the plain English numbers are ambiguous.
STRICT_DIGITS: Dict[str, str] = {
    "niner": "9",
    "fife": "5",
    "fower": "4",
    "tree": "3",
    "zeero": "0",
}

LOOSE_DIGITS: Dict[str, str] = {
    "zero": "0", "oh": "0", "o": "0", "nought": "0", "naught": "0",
    "one": "1", "won": "1", "wun": "1",
    "two": "2", "too": "2", "to": "2",
    "three": "3", "free": "3", "thee": "3",
    "four": "4", "for": "4", "fore": "4",
    "five": "5", "phi": "5",  # accented ASR mishearing of "five" (observed live, HA5JI)
    "six": "6", "sicks": "6",
    "seven": "7",
    "eight": "8", "ate": "8", "ait": "8",
    "nine": "9", "nina": "9",
}

# Phrases that immediately precede a callsign on the air. A run following one of
# these within a couple of tokens gets a large confidence boost, which is what
# lets us accept otherwise-ambiguous runs like "for radio sugar".
CUE_PHRASES: List[List[str]] = [
    ["this", "is"],
    ["cq"],
    ["de"],
    ["from"],
    ["callsign"],
    ["call", "sign"],
    ["my", "call"],
    ["station"],
    ["calling"],
    ["qrz"],
    ["back", "to"],
    ["over", "to"],
    ["handle", "is"],
    ["name", "here", "is"],
]

# Words that terminate a callsign run — spoken suffixes and sign-off words that
# would otherwise be swallowed into the candidate.
SUFFIX_WORDS: Dict[str, str] = {
    "portable": "/P",
    "mobile": "/M",
    "maritime": "/MM",
    "stroke": "/",
    "slash": "/",
}

STRICT_MAP = {**STRICT_LETTERS, **STRICT_DIGITS}
LOOSE_MAP = {**LOOSE_LETTERS, **LOOSE_DIGITS}
FULL_MAP = {**LOOSE_MAP, **STRICT_MAP}  # strict wins on collision

# Whisper does not consistently spell digits and letters as words. Live
# transcripts against a real instance produced things like "Delta Lima 2 Lima
# Tulu" and "Golf Mike 6 and Z.A.K." — the "2" and "6" are bare numerals, and
# "Z", "A", "K" are bare single letters, none of which the word-level maps
# above can see. Without handling these, exactly the segments most likely to
# be genuine callsigns were silently dropped before extraction even started.
#
# Bare single letters are necessarily LOOSE — "a"/"i"/"o" collide with the
# most common words in English, so on their own they carry no evidence. They
# are only useful for bridging inside a run that strict/cue evidence already
# justifies; the len>=3 minimum and the mandatory embedded-digit shape check
# (CALLSIGN_RE) keep a stray "a" or "i" from starting a false match — a random
# stretch of English very rarely happens to alternate letters and digits in
# the exact pattern a callsign requires.
#
# Bare digit clusters of 2+ characters (e.g. "96", "73") are treated as
# STRICT: a multi-digit numeral sitting inside otherwise phonetic speech is
# not something ordinary conversation produces by coincidence. A lone single
# digit ("2", "4") is far more ambiguous — it is exactly as likely to be a
# stray quantity as part of a callsign — so it stays LOOSE.
_VOWELS = set("aeiouy")  # y counted so "by", "my", "sky", "gym" don't qualify


def _token_mapping(token: str) -> Optional[Tuple[str, bool]]:
    """Return (chars, is_strict) if `token` contributes to a callsign run."""
    if token in STRICT_MAP:
        return STRICT_MAP[token], True
    if token in LOOSE_MAP:
        return LOOSE_MAP[token], False
    if token.isdigit():
        return token, len(token) >= 2
    if len(token) == 1 and token.isalpha():
        return token.upper(), False
    # A short all-consonant cluster ("jxg", "xlr") is very unlikely to be a
    # real English word — virtually every one contains a/e/i/o/u/y — so it's
    # a strong signal Whisper glued several spelled letters into one token
    # with no separating space (observed live: "JXG" for part of MI3JXG).
    # Loose, not strict: still needs corroborating evidence via the usual
    # gate before a run is promoted, which is what keeps genuine vowel-free
    # abbreviations ("TV", "CNN", "NYC") from causing false positives on
    # their own — verified in test_vowelless_blob_letters.
    if 2 <= len(token) <= 5 and token.isalpha() and not any(c in _VOWELS for c in token):
        return token.upper(), False
    # Hyphen-joined single characters ("I-0-W-F-T") — the tokenizer keeps
    # internal hyphens so "x-ray" survives as one word, but that means a
    # literally-spelled callsign written with hyphens instead of spaces glues
    # into one opaque token too (observed live, confirmed real: IU0WFT
    # rendered as "I-0-W-F-T"). Only fires when EVERY hyphen-separated part is
    # a single character, so "x-ray" (parts "x", "ray") is unaffected and
    # falls through to the LOOSE_MAP lookup above as before.
    if "-" in token:
        parts = token.split("-")
        if len(parts) >= 2 and all(len(p) == 1 and p.isalnum() for p in parts):
            return "".join(parts).upper(), False
    return None


# Filler words that may interrupt an otherwise-continuous callsign spelling
# without breaking it — "Golf Mike 6 and Z A K" is one utterance, not two.
# Only bridged when a mappable token follows immediately, so a connector at
# the true end of a run still ends it normally.
#
# "number" is operator speech, not filler: "Foxtrot number 2 Alpha Bravo
# Charlie" is a normal way to give F2ABC. Bridging it here is still safe for
# ordinary uses of the word ("what's the number") since the same
# followed-by-something-mappable rule applies — "number" alone never starts
# or extends a run.
CONNECTOR_WORDS = {"and", "uh", "um", "er", "ah", "number"}


def _letter_o_as_zero(text: str) -> Optional[str]:
    """
    Rescue a spelled run whose zero was heard as the letter O.

    Every ITU callsign carries a digit, so an all-letter run can never be one
    — but "zero" and "Oscar"/"oh" are among the most-confused pairs on noisy
    SSB, and Whisper writes the letter either way. Observed live: EOEOJ, which
    is E0EOJ with the zero lost.

    Returns the single-substitution variant that is callsign-shaped, or None.
    Tries one O at a time and requires exactly one candidate shape to work, so
    an ambiguous run is left alone rather than guessed at.

    Deliberately NOT applied to literally-spelled tokens (extract_literal):
    there the same substitution turns ordinary conversation into callsigns —
    TOM, NOT, GOT, HOT, JOB, HOUSE, LONDON and friends all become
    callsign-shaped, and each would cost a QRZ lookup and risk a coincidental
    hit. A phonetic run is safe because it only exists when someone actually
    spelled the letters out and the run then cleared the evidence gate; the
    spoken word "Tom" never forms one, only "Tango Oscar Mike" does.
    """
    if "O" not in text or any(ch.isdigit() for ch in text):
        return None
    variants = {
        text[:i] + "0" + text[i + 1:]
        for i, ch in enumerate(text)
        if ch == "O"
    }
    shaped = [v for v in variants if is_callsign_shaped(v)]
    return shaped[0] if len(shaped) == 1 else None


def _run_evidence(tokens: List[str]) -> Tuple[int, int]:
    """Total (strict_chars, loose_chars) contributed by a token slice."""
    strict_chars = 0
    loose_chars = 0
    for token in tokens:
        mapping = _token_mapping(token)
        if mapping is None:
            continue
        chars, is_strict = mapping
        if is_strict:
            strict_chars += len(chars)
        else:
            loose_chars += len(chars)
    return strict_chars, loose_chars

# ---------------------------------------------------------------------------
# Callsign structure
# ---------------------------------------------------------------------------

# ITU-shaped callsign: prefix (1-2 letters, or letter+digit, or digit+letter),
# a separating digit or two, then a 1-4 letter suffix.
# Matches M0ABC, MM3NDH, G4RS, W1AW, 2E0ABC, 9A1A, 4X4ABC, VK2XYZ.
CALLSIGN_RE = re.compile(r"^(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9]{1,2}[A-Z]{1,4}$")

# Callsigns Whisper wrote out literally, e.g. "MM3NDH" or "M0ABC" mid-sentence.
LITERAL_RE = re.compile(
    r"\b(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9]{1,2}[A-Z]{1,4}\b"
)

# Word-ish tokens; keeps hyphens so "x-ray" survives tokenisation.
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'-]*")


# What the server itself will accept, post-normalisation (reValidCallsign in
# lookup_api.go). Anything failing this is a guaranteed 400, so there is no
# point spending a request on it.
SERVER_CALLSIGN_RE = re.compile(r"^[A-Z0-9]{3,10}$")


def is_callsign_shaped(text: str) -> bool:
    """True if `text` has the structure of an amateur callsign."""
    return bool(CALLSIGN_RE.match(text))


def is_lookupable(call: str) -> bool:
    """
    Final gate before spending a lookup.

    Re-checks structure *after* normalisation, which the extraction-time check
    cannot do: stripping a prefix overlay can leave something that is no longer
    a callsign at all ("G/M" normalises to "M"). Also enforces the server's own
    3-10 alphanumeric rule so we never send a request that is certain to 400.
    """
    call = call.upper().strip()
    if not SERVER_CALLSIGN_RE.match(call):
        return False
    return is_callsign_shaped(call)


@dataclass
class Candidate:
    """A possible callsign recovered from a transcript."""

    callsign: str
    source: str                  # "phonetic" | "literal"
    confidence: float            # 0.0-1.0, heuristic prior before validation
    strict_tokens: int = 0       # strict-evidence character count (not token count)
    loose_tokens: int = 0        # loose-evidence character count
    cued: bool = False
    context: str = ""            # surrounding transcript text, for the log
    suffix: str = ""             # spoken "/P", "/M" etc. if heard

    def __hash__(self) -> int:
        return hash((self.callsign, self.source))


def normalise_text(text: str) -> str:
    """Lowercase and strip punctuation that would break tokenisation."""
    text = text.lower()
    text = text.replace("_", " ")
    # Keep intra-word hyphens/apostrophes; drop everything else.
    text = re.sub(r"[^a-z0-9'\- ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokenise(text: str) -> List[str]:
    return TOKEN_RE.findall(normalise_text(text))


def _cue_positions(tokens: List[str]) -> Set[int]:
    """Token indices at which a callsign could plausibly start."""
    positions: Set[int] = set()
    for cue in CUE_PHRASES:
        n = len(cue)
        for i in range(len(tokens) - n + 1):
            if tokens[i:i + n] == cue:
                # Allow a token or two of slack ("this is, uh, mike mike three").
                positions.update({i + n, i + n + 1, i + n + 2})
    return positions


def extract_phonetic(tokens: List[str], cues: Set[int]) -> List[Candidate]:
    """Find runs of phonetic tokens and promote the plausible ones."""
    candidates: List[Candidate] = []
    i = 0
    while i < len(tokens):
        if _token_mapping(tokens[i]) is None:
            i += 1
            continue

        # Consume the maximal run of mappable tokens, bridging a single
        # connector word ("and", "uh", ...) when another mappable token
        # follows — Whisper renders one continuous spelling as multiple
        # segments joined by these, e.g. "golf mike 6 and z a k".
        start = i
        chars: List[str] = []
        while i < len(tokens):
            mapping = _token_mapping(tokens[i])
            if mapping is None:
                if (
                    tokens[i] in CONNECTOR_WORDS
                    and i + 1 < len(tokens)
                    and _token_mapping(tokens[i + 1]) is not None
                ):
                    i += 1
                    continue
                break
            chars.append(mapping[0])
            i += 1

        # A spoken suffix directly after the run ("...delta hotel portable").
        suffix = ""
        if i < len(tokens) and tokens[i] in SUFFIX_WORDS:
            suffix = SUFFIX_WORDS[tokens[i]]

        cued = any(pos in cues for pos in range(start, start + 2))

        # Try the whole run first, then progressively trim from the left
        # and/or right. A run often has a leading stray ("...and four radio
        # sugar" → AND4RS) — the callsign is the right-hand part — or a
        # trailing one: a multi-digit sign-off number ("...Juliet Delta 73")
        # is STRICT digit evidence, so it gets swept in and produces a
        # shape-invalid string (a callsign's suffix must be letters only).
        # Right trim is tried first at each offset since a trailing sign-off
        # is the more common case observed live; left offset still wins
        # overall preference (tried in the outer loop) since a leading stray
        # word was the original, more common failure mode this trimming was
        # built for.
        max_offset = min(3, max(0, len(chars) - 3))
        for offset in range(0, max_offset + 1):
            max_right_trim = min(2, max(0, len(chars) - offset - 3))
            for right_trim in range(0, max_right_trim + 1):
                end = len(chars) - right_trim
                text = "".join(chars[offset:end])
                if len(text) < 3 or len(text) > 10:
                    continue
                if not is_callsign_shaped(text):
                    text = _letter_o_as_zero(text)
                    if text is None:
                        continue

                run_strict, run_loose = _run_evidence(
                    tokens[start + offset:i - right_trim]
                )

                # Evidence gate, weighted rather than a bare token count: 2
                # points per strict char, 2/3 per loose char, +2 for a cue;
                # needs >=4 to pass. This is deliberately permissive on long
                # all-loose runs (6+ loose chars clears the bar unaided)
                # because the mandatory embedded-digit shape check above
                # already rejects the vast majority of coincidental English
                # word sequences — getting a long run to ALSO alternate
                # letters/digits correctly is rare. Verified against the
                # false-positive corpus in test_phonetics.py.
                evidence = 2 * run_strict + (2 / 3) * run_loose + (2 if cued else 0)
                if evidence < 4:
                    continue

                confidence = 0.25
                confidence += 0.10 * min(run_strict, 4)
                if cued:
                    confidence += 0.25
                if offset == 0 and right_trim == 0:
                    confidence += 0.05
                # Round off before storing: summing several 0.1-ish float
                # literals here can land a hair below a clean boundary
                # (observed live: 0.25+0.10+0.05 evaluated to
                # 0.39999999999999997), which then fails a strict "< 0.4"
                # threshold comparison downstream even though the real score
                # is exactly 0.4. Rounding makes the value match what a
                # threshold like --min-extract-confidence 0.4 means.
                confidence = round(min(confidence, 0.95), 3)

                # A spoken suffix only applies when nothing was trimmed off
                # the right — it described what followed the FULL run, not a
                # right-trimmed subset of it.
                run_suffix = suffix if right_trim == 0 else ""

                candidates.append(
                    Candidate(
                        callsign=text,
                        source="phonetic",
                        confidence=confidence,
                        strict_tokens=run_strict,
                        loose_tokens=run_loose,
                        cued=cued,
                        context=" ".join(tokens[max(0, start - 4):i + 2]),
                        suffix=run_suffix,
                    )
                )
                break  # first (longest) shape-valid right-trim wins
            else:
                continue
            break  # first (longest) shape-valid offset wins

    return candidates


def extract_literal(text: str, tokens: List[str], cues: Set[int]) -> List[Candidate]:
    """Find callsigns Whisper already spelled out."""
    candidates: List[Candidate] = []
    for match in LITERAL_RE.finditer(text.upper()):
        call = match.group(0)
        if not is_callsign_shaped(call):
            continue
        # A literal hit is strong on its own — Whisper does not usually emit
        # callsign-shaped alphanumeric strings by accident.
        confidence = 0.7
        if cues:
            confidence += 0.1
        candidates.append(
            Candidate(
                callsign=call,
                source="literal",
                confidence=round(min(confidence, 0.95), 3),
                strict_tokens=0,
                loose_tokens=0,
                cued=bool(cues),
                context=text[max(0, match.start() - 40):match.end() + 20].strip(),
            )
        )
    return candidates


def extract_callsigns(text: str) -> List[Candidate]:
    """
    Extract candidate callsigns from one transcript segment.

    Returns candidates ordered by descending heuristic confidence. Confidence
    here is only a prior — it says how callsign-like the utterance was, not
    whether the callsign exists. Validation against CTY/QRZ happens downstream.
    """
    if not text or not text.strip():
        return []

    tokens = tokenise(text)
    if not tokens:
        return []

    cues = _cue_positions(tokens)

    candidates = extract_literal(text, tokens, cues)
    candidates.extend(extract_phonetic(tokens, cues))

    # Deduplicate, keeping the highest-confidence instance of each callsign.
    best: Dict[str, Candidate] = {}
    for cand in candidates:
        existing = best.get(cand.callsign)
        if existing is None or cand.confidence > existing.confidence:
            best[cand.callsign] = cand

    return sorted(best.values(), key=lambda c: c.confidence, reverse=True)


# ---------------------------------------------------------------------------
# Normalisation — mirrors NormaliseCallsign in qrz_lookup.go so that what we
# send to /api/lookup matches what the server will key its cache on.
# ---------------------------------------------------------------------------

KNOWN_SUFFIXES = {
    "P", "M", "MM", "AM", "QRP", "A", "B", "R", "LH", "BCN", "AG", "AE", "KT",
}


def normalise_callsign(call: str) -> str:
    """Strip portable suffixes and country-prefix overlays."""
    call = call.upper().strip()
    if not call:
        return call

    parts = call.split("/", 1)
    if len(parts) == 1:
        return call

    left, right = parts[0], parts[1]

    if right in KNOWN_SUFFIXES:
        return left
    if left.isalpha() and len(left) <= 3:
        return right
    if len(right) > len(left):
        return right
    return left
