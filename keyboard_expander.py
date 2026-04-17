#!/usr/bin/env python3
"""
Keyboard text expander with LLM integration and profile support.
Requires: pip install pynput pyperclip openai python-dotenv fpdf2

Usage:
  python keyboard_expander.py         # daemon only
  python keyboard_expander.py --ui    # daemon + mappings manager window
"""

import os
import sys
import time
import threading
import subprocess
import ctypes
from datetime import datetime
from pathlib import Path
import pyperclip
from dotenv import load_dotenv
from pynput import keyboard
from pynput.keyboard import Key, Controller

load_dotenv()

import db

MAX_BUFFER = 50

_buffer = ""
_controller = Controller()
_lock = threading.Lock()
_triggers: dict[str, dict] = {}
_triggers_lock = threading.Lock()
_session: dict[str, str] = {}
_show_ui_callback = None
_switch_profile_callback = None
_notify_callback = None
_llm_busy = False
_llm_busy_lock = threading.Lock()


def reload_triggers() -> None:
    global _triggers
    with _triggers_lock:
        _triggers = db.get_all()


def reload_session() -> None:
    global _session
    _session.clear()
    _session.update(db.get_session_vars())


def on_profile_changed() -> None:
    """Call after any profile switch to resync triggers and session."""
    reload_triggers()
    reload_session()


# ── action handlers ──────────────────────────────────────────────────────────

def _get_foreground_window():
    """Return the active window identifier (HWND on Windows, App Name on Mac)."""
    if sys.platform == "darwin":
        try:
            res = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to name of first process whose frontmost is true'],
                capture_output=True, text=True, check=False
            )
            return res.stdout.strip()
        except Exception:
            return None
    elif sys.platform == "win32":
        try:
            return ctypes.windll.user32.GetForegroundWindow()
        except Exception:
            return None
    return None


def _focus_window(handle) -> None:
    """Restore focus to a previously captured window handle."""
    if not handle:
        return
    if sys.platform == "darwin":
        try:
            # Use AppleScript to reactivate the application
            subprocess.run(["osascript", "-e", f'tell application "{handle}" to activate'], check=False)
            time.sleep(0.1)
        except Exception:
            pass
    elif sys.platform == "win32":
        try:
            user32 = ctypes.windll.user32
            # Attach input threads so SetForegroundWindow is allowed
            current_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            target_pid = ctypes.c_ulong(0)
            target_tid = user32.GetWindowThreadProcessId(handle, ctypes.byref(target_pid))
            user32.AttachThreadInput(current_tid, target_tid, True)
            user32.SetForegroundWindow(handle)
            user32.BringWindowToTop(handle)
            user32.AttachThreadInput(current_tid, target_tid, False)
            time.sleep(0.05)
        except Exception:
            pass


def _human_type(text: str, hwnd=None) -> None:
    """Type text character by character at human-like speed."""
    import random
    for char in text:
        _controller.type(char)
        time.sleep(random.uniform(0.04, 0.09))


def _type_output(text: str) -> None:
    """Type output using either human-like emulation or direct typing."""
    if db.get_setting("TYPING_EMULATION_ENABLED", "1") == "1":
        _human_type(text)
    else:
        _controller.type(text)


def _type_locked(text: str, hwnd) -> None:
    """Refocus the target window then type text."""
    _focus_window(hwnd)
    _type_output(text)


def _paste_to_window(text: str, hwnd) -> None:
    """Refocus the target window and paste text via clipboard (instant, no typing)."""
    prev_clipboard = pyperclip.paste()
    pyperclip.copy(text)
    _focus_window(hwnd)
    time.sleep(0.05)
    
    paste_key = Key.cmd if sys.platform == "darwin" else Key.ctrl
    with _controller.pressed(paste_key):
        _controller.tap('v')
    time.sleep(0.1)
    pyperclip.copy(prev_clipboard)


def _alert(title: str, message: str) -> None:
    """Show a blocking alert (for errors). Uses platform defaults."""
    if sys.platform == "darwin":
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display alert "{safe_title}" message "{safe_msg}"'],
            check=False,
        )
    elif sys.platform == "win32":
        # 0x10 is MB_ICONERROR
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    else:
        print(f"ALERT: [{title}] {message}")


def _show_notification(title: str, message: str) -> None:
    """Show a non-blocking notification banner. Uses Tk UI if available."""
    if _notify_callback:
        _notify_callback(title, message)
        return

    if sys.platform == "darwin":
        safe_title = title.replace('"', '\\"')
        safe_msg = message.replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e", f'display notification "{safe_msg}" with title "{safe_title}"'],
            check=False,
        )
    else:
        # Fallback for Windows/Linux without UI
        print(f"NOTIFICATION: [{title}] {message}")


