import requests
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import os
import configparser
import logging
from pathlib import Path
from typing import Callable
import time

# Logging-Konfiguration: Protokollierung in Datei "anki_updater.log"
logging.basicConfig(
    filename="anki_updater.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

# Globaler Schalter für detailliertes Logging
DETAILED_LOGGING = False

# -----------------------------
# Funktion zum Entfernen der Cloze-Deletions
# -----------------------------
def remove_cloze(text: str) -> str:
    print("Original:", text)
    cleaned_text = re.sub(r'\{\{\s*[cC]\d+\s*::.*?\}\}', '', text, flags=re.DOTALL)
    print("Bereinigt:", cleaned_text)
    cleaned_text = re.sub(r'\s+', ' ', cleaned_text).strip()
    return cleaned_text


# -----------------------------
# Konfigurationsverwaltung
# -----------------------------
class ConfigManager:
    CONFIG_FILE = "config.ini"
    DEFAULT_CONFIG = {
        'OpenRouter_API_Key': '',
        'Default_Prompt': 'Generiere eine prägnante und hilfreiche Antwort auf folgende Frage:\n\n{frage}',
        'Favorite_Model': '',
        'Favorite_Deck': '',
        'Target_Field': 'Extra',
        'Question_Field': 'Text',
        'Fill_Mode': 'Überspringen',
        # Erweiterte OpenRouter-Parameter:
        'max_tokens': '150',
        'temperature': '0.7',
        'top_p': '1.0',
        # Option, ob Cloze-Deletions entfernt werden sollen:
        'Remove_Cloze': 'True'
    }

    def __init__(self) -> None:
        self.config = configparser.ConfigParser()
        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE)
        else:
            self.config['DEFAULT'] = self.DEFAULT_CONFIG
            self.save_config()

    def save_config(self) -> None:
        with open(self.CONFIG_FILE, 'w') as configfile:
            self.config.write(configfile)

    def get(self, key: str, fallback: str = "") -> str:
        return self.config['DEFAULT'].get(key, fallback)

    def set(self, key: str, value: str) -> None:
        self.config['DEFAULT'][key] = value

# -----------------------------
# AnkiConnect-Funktionen
# -----------------------------
ANKI_CONNECT_URL = 'http://localhost:8765'

def anki_invoke(action: str, params: dict = {}) -> dict:
    """Führt einen API-Aufruf an AnkiConnect durch."""
    try:
        response = requests.post(ANKI_CONNECT_URL, json={
            'action': action,
            'version': 6,
            'params': params
        })
        return response.json()
    except Exception as e:
        logging.exception("Fehler in anki_invoke")
        return {"error": str(e)}

def get_deck_names() -> list:
    """Fragt über AnkiConnect die Namen aller Decks ab."""
    result = anki_invoke("deckNames")
    if "error" in result and result["error"] is not None:
        logging.error("Fehler beim Abrufen der Deck-Namen: " + str(result.get("error", "Unbekannter Fehler")))
        return []
    return result.get("result", [])

# -----------------------------
# OpenRouter-Funktionen
# -----------------------------
OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"

def fetch_models(api_key: str) -> list:
    """Holt die verfügbaren Modelle von OpenRouter und gibt eine Liste zurück."""
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    try:
        response = requests.get("https://openrouter.ai/api/v1/models", headers=headers)
        logging.debug(f"Modelle-Abruf: Status {response.status_code}, Response: {response.text}")
        response.raise_for_status()
        models_response = response.json()
        model_ids = [model["id"] for model in models_response.get("data", []) if "id" in model]
        if not model_ids:
            model_ids = ["Kein Modell gefunden"]
        return model_ids
    except Exception as e:
        logging.exception("Fehler beim Abrufen der Modelle")
        return []

