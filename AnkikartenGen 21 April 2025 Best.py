import re
import requests
import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import os
import configparser
import logging
from pathlib import Path
from typing import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

# Logging-Konfiguration
logging.basicConfig(
    filename="anki_updater.log",
    level=logging.DEBUG,
    format="%(asctime)s - %(levelname)s - %(message)s"
)

DETAILED_LOGGING = False

# ═══════════════════════════════════════════════════════════════════════════════
# LIQUID GLASS DESIGN SYSTEM - iOS 26 Style
# ═══════════════════════════════════════════════════════════════════════════════

class LiquidGlassColors:
    """Farbpalette für Liquid Glass Design"""
    # Basis-Hintergrund (dunkler Gradient-Effekt simuliert)
    BG_PRIMARY = "#1a1a2e"
    BG_SECONDARY = "#16213e"
    BG_TERTIARY = "#0f3460"
    
    # Glass-Effekte (halbtransparent simuliert durch helle Töne)
    GLASS_BG = "#ffffff"
    GLASS_OPACITY = 0.08  # Simuliert durch Mischfarben
    GLASS_SURFACE = "#2a2a4a"
    GLASS_BORDER = "#3a3a5a"
    GLASS_HIGHLIGHT = "#4a4a6a"
    
    # Akzentfarben
    ACCENT_BLUE = "#5e9eff"
    ACCENT_PURPLE = "#a78bfa"
    ACCENT_TEAL = "#5eead4"
    ACCENT_PINK = "#f472b6"
    ACCENT_GREEN = "#4ade80"
    ACCENT_ORANGE = "#fb923c"
    
    # Text
    TEXT_PRIMARY = "#ffffff"
    TEXT_SECONDARY = "#a0a0b0"
    TEXT_MUTED = "#606070"
    
    # Status
    SUCCESS = "#4ade80"
    WARNING = "#fbbf24"
    ERROR = "#f87171"
    INFO = "#60a5fa"


def remove_cloze(text: str) -> str:
    cleaned = re.sub(r'\{\{\s*[cC]\d+\s*::.*?\}\}', '', text, flags=re.DOTALL)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


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
        'max_tokens': '150',
        'temperature': '0.7',
        'top_p': '1.0',
        'Remove_Cloze': 'True',
        'Concurrency': '5'
    }

    def __init__(self) -> None:
        self.config = configparser.ConfigParser()
        if os.path.exists(self.CONFIG_FILE):
            self.config.read(self.CONFIG_FILE)
        else:
            self.config['DEFAULT'] = self.DEFAULT_CONFIG
            self.save_config()

    def save_config(self) -> None:
        with open(self.CONFIG_FILE, 'w') as cfg:
            self.config.write(cfg)

    def get(self, key: str, fallback: str = "") -> str:
        return self.config['DEFAULT'].get(key, fallback)

    def set(self, key: str, value: str) -> None:
        self.config['DEFAULT'][key] = value


ANKI_CONNECT_URL = 'http://localhost:8765'

def anki_invoke(action: str, params: dict = {}) -> dict:
    try:
        resp = requests.post(ANKI_CONNECT_URL, json={
            'action': action,
            'version': 6,
            'params': params
        })
        return resp.json()
    except Exception as e:
        logging.exception("Fehler in anki_invoke")
        return {"error": str(e)}


def get_deck_names() -> list:
    result = anki_invoke("deckNames")
    if result.get("error"):
        logging.error("Fehler beim Abrufen der Deck-Namen: %s", result["error"])
        return []
    return result.get("result", [])


OPENROUTER_CHAT_COMPLETIONS_URL = "https://openrouter.ai/api/v1/chat/completions"

def fetch_models(api_key: str) -> list:
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", headers=headers)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        return [m["id"] for m in data if "id" in m]
    except Exception:
        logging.exception("Fehler beim Abrufen der Modelle")
        return []


def get_openrouter_response(api_key: str, model: str, frage: str, prompt: str,
                            log_callback: Callable[[str], None],
                            max_tokens: int, temperature: float, top_p: float,
                            remove_cloze_flag: bool) -> str:
    if remove_cloze_flag:
        frage = remove_cloze(frage)
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt.replace("{frage}", frage)}],
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
        "n": 1
    }
    if DETAILED_LOGGING:
        log_callback(f"Request: {payload}")
    try:
        r = requests.post(OPENROUTER_CHAT_COMPLETIONS_URL,
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                          json=payload)
        if r.status_code == 200:
            return r.json()["choices"][0]["message"]["content"].strip()
        else:
            log_callback(f"OpenRouter Fehler: {r.status_code}")
            return ""
    except Exception as e:
        log_callback(f"Exception OpenRouter: {e}")
        logging.exception("Fehler in get_openrouter_response")
        return ""