_PDF_CHAR_MAP = {
    "\u2014": "-",    # em dash
    "\u2013": "-",    # en dash
    "\u2012": "-",    # figure dash
    "\u2010": "-",    # hyphen
    "\u2011": "-",    # non-breaking hyphen
    "\u2018": "'",    # left single quote
    "\u2019": "'",    # right single quote
    "\u201a": ",",    # single low quote
    "\u201c": '"',    # left double quote
    "\u201d": '"',    # right double quote
    "\u2026": "...",  # ellipsis
    "\u00a0": " ",    # non-breaking space
    "\u2022": "*",    # bullet
}


def _save_pdf(text: str, filepath: str) -> None:
    for char, replacement in _PDF_CHAR_MAP.items():
        text = text.replace(char, replacement)
    text = text.encode("latin-1", errors="replace").decode("latin-1")

    from fpdf import FPDF
    pdf = FPDF()
    pdf.set_margins(25, 25, 25)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()
    pdf.set_font("Helvetica", size=11)
    pdf.multi_cell(0, 6, text)
    pdf.output(filepath)


def _open_path(path: Path | str) -> None:
    """Open a file or directory in the platform's default file manager."""
    path = Path(path).resolve()
    if not path.exists():
        return

    path_str = str(path)
    try:
        if sys.platform == "darwin":
            subprocess.run(["open", path_str], check=False)
        elif sys.platform == "win32" or os.name == "nt":
            # subprocess with 'explorer' is often more robust than os.startfile in certain shells
            subprocess.run(["explorer", path_str], check=False)
        else:
            # Linux / other
            subprocess.run(["xdg-open", path_str], check=False)
    except Exception as e:
        print(f"Error opening path {path_str}: {e}")


def _do_expand(trigger: str, expansion: str) -> None:
    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)
    # Save and restore clipboard so we don't clobber the user's clipboard
    prev_clipboard = pyperclip.paste()
    pyperclip.copy(expansion)
    time.sleep(0.05)
    
    paste_key = Key.cmd if sys.platform == "darwin" else Key.ctrl
    with _controller.pressed(paste_key):
        _controller.tap('v')
    time.sleep(0.1)
    pyperclip.copy(prev_clipboard)


def _do_store_clipboard(trigger: str, var_name: str) -> None:
    text = pyperclip.paste()
    _session[var_name] = text
    db.set_session_var(var_name, text)
    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)
    _show_notification(f"Stored: {var_name}", f"{len(text)} chars saved to session")
    print(f"[autofiller] stored {len(text)} chars → session['{var_name}']")


def _do_llm_query(trigger: str, prompt_template: str) -> None:
    global _llm_busy
    with _llm_busy_lock:
        if _llm_busy:
            return
        _llm_busy = True
    try:
        _do_llm_query_inner(trigger, prompt_template)
    finally:
        with _llm_busy_lock:
            _llm_busy = False


def _do_llm_query_inner(trigger: str, prompt_template: str) -> None:
    # Capture the focused window BEFORE anything else — this is where output goes
    target_hwnd = _get_foreground_window()

    clipboard_text = pyperclip.paste()

    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)

    api_key = db.get_setting("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _paste_to_window("[ERROR: OPENAI_API_KEY not set]", target_hwnd)
        return

    model = db.get_setting("OPENAI_MODEL", "gpt-4o-mini")

    try:
        # Build prompt from template
        prompt = prompt_template.replace("{{clipboard}}", clipboard_text)
        for var_name, value in _session.items():
            prompt = prompt.replace(f"{{{{{var_name}}}}}", value)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        result = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        response = result.choices[0].message.content or ""

        if response:
            _paste_to_window(response, target_hwnd)
    except Exception as e:
        _paste_to_window(f"[LLM ERROR: {e}]", target_hwnd)


def _do_gen_cover_letter(trigger: str, prompt_template: str) -> None:
    global _llm_busy
    with _llm_busy_lock:
        if _llm_busy:
            return
        _llm_busy = True
    try:
        _do_gen_cover_letter_inner(trigger, prompt_template)
    finally:
        with _llm_busy_lock:
            _llm_busy = False


def _do_gen_cover_letter_inner(trigger: str, prompt_template: str) -> None:
    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)

    api_key = db.get_setting("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _alert("AutoFiller Error", "OPENAI_API_KEY not set")
        return

    model = db.get_setting("OPENAI_MODEL", "gpt-4o-mini")
    _show_notification("Cover Letter", "Generating (humanized)...")

    try:
        # Build prompt from template
        prompt = prompt_template
        prompt = prompt.replace("{{date}}", datetime.now().strftime("%B %d, %Y"))
        for var_name, value in _session.items():
            prompt = prompt.replace(f"{{{{{var_name}}}}}", value)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        result = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        text = result.choices[0].message.content or ""

        if not text:
            return

        filename = "coverletter.pdf"
        filepath = Path.home() / "Downloads" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.unlink(missing_ok=True)
        _save_pdf(text, str(filepath))
        pyperclip.copy(text)

        _show_notification("Cover Letter Generated", f"Saved as {filename} · Copied to clipboard")
        if db.get_setting("COVERLETTER_OPEN_FINDER", "1") == "1":
            _open_path(Path.home() / "Downloads")
    except Exception as e:
        _alert("AutoFiller Error", str(e))


def _do_gen_resume(trigger: str, prompt_template: str) -> None:
    global _llm_busy
    with _llm_busy_lock:
        if _llm_busy:
            return
        _llm_busy = True
    try:
        _do_gen_resume_inner(trigger, prompt_template)
    finally:
        with _llm_busy_lock:
            _llm_busy = False


def _do_gen_resume_inner(trigger: str, prompt_template: str) -> None:
    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)

    api_key = db.get_setting("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key:
        _alert("AutoFiller Error", "OPENAI_API_KEY not set")
        return

    model = db.get_setting("OPENAI_MODEL", "gpt-4o-mini")
    _show_notification("Resume", "Generating (humanized)...")

    try:
        # Build prompt from template
        prompt = prompt_template
        for var_name, value in _session.items():
            prompt = prompt.replace(f"{{{{{var_name}}}}}", value)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        result = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
        )
        text = result.choices[0].message.content or ""

        if not text:
            return

        filename = "resume.pdf"
        filepath = Path.home() / "Downloads" / filename
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.unlink(missing_ok=True)

        # Since we removed job_assistant/generate_resume dependencies,
        # we'll save the raw text to PDF using our internal helper.
        _save_pdf(text, str(filepath))
        pyperclip.copy(text)

        _show_notification("Resume Generated", f"Saved as {filename} · Copied to clipboard")
        if db.get_setting("RESUME_OPEN_FINDER", "1") == "1":
            _open_path(Path.home() / "Downloads")
    except Exception as e:
        _alert("AutoFiller Error", str(e))


