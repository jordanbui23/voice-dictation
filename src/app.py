"""Voice-dictation menu bar app.

Two modes, auto-selected by the cleanup toggle:
- Cleanup ON  (batch):  hold -> record -> transcribe -> Bedrock cleanup ->
                        paste at cursor (clipboard restored). Polished, one paste.
- Cleanup OFF (stream): hold -> live sliding-window transcription types words at
                        the cursor as you speak (keystrokes, clipboard untouched),
                        fully offline. Release flushes the final words.

The CGEventTap callback must never block, so hotkey press/release only flips
state and hands heavy work (transcribe/cleanup/paste, or the streaming loop) to a
worker thread.
"""
import json
import os
import sys
import threading
import time
from datetime import datetime

import rumps

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from audio import Recorder
from bedrock_cleanup import BedrockCleanup, is_auth_error
from config import Config, WHISPER_MODELS, HOTKEYS, LOG_DIR, STREAM_DEBUG_LOG
from hotkey import HotkeyListener
from known_words import apply_corrections, build_whisper_prompt
from overlay import Overlay
from paste import paste_text
from streaming import StreamingSession
from transcribe import Transcriber
from type_text import type_text

IDLE_ICON = "🎙️"

_DASH_MAP = str.maketrans({"\u2014": ", ", "\u2013": "-"})


def strip_dashes(text):
    """Replace em dashes with ', ' and en dashes with '-'. Collapse doubles."""
    out = text.translate(_DASH_MAP)
    while ",  " in out:
        out = out.replace(",  ", ", ")
    return out.replace(" , ", ", ")

HOTKEY_LABELS = {
    "right_cmd": "Right Command",
    "right_option": "Right Option",
    "right_control": "Right Control",
    "right_cmd+right_option": "Right Command + Right Option",
    "right_cmd+right_control": "Right Command + Right Control",
    "right_option+right_control": "Right Option + Right Control",
    "f5": "F5",
}


