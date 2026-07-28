#!/usr/bin/env python3
"""
Tests for callsign extraction.

The negative cases matter more than the positive ones. The extractor's whole
risk is false positives: many phonetic words are ordinary English, so plain
conversation trivially yields callsign-shaped token runs. Any change that makes
recall better while letting these through is a bad trade — QRZ will reject the
junk, but every rejection is a wasted lookup and a polluted log.
"""

import unittest

from phonetics import (
    _letter_o_as_zero,
    extract_callsigns,
    is_callsign_shaped,
    is_lookupable,
    normalise_callsign,
    tokenise,
)


def calls(text):
    return [c.callsign for c in extract_callsigns(text)]


class TestCallsignShape(unittest.TestCase):
    def test_accepts_real_formats(self):
        for call in [
            "M0ABC", "MM3NDH", "G4RS", "W1AW", "2E0ABC",
            "9A1A", "4X4ABC", "VK2XYZ", "OH2BH", "JA1ABC",
        ]:
            self.assertTrue(is_callsign_shaped(call), call)

    def test_rejects_non_callsigns(self):
        for text in ["ABC", "HELLO", "12345", "A", "TOOLONGCALLSIGN", "M"]:
            self.assertFalse(is_callsign_shaped(text), text)


class TestPhoneticExtraction(unittest.TestCase):
    def test_standard_nato_with_cue(self):
        text = "CQ CQ this is mike mike three november delta hotel calling CQ"
        self.assertIn("MM3NDH", calls(text))

    def test_without_cue_still_found_when_strict(self):
        text = "golf four alpha bravo charlie standing by"
        self.assertIn("G4ABC", calls(text))

    def test_ham_geographic_phonetics_with_cue(self):
        # Non-standard phonetics are extremely common on the air.
        text = "this is germany four radio sugar portable"
        self.assertIn("G4RS", calls(text))

    def test_aviation_digit_forms(self):
        text = "this is whiskey one alpha whiskey"
        self.assertIn("W1AW", calls(text))
        text = "kilo niner foxtrot oscar xray"
        self.assertIn("K9FOX", calls(text))

    def test_literal_callsign_in_text(self):
        text = "Thanks for the call MM3NDH, you are five nine here"
        self.assertIn("MM3NDH", calls(text))

    def test_literal_scores_high(self):
        best = extract_callsigns("Thanks MM3NDH for the contact")[0]
        self.assertEqual(best.source, "literal")
        self.assertGreater(best.confidence, 0.6)

    def test_cue_raises_confidence(self):
        cued = extract_callsigns(
            "this is mike mike three november delta hotel"
        )
        plain = extract_callsigns("mike mike three november delta hotel")
        self.assertTrue(cued and plain)
        self.assertGreater(cued[0].confidence, plain[0].confidence)

    def test_bare_digits_and_connector_bridging(self):
        """
        Real transcript from a live run: WhisperLive rendered a callsign as
        literal digits/letters split by a filler word rather than spelling
        everything as NATO words — "Golf Mike 6 and Z.A.K." for GM6ZAK. The
        extractor must recognise bare numerals, bare single letters, and
        bridge the "and" without losing evidence.
        """
        self.assertIn("GM6ZAK", calls("Golf Mike 6 and Z.A.K. Roger?"))

    def test_number_bridges_like_a_connector(self):
        # "Foxtrot number 2 Alpha Bravo Charlie" is normal operator speech
        # for F2ABC — "number" must not break the run.
        self.assertIn("F2ABC", calls("foxtrot number 2 alpha bravo charlie"))
        self.assertIn(
            "F2ABC",
            calls("this is foxtrot number two alpha bravo charlie calling"),
        )

    def test_number_alone_stays_inert(self):
        for text in [
            "what is the number for the pizza place",
            "give me your phone number and address",
        ]:
            self.assertEqual(calls(text), [], f"false positive on: {text}")

    def test_trailing_roger_not_absorbed(self):
        # "Roger" is the near-universal acknowledgment word and sits directly
        # after callsigns constantly; it must not be swallowed as a phonetic R.
        found = extract_callsigns("Golf Mike 6 and Z.A.K. Roger?")
        self.assertTrue(found)
        self.assertNotIn("GM6ZAKR", [c.callsign for c in found])

    def test_confidence_has_no_float_noise_at_default_threshold(self):
        """
        Regression: summing the 0.25/0.10/0.05-ish confidence terms produced
        0.39999999999999997 for some real inputs (observed live, "At Q Echo 1
        Delta Foxx..."), which then failed scanner.py's strict
        "confidence < 0.4" comparison against the default
        --min-extract-confidence even though the true score is exactly 0.4.
        Every candidate's confidence must land on a clean value so threshold
        comparisons behave as documented.

        The expected callsign is QE1DF, not QE1D: once "foxx" (double-x
        spelling of "fox") was added to the loose-letter map, the run
        legitimately extends one character further than when this test was
        first written — a correctness improvement, not a regression.
        """
        found = extract_callsigns(
            "At Q Echo 1 Delta Foxx on India, you are 5 and 7 now on Madeira."
        )
        self.assertTrue(found)
        candidate = next((c for c in found if c.callsign == "QE1DF"), None)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.confidence, 0.4)
        self.assertGreaterEqual(candidate.confidence, 0.4)  # the actual gate scanner.py applies

    def test_multidigit_numeral_is_strict(self):
        text = "this is delta lima 96 kilo"
        found = extract_callsigns(text)
        self.assertTrue(found)

    def test_right_trim_strips_trailing_signoff_number(self):
        """
        Real live capture: IZ4DJD, confirmed ground truth. A multi-digit
        sign-off ("73") is STRICT digit evidence, so it gets swept into the
        run and produces a shape-invalid string ending in digits (a
        callsign's suffix must be letters only). Trimming must be able to
        drop trailing as well as leading tokens to recover the real call.
        """
        found = extract_callsigns(
            "Papaico, Italy, Zulu 4, Delta, Juliet, Delta, 73, Tobias."
        )
        self.assertIn("IZ4DJD", [c.callsign for c in found])

    def test_right_trim_stays_safe_on_ordinary_signoffs(self):
        # Ordinary speech ending in a number is extremely common (times,
        # signal reports, "73") — right-trimming must not turn any of it
        # into a callsign on its own.
        for text in [
            "this is a great day 73",
            "thanks for the chat see you at 20",
            "my radio uses about 50",
            "the temperature here is about 20",
            "golf course closes at 73",
            "thanks victor romeo oscar for the call 20",
        ]:
            self.assertEqual(calls(text), [], f"false positive on: {text}")

    def test_spoken_suffix_captured(self):
        found = [
            c for c in extract_callsigns("this is germany four radio sugar portable")
            if c.callsign == "G4RS"
        ]
        self.assertTrue(found)
        self.assertEqual(found[0].suffix, "/P")


