"use strict";

// ── Mapping events serveur → états sphère ────────────────────────────────────
const VOICE_STATE_MAP = {
  vad_start:   "LISTENING",
  stt_done:    "THINKING",
  llm_start:   "THINKING",
  tts_start:   "SPEAKING",
  tts_done:    "IDLE",
  interrupted: "LISTENING",
};

// ── Label voice-status ────────────────────────────────────────────────────────
function showVoiceStatus(text, duration = 0) {
  const el = document.getElementById("voice-status");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("visible", text.length > 0);
  if (duration > 0 && text.length > 0) {
    setTimeout(() => {
      if (el.textContent === text) el.classList.remove("visible");
    }, duration);
  }
}

// ── BargeInDetector ───────────────────────────────────────────────────────────
class BargeInDetector {
  constructor(voiceClient) {
    this._client          = voiceClient;
    this._analyser        = null;
    this._active          = false;
    this._THRESHOLD_BASE  = 0.15;
    this._THRESHOLD_TTS   = 0.45;   // plus élevé quand TTS joue (évite auto-capture)
    this._SUSTAINED_MS    = 150;
    this._voiceStart      = null;
    this._rafId           = null;
  }

  start(audioCtx, stream) {
    const src       = audioCtx.createMediaStreamSource(stream);
    this._analyser  = audioCtx.createAnalyser();
    this._analyser.fftSize = 256;
    src.connect(this._analyser);
    this._active = true;
    this._detect();
  }

  _detect() {
    if (!this._active || !this._analyser) return;

    const buf = new Float32Array(this._analyser.fftSize);
    this._analyser.getFloatTimeDomainData(buf);
    const amplitude = Math.max(...buf.map(Math.abs));

    // Seuil adaptatif : plus élevé si L.Y.R.A parle (évite que le TTS se capte lui-même)
    const threshold = this._client._isSpeaking
      ? this._THRESHOLD_TTS
      : this._THRESHOLD_BASE;

    if (amplitude > threshold) {
      if (!this._voiceStart) {
        this._voiceStart = Date.now();
      } else if (Date.now() - this._voiceStart > this._SUSTAINED_MS) {
        this._trigger();
      }
    } else {
      this._voiceStart = null;
    }

    this._rafId = requestAnimationFrame(() => this._detect());
  }

  _trigger() {
    if (!this._client._isSpeaking) return;   // L.Y.R.A ne parle pas → pas de barge-in
    if (!this._client._ws || this._client._ws.readyState !== WebSocket.OPEN) return;

    console.log("[BargeIn] Interruption déclenchée");
    this._client._ws.send(JSON.stringify({ type: "interrupt" }));
    this._client.stopAudio();
    this._client._setSphereState("LISTENING");
    showVoiceStatus("...");
    this._voiceStart = null;
  }

  stop() {
    this._active = false;
    if (this._rafId) { cancelAnimationFrame(this._rafId); this._rafId = null; }
    this._analyser = null;
  }
}

// ── VoiceClient ───────────────────────────────────────────────────────────────
class VoiceClient {
  constructor() {
    this._ws        = null;
    this._ctx       = null;
    this._worklet   = null;
    this._stream    = null;
    this._sessionId = localStorage.getItem("L.Y.R.A_voice_session") || null;

    // Mute pendant le TTS pour éviter l'écho
    this._muted = false;

    // File d'attente audio WAV (chunks Piper, joués dans l'ordre)
    this._audioQueue    = [];
    this._isPlaying     = false;
    this._isSpeaking    = false;   // true quand l'audio TTS est en cours de lecture
    this._ttsServerDone = false;   // true quand le serveur a envoyé tts_done

    // Références pour pouvoir stopper l'audio en cours
    this._currentAudioCtx = null;
    this._currentSource   = null;

    this._bargeIn   = null;
    this._botBubble = null;

    // UI — le bouton perm-microphone (bas-gauche) est le seul toggle voix
    this._btn      = document.getElementById("perm-microphone");
    this._statusEl = null;  // pas d'élément texte séparé

    // Exposer l'interface window.L.Y.R.A pour index.html et panel.js
    window.L.Y.R.A = {
      get isSpeaking() { return window._voiceClient?._isSpeaking ?? false; },
      stopAudio:  () => window._voiceClient?.stopAudio(),
      setState:   (s) => window._voiceClient?._setSphereState(s),
    };
  }

