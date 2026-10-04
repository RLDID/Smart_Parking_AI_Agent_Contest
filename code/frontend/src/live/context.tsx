import { createContext, useContext, useEffect, useRef, useState } from 'react';
import type { ParkingApi, Row } from './api';

export type LiveData = {
  readiness: Row | null; view: Row | null; map: Row | null;
  incidents: Row[]; devices: Row[]; vehicles: Row[];
  relationships: Row | null; notifications: Row[];
};
export type LiveContextValue = {
  api: ParkingApi; me: Row; role: 'owner' | 'driver'; facilityId: string;
  data: LiveData; epoch: number; revision: number; blocked: boolean;
  refresh: () => Promise<void>; go: (path: string) => void;
  toast: (message: string) => void; knownCommands: string[];
  rememberCommand: (id: string) => void;
};
export const LiveContext = createContext<LiveContextValue | null>(null);
export function useLive(): LiveContextValue {
  const value = useContext(LiveContext);
  if (!value) throw new Error('연결 화면의 컨텍스트가 필요합니다.');
  return value;
}
export function useResource(path: string | null) {
  const { api, epoch, revision } = useLive();
  const [state, setState] = useState<{ value: Row | null; error: string; loading: boolean }>({ value: null, error: '', loading: !!path });
  const [retry, setRetry] = useState(0);
  const serial = useRef(0);
  useEffect(() => {
    const ticket = ++serial.current;
    setState({ value: null, error: '', loading: !!path });
    if (path) void api.get(path).then(value => {
      if (ticket === serial.current) setState({ value, error: '', loading: false });
    }).catch((error: unknown) => {
      if (ticket !== serial.current || (error instanceof DOMException && error.name === 'AbortError')) return;
      setState({ value: null, error: error instanceof Error ? error.message : '정보를 가져오지 못했습니다.', loading: false });
    });
    return () => { serial.current++; };
  }, [api, path, epoch, revision, retry]);
  return { ...state, reload: () => setRetry(value => value + 1) };
}
