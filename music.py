"""
Built-in music player for NotchIsland: YouTube Music without a browser window.

- MusicEngine: search (ytmusicapi), audio download to a local cache (yt-dlp),
  playback (Qt Multimedia), queue + automatic radio when the queue runs out.
  YouTube / YouTube Music links and Spotify links (track, album, playlist) are accepted:
  Spotify tracks are matched to the same song on YouTube Music.
- MusicPanel: the search window (Ctrl+Alt+M, or the magnifier in the notch).
- Hotkeys: Ctrl+Alt+M (or the first free fallback), plus the keyboard media keys while the built-in player is active.
"""

import os
import re
import json
import time
import shutil
import ctypes
import threading
from ctypes import wintypes
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs

from PySide6.QtCore import (
    Qt, QObject, Signal, QTimer, QUrl, QEvent, QRectF, QSize, QAbstractNativeEventFilter,
)
from PySide6.QtGui import QGuiApplication, QPixmap, QPainter, QPainterPath, QColor, QImage, QFont
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QLabel,
)

CACHE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "NotchIsland", "cache")
CACHE_KEEP = 40          # audio files kept in the cache
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")

SPOTIFY_RE = re.compile(r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?|spotify:)(track|album|playlist)[/:]([A-Za-z0-9]+)")
YOUTUBE_RE = re.compile(r"(?:youtube\.com|youtu\.be)/", re.I)


# --------------------------------------------------------------------------- #
#  Helpers
# --------------------------------------------------------------------------- #
class _QuietLog:
    """yt-dlp logger: the .exe has no console, so keep yt-dlp away from stdout/stderr."""

    def debug(self, msg):
        pass

    info = warning = debug

    def error(self, msg):
        print("yt-dlp:", msg)


def _seconds(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str) and v:
        n = 0
        for part in v.split(":"):
            n = n * 60 + int(part)
        return float(n)
    return 0.0


def _hi_res(url):
    # YouTube Music covers: ask for a bigger size than the 60-120 px given by the API
    return re.sub(r"=w\d+-h\d+[^/]*$", "=w544-h544-l90-rj", url) if url else url


def track_from(item):
    """ytmusicapi item (search result, playlist or 'up next' entry) -> track dict."""
    vid = item.get("videoId")
    if not vid:
        return None
    artists = ", ".join(a["name"] for a in (item.get("artists") or []) if a.get("name"))
    thumbs = item.get("thumbnails") or item.get("thumbnail") or []
    try:
        dur = _seconds(item.get("duration_seconds") or item.get("length") or item.get("duration"))
    except ValueError:
        dur = 0.0
    return {
        "videoId": vid,
        "title": item.get("title") or "",
        "artist": artists,
        "duration": dur,
        "thumb_url": _hi_res(thumbs[-1]["url"]) if thumbs else None,
    }


def fmt_dur(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}" if s else ""


def force_foreground(hwnd):
    """Bring our window to the front even if another app has the focus."""
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    fg = u.GetForegroundWindow()
    if fg == hwnd:
        return
    other = u.GetWindowThreadProcessId(fg, None)
    me = k.GetCurrentThreadId()
    u.AttachThreadInput(me, other, True)
    u.BringWindowToTop(hwnd)
    u.SetForegroundWindow(hwnd)
    u.AttachThreadInput(me, other, False)


