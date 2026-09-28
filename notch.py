"""
NotchIsland — une encoche façon macOS / Dynamic Island pour Windows.

- Encoche noire en haut au centre de l'écran principal
- S'agrandit quand de la musique joue (pochette + égaliseur réactif au son)
- Affiche un aperçu quand le morceau change
- Affiche le volume quand on le modifie
- Au survol : lecteur complet (pochette, titre, progression, contrôles)
- Sans musique, au survol : heure et date
"""

import sys
import os
import math
import time
import random
import asyncio
import threading
import ctypes

from PySide6.QtCore import Qt, QObject, Signal, QTimer, QRectF, QPointF, QLocale, QDateTime
from PySide6.QtGui import (
    QPainter, QPainterPath, QColor, QFont, QFontMetricsF, QImage, QRegion,
    QCursor, QIcon, QPixmap, QLinearGradient, QGuiApplication, QAction, QPen,
)
from PySide6.QtWidgets import QApplication, QWidget, QSystemTrayIcon, QMenu

APP_NAME = "NotchIsland"

# (largeur, hauteur, rayon des coins bas)
SIZES = {
    "idle": (200, 32, 10),
    "compact": (300, 32, 12),
    "volume": (320, 32, 12),
    "peek": (420, 78, 26),
    "expanded": (500, 196, 34),
}
EAR = 8          # petites courbes concaves qui raccordent l'encoche au bord de l'écran
WIN_W, WIN_H = 620, 250

PLAYING = 4      # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


# --------------------------------------------------------------------------- #
#  Lecture des infos média Windows (thread dédié + boucle asyncio)
# --------------------------------------------------------------------------- #
def pretty_app_name(aumid: str) -> str:
    if not aumid:
        return ""
    name = aumid.split("!")[-1]
    name = name.rsplit("\\", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    known = {
        "chrome": "Chrome", "msedge": "Edge", "firefox": "Firefox", "spotify": "Spotify",
        "opera": "Opera", "brave": "Brave", "vlc": "VLC", "deezer": "Deezer",
        "microsoft.zunemusic": "Lecteur multimédia", "music.ui": "Lecteur multimédia",
        "applemusic": "Apple Music", "discord": "Discord",
    }
    low = name.lower()
    for k, v in known.items():
        if k in low:
            return v
    return name[:1].upper() + name[1:]


class MediaWorker(QObject):
    changed = Signal(object)

    def __init__(self):
        super().__init__()
        self.loop = None
        self.session = None
        self.key = None
        self.thumb_tries = 0
        self._local_pos = (0.0, time.time())

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _main(self):
        from winrt.windows.media.control import (
            GlobalSystemMediaTransportControlsSessionManager as Manager,
        )
        mgr = None
        while True:
            try:
                if mgr is None:
                    mgr = await Manager.request_async()
                await self._poll(mgr)
            except Exception as e:  # une appli média peut disparaître à tout moment
                print("media poll error:", e)
                self.session = None
            await asyncio.sleep(0.35)

    @staticmethod
    def _pick(mgr):
        cur = mgr.get_current_session()
        try:
            if cur and cur.get_playback_info().playback_status == PLAYING:
                return cur
            for s in list(mgr.get_sessions()):
                if s.get_playback_info().playback_status == PLAYING:
                    return s
        except Exception:
            pass
        return cur

    async def _read_thumb(self, props):
        from winrt.windows.storage.streams import Buffer, InputStreamOptions
        if not props.thumbnail:
            return None
        stream = await props.thumbnail.open_read_async()
        n = stream.size
        if not n:
            return None
        buf = Buffer(n)
        await stream.read_async(buf, n, InputStreamOptions.READ_AHEAD)
        return bytes(buf)

    async def _poll(self, mgr):
        s = self._pick(mgr)
        self.session = s
        if s is None:
            self.key = None
            self.changed.emit({"has_media": False, "key": None})
            return

        info = s.get_playback_info()
        playing = info.playback_status == PLAYING
        props = await s.try_get_media_properties_async()
        title = props.title or ""
        artist = props.artist or props.album_artist or ""
        app = s.source_app_user_model_id or ""
        if not title:
            self.key = None
            self.changed.emit({"has_media": False, "key": None})
            return

        key = (app, title, artist)
        d = {}
        if key != self.key:
            self.key = key
            self.thumb_tries = 0
            d["thumb"] = None
        # la pochette arrive parfois un peu après le titre (navigateurs) : on réessaie
        if self.thumb_tries < 10 and ("thumb" in d or self.thumb_tries > 0):
            try:
                data = await self._read_thumb(props)
            except Exception:
                data = None
            if data:
                d["thumb"] = data
                self.thumb_tries = 99
            else:
                self.thumb_tries += 1

        tl = s.get_timeline_properties()
        pos = tl.position.total_seconds()
        dur = max(0.0, (tl.end_time - tl.start_time).total_seconds())
        now = time.time()
        try:
            upd = tl.last_updated_time.timestamp()
        except Exception:
            upd = 0
        if not (now - 86400 < upd <= now + 5):
            # horodatage inutilisable : on suit la position nous-mêmes
            if abs(pos - self._local_pos[0]) > 0.01:
                self._local_pos = (pos, now)
            pos, upd = self._local_pos

        c = info.controls
        d.update({
            "has_media": True,
            "key": key,
            "title": title,
            "artist": artist,
            "app": pretty_app_name(app),
            "playing": playing,
            "position": pos,
            "updated": upd,
            "duration": dur,
            "can_prev": bool(c.is_previous_enabled),
            "can_next": bool(c.is_next_enabled),
            "can_seek": bool(c.is_playback_position_enabled),
        })
        self.changed.emit(d)

    # --- commandes -------------------------------------------------------- #
    def command(self, name, arg=None):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._cmd(name, arg), self.loop)

    async def _cmd(self, name, arg):
        s = self.session
        if s is None:
            return
        try:
            if name == "toggle":
                await s.try_toggle_play_pause_async()
            elif name == "next":
                await s.try_skip_next_async()
            elif name == "prev":
                await s.try_skip_previous_async()
            elif name == "seek":
                await s.try_change_playback_position_async(int(arg * 10_000_000))
        except Exception as e:
            print("command error:", e)


