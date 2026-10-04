import { useEffect, useId, useRef, useState } from 'react';
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react';

export function Icon({ name }: { name: string }) {
  const shapes: Record<string, ReactNode> = {
    monitor: <><rect x="3" y="3" width="18" height="14"/><path d="M8 21h8M12 17v4M7 8h3v5H7zM14 7h3M14 11h3"/></>,
    incidents: <path d="M12 3 2 21h20L12 3zM12 9v5M12 17h.01"/>,
    commands: <><rect x="3" y="4" width="18" height="15"/><path d="m7 9 3 3-3 3M12 15h5"/></>,
    relationships: <><rect x="3" y="3" width="18" height="18"/><circle cx="9" cy="9" r="2"/><path d="M5 17v-1a4 4 0 0 1 8 0v1M15 8h3M15 12h3"/></>,
    car: <path d="m4 10 2-5h12l2 5M3 11h18v7H3zM5 18v2M19 18v2M6 14h2M16 14h2"/>,
    bell: <path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5l-2 3zM10 21h4"/>,
    arrow: <path d="M4 12h16m-6-6 6 6-6 6"/>,
    close: <path d="m6 6 12 12M18 6 6 18"/>,
    gate: <><path d="M7 12 20 5l1 2-14 7M11 10l1 2M16 7.5l1 2"/><rect x="3" y="11" width="4" height="10" rx="1"/><path d="M2 21h7"/></>,
    sound: <path d="M3 9h4l5-5v16l-5-5H3V9zM16 8a6 6 0 0 1 0 8M19 5a10 10 0 0 1 0 14"/>,
  };
  return <svg className="icon" viewBox="0 0 24 24" aria-hidden="true">{shapes[name] || shapes.monitor}</svg>;
}
export function Button({ variant = '', className = '', ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: string }) { return <button type="button" className={`btn ${variant} ${className}`} {...props}/>; }
export function RouteLink({ to, children, className = 'btn' }: { to: string; children: ReactNode; className?: string }) { return <a href={`#${to}`} className={className}>{children}</a>; }
export function Badge({ children, tone = '' }: { children: ReactNode; tone?: string }) { return <span className={`badge ${tone}`}>{children}</span>; }
export function Head({ title, action }: { title: string; action?: ReactNode }) { return <div className="page-head"><h1 tabIndex={-1}>{title}</h1>{action}</div>; }
export function Card({ title, action, children, body = true, footer }: { title?: string; action?: ReactNode; children: ReactNode; body?: boolean; footer?: ReactNode }) { return <section className="card">{title && <div className="card-head"><h2 tabIndex={-1}>{title}</h2>{action}</div>}{body ? <div className="card-body">{children}</div> : children}{footer && <div className="card-foot">{footer}</div>}</section>; }
export function Notice({ title, children, neutral = false }: { title: string; children?: ReactNode; neutral?: boolean }) { return <div className={`notice ${neutral ? 'neutral' : ''}`} role="status"><strong>{title}</strong>{children && <p>{children}</p>}</div>; }
export function Empty({ title, children }: { title: string; children?: ReactNode }) { return <div className="empty"><strong>{title}</strong>{children && <p>{children}</p>}</div>; }
export function Loading() { return <div className="card-body" role="status" aria-label="데이터 로딩 중" aria-busy="true"><div className="skeleton"/><div className="skeleton big"/></div>; }
export function Facts({ items }: { items: (string | ReactNode)[][] }) { return <div className="facts">{items.map(([label, value], i) => <div className="fact" key={i}><small>{label}</small><strong>{value}</strong></div>)}</div>; }
export function Timeline({ items }: { items: { title: string; text: string; pending?: boolean }[] }) { return <ol className="timeline">{items.map((item, i) => <li key={i} className={item.pending ? 'pending' : ''}><strong>{item.title}</strong><p>{item.text}</p></li>)}</ol>; }
type FieldProps = { label: string; error?: string; hint?: string } & ({ as?: 'input'; input?: InputHTMLAttributes<HTMLInputElement> } | { as: 'textarea'; input: TextareaHTMLAttributes<HTMLTextAreaElement> } | { as: 'select'; input: SelectHTMLAttributes<HTMLSelectElement>; children: ReactNode });
export function Field(props: FieldProps) {
  const generated = useId(); const id = props.input?.id || generated;
  const [touched, setTouched] = useState(false);
  const error = props.error || (touched && props.input?.required && !String(props.input.value || '').trim() ? `${props.label}을 입력해 주세요.` : '');
  const common = { id, 'aria-invalid': error ? true : undefined, 'aria-describedby': error ? `${id}-error` : props.hint ? `${id}-hint` : undefined, onBlur: () => setTouched(true) };
  return <div className="field"><label htmlFor={id}>{props.label}</label>{props.as === 'select' ? <select {...props.input} {...common}>{props.children}</select> : props.as === 'textarea' ? <textarea {...props.input} {...common}/> : <input {...props.input} {...common}/>} {error ? <small id={`${id}-error`} className="field-error" role="alert">{error}</small> : props.hint && <small id={`${id}-hint`}>{props.hint}</small>}</div>;
}
export function Dialog({ title, children, onClose, drawer = false }: { title: string; children: ReactNode; onClose: () => void; drawer?: boolean }) {
  const ref = useRef<HTMLDialogElement>(null); const id = useId();
  useEffect(() => { const origin = document.activeElement as HTMLElement | null; const d = ref.current!; d.showModal(); return () => { d.close(); if (origin?.isConnected) origin.focus(); }; }, []);
  return <dialog ref={ref} className={drawer ? 'drawer' : ''} aria-labelledby={id} onCancel={e => { e.preventDefault(); onClose(); }}><div className="dialog-head"><h2 id={id}>{title}</h2><Button aria-label="닫기" onClick={onClose}><Icon name="close"/></Button></div><div className="dialog-body">{children}</div></dialog>;
}