class AnkiUpdater:
    def __init__(self, log_callback: Callable[[str], None]) -> None:
        self.log_callback = log_callback

    def _process_note(self, note, api_key, model, prompt, target_field,
                      question_field, fill_mode, max_tokens, temperature,
                      top_p, remove_cloze_flag):
        note_id = note["noteId"]
        frage = note["fields"].get(question_field, {}).get("value", "")
        current = note["fields"].get(target_field, {}).get("value", "")
        if not frage:
            return
        if current.strip() and fill_mode == "Überspringen":
            return
        answer = get_openrouter_response(
            api_key, model, frage, prompt, self.log_callback,
            max_tokens, temperature, top_p, remove_cloze_flag
        )
        if not answer:
            self.log_callback(f"Notiz {note_id}: keine Antwort")
            return
        new_value = answer if not current.strip() or fill_mode != "Anhängen" else current.strip() + "\n" + answer
        upd = anki_invoke("updateNoteFields", {
            "note": {"id": note_id, "fields": {target_field: new_value}}
        })
        if upd.get("error"):
            self.log_callback(f"Notiz {note_id}: Update-Fehler")
        else:
            self.log_callback(f"Notiz {note_id} aktualisiert.")

    def process_notes(self, deck_name: str, api_key: str, model: str, prompt: str,
                      target_field: str, question_field: str, fill_mode: str,
                      concurrency: int, progress_callback: Callable[[int, int], None],
                      cancel_check: Callable[[], bool],
                      max_tokens: int, temperature: float, top_p: float,
                      remove_cloze_flag: bool) -> None:
        self.log_callback("Suche Notizen …")
        find = anki_invoke("findNotes", {"query": f'deck:"{deck_name}"'})
        if find.get("error"):
            self.log_callback("AnkiConnect Fehler")
            return
        ids = find.get("result", [])
        if not ids:
            self.log_callback("Keine Notizen gefunden.")
            return
        notes = anki_invoke("notesInfo", {"notes": ids}).get("result", [])
        total = len(notes)
        self.log_callback(f"{total} Notizen gefunden, starte {concurrency} Threads …")
        processed = 0

        with ThreadPoolExecutor(max_workers=concurrency if concurrency>0 else 1) as pool:
            futures = [pool.submit(
                self._process_note, note, api_key, model, prompt,
                target_field, question_field, fill_mode,
                max_tokens, temperature, top_p, remove_cloze_flag
            ) for note in notes]
            for f in as_completed(futures):
                if cancel_check():
                    self.log_callback("Abgebrochen.")
                    break
                processed += 1
                progress_callback(processed, total)

        self.log_callback("Verarbeitung abgeschlossen.")


# ═══════════════════════════════════════════════════════════════════════════════
# LIQUID GLASS UI COMPONENTS
# ═══════════════════════════════════════════════════════════════════════════════

class GlassCard(tk.Frame):
    """Glassmorphism Card Component"""
    def __init__(self, parent, title="", **kwargs):
        super().__init__(parent, **kwargs)
        self.configure(bg=LiquidGlassColors.GLASS_SURFACE, highlightthickness=1,
                      highlightbackground=LiquidGlassColors.GLASS_BORDER)
        
        if title:
            title_label = tk.Label(self, text=title, font=("SF Pro Display", 13, "bold"),
                                  bg=LiquidGlassColors.GLASS_SURFACE, 
                                  fg=LiquidGlassColors.TEXT_PRIMARY)
            title_label.pack(anchor="w", padx=16, pady=(12, 8))
        
        self.content = tk.Frame(self, bg=LiquidGlassColors.GLASS_SURFACE)
        self.content.pack(fill="both", expand=True, padx=16, pady=(0, 12))


class GlassButton(tk.Canvas):
    """Moderner Glassmorphism Button"""
    def __init__(self, parent, text="", command=None, accent=False, width=120, height=40, **kwargs):
        super().__init__(parent, width=width, height=height, 
                        bg=LiquidGlassColors.GLASS_SURFACE, highlightthickness=0, **kwargs)
        
        self.command = command
        self.text = text
        self.accent = accent
        self.width = width
        self.height = height
        self.hover = False
        self.disabled = False
        
        self._draw()
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
    
    def _draw(self):
        self.delete("all")
        
        if self.disabled:
            bg = LiquidGlassColors.GLASS_BORDER
            fg = LiquidGlassColors.TEXT_MUTED
        elif self.accent:
            bg = LiquidGlassColors.ACCENT_BLUE if not self.hover else LiquidGlassColors.ACCENT_PURPLE
            fg = LiquidGlassColors.TEXT_PRIMARY
        else:
            bg = LiquidGlassColors.GLASS_HIGHLIGHT if self.hover else LiquidGlassColors.GLASS_BORDER
            fg = LiquidGlassColors.TEXT_PRIMARY
        
        # Rounded rectangle
        r = 12
        self.create_oval(0, 0, r*2, r*2, fill=bg, outline="")
        self.create_oval(self.width-r*2, 0, self.width, r*2, fill=bg, outline="")
        self.create_oval(0, self.height-r*2, r*2, self.height, fill=bg, outline="")
        self.create_oval(self.width-r*2, self.height-r*2, self.width, self.height, fill=bg, outline="")
        self.create_rectangle(r, 0, self.width-r, self.height, fill=bg, outline="")
        self.create_rectangle(0, r, self.width, self.height-r, fill=bg, outline="")
        
        self.create_text(self.width/2, self.height/2, text=self.text, 
                        font=("SF Pro Text", 12, "bold"), fill=fg)
    
    def _on_enter(self, e):
        if not self.disabled:
            self.hover = True
            self._draw()
    
    def _on_leave(self, e):
        self.hover = False
        self._draw()
    
    def _on_click(self, e):
        if self.command and not self.disabled:
            self.command()
    
    def set_disabled(self, disabled):
        self.disabled = disabled
        self._draw()


