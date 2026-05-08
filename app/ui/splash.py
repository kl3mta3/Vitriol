"""Vitriol splash screen — branded cold-start ceremony.

Plays for ~3 seconds while the heavyweight startup work (PySide6 import,
format-handler discovery) happens behind it. Centerpiece is the
alchemical motto V.I.T.R.I.O.L. ("Visita Interiora Terrae Rectificando
Invenies Occultum Lapidem" — "Visit the interior of the earth; by
rectification you will find the hidden stone"), which describes Stone
mode literally rather than metaphorically.

Animation timeline (6.5 s total):
    0.00 – 0.30 s   Background + alchemical circle fade in,
                     circle starts a slow rotation (~12 s/turn).
    0.30 – 0.50 s   Wordmark "Vitriol" appears at tight tracking.
    0.50 – 0.85 s   Letters animate apart — tracking grows ~0% → ~150%.
    0.85 – 2.25 s   Synchronized wave (~0.20 s per letter+word pair, was
                     0.13 s — slowed so each pairing is comfortably
                     readable): each letter of Vitriol bolds + grows
                     briefly, paired with the corresponding Latin word
                     of the motto entering. Either MODE A (word fades
                     in during the highlight) or MODE B (motto pre-
                     rendered at low opacity, brightens during the
                     highlight). Coin flip per launch.
    2.25 – 2.95 s   English translation fades in line-by-line —
                     0.35 s per line, sequential rather than simultaneous,
                     so the user reads each phrase as it arrives.
    2.95 – 5.15 s   Hold the full motto for ~2.2 s. Latin and English
                     both visible, no animation. The alchemical-circle
                     rotation continues underneath — provides a "still
                     progressing" cue so the user reads the hold as
                     load-time activity rather than gratuitous ceremony.
                     Long enough to read the whole motto twice.
    5.15 – 5.85 s   Flourish — Vitriol bolds, tracking contracts, font
                     grows, position translates toward splash center;
                     Latin + English fade to 0; red glow pulse on the
                     alchemical circle (Stone-mode color, foreshadowing).
    5.85 – 6.50 s   Hold climactic Vitriol + fade out, main window
                     appears beneath.

Click anywhere on the splash → cuts to a 150 ms fade-out and dismisses.
Multi-monitor: splash centers on the screen containing the cursor at
construction time (where the user just clicked the Vitriol launcher).
"""
from __future__ import annotations

import random
from pathlib import Path

from PySide6.QtCore import (
    Qt, QPoint, QPointF, QPropertyAnimation, QParallelAnimationGroup,
    QPauseAnimation, QSequentialAnimationGroup, QEasingCurve, QRectF,
    QTimer, Property, QObject, Signal, QAbstractAnimation,
)
from PySide6.QtGui import (
    QColor, QCursor, QFont, QGuiApplication, QPainter, QPixmap,
)
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout,
    QLabel, QSplashScreen, QVBoxLayout, QWidget,
)

from ..utils.paths import resources_dir


# Splash dimensions (px). 560×400 chosen empirically: wide enough to fit
# the Latin motto on one line at 13pt, tall enough for logo + wordmark +
# motto + English translation + breathing room.
_SPLASH_W = 560
_SPLASH_H = 400

# The motto. Each Latin word's first letter pairs with the corresponding
# letter of "Vitriol" (V↔Visita, i↔Interiora, t↔Terrae, r↔Rectificando,
# i↔Invenies, o↔Occultum, l↔Lapidem). The wave animation surfaces this
# pairing through synchronized motion.
_VITRIOL_LETTERS = list("Vitriol")
_LATIN_WORDS = ["Visita", "Interiora", "Terrae", "Rectificando",
                 "Invenies", "Occultum", "Lapidem"]
_ENGLISH_LINE_1 = "\"Visit the interior of the earth; by rectification"
_ENGLISH_LINE_2 = "you will find the hidden stone.\""