# --------------------------------------------------------------------------- #
#  Engine
# --------------------------------------------------------------------------- #
class MusicEngine(QObject):
    changed = Signal(object)         # same dict format as MediaWorker.changed
    results = Signal(object)         # (token, {"tracks": [...], "title": str, "link": bool} | error str)
    active_changed = Signal(bool)
    _call = Signal(object)           # run a callable on the GUI thread

    def __init__(self):
        super().__init__()
        self._call.connect(lambda f: f())
        self.pool = ThreadPoolExecutor(4, thread_name_prefix="music")
        self.dl_pool = ThreadPoolExecutor(2, thread_name_prefix="music-dl")
        self._ytm = None
        self._ytm_lock = threading.Lock()
        self._http = None
        self._files = {}             # videoId -> Future[path]
        self._files_lock = threading.Lock()

        self.queue = []
        self.index = -1
        self.want_play = False
        self.loading = False
        self.thumb = None
        self._thumb_for = None
        self.fails = 0
        self._radio_busy = False
        self._radio_then_next = False

        self.player = QMediaPlayer(self)
        self.out = QAudioOutput(self)
        self.player.setAudioOutput(self.out)
        self.player.mediaStatusChanged.connect(self._on_status)
        self.player.playbackStateChanged.connect(lambda *_: self._emit())
        self.player.errorOccurred.connect(self._on_error)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._emit)
        self.timer.start(1000)

    # --- background work ------------------------------------------------ #
    def _bg(self, fn, done=None):
        """Run fn on a worker thread, then done(result, error) on the GUI thread."""
        def run():
            try:
                r, err = fn(), None
            except Exception as e:
                r, err = None, e
            if done:
                self._call.emit(lambda: done(r, err))
        return self.pool.submit(run)

    def ytm(self):
        with self._ytm_lock:
            if self._ytm is None:
                from ytmusicapi import YTMusic
                self._ytm = YTMusic()
            return self._ytm

    def http(self):
        if self._http is None:
            import requests
            self._http = requests.Session()
            self._http.headers["User-Agent"] = UA
        return self._http

    def fetch_bytes(self, url, done):
        self._bg(lambda: self.http().get(url, timeout=10).content, lambda data, err: done(data))

    # --- search / links ---------------------------------------------------- #
    def search(self, text, token):
        self._bg(lambda: self._lookup(text.strip()),
                 lambda r, err: self.results.emit((token, r if err is None else f"Error: {err}")))

    def _lookup(self, text):
        m = SPOTIFY_RE.search(text)
        if m:
            r = self._spotify(*m.groups())
        elif YOUTUBE_RE.search(text):
            r = self._youtube(text)
        else:
            items = self.ytm().search(text, filter="songs", limit=15)
            return {"title": None, "link": False, "tracks": [t for t in map(track_from, items) if t]}
        r["link"] = True
        return r

    def _youtube(self, url):
        u = urlparse(url if "://" in url else "https://" + url)
        q = parse_qs(u.query)
        vid = (q.get("v") or [None])[0]
        if not vid and "youtu.be" in u.netloc:
            vid = u.path.strip("/") or None
        lst = (q.get("list") or [None])[0]
        if lst and not vid and not lst.startswith("RD"):
            pl = self.ytm().get_playlist(lst, limit=300)
            return {"title": pl.get("title"), "tracks": [t for t in map(track_from, pl.get("tracks") or []) if t]}
        if not vid and not lst:
            raise RuntimeError("unrecognized YouTube link")
        w = self.ytm().get_watch_playlist(videoId=vid, playlistId=lst, limit=50)
        return {"title": w.get("playlist_title") if lst else None,
                "tracks": [t for t in map(track_from, w.get("tracks") or []) if t]}

    def _spotify(self, kind, sid):
        html = self.http().get(f"https://open.spotify.com/embed/{kind}/{sid}", timeout=10).text
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        if not m:
            raise RuntimeError("could not read this Spotify link")
        ent = json.loads(m.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
        if kind == "track":
            rows = [(ent.get("name") or ent.get("title"),
                     ", ".join(a["name"] for a in ent.get("artists") or []), ent.get("duration"))]
        else:
            rows = [(t.get("title"), t.get("subtitle"), t.get("duration")) for t in ent.get("trackList") or []]
        tracks = []
        for title, artist, dur in rows:
            if not title:
                continue
            artist = re.sub(r"\s+", " ", (artist or "").replace("\xa0", " ")).strip()
            tracks.append({"videoId": None, "query": f"{title} {artist}", "title": title, "artist": artist,
                           "duration": (dur or 0) / 1000, "thumb_url": None})
        return {"title": (ent.get("name") or ent.get("title")) if kind != "track" else None, "tracks": tracks}

    # --- audio files (worker threads) ------------------------------------ #
    def _resolve(self, t):
        """Spotify track -> same song on YouTube Music."""
        items = self.ytm().search(t["query"], filter="songs", limit=3)
        best = next((x for x in map(track_from, items) if x), None)
        if not best:
            raise RuntimeError("not found on YouTube Music")
        t["thumb_url"] = t.get("thumb_url") or best["thumb_url"]
        t["duration"] = t.get("duration") or best["duration"]
        t["videoId"] = best["videoId"]

    def _file(self, t):
        if not t.get("videoId"):
            self._resolve(t)
        vid = t["videoId"]
        with self._files_lock:
            fut = self._files.get(vid)
            if fut is None:
                fut = self._files[vid] = self.dl_pool.submit(self._download, vid)
        try:
            return fut.result()
        except Exception:
            with self._files_lock:
                self._files.pop(vid, None)   # allow a retry later
            raise

    def _download(self, vid):
        os.makedirs(CACHE_DIR, exist_ok=True)
        for f in os.listdir(CACHE_DIR):
            if f.startswith(vid + ".") and not f.endswith((".part", ".ytdl")):
                path = os.path.join(CACHE_DIR, f)
                os.utime(path)
                return path
        import yt_dlp
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio",
            "outtmpl": os.path.join(CACHE_DIR, "%(id)s.%(ext)s"),
            "quiet": True, "no_warnings": True, "noprogress": True, "logger": _QuietLog(),
            "socket_timeout": 15, "retries": 3,
        }
        if shutil.which("deno") is None and shutil.which("node"):
            opts["js_runtimes"] = {"node": {}}
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(f"https://music.youtube.com/watch?v={vid}", download=True)
            path = y.prepare_filename(info)
        self._prune()
        return path

    def _prune(self):
        try:
            files = [os.path.join(CACHE_DIR, f) for f in os.listdir(CACHE_DIR)]
            files.sort(key=os.path.getmtime, reverse=True)
            for f in files[CACHE_KEEP:]:
                try:
                    os.remove(f)
                except OSError:
                    pass       # file being played
        except OSError:
            pass

    # --- queue ------------------------------------------------------------ #
    def active(self):
        return self.index >= 0

    def current(self):
        return self.queue[self.index] if self.active() else None

    def play(self, tracks, start=0):
        self.queue = [dict(t) for t in tracks]
        self._start(start)

    def enqueue(self, track):
        """Play right after the current track."""
        if not self.active():
            return self.play([track])
        self.queue.insert(self.index + 1, dict(track))
        if not self.loading:
            self._prefetch()

    def jump(self, i):
        if 0 <= i < len(self.queue):
            self._start(i)

    def _start(self, i):
        was_active = self.active()
        self.index = i
        self.want_play = True
        self.loading = True
        self.player.stop()
        t = self.queue[i]
        self.thumb, self._thumb_for = None, None
        self._emit(thumb=True)
        if not was_active:
            self.active_changed.emit(True)
        if t.get("thumb_url"):
            self._load_thumb(t)
        self._bg(lambda: self._file(t), lambda path, err: self._loaded(t, path, err))

    def _loaded(self, t, path, err):
        if self.current() is not t:
            return             # the user moved on meanwhile
        if self._thumb_for is not t:
            self._load_thumb(t)
        if err is not None:
            print("music load error:", err)
            self.fails += 1
            if self.fails < 3 and self.index + 1 < len(self.queue):
                self._start(self.index + 1)
            else:
                self.fails = 0
                self.loading = self.want_play = False
                self._emit()
            return
        self.fails = 0
        self.loading = False
        self.player.setSource(QUrl.fromLocalFile(path))
        if self.want_play:
            self.player.play()
        self._emit()
        self._prefetch()

    def _load_thumb(self, t):
        self._thumb_for = t
        if t.get("thumb_url"):
            self.fetch_bytes(t["thumb_url"], lambda data: self._set_thumb(t, data))

    def _set_thumb(self, t, data):
        if self.current() is t and data:
            self.thumb = data
            self._emit(thumb=True)

    def _prefetch(self):
        """Download the next track in advance so the transition is instant."""
        i = self.index + 1
        if i >= len(self.queue):
            self._extend_radio()
            return
        t = self.queue[i]
        self._bg(lambda: self._file(t))

    def _extend_radio(self, then_next=False):
        """Queue finished: continue with YouTube Music's radio of the last tracks."""
        self._radio_then_next |= then_next
        if self._radio_busy:
            return
        seed = next((t["videoId"] for t in reversed(self.queue) if t.get("videoId")), None)
        if not seed:
            return
        self._radio_busy = True
        known = {t.get("videoId") for t in self.queue}

        def work():
            w = self.ytm().get_watch_playlist(videoId=seed, limit=40)
            return [t for t in map(track_from, w.get("tracks") or []) if t and t["videoId"] not in known]

        def done(tracks, err):
            self._radio_busy = False
            go, self._radio_then_next = self._radio_then_next, False
            if not self.active():
                return
            if tracks:
                self.queue.extend(tracks)
            if go and self.index + 1 < len(self.queue):
                self._start(self.index + 1)
            elif go:
                self.want_play = False
                self._emit()
            elif tracks and not self.loading:
                self._prefetch()

        self._bg(work, done)

    # --- controls (same API as MediaWorker.command) --------------------- #
    def command(self, name, arg=None):
        if not self.active():
            return
        if name == "toggle":
            self.toggle()
        elif name == "next":
            self.next()
        elif name == "prev":
            self.prev()
        elif name == "seek" and not self.loading:
            self.player.setPosition(int(arg * 1000))
            self._emit()
        elif name == "stop":
            self.stop()

    def toggle(self):
        self.want_play = not self.want_play
        if not self.loading:
            self.player.play() if self.want_play else self.player.pause()
        self._emit()

    def next(self):
        if self.index + 1 < len(self.queue):
            self._start(self.index + 1)
        else:
            self._extend_radio(then_next=True)

    def prev(self):
        if self.index > 0 and (self.loading or self.player.position() < 3000):
            self._start(self.index - 1)
        elif not self.loading:
            self.player.setPosition(0)
            self._emit()

    def stop(self):
        if not self.active():
            return
        self.player.stop()
        self.player.setSource(QUrl())
        self.queue, self.index = [], -1
        self.want_play = self.loading = False
        self._emit()
        self.active_changed.emit(False)

    def shutdown(self):
        self.player.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.dl_pool.shutdown(wait=False, cancel_futures=True)

    # --- player events ----------------------------------------------------- #
    def _on_status(self, status):
        if status == QMediaPlayer.EndOfMedia and self.active() and not self.loading:
            self.next()

    def _on_error(self, error, text):
        print("music player error:", error, text)
        if self.active() and not self.loading:
            self.next()

    def _emit(self, thumb=False):
        t = self.current()
        if t is None:
            self.changed.emit({"has_media": False, "key": None})
            return
        loaded = not self.loading and self.player.duration() > 0
        d = {
            "has_media": True,
            "key": ("builtin", self.index, t.get("videoId") or t.get("query")),
            "title": t["title"],
            "artist": t["artist"],
            "app": "Loading…" if self.loading else "YouTube Music",
            "playing": self.want_play,
            "position": self.player.position() / 1000 if loaded else 0.0,
            "updated": time.time(),
            "duration": self.player.duration() / 1000 if loaded else 0.0,   # 0 hides the progress bar
            "can_prev": True,
            "can_next": True,
            "can_seek": loaded,
        }
        if thumb:
            d["thumb"] = self.thumb
        self.changed.emit(d)


