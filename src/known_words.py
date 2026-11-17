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


def build_whisper_prompt(known_words, extra_names=()):
    """Return a short sentence in the speaker's own register naming the custom
    words, or ''. Whisper treats initial_prompt as the transcript that came just
    before, so a natural sentence biases spelling and style, while a labelled list
    ('Vocabulary: ...') gets continued and echoed back as if it were speech."""
    names = []
    for name in [*(w.get("correct", "") for w in known_words), *extra_names]:
        name = name.strip()
        if name and name[0].isupper() and name not in names:
            names.append(name)
    if not names:
        return ""
    listed = ", ".join(names[:-1]) + " and " + names[-1] if len(names) > 1 else names[0]
    return (
        f"Hung out with {listed} today. Lowkey gonna grab boba after, "
        "it was hella fun."
    )


def _tokens(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def is_prompt_echo(text, prompt, min_tokens=3):
    """True if text is just a contiguous run of the prompt's own words, which is
    Whisper regurgitating its initial_prompt on silence rather than speech."""
    out, src = _tokens(text), _tokens(prompt)
    if len(out) < min_tokens or not src:
        return False
    n = len(out)
    return any(src[i:i + n] == out for i in range(len(src) - n + 1))


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


_SENTENCE_START = re.compile(r"(?:^|[.?!]\s+)$")


def _replacement(correct):
    if correct != correct.lower():
        return correct

    def sub(m):
        if _SENTENCE_START.search(m.string[:m.start()]):
            return correct.capitalize()
        return correct

    return sub


def apply_corrections(text, known_words):
    """Replace known mishearings with their correct spelling. Case-insensitive
    match, whole-word only, replacement uses the configured correct casing; an
    all-lowercase word is capitalized only at the start of a sentence."""
    if not text or not known_words:
        return text
    out = text
    for pattern, correct in _compile(known_words):
        out = pattern.sub(_replacement(correct), out)
    return out
