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
  var DECAY = 1.15;
  //: How fast a chord gets out of the way when the next one interrupts it.  Long enough
  //: not to click, short enough that stepping quickly does not turn into a smear.
  var RELEASE = 0.09;
  //: An exponential ramp cannot reach zero.  Ramping to it does nothing, or throws,
  //: depending on the browser, and the note appears to hang forever.
  var SILENT = 0.0001;
  //: A chord struck dead flat sounds synthetic, and all the transients landing on one
  //: sample sums into a click.  A few milliseconds between notes fixes both.
  var SPREAD = 0.012;

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
    var level = 0.5 / Math.sqrt(midis.length);

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
