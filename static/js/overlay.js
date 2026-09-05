/* Sheeter overlay.
 *
 * Reads the global SHEETER_DATA (the analysis document, see docs/schema.md) and puts one
 * grouped label per event per hand on top of the straightened photo.
 *
 * Positions are percentages of the processed image, so the overlay tracks the image through
 * any amount of CSS scaling without a resize listener.  Per-notehead labels were tried and do
 * not work: a five note stack at unit 20px, scaled into a 390px viewport, puts them 6px apart.
 */
(function () {
  'use strict';

  var doc = window.SHEETER_DATA;
  var root = document.documentElement;
  root.classList.add('js');

  var viewport = document.getElementById('viewport');
  var stage = document.getElementById('stage');
  var labels = document.getElementById('labels');
  var detail = document.getElementById('detail');
  var stageEmpty = document.getElementById('stage-empty');
  var controls = document.querySelector('.stage-controls');

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

  // ---------------------------------------------------------------- labels

  function percentX(x) { return imageWidth ? clamp(x / imageWidth * 100, 0, 100) : 0; }
  function percentY(y) { return imageHeight ? clamp(y / imageHeight * 100, 0, 100) : 0; }

  function makePill(className, tagText, lines, step) {
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
    return pill;
  }

  steps.forEach(function (step) {
    var system = step.system;
    var event = step.event;

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

      var gap = (staff && staff.unit ? staff.unit : 10) * 0.6;
      var pill = makePill('hand-' + handTone(part, staff), handTag(part, staff),
                          displayNames(part), step);
      var anchor = percentX(right + gap);
      // The last event of a system sits hard against the right edge, where a label placed to
      // the right of the stack would be clipped by the viewport.  Flip it to the other side.
      if (anchor > 82) {
        pill.style.left = percentX(left - gap) + '%';
        pill.style.transform = 'translate(-100%, -50%)';
      } else {
        pill.style.left = anchor + '%';
        pill.style.transform = 'translate(0, -50%)';
      }
      pill.style.top = percentY((top + bottom) / 2) + '%';
      labels.appendChild(pill);
    });

    var combined = event.combined;
    var symbol = combined && combined.symbol;
    if (symbol) {
      var topStaff = (system.staves || [])[0];
      var unit = (topStaff && topStaff.unit) || 10;
      var lineTop = (topStaff && topStaff.lines && topStaff.lines[0]);
      if (typeof lineTop !== 'number') { lineTop = (system.y_range || [0])[0] || 0; }
      var chordPill = makePill('chord', 'C', [symbol], step);
      chordPill.style.left = clamp(percentX(event.x), 4, 96) + '%';
      chordPill.style.top = percentY(Math.max(lineTop - unit * 2.2, unit * 0.4)) + '%';
      chordPill.style.transform = 'translate(-50%, -100%)';
      labels.appendChild(chordPill);
    }
  });

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
    var previous = el('button', 'secondary outline', 'Previous');
    previous.type = 'button';
    previous.disabled = step.number <= 1;
    previous.addEventListener('click', function () { select(step.number - 1, true); });
    var next = el('button', 'secondary outline', 'Next');
    next.type = 'button';
    next.disabled = step.number >= steps.length;
    next.addEventListener('click', function () { select(step.number + 1, true); });
    nav.appendChild(previous);
    nav.appendChild(next);
    detail.appendChild(nav);
  }

  function select(number, scrollIntoView) {
    if (number < 1 || number > steps.length) { return; }
    selected = number;
    var step = steps[number - 1];

    var pills = labels.querySelectorAll('.pill');
    for (var i = 0; i < pills.length; i++) {
      pills[i].classList.toggle('selected', pills[i].dataset.step === String(number));
    }
    var rows = document.querySelectorAll('.reading tbody tr');
    for (var j = 0; j < rows.length; j++) {
      rows[j].classList.toggle('selected-row', j === number - 1);
    }

    buildDetail(step);
    detail.hidden = false;
    if (scrollIntoView) {
      detail.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    }
  }

  document.querySelectorAll('.reading tbody tr').forEach(function (row, index) {
    row.style.cursor = 'pointer';
    row.addEventListener('click', function () { select(index + 1, true); });
  });

  document.addEventListener('keydown', function (e) {
    if (!selected) { return; }
    var tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA') { return; }
    if (e.key === 'ArrowRight') { e.preventDefault(); select(selected + 1, true); }
    if (e.key === 'ArrowLeft') { e.preventDefault(); select(selected - 1, true); }
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
  }

  document.querySelectorAll('.view-button').forEach(function (button) {
    button.addEventListener('click', function () { setView(button.dataset.view); });
  });
  setView(recall('view', 'both'));

  var sizeRange = document.getElementById('size-range');

  function setSize(px) {
    var size = clamp(parseInt(px, 10) || 12, 7, 26);
    labels.style.setProperty('--label-size', size + 'px');
    if (sizeRange) { sizeRange.value = String(size); }
    store('labelSize', String(size));
  }

  if (sizeRange) {
    sizeRange.addEventListener('input', function () { setSize(sizeRange.value); });
  }
  setSize(recall('labelSize', '12'));

  // ---------------------------------------------------------------- zoom and pan

  var scale = 1;
  var offsetX = 0;
  var offsetY = 0;
  var MAX_SCALE = 8;

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
      moved += 20;
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

  // A drag that ends on a pill must not also count as a tap on it.
  viewport.addEventListener('click', function (e) {
    if (moved > 8) {
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

  window.addEventListener('resize', applyTransform);
  applyTransform();

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