# --------------------------------------------------------------------------- #
#  Global hotkeys (search, media keys)
# --------------------------------------------------------------------------- #
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
HK_SEARCH = 1
# the first combination not already taken by another app is used
SEARCH_KEYS = [("Ctrl+Alt+M", "M"), ("Ctrl+Alt+Y", "Y"), ("Ctrl+Alt+P", "P")]
MEDIA_KEYS = {2: (0xB3, "toggle"), 3: (0xB0, "next"), 4: (0xB1, "prev"), 5: (0xB2, "stop")}


class Hotkeys(QAbstractNativeEventFilter):
    def __init__(self, hwnd, engine, on_search):
        super().__init__()
        self.hwnd, self.engine, self.on_search = hwnd, engine, on_search
        self.media_on = False
        self.search_label = None
        for label, ch in SEARCH_KEYS:
            if ctypes.windll.user32.RegisterHotKey(hwnd, HK_SEARCH, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord(ch)):
                self.search_label = label
                break
        engine.active_changed.connect(self.set_media)

    def set_media(self, on):
        """While the built-in player is active, the media keys control it first."""
        if on == self.media_on:
            return
        self.media_on = on
        u = ctypes.windll.user32
        for hk, (vk, _) in MEDIA_KEYS.items():
            if on:
                u.RegisterHotKey(self.hwnd, hk, MOD_NOREPEAT, vk)
            else:
                u.UnregisterHotKey(self.hwnd, hk)

    def nativeEventFilter(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == WM_HOTKEY and msg.hWnd == self.hwnd:
            if msg.wParam == HK_SEARCH:
                self.on_search()
            elif msg.wParam in MEDIA_KEYS:
                self.engine.command(MEDIA_KEYS[msg.wParam][1])
            return True, 0
        return False, 0


# --------------------------------------------------------------------------- #
#  Search panel
# --------------------------------------------------------------------------- #
PANEL_QSS = """
#frame { background: #000; border-radius: 22px; }
QLineEdit {
    background: #1c1c1e; color: #fff; border: none; border-radius: 12px;
    padding: 9px 12px; font-size: 14px; selection-background-color: #3a3a3c;
}
QListWidget { background: transparent; border: none; outline: none; }
QListWidget::item { border-radius: 10px; padding: 0; }
QListWidget::item:selected, QListWidget::item:hover { background: #1c1c1e; }
QLabel { color: #fff; background: transparent; }
#sub, #hint { color: rgba(255,255,255,0.5); }
#head { color: rgba(255,255,255,0.55); font-weight: 600; }
QScrollBar:vertical { background: transparent; width: 6px; }
QScrollBar::handle:vertical { background: #3a3a3c; border-radius: 3px; min-height: 30px; }
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page { background: none; height: 0; }
"""
ROW_H = 52
MAX_ROWS = 7


def rounded(img: QImage, size, radius):
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, size, size), radius, radius)
    p.setClipPath(path)
    if img is None or img.isNull():
        p.fillRect(pm.rect(), QColor(44, 44, 48))
        p.setPen(QColor(255, 255, 255, 150))
        p.drawText(pm.rect(), Qt.AlignCenter, "♪")
    else:
        side = min(img.width(), img.height())
        img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
        p.drawImage(QRectF(0, 0, size, size), img)
    p.end()
    return pm


