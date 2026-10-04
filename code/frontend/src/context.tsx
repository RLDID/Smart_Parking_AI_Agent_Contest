import { createContext, useContext } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { State } from './model';
export type ModalSpec = { type: 'device' | 'inbox' | 'guide' | 'report' | 'record' | 'link' | 'mapping'; id?: string; kind?: 'customers' | 'vehicles'; version?: number };
export interface AppContextValue {
  state: State; setState: Dispatch<SetStateAction<State>>; route: string;
  go: (route: string) => void; toast: (text: string) => void;
  open: (modal: ModalSpec) => void; close: () => void;
  draft: (key: string, defaults?: Record<string, string>) => Record<string, string>;
  editDraft: (key: string, field: string, value: string, defaults?: Record<string, string>) => void;
  clearDraft: (key: string) => void; blocked: boolean;
}
export const AppContext = createContext<AppContextValue | null>(null);
export function useApp() { const app = useContext(AppContext); if (!app) throw new Error('AppContext missing'); return app; }
