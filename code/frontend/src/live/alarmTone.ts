export function hasAudibleAlarm(value: unknown, runId: string): boolean {
  if (!value || typeof value !== 'object') return false;
  const state = value as { run_id?: unknown; alarms?: unknown };
  return state.run_id === runId && Array.isArray(state.alarms) && state.alarms.some(alarm =>
    alarm && typeof alarm === 'object' && alarm.desired_active === true && alarm.audio === 'on');
}

/** Local browser sound only: never acknowledges a physical alarm or changes server state. */
export class AlarmTone {
  private context: AudioContext | null = null;
  private timer: ReturnType<typeof setInterval> | null = null;
  private voices = new Set<OscillatorNode>();
  async enable(onInterrupted?: () => void) {
    if (!this.context || this.context.state === 'closed') this.context = new AudioContext();
    const context = this.context;
    context.onstatechange = () => {
      if (this.context === context && context.state !== 'running') { this.stop(); onInterrupted?.(); }
    };
    await context.resume();
    return this.context === context && context.state === 'running';
  }
  play() {
    if (this.timer !== null) return;
    const pulse = () => {
      const context = this.context;
      if (!context || context.state !== 'running') { this.stop(); return; }
      const voice = context.createOscillator(); const gain = context.createGain();
      const at = context.currentTime;
      voice.frequency.value = 880;
      gain.gain.setValueAtTime(0, at);
      gain.gain.linearRampToValueAtTime(.08, at + .015);
      gain.gain.setValueAtTime(.08, at + .22);
      gain.gain.linearRampToValueAtTime(0, at + .25);
      voice.connect(gain); gain.connect(context.destination); this.voices.add(voice);
      voice.onended = () => { voice.disconnect(); gain.disconnect(); this.voices.delete(voice); };
      voice.start(at); voice.stop(at + .27);
    };
    pulse();
    if (this.context?.state === 'running') this.timer = setInterval(pulse, 750);
  }
  stop() {
    if (this.timer !== null) clearInterval(this.timer);
    this.timer = null;
    for (const voice of this.voices) { try { voice.stop(); } catch { /* Already ended. */ } voice.disconnect(); }
    this.voices.clear();
  }
  close() {
    this.stop(); const context = this.context; this.context = null;
    if (context) context.onstatechange = null;
    if (context && context.state !== 'closed') void context.close().catch(() => {});
  }
}