class GlassEntry(tk.Frame):
    """Modernes Eingabefeld mit Glass-Effekt"""
    def __init__(self, parent, placeholder="", show="", **kwargs):
        super().__init__(parent, bg=LiquidGlassColors.GLASS_SURFACE, **kwargs)
        
        self.inner = tk.Frame(self, bg=LiquidGlassColors.GLASS_BORDER, 
                             highlightthickness=1, highlightbackground=LiquidGlassColors.GLASS_HIGHLIGHT)
        self.inner.pack(fill="x", padx=2, pady=2)
        
        self.entry = tk.Entry(self.inner, font=("SF Pro Text", 12),
                             bg=LiquidGlassColors.GLASS_SURFACE, 
                             fg=LiquidGlassColors.TEXT_PRIMARY,
                             insertbackground=LiquidGlassColors.ACCENT_BLUE,
                             relief="flat", show=show)
        self.entry.pack(fill="x", padx=10, pady=8)
        
        self.placeholder = placeholder
        if placeholder:
            self.entry.insert(0, placeholder)
            self.entry.config(fg=LiquidGlassColors.TEXT_MUTED)
            self.entry.bind("<FocusIn>", self._on_focus_in)
            self.entry.bind("<FocusOut>", self._on_focus_out)
    
    def _on_focus_in(self, e):
        if self.entry.get() == self.placeholder:
            self.entry.delete(0, tk.END)
            self.entry.config(fg=LiquidGlassColors.TEXT_PRIMARY)
    
    def _on_focus_out(self, e):
        if not self.entry.get():
            self.entry.insert(0, self.placeholder)
            self.entry.config(fg=LiquidGlassColors.TEXT_MUTED)
    
    def get(self):
        val = self.entry.get()
        return "" if val == self.placeholder else val
    
    def set(self, value):
        self.entry.delete(0, tk.END)
        self.entry.insert(0, value)
        self.entry.config(fg=LiquidGlassColors.TEXT_PRIMARY)
    
    def insert(self, index, value):
        self.entry.delete(0, tk.END)
        self.entry.insert(0, value)
        self.entry.config(fg=LiquidGlassColors.TEXT_PRIMARY)


class GlassCombobox(tk.Frame):
    """Moderne Dropdown mit Glass-Effekt"""
    def __init__(self, parent, values=[], **kwargs):
        super().__init__(parent, bg=LiquidGlassColors.GLASS_SURFACE, **kwargs)
        
        self.var = tk.StringVar()
        self.values = values
        
        self.inner = tk.Frame(self, bg=LiquidGlassColors.GLASS_BORDER,
                             highlightthickness=1, highlightbackground=LiquidGlassColors.GLASS_HIGHLIGHT)
        self.inner.pack(fill="x", padx=2, pady=2)
        
        # Custom styled combobox
        style = ttk.Style()
        style.configure("Glass.TCombobox",
                       fieldbackground=LiquidGlassColors.GLASS_SURFACE,
                       background=LiquidGlassColors.GLASS_BORDER,
                       foreground=LiquidGlassColors.TEXT_PRIMARY)
        
        self.combo = ttk.Combobox(self.inner, textvariable=self.var, values=values,
                                  state="readonly", font=("SF Pro Text", 12))
        self.combo.pack(fill="x", padx=8, pady=6)
    
    def get(self):
        return self.var.get()
    
    def set(self, value):
        self.var.set(value)
    
    def configure(self, **kwargs):
        if 'values' in kwargs:
            self.combo['values'] = kwargs['values']
    
    def current(self, index):
        self.combo.current(index)