class Row(QWidget):
    def __init__(self, title, sub):
        super().__init__()
        h = QHBoxLayout(self)
        h.setContentsMargins(8, 6, 8, 6)
        h.setSpacing(12)
        self.art = QLabel()
        self.art.setFixedSize(40, 40)
        self.art.setPixmap(rounded(None, 40, 8))
        h.addWidget(self.art)
        v = QVBoxLayout()
        v.setSpacing(1)
        self.title = QLabel(title)
        self.title.setStyleSheet("font-size: 13px; font-weight: 600;")
        self.sub = QLabel(sub)
        self.sub.setObjectName("sub")
        self.sub.setStyleSheet("font-size: 12px;")
        for lb in (self.title, self.sub):
            lb.setTextFormat(Qt.PlainText)
            lb.setMinimumWidth(10)
        v.addWidget(self.title)
        v.addWidget(self.sub)
        h.addLayout(v, 1)

    def elide(self, width):
        for lb in (self.title, self.sub):
            full = lb.property("full") or lb.text()
            lb.setProperty("full", full)
            lb.setText(lb.fontMetrics().elidedText(full, Qt.ElideRight, width))


class MusicPanel(QWidget):
    W = 470

    def __init__(self, engine: MusicEngine):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(PANEL_QSS)
        f = QFont()
        f.setFamilies(["Segoe UI Variable Text", "Segoe UI"])
        self.setFont(f)
        self.engine = engine
        self.tracks = []
        self.mode = "queue"          # "queue" | "search" | "collection"
        self.token = 0
        self.play_when_ready = False
        self.thumb_cache = {}

        frame = QFrame(self)
        frame.setObjectName("frame")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(frame)
        v = QVBoxLayout(frame)
        v.setContentsMargins(14, 14, 14, 10)
        v.setSpacing(8)

        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Search songs, or paste a YouTube / Spotify link")
        self.edit.installEventFilter(self)
        v.addWidget(self.edit)
        self.head = QLabel()
        self.head.setObjectName("head")
        self.head.setStyleSheet("font-size: 12px; padding-left: 4px;")
        v.addWidget(self.head)
        self.list = QListWidget()
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.itemClicked.connect(lambda it: self.activate(self.list.row(it), False))
        v.addWidget(self.list)
        self.hint = QLabel("Enter  play     Ctrl+Enter  play next     Esc  close")
        self.hint.setObjectName("hint")
        self.hint.setStyleSheet("font-size: 11px; padding-left: 4px;")
        v.addWidget(self.hint)

        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(350)
        self.debounce.timeout.connect(self.run_search)
        self.edit.textChanged.connect(self.on_text)
        engine.results.connect(self.on_results)
        self.setFixedWidth(self.W)

    # --- show / hide ---------------------------------------------------- #
    def toggle(self):
        if self.isVisible():
            self.hide()
        else:
            self.open()

    def open(self):
        if not self.edit.text().strip():
            self.show_queue()
        g = QGuiApplication.primaryScreen().geometry()
        self.move(g.x() + (g.width() - self.W) // 2, g.y() + 40)
        self.show()
        self.raise_()
        self.activateWindow()
        force_foreground(int(self.winId()))
        self.edit.setFocus()
        self.edit.selectAll()

    def event(self, e):
        if e.type() == QEvent.WindowDeactivate:
            self.hide()
        return super().event(e)

    # --- input ------------------------------------------------------------ #
    def eventFilter(self, obj, e):
        if obj is self.edit and e.type() == QEvent.KeyPress:
            k = e.key()
            if k == Qt.Key_Escape:
                self.hide()
                return True
            if k in (Qt.Key_Down, Qt.Key_Up) and self.list.count():
                r = self.list.currentRow() + (1 if k == Qt.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, r)))
                return True
            if k in (Qt.Key_Return, Qt.Key_Enter):
                play_next = bool(e.modifiers() & Qt.ControlModifier)
                if self.debounce.isActive():      # results not there yet: search now, play the first one
                    self.debounce.stop()
                    self.run_search()
                    self.play_when_ready = not play_next
                elif self.list.count():
                    self.activate(max(0, self.list.currentRow()), play_next)
                return True
        return super().eventFilter(obj, e)

    def on_text(self, text):
        self.play_when_ready = False
        if text.strip():
            self.debounce.start()
        else:
            self.debounce.stop()
            self.show_queue()

    def run_search(self):
        text = self.edit.text().strip()
        if not text:
            return
        self.token += 1
        self.head.setText("Searching…")
        self.engine.search(text, self.token)

    def on_results(self, res):
        token, r = res
        if token != self.token:
            return             # an older search
        if isinstance(r, str):
            self.set_rows([], r)
            return
        self.mode = "collection" if r["link"] else "search"
        head = r.get("title") or ("Songs" if self.mode == "search" else "Tracks")
        if self.mode == "collection":
            head += f"  ·  {len(r['tracks'])} tracks"
        self.tracks = r["tracks"]
        self.set_rows(self.tracks, head if self.tracks else "No results")
        if self.play_when_ready and self.tracks:
            self.play_when_ready = False
            self.activate(0, False)

    def show_queue(self):
        self.mode = "queue"
        e = self.engine
        if e.active():
            self.tracks = e.queue[e.index:e.index + 40]
            self.set_rows(self.tracks, "Now playing  ·  up next", current=0)
        else:
            self.tracks = []
            self.set_rows([], "Type to search YouTube Music")

    def set_rows(self, tracks, head, current=None):
        self.head.setText(head)
        self.list.clear()
        for i, t in enumerate(tracks):
            sub = " · ".join(x for x in (t.get("artist"), fmt_dur(t.get("duration"))) if x)
            if i == current:
                sub = "▶  " + sub
            row = Row(t.get("title", ""), sub)
            row.elide(self.W - 28 - 16 - 40 - 12 - 24)
            it = QListWidgetItem()
            it.setSizeHint(QSize(0, ROW_H))
            self.list.addItem(it)
            self.list.setItemWidget(it, row)
            self.load_art(row, t.get("thumb_url"))
        if tracks:
            self.list.setCurrentRow(0)
        self.list.setFixedHeight(min(len(tracks), MAX_ROWS) * ROW_H + (4 if tracks else 0))
        self.list.setVisible(bool(tracks))
        self.adjustSize()

    def load_art(self, row, url):
        if not url:
            return
        if url in self.thumb_cache:
            row.art.setPixmap(self.thumb_cache[url])
            return

        def done(data):
            if not data:
                return
            pm = rounded(QImage.fromData(data), 40, 8)
            self.thumb_cache[url] = pm
            try:
                row.art.setPixmap(pm)
            except RuntimeError:
                pass           # the row was removed meanwhile
        self.engine.fetch_bytes(url, done)

    def activate(self, i, play_next):
        if not (0 <= i < len(self.tracks)):
            return
        e = self.engine
        if self.mode == "queue":
            e.jump(e.index + i)
        elif play_next:
            e.enqueue(self.tracks[i])
            self.head.setText(f"Added: {self.tracks[i]['title']}")
            return
        elif self.mode == "collection":
            e.play(self.tracks, i)
        else:
            e.play([self.tracks[i]])      # then YouTube Music's radio continues
        self.hide()
