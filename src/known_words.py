"""Config-driven custom vocabulary correction.

Two stages, since Whisper (which hears the audio) does not know custom words and
Bedrock (which knows the words) never hears the audio:

- build_whisper_prompt: seeds Whisper's initial_prompt so Stage 1 is biased toward
  transcribing the correct spellings at the source.
- apply_corrections: deterministic case-insensitive text replacement of known
  mishearings, applied in BOTH batch and streaming modes (no model needed).

A known word is {"correct": "Dhimu", "sounds_like": ["demu", "dee moo"]}. The
"correct" spelling is also treated as one of its own variants so casing is fixed
even when Whisper already spelled it right.
"""
import re

_WORD_BOUNDARY = r"(?<![A-Za-z]){variant}(?![A-Za-z])"


def build_whisper_prompt(known_words):
    """Return a comma-joined vocabulary string for Whisper initial_prompt, or ''."""
    names = [w["correct"] for w in known_words if w.get("correct")]
    if not names:
        return ""
    return "Vocabulary: " + ", ".join(names) + "."


def _variants(word):
    correct = word.get("correct", "")
    seen = {}
    for v in [correct, *word.get("sounds_like", [])]:
        v = v.strip()
        if v and v.lower() not in seen:
            seen[v.lower()] = v
    variants = list(seen.values())
    variants.sort(key=lambda v: len(v), reverse=True)
    return correct, variants


def _compile(known_words):
    rules = []
    for word in known_words:
        correct, variants = _variants(word)
        if not correct:
            continue
        for variant in variants:
            if variant.lower() == correct.lower() and variant == correct:
                continue
            pattern = re.compile(
                _WORD_BOUNDARY.format(variant=re.escape(variant)),
                re.IGNORECASE,
            )
            rules.append((pattern, correct))
    return rules


def apply_corrections(text, known_words):
    """Replace known mishearings with their correct spelling. Case-insensitive
    match, whole-word only, replacement uses the configured correct casing."""
    if not text or not known_words:
        return text
    out = text
    for pattern, correct in _compile(known_words):
        out = pattern.sub(correct, out)
    return out