# --------------------------------------------------------------------------- #
#  Audio système (niveau sonore + volume) via pycaw
# --------------------------------------------------------------------------- #
class AudioProbe:
    def __init__(self):
        self.meter = None
        self.volume = None
        self.last_refresh = 0
        self.refresh()

    def refresh(self):
        self.last_refresh = time.monotonic()
        try:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            dev = AudioUtilities.GetSpeakers()
            self.meter = dev._dev.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None) \
                .QueryInterface(IAudioMeterInformation)
            self.volume = dev.EndpointVolume
        except Exception as e:
            print("audio probe error:", e)
            self.meter = self.volume = None

    def tick(self):
        # re-sélectionne régulièrement le périphérique (casque branché, etc.)
        if time.monotonic() - self.last_refresh > 5:
            self.refresh()

    def peak(self):
        try:
            return float(self.meter.GetPeakValue()) if self.meter else 0.0
        except Exception:
            self.meter = None
            return 0.0

    def vol(self):
        try:
            if self.volume:
                return float(self.volume.GetMasterVolumeLevelScalar()), bool(self.volume.GetMute())
        except Exception:
            self.volume = None
        return None


# --------------------------------------------------------------------------- #
#  Petits outils d'animation
# --------------------------------------------------------------------------- #
class Spring:
    """Ressort légèrement sous-amorti : donne le petit rebond typique d'Apple."""

    def __init__(self, v, k=260.0, d=23.0):
        self.x = self.t = float(v)
        self.v = 0.0
        self.k, self.d = k, d

    def step(self, dt):
        a = self.k * (self.t - self.x) - self.d * self.v
        self.v += a * dt
        self.x += self.v * dt

    @property
    def settled(self):
        return abs(self.t - self.x) < 0.2 and abs(self.v) < 0.2


