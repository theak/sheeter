/* Sheeter overlay.
 *
 * Reads the global SHEETER_DATA (the analysis document, see docs/schema.md) and puts one
 * grouped label per event per hand on top of the straightened photo.
 *
 * Two modes:
 *   all  - every event's labels at once.  Works when the image is rendered wide enough that
 *          the labels do not touch.
 *   step - one event at a time, with a numbered marker on the image for every event.  This is
 *          the phone case: a 1855px image in a 350px viewport is scaled about 0.19x, and four
 *          events times two hands of multi-line labels cannot all fit at a readable size.
 * The starting mode is decided by measuring the laid out label boxes, not by a breakpoint.
 *
 * Label geometry lives in stage pixels (the image at zoom 1).  #labels sits inside #stage, so
 * a zoom scales the labels along with the photo; overlap is therefore scale invariant and only
 * has to be re-measured when the viewport width or the label size changes.
 *
 * Placement invariant, which is what keeps labels off the viewport edges: layoutLabels() clamps
 * every pill fully inside the stage rectangle, and applyTransform() never lets a stage edge move
 * inside the viewport.  So a pill is either wholly visible or panned past, never half cut off.
 *
 * Per-notehead labels were tried and do not work: a five note stack at unit 20px, scaled into a
 * 390px viewport, puts them 6px apart.
 */