def get_openrouter_response(api_key: str, model: str, frage: str, prompt: str, log_callback: Callable[[str], None],
                             max_tokens: int, temperature: float, top_p: float, remove_cloze_flag: bool = True) -> str:
    """
    Baut den finalen Prompt (ersetzt {frage}) und sendet ihn an den Chat-Completions-Endpunkt von OpenRouter.
    Es wird erwartet, dass die Antwort im JSON unter choices -> message -> content steht.
    """
    # Entfernen der Cloze-Deletions nur, wenn die Option aktiviert ist.
    if remove_cloze_flag:
        cleaned_frage = remove_cloze(frage)
    else:
        cleaned_frage = frage
    final_prompt = prompt.replace("{frage}", cleaned_frage)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": model,
        "messages": [
            {"role": "user", "content": final_prompt}
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "n": 1
    }
    # Detailliertes Logging der Anfrage, falls aktiviert
    if DETAILED_LOGGING:
        log_callback(f"OpenRouter Request Daten: {data}")
    try:
        log_callback(f"Frage an OpenRouter (Modell: {model}) senden ...")
        response = requests.post(OPENROUTER_CHAT_COMPLETIONS_URL, headers=headers, json=data)
        if response.status_code == 200:
            result = response.json()
            answer = result["choices"][0]["message"]["content"].strip()
            return answer
        else:
            log_callback(f"Fehler bei OpenRouter API-Anfrage: {response.status_code} - {response.text}")
            return ""
    except Exception as e:
        log_callback(f"Exception bei OpenRouter Anfrage: {str(e)}")
        logging.exception("Fehler in get_openrouter_response")
        return ""

# -----------------------------
# Batch-Prozess: Notizen aktualisieren
# -----------------------------
class AnkiUpdater:
    """
    Durchläuft alle Notizen eines angegebenen Decks und ergänzt ein Ziel-Feld
    mithilfe einer AI-generierten Antwort, basierend auf dem Inhalt des Frage-Feldes.
    """
    def __init__(self, log_callback: Callable[[str], None]) -> None:
        self.log_callback = log_callback

    def process_notes(self, deck_name: str, api_key: str, model: str, prompt: str,
                      target_field: str, question_field: str, fill_mode: str,
                      progress_callback: Callable[[int, int], None] = None,
                      cancel_check: Callable[[], bool] = lambda: False,
                      max_tokens: int = 150, temperature: float = 0.7, top_p: float = 1.0,
                      remove_cloze_flag: bool = True) -> None:
        self.log_callback("Suche Notizen im Deck ...")
        query = f'deck:"{deck_name}"'
        result = anki_invoke("findNotes", {"query": query})
        if result.get("error"):
            self.log_callback("Fehler bei AnkiConnect: " + str(result.get("error", "Unbekannter Fehler")))
            return

        note_ids = result.get("result", [])
        if not note_ids:
            self.log_callback("Keine Notizen gefunden.")
            return

        total_notes = len(note_ids)
        self.log_callback(f"{total_notes} Notizen gefunden. Starte Verarbeitung ...")
        notes_info = anki_invoke("notesInfo", {"notes": note_ids}).get("result", [])
        processed = 0
        for note in notes_info:
            if cancel_check():
                self.log_callback("Prozess abgebrochen.")
                break

            note_id = note.get("noteId")
            frage = note.get("fields", {}).get(question_field, {}).get("value", "")
            current_value = note.get("fields", {}).get(target_field, {}).get("value", "")
            if not frage:
                self.log_callback(f"Notiz {note_id}: Kein Text im Frage-Feld '{question_field}' gefunden – überspringe.")
                processed += 1
                if progress_callback:
                    progress_callback(processed, total_notes)
                continue

            if current_value.strip():
                if fill_mode == "Überspringen":
                    self.log_callback(f"Notiz {note_id}: Feld '{target_field}' bereits befüllt – überspringe.")
                    processed += 1
                    if progress_callback:
                        progress_callback(processed, total_notes)
                    continue
                elif fill_mode == "Anhängen":
                    # Antwort wird an den bestehenden Inhalt angehängt
                    pass
                elif fill_mode == "Überschreiben":
                    # Der alte Inhalt wird ersetzt
                    pass
                else:
                    self.log_callback(f"Notiz {note_id}: Unbekannter Fill Mode '{fill_mode}' – überspringe.")
                    processed += 1
                    if progress_callback:
                        progress_callback(processed, total_notes)
                    continue

            answer = get_openrouter_response(api_key, model, frage, prompt, self.log_callback,
                                               max_tokens, temperature, top_p, remove_cloze_flag)
            if answer:
                if current_value.strip() and fill_mode == "Anhängen":
                    new_value = current_value.strip() + "\n" + answer
                else:
                    new_value = answer

                update_result = anki_invoke("updateNoteFields", {"note": {"id": note_id, "fields": {target_field: new_value}}})
                if update_result.get("error"):
                    self.log_callback(f"Notiz {note_id}: Fehler beim Aktualisieren: {str(update_result.get('error'))}")
                else:
                    self.log_callback(f"Notiz {note_id} wurde aktualisiert.")
            else:
                self.log_callback(f"Notiz {note_id}: Keine Antwort erhalten.")

            processed += 1
            if progress_callback:
                progress_callback(processed, total_notes)

        self.log_callback("Verarbeitung abgeschlossen.")