def accent_from(img: QImage) -> QColor:
    small = img.scaled(24, 24, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    best, score = None, -1.0
    for y in range(small.height()):
        for x in range(small.width()):
            c = small.pixelColor(x, y)
            s, v = c.hsvSaturationF(), c.valueF()
            sc = s * s * v + 0.05 * v
            if sc > score:
                best, score = c, sc
    if best is None or best.hsvSaturationF() < 0.18:
        return QColor(235, 235, 240)
    h = max(0.0, best.hsvHueF())
    return QColor.fromHsvF(h, min(1.0, best.hsvSaturationF() * 0.9 + 0.1), max(best.valueF(), 0.85))


def fmt_time(s):
    s = max(0, int(s))
    if s >= 3600:
        return f"{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60}:{s % 60:02d}"


def font(size, weight=QFont.Normal):
    f = QFont()
    f.setFamilies(["Segoe UI Variable Display", "Segoe UI"])
    f.setPixelSize(size)
    f.setWeight(weight)
    f.setHintingPreference(QFont.PreferNoHinting)
    return f


# --------------------------------------------------------------------------- #
#  La fenêtre de l'encoche
# --------------------------------------------------------------------------- #
class Notch(QWidget):
    def __init__(self, worker: MediaWorker):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
                         | Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.resize(WIN_W, WIN_H)

        self.worker = worker
        worker.changed.connect(self.on_media)
        self.audio = AudioProbe()

        self.media = {"has_media": False, "key": None}
        self.art = None
        self.art_cache = {}
        self.accent = QColor(235, 235, 240)

        # démarrage : l'encoche « pousse » depuis le haut
        self.w, self.h, self.r = Spring(110), Spring(4), Spring(6)
        self.state = "idle"
        self.alpha = {k: 0.0 for k in ("compact", "volume", "peek", "expanded")}

        self.hover = False
        self.hover_since = None
        self.leave_since = None
        self.peek_until = 0.0
        self.vol_until = 0.0
        self.last_playing = 0.0
        self.first_media = True
        self.optimistic = None  # (playing, until)

        v = self.audio.vol()
        self.vol_level, self.vol_muted = v if v else (0.0, False)

        self.level = 0.0
        self.bar_seed = [(random.uniform(5, 9), random.uniform(0, 6.28), random.uniform(0.6, 1.0))
                         for _ in range(6)]
        self.buttons = {}
        self.progress_rect = None
        self.t0 = time.monotonic()
        self.last_tick = time.monotonic()
        self._mask_rect = None

        self.place()
        QGuiApplication.primaryScreen().geometryChanged.connect(lambda *_: self.place())

        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.PreciseTimer)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

        self.raise_timer = QTimer(self)
        self.raise_timer.timeout.connect(self.raise_)
        self.raise_timer.start(3000)

    # --- placement -------------------------------------------------------- #
    def place(self):
        g = QGuiApplication.primaryScreen().geometry()
        self.move(g.x() + (g.width() - WIN_W) // 2, g.y())

    def notch_rect(self):
        w, h = max(40.0, self.w.x), max(2.0, self.h.x)
        x0 = (WIN_W - w) / 2
        return QRectF(x0, 0, w, h)

    def notch_path(self):
        rc = self.notch_rect()
        x0, x1, h = rc.left(), rc.right(), rc.height()
        r = max(2.0, min(self.r.x, h / 2, rc.width() / 2))
        e = min(EAR, h / 2)
        k = 0.5523  # approximation d'un quart de cercle en Bézier
        p = QPainterPath()
        p.moveTo(x0 - e, 0)
        p.cubicTo(x0 - e + e * k, 0, x0, e - e * k, x0, e)
        p.lineTo(x0, h - r)
        p.cubicTo(x0, h - r + r * k, x0 + r - r * k, h, x0 + r, h)
        p.lineTo(x1 - r, h)
        p.cubicTo(x1 - r + r * k, h, x1, h - r + r * k, x1, h - r)
        p.lineTo(x1, e)
        p.cubicTo(x1, e - e * k, x1 + e - e * k, 0, x1 + e, 0)
        p.closeSubpath()
        return p

    # --- données média ---------------------------------------------------- #
    def on_media(self, d):
        prev_key = self.media.get("key")
        if "thumb" in d:
            self.set_art(d["thumb"])
        if d.get("has_media") and d.get("key") != prev_key:
            if not self.first_media and d.get("playing"):
                self.peek_until = time.monotonic() + 3.2
            self.first_media = False
        self.media.update(d)
        if not d.get("has_media"):
            self.media = {"has_media": False, "key": None}
        if self.optimistic and time.monotonic() > self.optimistic[1]:
            self.optimistic = None

    def set_art(self, data):
        self.art_cache.clear()
        if not data:
            self.art = None
            self.accent = QColor(235, 235, 240)
            return
        img = QImage.fromData(data)
        if img.isNull():
            self.art = None
            return
        side = min(img.width(), img.height())
        img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
        self.art = img
        self.accent = accent_from(img)

    def is_playing(self):
        if self.optimistic and time.monotonic() < self.optimistic[1]:
            return self.optimistic[0]
        return bool(self.media.get("playing"))

    def position(self):
        m = self.media
        pos = m.get("position", 0.0)
        if self.is_playing() and m.get("playing"):
            pos += time.time() - m.get("updated", time.time())
        dur = m.get("duration", 0.0)
        return max(0.0, min(pos, dur)) if dur > 0 else pos

    # --- boucle principale ------------------------------------------------ #
    def tick(self):
        now = time.monotonic()
        dt = min(0.05, now - self.last_tick)
        self.last_tick = now

        # survol (on teste la position du curseur : plus fiable qu'enter/leave avec un masque)
        rc = self.notch_rect().adjusted(-EAR, 0, EAR, 2)
        inside = rc.contains(QPointF(self.mapFromGlobal(QCursor.pos())))
        if inside:
            self.leave_since = None
            self.hover_since = self.hover_since or now
            if not self.hover and now - self.hover_since > 0.12:
                self.hover = True
        else:
            self.hover_since = None
            self.leave_since = self.leave_since or now
            if self.hover and now - self.leave_since > 0.3:
                self.hover = False

        # volume système
        self.audio.tick()
        v = self.audio.vol()
        if v:
            lvl, muted = v
            if abs(lvl - self.vol_level) > 0.004 or muted != self.vol_muted:
                self.vol_until = now + 1.6
            self.vol_level, self.vol_muted = lvl, muted

        # niveau sonore pour l'égaliseur
        playing = self.is_playing() and self.media.get("has_media")
        if playing:
            self.last_playing = now
            raw = min(1.0, (self.audio.peak() ** 0.6) * 1.25)
        else:
            raw = 0.0
        a = 0.55 if raw > self.level else 0.12
        self.level += (raw - self.level) * a

        # état cible
        has = self.media.get("has_media")
        if self.hover:
            st = "expanded"
        elif now < self.vol_until:
            st = "volume"
        elif has and now < self.peek_until:
            st = "peek"
        elif has and (playing or now - self.last_playing < 6):
            st = "compact"
        else:
            st = "idle"
        self.state = st
        tw, th, tr = SIZES[st]
        self.w.t, self.h.t, self.r.t = tw, th, tr
        for s in (self.w, self.h, self.r):
            s.step(dt)

        near = abs(self.w.x - tw) < 45 and abs(self.h.x - th) < 28
        for k in self.alpha:
            target = 1.0 if (k == st and near) else 0.0
            speed = 12.0 if target > self.alpha[k] else 22.0
            self.alpha[k] += (target - self.alpha[k]) * min(1.0, dt * speed)
            if abs(self.alpha[k] - target) < 0.003:
                self.alpha[k] = target

        # masque = zone cliquable ; le reste de la fenêtre laisse passer la souris
        rc = self.notch_rect()
        mr = (int(rc.left() - EAR - 1), 0, int(rc.width() + 2 * EAR + 3), int(rc.height() + 2))
        if mr != self._mask_rect:
            self._mask_rect = mr
            self.setMask(QRegion(*mr))
        self.update()

    # --- dessin ----------------------------------------------------------- #
    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform | QPainter.TextAntialiasing)
        path = self.notch_path()
        p.fillPath(path, QColor(0, 0, 0))
        p.setClipPath(path)
        rc = self.notch_rect()
        t = time.monotonic() - self.t0

        self.buttons = {}
        self.progress_rect = None
        for layer, fn in (("compact", self.draw_compact), ("volume", self.draw_volume),
                          ("peek", self.draw_peek), ("expanded", self.draw_expanded)):
            a = self.alpha[layer]
            if a > 0.01:
                p.save()
                p.setOpacity(a)
                fn(p, rc, t)
                p.restore()
        p.end()

    def draw_art(self, p, rect: QRectF, radius):
        path = QPainterPath()
        path.addRoundedRect(rect, radius, radius)
        p.save()
        p.setClipPath(path, Qt.IntersectClip)
        if self.art is not None:
            dpr = self.devicePixelRatioF()
            key = (round(rect.width()), dpr)
            img = self.art_cache.get(key)
            if img is None:
                px = max(1, int(rect.width() * dpr))
                img = self.art.scaled(px, px, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                self.art_cache[key] = img
            p.drawImage(rect, img)
        else:
            g = QLinearGradient(rect.topLeft(), rect.bottomRight())
            g.setColorAt(0, QColor(70, 70, 80))
            g.setColorAt(1, QColor(30, 30, 36))
            p.fillRect(rect, g)
            p.setPen(QColor(255, 255, 255, 170))
            p.setFont(font(int(rect.height() * 0.55)))
            p.drawText(rect, Qt.AlignCenter, "♪")
        p.restore()

    def draw_bars(self, p, cx, cy, n, bw, gap, max_h, t):
        color = QColor(self.accent)
        total = n * bw + (n - 1) * gap
        x = cx - total / 2
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        for i in range(n):
            f, ph, amp = self.bar_seed[i]
            wobble = 0.55 + 0.45 * math.sin(t * f + ph) * math.sin(t * f * 0.37 + ph * 2)
            hh = bw + (max_h - bw) * max(0.0, min(1.0, self.level * amp * (0.35 + 0.65 * abs(wobble))))
            p.drawRoundedRect(QRectF(x, cy - hh / 2, bw, hh), bw / 2, bw / 2)
            x += bw + gap

    def draw_compact(self, p, rc, t):
        self.draw_art(p, QRectF(rc.left() + 11, 5, 22, 22), 6)
        self.draw_bars(p, rc.right() - 26, 16, 4, 3, 2.5, 15, t)

    def draw_volume(self, p, rc, t):
        cy = 16
        x = rc.left() + 16
        # haut-parleur
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255))
        sp = QPainterPath()
        sp.moveTo(x, cy - 3)
        sp.lineTo(x + 3.5, cy - 3)
        sp.lineTo(x + 8, cy - 7)
        sp.lineTo(x + 8, cy + 7)
        sp.lineTo(x + 3.5, cy + 3)
        sp.lineTo(x, cy + 3)
        sp.closeSubpath()
        p.drawPath(sp)
        pen = QPen(QColor(255, 255, 255))
        pen.setWidthF(1.6)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        if self.vol_muted or self.vol_level < 0.005:
            p.drawLine(QPointF(x + 11, cy - 3.5), QPointF(x + 18, cy + 3.5))
            p.drawLine(QPointF(x + 18, cy - 3.5), QPointF(x + 11, cy + 3.5))
        else:
            waves = 1 + int(self.vol_level > 0.33) + int(self.vol_level > 0.66)
            for i in range(waves):
                r = 4 + i * 3.5
                p.drawArc(QRectF(x + 8 - r + 2, cy - r, 2 * r, 2 * r), -50 * 16, 100 * 16)

        # barre
        bx0, bx1 = rc.left() + 44, rc.right() - 46
        track = QRectF(bx0, cy - 2.5, bx1 - bx0, 5)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 55))
        p.drawRoundedRect(track, 2.5, 2.5)
        lvl = 0 if self.vol_muted else self.vol_level
        p.setBrush(QColor(255, 255, 255))
        p.drawRoundedRect(QRectF(bx0, cy - 2.5, max(5, track.width() * lvl), 5), 2.5, 2.5)
        p.setPen(QColor(255, 255, 255, 200))
        p.setFont(font(12, QFont.DemiBold))
        p.drawText(QRectF(bx1 + 6, 0, 40, 32), Qt.AlignVCenter | Qt.AlignLeft, f"{round(lvl * 100)}")

    def draw_text(self, p, text, fnt, rect, color, t=None):
        """Texte sur une ligne ; défile (marquee) s'il est trop long et que t est fourni."""
        p.setFont(fnt)
        p.setPen(color)
        fm = QFontMetricsF(fnt)
        tw = fm.horizontalAdvance(text)
        if tw <= rect.width() or t is None:
            p.drawText(rect, Qt.AlignLeft | Qt.AlignVCenter,
                       fm.elidedText(text, Qt.ElideRight, rect.width()))
            return
        gap, speed, pause = 40, 28, 2.0
        cycle = (tw + gap) / speed + pause
        off = max(0.0, (t % cycle) - pause) * speed
        p.save()
        p.setClipRect(rect, Qt.IntersectClip)
        for dx in (-off, -off + tw + gap):
            p.drawText(QRectF(rect.left() + dx, rect.top(), tw + 2, rect.height()),
                       Qt.AlignLeft | Qt.AlignVCenter, text)
        p.restore()

    def draw_peek(self, p, rc, t):
        m = self.media
        self.draw_art(p, QRectF(rc.left() + 14, 14, 50, 50), 12)
        tx = rc.left() + 78
        tw = rc.width() - 78 - 58
        self.draw_text(p, m.get("title", ""), font(15, QFont.DemiBold), QRectF(tx, 18, tw, 22),
                       QColor(255, 255, 255))
        self.draw_text(p, m.get("artist", ""), font(13), QRectF(tx, 40, tw, 20), QColor(255, 255, 255, 150))
        self.draw_bars(p, rc.right() - 32, 39, 4, 3.5, 3, 22, t)

    def draw_expanded(self, p, rc, t):
        if self.media.get("has_media"):
            self.draw_player(p, rc, t)
        else:
            self.draw_clock(p, rc)

    def draw_clock(self, p, rc):
        loc = QLocale(QLocale.French, QLocale.France)
        now = QDateTime.currentDateTime()
        p.setPen(QColor(255, 255, 255))
        p.setFont(font(46, QFont.Light))
        p.drawText(QRectF(rc.left(), 26, rc.width(), 64), Qt.AlignCenter, loc.toString(now, "HH:mm"))
        date = loc.toString(now, "dddd d MMMM")
        p.setFont(font(15, QFont.DemiBold))
        p.setPen(QColor(255, 255, 255, 200))
        p.drawText(QRectF(rc.left(), 92, rc.width(), 24), Qt.AlignCenter, date[:1].upper() + date[1:])
        p.setFont(font(12))
        p.setPen(QColor(255, 255, 255, 110))
        p.drawText(QRectF(rc.left(), 140, rc.width(), 20), Qt.AlignCenter, "♪  Aucune lecture en cours")

    def draw_player(self, p, rc, t):
        m = self.media
        pad = 24
        x0, x1 = rc.left(), rc.right()
        self.draw_art(p, QRectF(x0 + pad, 24, 88, 88), 16)

        tx = x0 + pad + 88 + 18
        tw = x1 - pad - tx - 34
        self.draw_text(p, m.get("title", ""), font(17, QFont.DemiBold), QRectF(tx, 34, tw, 24),
                       QColor(255, 255, 255), t)
        self.draw_text(p, m.get("artist", ""), font(14), QRectF(tx, 58, tw, 20),
                       QColor(255, 255, 255, 150), t)
        app = m.get("app", "")
        if app:
            p.setPen(Qt.NoPen)
            p.setBrush(self.accent)
            p.drawEllipse(QPointF(tx + 3, 92), 3, 3)
            self.draw_text(p, app, font(12, QFont.DemiBold), QRectF(tx + 11, 82, tw - 11, 20),
                           QColor(self.accent))
        self.draw_bars(p, x1 - pad - 10, 44, 5, 3, 2.5, 20, t)

        # progression
        dur = m.get("duration", 0.0)
        pos = self.position()
        py = 134
        p.setFont(font(11, QFont.DemiBold))
        p.setPen(QColor(255, 255, 255, 140))
        if dur > 0:
            p.drawText(QRectF(x0 + pad, py - 10, 44, 20), Qt.AlignLeft | Qt.AlignVCenter, fmt_time(pos))
            p.drawText(QRectF(x1 - pad - 44, py - 10, 44, 20), Qt.AlignRight | Qt.AlignVCenter,
                       "-" + fmt_time(dur - pos))
            bx0, bx1 = x0 + pad + 48, x1 - pad - 48
            cursor = QPointF(self.mapFromGlobal(QCursor.pos()))
            bar = QRectF(bx0, py - 3, bx1 - bx0, 6)
            hot = bar.adjusted(0, -8, 0, 8).contains(cursor) and m.get("can_seek")
            bh = 8 if hot else 6
            bar = QRectF(bx0, py - bh / 2, bx1 - bx0, bh)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 50))
            p.drawRoundedRect(bar, bh / 2, bh / 2)
            p.setBrush(QColor(255, 255, 255) if not hot else self.accent)
            p.drawRoundedRect(QRectF(bx0, bar.top(), max(bh, bar.width() * pos / dur), bh), bh / 2, bh / 2)
            self.progress_rect = QRectF(bx0, py - 10, bx1 - bx0, 20)

        # contrôles
        cy = 170
        cx = rc.center().x()
        cursor = QPointF(self.mapFromGlobal(QCursor.pos()))
        for name, x, size in (("prev", cx - 70, 15), ("toggle", cx, 20), ("next", cx + 70, 15)):
            hit = QRectF(x - 20, cy - 20, 40, 40)
            enabled = name == "toggle" or m.get("can_" + name, True)
            self.buttons[name] = hit if enabled else None
            if enabled and hit.contains(cursor):
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(255, 255, 255, 30))
                p.drawEllipse(hit.center(), 19, 19)
            col = QColor(255, 255, 255, 255 if enabled else 70)
            self.draw_icon(p, name, x, cy, size, col)

    def draw_icon(self, p, name, cx, cy, s, col):
        p.setPen(Qt.NoPen)
        p.setBrush(col)

        def tri(x, y, w, h, left=False):
            path = QPainterPath()
            if left:
                path.moveTo(x + w, y - h / 2)
                path.lineTo(x, y)
                path.lineTo(x + w, y + h / 2)
            else:
                path.moveTo(x, y - h / 2)
                path.lineTo(x + w, y)
                path.lineTo(x, y + h / 2)
            path.closeSubpath()
            p.drawPath(path)

        if name == "toggle":
            if self.is_playing():
                w = s * 0.3
                p.drawRoundedRect(QRectF(cx - s * 0.4, cy - s / 2, w, s), 2, 2)
                p.drawRoundedRect(QRectF(cx + s * 0.1, cy - s / 2, w, s), 2, 2)
            else:
                tri(cx - s * 0.38, cy, s * 0.9, s)
        elif name == "next":
            tri(cx - s * 0.75, cy, s * 0.7, s * 0.85)
            tri(cx - s * 0.1, cy, s * 0.7, s * 0.85)
            p.drawRoundedRect(QRectF(cx + s * 0.6, cy - s * 0.42, 2.2, s * 0.84), 1, 1)
        elif name == "prev":
            tri(cx + s * 0.05, cy, s * 0.7, s * 0.85, left=True)
            tri(cx - s * 0.6, cy, s * 0.7, s * 0.85, left=True)
            p.drawRoundedRect(QRectF(cx - s * 0.6 - 2.2, cy - s * 0.42, 2.2, s * 0.84), 1, 1)

    # --- clics ------------------------------------------------------------ #
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or self.state != "expanded":
            return
        pos = e.position()
        for name, rect in self.buttons.items():
            if rect is not None and rect.contains(pos):
                if name == "toggle":
                    self.optimistic = (not self.is_playing(), time.monotonic() + 1.5)
                self.worker.command(name)
                return
        pr = self.progress_rect
        if pr is not None and pr.contains(pos) and self.media.get("can_seek"):
            frac = (pos.x() - pr.left()) / pr.width()
            target = max(0.0, min(1.0, frac)) * self.media.get("duration", 0)
            self.media["position"], self.media["updated"] = target, time.time()
            self.worker.command("seek", target)


