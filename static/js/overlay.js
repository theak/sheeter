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
  var detail = document.getElementById('detail');
  var stageEmpty = document.getElementById('stage-empty');
  var controls = document.querySelector('.stage-controls');
  var hint = document.querySelector('.stage-hint');
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

  // Note names read top to bottom on the page, and the schema sorts them low to high.
  function displayNames(part) {
    var out = [];
    for (var i = part.notes.length - 1; i >= 0; i--) {
      out.push(part.notes[i].pretty || part.notes[i].name);
    }
    return out;
  }

  var ORDINALS = ['', 'unison', '2nd', '3rd', '4th', '5th', '6th', '7th', 'octave',
                  '9th', '10th', '11th', '12th', '13th', '14th', 'two octaves'];
  var QUALITIES = {
    P: 'perfect', M: 'major', m: 'minor', A: 'augmented', d: 'diminished',
    AA: 'doubly augmented', dd: 'doubly diminished'
  };

  function intervalWords(code) {
    var match = /^([PMmAd]{1,2})(\d{1,2})$/.exec(String(code || ''));
    if (!match) { return String(code || ''); }
    var number = parseInt(match[2], 10);
    if (number === 1) { return 'the bass note'; }
    var quality = QUALITIES[match[1]] || match[1];
    var name = ORDINALS[number] || (number + 'th');
    if (number === 8 || number === 15) { return quality === 'perfect' ? 'an octave up' : quality + ' ' + name; }
    return 'a ' + quality + ' ' + name + ' up';
  }

  // ---------------------------------------------------------------- events

  var steps = [];

  systems.forEach(function (system) {
    (system.events || []).forEach(function (event) {
      steps.push({ system: system, event: event, number: steps.length + 1 });
    });
  });

  if (!systems.length || !steps.length) {
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
    if (staves.length > 1) {
      var upper = staves[0].lines || [];
      var lower = staves[1].lines || [];
      if (upper.length && lower.length) {
        dotY = (upper[upper.length - 1] + lower[0]) / 2;
      }
    }

    system.__geom = {
      top: top,
      bottom: bottom,
      unit: unit,
      dotY: clamp(dotY, unit, Math.max(unit, imageHeight - unit)),
      bandTop: clamp(top - unit * 0.8, 0, imageHeight),
      bandBottom: clamp(bottom + unit * 0.8, 0, imageHeight),
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

  var pills = [];
  var marks = [];

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
    mark.addEventListener('click', function () { select(step.number, false); });
    labels.appendChild(mark);
    marks.push(mark);
  });

  function makePill(className, tagText, lines, step, anchor) {
    var pill = el('button', 'pill ' + className);
    pill.type = 'button';
    pill.appendChild(el('span', 'tag', tagText));
    var names = el('span', 'names');
    lines.forEach(function (line) { names.appendChild(el('span', null, line)); });
    pill.appendChild(names);
    pill.dataset.step = String(step.number);
    pill.setAttribute('aria-label',
      'Step ' + step.number + ', ' + tagText + ', ' + lines.join(' '));
    if ((step.event.confidence || 0) < 0.7) { pill.classList.add('low'); }
    pill.addEventListener('click', function () { select(step.number, false); });
    pill.__anchor = anchor;
    labels.appendChild(pill);
    pills.push(pill);
    return pill;
  }

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

  // ---------------------------------------------------------------- layout

  var EDGE = 3;

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
    if (hint) {
      hint.textContent = (mode === 'step'
        ? 'Tap a numbered dot to read that step, or use Prev and Next, or the arrow keys. '
        : 'Tap any label to read that step. ') +
        'Pinch or use + and - to zoom; drag to move around; Fit puts it back.';
    }
    layoutLabels();
    if (remember) {
      chosenByUser = true;
      store('labelMode', mode);
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
      stepStatus.textContent = steps.length + ' steps - pick one';
    }
    if (stepPrev) { stepPrev.disabled = selected <= 1; }
    if (stepNext) { stepNext.disabled = selected >= steps.length; }
  }

  // ---------------------------------------------------------------- detail

  var selected = 0;

  function buildDetail(step) {
    var event = step.event;
    var combined = event.combined || {};
    detail.textContent = '';

    detail.appendChild(el('h2', null, 'Step ' + step.number + ' of ' + steps.length));

    var names = combined.pretty || combined.names || [];
    detail.appendChild(el('p', 'chord-line', combined.symbol || names.join(' ') || 'no chord named'));
    if (combined.common_name) {
      detail.appendChild(el('p', 'common-name', combined.common_name));
    }

    (event.parts || []).forEach(function (part) {
      var staff = staffOf(step.system, part.staff);
      var block = el('div', 'hand-block');
      block.appendChild(el('h3', null, handName(part, staff)));
      var shown = displayNames(part);
      block.appendChild(el('p', 'notes-line', shown.join('  ')));
      var extras = [];
      if (part.chord && part.chord.symbol) { extras.push('on its own: ' + part.chord.symbol); }
      if (part.duration_hint) { extras.push(part.duration_hint + (part.dotted ? ', dotted' : '') + ' notes'); }
      if (shown.length === 1) {
        extras.push('a single note');
      } else {
        extras.push(shown.length + ' notes together');
      }
      block.appendChild(el('p', 'muted', extras.join(' - ')));
      detail.appendChild(block);
    });

    if (names.length && combined.bass) {
      detail.appendChild(el('h3', null, 'Every note, from the bass up'));
      var list = el('ul', 'intervals');
      var intervals = combined.intervals_from_bass || [];
      names.forEach(function (name, index) {
        var item = el('li');
        item.appendChild(el('b', null, name));
        var words = intervals.length === names.length ? intervalWords(intervals[index]) : '';
        if (words) { item.appendChild(document.createTextNode(' - ' + words)); }
        list.appendChild(item);
      });
      detail.appendChild(list);
    }

    if (combined.roman) {
      detail.appendChild(el('p', 'muted', 'Reads as ' + combined.roman + ' in the key of the staff.'));
    }

    if (event.note) {
      var quote = el('div', 'verifier-note');
      quote.appendChild(el('p', null, event.note));
      quote.appendChild(el('p', 'muted', 'Note from the model that checked this photo.'));
      detail.appendChild(quote);
    }

    if ((event.confidence || 0) < 0.7) {
      detail.appendChild(el('p', 'muted', 'Sheeter is not confident about this step. Check it by eye.'));
    }

    var nav = el('p', 'step-nav');
    var previous = el('button', 'secondary outline', 'Previous step');
    previous.type = 'button';
    previous.disabled = step.number <= 1;
    previous.addEventListener('click', function () { select(step.number - 1, true); });
    var next = el('button', 'secondary outline', 'Next step');
    next.type = 'button';
    next.disabled = step.number >= steps.length;
    next.addEventListener('click', function () { select(step.number + 1, true); });
    nav.appendChild(previous);
    nav.appendChild(next);
    detail.appendChild(nav);
  }

  var rows = document.querySelectorAll('.reading tbody tr');

  function select(number, scrollToDetail) {
    if (number < 1 || number > steps.length) { return; }
    selected = number;
    var step = steps[number - 1];
    var i;

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
    for (i = 0; i < rows.length; i++) {
      rows[i].classList.toggle('selected-row', i === number - 1);
      if (i === number - 1) {
        rows[i].setAttribute('aria-current', 'true');
      } else {
        rows[i].removeAttribute('aria-current');
      }
    }

    buildDetail(step);
    detail.hidden = false;
    layoutLabels();
    updateStepBar();
    if (mode === 'step') {
      frameStep(step);
      keepStageVisible();
    }
    if (scrollToDetail) {
      detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  // Stepping is useless if the photo is off screen or hiding under the sticky control bar,
  // which is easy on a phone once the reading table has been scrolled to.
  function keepStageVisible() {
    var top = viewport.getBoundingClientRect().top;
    // While the control bar is stuck to the top of the window it hides everything above its
    // bottom edge, which is exactly where a chord label sits.
    var floor = controls ? Math.max(controls.getBoundingClientRect().bottom, 8) : 8;
    if (top >= floor && top < window.innerHeight * 0.5) { return; }
    window.scrollBy({ top: top - floor - 8, left: 0, behavior: 'smooth' });
  }

  for (var r = 0; r < rows.length; r++) {
    (function (row, index) {
      row.style.cursor = 'pointer';
      row.addEventListener('click', function () { select(index + 1, true); });
    }(rows[r], r));
  }

  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') { return; }
    if (!selected && mode !== 'step') { return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); select(selected + 1, false); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); select(selected ? selected - 1 : 1, false); }
  });

  if (stepPrev) { stepPrev.addEventListener('click', function () { select(selected - 1, false); }); }
  if (stepNext) { stepNext.addEventListener('click', function () { select(selected + 1, false); }); }

  document.querySelectorAll('.mode-button').forEach(function (button) {
    button.addEventListener('click', function () { setMode(button.dataset.mode, true); });
  });

  // ---------------------------------------------------------------- view controls

  var VIEWS = ['notes', 'chords', 'both'];

  function setView(view) {
    if (VIEWS.indexOf(view) === -1) { view = 'both'; }
    VIEWS.forEach(function (name) { labels.classList.toggle('show-' + name, name === view); });
    var buttons = document.querySelectorAll('.view-button');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', String(buttons[i].dataset.view === view));
    }
    store('view', view);
    layoutLabels();
  }

  document.querySelectorAll('.view-button').forEach(function (button) {
    button.addEventListener('click', function () {
      setView(button.dataset.view);
      reconsider();
    });
  });

  var sizeRange = document.getElementById('size-range');

  function setSize(px) {
    var size = clamp(parseInt(px, 10) || 12, 7, 26);
    labels.style.setProperty('--label-size', size + 'px');
    if (sizeRange) { sizeRange.value = String(size); }
    store('labelSize', String(size));
    layoutLabels();
  }

  if (sizeRange) {
    sizeRange.addEventListener('input', function () {
      setSize(sizeRange.value);
      reconsider();
    });
  }

  // ---------------------------------------------------------------- zoom and pan

  var scale = 1;
  var offsetX = 0;
  var offsetY = 0;
  var MAX_SCALE = 8;
  var STEP_MAX_SCALE = 4;
  var LEGIBLE_UNIT = 26;

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

    var wanted = LEGIBLE_UNIT / Math.max(1, geom.unit * k);
    var band = vh / Math.max(1, (geom.frameBottom - geom.frameTop) * k);

    var boxLeft = null;
    var boxRight = 0;
    var boxTop = null;
    var boxBottom = 0;
    for (var i = 0; i < pills.length; i++) {
      var box = pills[i].__box;
      if (!box || pills[i].dataset.step !== String(step.number)) { continue; }
      boxLeft = boxLeft === null ? box.l : Math.min(boxLeft, box.l);
      boxTop = boxTop === null ? box.t : Math.min(boxTop, box.t);
      boxRight = Math.max(boxRight, box.r);
      boxBottom = Math.max(boxBottom, box.b);
    }
    var byLabels = STEP_MAX_SCALE;
    if (boxLeft !== null) {
      byLabels = Math.min(vw / Math.max(1, boxRight - boxLeft), vh / Math.max(1, boxBottom - boxTop));
    }

    var target = clamp(Math.min(wanted, band, byLabels), 1, STEP_MAX_SCALE);
    var centreX = step.event.x * k;
    var centreY = (geom.frameTop + geom.frameBottom) / 2 * k;
    if (boxLeft !== null) {
      centreX = (Math.min(centreX, boxLeft) + Math.max(centreX, boxRight)) / 2;
    }

    glide();
    scale = Math.max(scale, target);
    offsetX = vw / 2 - centreX * scale;
    offsetY = vh / 2 - centreY * scale;
    applyTransform();
  }

  var zoomIn = document.getElementById('zoom-in');
  var zoomOut = document.getElementById('zoom-out');
  var zoomFit = document.getElementById('zoom-fit');
  if (zoomIn) { zoomIn.addEventListener('click', function () { zoomCentre(1.5); }); }
  if (zoomOut) { zoomOut.addEventListener('click', function () { zoomCentre(1 / 1.5); }); }
  if (zoomFit) { zoomFit.addEventListener('click', fit); }

  var pointers = {};
  var pointerCount = 0;
  var lastMid = null;
  var lastSpread = 0;
  var moved = 0;

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
    if (pointerCount > 1) { e.preventDefault(); }
    viewport.setPointerCapture(e.pointerId);
  });

  viewport.addEventListener('pointermove', function (e) {
    if (!pointers[e.pointerId]) { return; }
    pointers[e.pointerId] = localPoint(e);
    var g = gesture();
    if (!g || !lastMid) { return; }

    if (pointerCount > 1 && lastSpread > 0 && g.spread > 0) {
      e.preventDefault();
      zoomAt(scale * (g.spread / lastSpread), g.x, g.y);
      offsetX += g.x - lastMid.x;
      offsetY += g.y - lastMid.y;
      applyTransform();
      moved += 40;
    } else if (pointerCount === 1 && scale > 1.01) {
      e.preventDefault();
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
  setView(recall('view', 'both'));
  setSize(recall('labelSize', '12'));
  chosenByUser = (stored === 'step' || stored === 'all');
  setMode(chosenByUser ? stored : (wouldCollide() ? 'step' : 'all'), false);
  applyTransform();

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
    lines.push(doc.engine && doc.engine.verified
      ? 'Checked against the photo by ' + (doc.engine.verifier || 'a vision model') + '.'
      : 'Read from staff geometry only, not checked by a model.');
    return lines.join('\n');
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