class TestFalsePositives(unittest.TestCase):
    """Ordinary speech must not produce callsigns."""

    def test_plain_conversation(self):
        for text in [
            "I would like for you to read that back to me",
            "we have one or two things to do",
            "it was for the best and the weather is fine",
            "thanks for watching and don't forget to subscribe",
            "the king and queen were easy to see",
            "hello there how are you doing today",
            "that is one to four and back again",
        ]:
            self.assertEqual(calls(text), [], f"false positive on: {text}")

    def test_bare_digits_and_letters_in_ordinary_speech(self):
        """
        Guards the extended surface added for real-world Whisper output: bare
        numerals ("96"), bare single letters ("a"), and connector-word
        bridging ("and") must not turn ordinary sentences into callsigns.
        """
        for text in [
            "and I have a lot of things to say to you today",
            "she said 6 and 3 were the numbers he gave me",
            "a b c d e f g learning the alphabet with my son",
            "one two three four five six count with me",
            "my flight number was 96 and I missed it completely",
            "the meeting is at 2 and 4 today",
            "grab a coffee and a snack for the trip",
            "have you and I met before at the store",
            "this is uh a really nice day for a walk",
            "a e i o u are the vowels in english",
        ]:
            self.assertEqual(calls(text), [], f"false positive on: {text}")

    def test_expanded_loose_letter_vocabulary_stays_safe(self):
        """
        LOOSE_LETTERS carries many geographic/first-name alternatives per
        letter (hams routinely say "Germany Four Radio Sugar" instead of
        NATO). More words means more collision surface with ordinary
        sentences, so this stresses dense combinations of the newer entries
        specifically.
        """
        for text in [
            "america and canada are both in the western hemisphere",
            "george went to germany and then to japan for business",
            "my uncle king henry lives in london with mary and peter",
            "washington and virginia are both us states near dc",
            "the golf tournament in florida was fun and mexico too",
            "sydney and york are both in the news this week extra",
            "yellow and young david went to denver with frank and fred",
            "russia and poland border each other near warsaw",
            "thomas and richard went fishing near sydney scotland",
            "my friend jack and john went to japan and korea",
        ]:
            self.assertEqual(calls(text), [], f"false positive on: {text}")

    def test_extra_maps_to_xray(self):
        # "extra" for X-ray is a very common live ASR mishearing (observed
        # repeatedly), distinct from the literal word "xylophone".
        self.assertIn(
            "G4EXR", calls("this is golf four echo extra romeo calling")
        )

    def test_vowelless_blob_letters(self):
        """
        Whisper sometimes glues several spelled letters into one token with
        no separating space — observed live: "JXG" for part of MI3JXG. A
        short all-consonant cluster is very unlikely to be a real English
        word (virtually all of them contain a/e/i/o/u/y), so it's treated as
        bare letters rather than an opaque unmapped word.
        """
        self.assertIn("GM6JXG", calls("golf mike six and jxg over"))
        self.assertIn("GX3JXG", calls("this is golf x-ray three jxg calling"))

    def test_vowelless_real_abbreviations_stay_safe(self):
        # The risk with treating consonant blobs as bare letters: real
        # vowel-free abbreviations must not become false positives on their
        # own strength.
        for text in [
            "I watched the news on TV last night",
            "CNN and BBC both covered the story",
            "we drove into NYC for the weekend",
            "please RSVP by friday for the party",
            "the NFL and MLB seasons start soon",
            "my TV and CD player both broke",
            "the DJ played great music at the club",
            "I need to check my ID before we go",
            "FYI the meeting moved to friday",
            "the CD skipped during the song",
        ]:
            self.assertEqual(calls(text), [], f"false positive on: {text}")

    def test_loose_only_run_without_cue_rejected(self):
        # "for radio sugar" is 4RS — all loose, no cue. Must not fire.
        self.assertEqual(calls("waiting for radio sugar to arrive"), [])

    def test_empty_and_noise(self):
        for text in ["", "   ", "...", "uh um er"]:
            self.assertEqual(calls(text), [])