# -----------------------------
# GUI-Anwendung (modernes, praktisches Design)
# -----------------------------
class Application(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Anki Updater")
        self.geometry("770x1090")
        self.configure(bg="#EFEFEF")
        self.minsize(800, 600)
        self.config_manager = ConfigManager()
        self.anki_updater = AnkiUpdater(self.log)
        self.all_models = []  # Modelle von OpenRouter
        self.all_decks = []   # Deck-Namen von AnkiConnect
        self.cancelled = False

        # Erweiterte OpenRouter-Parameter aus der Config laden
        self.max_tokens = int(self.config_manager.get('max_tokens', '150'))
        self.temperature = float(self.config_manager.get('temperature', '0.7'))
        self.top_p = float(self.config_manager.get('top_p', '1.0'))

        self.create_style()
        self.create_widgets()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        # Modelle und Decks asynchron laden
        threading.Thread(target=self.fetch_models, daemon=True).start()
        threading.Thread(target=self.fetch_decks, daemon=True).start()

    def create_style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        base_font = ("Segoe UI", 11)
        style.configure(".", background="#EFEFEF", foreground="#222222", font=base_font)
        style.configure("TLabel", background="#EFEFEF", font=base_font)
        style.configure("TButton", font=base_font, padding=5)
        style.configure("TEntry", font=base_font)
        style.configure("TCombobox", font=base_font)
        style.configure("TLabelframe", background="#EFEFEF", font=("Segoe UI", 12, "bold"))
        style.configure("TLabelframe.Label", background="#EFEFEF")
        style.configure("blue.Horizontal.TProgressbar", foreground="blue", background="blue", thickness=20)
        style.configure("green.Horizontal.TProgressbar", foreground="green", background="green", thickness=20)

    def create_widgets(self) -> None:
        main_frame = ttk.Frame(self, padding=15)
        main_frame.pack(fill=tk.BOTH, expand=True)
        main_frame.columnconfigure(0, weight=1)
        main_frame.rowconfigure(0, weight=2)
        main_frame.rowconfigure(1, weight=1)

        # --- Einstellungen ---
        settings_frame = ttk.Labelframe(main_frame, text="Einstellungen", padding=(15, 10))
        settings_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        settings_frame.columnconfigure(1, weight=1)

        # OpenRouter API-Key mit Schlüssel-Symbol
        ttk.Label(settings_frame, text="OpenRouter API-Key:").grid(row=0, column=0, sticky="w", padx=5, pady=5)
        api_frame = ttk.Frame(settings_frame)
        api_frame.grid(row=0, column=1, sticky="ew", padx=5, pady=5)
        self.api_key_entry = ttk.Entry(api_frame, width=40, show="*")
        self.api_key_entry.insert(0, self.config_manager.get('OpenRouter_API_Key', ''))
        self.api_key_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Label(api_frame, text="🔒").pack(side=tk.LEFT, padx=5)

        # Deck-Auswahl
        ttk.Label(settings_frame, text="Deck auswählen:").grid(row=1, column=0, sticky="w", padx=5, pady=5)
        self.deck_combobox = ttk.Combobox(settings_frame, state="readonly", width=38)
        self.deck_combobox.grid(row=1, column=1, sticky="ew", padx=5, pady=5)
        self.deck_combobox['values'] = ["Lade Decks..."]
        self.deck_combobox.current(0)

        # Deck-Suche
        ttk.Label(settings_frame, text="Deck suchen:").grid(row=2, column=0, sticky="w", padx=5, pady=5)
        self.deck_search_entry = ttk.Entry(settings_frame, width=38)
        self.deck_search_entry.grid(row=2, column=1, sticky="ew", padx=5, pady=5)
        self.deck_search_entry.bind("<KeyRelease>", self.apply_deck_filter)

        # Prompt
        ttk.Label(settings_frame, text="Prompt (mit {frage} als Platzhalter):").grid(row=3, column=0, sticky="nw", padx=5, pady=5)
        self.prompt_text = tk.Text(settings_frame, width=50, height=6, font=("Segoe UI", 11))
        self.prompt_text.insert("1.0", self.config_manager.get('Default_Prompt', ''))
        self.prompt_text.grid(row=3, column=1, sticky="ew", padx=5, pady=5)

        # Frage-Feld
        ttk.Label(settings_frame, text="Frage-Feld:").grid(row=4, column=0, sticky="w", padx=5, pady=5)
        self.question_field_entry = ttk.Entry(settings_frame, width=20)
        self.question_field_entry.insert(0, self.config_manager.get('Question_Field', 'Text'))
        self.question_field_entry.grid(row=4, column=1, sticky="w", padx=5, pady=5)

        # Ziel-Feld
        ttk.Label(settings_frame, text="Ziel-Feld:").grid(row=5, column=0, sticky="w", padx=5, pady=5)
        self.target_field_entry = ttk.Entry(settings_frame, width=20)
        self.target_field_entry.insert(0, self.config_manager.get('Target_Field', 'Extra'))
        self.target_field_entry.grid(row=5, column=1, sticky="w", padx=5, pady=5)

        # Fill-Mode
        ttk.Label(settings_frame, text="Wenn Ziel-Feld gefüllt:").grid(row=6, column=0, sticky="w", padx=5, pady=5)
        self.fill_mode_combobox = ttk.Combobox(settings_frame, state="readonly", width=18)
        self.fill_mode_combobox['values'] = ["Überspringen", "Überschreiben", "Anhängen"]
        current_mode = self.config_manager.get('Fill_Mode', 'Überspringen')
        self.fill_mode_combobox.set(current_mode)
        self.fill_mode_combobox.grid(row=6, column=1, sticky="w", padx=5, pady=5)

        # Option: Cloze-Deletions entfernen
        ttk.Label(settings_frame, text="Cloze-Deletions entfernen:").grid(row=7, column=0, sticky="w", padx=5, pady=5)
        self.cloze_removal_var = tk.BooleanVar(value=self.config_manager.get('Remove_Cloze', 'True') == 'True')
        cloze_cb = ttk.Checkbutton(settings_frame, variable=self.cloze_removal_var)
        cloze_cb.grid(row=7, column=1, sticky="w", padx=5, pady=5)

        # OpenRouter Modell-Auswahl
        ttk.Label(settings_frame, text="OpenRouter Modell:").grid(row=8, column=0, sticky="w", padx=5, pady=5)
        self.model_combobox = ttk.Combobox(settings_frame, state="readonly", width=38)
        self.model_combobox.grid(row=8, column=1, sticky="ew", padx=5, pady=5)
        self.model_combobox['values'] = ["Lade Modelle..."]
        self.model_combobox.current(0)

        # Modell-Suche
        ttk.Label(settings_frame, text="Modell suchen:").grid(row=9, column=0, sticky="w", padx=5, pady=(5,0))
        self.model_search_entry = ttk.Entry(settings_frame, width=38)
        self.model_search_entry.grid(row=9, column=1, sticky="ew", padx=5, pady=(5,5))
        self.model_search_entry.bind("<KeyRelease>", self.apply_model_filter)

        # Button für erweiterte OpenRouter-Einstellungen
        btn_advanced = ttk.Button(settings_frame, text="Erweiterte Einstellungen", command=self.open_advanced_settings)
        btn_advanced.grid(row=10, column=1, sticky="w", padx=5, pady=5)

        # Detailliertes Logging aktivieren/deaktivieren
        self.detailed_logging = tk.BooleanVar(value=False)
        detailed_log_cb = ttk.Checkbutton(settings_frame, text="Detailliertes Logging", variable=self.detailed_logging, command=self.toggle_detailed_logging)
        detailed_log_cb.grid(row=11, column=0, columnspan=2, sticky="w", padx=5, pady=5)

        # Buttons: Modelle aktualisieren und Favorit setzen
        btn_modell_frame = ttk.Frame(settings_frame)
        btn_modell_frame.grid(row=12, column=1, sticky="w", padx=5, pady=5)
        ttk.Button(btn_modell_frame, text="Modelle aktualisieren",
                   command=lambda: threading.Thread(target=self.fetch_models, daemon=True).start()
                   ).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_modell_frame, text="Favorit festlegen", command=self.set_favorite_model).pack(side=tk.LEFT, padx=(5,0))

        # Ladebalken (Progressbar)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(settings_frame, variable=self.progress_var, maximum=100,
                                            style="blue.Horizontal.TProgressbar")
        self.progress_bar.grid(row=13, column=0, columnspan=2, sticky="ew", padx=5, pady=(10,5))

        # Start- und Abbrechen-Buttons
        btn_proc_frame = ttk.Frame(settings_frame)
        btn_proc_frame.grid(row=14, column=0, columnspan=2, sticky="e", padx=5, pady=(15,5))
        self.start_btn = ttk.Button(btn_proc_frame, text="Start", command=self.start_processing)
        self.start_btn.pack(side=tk.LEFT, padx=5)
        self.cancel_btn = ttk.Button(btn_proc_frame, text="Abbrechen", command=self.cancel_processing, state=tk.DISABLED)
        self.cancel_btn.pack(side=tk.LEFT, padx=5)

        # --- Log-Ausgabe ---
        log_frame = ttk.Labelframe(main_frame, text="Log", padding=(15, 10))
        log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=10)
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_area = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Segoe UI", 11), state="disabled")
        self.log_area.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)

    def toggle_detailed_logging(self) -> None:
        global DETAILED_LOGGING
        DETAILED_LOGGING = self.detailed_logging.get()
        if DETAILED_LOGGING:
            self.log("Detailliertes Logging aktiviert.")
        else:
            self.log("Detailliertes Logging deaktiviert.")

    def open_advanced_settings(self) -> None:
        """Öffnet ein neues Fenster zur Einstellung der erweiterten OpenRouter-Parameter."""
        adv_window = tk.Toplevel(self)
        adv_window.title("Erweiterte OpenRouter-Einstellungen")
        adv_window.geometry("400x250")
        adv_window.transient(self)
        adv_window.grab_set()

        # max_tokens: Eingabefeld
        ttk.Label(adv_window, text="max_tokens:").pack(padx=10, pady=(15, 5), anchor="w")
        max_tokens_var = tk.StringVar(value=str(self.max_tokens))
        max_tokens_entry = ttk.Entry(adv_window, textvariable=max_tokens_var)
        max_tokens_entry.pack(padx=10, pady=5, fill=tk.X)

        # temperature: tk.Scale mit Schrittweite 0.1 (von 0.0 bis 1.0)
        ttk.Label(adv_window, text="temperature:").pack(padx=10, pady=(15, 5), anchor="w")
        temperature_var = tk.DoubleVar(value=self.temperature)
        temperature_scale = tk.Scale(adv_window, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                                     resolution=0.1, variable=temperature_var)
        temperature_scale.pack(padx=10, pady=5, fill=tk.X)
        ttk.Label(adv_window, textvariable=temperature_var).pack(padx=10, pady=(0,5), anchor="e")

        # top_p: tk.Scale mit Schrittweite 0.1 (von 0.0 bis 1.0)
        ttk.Label(adv_window, text="top_p:").pack(padx=10, pady=(15, 5), anchor="w")
        top_p_var = tk.DoubleVar(value=self.top_p)
        top_p_scale = tk.Scale(adv_window, from_=0.0, to=1.0, orient=tk.HORIZONTAL,
                               resolution=0.1, variable=top_p_var)
        top_p_scale.pack(padx=10, pady=5, fill=tk.X)
        ttk.Label(adv_window, textvariable=top_p_var).pack(padx=10, pady=(0,5), anchor="e")

        def save_advanced_settings():
            try:
                self.max_tokens = int(max_tokens_var.get())
            except ValueError:
                messagebox.showerror("Fehler", "Bitte eine gültige Zahl für max_tokens eingeben.")
                return
            self.temperature = float(temperature_var.get())
            self.top_p = float(top_p_var.get())
            # In der Konfiguration speichern:
            self.config_manager.set('max_tokens', str(self.max_tokens))
            self.config_manager.set('temperature', str(self.temperature))
            self.config_manager.set('top_p', str(self.top_p))
            self.config_manager.save_config()
            self.log("Erweiterte Einstellungen gespeichert.")
            adv_window.destroy()

        ttk.Button(adv_window, text="Speichern", command=save_advanced_settings).pack(padx=10, pady=15)

    def log(self, message: str) -> None:
        self.log_area.configure(state="normal")
        self.log_area.insert(tk.END, message + "\n")
        self.log_area.see(tk.END)
        self.log_area.configure(state="disabled")
        self.update_idletasks()
        logging.info(message)

    def fetch_models(self) -> None:
        api_key = self.api_key_entry.get().strip()
        if not api_key:
            self.log("Bitte zuerst den API-Key eingeben.")
            return
        self.log("Aktualisiere Modelle von OpenRouter ...")
        models = fetch_models(api_key)
        self.all_models = models
        self.after(0, self.apply_model_filter)

    def apply_model_filter(self, event=None) -> None:
        filter_text = self.model_search_entry.get().lower()
        if filter_text:
            filtered = [m for m in self.all_models if filter_text in m.lower()]
        else:
            filtered = self.all_models
        self.model_combobox['values'] = filtered
        favorite = self.config_manager.get('Favorite_Model', '')
        if favorite in filtered:
            self.model_combobox.set(favorite)
        elif filtered:
            self.model_combobox.current(0)

    def set_favorite_model(self) -> None:
        selected_model = self.model_combobox.get()
        if selected_model:
            self.config_manager.set('Favorite_Model', selected_model)
            self.config_manager.save_config()
            self.log(f"Favorit gesetzt: {selected_model}")
        else:
            self.log("Kein Modell ausgewählt.")

    def fetch_decks(self) -> None:
        self.log("Hole Deck-Namen von AnkiConnect ...")
        decks = get_deck_names()
        self.all_decks = decks
        self.after(0, self.update_deck_combobox)

    def update_deck_combobox(self) -> None:
        self.deck_combobox['values'] = self.all_decks
        favorite = self.config_manager.get('Favorite_Deck', '')
        if favorite in self.all_decks:
            self.deck_combobox.set(favorite)
        elif self.all_decks:
            self.deck_combobox.current(0)

    def apply_deck_filter(self, event=None) -> None:
        filter_text = self.deck_search_entry.get().lower()
        words = filter_text.split()
        if words:
            filtered = [d for d in self.all_decks if all(word in d.lower() for word in words)]
        else:
            filtered = self.all_decks
        self.deck_combobox['values'] = filtered
        favorite = self.config_manager.get('Favorite_Deck', '')
        if favorite in filtered:
            self.deck_combobox.set(favorite)
        elif filtered:
            self.deck_combobox.current(0)

    def start_processing(self) -> None:
        if not self.validate_inputs():
            return

        self.start_btn.config(state=tk.DISABLED)
        self.cancel_btn.config(state=tk.NORMAL)
        self.cancelled = False
        self.progress_var.set(0)
        self.progress_bar.config(style="blue.Horizontal.TProgressbar")

        self.config_manager.set('OpenRouter_API_Key', self.api_key_entry.get().strip())
        self.config_manager.set('Default_Prompt', self.prompt_text.get("1.0", tk.END).strip())
        self.config_manager.set('Favorite_Deck', self.deck_combobox.get().strip())
        self.config_manager.set('Favorite_Model', self.model_combobox.get())
        self.config_manager.set('Target_Field', self.target_field_entry.get().strip())
        self.config_manager.set('Question_Field', self.question_field_entry.get().strip())
        self.config_manager.set('Fill_Mode', self.fill_mode_combobox.get())
        self.config_manager.save_config()

        threading.Thread(target=self.run_processing, daemon=True).start()

    def run_processing(self) -> None:
        deck_name = self.deck_combobox.get().strip()
        api_key = self.api_key_entry.get().strip()
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        model = self.model_combobox.get().strip()
        target_field = self.target_field_entry.get().strip()
        question_field = self.question_field_entry.get().strip()
        fill_mode = self.fill_mode_combobox.get().strip()
        remove_cloze_flag = self.cloze_removal_var.get()

        self.log("Starte Batch-Prozess ...")

        def progress_update(processed: int, total: int) -> None:
            progress = (processed / total) * 100
            self.after(0, lambda: self.progress_var.set(progress))
            if processed == total:
                self.after(0, lambda: self.progress_bar.config(style="green.Horizontal.TProgressbar"))
            self.log(f"Fortschritt: {processed} von {total} Notizen verarbeitet.")

        # Übergabe der erweiterten Parameter an den Batch-Prozess
        self.anki_updater.process_notes(
            deck_name, api_key, model, prompt, target_field, question_field, fill_mode,
            progress_callback=progress_update,
            cancel_check=lambda: self.cancelled,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            remove_cloze_flag=remove_cloze_flag
        )
        self.log("Batch-Prozess abgeschlossen.")
        self.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
        self.after(0, lambda: self.cancel_btn.config(state=tk.DISABLED))

    def cancel_processing(self) -> None:
        self.cancelled = True
        self.log("Abbruch angefordert...")
        self.start_btn.config(state=tk.NORMAL)
        self.cancel_btn.config(state=tk.DISABLED)

    def validate_inputs(self) -> bool:
        if not self.deck_combobox.get().strip():
            self.log("Bitte ein Deck auswählen.")
            return False
        if not self.api_key_entry.get().strip():
            self.log("Bitte den OpenRouter API-Key eingeben.")
            return False
        if not self.prompt_text.get("1.0", tk.END).strip():
            self.log("Bitte einen Prompt eingeben.")
            return False
        if not self.model_combobox.get().strip():
            self.log("Bitte ein Modell auswählen.")
            return False
        if not self.target_field_entry.get().strip():
            self.log("Bitte den Namen des Ziel-Felds eingeben.")
            return False
        if not self.question_field_entry.get().strip():
            self.log("Bitte den Namen des Frage-Felds eingeben.")
            return False
        return True

    def on_close(self) -> None:
        self.config_manager.set('Default_Prompt', self.prompt_text.get("1.0", tk.END).strip())
        self.config_manager.set('Favorite_Deck', self.deck_combobox.get().strip())
        self.config_manager.set('OpenRouter_API_Key', self.api_key_entry.get().strip())
        self.config_manager.set('Favorite_Model', self.model_combobox.get().strip())
        self.config_manager.set('Target_Field', self.target_field_entry.get().strip())
        self.config_manager.set('Question_Field', self.question_field_entry.get().strip())
        self.config_manager.set('Fill_Mode', self.fill_mode_combobox.get().strip())
        # Erweiterte Parameter auch speichern
        self.config_manager.set('max_tokens', str(self.max_tokens))
        self.config_manager.set('temperature', str(self.temperature))
        self.config_manager.set('top_p', str(self.top_p))
        self.config_manager.set('Remove_Cloze', str(self.cloze_removal_var.get()))
        self.config_manager.save_config()
        self.destroy()

if __name__ == "__main__":
    app = Application()
    app.mainloop()