class GlassSlider(tk.Frame):
    """Moderner Slider mit Glass-Effekt"""
    def __init__(self, parent, from_=0, to=100, value=50, command=None, **kwargs):
        super().__init__(parent, bg=LiquidGlassColors.GLASS_SURFACE, **kwargs)
        
        self.value_var = tk.IntVar(value=value)
        self.command = command
        
        # Value display
        self.value_label = tk.Label(self, text=str(value), font=("SF Pro Display", 14, "bold"),
                                   bg=LiquidGlassColors.GLASS_SURFACE, 
                                   fg=LiquidGlassColors.ACCENT_BLUE, width=4)
        self.value_label.pack(side="right", padx=(10, 0))
        
        # Slider
        self.scale = tk.Scale(self, from_=from_, to=to, orient="horizontal",
                             variable=self.value_var, showvalue=False,
                             bg=LiquidGlassColors.GLASS_SURFACE,
                             fg=LiquidGlassColors.ACCENT_BLUE,
                             troughcolor=LiquidGlassColors.GLASS_BORDER,
                             activebackground=LiquidGlassColors.ACCENT_PURPLE,
                             highlightthickness=0, length=200,
                             command=self._on_change)
        self.scale.pack(side="left", fill="x", expand=True)
    
    def _on_change(self, val):
        self.value_label.config(text=str(int(float(val))))
        if self.command:
            self.command(val)
    
    def get(self):
        return self.value_var.get()
    
    def set(self, value):
        self.value_var.set(value)
        self.value_label.config(text=str(value))


class GlassToggle(tk.Canvas):
    """iOS-Style Toggle Switch"""
    def __init__(self, parent, value=False, command=None, **kwargs):
        super().__init__(parent, width=50, height=28, 
                        bg=LiquidGlassColors.GLASS_SURFACE, highlightthickness=0, **kwargs)
        
        self.value = value
        self.command = command
        self._draw()
        self.bind("<Button-1>", self._toggle)
    
    def _draw(self):
        self.delete("all")
        
        bg = LiquidGlassColors.ACCENT_BLUE if self.value else LiquidGlassColors.GLASS_BORDER
        
        # Track
        self.create_oval(0, 0, 28, 28, fill=bg, outline="")
        self.create_oval(22, 0, 50, 28, fill=bg, outline="")
        self.create_rectangle(14, 0, 36, 28, fill=bg, outline="")
        
        # Knob
        knob_x = 26 if self.value else 4
        self.create_oval(knob_x, 4, knob_x+20, 24, fill="white", outline="")
    
    def _toggle(self, e=None):
        self.value = not self.value
        self._draw()
        if self.command:
            self.command(self.value)
    
    def get(self):
        return self.value
    
    def set(self, value):
        self.value = value
        self._draw()


class GlassProgressBar(tk.Canvas):
    """Moderne Progress Bar mit Glaseffekt"""
    def __init__(self, parent, **kwargs):
        super().__init__(parent, height=8, bg=LiquidGlassColors.GLASS_SURFACE, 
                        highlightthickness=0, **kwargs)
        self.progress = 0
        self.bind("<Configure>", self._on_resize)
    
    def _on_resize(self, e=None):
        self._draw()
    
    def _draw(self):
        self.delete("all")
        w = self.winfo_width()
        h = self.winfo_height()
        
        # Background track
        self.create_rectangle(0, 0, w, h, fill=LiquidGlassColors.GLASS_BORDER, outline="")
        
        # Progress fill with gradient effect
        if self.progress > 0:
            pw = int(w * (self.progress / 100))
            color = LiquidGlassColors.SUCCESS if self.progress >= 100 else LiquidGlassColors.ACCENT_BLUE
            self.create_rectangle(0, 0, pw, h, fill=color, outline="")
    
    def set(self, value):
        self.progress = max(0, min(100, value))
        self._draw()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN APPLICATION - LIQUID GLASS UI
# ═══════════════════════════════════════════════════════════════════════════════