class TestNormalisation(unittest.TestCase):
    def test_strips_known_suffixes(self):
        self.assertEqual(normalise_callsign("MM3NDH/P"), "MM3NDH")
        self.assertEqual(normalise_callsign("W1AW/QRP"), "W1AW")
        self.assertEqual(normalise_callsign("G4ABC/M"), "G4ABC")

    def test_strips_country_prefix(self):
        self.assertEqual(normalise_callsign("G/MM3NDH"), "MM3NDH")
        self.assertEqual(normalise_callsign("VK/W1AW"), "W1AW")

    def test_keeps_longer_part(self):
        self.assertEqual(normalise_callsign("W1AW/KH6ABC"), "KH6ABC")

    def test_plain_callsign_unchanged(self):
        self.assertEqual(normalise_callsign("mm3ndh"), "MM3NDH")


class TestTokenise(unittest.TestCase):
    def test_hyphen_preserved(self):
        self.assertIn("x-ray", tokenise("x-ray"))

    def test_punctuation_stripped(self):
        self.assertEqual(tokenise("Hello, world!"), ["hello", "world"])



class TestLookupGate(unittest.TestCase):
    """The final gate before a QRZ request is spent."""

    def test_accepts_normal_callsigns(self):
        for call in ["M0ABC", "MM3NDH", "W1AW", "2E0ABC", "9A1A"]:
            self.assertTrue(is_lookupable(call), call)

    def test_rejects_post_normalisation_junk(self):
        # "G/M" normalises to "M" — shaped like nothing, and the server would
        # 400 it for being under 3 characters.
        self.assertFalse(is_lookupable(normalise_callsign("G/M")))

    def test_rejects_wrong_shape(self):
        for call in ["ABC", "HELLO", "12345", "TOOLONGCALL"]:
            self.assertFalse(is_lookupable(call), call)

    def test_rejects_non_alphanumeric(self):
        self.assertFalse(is_lookupable("M0-ABC"))
        self.assertFalse(is_lookupable("MM3NDH/P"))


class TestLetterOHeardAsZero(unittest.TestCase):
    """
    "Zero" and "Oscar"/"oh" are among the most-confused pairs on noisy SSB.
    A run spelled with Oscar where zero was meant has no digit at all, so it
    fails the ITU shape check outright and never even reaches QRZ — observed
    live as EOEOJ, which is E0EOJ with the zero lost.
    """

    def test_rescues_zero_heard_as_oscar(self):
        self.assertIn(
            "E0EOJ", [c.callsign for c in extract_callsigns("echo oscar echo oscar juliet")]
        )
        self.assertIn(
            "M0ABC",
            [c.callsign for c in extract_callsigns("this is mike oscar alpha bravo charlie")],
        )

    def test_real_digits_are_untouched(self):
        # The rescue must only fire when there is no digit at all, so a run
        # that already parsed correctly cannot be rewritten by it.
        self.assertIn(
            "M0ABC",
            [c.callsign for c in extract_callsigns("this is mike zero alpha bravo charlie")],
        )

    def test_ordinary_speech_is_not_rescued(self):
        # The same substitution applied to literal text would turn TOM, NOT,
        # GOT, HOT, JOB, HOUSE and LONDON into callsign shapes. It is confined
        # to phonetic runs precisely so plain conversation cannot reach it.
        for text in [
            "tom is not at home",
            "it got hot in the house",
            "my job in london",
            "roger roger good copy",
            "the dog and the log",
            "top of the hour",
            "i have a radio and a motor",
        ]:
            self.assertEqual([c.callsign for c in extract_callsigns(text)], [], text)

    def test_ambiguous_runs_are_left_alone(self):
        # Adjacent O's make both substitutions land in a valid digit position,
        # so there is no way to tell which zero was spoken. That is a guess,
        # not a rescue, so it is refused rather than picked arbitrarily.
        for ambiguous in ["GOOD", "MOON", "NOON"]:
            self.assertIsNone(_letter_o_as_zero(ambiguous), ambiguous)

    def test_unambiguous_single_substitution_is_taken(self):
        # Only one position yields a valid shape here, so there is nothing to
        # guess between and the rescue applies.
        self.assertEqual(_letter_o_as_zero("MOTO"), "M0TO")

    def test_no_o_or_already_has_digit(self):
        self.assertIsNone(_letter_o_as_zero("ABCDE"))
        self.assertIsNone(_letter_o_as_zero("M0ABC"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
