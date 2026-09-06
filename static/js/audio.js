/*
 * Chord playback, in as little synthesis as the job needs.
 *
 * The point is to answer "what does this chord sound like", not to imitate a piano, so
 * each note is one triangle oscillator through a struck-sounding envelope: near instant
 * attack, then an exponential decay.  That reads as a mallet or a music box, which is
 * pleasant enough to hear four times in a row and costs no samples, no library and no
 * network.
 *
 * Exposed as window.SheeterAudio so overlay.js can call it.  This file knows how to make
 * a noise and nothing else: which chord to play, and whether the reader wants to hear it
 * at all, are overlay.js's business.
 */
(function () {
  'use strict';

  var Ctx = window.AudioContext || window.webkitAudioContext;

  var ATTACK = 0.006;
  //: Long enough that a strummed chord is still a chord by the time the top note lands.
  //: The notes are 120ms apart, so a six-note voicing takes 600ms to roll out, and on the
  //: old 1.15s decay the bottom note was down to 2% of its peak by then: six notes in a
  //: row rather than one chord.  At 2.6s it is still near a fifth of peak and they ring
  //: together.
  var DECAY = 2.6;
  //: How fast a chord gets out of the way when the next one interrupts it.  Long enough
  //: not to click, short enough that stepping quickly does not turn into a smear.
  var RELEASE = 0.09;
  //: An exponential ramp cannot reach zero.  Ramping to it does nothing, or throws,
  //: depending on the browser, and the note appears to hang forever.
  var SILENT = 0.0001;
  //: The gap between one note of a chord and the next.  Wide enough to hear as a strum
  //: rather than as a chord with a soft edge, which is the point: the notes arrive one at
  //: a time so you can follow them, and still overlap into the chord they make.
  var SPREAD = 0.12;

  var context = null;
  var master = null;
  var sounding = [];
  var muted = false;

  function hz(midi) {
    return 440 * Math.pow(2, (midi - 69) / 12);
  }

  // Left until the first actual play, which is always inside a click or a keypress.  A
  // context built at load starts suspended under the autoplay rules and the first chord
  // is lost.
  function ready() {
    if (!Ctx) { return null; }
    if (!context) {
      context = new Ctx();
      master = context.createGain();
      master.gain.value = 0.5;
      master.connect(context.destination);
    }
    // Suspended is the normal state after a tab is backgrounded, not just at startup.
    if (context.state === 'suspended' && context.resume) { context.resume(); }
    return context;
  }

  function hush(at) {
    sounding.forEach(function (voice) {
      var gain = voice.gain.gain;
      try {
        gain.cancelScheduledValues(at);
        // Ramping from wherever the decay has already got to, rather than from the peak,
        // so cutting a chord short does not make it briefly louder.
        gain.setValueAtTime(Math.max(gain.value, SILENT), at);
        gain.exponentialRampToValueAtTime(SILENT, at + RELEASE);
        voice.osc.stop(at + RELEASE);
      } catch (e) {
        // A voice that has already finished throws on stop.  Nothing to do about it.
      }
    });
    sounding = [];
  }

  function play(midis) {
    if (muted || !midis || !midis.length) { return; }
    var ctx = ready();
    if (!ctx) { return; }

    var now = ctx.currentTime;
    hush(now);

    // Chords here run from two notes to about eight.  Dividing the budget flat would
    // make the big ones inaudible, so it comes down gently with the count instead.
    // The strum helps here too: the notes no longer all peak on the same sample.
    var level = 0.5 / Math.sqrt(midis.length);

    // Already in playing order when it arrives: low to high, left hand before right.
    midis.forEach(function (midi, i) {
      var at = now + i * SPREAD;
      var osc = ctx.createOscillator();
      var gain = ctx.createGain();

      osc.type = 'triangle';
      osc.frequency.value = hz(midi);

      gain.gain.setValueAtTime(SILENT, at);
      gain.gain.exponentialRampToValueAtTime(level, at + ATTACK);
      gain.gain.exponentialRampToValueAtTime(SILENT, at + ATTACK + DECAY);

      osc.connect(gain);
      gain.connect(master);
      osc.start(at);
      osc.stop(at + ATTACK + DECAY + 0.02);

      sounding.push({ osc: osc, gain: gain });
    });
  }

  function setMuted(value) {
    muted = !!value;
    if (muted && context) { hush(context.currentTime); }
    return muted;
  }

  window.SheeterAudio = {
    // False on anything without Web Audio, which is overlay.js's cue to not offer a
    // mute button for a thing that was never going to make a sound.
    supported: !!Ctx,
    play: play,
    setMuted: setMuted,
    stop: function () { if (context) { hush(context.currentTime); } }
  };
}());