  // ── Lifecycle ────────────────────────────────────────────────────────────

  async _start() {
    this._ws = await this._connect();

    this._stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        sampleRate: 16000,
        channelCount: 1,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    });

    this._ctx = new AudioContext({ sampleRate: 16000 });
    await this._ctx.audioWorklet.addModule("/audio-processor.js");

    const src     = this._ctx.createMediaStreamSource(this._stream);
    this._worklet = new AudioWorkletNode(this._ctx, "audio-processor");
    this._worklet.port.onmessage = (e) => this._onChunk(e.data);
    src.connect(this._worklet);

    // BargeInDetector tap on the same mic stream
    this._bargeIn = new BargeInDetector(this);
    this._bargeIn.start(this._ctx, this._stream);

    this._setState("listening");
  }

  _stop() {
    this._bargeIn?.stop();
    this._worklet?.disconnect();
    this._stream?.getTracks().forEach((t) => t.stop());
    this._ctx?.close();
    this._ws?.close();

    this.stopAudio();

    this._bargeIn = null;
    this._worklet = null;
    this._stream  = null;
    this._ctx     = null;
    this._ws      = null;
    this._muted   = false;
    this._setState("idle");
    showVoiceStatus("");

    // Synchronise le bouton perm et l'état global
    if (window._perms) window._perms.microphone = false;
    document.getElementById("perm-microphone")?.classList.remove("active");
  }

  // ── WebSocket ────────────────────────────────────────────────────────────

  _connect() {
    return new Promise((resolve, reject) => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      const sid   = this._sessionId ? `?session_id=${encodeURIComponent(this._sessionId)}` : "";
      const ws    = new WebSocket(`${proto}://${location.host}/ws/voice${sid}`);
      ws.binaryType = "arraybuffer";
      ws.onopen    = () => resolve(ws);
      ws.onerror   = reject;
      ws.onmessage = (e) => this._onMessage(e);
      ws.onclose   = () => { if (this._stream) this._stop(); };
    });
  }

  // ── Envoi PCM au serveur ─────────────────────────────────────────────────

  _onChunk(buffer) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN || this._muted) return;
    this._ws.send(buffer);
  }

  // ── Messages WebSocket ───────────────────────────────────────────────────

  _onMessage(e) {
    if (e.data instanceof ArrayBuffer) {
      this._audioQueue.push(e.data);
      this._playNext();
      return;
    }

    const msg = JSON.parse(e.data);

    // Dispatch vers index.html pour la sphère Text WebSocket (sync entre les deux WS)
    window.dispatchEvent(new CustomEvent("L.Y.R.A:ws", { detail: msg }));

    // États sphère
    if (VOICE_STATE_MAP[msg.type]) {
      this._setSphereState(VOICE_STATE_MAP[msg.type]);
    }

    switch (msg.type) {

      case "vad_start":
        showVoiceStatus("...");
        break;

      case "transcript":
        if (msg.text) addMsg("vous", msg.text);
        break;

      case "stt_done":
        showVoiceStatus(msg.transcript || "", 2000);
        this._setState("processing");
        break;

      case "llm_start":
        showVoiceStatus("L.Y.R.A réfléchit...");
        break;

      case "tts_start":
        this._muted = true;   // mute le micro pendant la lecture TTS
        break;

      case "start":
        this._sessionId = msg.session_id;
        localStorage.setItem("L.Y.R.A_voice_session", this._sessionId);
        this._botBubble = addMsg("L.Y.R.A", "", true);
        this._setState("responding");
        break;

      case "chunk":
        if (this._botBubble) {
          this._botBubble.textContent += msg.content;
          const chat = document.getElementById("chat");
          if (chat) chat.scrollTop = chat.scrollHeight;
        }
        break;

      case "done":
        if (this._botBubble && typeof checkForMindmap === 'function') checkForMindmap(this._botBubble);
        this._botBubble?.classList.remove("streaming");
        this._botBubble = null;
        break;

      case "tts_done":
        this._ttsServerDone = true;
        // Si aucun audio n'est en cours de lecture → unmute immédiatement
        if (!this._isPlaying && this._audioQueue.length === 0) {
          this._finalizeTts();
        }
        break;

      case "interrupted":
        this.stopAudio();
        this._botBubble?.classList.remove("streaming");
        this._botBubble = null;
        this._muted  = false;
        this._isSpeaking = false;
        showVoiceStatus("...");
        this._setState("listening");
        break;

      case "notification":
        addMsg("L.Y.R.A", msg.content, false, true);
        break;

      case "error":
        this._botBubble?.classList.remove("streaming");
        this._botBubble = null;
        this._muted     = false;
        this._isSpeaking = false;
        addMsg("L.Y.R.A", msg.content || "Erreur vocale.");
        this._setState("listening");
        showVoiceStatus("");
        break;
    }
  }

  // ── Lecture audio ordonnée ───────────────────────────────────────────────

  _playNext() {
    if (this._isPlaying || this._audioQueue.length === 0) return;
    this._isPlaying = true;
    const buffer = this._audioQueue.shift();

    this._currentAudioCtx = new AudioContext({ sampleRate: 22050 });
    this._currentAudioCtx.decodeAudioData(buffer)
      .then((decoded) => {
        this._currentSource = this._currentAudioCtx.createBufferSource();
        this._currentSource.buffer = decoded;
        this._currentSource.connect(this._currentAudioCtx.destination);

        this._currentSource.onended = () => {
          this._currentAudioCtx?.close();
          this._currentAudioCtx = null;
          this._currentSource   = null;
          this._isPlaying       = false;

          if (this._audioQueue.length === 0 && this._ttsServerDone) {
            // Dernier chunk terminé ET serveur done → unmute proprement
            this._finalizeTts();
          } else {
            this._playNext();
          }
        };

        this._currentSource.start();
        this._isSpeaking = true;
      })
      .catch((err) => {
        console.error("Audio decode error:", err);
        this._currentAudioCtx?.close();
        this._currentAudioCtx = null;
        this._isPlaying = false;
        this._playNext();
      });
  }

  _finalizeTts() {
    this._ttsServerDone = false;
    this._isSpeaking    = false;
    this._muted         = false;
    this._setState("listening");
    this._setSphereState("IDLE");
    showVoiceStatus("");
  }

  // ── Stop audio immédiat (barge-in) ───────────────────────────────────────

  stopAudio() {
    try { this._currentSource?.stop(); } catch (_) {}
    try { this._currentAudioCtx?.close(); } catch (_) {}
    this._currentSource   = null;
    this._currentAudioCtx = null;
    this._audioQueue      = [];
    this._isPlaying       = false;
    this._isSpeaking      = false;
    this._ttsServerDone   = false;
  }

  // ── État sphère ──────────────────────────────────────────────────────────

  _setSphereState(state) {
    // sphereState est un global de index.html
    if (typeof sphereState !== "undefined") {
      // eslint-disable-next-line no-global-assign
      sphereState = state;
    }
  }

  // ── État bouton micro ────────────────────────────────────────────────────

  _setState(state) {
    if (!this._btn) return;
    this._btn.dataset.state = state;
    // Le bouton reste visuellement actif tant que la voix tourne
    const voiceOn = state !== "idle" && state !== "error";
    this._btn.classList.toggle("active", voiceOn);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  window._voiceClient = new VoiceClient();
});