# --------------------------------------------------------------------------- #
#  Icône de la zone de notification + démarrage automatique
# --------------------------------------------------------------------------- #
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def launch_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{os.path.abspath(__file__)}"'


def autostart_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(245, 245, 247))
    p.drawRoundedRect(QRectF(2, 2, 60, 60), 14, 14)
    p.setBrush(QColor(0, 0, 0))
    p.drawRoundedRect(QRectF(12, -10, 40, 30), 10, 10)
    p.end()
    return QIcon(pm)


def main():
    # une seule instance à la fois
    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\NotchIsland_single_instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        return

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(APP_NAME)

    worker = MediaWorker()
    notch = Notch(worker)
    worker.start()
    notch.show()

    icon = make_icon()
    app.setWindowIcon(icon)
    tray = QSystemTrayIcon(icon)
    tray.setToolTip("NotchIsland")
    menu = QMenu()
    title = QAction("NotchIsland", menu)
    title.setEnabled(False)
    menu.addAction(title)
    menu.addSeparator()
    auto = QAction("Lancer au démarrage de Windows", menu, checkable=True)
    auto.setChecked(autostart_enabled())
    auto.toggled.connect(set_autostart)
    menu.addAction(auto)
    menu.addSeparator()
    quit_action = QAction("Quitter", menu)
    quit_action.triggered.connect(app.quit)
    menu.addAction(quit_action)
    tray.setContextMenu(menu)
    tray.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