(function () {
  'use strict';

  var doc = window.SHEETER_DATA;
  var root = document.documentElement;
  root.classList.add('js');

  var viewport = document.getElementById('viewport');
  var stage = document.getElementById('stage');
  var sheet = document.getElementById('sheet');
  var labels = document.getElementById('labels');
  var fixes = document.getElementById('fixes');
  var stageEmpty = document.getElementById('stage-empty');
  var controls = document.querySelector('.stage-controls');
  var stepBar = document.getElementById('step-bar');
  var stepStatus = document.getElementById('step-status');
  var stepPrev = document.getElementById('step-prev');
  var stepNext = document.getElementById('step-next');

  if (!doc || !viewport || !stage || !labels) { return; }

  var imageWidth = (doc.image && doc.image.width) || 0;
  var imageHeight = (doc.image && doc.image.height) || 0;
  var systems = doc.systems || [];

  // ---------------------------------------------------------------- helpers

  function store(key, value) {
    try { window.localStorage.setItem('sheeter.' + key, value); } catch (e) { /* private mode */ }
  }

  function recall(key, fallback) {
    try {
      var found = window.localStorage.getItem('sheeter.' + key);
      return found === null ? fallback : found;
    } catch (e) {
      return fallback;
    }
  }

  function clamp(value, low, high) {
    return value < low ? low : (value > high ? high : value);
  }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function staffOf(system, index) {
    var staves = system.staves || [];
    for (var i = 0; i < staves.length; i++) {
      if (staves[i].index === index) { return staves[i]; }
    }
    return null;
  }

  function handName(part, staff) {
    if (part.hand === 'right') { return 'Right hand'; }
    if (part.hand === 'left') { return 'Left hand'; }
    if (staff && staff.clef) { return staff.clef.charAt(0).toUpperCase() + staff.clef.slice(1) + ' staff'; }
    return 'Staff ' + (part.staff + 1);
  }

  function handTag(part, staff) {
    if (part.hand === 'right') { return 'R'; }
    if (part.hand === 'left') { return 'L'; }
    if (staff && staff.clef) { return staff.clef.charAt(0).toUpperCase() + staff.clef.slice(1); }
    return '?';
  }

  function handTone(part, staff) {
    if (part.hand === 'left') { return 'l'; }
    if (part.hand === 'right') { return 'r'; }
    return (staff && staff.clef === 'bass') ? 'l' : 'r';
  }

  // "G\u266f 4" rather than "G\u266f4": the octave is a separate thing from the note, and
  // at label size the two run together.  Done at display time so every stored reading
  // shows the same way, and the JSON keeps the compact form the rest of the code reads.
  function spaced(name) {
    return String(name).replace(/(\d+)$/, ' $1');
  }

  // Note names read top to bottom on the page, and the schema sorts them low to high.
  function displayNames(part) {
    var out = [];
    for (var i = part.notes.length - 1; i >= 0; i--) {
      out.push(spaced(part.notes[i].pretty || part.notes[i].name));
    }
    return out;
  }

  // ---------------------------------------------------------------- events

  var steps = [];
  var pills = [];
  var marks = [];

  // Everything drawn over the photo, built from the document as it stands and thrown
  // away and built again when a correction lands.  Rebuilt rather than patched because
  // an edit can rename the chord, move a notehead to another staff position or take one
  // out of the reading, and each of those changes a label's text, its anchor, or both.
  // Returns how many steps there are, which is zero on a page with no noteheads.
  function buildOverlay() {
    labels.innerHTML = '';
    steps = [];
    pills = [];
    marks = [];
    systems.forEach(function (system) {
      (system.events || []).forEach(function (event) {
        steps.push({ system: system, event: event, number: steps.length + 1 });
      });
    });
    if (!steps.length) { return 0; }
    buildMarks();
    buildPills();
    return steps.length;
  }

  if (!systems.length || !buildOverlay()) {
    if (stageEmpty) {
      stageEmpty.textContent = systems.length
        ? 'Staff lines were found, but no noteheads on them. Try a sharper photo, or one taken square on to the page.'
        : 'No staff lines were found in this photo. Try again closer in, with the music filling the frame and the page flat.';
      stageEmpty.hidden = false;
    }
    if (controls) { controls.hidden = true; }
    if (!systems.length) { viewport.hidden = true; }
    return;
  }

  // Staff lines, not y_range: on a two system page the systems' y_ranges overlap, and a lone
  // system's y_range can start above the top of the image.
  function systemGeometry(system) {
    if (system.__geom) { return system.__geom; }
    var staves = (system.staves || []).slice().sort(function (a, b) {
      return (a.lines && a.lines[0] ? a.lines[0] : 0) - (b.lines && b.lines[0] ? b.lines[0] : 0);
    });
    var top = null;
    var bottom = null;
    var unit = 0;
    staves.forEach(function (staff) {
      var lines = staff.lines || [];
      if (lines.length) {
        top = top === null ? lines[0] : Math.min(top, lines[0]);
        bottom = bottom === null ? lines[lines.length - 1] : Math.max(bottom, lines[lines.length - 1]);
      }
      if (staff.unit) { unit = Math.max(unit, staff.unit); }
    });
    if (top === null) {
      top = (system.y_range || [0, imageHeight])[0];
      bottom = (system.y_range || [0, imageHeight])[1];
    }
    if (!unit) { unit = Math.max(6, (bottom - top) / 8); }

    // The numbered dot goes in the gap between the two staves of a grand staff, which is the
    // one part of a system that is reliably empty.  A lone staff gets it underneath.
    var dotY = bottom + unit * 1.6;
    var below = unit * 2.4;
    if (staves.length > 1) {
      var upper = staves[0].lines || [];
      var lower = staves[1].lines || [];
      if (upper.length && lower.length) {
        dotY = (upper[upper.length - 1] + lower[0]) / 2;
        below = unit * 0.8;
      }
    }

    system.__geom = {
      top: top,
      bottom: bottom,
      unit: unit,
      dotY: clamp(dotY, unit, Math.max(unit, imageHeight - unit)),
      bandTop: clamp(top - unit * 0.8, 0, imageHeight),
      bandBottom: clamp(bottom + below, 0, imageHeight),
      // What a zoom has to keep on screen: the staves plus room for a chord label above and
      // the numbered dot below.
      frameTop: clamp(top - unit * 3.2, 0, imageHeight),
      frameBottom: clamp(bottom + unit * 2.0, 0, imageHeight)
    };
    return system.__geom;
  }

  // ---------------------------------------------------------------- markers and labels

  function percentX(x) { return imageWidth ? clamp(x / imageWidth * 100, 0, 100) : 0; }
  function percentY(y) { return imageHeight ? clamp(y / imageHeight * 100, 0, 100) : 0; }

  // The numbered dot for every event, sitting in the gap of the grand staff.
  function buildMarks() {
    steps.forEach(function (step) {
      var geom = systemGeometry(step.system);
      var combined = step.event.combined || {};
      var mark = el('button', 'step-mark');
      mark.type = 'button';
      mark.dataset.step = String(step.number);
      mark.style.left = percentX(step.event.x) + '%';
      mark.style.top = percentY(geom.bandTop) + '%';
      mark.style.height = Math.max(0, percentY(geom.bandBottom) - percentY(geom.bandTop)) + '%';
      var dot = el('span', 'step-dot', String(step.number));
      var span = Math.max(1, geom.bandBottom - geom.bandTop);
      dot.style.top = clamp((geom.dotY - geom.bandTop) / span * 100, 0, 100) + '%';
      mark.appendChild(dot);
      mark.setAttribute('aria-label', 'Step ' + step.number + ' of ' + steps.length +
        (combined.symbol ? ', ' + combined.symbol : ''));
      mark.addEventListener('click', function () { select(step.number, 'stage', true); });
      labels.appendChild(mark);
      marks.push(mark);
    });
  }

  var NAME_STEP_PX = 4;

  function makePill(className, tagText, lines, step, anchor) {
    var pill = el('button', 'pill ' + className);
    pill.type = 'button';
    pill.appendChild(el('span', 'tag', tagText));
    var names = el('span', 'names');
    lines.forEach(function (line, index) {
      var name = el('span', null, line);
      // The bottom line sits flush and each one above it steps right by NAME_STEP_PX, so a
      // chord's notes read left to right as well as top to bottom.  Pixels rather than em
      // on purpose: the step is a reading aid, not part of the type, and it stays legible
      // at the smallest label size without growing into a stair at the largest.
      var above = lines.length - 1 - index;
      if (above) { name.style.marginLeft = (above * NAME_STEP_PX) + 'px'; }
      names.appendChild(name);
    });
    pill.appendChild(names);
    pill.dataset.step = String(step.number);
    pill.setAttribute('aria-label',
      'Step ' + step.number + ', ' + tagText + ', ' + lines.join(' '));
    if ((step.event.confidence || 0) < 0.7) { pill.classList.add('low'); }
    pill.addEventListener('click', function () { select(step.number, 'stage', true); });
    pill.__anchor = anchor;
    labels.appendChild(pill);
    pills.push(pill);
    return pill;
  }

  // One label per hand per event, plus the chord symbol over each: what the reader
  // actually reads off the photo.
  function buildPills() {
    steps.forEach(function (step) {
      var system = step.system;
      var event = step.event;
      var geom = systemGeometry(system);

      (event.parts || []).forEach(function (part) {
        if (!part.notes || !part.notes.length) { return; }
        var staff = staffOf(system, part.staff);
        var left = part.notes[0].x;
        var right = part.notes[0].x;
        var top = part.notes[0].y;
        var bottom = part.notes[0].y;
        part.notes.forEach(function (note) {
          var halfWidth = (note.w || 0) / 2;
          left = Math.min(left, note.x - halfWidth);
          right = Math.max(right, note.x + halfWidth);
          top = Math.min(top, note.y);
          bottom = Math.max(bottom, note.y);
        });
        var gap = (staff && staff.unit ? staff.unit : geom.unit) * 0.6;
        makePill('hand-' + handTone(part, staff), handTag(part, staff), displayNames(part), step,
                 { kind: 'hand', left: left - gap, right: right + gap, cy: (top + bottom) / 2 });
      });

      var symbol = event.combined && event.combined.symbol;
      if (symbol) {
        // Clear of this event's own noteheads where the photo has room for it, otherwise just
        // clear of the staff, otherwise underneath the system.  layoutLabels() picks.
        var highest = geom.top;
        (event.parts || []).forEach(function (part) {
          (part.notes || []).forEach(function (note) {
            highest = Math.min(highest, note.y - (note.h || 0) / 2);
          });
        });
        makePill('chord', 'C', [symbol], step, {
          kind: 'chord',
          x: event.x,
          raised: highest - geom.unit * 0.7,
          above: geom.top - geom.unit * 0.7,
          below: geom.bottom + geom.unit * 0.7
        });
      }
    });
  }

  // ---------------------------------------------------------------- layout

  var EDGE = 3;

  // A chord symbol with no headroom above the staff lands on its own hand labels, which hides
  // the note names.  Slide it clear of them; a step's hand labels are always laid out before
  // its chord, so their boxes are already known.
  function dodge(pill, x, y, size, width) {
    var left = null;
    var right = 0;
    for (var i = 0; i < pills.length; i++) {
      var box = pills[i].__box;
      if (!box || pills[i] === pill || pills[i].dataset.step !== pill.dataset.step) { continue; }
      if (box.t >= y + size.h || box.b <= y) { continue; }
      if (box.l >= x + size.w || box.r <= x) { continue; }
      left = left === null ? box.l : Math.min(left, box.l);
      right = Math.max(right, box.r);
    }
    if (left === null) { return x; }
    if (left - EDGE - size.w >= EDGE) { return left - EDGE - size.w; }
    if (right + EDGE + size.w <= width - EDGE) { return right + EDGE; }
    return x;
  }

  // Sets every visible pill's box in stage pixels and records it for the overlap test.  Pills
  // are placed beside their notes and then clamped inside the stage, so nothing is ever cut off
  // by the viewport edge; a chord symbol with no headroom above the staff drops below it.
  function layoutLabels() {
    var width = labels.clientWidth;
    var height = labels.clientHeight;
    if (!width || !height || !imageWidth) { return; }
    var k = width / imageWidth;
    var sizes = [];
    var i;

    for (i = 0; i < pills.length; i++) {
      sizes.push(pills[i].offsetParent === null
        ? null
        : { w: pills[i].offsetWidth, h: pills[i].offsetHeight });
    }

    for (i = 0; i < pills.length; i++) {
      var pill = pills[i];
      var size = sizes[i];
      if (!size || !size.w) { pill.__box = null; continue; }
      var anchor = pill.__anchor;
      var x;
      var y;
      if (anchor.kind === 'hand') {
        x = anchor.right * k;
        if (x + size.w > width - EDGE) { x = anchor.left * k - size.w; }
        y = anchor.cy * k - size.h / 2;
      } else {
        x = anchor.x * k - size.w / 2;
        y = anchor.raised * k - size.h;
        if (y < EDGE) { y = anchor.above * k - size.h; }
        if (y < EDGE) { y = anchor.below * k; }
        x = dodge(pill, x, y, size, width);
      }
      x = clamp(x, EDGE, Math.max(EDGE, width - size.w - EDGE));
      y = clamp(y, EDGE, Math.max(EDGE, height - size.h - EDGE));
      pill.style.left = x + 'px';
      pill.style.top = y + 'px';
      pill.__box = { l: x, t: y, r: x + size.w, b: y + size.h, w: size.w, h: size.h };
    }
  }

  // A touch of overlap between two labels is fine; a fifth of the smaller box is a pile-up.
  // Proportional rather than a pixel count so the answer does not change with the size slider.
  function anyOverlap() {
    for (var i = 0; i < pills.length; i++) {
      var a = pills[i].__box;
      if (!a) { continue; }
      for (var j = i + 1; j < pills.length; j++) {
        var b = pills[j].__box;
        if (!b) { continue; }
        var across = Math.min(a.r, b.r) - Math.max(a.l, b.l);
        var down = Math.min(a.b, b.b) - Math.max(a.t, b.t);
        if (across > 0.2 * Math.min(a.w, b.w) && down > 0.2 * Math.min(a.h, b.h)) { return true; }
      }
    }
    return false;
  }

  // ---------------------------------------------------------------- mode

  var mode = 'all';
  var chosenByUser = false;

  function applyModeClass(name) {
    labels.classList.toggle('mode-step', name === 'step');
    labels.classList.toggle('mode-all', name !== 'step');
  }

  function wouldCollide() {
    var wasStep = mode === 'step';
    if (wasStep) { applyModeClass('all'); }
    layoutLabels();
    var hit = anyOverlap();
    if (wasStep) {
      applyModeClass('step');
      layoutLabels();
    }
    return hit;
  }

  function setMode(next, remember) {
    mode = next === 'step' ? 'step' : 'all';
    applyModeClass(mode);
    var buttons = document.querySelectorAll('.mode-button');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(buttons[i].dataset.mode === mode));
    }
    if (stepBar) {
      stepBar.classList.add('on');
      stepBar.classList.toggle('step-mode', mode === 'step');
    }
    layoutLabels();
    if (remember) {
      chosenByUser = true;
      store('labelMode', mode);
      // Asking for every label at once on a dense reading otherwise gives labels stacked
      // on top of each other, and zooming cannot separate them: it scales the labels and
      // the gaps between them by the same amount.  Shrinking them can.  Sometimes no
      // size fits, and there is nothing to say about that: the mode buttons are right
      // there, and the reader can see the labels touching as well as we can.
      if (mode === 'all') { shrinkToFit(); }
    }
    if (mode === 'step') {
      if (selected) { frameStep(steps[selected - 1]); }
    } else if (remember) {
      fit();
    }
    updateStepBar();
  }

  function updateStepBar() {
    if (!stepStatus) { return; }
    if (selected) {
      var combined = steps[selected - 1].event.combined || {};
      stepStatus.textContent = 'Step ' + selected + ' of ' + steps.length +
        (combined.symbol ? ' - ' + combined.symbol : '');
    } else {
      stepStatus.textContent = steps.length + (steps.length === 1 ? ' step' : ' steps') + ' - pick one';
    }
    if (stepPrev) { stepPrev.disabled = selected <= 1; }
    if (stepNext) { stepNext.disabled = selected >= steps.length; }
  }

  // ---------------------------------------------------------------- fix panels

  var selected = 0;

  // Server rendered, one per step, all present in the page.  With scripting off that is
  // the whole feature: every panel is visible and labelled with its step.  With
  // scripting on, only the selected step's panel is shown, directly under the photo,
  // which is what makes it read as belonging to the step rather than to the page.
  var panels = fixes ? fixes.querySelectorAll('.fix-panel') : [];

  function showPanel(number) {
    for (var i = 0; i < panels.length; i++) {
      panels[i].hidden = Number(panels[i].dataset.step) !== number;
    }
  }

  // Move applies a pitch the reader picked from the menu, so until they pick a different
  // one there is nothing for it to apply and it only asks to be puzzled over.  Hidden
  // here rather than shown here, so with no scripting it is simply always there and the
  // menu still has its submit button: the direction that fails safe.
  function armPitchMenus(root) {
    var forms = root.querySelectorAll('.fix-pitch');
    for (var i = 0; i < forms.length; i++) {
      (function (group) {
        var menu = group.querySelector('select');
        var apply = group.querySelector('.fix-go');
        if (!menu || !apply) { return; }
        var was = menu.value;
        apply.hidden = true;
        menu.addEventListener('change', function () {
          apply.hidden = menu.value === was;
        });
      }(forms[i]));
    }
  }

  // The selected step, left in the address bar so that reloading comes back to it.  A
  // correction used to put it there by redirecting to #step-N; nothing navigates now, and
  // clicking through the steps never did.  replaceState and not location.hash, which
  // would scroll the page to the panel every time the reader pressed Next.
  function rememberStep(number) {
    if (!window.history || !window.history.replaceState) { return; }
    try {
      window.history.replaceState(null, '', '#step-' + number);
    } catch (e) { /* opened from a file, or a history that will not have it */ }
  }

  // A correction with no scripting posts back to #step-N, so the reader lands on the step
  // they were fixing rather than at the top of the page with nothing selected.
  function stepFromHash() {
    var match = /^#step-(\d+)$/.exec(window.location.hash || '');
    return match ? parseInt(match[1], 10) : 0;
  }


  // ---------------------------------------------------------------- playback

  var audio = window.SheeterAudio;
  var soundButton = document.getElementById('sound-toggle');
  // The stored value is the mute, so a reader who has never touched the button gets
  // sound.  Absent audio, muted stays true and nothing below ever tries to make a noise.
  var muted = !(audio && audio.supported) || recall('muted', '0') === '1';

  // Left hand before right, low to high inside each.  Not the same as sorting every note
  // by pitch: the hands can overlap, and a left-hand note sitting above a right-hand one
  // should still be struck first.  A lone staff has no hand at all, and then pitch is the
  // only thing left to order by, which this does by giving every note the same rank.
  var HAND_ORDER = { left: 0, right: 1 };

  function stepMidis(step) {
    var notes = [];
    (step.event.parts || []).forEach(function (part) {
      var hand = HAND_ORDER[part.hand];
      if (hand === undefined) { hand = 0; }
      (part.notes || []).forEach(function (note) {
        if (typeof note.midi === 'number') { notes.push({ hand: hand, midi: note.midi }); }
      });
    });
    notes.sort(function (a, b) { return a.hand - b.hand || a.midi - b.midi; });
    return notes.map(function (note) { return note.midi; });
  }

  function playStep(step) {
    if (muted || !audio || !audio.supported) { return; }
    audio.play(stepMidis(step));
  }

  // One note of a chord on its own.  play() hushes whatever is ringing first, which is
  // what "in isolation" has to mean here: the chord this note belongs to is usually still
  // sounding from the click that selected it.
  function playNote(midi) {
    if (muted || !audio || !audio.supported) { return; }
    audio.play([midi]);
  }

  // The play buttons in the fix panels, one per notehead.  They live there rather than on
  // the labels over the photo because that is where there is room for them: a label with
  // six notes stacked in it is 60px across on a phone, which leaves each note about 5x10
  // to aim at, and a tap that close to a button is given to the button by the browser's own
  // touch adjustment before any of this code sees it.  A panel row is already thumb sized.
  function hearButton(target) {
    return (target && target.closest) ? target.closest('.fix-hear') : null;
  }

  function buttonMidi(button) {
    var midi = parseInt(button.dataset.midi, 10);
    return isNaN(midi) ? null : midi;
  }

  // Disabled rather than hidden while the sound is off, so the row keeps its shape and
  // the button stops answering to a pointer instead of answering with silence; the
  // tooltip says why.  Re-queried on every call rather than held in a list: a corrected
  // panel is redrawn from the server, so its buttons are new nodes and a list taken once
  // would be pointing at the ones that have gone.
  function syncHear() {
    if (!fixes) { return; }
    var buttons = fixes.querySelectorAll('.fix-hear');
    for (var i = 0; i < buttons.length; i++) {
      var hear = buttons[i];
      if (hear.__title === undefined) { hear.__title = hear.title; }
      hear.disabled = muted;
      hear.title = muted ? 'Turn the sound on to hear this note' : hear.__title;
    }
  }

  // Two ways in, and they are not interchangeable.
  //
  // A click is a gesture, so it plays whatever it is asked to, from a tap, a mouse or the
  // keyboard, and it is also what makes the audio context live in the first place.  A
  // mouse arriving over a button is not a gesture: it can neither build the context nor
  // resume a suspended one, and a note scheduled against a suspended context is not lost
  // but queued, so a few hovers would arrive together as a chord the moment a later click
  // resumed it.  So the hover path waits for the context to be running, and only answers
  // to a mouse: passing a stylus over a row is not a request to hear it.
  //
  // pointermove and not pointerover, which is the obvious choice and the wrong one.  A
  // scroll moves rows under a cursor that is standing still, and the browser reports that
  // as arriving over each of them, so a page scrolled with the pointer resting over this
  // column would play its way down the chord.  Only real movement gets here, and lastHeard
  // turns the stream of moves back into "arrived at a new one".
  var lastHeard = null;

  if (fixes) {
    fixes.addEventListener('pointermove', function (e) {
      if (e.pointerType && e.pointerType !== 'mouse') { return; }
      var button = hearButton(e.target);
      if (button === lastHeard) { return; }
      lastHeard = button;
      if (!button || button.disabled || !audio.live || !audio.live()) { return; }
      var midi = buttonMidi(button);
      if (midi !== null) { playNote(midi); }
    });

    // Leaving the panel forgets where the pointer was, so coming back to the same button
    // is arriving at it again.  pointerleave does not bubble, which is why it is here.
    fixes.addEventListener('pointerleave', function () { lastHeard = null; });

    fixes.addEventListener('click', function (e) {
      var button = hearButton(e.target);
      if (!button || button.disabled) { return; }
      var midi = buttonMidi(button);
      if (midi !== null) { playNote(midi); }
    });
  }

  function setMuted(next, remember) {
    muted = !!next;
    if (audio && audio.supported) { audio.setMuted(muted); }
    syncHear();
    if (soundButton) {
      soundButton.setAttribute('aria-pressed', String(!muted));
      soundButton.textContent = muted ? 'Sound off' : 'Sound on';
      soundButton.setAttribute('aria-label',
        muted ? 'Sound off, turn chord playback on' : 'Sound on, turn chord playback off');
    }
    if (remember) { store('muted', muted ? '1' : '0'); }
  }

  if (audio && audio.supported && fixes) {
    // The column the buttons sit in is cut into the row by this class, so a page with no
    // Web Audio keeps the five column row it had rather than five and an empty gap.
    fixes.classList.add('can-hear');
  }

  if (soundButton && audio && audio.supported) {
    setMuted(muted, false);
    soundButton.addEventListener('click', function () {
      setMuted(!muted, true);
      // Unmuting with a step already open should prove it worked, and the click is the
      // gesture that lets the audio context start in the first place.
      if (!muted && selected) { playStep(steps[selected - 1]); }
    });
  } else if (soundButton) {
    // No Web Audio: a mute button for silence that was never going to be broken.
    soundButton.hidden = true;
  }

  // ---------------------------------------------------------------- corrections

  // A correction is a form post, and with no scripting that is the whole feature: the
  // server applies it and sends the page back, and the reader lands on #step-N.  What
  // that costs is the zoom, the pan and the reader's place on the page, to alter one
  // panel of a page that is 86% panels.  So with scripting the same post goes by fetch
  // and the answer is only the panels the change moved.  The server renders those,
  // because the server is what knows how a panel looks: building them here would be the
  // same markup, the same titles and the same labels written twice in two languages, and
  // one of the two would drift.
  //
  // Which panels moved is the server's answer too, and it is not always the one the
  // reader clicked in: a written accidental holds to the end of its measure, so
  // flattening a note re-spells the later notes at that staff position as well, and
  // their panels are as wrong as the edited one until they are redrawn.

  //: One at a time.  The page reload used to serialize these for free -- there was no
  //: clicking again before it came back -- and writing an edit back is read, splice,
  //: write, so two of them in flight together have the second drop the first.  Dropped
  //: rather than queued: an edit is arithmetic on a stored reading and answers in
  //: milliseconds, and a reader leaning on a button wants the edit, not five of them.
  var busy = false;

  function editForm(target) {
    return (target && target.closest)
      ? target.closest('.fix-note, .fix-add, .fix-delete')
      : null;
  }

  function fieldValue(form, name) {
    var field = form.querySelector('input[name="' + name + '"]');
    return field ? field.value : '';
  }

  function setField(form, name, value) {
    var field = form.querySelector('input[name="' + name + '"]');
    if (field) { field.value = String(value); }
  }

  function setNumber(panel, selector, value) {
    var node = panel.querySelector(selector);
    if (node) { node.textContent = String(value); }
  }

  // Take out the panel of a chord that has gone.  The list of panels is taken again
  // rather than left holding a node that is no longer in the page.
  function dropPanel(number) {
    var panel = fixPanel(number);
    if (panel && panel.parentNode) { panel.parentNode.removeChild(panel); }
    panels = fixes.querySelectorAll('.fix-panel');
  }

  // What every panel now says it is.  A chord going renumbers every step after it, and
  // with it each panel's heading, the step each of its forms names, and the event index
  // each of them posts at.  None of that is worked out here: the reading that came back
  // already says what each step is, and the panels are in the same order as the steps in
  // it, so panel i is step i and is stamped from it.  A sum done here and there would be
  // two places to get it wrong, and getting it wrong is a form quietly posting at the
  // chord next door, so this is a copy rather than a sum.
  function renumberPanels() {
    var many = Math.min(panels.length, steps.length);
    for (var i = 0; i < many; i++) {
      var panel = panels[i];
      var step = steps[i];
      panel.id = 'fix-' + step.number;
      panel.dataset.step = String(step.number);
      setNumber(panel, '.fix-n', step.number);
      setNumber(panel, '.fix-of', steps.length);
      var forms = panel.querySelectorAll('form');
      for (var f = 0; f < forms.length; f++) {
        setField(forms[f], 'step_number', step.number);
        setField(forms[f], 'system', step.system.index);
        setField(forms[f], 'event', step.event.index);
      }
    }
  }

  function swapPanels(sent) {
    var numbers = Object.keys(sent || {});
    for (var i = 0; i < numbers.length; i++) {
      var panel = fixPanel(numbers[i]);
      // innerHTML, so the .fix-panel itself is never replaced: it carries which step it
      // is and whether it is showing, and the list of panels points at it.
      if (panel) {
        panel.innerHTML = sent[numbers[i]];
        armPitchMenus(panel);
      }
    }
  }

  function fixPanel(step) {
    return fixes.querySelector('.fix-panel[data-step="' + step + '"]');
  }

  // The note rows for one hand of one chord, in the order its panel shows them.  Both
  // the playback and the focus below work from this, so they cannot come to filter
  // differently: a pitch is only ever looked for inside the hand that was edited, and a
  // page has plenty of other rows sounding the same pitch, some of them in panels that
  // are not even showing.
  function handRows(step, staff) {
    var rows = [];
    var panel = fixPanel(step);
    if (!panel) { return rows; }
    var forms = panel.querySelectorAll('.fix-note');
    for (var i = 0; i < forms.length; i++) {
      var field = forms[i].querySelector('input[name="staff"]');
      if (field && field.value === staff) { rows.push(forms[i]); }
    }
    return rows;
  }

  // What one hand of one chord is sounding, as its panel says.  Read off the play buttons
  // rather than out of the reading, because the panel is the thing that gets replaced,
  // and this before and after is how the note that changed gets identified.
  function handMidis(step, staff) {
    var found = [];
    handRows(step, staff).forEach(function (form) {
      var button = form.querySelector('.fix-hear');
      var midi = button ? buttonMidi(button) : null;
      if (midi !== null) { found.push(midi); }
    });
    return found;
  }

  // The note a correction just made, when it made exactly one.  A difference on pitches
  // and not on positions, because settling a chord re-sorts it and the note that moved is
  // no longer in the row it was in.  Sharpening, flattening and moving each retire one
  // pitch and bring another in; adding brings one in and retires none; removing retires
  // one and brings in none, and then there is nothing new to hear.  Two notes landing on
  // the same pitch is the one case with nothing new either, and silence beats a guess.
  function newNote(was, now) {
    var left = was.slice();
    var fresh = [];
    now.forEach(function (midi) {
      var at = left.indexOf(midi);
      if (at === -1) { fresh.push(midi); } else { left.splice(at, 1); }
    });
    return fresh.length === 1 ? fresh[0] : null;
  }

  // The count in "N note(s) corrected by hand".  The sentence is in the page; the page
  // rendered the number, and a correction that lands without a page load has changed it.
  function showEditCount(reading) {
    var line = document.getElementById('edits-note');
    var count = document.getElementById('edits-count');
    var total = ((reading || {}).edits || []).length;
    if (count) { count.textContent = String(total); }
    if (line) { line.hidden = !total; }
  }

  // Which control was pressed, as something findable again after the swap.  Only the
  // accidental buttons, because they are the ones pressed in a row: sharp, hear it, flat,
  // hear that.  Move hides itself once the menu agrees with the note again and the bin
  // leaves nothing behind to stand on, so for those the answer is to leave focus alone.
  function pressedAgain(submitter) {
    return (submitter && submitter.name === 'value' && submitter.value !== 'clear')
      ? 'button[name="value"][value="' + submitter.value + '"]'
      : '';
  }

  // The button the reader pressed is destroyed by the swap, so focus falls to the body
  // and somebody working by keyboard loses their place in the panel.  Put them back on
  // the same control in the row for the note that just changed: pressing sharp and then
  // flat on one note should not mean tabbing in from the top of the page in between.
  // Found by the note's new pitch rather than by its old row, because settling a chord
  // re-sorts it.
  function refocus(asked, midi) {
    if (!asked.pressed || midi === null) { return; }
    var rows = handRows(asked.step, asked.staff);
    for (var i = 0; i < rows.length; i++) {
      var button = rows[i].querySelector('.fix-hear');
      if (!button || buttonMidi(button) !== midi) { continue; }
      var again = rows[i].querySelector(asked.pressed);
      if (again) { again.focus(); }
      return;
    }
  }

  // *answer* is the server's; *asked* is what this end knows about the click that
  // caused it, which the server has no business being told: which panel and hand it was
  // in, what that hand was sounding beforehand, and which control was pressed.
  function applyEdit(answer, asked) {
    // The reading first, since everything after it is stamped or drawn from the reading
    // and not from what was in the page a moment ago.
    doc = window.SHEETER_DATA = answer.document;
    systems = doc.systems || [];
    if (!buildOverlay()) {
      // The last notehead went with that edit.  There is nothing left to correct, and
      // the page the server draws for a reading with no noteheads says so properly.
      window.location.reload();
      return;
    }

    // Before the panels are swapped, not after: a chord going renumbers the steps, and
    // the panels the server sent are named by what they are now.  Swapping first would
    // look them up by their new numbers while the page still had the old ones on, and
    // put a panel in the wrong place.
    if (answer.deleted) {
      dropPanel(answer.deleted);
      renumberPanels();
    }
    swapPanels(answer.panels);
    syncHear();
    showEditCount(doc);

    // Not fromUser, which would play the whole chord: the chord did not ask to be heard,
    // one note of it did, and that is the next thing below.
    select(Math.min(selected || 1, steps.length), 'hold', false);

    // Nothing to hear when a chord went, and nothing safe to look for either: the step
    // number the click carried now belongs to whichever chord moved up into it, so the
    // pitches there are a different chord's and are not this edit's to play.
    if (asked.heard && !answer.deleted) {
      var midi = newNote(asked.heard, handMidis(asked.step, asked.staff));
      if (midi !== null) {
        playNote(midi);
        refocus(asked, midi);
      }
    }
  }

  if (fixes) {
    fixes.addEventListener('submit', function (e) {
      var form = editForm(e.target);
      if (!form) { return; }
      // Something has already said no: the chord delete asks for a confirmation from an
      // inline onsubmit, and cancelling it prevents the default without stopping the
      // event getting here.  Without this, saying no to the dialog would delete the
      // chord anyway, by fetch.
      if (e.defaultPrevented) { return; }
      if (busy) { e.preventDefault(); return; }
      var submitter = e.submitter;
      // The pressed button's own formaction, and only when it has one.  With the
      // attribute absent the property is not the form's action, it is the address of this
      // page, so reading it unguarded would post an accidental at /a/<id> instead of at
      // /a/<id>/note -- and the accidental buttons are exactly the ones without it.
      var action = (submitter && submitter.hasAttribute('formaction'))
        ? submitter.formAction : form.action;
      var data = new FormData(form);
      // Which button was pressed is left out of FormData, and on these forms that is the
      // whole instruction: the same four indices, with "sharp" or "flat" or nothing.
      if (submitter && submitter.name) { data.append(submitter.name, submitter.value); }

      var step = fieldValue(form, 'step_number');
      var staff = fieldValue(form, 'staff');
      var asked = {
        step: step,
        staff: staff,
        // What the hand sounds now, to compare with what it sounds afterwards.
        heard: (step && staff) ? handMidis(step, staff) : null,
        pressed: pressedAgain(submitter)
      };

      e.preventDefault();
      busy = true;
      if (submitter) { submitter.setAttribute('aria-busy', 'true'); }

      window.fetch(action, {
        method: 'POST',
        body: data,
        headers: { Accept: 'application/json' },
        credentials: 'same-origin'
      }).then(function (answer) {
        return answer.ok ? answer.json() : Promise.reject(answer.status);
      }).then(function (answer) {
        if (answer.reload || !answer.document) {
          window.location.reload();
          return;
        }
        applyEdit(answer, asked);
        busy = false;
        if (submitter) { submitter.removeAttribute('aria-busy'); }
      }, function () {
        // Any trouble at all: a status, a body that will not parse, a dropped
        // connection.  Load the page again rather than post the form the old way, which
        // is not the safe fallback it looks like -- deleting a chord renumbers the
        // events, so a replayed post lands on a different one.  Worst case the reader
        // presses the button a second time.
        window.location.reload();
      });
    });
  }

  // fromUser says a person asked for this step, as opposed to the page settling itself.
  // Only a person's choice makes a sound, which is also what keeps the audio context's
  // first use inside a real gesture where the autoplay rules want it.
  //
  // *scroll* says what the window should do about it, and there are three answers rather
  // than the two a boolean allowed.  'stage' brings the step's labels into view, which is
  // what stepping wants: the labels you just asked for are no use below the fold.
  // 'panel' goes to the fix panel, which is where a page loaded at #step-N is headed.
  // 'hold' leaves the window exactly where it is, which is what a correction wants: the
  // reader is already looking at the panel they just clicked in, and moving the page out
  // from under them is the thing this whole change is here to stop.
  function select(number, scroll, fromUser) {
    if (number < 1 || number > steps.length) { return; }
    selected = number;
    var step = steps[number - 1];
    var i;

    if (fromUser) { playStep(step); }

    for (i = 0; i < pills.length; i++) {
      pills[i].classList.toggle('selected', pills[i].dataset.step === String(number));
    }
    for (i = 0; i < marks.length; i++) {
      if (marks[i].dataset.step === String(number)) {
        marks[i].setAttribute('aria-current', 'true');
      } else {
        marks[i].removeAttribute('aria-current');
      }
    }
    showPanel(number);
    rememberStep(number);
    layoutLabels();
    updateStepBar();
    if (mode === 'step') {
      // The framing happens whatever the window is doing: it moves the photo inside the
      // viewport, not the page, so it costs the reader nothing and leaves the label of a
      // note that just moved where it can be seen.
      frameStep(step);
      // One scroll at a time.  Two smooth ones in a turn fight each other.
      if (scroll === 'stage') { keepStageVisible(step); }
    }
    if (scroll === 'panel' && panels.length) {
      var panel = panels[Math.min(number, panels.length) - 1];
      if (panel) { panel.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); }
    }
  }

  // Stepping is useless if the labels you just asked for are not on screen: below the fold on
  // a tall photo, or, once the fix panel has been scrolled to, hidden behind the control
  // bar, which while stuck to the top of the window covers everything above its bottom edge.
  function keepStageVisible(step) {
    var floor = controls ? Math.max(controls.getBoundingClientRect().bottom, 8) : 8;
    var top = null;
    var bottom = 0;
    for (var i = 0; i < pills.length; i++) {
      if (!pills[i].__box || pills[i].dataset.step !== String(step.number)) { continue; }
      var rect = pills[i].getBoundingClientRect();
      top = top === null ? rect.top : Math.min(top, rect.top);
      bottom = Math.max(bottom, rect.bottom);
    }
    if (top === null) {
      var box = viewport.getBoundingClientRect();
      top = box.top;
      bottom = Math.min(box.bottom, box.top + window.innerHeight);
    }

    var delta = 0;
    if (top < floor + 8) {
      delta = top - floor - 8;
    } else if (bottom > window.innerHeight - 8) {
      delta = Math.min(bottom - window.innerHeight + 8, top - floor - 8);
    }
    if (delta) { window.scrollBy({ top: delta, left: 0, behavior: 'smooth' }); }
  }

  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return; }
    if (!selected && mode !== 'step') { return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); select(selected + 1, 'stage', true); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); select(selected ? selected - 1 : 1, 'stage', true); }
  });

  if (stepPrev) { stepPrev.addEventListener('click', function () { select(selected - 1, 'stage', true); }); }
  if (stepNext) { stepNext.addEventListener('click', function () { select(selected + 1, 'stage', true); }); }

  document.querySelectorAll('.mode-button').forEach(function (button) {
    button.addEventListener('click', function () { setMode(button.dataset.mode, true); });
  });

  // ---------------------------------------------------------------- view controls

  var sizeRange = document.getElementById('size-range');
  var MIN_LEGIBLE_LABEL = 8;

  function shrinkToFit() {
    var size = parseInt(labels.style.getPropertyValue('--label-size'), 10) || 12;
    while (size > MIN_LEGIBLE_LABEL && anyOverlap()) {
      size -= 1;
      setSize(size);
    }
    return !anyOverlap();
  }

  function setSize(px) {
    var size = clamp(parseInt(px, 10) || 12, 7, 26);
    labels.style.setProperty('--label-size', size + 'px');
    if (sizeRange) { sizeRange.value = String(size); }
    store('labelSize', String(size));
    layoutLabels();
  }

  if (sizeRange) {
    // Relayout on every pixel of travel, but only reconsider the mode on release: bigger
    // labels can start colliding, and flipping mode mid-drag is nauseating.
    sizeRange.addEventListener('input', function () { setSize(sizeRange.value); });
    sizeRange.addEventListener('change', reconsider);
  }

  // ---------------------------------------------------------------- zoom and pan

  var scale = 1;
  var offsetX = 0;
  var offsetY = 0;
  var MAX_SCALE = 8;
  var STEP_MAX_SCALE = 4;
  var LEGIBLE_UNIT = 26;
  var MARGIN = 12;

  function applyTransform() {
    var width = viewport.clientWidth;
    var height = stage.offsetHeight;
    var scaledWidth = width * scale;
    var scaledHeight = height * scale;
    offsetX = scaledWidth <= width ? 0 : clamp(offsetX, width - scaledWidth, 0);
    offsetY = scaledHeight <= height ? 0 : clamp(offsetY, height - scaledHeight, 0);
    stage.style.transform = 'translate(' + offsetX + 'px,' + offsetY + 'px) scale(' + scale + ')';
    viewport.classList.toggle('zoomed', scale > 1.01);
  }

  function zoomAt(nextScale, pointX, pointY) {
    nextScale = clamp(nextScale, 1, MAX_SCALE);
    var ratio = nextScale / scale;
    offsetX = pointX - (pointX - offsetX) * ratio;
    offsetY = pointY - (pointY - offsetY) * ratio;
    scale = nextScale;
    applyTransform();
  }

  function zoomCentre(factor) {
    zoomAt(scale * factor, viewport.clientWidth / 2, viewport.clientHeight / 2);
  }

  function fit() {
    scale = 1;
    offsetX = 0;
    offsetY = 0;
    applyTransform();
  }

  var glideTimer = 0;

  function glide() {
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) { return; }
    stage.classList.add('gliding');
    window.clearTimeout(glideTimer);
    glideTimer = window.setTimeout(function () { stage.classList.remove('gliding'); }, 320);
  }

  // Zoom far enough in that the staff is legible, but never so far that the selected step's own
  // labels, or the staves they belong to, fall outside the viewport.
  function frameStep(step) {
    var vw = viewport.clientWidth;
    var vh = viewport.clientHeight;
    var width = labels.clientWidth;
    if (!vw || !vh || !width || !imageWidth) { return; }
    var k = width / imageWidth;
    var geom = systemGeometry(step.system);

    // Everything this step needs on screen, in stage pixels: its staves, and its own labels,
    // which can reach above the staff when the chord sits over a high notehead.
    var left = step.event.x * k;
    var right = left;
    var top = geom.frameTop * k;
    var bottom = geom.frameBottom * k;
    for (var i = 0; i < pills.length; i++) {
      var box = pills[i].__box;
      if (!box || pills[i].dataset.step !== String(step.number)) { continue; }
      left = Math.min(left, box.l);
      right = Math.max(right, box.r);
      top = Math.min(top, box.t);
      bottom = Math.max(bottom, box.b);
    }

    var wanted = LEGIBLE_UNIT / Math.max(1, geom.unit * k);
    var fits = Math.min(vw / Math.max(1, right - left + MARGIN),
                        vh / Math.max(1, bottom - top + MARGIN));
    var target = clamp(Math.min(wanted, fits), 1, STEP_MAX_SCALE);

    glide();
    // Never Math.max here: a reader who zoomed in by hand and then pressed Next would
    // keep that zoom, and the step being framed would sit off screen with its labels.
    // Keep a manual zoom only while it still shows the whole step.
    scale = (scale > target && scale <= fits) ? scale : target;
    offsetX = vw / 2 - (left + right) / 2 * scale;
    offsetY = vh / 2 - (top + bottom) / 2 * scale;
    applyTransform();
  }

  var zoomIn = document.getElementById('zoom-in');
  var zoomOut = document.getElementById('zoom-out');
  var zoomFit = document.getElementById('zoom-fit');
  if (zoomIn) { zoomIn.addEventListener('click', function () { zoomCentre(1.5); }); }
  if (zoomOut) { zoomOut.addEventListener('click', function () { zoomCentre(1 / 1.5); }); }
  if (zoomFit) { zoomFit.addEventListener('click', fit); }

  var pointers = {};
  var captured = {};
  var pointerCount = 0;
  var lastMid = null;
  var lastSpread = 0;
  var moved = 0;

  // Capturing on pointerdown would make the viewport the target of the click that follows, so
  // taps on a label or a step marker never reached their buttons.  Capture only once a drag or
  // a pinch is actually under way, which is the only time the events have to keep coming.
  function capturePointer(e) {
    if (captured[e.pointerId]) { return; }
    captured[e.pointerId] = true;
    try { viewport.setPointerCapture(e.pointerId); } catch (err) { /* pointer already gone */ }
  }

  function localPoint(e) {
    var box = viewport.getBoundingClientRect();
    return { x: e.clientX - box.left, y: e.clientY - box.top };
  }

  function gesture() {
    var points = [];
    for (var id in pointers) {
      if (Object.prototype.hasOwnProperty.call(pointers, id)) { points.push(pointers[id]); }
    }
    if (!points.length) { return null; }
    if (points.length === 1) { return { x: points[0].x, y: points[0].y, spread: 0 }; }
    var dx = points[0].x - points[1].x;
    var dy = points[0].y - points[1].y;
    return {
      x: (points[0].x + points[1].x) / 2,
      y: (points[0].y + points[1].y) / 2,
      spread: Math.sqrt(dx * dx + dy * dy)
    };
  }

  viewport.addEventListener('pointerdown', function (e) {
    if (e.pointerType === 'mouse' && e.button !== 0) { return; }
    pointers[e.pointerId] = localPoint(e);
    pointerCount += 1;
    moved = 0;
    var g = gesture();
    lastMid = g;
    lastSpread = g ? g.spread : 0;
    if (pointerCount > 1) {
      e.preventDefault();
      capturePointer(e);
    }
  });

  viewport.addEventListener('pointermove', function (e) {
    if (!pointers[e.pointerId]) { return; }
    pointers[e.pointerId] = localPoint(e);
    var g = gesture();
    if (!g || !lastMid) { return; }

    if (pointerCount > 1 && lastSpread > 0 && g.spread > 0) {
      e.preventDefault();
      capturePointer(e);
      zoomAt(scale * (g.spread / lastSpread), g.x, g.y);
      offsetX += g.x - lastMid.x;
      offsetY += g.y - lastMid.y;
      applyTransform();
      moved += 40;
    } else if (pointerCount === 1 && scale > 1.01) {
      e.preventDefault();
      capturePointer(e);
      offsetX += g.x - lastMid.x;
      offsetY += g.y - lastMid.y;
      moved += Math.abs(g.x - lastMid.x) + Math.abs(g.y - lastMid.y);
      viewport.classList.add('grabbing');
      applyTransform();
    }
    lastMid = g;
    lastSpread = g.spread;
  });

  function endPointer(e) {
    if (!pointers[e.pointerId]) { return; }
    delete pointers[e.pointerId];
    delete captured[e.pointerId];
    pointerCount = Math.max(0, pointerCount - 1);
    lastMid = gesture();
    lastSpread = lastMid ? lastMid.spread : 0;
    if (!pointerCount) { viewport.classList.remove('grabbing'); }
  }

  viewport.addEventListener('pointerup', endPointer);
  viewport.addEventListener('pointercancel', endPointer);

  // A drag that ends on a marker must not also count as a tap on it.  Step mode leaves the
  // image zoomed most of the time, so this runs on nearly every tap: keep the slack wide
  // enough that a thumb on a 22px dot still selects.
  viewport.addEventListener('click', function (e) {
    if (moved > 14) {
      e.preventDefault();
      e.stopPropagation();
      moved = 0;
    }
  }, true);

  viewport.addEventListener('wheel', function (e) {
    if (!e.ctrlKey) { return; }
    e.preventDefault();
    var point = localPoint(e);
    zoomAt(scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12), point.x, point.y);
  }, { passive: false });

  viewport.addEventListener('dblclick', function (e) {
    var point = localPoint(e);
    if (scale > 1.01) { fit(); } else { zoomAt(2.5, point.x, point.y); }
  });

  // ---------------------------------------------------------------- start and resize

  // Overlap is a property of the stage layout, so only a width change or a new label size can
  // change the answer; a zoom scales labels and photo together and cannot.
  function reconsider() {
    layoutLabels();
    if (!chosenByUser) { setMode(wouldCollide() ? 'step' : 'all', false); }
  }

  var stored = recall('labelMode', '');
  applyModeClass('all');
  setSize(recall('labelSize', '12'));
  chosenByUser = (stored === 'step' || stored === 'all');
  setMode(chosenByUser ? stored : (wouldCollide() ? 'step' : 'all'), false);
  applyTransform();

  // Every panel is rendered visible so the page works with scripting off; with scripting
  // on, hide them and show one.  A correction posts back to #step-N, so the step it was
  // made on is selected again and the reader carries on where they were; otherwise the
  // first step, which is what makes the panel a step's panel rather than a page's.
  var landed = stepFromHash();
  if (panels.length) {
    armPitchMenus(fixes);
    select(landed || 1, landed ? 'panel' : 'stage', false);
  }

  var pending = 0;
  window.addEventListener('resize', function () {
    window.clearTimeout(pending);
    pending = window.setTimeout(function () {
      reconsider();
      applyTransform();
      if (mode === 'step' && selected) { frameStep(steps[selected - 1]); }
    }, 120);
  });

  if (sheet && !sheet.complete) {
    sheet.addEventListener('load', function () {
      reconsider();
      applyTransform();
    });
  }

  // ---------------------------------------------------------------- copy

  function plainText() {
    var lines = [doc.title || 'Sheeter reading', ''];
    steps.forEach(function (step) {
      var combined = step.event.combined || {};
      var head = 'Step ' + step.number + ': ' + (combined.symbol || 'unnamed');
      if (combined.common_name) { head += ' (' + combined.common_name + ')'; }
      lines.push(head);
      (step.event.parts || []).forEach(function (part) {
        var staff = staffOf(step.system, part.staff);
        lines.push('  ' + handName(part, staff) + ': ' + displayNames(part).join(', '));
      });
      if (step.event.note) { lines.push('  note: ' + step.event.note); }
    });
    lines.push('');
    lines.push('Read from staff geometry only, not checked by a model.');
    return lines.join('\n');
  }

  // ---------------------------------------------------------------- renaming

  // The form is in the page and visible; this hides it and offers the pencil instead.
  // That way renaming is not behind javascript: with none, the title is editable where
  // it stands, which is the same direction Move and the fix panels take.
  (function () {
    var form = document.getElementById('rename-form');
    var toggle = document.getElementById('rename-toggle');
    var heading = document.getElementById('doc-title');
    var field = document.getElementById('title-input');
    if (!form || !toggle || !heading || !field) { return; }

    var was = field.value;

    function editing(on) {
      form.hidden = !on;
      toggle.hidden = on;
      heading.hidden = on;
      if (on) { field.focus(); field.select(); }
    }

    editing(false);
    toggle.addEventListener('click', function () { editing(true); });
    // Escape is what a text field that appeared over a heading should answer to, and it
    // puts the title back rather than leaving a half-typed one in the box.
    field.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        e.preventDefault();
        field.value = was;
        editing(false);
        toggle.focus();
      }
    });
  }());

  // Was an inline onsubmit with the correction count written into the sentence.  A
  // correction lands without a page load now, so a written-in count goes stale; this
  // reads it off the reading in hand.  The busy state comes along because the two were
  // one attribute, and because a handler that can cancel the submit has to be the thing
  // that decides whether to say "Re-analyzing".
  var reanalyzeForm = document.getElementById('reanalyze-form');
  if (reanalyzeForm) {
    reanalyzeForm.addEventListener('submit', function (e) {
      var count = ((doc && doc.edits) || []).length;
      if (count && !window.confirm('Re-reading the photo starts over, so the ' + count +
          ' correction(s) you made by hand will be cleared. Carry on?')) {
        e.preventDefault();
        return;
      }
      var button = reanalyzeForm.querySelector('button');
      if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
        button.textContent = 'Re-analyzing';
      }
    });
  }

  var copyButton = document.getElementById('copy-button');
  if (copyButton) {
    copyButton.addEventListener('click', function () {
      var text = plainText();
      var done = function () {
        copyButton.textContent = 'Copied';
        window.setTimeout(function () { copyButton.textContent = 'Copy as text'; }, 1600);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text, done); });
      } else {
        fallbackCopy(text, done);
      }
    });
  }

  function fallbackCopy(text, done) {
    var area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); done(); } catch (e) { /* nothing else to try */ }
    document.body.removeChild(area);
  }
}());
