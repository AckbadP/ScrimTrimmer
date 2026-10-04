"""
gui.py - Tkinter GUI for the EVE AT Practice Trimmer.

Prerequisites:
  sudo apt-get install python3-tk
  pip install tkinterdnd2

Launch:
  python src/gui.py
"""

import argparse
import os
import re
import sys
import threading
import time

import psutil

# When frozen by PyInstaller, restore the original LD_LIBRARY_PATH so that
# system binaries (tesseract, ffmpeg) load the system libstdc++ instead of
# the older bundled copy. Bundled .so files (cv2, numpy, etc.) use RPATH and
# don't need LD_LIBRARY_PATH to find their dependencies.
if getattr(sys, "frozen", False):
    _orig_ld = os.environ.get("LD_LIBRARY_PATH_ORIG")
    if _orig_ld is not None:
        os.environ["LD_LIBRARY_PATH"] = _orig_ld
    else:
        os.environ.pop("LD_LIBRARY_PATH", None)


def _resource_path(relative_to_src: str) -> str:
    """Return absolute path to a bundled resource, works for dev and PyInstaller."""
    if getattr(sys, "frozen", False):
        base = sys._MEIPASS  # type: ignore[attr-defined]
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative_to_src)

import config as cfg

try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, font as tkfont
except ImportError as e:
    print(f"Error: {e}")
    print("Install prerequisites: sudo apt-get install python3-tk && pip install tkinterdnd2")
    sys.exit(1)

try:
    import cv2
    from PIL import Image, ImageTk
except ImportError as e:
    print(f"Error: {e}")
    print("Install prerequisites: pip install opencv-python pillow")
    sys.exit(1)

from frame_extractor import get_video_duration, is_growing
import main as pipeline
import native_dialogs


VALID_EXTENSIONS = {".mp4", ".mkv"}
THUMB_W, THUMB_H = 320, 180
VIDEO_POLL_INTERVAL_MS = 5000  # how often to re-check the default video dir for a newer recording

_DEFAULT_REGION = [0.0, 0.35, 0.15, 1.0]  # [x1, y1, x2, y2] as fractions