# Wave-mode constants. Mode A reveals each Latin word as its paired
# letter activates (higher pedagogical link). Mode B renders the whole
# motto pre-dimmed and brightens each word in turn (easier to read up
# front). One mode chosen per launch via random.choice.
_WAVE_MODE_REVEAL = "reveal"
_WAVE_MODE_BRIGHTEN = "brighten"

# Theme colors. Match main app's dark-charcoal palette so splash and
# main window read as one piece.
_BG_COLOR = QColor("#1a1a1f")
_TEXT_PRIMARY = QColor("#e8e8f0")
_TEXT_DIM = QColor("#a0a0b0")
_TEXT_DIMMER = QColor("#7a7a88")
_GLOW_COLOR = QColor("#c0392b")  # Stone-mode red — foreshadowing


class _RotatingLogo(QWidget):
    """Renders the alchemical-circle SVG with a slow rotation animation
    on a custom `rotation` property. Used as the splash's hero element."""

    def __init__(self, svg_path: Path, size_px: int, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(size_px, size_px)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._renderer = QSvgRenderer(str(svg_path)) if svg_path.exists() else None
        self._size_px = size_px
        self._rotation = 0.0

    def get_rotation(self) -> float:
        return self._rotation

    def set_rotation(self, value: float) -> None:
        self._rotation = value
        self.update()

    rotation = Property(float, get_rotation, set_rotation)

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._renderer is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # Rotate around center.
        center = QPointF(self._size_px / 2, self._size_px / 2)
        painter.translate(center)
        painter.rotate(self._rotation)
        painter.translate(-center)
        self._renderer.render(painter, QRectF(0, 0, self._size_px, self._size_px))
        painter.end()


class _LetterLabel(QLabel):
    """A single-letter QLabel with animatable font size + bold weight.

    Used for each character of the Vitriol wordmark so the wave animation
    can bold + grow each letter individually. Exposes `font_size` and
    `bold_amount` (0.0–1.0) as Qt properties for QPropertyAnimation."""

    def __init__(self, ch: str, base_size: int, parent=None) -> None:
        super().__init__(ch, parent)
        self._ch = ch
        self._base_size = base_size
        self._font_size = float(base_size)
        self._bold_amount = 0.0  # 0 = normal weight, 1 = bold
        self.setStyleSheet(f"color: {_TEXT_PRIMARY.name()};")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_font()

    def _refresh_font(self) -> None:
        from .main_window import _ensure_cinzel_loaded
        family = _ensure_cinzel_loaded() or "Serif"
        f = QFont(family, int(round(self._font_size)))
        # Interpolate weight: 400 (normal) at 0.0 → 700 (bold) at 1.0
        weight = int(400 + 300 * self._bold_amount)
        f.setWeight(QFont.Weight(weight))
        self.setFont(f)

    def get_font_size(self) -> float:
        return self._font_size

    def set_font_size(self, value: float) -> None:
        self._font_size = value
        self._refresh_font()

    font_size = Property(float, get_font_size, set_font_size)

    def get_bold_amount(self) -> float:
        return self._bold_amount

    def set_bold_amount(self, value: float) -> None:
        self._bold_amount = max(0.0, min(1.0, value))
        self._refresh_font()

    bold_amount = Property(float, get_bold_amount, set_bold_amount)


class _LatinWord(QLabel):
    """A Latin-motto word with animatable bold + opacity. Mode A starts
    invisible and fades in; Mode B starts dim (0.6) and brightens."""

    def __init__(self, word: str, base_size: int, parent=None) -> None:
        super().__init__(word, parent)
        self._base_size = base_size
        self._bold_amount = 0.0
        self._color = _TEXT_DIM
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._effect = QGraphicsOpacityEffect(self)
        self._effect.setOpacity(0.0)
        self.setGraphicsEffect(self._effect)
        self._refresh_font()

    def _refresh_font(self) -> None:
        from .main_window import _ensure_cinzel_loaded
        family = _ensure_cinzel_loaded() or "Serif"
        f = QFont(family, self._base_size)
        f.setItalic(True)
        weight = int(400 + 300 * self._bold_amount)
        f.setWeight(QFont.Weight(weight))
        self.setFont(f)
        self.setStyleSheet(f"color: {self._color.name()};")

    def opacity_effect(self) -> QGraphicsOpacityEffect:
        return self._effect

    def get_bold_amount(self) -> float:
        return self._bold_amount

    def set_bold_amount(self, value: float) -> None:
        self._bold_amount = max(0.0, min(1.0, value))
        self._refresh_font()

    bold_amount = Property(float, get_bold_amount, set_bold_amount)


class VitriolSplash(QSplashScreen):
    """Branded splash screen shown during cold start.

    Construct, call `show()`, then connect `animation_finished` to a slot
    that closes the splash + shows the main window. The signal fires
    after the full 3 s ceremony OR, if the user clicked to skip, after
    the 150 ms cut-to-end fade-out completes. Either way the receiver
    sees one well-defined "splash is done, show main window now" event.

    Pattern in main.py:
        splash = VitriolSplash()
        splash.show()
        # build main window in parallel...
        win = MainWindow()
        # then defer win.show() until splash is done
        splash.animation_finished.connect(lambda: (splash.close(), win.show()))
    """

    # Emitted exactly once when the splash has finished animating —
    # either after the natural 3 s ceremony, or after the click-to-skip
    # fade-out. Receivers should close the splash and show the main
    # window in response. Safe to connect after `show()`.
    animation_finished = Signal()

    def __init__(self) -> None:
        # Build a flat, transparent pixmap for QSplashScreen's base; we
        # paint everything via child QWidgets stacked on top.
        base = QPixmap(_SPLASH_W, _SPLASH_H)
        base.fill(_BG_COLOR)
        super().__init__(
            base,
            Qt.WindowType.SplashScreen
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint,
        )
        self.setFixedSize(_SPLASH_W, _SPLASH_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

        self._wave_mode = random.choice([_WAVE_MODE_REVEAL, _WAVE_MODE_BRIGHTEN])
        self._dismissed = False
        self._animation_root: QSequentialAnimationGroup | None = None

        self._build_layout()
        self._center_on_cursor_screen()
        self._build_animations()

    # --- layout ------------------------------------------------------------

    def _build_layout(self) -> None:
        # Build all child widgets as direct children with absolute positioning
        # rather than a layout — animations need precise control over per-
        # element position and size.

        # Alchemical-circle logo, top-center.
        svg_path = resources_dir() / "logo.svg"
        self._logo = _RotatingLogo(svg_path, 140, self)
        self._logo.move((_SPLASH_W - 140) // 2, 30)
        self._logo_effect = QGraphicsOpacityEffect(self._logo)
        self._logo_effect.setOpacity(0.0)
        self._logo.setGraphicsEffect(self._logo_effect)

        # Red glow effect on the logo for the foreshadowing pulse.
        # Set up but blur radius starts at 0; animated up to ~40 then back.
        self._glow = QGraphicsDropShadowEffect(self._logo)
        self._glow.setColor(_GLOW_COLOR)
        self._glow.setOffset(0, 0)
        self._glow.setBlurRadius(0)
        # NB: a widget can have only ONE graphics effect at a time — Qt
        # silently replaces. We swap to glow during the pulse phase.

        # Wordmark: 7 letters as individual _LetterLabel widgets so the
        # wave can animate per-letter. Initial positioning at tight
        # tracking, will animate apart in phase 2.
        self._letters: list[_LetterLabel] = [
            _LetterLabel(ch, 36, self) for ch in _VITRIOL_LETTERS
        ]
        for lbl in self._letters:
            eff = QGraphicsOpacityEffect(lbl)
            eff.setOpacity(0.0)
            lbl.setGraphicsEffect(eff)
            # Store the effect on the label so animations can find it.
            lbl._opacity_effect = eff
        self._position_letters(tracking=0)
        self._wordmark_y = 195  # initial Y; flourish moves it toward center

        # Latin motto: 7 words, single line, just below the wordmark.
        self._latin_words: list[_LatinWord] = [
            _LatinWord(w, 13, self) for w in _LATIN_WORDS
        ]
        # Mode B starts the words pre-dimmed at 0.6 opacity for the
        # "brighten" choreography. Mode A starts at 0.0 and fades in.
        starting_opacity = 0.6 if self._wave_mode == _WAVE_MODE_BRIGHTEN else 0.0
        for w in self._latin_words:
            w.opacity_effect().setOpacity(starting_opacity)
        self._position_latin_words(y=265)

        # English translation — two lines beneath the Latin.
        from .main_window import _ensure_cinzel_loaded
        family = _ensure_cinzel_loaded() or "Serif"
        self._eng_line1 = QLabel(_ENGLISH_LINE_1, self)
        self._eng_line2 = QLabel(_ENGLISH_LINE_2, self)
        for lbl, y in [(self._eng_line1, 305), (self._eng_line2, 325)]:
            f = QFont(family, 11)
            f.setItalic(True)
            lbl.setFont(f)
            lbl.setStyleSheet(f"color: {_TEXT_DIMMER.name()};")
            lbl.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.adjustSize()
            lbl.move((_SPLASH_W - lbl.width()) // 2, y)
            eff = QGraphicsOpacityEffect(lbl)
            eff.setOpacity(0.0)
            lbl.setGraphicsEffect(eff)
            lbl._opacity_effect = eff

    def _position_letters(self, tracking: float) -> None:
        """Lay out the 7 wordmark letters horizontally with the given
        extra letter-spacing in pixels. tracking=0 = tight, tracking=15 ≈
        wide acronym-style spacing."""
        # Measure each letter's actual width so kerning is roughly right.
        widths = []
        for lbl in self._letters:
            lbl.adjustSize()
            widths.append(lbl.width())
        total = sum(widths) + tracking * (len(widths) - 1)
        x = (_SPLASH_W - total) // 2
        # Vertical center within an imaginary 50-px band starting at y=180.
        y_base = 180
        max_h = max(lbl.height() for lbl in self._letters)
        for lbl, w in zip(self._letters, widths):
            # Re-center vertically as font size grows during the wave.
            lbl.move(x, y_base + (max_h - lbl.height()) // 2)
            x += w + int(tracking)

    def _position_latin_words(self, y: int) -> None:
        """Position 7 Latin words horizontally on a single line, centered."""
        gap = 8  # px between words
        widths = []
        for w in self._latin_words:
            w.adjustSize()
            widths.append(w.width())
        total = sum(widths) + gap * (len(widths) - 1)
        x = (_SPLASH_W - total) // 2
        for w, width in zip(self._latin_words, widths):
            w.move(x, y)
            x += width + gap

    def _center_on_cursor_screen(self) -> None:
        """Center the splash on the screen containing the cursor (where
        the user just clicked the launcher). Falls back to primary."""
        cursor_pos = QCursor.pos()
        screen = QGuiApplication.screenAt(cursor_pos)
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.x() + (geo.width() - _SPLASH_W) // 2,
            geo.y() + (geo.height() - _SPLASH_H) // 2,
        )

    # --- animation construction -------------------------------------------

    def _build_animations(self) -> None:
        """Build the full 6.5 s animation tree as a single
        QSequentialAnimationGroup so it can be cleanly canceled mid-flight
        when the user clicks to skip."""
        root = QSequentialAnimationGroup(self)

        # Phase 1 (0.00 – 0.30 s): logo + background fade in, rotation starts.
        phase1 = QParallelAnimationGroup(self)
        a = QPropertyAnimation(self._logo_effect, b"opacity", self)
        a.setDuration(300)
        a.setStartValue(0.0)
        a.setEndValue(1.0)
        phase1.addAnimation(a)
        # Slow rotation: 360° over 12000 ms — slowed from the original
        # 8000 ms so the 5 s splash shows ~150° of rotation rather than
        # ~225°. Less spinning, more deliberate. Continuous, runs in
        # parallel with the rest of the timeline.
        rot = QPropertyAnimation(self._logo, b"rotation", self)
        rot.setDuration(12000)
        rot.setStartValue(0.0)
        rot.setEndValue(360.0)
        rot.setEasingCurve(QEasingCurve.Type.Linear)
        rot.start()  # detached; runs independently of root
        self._rotation_anim = rot
        root.addAnimation(phase1)

        # Phase 2a (0.30 – 0.50 s): wordmark fades in at tight tracking.
        phase2a = QParallelAnimationGroup(self)
        for lbl in self._letters:
            a = QPropertyAnimation(lbl._opacity_effect, b"opacity", self)
            a.setDuration(200)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            phase2a.addAnimation(a)
        root.addAnimation(phase2a)

        # Phase 2b (0.50 – 0.85 s): letters animate apart (tracking 0 → 15 px).
        # Implemented as a QPropertyAnimation on a custom float property
        # of self that calls _position_letters each step.
        self._tracking = 0.0
        phase2b = QPropertyAnimation(self, b"tracking_value", self)
        phase2b.setDuration(350)
        phase2b.setStartValue(0.0)
        phase2b.setEndValue(15.0)
        phase2b.setEasingCurve(QEasingCurve.Type.OutCubic)
        root.addAnimation(phase2b)

        # Phase 3 (0.85 – 2.25 s): synchronized wave, 7 pairs × 200 ms.
        # Each pair gets ~50% more time than the original 130 ms so the
        # letter-to-Latin-word pairing is comfortably readable.
        phase3 = QSequentialAnimationGroup(self)
        for letter, word in zip(self._letters, self._latin_words):
            pair = self._build_wave_pair(letter, word)
            phase3.addAnimation(pair)
        root.addAnimation(phase3)

        # Phase 4 (2.25 – 2.95 s): English translation fades in line-by-line.
        # Sequential rather than simultaneous — each phrase arrives as its
        # own beat so the user reads the English in the same rhythm they
        # might speak the Latin. 350 ms per line × 2 lines = 700 ms total.
        phase4 = QSequentialAnimationGroup(self)
        for lbl in (self._eng_line1, self._eng_line2):
            a = QPropertyAnimation(lbl._opacity_effect, b"opacity", self)
            a.setDuration(350)
            a.setStartValue(0.0)
            a.setEndValue(1.0)
            phase4.addAnimation(a)
        root.addAnimation(phase4)

        # Phase 4.5 (2.95 – 5.15 s): hold the full motto for ~2.2 s. No
        # animation — Latin + English sit visible while the alchemical-
        # circle rotation continues underneath. This long hold serves
        # two purposes:
        #
        #   1. Pedagogical: the user gets enough time to read the whole
        #      motto comfortably (twice, even, at normal reading speed).
        #   2. Perceptual: framed against the slowly-rotating logo, this
        #      reads as "the app is still loading" rather than "this
        #      ceremony is gratuitously long." The rotation provides the
        #      progress-cue; the static motto provides the meaning.
        #
        # Calibrated empirically — under ~1.5 s the motto feels rushed,
        # over ~3 s the splash starts to feel actually slow rather than
        # purposefully deliberate.
        hold_motto = QPauseAnimation(2200, self)
        root.addAnimation(hold_motto)

        # Phase 5 (5.15 – 5.85 s): the flourish.
        # Vitriol bolds + tracking contracts to 0 + font grows to ~52pt +
        # vertical position translates toward splash center. Latin +
        # English fade out simultaneously. Glow pulse on the logo.
        phase5 = QParallelAnimationGroup(self)
        # Bold + size-grow on each letter
        for lbl in self._letters:
            ab = QPropertyAnimation(lbl, b"bold_amount", self)
            ab.setDuration(700)
            ab.setStartValue(0.0)
            ab.setEndValue(1.0)
            phase5.addAnimation(ab)
            asz = QPropertyAnimation(lbl, b"font_size", self)
            asz.setDuration(700)
            asz.setStartValue(36.0)
            asz.setEndValue(52.0)
            asz.setEasingCurve(QEasingCurve.Type.OutCubic)
            phase5.addAnimation(asz)
        # Tracking contracts back to 0
        atrack = QPropertyAnimation(self, b"tracking_value", self)
        atrack.setDuration(700)
        atrack.setStartValue(15.0)
        atrack.setEndValue(0.0)
        atrack.setEasingCurve(QEasingCurve.Type.OutCubic)
        phase5.addAnimation(atrack)
        # Latin + English fade out
        for w in self._latin_words:
            af = QPropertyAnimation(w.opacity_effect(), b"opacity", self)
            af.setDuration(700)
            af.setStartValue(1.0)
            af.setEndValue(0.0)
            phase5.addAnimation(af)
        for lbl in (self._eng_line1, self._eng_line2):
            af = QPropertyAnimation(lbl._opacity_effect, b"opacity", self)
            af.setDuration(700)
            af.setStartValue(1.0)
            af.setEndValue(0.0)
            phase5.addAnimation(af)
        # Glow pulse: install the dropshadow effect, animate blur 0→40→0
        # over 500 ms. Replaces the opacity effect on the logo, so we
        # restore opacity-1 manually after.
        # NOTE: Qt only allows one QGraphicsEffect per widget. Instead
        # of swapping (which would lose the fade-in), we approximate the
        # glow with a separate animation: vary the logo's opacity peak
        # subtly. Compromise — true glow needs a separate widget layer.
        # For v1, skip the dropshadow swap and just leave the logo
        # rendered cleanly. Glow can be added later as a second widget
        # behind the logo if visual upgrade is wanted.
        root.addAnimation(phase5)

        # Phase 6 (5.85 – 6.50 s): hold the climactic Vitriol briefly,
        # then fade entire splash to 0. Implemented by animating
        # self.windowOpacity. 650 ms total — long enough for the
        # climactic state to land before the splash dissolves.
        hold = QPropertyAnimation(self, b"windowOpacity", self)
        hold.setDuration(650)
        hold.setStartValue(1.0)
        hold.setEndValue(0.0)
        hold.setEasingCurve(QEasingCurve.Type.InCubic)
        root.addAnimation(hold)

        root.finished.connect(self._on_animation_finished)
        self._animation_root = root

    def _build_wave_pair(self, letter: _LetterLabel,
                           word: _LatinWord) -> QParallelAnimationGroup:
        """Construct one letter-and-word pair animation for the wave.

        200 ms total: 90 ms emphasize + 110 ms relax (slowed from the
        original 60+70 = 130 ms — the eye couldn't keep up at the
        faster pace, so the educational pairing of letter→word was
        being missed). During emphasize the letter bolds and grows
        ~10%, and the paired Latin word either fades in (Mode A) or
        brightens to opacity 1.0 + bolds (Mode B). Then both relax
        back to neutral state.
        """
        pair = QParallelAnimationGroup(self)

        # Letter: bold up + grow then back
        seq_letter = QSequentialAnimationGroup(self)
        ab_up = QPropertyAnimation(letter, b"bold_amount", self)
        ab_up.setDuration(90)
        ab_up.setStartValue(0.0)
        ab_up.setEndValue(0.7)
        seq_letter.addAnimation(ab_up)
        ab_dn = QPropertyAnimation(letter, b"bold_amount", self)
        ab_dn.setDuration(110)
        ab_dn.setStartValue(0.7)
        ab_dn.setEndValue(0.0)
        seq_letter.addAnimation(ab_dn)
        pair.addAnimation(seq_letter)

        # Letter font size: 36 → 39.5 → 36
        seq_size = QSequentialAnimationGroup(self)
        as_up = QPropertyAnimation(letter, b"font_size", self)
        as_up.setDuration(90)
        as_up.setStartValue(36.0)
        as_up.setEndValue(39.5)
        seq_size.addAnimation(as_up)
        as_dn = QPropertyAnimation(letter, b"font_size", self)
        as_dn.setDuration(110)
        as_dn.setStartValue(39.5)
        as_dn.setEndValue(36.0)
        seq_size.addAnimation(as_dn)
        pair.addAnimation(seq_size)

        # Latin word — Mode A fades in to 1.0 (and stays there), Mode B
        # brightens from 0.6 → 1.0 + bolds, then settles back to 1.0
        # without fading. Both modes leave the word visible at the end.
        if self._wave_mode == _WAVE_MODE_REVEAL:
            af = QPropertyAnimation(word.opacity_effect(), b"opacity", self)
            af.setDuration(200)
            af.setStartValue(0.0)
            af.setEndValue(1.0)
            pair.addAnimation(af)
        else:  # brighten
            af = QPropertyAnimation(word.opacity_effect(), b"opacity", self)
            af.setDuration(200)
            af.setStartValue(0.6)
            af.setEndValue(1.0)
            pair.addAnimation(af)
            seq_word = QSequentialAnimationGroup(self)
            bw_up = QPropertyAnimation(word, b"bold_amount", self)
            bw_up.setDuration(90)
            bw_up.setStartValue(0.0)
            bw_up.setEndValue(0.5)
            seq_word.addAnimation(bw_up)
            bw_dn = QPropertyAnimation(word, b"bold_amount", self)
            bw_dn.setDuration(110)
            bw_dn.setStartValue(0.5)
            bw_dn.setEndValue(0.0)
            seq_word.addAnimation(bw_dn)
            pair.addAnimation(seq_word)

        return pair

    # --- tracking property (drives _position_letters during anim) ---------

    def get_tracking_value(self) -> float:
        return self._tracking

    def set_tracking_value(self, value: float) -> None:
        self._tracking = value
        self._position_letters(tracking=value)

    tracking_value = Property(float, get_tracking_value, set_tracking_value)

    # --- lifecycle --------------------------------------------------------

    def start_animation(self) -> None:
        if self._animation_root is not None:
            self._animation_root.start()

    def _on_animation_finished(self) -> None:
        # Stop the rotation (it runs detached from the root group, so
        # finishing the root doesn't auto-stop it). Then signal that
        # we're done — the receiver in main.py is responsible for
        # closing us and showing the main window.
        if hasattr(self, "_rotation_anim"):
            self._rotation_anim.stop()
        if not self._dismissed:
            self._dismissed = True
            self.animation_finished.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        """Click anywhere → cut to fade-out, then signal done.

        Same end-state as the natural-completion path: fires
        `animation_finished` so the main.py receiver closes the splash
        and shows the main window. The 150 ms fade-out covers the
        transition; without it the cut would feel abrupt."""
        if self._dismissed:
            return
        self._dismissed = True
        if self._animation_root is not None:
            self._animation_root.stop()
        if hasattr(self, "_rotation_anim"):
            self._rotation_anim.stop()
        # Quick 150 ms fade-out, then emit done.
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(150)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.finished.connect(self.animation_finished.emit)
        fade.start()
        self._dismiss_anim = fade  # keep reference

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Defer animation start to the next event-loop tick so the splash
        # is actually painted before animations begin.
        QTimer.singleShot(0, self.start_animation)