def _do_show_ui(trigger: str) -> None:
    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)
    if _show_ui_callback:
        _show_ui_callback()


def _do_switch_profile(trigger: str) -> None:
    time.sleep(0.05)
    for _ in range(len(trigger)):
        _controller.tap(Key.backspace)
        time.sleep(0.02)
    if _switch_profile_callback:
        _switch_profile_callback()


# ── keyboard listener ─────────────────────────────────────────────────────────

def _on_press(key):
    global _buffer

    try:
        char = key.char
    except AttributeError:
        char = None

    with _lock:
        if key in (Key.enter, Key.tab, Key.esc,
                   Key.left, Key.right, Key.up, Key.down,
                   Key.home, Key.end, Key.page_up, Key.page_down):
            _buffer = ""
            return

        if key == Key.backspace:
            _buffer = _buffer[:-1]
            return

        if char is None:
            return

        _buffer = (_buffer + char)[-MAX_BUFFER:]

        with _triggers_lock:
            current = dict(_triggers)

        match = None
        for shortcut in sorted(current, key=len, reverse=True):
            if _buffer.endswith(shortcut):
                match = shortcut
                break

        if not match:
            return

        entry = current[match]
        action = entry["action"]
        expansion = entry["expansion"]
        _buffer = ""

        if action == "expand":
            t = threading.Thread(target=_do_expand, args=(match, expansion), daemon=True)
        elif action == "store_clipboard":
            t = threading.Thread(target=_do_store_clipboard, args=(match, expansion), daemon=True)
        elif action == "llm_query":
            t = threading.Thread(target=_do_llm_query, args=(match, expansion), daemon=True)
        elif action == "gen_cover_letter":
            t = threading.Thread(target=_do_gen_cover_letter, args=(match, expansion), daemon=True)
        elif action == "gen_resume":
            t = threading.Thread(target=_do_gen_resume, args=(match, expansion), daemon=True)
        elif action == "show_ui":
            t = threading.Thread(target=_do_show_ui, args=(match,), daemon=True)
        elif action == "switch_profile":
            t = threading.Thread(target=_do_switch_profile, args=(match,), daemon=True)
        else:
            return

        t.start()


def run_listener():
    with keyboard.Listener(on_press=_on_press) as listener:
        listener.join()


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    global _show_ui_callback, _switch_profile_callback, _notify_callback

    db.init_db()
    reload_session()
    reload_triggers()

    if "--ui" in sys.argv:
        listener_thread = threading.Thread(target=run_listener, daemon=True)
        listener_thread.start()

        from ui import ManagerWindow
        app = ManagerWindow(
            on_profile_changed=on_profile_changed,
            get_session=db.get_session_vars,
        )
        _show_ui_callback = app.show_window
        _switch_profile_callback = app.show_profile_switcher
        _notify_callback = app.show_notification
        print("AutoFiller running with UI. Close the window to quit.")
        app.mainloop()
    else:
        print("Keyboard expander running. Press Ctrl+C to quit.")
        print(f"Profile: {db.get_current_profile_name()}")
        print("Shortcuts:", list(_triggers.keys()))
        print("Run with --ui to open the mappings manager.")
        try:
            run_listener()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
