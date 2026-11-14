"""Configuration for voice-dictation. Loads/saves JSON at project root."""
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PROJECT_ROOT, "config.json")
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")
STREAM_DEBUG_LOG = os.path.join(LOG_DIR, "stream_debug.log")

DEFAULTS = {
    "aws_profile": "default",
    "aws_region": "us-west-2",
    "path_prepend": [],
    "bedrock_model_id": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "cleanup_enabled": True,
    "cleanup_timeout_seconds": 3.0,
    "cleanup_max_tokens": 512,
    "transcribe_timeout_seconds": 60.0,
    "whisper_model": "mlx-community/distil-whisper-large-v3",
    "whisper_model_label": "distil-large-v3",
    "metal_cache_limit_gb": 1.0,
    "hotkey": "right_cmd",
    "sample_rate": 16000,
    "clipboard_restore_delay": 0.25,
    "stream_interval_seconds": 1.2,
    "stream_debug": False,
    "known_words": [
        {"correct": "Dhimu", "sounds_like": ["demu", "dee moo", "the moo"]},
        {"correct": "Cat", "sounds_like": ["kat"]},
        {"correct": "Kathryn", "sounds_like": ["catherine", "katherine", "kathern"]},
        {"correct": "Bat", "sounds_like": ["bot"]},
        {"correct": "Hanhee", "sounds_like": ["honey", "han hee", "hanni", "hani", "hanhi"]},
        {"correct": "Haijia", "sounds_like": ["high jia", "hi jia", "haija", "hija", "hi ja", "high ja", "hajja", "hyja"]},
        {"correct": "ThuyVy", "sounds_like": ["thuy vy", "thuy vi", "thuyvi", "twee vee", "twee vy", "tweevy", "tui vy", "tui vi"]},
        {"correct": "Vilan", "sounds_like": ["vi lan", "vee lan", "vy lan", "vylan", "villan"]},
    ],
}

WHISPER_MODELS = {
    "medium": "mlx-community/whisper-medium-mlx",
    "distil-large-v3": "mlx-community/distil-whisper-large-v3",
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
}

HOTKEYS = [
    "right_cmd",
    "right_option",
    "right_control",
    "right_cmd+right_option",
    "right_cmd+right_control",
    "right_option+right_control",
    "f5",
]


class Config:
    def __init__(self):
        self._data = dict(DEFAULTS)
        self.load()

    def load(self):
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH) as f:
                    saved = json.load(f)
                for k, v in saved.items():
                    self._data[k] = v
            except (json.JSONDecodeError, OSError):
                pass
        if self._data.get("whisper_model_label") not in WHISPER_MODELS:
            self._data["whisper_model"] = DEFAULTS["whisper_model"]
            self._data["whisper_model_label"] = DEFAULTS["whisper_model_label"]
        return self

    def save(self):
        os.makedirs(PROJECT_ROOT, exist_ok=True)
        with open(CONFIG_PATH, "w") as f:
            json.dump(self._data, f, indent=2)

    def get(self, key):
        return self._data.get(key, DEFAULTS.get(key))

    def get_int(self, key) -> int:
        return int(self._data.get(key, DEFAULTS[key]))

    def get_float(self, key) -> float:
        return float(self._data.get(key, DEFAULTS[key]))

    def get_str(self, key) -> str:
        return str(self._data.get(key, DEFAULTS[key]))

    def set(self, key, value):
        self._data[key] = value
        self.save()

    def set_whisper_model(self, label):
        if label in WHISPER_MODELS:
            self._data["whisper_model"] = WHISPER_MODELS[label]
            self._data["whisper_model_label"] = label
            self.save()

    def get_known_words(self):
        """Return DEFAULTS and config.json vocabulary merged by spelling, with
        sounds_like variants unioned, so neither list silently hides the other."""
        saved = self._data.get("known_words", [])
        if not isinstance(saved, list):
            saved = []
        merged = {}
        for entry in [*DEFAULTS["known_words"], *saved]:
            if not isinstance(entry, dict):
                continue
            correct = str(entry.get("correct", "")).strip()
            if not correct:
                continue
            sounds = entry.get("sounds_like", [])
            if not isinstance(sounds, list):
                sounds = []
            slot = merged.setdefault(correct.lower(), {"correct": correct, "sounds_like": []})
            slot["correct"] = correct
            for s in sounds:
                s = str(s).strip()
                if s and s.lower() not in (v.lower() for v in slot["sounds_like"]):
                    slot["sounds_like"].append(s)
        return list(merged.values())
