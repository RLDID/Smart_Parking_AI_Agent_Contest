import { useEffect, useRef, useState } from 'react';
import { Button } from '../components';
import { useLive } from './context';
import { AlarmTone, hasAudibleAlarm } from './alarmTone';

export function AlarmAudio() {
  const { api, data, facilityId, blocked } = useLive();
  const [tone] = useState(() => new AlarmTone());
  const [enabled, setEnabled] = useState(false);
  const [active, setActive] = useState(false);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState('');
  const alive = useRef(true); const intent = useRef(0);
  const runId = typeof data.readiness?.current_run_id === 'string' ? data.readiness.current_run_id : '';

  useEffect(() => () => { alive.current = false; intent.current++; tone.close(); }, [tone]);
  useEffect(() => {
    let valid = true; let polling = false; let lastRead = 0;
    setFresh(false); setActive(false); tone.stop();
    if (blocked || !runId || !enabled) return;
    async function read() {
      if (polling) return;
      polling = true;
      try {
        const state = await api.get(`/api/v1/facilities/${encodeURIComponent(facilityId)}/devices?run_id=${encodeURIComponent(runId)}`);
        if (!valid) return;
        if (state.run_id !== runId || !Array.isArray(state.alarms)) throw new Error('Invalid alarm state');
        lastRead = Date.now(); setFresh(true); setActive(hasAudibleAlarm(state, runId));
      } catch {
        if (valid) { setFresh(false); setActive(false); tone.stop(); }
      } finally { polling = false; }
    }
    void read();
    const poll = setInterval(() => void read(), 1000);
    const expiry = setInterval(() => {
      if (!lastRead || Date.now() - lastRead > 3500) { setFresh(false); tone.stop(); }
    }, 250);
    return () => { valid = false; clearInterval(poll); clearInterval(expiry); tone.stop(); };
  }, [api, facilityId, runId, blocked, enabled, tone]);

  const sounding = enabled && active && fresh && !blocked;
  useEffect(() => { if (sounding) tone.play(); else tone.stop(); return () => tone.stop(); }, [sounding, tone]);
  async function toggle() {
    const ticket = ++intent.current;
    if (enabled) { tone.stop(); setEnabled(false); return; }
    setError('');
    try {
      const ready = await tone.enable(() => {
        if (alive.current) { setEnabled(false); setError('브라우저 소리 출력이 중단됐어요. 경보음을 다시 켜 주세요.'); }
      });
      if (alive.current && ticket === intent.current) {
        setEnabled(ready); if (!ready) setError('브라우저 소리 허용을 확인해 주세요.');
      }
    } catch { if (alive.current && ticket === intent.current) setError('소리를 켜지 못했어요. 브라우저의 소리 권한을 확인해 주세요.'); }
  }
  return <div className="alarm-audio">
    <Button aria-pressed={enabled} onClick={() => void toggle()}>{enabled ? '경보음 끄기' : '경보음 켜기'}</Button>
    <span role="status">{error || (sounding ? '경보음 재생 중' : enabled ? blocked || !fresh ? '연결 확인 중 · 경보음 중지' : '경보음 켜짐 · 대기' : '경보음 꺼짐')}</span>
    <small>이 브라우저의 경보음</small>
  </div>;
}