class DictationApp(rumps.App):
    def __init__(self):
        super().__init__("VoiceDictation", title=IDLE_ICON, quit_button=None)
        self.cfg = Config()
        self.known_words = self.cfg.get_known_words()
        self.recorder = Recorder(self.cfg.get_int("sample_rate"))
        self.transcriber = Transcriber(
            self.cfg.get("whisper_model"),
            initial_prompt=build_whisper_prompt(self.known_words),
        )
        self.cleanup_client = None
        self._recording = False
        self._streaming_session = None
        self._auth_alert_open = False
        self._worker_lock = threading.Lock()
        self.overlay = Overlay()
        os.makedirs(LOG_DIR, exist_ok=True)
        self.transcript_log = os.path.join(LOG_DIR, "transcripts.jsonl")

        self._build_menu()
        self._init_cleanup_client()

        self.listener = HotkeyListener(
            self.cfg.get("hotkey"), self._on_press, self._on_release
        )
        self.listener.start()

        threading.Thread(target=self._warm_up, daemon=True).start()

    # ---------- menu ----------

    def _build_menu(self):
        self.cleanup_item = rumps.MenuItem(
            "Cleanup (Bedrock)", callback=self._toggle_cleanup
        )
        self.cleanup_item.state = 1 if self.cfg.get("cleanup_enabled") else 0

        model_menu = rumps.MenuItem("Whisper Model")
        self._model_items = {}
        for label in WHISPER_MODELS:
            item = rumps.MenuItem(label, callback=self._set_model)
            item.state = 1 if label == self.cfg.get("whisper_model_label") else 0
            model_menu.add(item)
            self._model_items[label] = item

        hotkey_menu = rumps.MenuItem("Hotkey")
        self._hotkey_items = {}
        for hk in HOTKEYS:
            item = rumps.MenuItem(
                HOTKEY_LABELS.get(hk, hk), callback=self._set_hotkey
            )
            item._hotkey_value = hk
            item.state = 1 if hk == self.cfg.get("hotkey") else 0
            hotkey_menu.add(item)
            self._hotkey_items[hk] = item

        self.status_item = rumps.MenuItem("Ready", callback=None)

        self.menu = [
            self.status_item,
            None,
            self.cleanup_item,
            model_menu,
            hotkey_menu,
            None,
            rumps.MenuItem("Open Log Folder", callback=self._open_logs),
            rumps.MenuItem("Quit", callback=self._quit),
        ]

    def _init_cleanup_client(self):
        try:
            self.cleanup_client = BedrockCleanup(
                self.cfg.get("aws_profile"),
                self.cfg.get("aws_region"),
                self.cfg.get("bedrock_model_id"),
                self.cfg.get("cleanup_timeout_seconds"),
                self.cfg.get("cleanup_max_tokens"),
                known_words=self.known_words,
            )
        except Exception as e:
            self.cleanup_client = None
            self._log_event("bedrock_init_failed", error=str(e))

    def _warm_up(self):
        try:
            self.status_item.title = "Loading Whisper model…"
            self.transcriber.warm_up()
            self.status_item.title = "Ready"
        except Exception as e:
            self.status_item.title = f"Model load failed: {e}"

    # ---------- callbacks ----------

    def _toggle_cleanup(self, sender):
        sender.state = 0 if sender.state else 1
        self.cfg.set("cleanup_enabled", bool(sender.state))

    def _set_model(self, sender):
        for label, item in self._model_items.items():
            item.state = 1 if item is sender else 0
        self.cfg.set_whisper_model(sender.title)
        threading.Thread(
            target=self._load_model, args=(sender.title,), daemon=True
        ).start()

    def _load_model(self, label):
        cached = self._model_is_cached(label)
        self.status_item.title = (
            f"Loading {label}…" if cached else f"Downloading {label} model…"
        )
        try:
            new_transcriber = Transcriber(
                self.cfg.get("whisper_model"),
                initial_prompt=build_whisper_prompt(self.known_words),
            )
            new_transcriber.warm_up()
            self.transcriber = new_transcriber
            self.status_item.title = "Ready"
        except Exception as e:
            self.status_item.title = f"Model load failed: {e}"

    @staticmethod
    def _model_is_cached(label):
        repo = WHISPER_MODELS[label].split("/", 1)[-1]
        cache = os.path.expanduser(
            f"~/.cache/huggingface/hub/models--mlx-community--{repo}"
        )
        return os.path.isdir(cache)

    def _set_hotkey(self, sender):
        hk = getattr(sender, "_hotkey_value", None)
        if not hk:
            return
        self.cfg.set("hotkey", hk)
        for other, item in self._hotkey_items.items():
            item.state = 1 if item is sender else 0
        self.listener.stop()
        self.listener = HotkeyListener(hk, self._on_press, self._on_release)
        self.listener.start()
        rumps.notification(
            "Voice Dictation", "Hotkey changed",
            f"Now hold {HOTKEY_LABELS.get(hk, hk)} to dictate.",
        )

    def _open_logs(self, _):
        os.system(f'open "{LOG_DIR}"')

    def _alert_auth_expired(self):
        if self._auth_alert_open:
            return
        self._auth_alert_open = True
        try:
            rumps.alert(
                title="AWS credentials expired",
                message=(
                    "Bedrock cleanup needs valid AWS credentials.\n\n"
                    "Refresh your AWS credentials, then dictate again.\n\n"
                    "Your words were still pasted (raw, uncleaned)."
                ),
                ok="Got it",
            )
        finally:
            self._auth_alert_open = False

    def _quit(self, _):
        try:
            self.listener.stop()
        except Exception:
            pass
        rumps.quit_application()

    # ---------- hotkey (runs on tap thread — keep it trivial) ----------

    def _streaming_mode(self):
        return not self.cfg.get("cleanup_enabled")

    def _on_press(self):
        if self._recording:
            return
        if self._streaming_session is not None:
            self._streaming_session.request_stop()
            return
        self._recording = True
        self.overlay.show_state("listening")
        try:
            if self._streaming_mode():
                dbg = self._stream_debug()
                base_emit = (
                    (lambda text: type_text(text, debug=dbg)) if dbg else type_text
                )
                kw = self.known_words

                def emit(text, _base=base_emit, _kw=kw):
                    _base(apply_corrections(text, _kw))

                self._streaming_session = StreamingSession(
                    self.recorder,
                    self.transcriber,
                    emit,
                    sample_rate=self.cfg.get_int("sample_rate"),
                    interval=self.cfg.get_float("stream_interval_seconds"),
                    debug=dbg,
                )
                self.recorder.start()
                self._streaming_session.start()
            else:
                self.recorder.start()
        except Exception as e:
            self._recording = False
            self._streaming_session = None
            self.overlay.hide()
            self._log_event("record_start_failed", error=str(e))

    def _stream_debug(self):
        """Return a debug logger writing to stream_debug.log, or None if disabled."""
        if not self.cfg.get("stream_debug"):
            return None

        def log(phase, **fields):
            rec = {"ts": datetime.now().isoformat(), "phase": phase}
            rec.update(fields)
            try:
                with open(STREAM_DEBUG_LOG, "a") as f:
                    f.write(json.dumps(rec) + "\n")
            except OSError:
                pass

        return log

    def _on_release(self):
        if not self._recording:
            return
        self._recording = False
        self.overlay.show_state("transcribing")
        session = self._streaming_session
        if session is not None:
            session.request_stop()
            threading.Thread(target=self._finish_stream, daemon=True).start()
        else:
            threading.Thread(target=self._process, daemon=True).start()

    # ---------- worker ----------

    def _finish_stream(self):
        session = self._streaming_session
        self._streaming_session = None
        if session is None:
            self.overlay.hide()
            return
        self._worker_lock.acquire()
        try:
            full_text = session.stop()
            self._log_event(
                "dictation",
                raw=full_text,
                final=full_text,
                cleanup=False,
                mode="stream",
                **session.diagnostics,
            )
        except Exception as e:
            self._log_event("stream_failed", error=str(e))
            rumps.notification("Voice Dictation", "Dictation failed", str(e)[:120])
        finally:
            self.overlay.done()
            self._worker_lock.release()

    def _process(self):
        if not self._worker_lock.acquire(blocking=False):
            self.overlay.hide()
            return
        auth_needed = False
        timed_out = False
        try:
            audio = self.recorder.stop()
            diag = self.recorder.diagnostics(audio)
            if audio.size < self.cfg.get_int("sample_rate") * 0.2:
                self._log_event("dictation_discarded", reason="too_short", **diag)
                self.overlay.hide()
                return

            try:
                raw = self.transcriber.transcribe_with_timeout(
                    audio, self.cfg.get_float("transcribe_timeout_seconds")
                )
            except TimeoutError as e:
                timed_out = True
                self._log_event("transcribe_timeout", error=str(e), **diag)
                rumps.notification(
                    "Voice Dictation",
                    "Transcription timed out",
                    "The clip was too long or Whisper stalled. Try again.",
                )
                return
            if not raw:
                self._log_event("dictation_discarded", reason="empty_transcript", **diag)
                self.overlay.hide()
                return

            final_text = raw
            used_cleanup = False
            cleanup_error = None
            auth_needed = False

            if self.cfg.get("cleanup_enabled") and self.cleanup_client is not None:
                t0 = time.time()
                try:
                    final_text = self.cleanup_client.clean(raw)
                    used_cleanup = True
                except Exception as e:
                    cleanup_error = str(e)
                    final_text = raw
                    if is_auth_error(e):
                        auth_needed = True
                        self._alert_auth_expired()
                    else:
                        rumps.notification(
                            "Voice Dictation",
                            "Cleanup unavailable, pasted raw transcript",
                            (str(e)[:120]),
                        )
                _ = time.time() - t0

            final_text = apply_corrections(final_text, self.known_words)
            final_text = strip_dashes(final_text)
            paste_text(final_text, self.cfg.get_float("clipboard_restore_delay"))
            self._log_event(
                "dictation",
                raw=raw,
                final=final_text,
                cleanup=used_cleanup,
                cleanup_error=cleanup_error,
                **diag,
            )
        except Exception as e:
            self._log_event("process_failed", error=str(e))
            rumps.notification("Voice Dictation", "Dictation failed", str(e)[:120])
        finally:
            if timed_out:
                self.overlay.error()
            elif auth_needed:
                self.overlay.auth_needed()
            else:
                self.overlay.done()
            self._worker_lock.release()

    # ---------- logging ----------

    def _log_event(self, kind, **fields):
        rec = {"ts": datetime.now().isoformat(), "event": kind}
        rec.update(fields)
        try:
            with open(self.transcript_log, "a") as f:
                f.write(json.dumps(rec) + "\n")
        except OSError:
            pass


if __name__ == "__main__":
    DictationApp().run()
