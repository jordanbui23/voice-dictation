"""py2app build config for VoiceDictation.

Built in ALIAS mode: produces a real VoiceDictation.app bundle (its own identity, so
macOS attaches Microphone / Accessibility / Input Monitoring permissions to IT rather
than to the launching terminal), while symlinking back to this venv's site-packages.
Alias mode avoids freezing mlx (whose compiled core + mlx.metallib py2app's dependency
scanner would otherwise miss). The .app therefore depends on this project's venv staying
in place — fine for a personal tool.

Build:  source venv/bin/activate && python setup.py py2app -A
Result: dist/VoiceDictation.app
"""
from setuptools import setup

APP = ["src/app.py"]

OPTIONS = {
    "argv_emulation": False,
    "plist": {
        "CFBundleName": "VoiceDictation",
        "CFBundleDisplayName": "VoiceDictation",
        "CFBundleIdentifier": "com.jbui.voicedictation",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        # LSUIElement = menu bar agent, no Dock icon / app switcher entry.
        "LSUIElement": True,
        "NSMicrophoneUsageDescription": (
            "VoiceDictation records audio locally to transcribe your speech. "
            "Audio never leaves this Mac."
        ),
        "NSAppleEventsUsageDescription": (
            "VoiceDictation pastes transcribed text into the focused app."
        ),
    },
}

setup(
    app=APP,
    name="VoiceDictation",
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