class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__(className="scrim-trimmer")
        self.title("EVE AT Practice Trimmer")
        self.wm_iconname("scrim-trimmer")
        self.resizable(True, True)

        self.video_path: str | None = None
        # True while the current selection came from auto-load rather than the
        # user explicitly picking a file (Browse/drag-drop) — only then do we
        # keep re-scanning the default video dir for a newer recording.
        self._auto_video = True
        self._video_poll_after_id = None
        self._thumb_ref = None  # prevent GC of PhotoImage
        self._thumb_pil: Image.Image | None = None  # source image for re-rendering on resize
        self._cancel_event = threading.Event()
        self._timer_start: float | None = None
        self._timer_frozen: float | None = None
        self._timer_after_id = None
        self._geom_save_after = None

        # Canvas state
        self._canvas_img_id = None
        self._canvas_rect_id = None
        self._drag_start = None
        # Letterboxed image rect within the canvas: (x_offset, y_offset, width, height)
        self._img_rect: tuple[int, int, int, int] | None = None

        self._conf = cfg.load_config()
        _conf = self._conf
        self.default_video_dir_var = tk.StringVar(value=_conf.get("video_dir", ""))
        self.default_log_dir_var = tk.StringVar(value=_conf.get("log_dir", ""))
        self.default_output_dir_var = tk.StringVar(value=_conf.get("output_dir", ""))
        self.default_chapters_dir_var = tk.StringVar(value=_conf.get("chapters_dir", ""))

        raw = _conf.get("chat_region", _DEFAULT_REGION)
        self._chat_region: list[float] = list(raw) if len(raw) == 4 else list(_DEFAULT_REGION)

        # Auto-save config whenever any default directory changes
        self.default_video_dir_var.trace_add("write", self._save_config)
        self.default_log_dir_var.trace_add("write", self._save_config)
        self.default_output_dir_var.trace_add("write", self._save_config)
        self.default_chapters_dir_var.trace_add("write", self._save_config)
        self.show_chapters_popup_var = tk.BooleanVar(value=bool(_conf.get("show_chapters_popup", True)))
        self.show_chapters_popup_var.trace_add("write", self._save_config)
        self.close_on_complete_var = tk.BooleanVar(value=bool(_conf.get("close_on_complete", False)))
        self.close_on_complete_var.trace_add("write", self._save_config)
        self.show_debug_popup_var = tk.BooleanVar(value=bool(_conf.get("show_debug_popup", False)))
        self.show_debug_popup_var.trace_add("write", self._save_config)
        self.tournament_match_var = tk.BooleanVar(value=bool(_conf.get("tournament_match", False)))
        self.tournament_match_var.trace_add("write", self._save_config)
        self.detect_countdown_var = tk.BooleanVar(value=bool(_conf.get("detect_countdown", True)))
        self.detect_countdown_var.trace_add("write", self._save_config)
        self.buffer_start_var = tk.IntVar(value=_conf.get("buffer_start", 0))
        self.buffer_start_var.trace_add("write", self._save_config)
        self.buffer_end_var = tk.IntVar(value=_conf.get("buffer_end", 0))
        self.buffer_end_var.trace_add("write", self._save_config)
        self.tournament_buffer_start_var = tk.IntVar(value=_conf.get("tournament_buffer_start", 0))
        self.tournament_buffer_start_var.trace_add("write", self._save_config)
        self.tournament_buffer_end_var = tk.IntVar(value=_conf.get("tournament_buffer_end", 10))
        self.tournament_buffer_end_var.trace_add("write", self._save_config)
        self.extract_audio_var = tk.BooleanVar(value=bool(_conf.get("extract_audio", False)))
        self.extract_audio_var.trace_add("write", self._save_config)
        self.youtube_upload_var = tk.BooleanVar(value=bool(_conf.get("youtube_upload", False)))
        self.youtube_upload_var.trace_add("write", self._save_config)
        self.youtube_title_var = tk.StringVar(value=cfg.default_youtube_title())

        self._build_ui()

        saved_geom = _conf.get("window_geometry", "")
        if saved_geom:
            self.geometry(saved_geom)
        self.bind("<Configure>", self._on_window_configure)

        # Auto-load most recent video if a default video directory is configured.
        # The scan samples file size to tell a still-recording file apart from a
        # finished one (see _find_latest_video), so it can block briefly — run it
        # off the main thread so it never delays showing the window.
        video_dir = os.path.expanduser(self.default_video_dir_var.get().strip())
        if video_dir and os.path.isdir(video_dir):
            self._scan_latest_video_async(video_dir)
        self._schedule_video_poll()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self):
        # --- Drop zone ---
        self.drop_frame = ttk.LabelFrame(self, text="Video File", padding=12)
        self.drop_frame.pack(fill=tk.X, padx=16, pady=(16, 0))

        self.drop_hint = ttk.Label(
            self.drop_frame,
            text="Drag a video file here",
            font=("TkDefaultFont", 13),
            anchor="center",
        )
        self.drop_hint.pack(fill=tk.X, pady=(8, 6))

        ttk.Button(self.drop_frame, text="Browse...", command=self._browse).pack()

        self.drop_frame.drop_target_register(DND_FILES)
        self.drop_frame.dnd_bind("<<Drop>>", self._on_drop)

        # --- Preview (expands to fill extra window space when resized) ---
        self.preview_frame = ttk.LabelFrame(self, text="Preview", padding=8)

        _bg = ttk.Style().lookup("TFrame", "background") or self.cget("bg")

        # Left column: canvas + controls
        canvas_col = ttk.Frame(self.preview_frame)
        canvas_col.pack(side=tk.LEFT, padx=(0, 12), fill=tk.BOTH, expand=True)

        # Canvas with horizontal and vertical scrollbars
        canvas_container = ttk.Frame(canvas_col)
        canvas_container.pack(fill=tk.BOTH, expand=True)
        canvas_container.rowconfigure(0, weight=1)
        canvas_container.columnconfigure(0, weight=1)

        self._cv_vscroll_preview = ttk.Scrollbar(canvas_container, orient=tk.VERTICAL)
        self._cv_vscroll_preview.grid(row=0, column=1, sticky=tk.NS)

        self._cv_hscroll = ttk.Scrollbar(canvas_container, orient=tk.HORIZONTAL)
        self._cv_hscroll.grid(row=1, column=0, sticky=tk.EW)

        self.thumb_canvas = tk.Canvas(
            canvas_container,
            bg=_bg, bd=0,
            highlightthickness=0,
            cursor="crosshair",
            xscrollcommand=self._cv_hscroll.set,
            yscrollcommand=self._cv_vscroll_preview.set,
        )
        self.thumb_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        self._cv_hscroll.config(command=self.thumb_canvas.xview)
        self._cv_vscroll_preview.config(command=self.thumb_canvas.yview)

        ttk.Label(
            canvas_col,
            text="Drag to select chat region",
            font=("TkDefaultFont", 8),
            foreground="#888",
        ).pack(pady=(2, 0))

        self.thumb_canvas.bind("<ButtonPress-1>", self._on_region_press)
        self.thumb_canvas.bind("<B1-Motion>", self._on_region_drag)
        self.thumb_canvas.bind("<ButtonRelease-1>", self._on_region_release)
        self.thumb_canvas.bind("<Configure>", self._on_canvas_resize)

        self.info_label = ttk.Label(canvas_col, text="", justify=tk.CENTER)
        self.info_label.pack(pady=(2, 0))


        # Pre-pack then hide; _load_video re-packs it after drop_frame
        self.preview_frame.pack(after=self.drop_frame, fill=tk.BOTH, expand=True, padx=16, pady=(10, 0))
        self.preview_frame.pack_forget()

        # --- Options ---
        self.opts_frame = ttk.LabelFrame(self, text="Options", padding=8)
        self.opts_frame.pack(fill=tk.X, padx=16, pady=(10, 0))

        saved_out = self.default_output_dir_var.get()
        self.output_var = tk.StringVar(value=saved_out if saved_out else "out")
        self.t0_var = tk.StringVar()
        self.chat_log_var = tk.StringVar()
        self.verbose_var = tk.BooleanVar(value=False)
        self.run_without_ffmpeg_var = tk.BooleanVar(value=False)
        self.force_ocr_var = tk.BooleanVar(value=False)
        _cpu = os.cpu_count() or 4
        self.threads_var = tk.IntVar(value=self._conf.get("threads") or _cpu)
        self.ram_cap_var = tk.IntVar(value=self._conf.get("ram_cap_gb") or 10)

        notebook = ttk.Notebook(self.opts_frame)
        notebook.pack(fill=tk.X)

        # --- Main tab ---
        main_tab = ttk.Frame(notebook, padding=(4, 6))
        main_tab.columnconfigure(1, weight=1)
        notebook.add(main_tab, text="Main")

        # output dir
        out_frame = ttk.Frame(main_tab)
        out_frame.grid(row=0, column=1, sticky=tk.EW, pady=2)
        out_frame.columnconfigure(0, weight=1)
        ttk.Label(main_tab, text="Output dir").grid(row=0, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Entry(out_frame, textvariable=self.output_var).grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(out_frame, text="Browse", width=7,
                   command=self._browse_output).grid(row=0, column=1, padx=(4, 0))

        # chat-log
        ttk.Label(main_tab, text="Chat log").grid(row=1, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        cl_frame = ttk.Frame(main_tab)
        cl_frame.grid(row=1, column=1, sticky=tk.EW, pady=2)
        cl_frame.columnconfigure(0, weight=1)
        ttk.Entry(cl_frame, textvariable=self.chat_log_var).grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(cl_frame, text="Browse", width=7,
                   command=self._browse_chatlog).grid(row=0, column=1, padx=(4, 0))

        # tournament match toggle
        ttk.Label(main_tab, text="Tournament Match?").grid(row=2, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(main_tab, variable=self.tournament_match_var).grid(row=2, column=1, sticky=tk.W, pady=2)

        # video title
        ttk.Label(main_tab, text="Youtube Video Title").grid(row=3, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        yt_title_frame = ttk.Frame(main_tab)
        yt_title_frame.grid(row=3, column=1, sticky=tk.EW, pady=2)
        yt_title_frame.columnconfigure(0, weight=1)
        self._yt_title_entry = ttk.Entry(yt_title_frame, textvariable=self.youtube_title_var)
        self._yt_title_entry.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(main_tab, text="Defaults to today's date (UTC). Leave blank to use the video filename.",
                  foreground="#888", font=("TkDefaultFont", 8)).grid(
            row=4, column=0, columnspan=2, sticky=tk.W, pady=(0, 4))

        # --- Advanced tab ---
        adv_tab = ttk.Frame(notebook, padding=(4, 6))
        adv_tab.columnconfigure(1, weight=1)
        notebook.add(adv_tab, text="Advanced")

        # defaults sub-section
        defaults_lf = ttk.LabelFrame(adv_tab, text="Default Directories", padding=6)
        defaults_lf.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))
        defaults_lf.columnconfigure(1, weight=1)

        def _defaults_row(row, label, var, browse_cmd):
            ttk.Label(defaults_lf, text=label).grid(
                row=row, column=0, sticky=tk.W, pady=2, padx=(0, 8))
            f = ttk.Frame(defaults_lf)
            f.grid(row=row, column=1, sticky=tk.EW, pady=2)
            f.columnconfigure(0, weight=1)
            e = ttk.Entry(f, textvariable=var)
            e.grid(row=0, column=0, sticky=tk.EW)
            e.bind("<FocusOut>", lambda _: self._save_config())
            ttk.Button(f, text="Browse", width=7, command=browse_cmd).grid(
                row=0, column=1, padx=(4, 0))

        _defaults_row(0, "Video directory", self.default_video_dir_var, self._browse_default_video_dir)
        _defaults_row(1, "Log directory", self.default_log_dir_var, self._browse_default_log_dir)
        _defaults_row(2, "Output directory", self.default_output_dir_var, self._browse_default_output_dir)
        _defaults_row(3, "Chapters file dir", self.default_chapters_dir_var, self._browse_default_chapters_dir)

        # clip buffers sub-section
        buffers_lf = ttk.LabelFrame(adv_tab, text="Clip Buffers (seconds)", padding=6)
        buffers_lf.grid(row=1, column=0, columnspan=2, sticky=tk.EW, pady=(0, 8))

        ttk.Label(buffers_lf, text="").grid(row=0, column=0)
        ttk.Label(buffers_lf, text="Start").grid(row=0, column=1, padx=(4, 12))
        ttk.Label(buffers_lf, text="End").grid(row=0, column=2, padx=(4, 0))

        def _buffer_spinbox(parent, var):
            sb = ttk.Spinbox(parent, from_=-300, to=300, textvariable=var,
                              width=5, command=self._save_config)
            sb.bind("<FocusOut>", lambda _: self._save_config())
            return sb

        ttk.Label(buffers_lf, text="Normal").grid(row=1, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        _buffer_spinbox(buffers_lf, self.buffer_start_var).grid(row=1, column=1, padx=(4, 12), pady=2)
        _buffer_spinbox(buffers_lf, self.buffer_end_var).grid(row=1, column=2, padx=(4, 0), pady=2)

        ttk.Label(buffers_lf, text="Tournament").grid(row=2, column=0, sticky=tk.W, padx=(0, 8), pady=2)
        _buffer_spinbox(buffers_lf, self.tournament_buffer_start_var).grid(row=2, column=1, padx=(4, 12), pady=2)
        _buffer_spinbox(buffers_lf, self.tournament_buffer_end_var).grid(row=2, column=2, padx=(4, 0), pady=2)

        ttk.Label(buffers_lf, text="Added to each clip's start/end before extraction. Negative = earlier.",
                  foreground="#888", font=("TkDefaultFont", 8)).grid(
            row=3, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))

        # t0
        ttk.Label(adv_tab, text="Recording start time (UTC)").grid(
            row=2, column=0, sticky=tk.W, pady=(4, 0), padx=(0, 8))
        t0_entry = ttk.Entry(adv_tab, textvariable=self.t0_var, width=14)
        t0_entry.grid(row=2, column=1, sticky=tk.W, pady=(4, 0))
        t0_entry.insert(0, "HH:MM:SS (optional)")
        t0_entry.bind("<FocusIn>", lambda e: t0_entry.delete(0, tk.END)
                      if self.t0_var.get() == "HH:MM:SS (optional)" else None)
        t0_entry.bind("<FocusOut>", lambda e: t0_entry.insert(0, "HH:MM:SS (optional)")
                      if not self.t0_var.get() else None)
        ttk.Label(adv_tab, text="EVE game clock (UTC) when the video started — used to sync\n"
                                "chat log timestamps. Auto-detected if left blank.",
                  foreground="#888", font=("TkDefaultFont", 8)).grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=(0, 4), padx=(0, 8))

        # threads
        ttk.Label(adv_tab, text="Threads").grid(row=4, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        threads_frame = ttk.Frame(adv_tab)
        threads_frame.grid(row=4, column=1, sticky=tk.W, pady=2)
        ttk.Spinbox(threads_frame, from_=1, to=64, textvariable=self.threads_var,
                    width=5, command=self._save_config).grid(row=0, column=0)
        ttk.Label(threads_frame, text=f"(CPU cores: {os.cpu_count() or '?'})",
                  foreground="#888").grid(row=0, column=1, padx=(6, 0))

        # ram cap
        ttk.Label(adv_tab, text="RAM cap (GB)").grid(row=5, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ram_frame = ttk.Frame(adv_tab)
        ram_frame.grid(row=5, column=1, sticky=tk.W, pady=2)
        ttk.Spinbox(ram_frame, from_=1, to=256, textvariable=self.ram_cap_var,
                    width=5, command=self._save_config).grid(row=0, column=0)
        _total_ram = round(psutil.virtual_memory().total / 1024 ** 3)
        ttk.Label(ram_frame, text=f"GB max frames in memory (total: {_total_ram} GB)",
                  foreground="#888").grid(row=0, column=1, padx=(6, 0))

        # verbose
        ttk.Label(adv_tab, text="verbose").grid(row=6, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.verbose_var).grid(row=6, column=1, sticky=tk.W, pady=2)

        # run without ffmpeg
        ttk.Label(adv_tab, text="Run without ffmpeg").grid(
            row=7, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(
            adv_tab, variable=self.run_without_ffmpeg_var,
            command=self._on_run_without_ffmpeg_toggle,
        ).grid(row=7, column=1, sticky=tk.W, pady=2)

        # force OCR pipeline
        ttk.Label(adv_tab, text="Force OCR pipeline").grid(
            row=8, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.force_ocr_var).grid(
            row=8, column=1, sticky=tk.W, pady=2)

        # bare-countdown detection
        ttk.Label(adv_tab, text="Detect bare countdowns as CD").grid(
            row=9, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.detect_countdown_var).grid(
            row=9, column=1, sticky=tk.W, pady=2)
        ttk.Label(adv_tab, text="Treat a bare descending countdown ('10', '9', '8', ...) as an\n"
                                "implicit CD when no 'CD' was typed. In OCR mode this can produce\n"
                                "spurious clip starts from misread digits.",
                  foreground="#888", font=("TkDefaultFont", 8)).grid(
            row=10, column=0, columnspan=2, sticky=tk.W, pady=(0, 4), padx=(0, 8))

        # per-clip audio extraction
        ttk.Label(adv_tab, text="Extract match audio").grid(
            row=11, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.extract_audio_var).grid(
            row=11, column=1, sticky=tk.W, pady=2)
        ttk.Label(adv_tab, text="Save each match's audio as an MP3 in a folder named after the\n"
                                "video, inside the output directory. Requires ffmpeg.",
                  foreground="#888", font=("TkDefaultFont", 8)).grid(
            row=12, column=0, columnspan=2, sticky=tk.W, pady=(0, 4), padx=(0, 8))

        # chapter timestamps popup
        ttk.Label(adv_tab, text="Show timestamps popup").grid(
            row=13, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.show_chapters_popup_var).grid(
            row=13, column=1, sticky=tk.W, pady=2)

        # close on complete
        ttk.Label(adv_tab, text="Close when done").grid(
            row=14, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.close_on_complete_var).grid(
            row=14, column=1, sticky=tk.W, pady=2)

        # debug output popup
        ttk.Label(adv_tab, text="Show debug output").grid(
            row=15, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.show_debug_popup_var).grid(
            row=15, column=1, sticky=tk.W, pady=2)

        # YouTube upload
        ttk.Separator(adv_tab, orient=tk.HORIZONTAL).grid(
            row=16, column=0, columnspan=2, sticky=tk.EW, pady=(8, 4))
        ttk.Label(adv_tab, text="Upload to YouTube").grid(
            row=17, column=0, sticky=tk.W, pady=2, padx=(0, 8))
        ttk.Checkbutton(adv_tab, variable=self.youtube_upload_var).grid(
            row=17, column=1, sticky=tk.W, pady=2)

        _last_nb_content_height = [None]

        def _on_tab_changed(event=None):
            tab = notebook.nametowidget(notebook.select())
            new_h = tab.winfo_reqheight()
            if _last_nb_content_height[0] is not None:
                delta = new_h - _last_nb_content_height[0]
                if delta != 0:
                    self.geometry(f"{self.winfo_width()}x{self.winfo_height() + delta}")
            _last_nb_content_height[0] = new_h
            notebook.configure(height=new_h)

        notebook.bind("<<NotebookTabChanged>>", _on_tab_changed)

        def _apply_initial_tab_size():
            self.update_idletasks()
            tab = notebook.nametowidget(notebook.select())
            h = tab.winfo_reqheight()
            _last_nb_content_height[0] = h
            notebook.configure(height=h)

        self.after_idle(_apply_initial_tab_size)

        # --- Reset / Help buttons ---
        btn_frame = ttk.Frame(self)
        btn_frame.pack(pady=(12, 4))

        ttk.Button(btn_frame, text="Reset", command=self._reset, width=10).pack(side=tk.LEFT, padx=6)
        ttk.Button(btn_frame, text="Help", command=self._show_help, width=10).pack(side=tk.LEFT, padx=6)

        # --- Status / progress bar ---
        style = ttk.Style()
        _trough = style.lookup("TProgressbar", "troughcolor") or "#d9d9d9"
        _fill = "#4a90d9"
        _fg = style.lookup("TLabel", "foreground") or "black"
        self._progress_pct = 0
        self._progress_msg = "Ready"
        self._progress_running = False
        self._progress_fg_idle = _fg
        self.progress_canvas = tk.Canvas(self, height=22, bd=1, relief=tk.SUNKEN,
                                         highlightthickness=0, bg=_trough)
        self.progress_canvas.pack(fill=tk.X, padx=16, pady=(4, 0))
        self._prog_fill = self.progress_canvas.create_rectangle(0, 0, 0, 22, fill=_fill, outline="")
        self._prog_text = self.progress_canvas.create_text(
            0, 11, text="Ready", fill=_fg, anchor="center",
            font=("TkDefaultFont", 9, "bold"))
        self.progress_canvas.bind("<Configure>", lambda e: self._redraw_progress())

        # --- Run / Cancel buttons (bottom) ---
        run_frame = ttk.Frame(self)
        run_frame.pack(fill=tk.X, padx=16, pady=(8, 16))
        run_frame.columnconfigure(0, weight=1)

        self.run_btn = ttk.Button(run_frame, text="Run", command=self._run,
                                  padding=(0, 8))
        self.run_btn.grid(row=0, column=0, sticky=tk.EW, padx=(6, 3))

        self.cancel_btn = ttk.Button(run_frame, text="Cancel", command=self._cancel,
                                     padding=(0, 8), state="disabled", width=10)
        self.cancel_btn.grid(row=0, column=1, padx=(3, 6))

        self.timer_label = ttk.Label(run_frame, text="", width=7, anchor="e",
                                     font=("TkFixedFont", 9))
        self.timer_label.grid(row=0, column=2, padx=(0, 6))

    # ------------------------------------------------------------------
    # File acquisition
    # ------------------------------------------------------------------

    def _on_drop(self, event):
        path = event.data.strip().strip("{}")
        self._load_video(path)

    def _browse(self):
        path = native_dialogs.askopenfilename(
            title="Select video file",
            filetypes=[("Video files", "*.mp4 *.mkv"), ("All files", "*.*")],
        )
        if path:
            self._load_video(path)

    def _browse_output(self):
        d = native_dialogs.askdirectory(title="Select output directory")
        if d:
            self.output_var.set(d)

    def _browse_chatlog(self):
        paths = native_dialogs.askopenfilenames(
            title="Select EVE chat log(s)",
            filetypes=[("Log files", "*.txt *.log"), ("All files", "*.*")],
        )
        if paths:
            self.chat_log_var.set(";".join(paths))

    def _browse_default_video_dir(self):
        d = native_dialogs.askdirectory(title="Select default video directory")
        if d:
            self.default_video_dir_var.set(d)
            self._scan_latest_video_async(d)

    def _browse_default_log_dir(self):
        d = native_dialogs.askdirectory(title="Select default log directory")
        if d:
            self.default_log_dir_var.set(d)

    def _browse_default_output_dir(self):
        d = native_dialogs.askdirectory(title="Select default output directory")
        if d:
            self.default_output_dir_var.set(d)

    def _browse_default_chapters_dir(self):
        d = native_dialogs.askdirectory(title="Select default chapters file directory")
        if d:
            self.default_chapters_dir_var.set(d)

    # ------------------------------------------------------------------
    # Video loading + thumbnail
    # ------------------------------------------------------------------

    def _load_video(self, path: str, auto: bool = False, skipped: str | None = None):
        """
        Load path as the active video.

        auto marks the load as coming from the default-video-dir auto-selection
        rather than an explicit user choice (Browse/drag-drop); only an auto
        load keeps the periodic re-scan (_poll_latest_video) armed. skipped, if
        given, is the basename of a newer file in the same dir that was passed
        over because it looked still-in-progress — surfaced in the status line.
        """
        ext = os.path.splitext(path)[1].lower()
        if ext not in VALID_EXTENSIONS:
            if auto:
                return
            messagebox.showerror("Invalid file",
                                 f"Unsupported file type '{ext}'.\nExpected .mp4 or .mkv.")
            return

        if not os.path.isfile(path):
            if auto:
                return
            messagebox.showerror("File not found", f"Cannot find:\n{path}")
            return

        self.video_path = path
        self._auto_video = auto

        # Thumbnail
        pil = self._extract_thumbnail(path)
        if pil:
            self._update_canvas_image(pil)
        else:
            self._thumb_pil = None
            self.thumb_canvas.delete("all")
            self._canvas_img_id = None
            self._canvas_rect_id = None
            self.thumb_canvas.create_text(
                THUMB_W // 2, THUMB_H // 2, text="(no preview)", fill="white"
            )

        # Info label
        try:
            duration = get_video_duration(path)
            dur_str = self._fmt_duration(duration)
        except Exception:
            dur_str = "?"
        self.info_label.configure(
            text=f"{os.path.basename(path)}\n{dur_str}"
        )

        # Auto-fill output dir from default
        default_out = os.path.expanduser(self.default_output_dir_var.get().strip())
        if default_out:
            self.output_var.set(default_out)

        # Auto-select closest "Local" log — reveal first so errors are always visible
        self.preview_frame.pack(after=self.drop_frame, fill=tk.BOTH, expand=True, padx=16, pady=(10, 0))
        self.run_btn.configure(state="normal")

        status_log = ""
        _log_dir_setting = self.default_log_dir_var.get().strip()
        default_log = os.path.expanduser(_log_dir_setting) if _log_dir_setting else cfg.default_log_dir()
        if default_log:
            if not os.path.isdir(default_log):
                status_log = f"  |  Log dir not found: {default_log}"
            else:
                try:
                    best = self._find_closest_log(path, default_log)
                    if best:
                        self.chat_log_var.set(best)
                        status_log = f"  |  Log: {os.path.basename(best)}"
                    else:
                        all_files = [
                            f for f in os.listdir(default_log)
                            if os.path.isfile(os.path.join(default_log, f))
                        ]
                        status_log = (
                            f"  |  No 'Local' file in {os.path.basename(default_log)}"
                            f" ({len(all_files)} files total)"
                        )
                except Exception as exc:
                    status_log = f"  |  Log error: {exc}"

        status_skip = f"  |  skipped {skipped} (still recording)" if skipped else ""
        self._set_status(f"Loaded: {os.path.basename(path)}{status_log}{status_skip}")

    def _extract_thumbnail(self, path: str) -> Image.Image | None:
        try:
            cap = cv2.VideoCapture(path)
            total = cap.get(cv2.CAP_PROP_FRAME_COUNT)
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(total * 0.10))
            ret, frame = cap.read()
            cap.release()
            if not ret:
                return None
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(rgb)
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Canvas region drawing & interaction
    # ------------------------------------------------------------------

    def _update_canvas_image(self, pil: Image.Image):
        """Store the source PIL image and render it at the current canvas size."""
        self._thumb_pil = pil
        self._redraw_canvas()

    def _redraw_canvas(self):
        """Re-render stored PIL image letterboxed into the canvas; update scrollregion and rect."""
        cw = self.thumb_canvas.winfo_width()
        ch = self.thumb_canvas.winfo_height()
        if self._thumb_pil is None or cw <= 1 or ch <= 1:
            return
        iw, ih = self._thumb_pil.size
        scale = min(cw / iw, ch / ih)
        dw, dh = int(iw * scale), int(ih * scale)
        ox, oy = (cw - dw) // 2, (ch - dh) // 2
        self._img_rect = (ox, oy, dw, dh)

        resized = self._thumb_pil.resize((dw, dh), Image.LANCZOS)
        photo = ImageTk.PhotoImage(resized)
        self._thumb_ref = photo
        if self._canvas_img_id is None:
            self._canvas_img_id = self.thumb_canvas.create_image(
                ox, oy, anchor=tk.NW, image=photo
            )
        else:
            self.thumb_canvas.itemconfig(self._canvas_img_id, image=photo)
            self.thumb_canvas.coords(self._canvas_img_id, ox, oy)
        self.thumb_canvas.configure(scrollregion=(0, 0, max(cw, ox + dw), max(ch, oy + dh)))
        self._draw_region_rect()

    def _on_canvas_resize(self, event):
        self._redraw_canvas()

    def _draw_region_rect(self):
        """Draw/update the chat region rectangle mapped through the letterboxed image rect."""
        if self._img_rect is None:
            return
        ox, oy, dw, dh = self._img_rect
        x1, y1, x2, y2 = self._chat_region
        cx1, cy1 = ox + int(x1 * dw), oy + int(y1 * dh)
        cx2, cy2 = ox + int(x2 * dw), oy + int(y2 * dh)
        if self._canvas_rect_id is None:
            self._canvas_rect_id = self.thumb_canvas.create_rectangle(
                cx1, cy1, cx2, cy2, outline="#00ff00", width=2, dash=(4, 2)
            )
        else:
            self.thumb_canvas.coords(self._canvas_rect_id, cx1, cy1, cx2, cy2)
        self.thumb_canvas.tag_raise(self._canvas_rect_id)

    def _on_region_press(self, event):
        cx = self.thumb_canvas.canvasx(event.x)
        cy = self.thumb_canvas.canvasy(event.y)
        self._drag_start = (cx, cy)
        if self._canvas_rect_id is None:
            self._canvas_rect_id = self.thumb_canvas.create_rectangle(
                cx, cy, cx, cy,
                outline="#00ff00", width=2, dash=(4, 2)
            )
        else:
            self.thumb_canvas.coords(
                self._canvas_rect_id, cx, cy, cx, cy
            )
        self.thumb_canvas.tag_raise(self._canvas_rect_id)

    def _on_region_drag(self, event):
        if self._drag_start is None or self._canvas_rect_id is None:
            return
        x0, y0 = self._drag_start
        cx = self.thumb_canvas.canvasx(event.x)
        cy = self.thumb_canvas.canvasy(event.y)
        self.thumb_canvas.coords(
            self._canvas_rect_id,
            min(x0, cx), min(y0, cy),
            max(x0, cx), max(y0, cy),
        )

    def _on_region_release(self, event):
        if self._drag_start is None:
            return
        x0, y0 = self._drag_start
        self._drag_start = None

        if self._img_rect is None:
            return
        ox, oy, dw, dh = self._img_rect
        cx = self.thumb_canvas.canvasx(event.x)
        cy = self.thumb_canvas.canvasy(event.y)
        x1 = max(0.0, min(1.0, (min(x0, cx) - ox) / dw))
        y1 = max(0.0, min(1.0, (min(y0, cy) - oy) / dh))
        x2 = max(0.0, min(1.0, (max(x0, cx) - ox) / dw))
        y2 = max(0.0, min(1.0, (max(y0, cy) - oy) / dh))

        # Ignore tiny drags (accidental clicks)
        if x2 - x1 < 0.02 or y2 - y1 < 0.02:
            self._draw_region_rect()  # restore previous rect
            return

        self._chat_region = [x1, y1, x2, y2]
        self._draw_region_rect()
        self._save_config()

    # ------------------------------------------------------------------
    # Pipeline execution
    # ------------------------------------------------------------------

    _FFMPEG_WARNING = (
        "You are enabling the Python/OpenCV clipper fallback.\n\n"
        "Compared to ffmpeg, this mode has significant drawbacks:\n"
        "  \u2022  Audio is stripped — the output video will be silent.\n"
        "  \u2022  Video is re-encoded — slower and may lose quality.\n\n"
        "It is strongly recommended to install ffmpeg instead:\n\n"
        "  Ubuntu / Debian:\n"
        "    sudo apt install ffmpeg\n\n"
        "  Windows:\n"
        "    1. Download from https://ffmpeg.org/download.html\n"
        "    2. Extract the archive and add the bin\\ folder to your PATH.\n\n"
        "Continue with the Python clipper anyway?"
    )

    def _on_run_without_ffmpeg_toggle(self):
        if self.run_without_ffmpeg_var.get():
            proceed = messagebox.askokcancel(
                "Warning: Python clipper has limitations",
                self._FFMPEG_WARNING,
                icon="warning",
            )
            if not proceed:
                self.run_without_ffmpeg_var.set(False)

    def _run(self):
        if not self.video_path:
            return

        t0_raw = self.t0_var.get().strip()
        if t0_raw in ("", "HH:MM:SS (optional)"):
            t0_raw = None

        chat_log_raw = self.chat_log_var.get().strip()
        chat_logs = [p for p in chat_log_raw.split(";") if p] if chat_log_raw else None

        if not chat_logs:
            _warning_file = _resource_path("no_log_warning.txt")
            with open(_warning_file) as _f:
                _warning_text = _f.read()
            proceed = messagebox.askokcancel("No logs provided", _warning_text)
            if not proceed:
                return

        chapters_dir = os.path.expanduser(self.default_chapters_dir_var.get().strip()) or None
        args = argparse.Namespace(
            video=self.video_path,
            output=os.path.expanduser(self.output_var.get()) or "out",
            chat_region=list(self._chat_region),
            verbose=bool(self.verbose_var.get()),
            chat_logs=chat_logs,
            t0=t0_raw,
            threads=self.threads_var.get(),
            ram_cap_gb=self.ram_cap_var.get(),
            run_without_ffmpeg=bool(self.run_without_ffmpeg_var.get()),
            force_ocr=bool(self.force_ocr_var.get()),
            chapters_dir=chapters_dir,
            youtube_upload=bool(self.youtube_upload_var.get()),
            youtube_title=self.youtube_title_var.get().strip(),
            tournament_match=bool(self.tournament_match_var.get()),
            detect_countdown=bool(self.detect_countdown_var.get()),
            buffer_start=self._safe_int(self.buffer_start_var, 0),
            buffer_end=self._safe_int(self.buffer_end_var, 0),
            tournament_buffer_start=self._safe_int(self.tournament_buffer_start_var, 0),
            tournament_buffer_end=self._safe_int(self.tournament_buffer_end_var, 10),
            extract_audio=bool(self.extract_audio_var.get()),
        )

        self._launch_worker(args)

    def _launch_worker(self, args):
        """Set up UI state and start the pipeline worker thread with the given args."""
        self._cancel_event.clear()
        self.run_btn.configure(state="disabled")
        self.cancel_btn.configure(state="normal")
        self.timer_label.configure(text="")
        self._start_timer()
        self._set_status("Running...")

        def _on_progress(current, total):
            pct = int(current / total * 100) if total > 0 else 0
            self.after(0, lambda p=pct: self._update_progress(p))

        def _on_status(msg: str):
            self.after(0, lambda m=msg: self._set_status(m))

        args.progress_callback = _on_progress
        args.status_callback = _on_status
        args.cancel_event = self._cancel_event

        debug_append = None
        if self.show_debug_popup_var.get():
            debug_append = self._show_debug_window()

        def worker():
            import io
            import queue

            _app = self
            # Coalesce writes instead of scheduling a Tk `after` per write —
            # OCR mode writes one progress line per video-second, so a long
            # run could queue thousands of individual Tk callbacks, each one
            # mutating the debug Text widget and flooding the event loop.
            _log_queue: "queue.SimpleQueue[str]" = queue.SimpleQueue()
            _drain_scheduled = [False]

            def _drain_log_queue(fn):
                chunks = []
                try:
                    while True:
                        chunks.append(_log_queue.get_nowait())
                except queue.Empty:
                    pass
                if chunks:
                    fn("".join(chunks))
                _drain_scheduled[0] = False

            class _TeeStream:
                """Writes to both the original stream and the debug window."""
                def __init__(self, original, gui_append):
                    self._orig = original
                    self._append = gui_append

                def write(self, data):
                    if self._orig is not None:
                        self._orig.write(data)
                    if data:
                        _log_queue.put(data)
                        if not _drain_scheduled[0]:
                            _drain_scheduled[0] = True
                            _app.after(100, lambda fn=self._append: _drain_log_queue(fn))

                def flush(self):
                    if self._orig is not None:
                        self._orig.flush()

            stderr_capture = io.StringIO()
            old_stderr = sys.stderr
            old_stdout = sys.stdout
            if debug_append is not None:
                sys.stderr = _TeeStream(stderr_capture, debug_append)
                sys.stdout = _TeeStream(old_stdout, debug_append)
            else:
                sys.stderr = stderr_capture
            try:
                chapters_text, youtube_url = pipeline.run(args)
                status_msg = f"Done! Output: {pipeline.output_video_path(args, args.output)}"
                if youtube_url:
                    status_msg += f"  |  YouTube: {youtube_url}"
                self.after(0, lambda m=status_msg: self._set_status(m))
                if chapters_text and self.show_chapters_popup_var.get():
                    self.after(0, lambda t=chapters_text, u=youtube_url: self._show_chapters(t, u))
                if self.close_on_complete_var.get():
                    self.after(0, self.destroy)
            except pipeline.CancelledError:
                self.after(0, lambda: self._set_status("Cancelled"))
            except pipeline.MissingDependencyError as exc:
                self.after(0, lambda e=exc, a=args: self._show_install_dialog(e.tool, a, debug_append=debug_append))
            except pipeline.UnfinalizedSourceError as exc:
                self.after(0, lambda e=exc, a=args: self._show_unfinalized_dialog(str(e), a))
            except SystemExit as e:
                captured = stderr_capture.getvalue().strip()
                msg = captured if captured else f"Pipeline stopped (exit {e.code})"
                self.after(0, lambda m=msg: self._set_status(f"Error: {m}"))
                self.after(0, lambda m=msg: messagebox.showerror("Error", m))
            except Exception as e:
                msg = str(e)
                self.after(0, lambda m=msg: self._set_status(f"Error: {m}"))
                self.after(0, lambda m=msg: messagebox.showerror("Error", m))
            finally:
                sys.stderr = old_stderr
                sys.stdout = old_stdout
                self.after(0, lambda: self.run_btn.configure(state="normal"))
                self.after(0, lambda: self.cancel_btn.configure(state="disabled"))
                self.after(0, lambda: self._finish_progress())
                self.after(0, self._stop_timer)

        threading.Thread(target=worker, daemon=True).start()

    def _cancel(self):
        self._cancel_event.set()
        self.cancel_btn.configure(state="disabled")
        self._set_status("Cancelling...")

    # ------------------------------------------------------------------
    # Reset
    # ------------------------------------------------------------------

    def _reset(self):
        self.video_path = None
        self._auto_video = True  # resume following the default video dir's latest recording
        self._thumb_ref = None
        self._thumb_pil = None
        self._img_rect = None
        self.thumb_canvas.delete("all")
        self._canvas_img_id = None
        self._canvas_rect_id = None
        self.info_label.configure(text="")
        default_out = os.path.expanduser(self.default_output_dir_var.get().strip())
        self.output_var.set(default_out if default_out else "out")
        self.t0_var.set("")
        self.chat_log_var.set("")
        self.youtube_title_var.set(cfg.default_youtube_title())
        self.verbose_var.set(False)
        self.preview_frame.pack_forget()
        self.run_btn.configure(state="disabled")
        self._finish_progress()
        self._stop_timer()
        self._timer_start = None
        self._timer_frozen = None
        self.timer_label.configure(text="")
        self._set_status("Ready")

    # ------------------------------------------------------------------
    # Help window
    # ------------------------------------------------------------------

    def _show_help(self):
        if getattr(sys, "frozen", False):
            readme_path = _resource_path("README.md")
        else:
            readme_path = _resource_path("../README.md")
        try:
            with open(readme_path, "r") as f:
                content = f.read()
        except OSError:
            content = "README.md not found."

        win = tk.Toplevel(self)
        win.title("Help")
        win.geometry("700x520")
        win.minsize(400, 300)

        style = ttk.Style()
        bg = style.lookup("TFrame", "background") or self.cget("bg")
        fg = style.lookup("TLabel", "foreground") or "black"
        code_bg = style.lookup("TEntry", "fieldbackground") or bg
        sel_bg = style.lookup("TEntry", "selectbackground") or "blue"
        sel_fg = style.lookup("TEntry", "selectforeground") or "white"

        font_name = style.lookup("TLabel", "font") or "TkDefaultFont"
        try:
            f = tkfont.Font(font=font_name)
            fam, fsz = f.actual("family"), f.actual("size")
        except Exception:
            fam, fsz = "TkDefaultFont", 10

        text = tk.Text(win, wrap=tk.WORD, padx=16, pady=12,
                       font=(fam, fsz), background=bg, foreground=fg,
                       relief=tk.FLAT, borderwidth=0, cursor="arrow",
                       selectbackground=sel_bg, selectforeground=sel_fg)
        scrollbar = ttk.Scrollbar(win, orient=tk.VERTICAL, command=text.yview)
        text.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        text.tag_configure("h1", font=(fam, fsz + 7, "bold"), spacing1=14, spacing3=4)
        text.tag_configure("h2", font=(fam, fsz + 3, "bold"), spacing1=10, spacing3=3)
        text.tag_configure("h3", font=(fam, fsz + 1, "bold"), spacing1=8, spacing3=2)
        text.tag_configure("code_block", font=("Courier", fsz - 1), background=code_bg,
                           lmargin1=20, lmargin2=20, spacing1=1, spacing3=1)
        text.tag_configure("inline_code", font=("Courier", fsz - 1), background=code_bg)
        text.tag_configure("bold", font=(fam, fsz, "bold"))
        text.tag_configure("bullet", lmargin1=16, lmargin2=28)
        text.tag_configure("th", font=(fam, fsz, "bold"))
        text.tag_configure("sep", foreground="#888888")

        _TABLE_SEP = re.compile(r'^\|[-| :]+\|$')
        in_code = False
        for line in content.splitlines():
            if line.startswith("```"):
                in_code = not in_code
                text.insert(tk.END, "\n")
                continue
            if in_code:
                text.insert(tk.END, line + "\n", "code_block")
                continue
            if line.startswith("### "):
                text.insert(tk.END, line[4:] + "\n", "h3")
            elif line.startswith("## "):
                text.insert(tk.END, line[3:] + "\n", "h2")
            elif line.startswith("# "):
                text.insert(tk.END, line[2:] + "\n", "h1")
            elif line.startswith("|"):
                if _TABLE_SEP.match(line):
                    text.insert(tk.END, "─" * 60 + "\n", "sep")
                else:
                    cells = [c.strip() for c in line.strip("|").split("|")]
                    for j, cell in enumerate(cells):
                        if j:
                            text.insert(tk.END, "  │  ", "sep")
                        self._md_inline(text, cell, "th" if j == 0 else None)
                    text.insert(tk.END, "\n")
            elif re.match(r"^[-*+] ", line):
                text.insert(tk.END, "• ", "bullet")
                self._md_inline(text, line[2:], "bullet")
                text.insert(tk.END, "\n")
            elif line.strip() == "":
                text.insert(tk.END, "\n")
            else:
                self._md_inline(text, line)
                text.insert(tk.END, "\n")

        text.configure(state="disabled")

    def _show_debug_window(self):
        """Open a live debug output window. Returns an append function to write text to it."""
        win = tk.Toplevel(self)
        win.title("Debug Output")
        win.resizable(True, True)
        win.minsize(500, 300)
        win.geometry("700x400")

        text_frame = ttk.Frame(win)
        text_frame.pack(fill=tk.BOTH, expand=True, padx=8, pady=(8, 0))

        scrollbar = ttk.Scrollbar(text_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        text = tk.Text(
            text_frame,
            wrap=tk.WORD,
            font=("TkFixedFont", 9),
            yscrollcommand=scrollbar.set,
            state="normal",
        )
        text.pack(fill=tk.BOTH, expand=True)
        scrollbar.configure(command=text.yview)

        btn_frame = ttk.Frame(win, padding=(8, 4, 8, 8))
        btn_frame.pack(fill=tk.X)
        ttk.Button(btn_frame, text="Clear", command=lambda: text.delete("1.0", tk.END)).pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Close", command=win.destroy).pack(side=tk.RIGHT)

        # Center on parent
        self.update_idletasks()
        px, py = self.winfo_rootx(), self.winfo_rooty()
        pw, ph = self.winfo_width(), self.winfo_height()
        win.update_idletasks()
        ww, wh = win.winfo_width(), win.winfo_height()
        win.geometry(f"+{px + (pw - ww) // 2}+{py + (ph - wh) // 2}")

        _MAX_LOG_LINES = 2000
        _progress_open = [False]
        # NB: "end" as an insert/delete *index* resolves to right before the
        # widget's mandatory trailing newline, one position earlier than the
        # literal index mark_set("m", tk.END) would freeze on an empty
        # widget. Anchor the mark on "end-1c" so it lines up with where
        # insert(tk.END, ...) actually writes.
        text.mark_set("progress_start", "end-1c")
        text.mark_gravity("progress_start", tk.LEFT)

        def append(data: str):
            # Progress bars write '\r...' repeatedly to redraw one line in a
            # terminal; without this, a long OCR run leaves thousands of
            # stale progress lines piled up in the Text widget instead of
            # one that updates in place.
            for i, seg in enumerate(data.split("\r")):
                if i > 0 and _progress_open[0]:
                    text.delete("progress_start", "end-1c")
                if seg:
                    if not _progress_open[0]:
                        text.mark_set("progress_start", "end-1c")
                    text.insert(tk.END, seg)
                    _progress_open[0] = not seg.endswith("\n")
                elif i > 0:
                    _progress_open[0] = False

            line_count = int(text.index("end-1c").split(".")[0])
            if line_count > _MAX_LOG_LINES:
                text.delete("1.0", f"{line_count - _MAX_LOG_LINES}.0")
            text.see(tk.END)

        return append

    def _show_chapters(self, chapters_text: str, youtube_url: "str | None" = None):
        """Open a window showing YouTube chapter timestamps with a copy button."""
        win = tk.Toplevel(self)
        win.title("YouTube Chapter Timestamps")
        win.resizable(True, True)
        win.minsize(300, 200)

        if youtube_url:
            url_frame = ttk.Frame(win, padding=(12, 10, 12, 0))
            url_frame.pack(fill=tk.X)

            def _open_url():
                import webbrowser
                webbrowser.open(youtube_url)

            def _copy_url():
                win.clipboard_clear()
                win.clipboard_append(youtube_url)

            ttk.Button(url_frame, text="Open in Browser", command=_open_url).pack(side=tk.LEFT)
            ttk.Button(url_frame, text="Copy URL", command=_copy_url).pack(side=tk.LEFT, padx=(4, 0))

        ttk.Label(
            win,
            text="Paste these into your YouTube video description:",
            padding=(12, 10, 12, 4),
        ).pack(anchor=tk.W)

        text_frame = ttk.Frame(win, padding=(12, 0, 12, 0))
        text_frame.pack(fill=tk.BOTH, expand=True)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        text_widget = tk.Text(
            text_frame,
            wrap=tk.NONE,
            font=("Courier", 11),
            height=min(20, chapters_text.count("\n") + 2),
            width=30,
        )
        text_widget.insert(tk.END, chapters_text)
        text_widget.configure(state="disabled")
        text_widget.grid(row=0, column=0, sticky=tk.NSEW)

        sb = ttk.Scrollbar(text_frame, orient=tk.VERTICAL, command=text_widget.yview)
        text_widget.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=1, sticky=tk.NS)

        btn_frame = ttk.Frame(win, padding=(12, 8))
        btn_frame.pack(fill=tk.X)

        def _copy():
            win.clipboard_clear()
            win.clipboard_append(chapters_text)
            copy_btn.configure(text="Copied!")
            win.after(1500, lambda: copy_btn.configure(text="Copy to Clipboard"))

        copy_btn = ttk.Button(btn_frame, text="Copy to Clipboard", command=_copy)
        copy_btn.pack(side=tk.LEFT)
        ttk.Button(btn_frame, text="Close", command=win.destroy).pack(side=tk.RIGHT)

        win.update_idletasks()
        w = win.winfo_reqwidth() + 20
        h = win.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")

    def _md_inline(self, widget, text, base_tag=None):
        """Insert text with inline `code` and **bold** formatting applied."""
        for part in re.split(r"(`[^`]+`|\*\*[^*]+\*\*)", text):
            if part.startswith("`") and part.endswith("`"):
                tags = ("inline_code", base_tag) if base_tag else ("inline_code",)
                widget.insert(tk.END, part[1:-1], tags)
            elif part.startswith("**") and part.endswith("**"):
                tags = ("bold", base_tag) if base_tag else ("bold",)
                widget.insert(tk.END, part[2:-2], tags)
            else:
                widget.insert(tk.END, part, (base_tag,) if base_tag else ())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _on_window_configure(self, event):
        if event.widget is not self:
            return
        if self._geom_save_after is not None:
            self.after_cancel(self._geom_save_after)
        self._geom_save_after = self.after(500, self._save_config)

    @staticmethod
    def _safe_int(var: "tk.IntVar", default: int = 0) -> int:
        """var.get() but tolerant of a mid-edit Spinbox value like '-' or ''."""
        try:
            return var.get()
        except tk.TclError:
            return default

    def _save_config(self, *_):
        self._geom_save_after = None
        cfg.save_config({
            "video_dir": self.default_video_dir_var.get(),
            "log_dir": self.default_log_dir_var.get(),
            "output_dir": self.default_output_dir_var.get(),
            "chapters_dir": self.default_chapters_dir_var.get(),
            "show_chapters_popup": bool(self.show_chapters_popup_var.get()),
            "close_on_complete": bool(self.close_on_complete_var.get()),
            "show_debug_popup": bool(self.show_debug_popup_var.get()),
            "chat_region": self._chat_region,
            "threads": self.threads_var.get(),
            "ram_cap_gb": self.ram_cap_var.get(),
            "youtube_upload": bool(self.youtube_upload_var.get()),
            "tournament_match": bool(self.tournament_match_var.get()),
            "detect_countdown": bool(self.detect_countdown_var.get()),
            "buffer_start": self._safe_int(self.buffer_start_var, 0),
            "buffer_end": self._safe_int(self.buffer_end_var, 0),
            "tournament_buffer_start": self._safe_int(self.tournament_buffer_start_var, 0),
            "tournament_buffer_end": self._safe_int(self.tournament_buffer_end_var, 10),
            "extract_audio": bool(self.extract_audio_var.get()),
            "window_geometry": self.winfo_geometry(),
        })

    @staticmethod
    def _find_latest_video(video_dir: str) -> tuple[str | None, str | None]:
        """
        Return (path, skipped) for the most recently modified *finished*
        video file in video_dir.

        A candidate is treated as still being written by sampling its size
        (see frame_extractor.is_growing), not by mtime age: a recorder can
        keep flushing/finalizing a large file — advancing its mtime — for
        well over a minute after the user stops recording, so a fresh mtime
        alone doesn't mean the file is still in progress. Falls back to the
        newest candidate if every candidate looks like it's still growing.

        skipped is the basename of the newest candidate, when it was passed
        over for looking still-in-progress; otherwise None.
        """
        candidates = [
            os.path.join(video_dir, f)
            for f in os.listdir(video_dir)
            if os.path.splitext(f)[1].lower() in VALID_EXTENSIONS
            and os.path.isfile(os.path.join(video_dir, f))
        ]
        if not candidates:
            return None, None
        candidates.sort(key=os.path.getmtime, reverse=True)
        for path in candidates:
            if not is_growing(path):
                skipped = os.path.basename(candidates[0]) if path != candidates[0] else None
                return path, skipped
        return candidates[0], None

    def _scan_latest_video_async(self, video_dir: str):
        """
        Run _find_latest_video off the main thread and, if it finds something,
        load it as an auto-selection back on the Tk thread.

        The scan samples file size (see is_growing) to decide whether the
        newest file is still being recorded, which can block for up to
        ~1.5s per busy candidate — doing that on the main thread would delay
        showing the window or freeze the UI mid-poll.
        """
        def worker():
            try:
                path, skipped = self._find_latest_video(video_dir)
            except OSError:
                return
            # Skip a reload when the scan just confirms the current selection —
            # matters for the periodic poll, which would otherwise re-extract
            # the thumbnail and re-match the chat log every tick for no reason.
            if path and path != self.video_path and self.winfo_exists():
                self.after(0, lambda: self._load_video(path, auto=True, skipped=skipped))

        threading.Thread(target=worker, daemon=True).start()

    def _schedule_video_poll(self):
        self._video_poll_after_id = self.after(VIDEO_POLL_INTERVAL_MS, self._poll_latest_video)

    def _poll_latest_video(self):
        """
        Periodically re-scan the default video dir for a newer recording.

        Stops rescanning once the user has explicitly picked a video
        (Browse/drag-drop) or a job is running — see the `auto` flag threaded
        through _load_video. Reset (`_reset`) re-arms it rather than stopping
        it, since Reset is "go back to following the default dir," not "stop
        following it."
        """
        self._video_poll_after_id = None
        # A pending `after` callback can still fire after close_on_complete
        # destroys the window (self.after(0, self.destroy) in the run-worker
        # completion handler) — bail out before touching any widget.
        if not self.winfo_exists():
            return
        try:
            if not self._auto_video or str(self.cancel_btn.cget("state")) != "disabled":
                # cancel_btn is only enabled while a pipeline job is running.
                return
            video_dir = os.path.expanduser(self.default_video_dir_var.get().strip())
            if not video_dir or not os.path.isdir(video_dir):
                return
            self._scan_latest_video_async(video_dir)
        finally:
            if self.winfo_exists():
                self._schedule_video_poll()

    @staticmethod
    def _find_closest_log(video_path: str, log_dir: str) -> str | None:
        """Return the path of the 'Local' log file whose mtime is closest to the video's mtime."""
        video_mtime = os.path.getmtime(video_path)
        candidates = [
            os.path.join(log_dir, f)
            for f in os.listdir(log_dir)
            if "Local" in f and os.path.isfile(os.path.join(log_dir, f))
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda p: abs(os.path.getmtime(p) - video_mtime))

    def _redraw_progress(self):
        w = self.progress_canvas.winfo_width()
        h = self.progress_canvas.winfo_height()
        fill_w = int(w * self._progress_pct / 100)
        self.progress_canvas.coords(self._prog_fill, 0, 0, fill_w, h)
        self.progress_canvas.coords(self._prog_text, w // 2, h // 2)
        text_color = "white" if self._progress_running else self._progress_fg_idle
        self.progress_canvas.itemconfigure(self._prog_text, text=self._progress_msg, fill=text_color)

    def _update_progress(self, pct: int):
        self._progress_pct = pct
        self._progress_running = True
        self._redraw_progress()

    def _finish_progress(self):
        self._progress_pct = 0
        self._progress_running = False
        self._redraw_progress()

    def _set_status(self, msg: str):
        self._progress_msg = msg
        self._redraw_progress()

    # ------------------------------------------------------------------
    # Execution timer
    # ------------------------------------------------------------------

    def _start_timer(self):
        if self._timer_after_id is not None:
            self.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        self._timer_start = time.monotonic()
        self._timer_frozen = None
        self._tick_timer()

    def _stop_timer(self):
        if self._timer_after_id is not None:
            self.after_cancel(self._timer_after_id)
            self._timer_after_id = None
        if self._timer_start is not None:
            self._timer_frozen = time.monotonic() - self._timer_start
        self._render_timer()

    def _tick_timer(self):
        self._render_timer()
        self._timer_after_id = self.after(500, self._tick_timer)

    def _render_timer(self):
        if self._timer_start is None:
            return
        elapsed = self._timer_frozen if self._timer_frozen is not None else (
            time.monotonic() - self._timer_start)
        m, s = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        if h:
            text = f"{h}:{m:02}:{s:02}"
        else:
            text = f"{m}:{s:02}"
        self.timer_label.configure(text=text)

    # ------------------------------------------------------------------
    # Dependency install dialog
    # ------------------------------------------------------------------

    _WINGET_IDS = {
        "tesseract": "UB-Mannheim.TesseractOCR",
        "ffmpeg": "Gyan.FFmpeg",
    }

    _MANUAL_URLS = {
        "tesseract": "https://github.com/UB-Mannheim/tesseract/wiki",
        "ffmpeg": "https://ffmpeg.org/download.html",
    }

    def _show_unfinalized_dialog(self, warning: str, args):
        """
        Ask whether to proceed after check_source_video() flagged the source
        as still-being-written or missing a valid container index. Proceeding
        re-runs the pipeline with force_unfinalized set so the check is
        skipped the second time.
        """
        self._set_status("Source video may still be recording")
        proceed = messagebox.askokcancel(
            "Recording may still be in progress",
            warning + "\n\nProceed anyway? This may be very slow.",
            icon="warning",
        )
        if proceed:
            args.force_unfinalized = True
            self._launch_worker(args)
        else:
            self._set_status("Cancelled")

    def _show_install_dialog(self, tool: str, args=None, debug_append=None):
        """Show a dialog offering to install a missing dependency via winget."""
        import platform
        import subprocess

        win = tk.Toplevel(self)
        win.title(f"Missing dependency: {tool}")
        win.resizable(False, False)
        win.grab_set()

        pad = {"padx": 16, "pady": 6}

        ttk.Label(
            win,
            text=f"'{tool}' is required but was not found.",
            font=("TkDefaultFont", 11, "bold"),
        ).pack(anchor=tk.W, padx=16, pady=(14, 0))

        is_windows = platform.system() == "Windows"
        is_mac = platform.system() == "Darwin"
        if is_windows:
            ttk.Label(
                win,
                text="Install it automatically via winget, or visit the download page.",
                wraplength=380,
            ).pack(anchor=tk.W, **pad)
        else:
            if is_mac:
                install_cmd = f"brew install {'tesseract' if tool == 'tesseract' else tool}"
            else:
                pkg = "tesseract-ocr" if tool == "tesseract" else tool
                install_cmd = f"sudo apt install {pkg}"
            ttk.Label(
                win,
                text=f"Install with:\n    {install_cmd}",
                wraplength=380,
                justify=tk.LEFT,
            ).pack(anchor=tk.W, **pad)

        # Log area (hidden until install starts)
        log_frame = ttk.Frame(win)
        log_text = tk.Text(log_frame, height=10, width=52, wrap=tk.WORD,
                           font=("Courier", 9), state="disabled")
        log_sb = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=log_text.yview)
        log_text.configure(yscrollcommand=log_sb.set)
        log_sb.pack(side=tk.RIGHT, fill=tk.Y)
        log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Progress bar (hidden until install starts)
        progress_bar = ttk.Progressbar(win, mode="indeterminate", length=380)

        def _append_log(text: str):
            log_text.configure(state="normal")
            log_text.insert(tk.END, text)
            log_text.see(tk.END)
            log_text.configure(state="disabled")

        btn_frame = ttk.Frame(win)
        btn_frame.pack(fill=tk.X, padx=16, pady=(4, 14))

        def _open_browser():
            import webbrowser
            webbrowser.open(self._MANUAL_URLS.get(tool, ""))

        def _do_install():
            install_btn.configure(state="disabled")
            cancel_btn.configure(state="disabled")
            continue_btn.configure(state="disabled")
            log_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 4))
            progress_bar.pack(fill=tk.X, padx=16, pady=(0, 8))
            progress_bar.start(12)
            win.update_idletasks()

            def _install_thread():
                winget_id = self._WINGET_IDS.get(tool, tool)
                cmd = [
                    "winget", "install",
                    "--id", winget_id,
                    "-e",
                    "--accept-source-agreements",
                    "--accept-package-agreements",
                ]
                win.after(0, lambda: _append_log(f"$ {' '.join(cmd)}\n\n"))
                if debug_append is not None:
                    win.after(0, lambda: debug_append(f"[install] $ {' '.join(cmd)}\n\n"))
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                    )
                    for line in proc.stdout:
                        win.after(0, lambda l=line: _append_log(l))
                        if debug_append is not None:
                            win.after(0, lambda l=line: debug_append(l))
                    proc.wait()
                    success = proc.returncode == 0
                except FileNotFoundError:
                    msg = "\nwinget not found. Please install manually.\n"
                    win.after(0, lambda: _append_log(msg))
                    if debug_append is not None:
                        win.after(0, lambda: debug_append(f"[install] {msg}"))
                    success = False

                def _after_install():
                    progress_bar.stop()
                    if success:
                        # Probe for the newly installed binary and update PATH / pytesseract
                        found_dir = pipeline._find_windows_tool(tool)
                        if found_dir:
                            import os as _os
                            _os.environ["PATH"] = found_dir + _os.pathsep + _os.environ.get("PATH", "")
                            if tool == "tesseract":
                                try:
                                    import pytesseract
                                    pytesseract.pytesseract.tesseract_cmd = _os.path.join(
                                        found_dir, "tesseract.exe"
                                    )
                                except ImportError:
                                    pass
                        progress_bar.configure(mode="determinate", value=100)
                        _append_log("\nInstallation complete!\n")
                        if args is not None:
                            continue_btn.configure(state="normal")
                            _append_log("Click 'Continue' to resume the pipeline.\n")
                        else:
                            _append_log("Click 'Run' to start the pipeline.\n")
                        close_btn.configure(state="normal")
                    else:
                        progress_bar.pack_forget()
                        _append_log(
                            f"\nInstallation failed or winget not available.\n"
                            f"Install manually: {self._MANUAL_URLS.get(tool, '')}\n"
                        )
                        manual_btn.configure(state="normal")
                        close_btn.configure(state="normal")
                        continue_btn.configure(state="normal")

                win.after(0, _after_install)

            threading.Thread(target=_install_thread, daemon=True).start()

        def _continue():
            win.destroy()
            self._launch_worker(args)

        def _run_without_ffmpeg():
            if not messagebox.askokcancel(
                "Warning: Python clipper has limitations",
                self._FFMPEG_WARNING,
                icon="warning",
            ):
                return
            args.run_without_ffmpeg = True
            win.destroy()
            self._launch_worker(args)

        install_btn = ttk.Button(btn_frame, text="Install via winget", command=_do_install)
        manual_btn = ttk.Button(btn_frame, text="Download page", command=_open_browser)
        continue_btn = ttk.Button(btn_frame, text="Continue", command=_continue)
        alt_pipeline_btn = ttk.Button(
            btn_frame,
            text="Run without ffmpeg",
            command=_run_without_ffmpeg,
        )
        close_btn = ttk.Button(btn_frame, text="Close", command=win.destroy)
        cancel_btn = close_btn  # alias used inside _do_install

        if is_windows:
            install_btn.pack(side=tk.LEFT)
            manual_btn.pack(side=tk.LEFT, padx=(6, 0))
        else:
            manual_btn.pack(side=tk.LEFT)
        if args is not None and tool == "ffmpeg":
            alt_pipeline_btn.pack(side=tk.LEFT, padx=(6, 0))
        if args is not None:
            continue_btn.pack(side=tk.RIGHT, padx=(0, 6))
        close_btn.pack(side=tk.RIGHT)

        win.update_idletasks()
        w = win.winfo_reqwidth() + 20
        h = win.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = int(seconds)
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"


if __name__ == "__main__":
    App().mainloop()