class Application(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("⚡ Anki Card Generator")
        
        # Feste, optimale Fenstergröße - kein Resize nötig
        self.geometry("1000x700")
        self.minsize(900, 650)
        self.configure(bg=LiquidGlassColors.BG_PRIMARY)
        
        self.config_manager = ConfigManager()
        self.anki_updater = AnkiUpdater(self.log)
        self.cancelled = False

        self.max_tokens = int(self.config_manager.get('max_tokens', '150'))
        self.temperature = float(self.config_manager.get('temperature', '0.7'))
        self.top_p = float(self.config_manager.get('top_p', '1.0'))
        self.concurrency = int(self.config_manager.get('Concurrency', '5'))
        
        self.all_models = []
        self.all_decks = []

        self.create_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        threading.Thread(target=self.fetch_models, daemon=True).start()
        threading.Thread(target=self.fetch_decks, daemon=True).start()

    def create_ui(self) -> None:
        """Erstellt die komplette Liquid Glass UI"""
        
        # Main Container
        main = tk.Frame(self, bg=LiquidGlassColors.BG_PRIMARY)
        main.pack(fill="both", expand=True, padx=20, pady=20)
        
        # ═══ HEADER ═══
        header = tk.Frame(main, bg=LiquidGlassColors.BG_PRIMARY)
        header.pack(fill="x", pady=(0, 20))
        
        # App Title mit Icon
        title_frame = tk.Frame(header, bg=LiquidGlassColors.BG_PRIMARY)
        title_frame.pack(side="left")
        
        tk.Label(title_frame, text="⚡", font=("SF Pro Display", 28),
                bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.ACCENT_BLUE).pack(side="left")
        tk.Label(title_frame, text="Anki Card Generator", font=("SF Pro Display", 22, "bold"),
                bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.TEXT_PRIMARY).pack(side="left", padx=(8, 0))
        
        # Status Badge
        self.status_label = tk.Label(header, text="● Bereit", font=("SF Pro Text", 11),
                                    bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.SUCCESS)
        self.status_label.pack(side="right")
        
        # ═══ CONTENT AREA - Zwei Spalten ═══
        content = tk.Frame(main, bg=LiquidGlassColors.BG_PRIMARY)
        content.pack(fill="both", expand=True)
        
        # Linke Spalte - Haupteinstellungen (60%)
        left_col = tk.Frame(content, bg=LiquidGlassColors.BG_PRIMARY)
        left_col.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        # Rechte Spalte - Erweitert & Log (40%)
        right_col = tk.Frame(content, bg=LiquidGlassColors.BG_PRIMARY)
        right_col.pack(side="right", fill="both", expand=True, padx=(10, 0))
        
        # ═══ CARD 1: API & Deck ═══
        card1 = GlassCard(left_col, title="🔗 Verbindung")
        card1.pack(fill="x", pady=(0, 12))
        
        # API Key
        row1 = tk.Frame(card1.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row1.pack(fill="x", pady=4)
        tk.Label(row1, text="API Key", font=("SF Pro Text", 11), width=12, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.api_key_entry = GlassEntry(row1, placeholder="OpenRouter API Key eingeben...", show="●")
        self.api_key_entry.pack(side="left", fill="x", expand=True)
        saved_key = self.config_manager.get('OpenRouter_API_Key', '')
        if saved_key:
            self.api_key_entry.set(saved_key)
        
        # Deck Selection
        row2 = tk.Frame(card1.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row2.pack(fill="x", pady=4)
        tk.Label(row2, text="Deck", font=("SF Pro Text", 11), width=12, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.deck_combobox = GlassCombobox(row2, values=["Lade Decks..."])
        self.deck_combobox.pack(side="left", fill="x", expand=True)
        
        # Deck Search
        row2b = tk.Frame(card1.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row2b.pack(fill="x", pady=4)
        tk.Label(row2b, text="Suche", font=("SF Pro Text", 11), width=12, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.deck_search_entry = GlassEntry(row2b, placeholder="Deck filtern...")
        self.deck_search_entry.pack(side="left", fill="x", expand=True)
        self.deck_search_entry.entry.bind("<KeyRelease>", self.apply_deck_filter)
        
        # ═══ CARD 2: Modell ═══
        card2 = GlassCard(left_col, title="🤖 KI Modell")
        card2.pack(fill="x", pady=(0, 12))
        
        # Model Selection
        row3 = tk.Frame(card2.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row3.pack(fill="x", pady=4)
        tk.Label(row3, text="Modell", font=("SF Pro Text", 11), width=12, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.model_combobox = GlassCombobox(row3, values=["Lade Modelle..."])
        self.model_combobox.pack(side="left", fill="x", expand=True)
        
        # Model Search
        row3b = tk.Frame(card2.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row3b.pack(fill="x", pady=4)
        tk.Label(row3b, text="Suche", font=("SF Pro Text", 11), width=12, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.model_search_entry = GlassEntry(row3b, placeholder="Modell filtern...")
        self.model_search_entry.pack(side="left", fill="x", expand=True)
        self.model_search_entry.entry.bind("<KeyRelease>", self.apply_model_filter)
        
        # Model Actions
        row3c = tk.Frame(card2.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row3c.pack(fill="x", pady=(8, 0))
        GlassButton(row3c, text="🔄 Aktualisieren", width=130, height=32,
                   command=lambda: threading.Thread(target=self.fetch_models, daemon=True).start()).pack(side="left", padx=(0, 8))
        GlassButton(row3c, text="⭐ Als Favorit", width=110, height=32,
                   command=self.set_favorite_model).pack(side="left")
        
        # ═══ CARD 3: Prompt ═══
        card3 = GlassCard(left_col, title="✍️ Prompt")
        card3.pack(fill="both", expand=True, pady=(0, 12))
        
        # Prompt Text
        prompt_frame = tk.Frame(card3.content, bg=LiquidGlassColors.GLASS_BORDER,
                               highlightthickness=1, highlightbackground=LiquidGlassColors.GLASS_HIGHLIGHT)
        prompt_frame.pack(fill="both", expand=True)
        
        self.prompt_text = tk.Text(prompt_frame, font=("SF Pro Text", 11), height=6,
                                  bg=LiquidGlassColors.GLASS_SURFACE,
                                  fg=LiquidGlassColors.TEXT_PRIMARY,
                                  insertbackground=LiquidGlassColors.ACCENT_BLUE,
                                  relief="flat", wrap="word", padx=10, pady=8)
        self.prompt_text.pack(fill="both", expand=True)
        self.prompt_text.insert("1.0", self.config_manager.get('Default_Prompt', ''))
        
        tk.Label(card3.content, text="Verwende {frage} als Platzhalter für die Karteninhalte",
                font=("SF Pro Text", 10), bg=LiquidGlassColors.GLASS_SURFACE, 
                fg=LiquidGlassColors.TEXT_MUTED).pack(anchor="w", pady=(8, 0))
        
        # ═══ CARD 4: Felder & Optionen (Rechte Spalte) ═══
        card4 = GlassCard(right_col, title="⚙️ Felder & Optionen")
        card4.pack(fill="x", pady=(0, 12))
        
        # Question Field
        row4 = tk.Frame(card4.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row4.pack(fill="x", pady=4)
        tk.Label(row4, text="Frage-Feld", font=("SF Pro Text", 11), width=14, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.question_field_entry = GlassEntry(row4)
        self.question_field_entry.pack(side="left", fill="x", expand=True)
        self.question_field_entry.set(self.config_manager.get('Question_Field', 'Text'))
        
        # Target Field
        row5 = tk.Frame(card4.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row5.pack(fill="x", pady=4)
        tk.Label(row5, text="Ziel-Feld", font=("SF Pro Text", 11), width=14, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.target_field_entry = GlassEntry(row5)
        self.target_field_entry.pack(side="left", fill="x", expand=True)
        self.target_field_entry.set(self.config_manager.get('Target_Field', 'Extra'))
        
        # Fill Mode
        row6 = tk.Frame(card4.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row6.pack(fill="x", pady=4)
        tk.Label(row6, text="Bei gefüllt", font=("SF Pro Text", 11), width=14, anchor="w",
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.fill_mode_combobox = GlassCombobox(row6, values=["Überspringen", "Überschreiben", "Anhängen"])
        self.fill_mode_combobox.pack(side="left", fill="x", expand=True)
        self.fill_mode_combobox.set(self.config_manager.get('Fill_Mode', 'Überspringen'))
        
        # Cloze Toggle
        row7 = tk.Frame(card4.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row7.pack(fill="x", pady=(8, 4))
        tk.Label(row7, text="Cloze entfernen", font=("SF Pro Text", 11),
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.cloze_toggle = GlassToggle(row7, value=self.config_manager.get('Remove_Cloze', 'True') == 'True')
        self.cloze_toggle.pack(side="right")
        
        # Detailed Logging Toggle
        row8 = tk.Frame(card4.content, bg=LiquidGlassColors.GLASS_SURFACE)
        row8.pack(fill="x", pady=4)
        tk.Label(row8, text="Detail-Logging", font=("SF Pro Text", 11),
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(side="left")
        self.logging_toggle = GlassToggle(row8, value=False, command=self.toggle_detailed_logging)
        self.logging_toggle.pack(side="right")
        
        # ═══ CARD 5: Performance (Rechte Spalte) ═══
        card5 = GlassCard(right_col, title="⚡ Performance")
        card5.pack(fill="x", pady=(0, 12))
        
        # Concurrency Slider
        tk.Label(card5.content, text="Parallele Anfragen", font=("SF Pro Text", 11),
                bg=LiquidGlassColors.GLASS_SURFACE, fg=LiquidGlassColors.TEXT_SECONDARY).pack(anchor="w")
        self.conc_slider = GlassSlider(card5.content, from_=1, to=50, value=self.concurrency,
                                       command=self.on_concurrency_change)
        self.conc_slider.pack(fill="x", pady=(4, 8))
        
        # Advanced Settings Button
        GlassButton(card5.content, text="🔧 Erweiterte Einstellungen", width=200, height=36,
                   command=self.open_advanced_settings).pack(anchor="w")
        
        # ═══ CARD 6: Log (Rechte Spalte) ═══
        card6 = GlassCard(right_col, title="📋 Log")
        card6.pack(fill="both", expand=True, pady=(0, 12))
        
        log_frame = tk.Frame(card6.content, bg=LiquidGlassColors.GLASS_BORDER)
        log_frame.pack(fill="both", expand=True)
        
        self.log_area = tk.Text(log_frame, font=("SF Mono", 10), height=8,
                               bg=LiquidGlassColors.BG_SECONDARY,
                               fg=LiquidGlassColors.TEXT_SECONDARY,
                               insertbackground=LiquidGlassColors.ACCENT_BLUE,
                               relief="flat", wrap="word", padx=10, pady=8, state="disabled")
        self.log_area.pack(fill="both", expand=True)
        
        # ═══ FOOTER - Progress & Actions ═══
        footer = tk.Frame(main, bg=LiquidGlassColors.BG_PRIMARY)
        footer.pack(fill="x", pady=(12, 0))
        
        # Progress Bar
        progress_frame = tk.Frame(footer, bg=LiquidGlassColors.BG_PRIMARY)
        progress_frame.pack(fill="x", pady=(0, 12))
        
        self.progress_label = tk.Label(progress_frame, text="0%", font=("SF Pro Display", 12, "bold"),
                                      bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.ACCENT_BLUE)
        self.progress_label.pack(side="right", padx=(10, 0))
        
        self.progress_bar = GlassProgressBar(progress_frame)
        self.progress_bar.pack(fill="x", expand=True)
        
        # Action Buttons
        actions = tk.Frame(footer, bg=LiquidGlassColors.BG_PRIMARY)
        actions.pack(fill="x")
        
        self.cancel_btn = GlassButton(actions, text="✕ Abbrechen", width=130, height=44,
                                      command=self.cancel_processing)
        self.cancel_btn.pack(side="right", padx=(10, 0))
        self.cancel_btn.set_disabled(True)
        
        self.start_btn = GlassButton(actions, text="▶ Start", width=160, height=44,
                                     accent=True, command=self.start_processing)
        self.start_btn.pack(side="right")

    def on_concurrency_change(self, val):
        self.concurrency = int(float(val))

    def toggle_detailed_logging(self, value):
        global DETAILED_LOGGING
        DETAILED_LOGGING = value
        self.log("Detail-Logging " + ("aktiviert" if value else "deaktiviert"))

    def open_advanced_settings(self):
        adv = tk.Toplevel(self)
        adv.title("Erweiterte Einstellungen")
        adv.geometry("400x350")
        adv.configure(bg=LiquidGlassColors.BG_PRIMARY)
        adv.transient(self)
        adv.grab_set()
        
        # Header
        tk.Label(adv, text="🔧 Erweiterte Einstellungen", font=("SF Pro Display", 16, "bold"),
                bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.TEXT_PRIMARY).pack(pady=(20, 20))
        
        content = tk.Frame(adv, bg=LiquidGlassColors.BG_PRIMARY)
        content.pack(fill="both", expand=True, padx=20)
        
        # Max Tokens
        tk.Label(content, text="Max Tokens", font=("SF Pro Text", 11),
                bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.TEXT_SECONDARY).pack(anchor="w")
        mt_entry = GlassEntry(content)
        mt_entry.pack(fill="x", pady=(4, 12))
        mt_entry.set(str(self.max_tokens))
        
        # Temperature
        tk.Label(content, text="Temperature (0.0 - 1.0)", font=("SF Pro Text", 11),
                bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.TEXT_SECONDARY).pack(anchor="w")
        temp_slider = GlassSlider(content, from_=0, to=10, value=int(self.temperature * 10))
        temp_slider.pack(fill="x", pady=(4, 12))
        
        # Top P
        tk.Label(content, text="Top P (0.0 - 1.0)", font=("SF Pro Text", 11),
                bg=LiquidGlassColors.BG_PRIMARY, fg=LiquidGlassColors.TEXT_SECONDARY).pack(anchor="w")
        tp_slider = GlassSlider(content, from_=0, to=10, value=int(self.top_p * 10))
        tp_slider.pack(fill="x", pady=(4, 20))
        
        def save_adv():
            try:
                self.max_tokens = int(mt_entry.get())
            except ValueError:
                messagebox.showerror("Fehler", "Ungültige Zahl für max_tokens")
                return
            self.temperature = temp_slider.get() / 10.0
            self.top_p = tp_slider.get() / 10.0
            self.config_manager.set('max_tokens', str(self.max_tokens))
            self.config_manager.set('temperature', str(self.temperature))
            self.config_manager.set('top_p', str(self.top_p))
            self.config_manager.save_config()
            self.log("Einstellungen gespeichert")
            adv.destroy()
        
        GlassButton(content, text="💾 Speichern", width=140, height=40, accent=True,
                   command=save_adv).pack(pady=(10, 0))

    def log(self, message: str):
        self.log_area.configure(state="normal")
        self.log_area.insert(tk.END, f"› {message}\n")
        self.log_area.see(tk.END)
        self.log_area.configure(state="disabled")
        logging.info(message)

    def fetch_models(self):
        key = self.api_key_entry.get().strip()
        if not key:
            self.log("Bitte API-Key eingeben")
            return
        self.log("Lade Modelle...")
        self.all_models = fetch_models(key)
        self.after(0, self.apply_model_filter)

    def apply_model_filter(self, event=None):
        ft = self.model_search_entry.get().lower()
        vals = [m for m in self.all_models if ft in m.lower()] if ft else self.all_models
        self.model_combobox.configure(values=vals)
        fav = self.config_manager.get('Favorite_Model', '')
        if fav in vals:
            self.model_combobox.set(fav)
        elif vals:
            self.model_combobox.current(0)

    def set_favorite_model(self):
        sel = self.model_combobox.get()
        if sel:
            self.config_manager.set('Favorite_Model', sel)
            self.config_manager.save_config()
            self.log(f"⭐ Favorit: {sel}")

    def fetch_decks(self):
        self.log("Lade Decks...")
        self.all_decks = get_deck_names()
        self.after(0, self.update_deck_combobox)

    def update_deck_combobox(self):
        self.deck_combobox.configure(values=self.all_decks)
        fav = self.config_manager.get('Favorite_Deck', '')
        if fav in self.all_decks:
            self.deck_combobox.set(fav)
        elif self.all_decks:
            self.deck_combobox.current(0)

    def apply_deck_filter(self, event=None):
        ft = self.deck_search_entry.get().lower().split()
        vals = [d for d in self.all_decks if all(w in d.lower() for w in ft)] if ft else self.all_decks
        self.deck_combobox.configure(values=vals)
        fav = self.config_manager.get('Favorite_Deck', '')
        if fav in vals:
            self.deck_combobox.set(fav)
        elif vals:
            self.deck_combobox.current(0)

    def start_processing(self):
        # Validierung
        if not self.deck_combobox.get().strip():
            self.log("⚠️ Bitte Deck auswählen")
            return
        if not self.api_key_entry.get().strip():
            self.log("⚠️ Bitte API-Key eingeben")
            return
        if not self.prompt_text.get("1.0", tk.END).strip():
            self.log("⚠️ Bitte Prompt eingeben")
            return
        if not self.model_combobox.get().strip():
            self.log("⚠️ Bitte Modell auswählen")
            return

        self.start_btn.set_disabled(True)
        self.cancel_btn.set_disabled(False)
        self.progress_bar.set(0)
        self.progress_label.config(text="0%")
        self.status_label.config(text="● Läuft...", fg=LiquidGlassColors.ACCENT_BLUE)
        self.cancelled = False

        # Config speichern
        self.config_manager.set('OpenRouter_API_Key', self.api_key_entry.get().strip())
        self.config_manager.set('Default_Prompt', self.prompt_text.get("1.0", tk.END).strip())
        self.config_manager.set('Favorite_Deck', self.deck_combobox.get().strip())
        self.config_manager.set('Favorite_Model', self.model_combobox.get().strip())
        self.config_manager.set('Target_Field', self.target_field_entry.get().strip())
        self.config_manager.set('Question_Field', self.question_field_entry.get().strip())
        self.config_manager.set('Fill_Mode', self.fill_mode_combobox.get().strip())
        self.config_manager.set('Remove_Cloze', str(self.cloze_toggle.get()))
        self.config_manager.set('Concurrency', str(self.concurrency))
        self.config_manager.save_config()

        threading.Thread(target=self.run_processing, daemon=True).start()

    def run_processing(self):
        deck = self.deck_combobox.get().strip()
        key = self.api_key_entry.get().strip()
        prompt = self.prompt_text.get("1.0", tk.END).strip()
        model = self.model_combobox.get().strip()
        qf = self.question_field_entry.get().strip()
        tf = self.target_field_entry.get().strip()
        fm = self.fill_mode_combobox.get().strip()
        cc = self.concurrency
        mt = self.max_tokens
        tt = self.temperature
        tp = self.top_p
        rc = self.cloze_toggle.get()

        def progress_cb(done, total):
            pct = int((done / total) * 100)
            self.progress_bar.set(pct)
            self.progress_label.config(text=f"{pct}%")
            if done == total:
                self.status_label.config(text="● Fertig!", fg=LiquidGlassColors.SUCCESS)

        self.log("▶ Starte Verarbeitung...")
        self.anki_updater.process_notes(
            deck, key, model, prompt,
            tf, qf, fm,
            cc, progress_cb, lambda: self.cancelled,
            mt, tt, tp, rc
        )
        
        self.start_btn.set_disabled(False)
        self.cancel_btn.set_disabled(True)
        if not self.cancelled:
            self.status_label.config(text="● Fertig!", fg=LiquidGlassColors.SUCCESS)
        else:
            self.status_label.config(text="● Abgebrochen", fg=LiquidGlassColors.WARNING)

    def cancel_processing(self):
        self.cancelled = True
        self.log("⏹ Abbruch angefordert...")

    def on_close(self):
        self.config_manager.save_config()
        self.destroy()


if __name__ == "__main__":
    app = Application()
    app.mainloop()
