# Anki Card Updater

macOS app for updating Anki cards with OpenRouter-generated answers through AnkiConnect.

## Build and run

```bash
./script/build_and_run.sh
```

The finished app bundle is created at:

```text
dist/Anki Card Updater.app
```

Settings and logs are stored in:

```text
~/Library/Application Support/Anki Card Updater/
```

## Requirements

- macOS with Python 3
- Anki running locally
- AnkiConnect enabled on `http://localhost:8765`
- OpenRouter API key
